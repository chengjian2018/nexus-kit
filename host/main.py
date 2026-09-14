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
# Logging switch: every module in the project only calls getLogger without
# configuring a handler — unconfigured, all INFO/DEBUG would be swallowed.
# NEXUS_LOG=INFO / DEBUG exposes session turns, MCP connections, tool
# dispatch; under DEBUG the httpx/httpcore/mcp.client noise is pushed back
# to WARNING (uvicorn's access logs are unaffected)
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

# Studio console-managed artifacts (ui/studio): generated plugin modules +
# console pattern YAMLs under host/config/{plugins,patterns}. Replayed after
# the builtin discovery — plugins before patterns (pattern validation resolves
# plugin codes), console patterns after code patterns (same-code console
# version wins, the fork-to-edit semantics).
from ui.studio.store import load_console_artifacts  # noqa: E402

load_console_artifacts()

# RAG retrieval config (ops-console): applied when host/config/rag.yaml
# exists (declarative assembly of the clarify recall pipeline); a missing
# file = builtin defaults, zero behavior change. A broken file only logs
# ERROR and never drags down the dialogue service (see
# atoms/stages/rag_config.py).
from atoms.stages.rag_config import load_and_apply_rag_config  # noqa: E402

load_and_apply_rag_config()


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
    into node_map; unregistered patterns are skipped with a
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
    # Cron scheduler: restore persisted jobs + start the tick loop on the
    # host loop (no-op under NEXUS_CRON_DISABLED=1)
    try:
        from atoms.tools._cron_core import ensure_scheduler
        await ensure_scheduler()
    except Exception:
        logger.exception("cron 调度器启动失败（create_cron 仍可用，但不会自动触发）")


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
    # Cron scheduler: cancel the tick loop (in-flight fires finish their
    # current record write; persisted next_fire_at re-arms on next boot)
    try:
        from atoms.tools._cron_core import stop_scheduler
        await stop_scheduler()
    except Exception:
        logger.exception("关闭 cron 调度器失败")


class DialogueRequest(BaseModel):
    request_id: str = Field(max_length=128)
    session_id: str = Field(max_length=256)
    pattern_code: str = Field(max_length=128)
    # 值放宽为任意 JSON 标量/数组：install/repair 预约类应用的
    # available_slots 是 "YYYY-MM-DD HH:MM-HH:MM" 字符串列表（纯字符串
    # 值的应用不受影响；store 快照走 json.dumps 天然兼容）
    task_info: Dict[str, Any]


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
    current_node_code: Optional[str] = None
    graph_state: Dict[str, Any] = {}
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
    task_info: Dict[str, Any],
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
        # Audit finding M-1: sink failures never block the dialogue, but the
        # silent degradation must stay visible — sessions with known missing
        # rows get one warning per turn (compression also abandons them on
        # the count mismatch)
        if session.cxt.sink_failure_count > 0:
            logger.warning(
                "session=%s 存在未落库消息（本进程内 sink 失败 %d 次，"
                "重启后该段对话将丢失）",
                session.session_id, session.cxt.sink_failure_count,
            )

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
        # External sanitization: exception details may carry path/config/SQL
        # information — return a uniform message only (details already logged)
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


