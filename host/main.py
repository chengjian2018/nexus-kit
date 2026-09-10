"""Host assembly root: FastAPI app + endpoints + channel wiring.

Differences from the old repo-root main.py:
- session governance (TTL/LRU) lives in host.governor.SessionGovernor;
- config is injected once at import (host.config -> nexus.settings);
- discovery scans apps/ (patterns + business channels) and atoms/ (tools,
  providers, generic channels) instead of the old flat packages.
"""

import asyncio
import hmac
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import fastapi
from pydantic import BaseModel, Field

import host.config  # noqa: F401 -- side effect: inject config path into nexus.settings
from nexus.channels.base import EngineOps
from nexus.channels.webhooks import build_channel_routers
from nexus.engine.chat import chat
from nexus.engine.session import Session
from nexus.engine.store import SessionStore
from nexus.registry.channels import discover_builtin_channels
from nexus.registry.patterns import discover_builtin_patterns, registry as pattern_registry
from nexus.registry.plugins import discover_builtin_plugins
from nexus.registry.tools import discover_builtin_tools
from nexus.settings import get_session_db_path, load_config
from host.governor import SessionGovernor

logger = logging.getLogger(__name__)


# ----init----
# 日志开关:项目各模块只 getLogger 不配 handler,不配置则 INFO/DEBUG 全部
# 被吞。NEXUS_LOG=INFO / DEBUG 可见会话轮次、MCP 连接、工具分派过程;
# DEBUG 下把 httpx/httpcore/mcp.client 噪声压回 WARNING(uvicorn 侧的
# 访问日志不受影响)
def _setup_logging() -> None:
    level_name = os.environ.get("NEXUS_LOG", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if level >= logging.DEBUG:
        for noisy in ("httpx", "httpcore", "mcp.client", "asyncio",
                      "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


_setup_logging()

app = fastapi.FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
discover_builtin_tools()
discover_builtin_patterns()
discover_builtin_plugins()


# ---------------------------------------------------------------------------
# API-key middleware for /api/v1/* core endpoints (channel endpoints keep their
# own per-channel token check). Enabled only when NEXUS_API_KEY is set to a
# non-empty value; when unset every request is allowed through but a warning is
# logged once per minute so a misconfigured deployment stays visible.
# ---------------------------------------------------------------------------

_API_KEY_WARN_INTERVAL_SECONDS = 60.0
_last_api_key_warn: float = 0.0


@app.middleware("http")
async def _api_key_guard(request, call_next):
    global _last_api_key_warn
    path = request.url.path
    if path.startswith("/api/v1/") and not path.startswith("/api/v1/channel/"):
        expected = os.getenv("NEXUS_API_KEY", "")
        if not expected:
            now = time.monotonic()
            if now - _last_api_key_warn >= _API_KEY_WARN_INTERVAL_SECONDS:
                _last_api_key_warn = now
                logger.warning(
                    "NEXUS_API_KEY 未设置，核心 API 处于无认证状态（请尽快配置）"
                )
        else:
            provided = (request.headers.get("X-API-Key") or
                        request.query_params.get("api_key") or "")
            if not hmac.compare_digest(provided, expected):
                return fastapi.responses.JSONResponse(
                    status_code=401,
                    content={"code": "401", "status": False,
                             "message": "API key 校验失败"},
                )
    return await call_next(request)

# Session governance (TTL expiry + LRU cap); tunable via governor.ttl_seconds
# / governor.max_sessions, replaceable wholesale in tests.
governor = SessionGovernor()

# Session persistence store (SQLite audit + restart restore); initialized at
# startup, replaceable in tests.
# None = not enabled (degraded: dialogue works, no audit / no restore)
store: Optional[SessionStore] = None


async def _restore_sessions() -> int:
    """Restore non-expired sessions from the store back into memory (restart
    restore).

    Patterns are re-resolved from the registry by pattern_code and injected
    into node_map/module_map; unregistered patterns are skipped with a
    warning. DB wall-clock times are converted onto the monotonic base.

    Returns:
        int: number of sessions actually restored
    """
    if store is None:
        return 0
    restored = 0
    now_wall = time.time()
    try:
        active_sessions = await store.load_active_sessions(governor.ttl_seconds)
    except Exception:
        logger.exception("加载未过期会话失败，跳过恢复")
        return 0
    for session, last_active_wall in active_sessions:
        try:
            pattern = pattern_registry.get(session.pattern_code)
            if pattern is None:
                logger.warning(
                    "恢复跳过会话 %s: pattern '%s' 未注册",
                    session.session_id,
                    session.pattern_code,
                )
                continue
            session.pattern = pattern
            session.cxt.module_map = pattern.module_map
            session.cxt.node_map = pattern.node_map
            store.attach(session)  # re-attach write-through for the restored session (before it enters memory)
            governor.adopt_restored(session, idle_seconds=now_wall - last_active_wall)
            restored += 1
        except Exception:
            logger.exception("恢复会话失败，跳过: session=%s", session.session_id)
            continue
    if restored:
        logger.info("重启恢复会话 %d 个", restored)
    return restored


def _cross_check_pattern_llm(config_path: str = "") -> None:
    """Cross-check that pattern_llm codes exist (spec §5): unknown ones only warn, never block."""
    try:
        pattern_llm = load_config(config_path).get("pattern_llm", {})
    except Exception:
        logger.exception("加载配置失败，跳过 pattern_llm 交叉校验")
        return
    for pcode, pcfg in pattern_llm.items():
        pattern = pattern_registry.get(pcode)
        if pattern is None:
            logger.warning("pattern_llm 配置了未注册的 pattern '%s'", pcode)
            continue
        for mcode in (pcfg.get("modules") or {}):
            if mcode not in pattern.module_map:
                logger.warning(
                    "pattern '%s' 的 pattern_llm.modules 配置了未注册 module '%s'",
                    pcode, mcode)
        for ncode in (pcfg.get("nodes") or {}):
            if ncode not in pattern.node_map:
                logger.warning(
                    "pattern '%s' 的 pattern_llm.nodes 配置了未注册 node '%s'",
                    pcode, ncode)


async def _init_store() -> None:
    """Initialize the session persistence store; on failure degrade to None (dialogue works, audit/restore disabled)."""
    global store
    try:
        db_path = get_session_db_path()
        store = await SessionStore.create(db_path)
        logger.info("会话存储已启用: %s", db_path)
    except Exception:
        logger.exception("初始化会话存储失败，审计与重启恢复降级")
        store = None


def _validate_registered_patterns() -> None:
    """Assembly-time validation (plan-③): every registered pattern passes
    base-info + plugin-declaration checks. Runs after the discovery warm-ups
    so plugin codes resolvable; a failure is a declaration bug, fail loudly
    (startup refuses to serve a mis-declared pattern set)."""
    from nexus.model.validation import validate_pattern

    for code in pattern_registry.list_codes():
        pattern = pattern_registry.get(code)
        try:
            validate_pattern(pattern)
        except ValueError as e:
            raise SystemExit(f"pattern 声明校验失败（启动终止）: {e}") from e
    logger.info("pattern 校验通过: %d 个", len(pattern_registry.list_codes()))


@app.on_event("startup")
async def _startup_persistence() -> None:
    """Service startup: initialize the session store + restore non-expired
    sessions + cross-check pattern_llm + start MCP connections."""
    # heavy/blocking warm-ups go through to_thread so the startup loop stays responsive
    await _init_store()
    try:
        await _restore_sessions()
    except Exception:
        logger.exception("重启恢复失败，跳过恢复")
    # Warm up the knowledge-base connection (moves the tools' lazy-init
    # fallback to startup); jieba first-load (~1s) off the loop.
    # Failure does not block the service.
    try:
        from atoms.knowledge.store import get_knowledge_store

        def _warm_kb():
            kb = get_knowledge_store()
            return kb._conn and "ok"
        logger.info("知识库已启用: %s",
                    await asyncio.to_thread(_warm_kb))
    except Exception:
        logger.exception("初始化知识库失败，知识工具将在首次调用时重试")
    await asyncio.to_thread(_cross_check_pattern_llm)
    await asyncio.to_thread(_validate_registered_patterns)
    # MCP: spawn server connections on the main loop (registered lazily by
    # ensure_mcp_ready too — this just front-loads the startup race)
    try:
        from atoms.mcp.manager import get_mcp_manager
        await get_mcp_manager().ensure_started()
    except Exception:
        logger.exception("MCP 连接启动失败（首轮对话会重试）")


@app.on_event("shutdown")
async def _shutdown_stores() -> None:
    """Service shutdown: release the knowledge-base / session-store / MCP connections."""
    global store
    try:
        from atoms.knowledge.store import close_knowledge_store
        await asyncio.to_thread(close_knowledge_store)
    except Exception:
        logger.exception("关闭知识库失败")
    if store is not None:
        try:
            await store.close()
        except Exception:
            logger.exception("关闭会话存储失败")
        store = None
    # MCP manager: close server connections (no-op when none configured;
    # the loop itself belongs to the host and is not stopped here)
    try:
        from atoms.mcp.manager import get_mcp_manager
        await get_mcp_manager().shutdown()
    except Exception:
        logger.exception("关闭 MCP 连接失败")


class DialogueRequest(BaseModel):
    request_id: str = Field(max_length=128)
    session_id: str = Field(max_length=256)
    pattern_code: str = Field(max_length=128)
    task_info: Dict[str, str]


class DialogueResponse(BaseModel):
    code: str
    message: str
    status: bool


class ChatRequest(BaseModel):
    request_id: str = Field(max_length=128)
    session_id: str = Field(max_length=256)
    query: str = Field(max_length=4000)


class ChatResponse(BaseModel):
    code: str
    message: str
    status: bool
    data: Dict[str, Any] = {}


class SessionSummary(BaseModel):
    session_id: str
    pattern_code: str
    current_module_code: Optional[str] = None
    current_node_code: Optional[str] = None
    message_count: int
    created_at: float
    last_active_at: float


class SessionListResponse(BaseModel):
    code: str
    message: str
    status: bool
    data: Dict[str, List[SessionSummary]] = {}


class MessageItem(BaseModel):
    id: int
    role: str
    content: str
    stage: str
    metadata: Dict[str, Any] = {}
    created_at: float
    action: Dict[str, str] = {}


class SessionMessagesResponse(BaseModel):
    code: str
    message: str
    status: bool
    data: Dict[str, List[MessageItem]] = {}


# ----Engine-op core (shared by endpoints and channels; main injects these functions into channels)----

async def _launch_session_core(
    pattern_code: str,
    session_id: str,
    task_info: Dict[str, str],
    request_id: str,
    exist_ok: bool = False,
) -> Tuple[Optional[Session], str, str]:
    """Launch core: pattern validation + session governance
    (purge/duplicate-check/eviction) + audit persistence.

    Args:
        exist_ok: when True, an already-existing session_id counts as success
            and returns the existing session (channel get-or-create
            semantics) without overwriting it.

    Returns:
        (session, code, message): code == "0" means success; on failure
        session is None and code/message carry the same semantics as the
        /api/v1/launch response.
    """
    pattern = pattern_registry.get(pattern_code)
    if pattern is None:
        return None, "404", (
            f"pattern_code '{pattern_code}' 未注册，已注册: {pattern_registry.list_codes()}"
        )

    session = Session(session_id=session_id, pattern_code=pattern_code)
    session.pattern = pattern
    session.task_info = task_info
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = task_info
    session.cxt.metadata["request_id"] = request_id

    inserted, existing = governor.register_new(session, exist_ok=exist_ok)
    if not inserted:
        if exist_ok:
            return existing, "0", f"session_id '{session_id}' 已存在，复用既有会话"
        return None, "409", (
            f"session_id '{session_id}' 已存在，请更换 session_id 重新发起"
        )

    # Audit persistence (after in-memory registration succeeded); failure is
    # only logged and never blocks launch.
    # attach only when create_session succeeded: a session with no sessions row
    # would make the sink write orphan message rows (epoch lookup falls back to
    # 0, and restart-restore never revives them — silent audit loss). Better to
    # lose persistence for a launch-time-broken DB than to write unreachable rows.
    if store is not None:
        try:
            await store.create_session(session)
            store.attach(session)
        except Exception:
            logger.exception("会话落盘失败（本轮关闭消息持久化）: session=%s", session_id)

    return session, "0", f"对话任务发起成功: session_id={session_id}"


def _get_session(session_id: str) -> Optional[Session]:
    """Governance-aware session lookup (purge expired + sliding renewal)."""
    return governor.get(session_id)


async def _run_chat_turn_core(
    session: Session, query: str
) -> Tuple[Optional[str], Optional[Exception]]:
    """Single chat-turn core: run chat + end-of-turn audit persistence.

    Runs outside the lock (LLM calls are slow and must not block other
    requests); chat re-fetches the session by session_id internally, and the
    local session reference here exists only for persisting the snapshot —
    even a concurrent eviction mid-turn does not affect this dialogue. The
    exception path persists too (same as the success path). Messages were
    already persisted per-message by the sink; end of turn only writes back
    the state snapshot.

    Returns:
        (reply, error): error is None on success; reply is None only on the
        exception path.
    """
    error: Optional[Exception] = None
    try:
        # Per-session serialization: concurrent turns on the same session_id
        # (buyer retries / channel replays) would otherwise interleave
        # begin_turn resets and history appends. Cross-session parallelism is
        # unaffected — each session holds only its own lock (asyncio.Lock:
        # waiters queue as tasks, the loop keeps serving other sessions).
        async with session.turn_lock:
            response_text = await chat(
                query=query,
                session_id=session.session_id,
                all_sessions=governor.sessions,
                store=store,
            )
    except Exception as e:
        logger.exception("对话处理异常")
        error = e

    if store is not None:
        try:
            await store.save_snapshot(session)
        except Exception:
            logger.exception("会话轮末快照失败: session=%s", session.session_id)

    if error is not None:
        return None, error
    return response_text, None


# func1
@app.post("/api/v1/launch")
async def launch_dialogue(dialogue_request: DialogueRequest) -> DialogueResponse:
    _session, code, message = await _launch_session_core(
        pattern_code=dialogue_request.pattern_code,
        session_id=dialogue_request.session_id,
        task_info=dialogue_request.task_info,
        request_id=dialogue_request.request_id,
    )
    return DialogueResponse(code=code, status=(code == "0"), message=message)


# func2
@app.post("/api/v1/chat")
async def chat_dialogue(chat_request: ChatRequest) -> ChatResponse:
    session = _get_session(chat_request.session_id)
    if session is None:
        return ChatResponse(
            code="404",
            status=False,
            message=f"session_id '{chat_request.session_id}' 不存在或已过期，请先发起对话任务",
        )

    response_text, error = await _run_chat_turn_core(session, chat_request.query)

    if error is not None:
        # 对外脱敏：异常细节可能含路径/配置/SQL 信息，只回统一话术（细节已进日志）
        return ChatResponse(
            code="500",
            status=False,
            message="对话处理异常，请稍后重试",
        )

    return ChatResponse(
        code="0",
        status=True,
        message="success",
        data={
            "request_id": chat_request.request_id,
            "session_id": chat_request.session_id,
            "response": response_text,
        },
    )


# func2b (debug-only, env-gated): streaming chat via SSE
async def _chat_dialogue_stream(chat_request: ChatRequest):
    """SSE debug endpoint for the plan-⑤ streaming protocol (async since
    the asyncio rewrite — an async generator driving the async engine core;
    the per-session turn_lock is held across yields by design: same-session
    requests queue as tasks while the loop keeps serving other sessions).

    Mounted only when NEXUS_STREAM_DEBUG=1 — a debugging/observability tool,
    not a production API.

    Event stream (text/event-stream, one JSON payload per line):
        data: {"kind": "delta", "text": "..."}
        data: {"kind": "round", "round_info": {...}}
        data: {"kind": "done", "result": {"text": "...", "actions": [...]}}
    """
    import json as _json

    from nexus.engine.chat import chat_turn_stream

    session = _get_session(chat_request.session_id)
    if session is None:
        return fastapi.responses.JSONResponse(
            status_code=404,
            content={"code": "404", "status": False,
                     "message": f"session_id '{chat_request.session_id}' 不存在或已过期"},
        )

    async def _gen():
        try:
            async with session.turn_lock:
                agen = chat_turn_stream(
                    query=chat_request.query,
                    session_id=chat_request.session_id,
                    all_sessions=governor.sessions,
                    store=store,
                )
                result = None
                async for event in agen:
                    if event.kind == "done":
                        result = event.result
                        yield "data: " + _json.dumps({
                            "kind": "done",
                            "result": {"text": result.text,
                                       "actions": result.actions},
                        }, ensure_ascii=False) + "\n\n"
                    elif event.kind == "round":
                        yield "data: " + _json.dumps({
                            "kind": "round",
                            "round_info": event.round_info,
                        }, ensure_ascii=False) + "\n\n"
                    else:
                        yield "data: " + _json.dumps({
                            "kind": "delta", "text": event.text,
                        }, ensure_ascii=False) + "\n\n"
                del result
        except Exception:
            logger.exception("流式对话异常")
            yield "data: " + _json.dumps({
                "kind": "error", "message": "对话处理异常，请稍后重试",
            }, ensure_ascii=False) + "\n\n"

    return fastapi.responses.StreamingResponse(
        _gen(), media_type="text/event-stream")


if os.getenv("NEXUS_STREAM_DEBUG", "") == "1":
    # Env-gated mount: the SSE debug endpoint exists only when explicitly
    # requested (keep the production surface minimal)
    app.post("/api/v1/chat/stream")(_chat_dialogue_stream)


# ----Channel wiring (external message sources -> engine ops)----
# AST-discovers the declarative channels in atoms/channels/*.py and apps/*/
# (token / default pattern come from each channel's declared env vars,
# re-read on every request and thus hot-reloadable); a generic handler
# generates the routers
discover_builtin_channels()
for _router in build_channel_routers(EngineOps(
    get_session=_get_session,
    launch_session=_launch_session_core,
    run_chat_turn=_run_chat_turn_core,
)):
    app.include_router(_router)


# func3 (read-only audit)
@app.get("/api/v1/sessions")
async def list_sessions(
    pattern_code: str = "", limit: int = 50, offset: int = 0
) -> SessionListResponse:
    if store is None:
        return SessionListResponse(code="500", status=False, message="会话存储未启用")

    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    try:
        sessions = await store.list_sessions(
            pattern_code=pattern_code or None, limit=limit, offset=offset
        )
    except Exception as e:
        logger.exception("查询会话列表失败")
        return SessionListResponse(code="500", status=False, message="查询会话列表失败，请稍后重试")

    return SessionListResponse(
        code="0", status=True, message="success", data={"sessions": sessions}
    )


# func4 (read-only audit)
@app.get("/api/v1/sessions/{session_id}/messages")
async def get_session_messages(session_id: str) -> SessionMessagesResponse:
    if store is None:
        return SessionMessagesResponse(code="500", status=False, message="会话存储未启用")

    try:
        messages = await store.get_messages(session_id)
    except Exception as e:
        logger.exception("查询会话消息失败")
        return SessionMessagesResponse(code="500", status=False, message="查询会话消息失败，请稍后重试")

    if messages is None:
        return SessionMessagesResponse(
            code="404", status=False, message=f"session_id '{session_id}' 不存在"
        )

    return SessionMessagesResponse(
        code="0", status=True, message="success", data={"messages": messages}
    )