# func2b (streaming chat via SSE — the studio 模版测试 page's dialogue
# channel; formerly a NEXUS_STREAM_DEBUG-gated debug tool, now first-class)
async def _chat_dialogue_stream(chat_request: ChatRequest):
    """SSE streaming chat: forwards the engine's chat_turn_stream events.

    Always mounted (the same NEXUS_API_KEY middleware covers it as any
    /api/v1/* endpoint; ops-console-prd §6.8 anticipated this promotion).
    An async generator driving the async engine core; the per-session
    turn_lock is held across yields by design: same-session requests queue
    as tasks while the loop keeps serving other sessions.

    Event stream (text/event-stream, one JSON payload per line):
        data: {"kind": "delta", "text": "..."}
        data: {"kind": "round", "round_info": {"outcome": "tool|final|...", "round_idx": n}}
        data: {"kind": "trace", "trace": {"event": "node_start", "node_code": ...}}
        data: {"kind": "done", "result": {"text": "...", "actions": [...]}}

    End-of-turn audit parity with /api/v1/chat (_run_chat_turn_core): the
    state snapshot persists after the stream ends (success, error, or client
    disconnect alike) — without it streaming turns would lose restart-restore
    state while /api/v1/chat keeps it.
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
                    elif event.kind == "trace":
                        yield "data: " + _json.dumps({
                            "kind": "trace",
                            "trace": (event.trace.to_dict()
                                      if event.trace is not None else {}),
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
        finally:
            # Audit parity with _run_chat_turn_core: persist the end-of-turn
            # snapshot whatever way the stream ended (done / error / client
            # disconnect). The lock is already released here.
            if store is not None:
                try:
                    await store.save_snapshot(session)
                except Exception:
                    logger.exception(
                        "会话轮末快照失败: session=%s", session.session_id)
                if session.cxt.sink_failure_count > 0:
                    logger.warning(
                        "session=%s 存在未落库消息（本进程内 sink 失败 %d 次，"
                        "重启后该段对话将丢失）",
                        session.session_id, session.cxt.sink_failure_count,
                    )

    return fastapi.responses.StreamingResponse(
        _gen(), media_type="text/event-stream")


app.post("/api/v1/chat/stream")(_chat_dialogue_stream)


# ----Channel wiring (external message sources -> engine ops)----
# AST-discovers the declarative channels in atoms/channels/*.py and apps/*/
# (token / default pattern come from each channel's declared env vars,
# re-read on every request and thus hot-reloadable); a generic handler
# generates the routers. The handler resolves the spec from the registry
# per request, so a hot-reloaded channel spec takes effect without a router
# rebuild (see nexus/channels/webhooks.py).
discover_builtin_channels()
for _router in build_channel_routers(EngineOps(
    get_session=_get_session,
    launch_session=_launch_session_core,
    run_chat_turn=_run_chat_turn_core,
)):
    app.include_router(_router)


# ---------------------------------------------------------------------------
# Ops-console (ui/, PRD: docs/design/ops-console-prd.md P0) — the ops
# configuration console. API mounted at /api/v1/console/* (covered by the
# NEXUS_API_KEY middleware above, same auth as the core API); the
# build-less static frontend mounts at /console (the page shell itself
# holds no sensitive data).
# ---------------------------------------------------------------------------
from fastapi.staticfiles import StaticFiles  # noqa: E402 -- assembled together with the console

import ui.api as _console  # noqa: E402

app.include_router(_console.router)
app.mount(
    "/console",
    StaticFiles(directory=str(_console.static_dir()), html=True),
    name="console",
)


# ---------------------------------------------------------------------------
# Studio (ui/studio) — the orchestration workbench (自动编排 / 流程编排 /
# 模版测试), a standalone page independent of the ops console above. API at
# /api/v1/studio/* (same NEXUS_API_KEY middleware); build-less static
# frontend at /studio. Its console-managed artifacts (hosted plugin modules
# + pattern YAMLs) were loaded at import time above and are replayed by the
# /api/v1/reload endpoint.
# ---------------------------------------------------------------------------
import ui.studio as _studio  # noqa: E402
import ui.studio.api as _studio_api  # noqa: E402

app.include_router(_studio_api.router)
app.mount(
    "/studio",
    StaticFiles(directory=str(_studio.static_dir()), html=True),
    name="studio",
)


# ---------------------------------------------------------------------------
# Hot reload — llm config cache invalidation + reload of the
# pattern/plugin/channel code modules. Coverage and boundaries: see the
# host/reload.py module docstring (tools/MCP/providers are out of scope —
# restart the process when needed). With NEXUS_API_KEY unset this endpoint
# is unauthenticated just like the core API (same once-per-minute warning).
# ---------------------------------------------------------------------------

@app.post("/api/v1/reload")
async def reload_modules() -> DialogueResponse:
    """Reload changed pattern / plugin / channel modules + invalidate the
    llm config cache.

    Afterwards rebinds in-memory sessions to the registry's latest pattern
    objects (in-flight turns holding old references finish on the old
    topology, unaffected).
    """
    from host.reload import reload_all, rebind_sessions

    result = reload_all()
    # Replay the studio hosted dirs: reload_all re-imports code modules and
    # re-registers code patterns, which would silently overwrite the console
    # versions — replay restores them (ops-console-prd risk R-2 mitigation).
    from ui.studio.store import load_console_artifacts

    studio_report = load_console_artifacts()
    rebound = rebind_sessions(governor.sessions, pattern_registry)
    changed = result.get("changed") or []
    failed = result.get("failed") or []
    studio_failed = (len(studio_report["plugins"]["failed"])
                     + len(studio_report["patterns"]["failed"]))
    if failed or studio_failed:
        message = (f"重载完成：变更 {len(changed)} 个，失败 {len(failed)} 个"
                   f"（保持旧注册）: {failed}；studio 托管产物失败 "
                   f"{studio_failed} 个；会话重绑 {rebound} 个")
    else:
        message = (f"重载完成：变更 {len(changed)} 个模块"
                   f"{'（无变更）' if not changed else ''}；会话重绑 {rebound} 个")
    logger.info("[reload] %s", message)
    return DialogueResponse(code="0", status=True, message=message)


# ---------------------------------------------------------------------------
# System hot-reload surface (studio「系统插件」页)——/api/v1/reload 之外的可
# 视化选择性重载面。插件按归属模块重载：studio 托管插件按文件重放（自包含
# 模块，store.import_plugin_module 自带 replace 窗口）；代码插件走
# host.reload.reload_modules（所选模块 + 其 consumer 按依赖序重放）。MCP 工
# 具跟随连接生命周期而非文件 mtime：reload = 重读配置 → shutdown（注销
# mcp-* 工具）→ 按新配置重建连接重注册。
# ---------------------------------------------------------------------------

_system_reload_lock: Optional[asyncio.Lock] = None

# 系统面展示 / 可选择重载的插件 kind（与 plugin_registry 的业务 kind 集合一致）
_SYSTEM_PLUGIN_KINDS = ("executor", "stage", "messages_builder", "agent_hooks")


def _plugin_source(owner: str) -> Tuple[str, str]:
    """(source, module)：归属模块名 → 展示来源。

    ``studio_plugin_<stem>`` → studio 托管（module = stem，按文件重放）；
    ``apps.*`` / ``atoms.executors.*`` → code（module = 模块名，走依赖序
    重放）；其余（nexus.* 内核默认实现）→ kernel，不可热重载。
    """
    if owner.startswith("studio_plugin_"):
        return "studio", owner[len("studio_plugin_"):]
    if owner.startswith(("apps.", "atoms.")):
        return "code", owner
    return "kernel", owner


def _system_payload() -> Dict[str, Any]:
    """系统面状态数据：插件（kind/code/来源/归属模块）+ MCP server 连接
    列表 + ToolRegistry toolset 概览。"""
    from atoms.mcp.manager import get_mcp_manager
    from nexus.registry.plugins import registry as plugin_registry
    from nexus.registry.tools import registry as tool_registry

    plugins = []
    for kind in _SYSTEM_PLUGIN_KINDS:
        for code in plugin_registry.list_codes(kind):
            source, module = _plugin_source(plugin_registry.owner_of(kind, code))
            plugins.append({"kind": kind, "code": code,
                            "source": source, "module": module})
    return {"plugins": plugins,
            "servers": get_mcp_manager().list_servers(),
            "toolsets": tool_registry.get_available_toolsets()}


@app.get("/api/v1/system/status")
def system_status() -> Dict[str, Any]:
    """系统面状态：插件（kind/code/来源/归属模块）+ MCP server 连接态 +
    toolset 概览（studio「系统插件」页数据源）。"""
    return {"code": "0", "status": True, "message": "success",
            "data": _system_payload()}


class SystemReloadIn(BaseModel):
    # 勾选的插件引用列表（"kind:code" 形态，UI 复选框直接产出）
    plugin_codes: List[str] = Field(default_factory=list, max_length=200)
    mcp: bool = False


@app.post("/api/v1/system/reload")
async def system_reload(body: SystemReloadIn) -> Dict[str, Any]:
    """选择性热重载：勾选的插件（按归属分流）+ 可选的 MCP 工具面。

    - studio 托管插件：按 stem 从托管目录重放（自包含模块，重新 exec +
      replace 窗口重注册）；
    - 代码插件（apps.* / atoms.executors.*）：host.reload.reload_modules
      依赖序重放所选模块及其 consumer，完成后重绑内存会话（与 /reload
      语义一致：进行中的轮次持旧引用跑完）；
    - mcp：重读 mcp_servers 配置 → 断开重连 → 重注册 mcp-* 工具。配置
      先读后拆——非法配置在拆掉现有连接之前就失败，保持现状可用。
    """
    global _system_reload_lock
    from host.reload import reload_modules, rebind_sessions
    from nexus.registry.plugins import registry as plugin_registry
    from ui.studio import store as studio_store

    if not body.plugin_codes and not body.mcp:
        return {"code": "400", "status": False, "message": "未选择任何重载目标"}

    if _system_reload_lock is None:
        _system_reload_lock = asyncio.Lock()
    async with _system_reload_lock:
        report: Dict[str, Any] = {}
        studio_stems: List[str] = []
        code_modules: set = set()
        skipped: List[str] = []
        for ref in body.plugin_codes:
            kind, _, code = ref.partition(":")
            owner = plugin_registry.owner_of(kind, code)
            if owner.startswith("studio_plugin_"):
                studio_stems.append(owner[len("studio_plugin_"):])
            elif owner.startswith(("apps.", "atoms.")):
                code_modules.add(owner)
            else:
                skipped.append(f"{ref}（未注册或内核实现，不可热重载）")

        if studio_stems:
            reloaded_stems, failed_stems = [], []
            for stem in studio_stems:
                path = studio_store.PLUGINS_DIR / f"{stem}.py"
                try:
                    studio_store.import_plugin_module(path)
                    reloaded_stems.append(stem)
                except Exception as e:  # noqa: BLE001 -- 单文件失败不拦其余
                    logger.exception("studio 插件重放失败: %s", stem)
                    failed_stems.append(f"{stem}: {e}")
            report["studio_plugins"] = {"reloaded": reloaded_stems,
                                        "failed": failed_stems}
        if code_modules:
            result = reload_modules(sorted(code_modules))
            report["code_modules"] = dict(
                result, sessions_rebound=rebind_sessions(
                    governor.sessions, pattern_registry))
        if skipped:
            report["skipped"] = skipped

        if body.mcp:
            from atoms.mcp.manager import get_mcp_manager
            from nexus.settings import get_mcp_servers, invalidate_config_cache

            try:
                invalidate_config_cache()
                servers_cfg = get_mcp_servers()
            except Exception as e:
                return {"code": "400", "status": False,
                        "message": f"mcp_servers 配置非法，工具面未重载"
                                   f"（保持现状）: {e}"}
            manager = get_mcp_manager()
            await manager.shutdown()
            manager.bootstrap(servers_cfg)
            await manager.ensure_started()
            await manager.wait_ready(timeout=30.0)
            servers = manager.list_servers()
            report["mcp"] = {"servers": servers,
                             "ready": sum(1 for s in servers if s.get("ready"))}

    parts: List[str] = []
    sp = report.get("studio_plugins")
    if sp:
        parts.append(f"studio 插件重放 {len(sp['reloaded'])} 个"
                     + (f"、失败 {len(sp['failed'])}" if sp["failed"] else ""))
    cm = report.get("code_modules")
    if cm:
        parts.append(f"代码模块重放 {len(cm['reloaded'])} 个（会话重绑 "
                     f"{cm['sessions_rebound']} 个）"
                     + (f"、失败 {len(cm['failed'])}" if cm["failed"] else ""))
    mc = report.get("mcp")
    if mc:
        parts.append(f"MCP {mc['ready']}/{len(mc['servers'])} 就绪")
    if report.get("skipped"):
        parts.append(f"跳过 {len(report['skipped'])} 项")
    return {"code": "0", "status": True,
            "message": "系统面重载完成：" + "；".join(parts),
            "data": {"report": report, **_system_payload()}}


@app.on_event("startup")
async def _startup_reload_watcher():
    """建立热重载 mtime 基线（首个 /reload 即可检测开机以来的变更）；
    NEXUS_RELOAD_WATCH=1 时启动后台 watcher（开发期便利，默认关）。"""
    from host.reload import init_baseline, start_watcher

    init_baseline()
    if os.getenv("NEXUS_RELOAD_WATCH", "") == "1":
        start_watcher()


@app.on_event("shutdown")
async def _shutdown_reload_watcher():
    from host.reload import stop_watcher
    stop_watcher()


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

    # graph_state persists as a JSON TEXT column — decode for the typed
    # response model (a broken payload degrades to an empty state, never a 500)
    import json as _json

    for row in sessions:
        raw_state = row.get("graph_state")
        if isinstance(raw_state, str):
            try:
                row["graph_state"] = _json.loads(raw_state or "{}")
            except ValueError:
                row["graph_state"] = {}

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
