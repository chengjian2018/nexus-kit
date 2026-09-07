"""Host assembly root: FastAPI app + endpoints + channel wiring.

Differences from the old repo-root main.py:
- session governance (TTL/LRU) lives in host.governor.SessionGovernor;
- config is injected once at import (host.config -> nexus.settings);
- discovery scans apps/ (patterns + business channels) and atoms/ (tools,
  providers, generic channels) instead of the old flat packages.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import fastapi
from pydantic import BaseModel

import host.config  # noqa: F401 -- side effect: inject config path into nexus.settings
from nexus.channels.base import EngineOps
from nexus.channels.webhooks import build_channel_routers
from nexus.engine.chat import chat
from nexus.engine.session import Session
from nexus.engine.store import SessionStore
from nexus.registry.channels import discover_builtin_channels
from nexus.registry.patterns import discover_builtin_patterns, registry as pattern_registry
from nexus.registry.tools import discover_builtin_tools
from nexus.settings import get_session_db_path, load_config
from host.governor import SessionGovernor

logger = logging.getLogger(__name__)


# ----init----
app = fastapi.FastAPI()
discover_builtin_tools()
discover_builtin_patterns()

# Session governance (TTL expiry + LRU cap); tunable via governor.ttl_seconds
# / governor.max_sessions, replaceable wholesale in tests.
governor = SessionGovernor()

# Session persistence store (SQLite audit + restart restore); initialized at
# startup, replaceable in tests.
# None = not enabled (degraded: dialogue works, no audit / no restore)
store: Optional[SessionStore] = None


def _restore_sessions() -> int:
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
        active_sessions = store.load_active_sessions(governor.ttl_seconds)
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


def _init_store() -> None:
    """Initialize the session persistence store; on failure degrade to None (dialogue works, audit/restore disabled)."""
    global store
    try:
        db_path = get_session_db_path()
        store = SessionStore(db_path)
        logger.info("会话存储已启用: %s", db_path)
    except Exception:
        logger.exception("初始化会话存储失败，审计与重启恢复降级")
        store = None


@app.on_event("startup")
def _startup_persistence() -> None:
    """Service startup: initialize the session store + restore non-expired sessions + cross-check pattern_llm."""
    _init_store()
    try:
        _restore_sessions()
    except Exception:
        logger.exception("重启恢复失败，跳过恢复")
    # Warm up the knowledge-base connection (moves the tools' lazy-init
    # fallback to startup); failure does not block the service.
    try:
        from atoms.knowledge.store import get_knowledge_store
        kb = get_knowledge_store()
        logger.info("知识库已启用: %s", kb._conn and "ok")
    except Exception:
        logger.exception("初始化知识库失败，知识工具将在首次调用时重试")
    _cross_check_pattern_llm()


@app.on_event("shutdown")
def _shutdown_stores() -> None:
    """Service shutdown: release the knowledge-base / session-store connections."""
    global store
    try:
        from atoms.knowledge.store import close_knowledge_store
        close_knowledge_store()
    except Exception:
        logger.exception("关闭知识库失败")
    if store is not None:
        try:
            store.close()
        except Exception:
            logger.exception("关闭会话存储失败")
        store = None


class DialogueRequest(BaseModel):
    request_id: str
    session_id: str
    pattern_code: str
    task_info: Dict[str, str]


class DialogueResponse(BaseModel):
    code: str
    message: str
    status: bool


class ChatRequest(BaseModel):
    request_id: str
    session_id: str
    query: str


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

def _launch_session_core(
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
    # attach runs unconditionally — if create_session failed, sink write
    # failures are swallowed anyway, so no messages are lost once the DB
    # recovers mid-way.
    if store is not None:
        try:
            store.create_session(session)
        except Exception:
            logger.exception("会话落盘失败: session=%s", session_id)
        store.attach(session)

    return session, "0", f"对话任务发起成功: session_id={session_id}"


def _get_session(session_id: str) -> Optional[Session]:
    """Governance-aware session lookup (purge expired + sliding renewal)."""
    return governor.get(session_id)


def _run_chat_turn_core(
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
        response_text = chat(
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
            store.save_snapshot(session)
        except Exception:
            logger.exception("会话轮末快照失败: session=%s", session.session_id)

    if error is not None:
        return None, error
    return response_text, None


# func1
@app.post("/api/v1/launch")
def launch_dialogue(dialogue_request: DialogueRequest) -> DialogueResponse:
    _session, code, message = _launch_session_core(
        pattern_code=dialogue_request.pattern_code,
        session_id=dialogue_request.session_id,
        task_info=dialogue_request.task_info,
        request_id=dialogue_request.request_id,
    )
    return DialogueResponse(code=code, status=(code == "0"), message=message)


# func2
@app.post("/api/v1/chat")
def chat_dialogue(chat_request: ChatRequest) -> ChatResponse:
    session = _get_session(chat_request.session_id)
    if session is None:
        return ChatResponse(
            code="404",
            status=False,
            message=f"session_id '{chat_request.session_id}' 不存在或已过期，请先发起对话任务",
        )

    response_text, error = _run_chat_turn_core(session, chat_request.query)

    if error is not None:
        return ChatResponse(
            code="500",
            status=False,
            message=f"对话处理异常: {error}",
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
def list_sessions(
    pattern_code: str = "", limit: int = 50, offset: int = 0
) -> SessionListResponse:
    if store is None:
        return SessionListResponse(code="500", status=False, message="会话存储未启用")

    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    try:
        sessions = store.list_sessions(
            pattern_code=pattern_code or None, limit=limit, offset=offset
        )
    except Exception as e:
        logger.exception("查询会话列表失败")
        return SessionListResponse(code="500", status=False, message=f"查询会话列表失败: {e}")

    return SessionListResponse(
        code="0", status=True, message="success", data={"sessions": sessions}
    )


# func4 (read-only audit)
@app.get("/api/v1/sessions/{session_id}/messages")
def get_session_messages(session_id: str) -> SessionMessagesResponse:
    if store is None:
        return SessionMessagesResponse(code="500", status=False, message="会话存储未启用")

    try:
        messages = store.get_messages(session_id)
    except Exception as e:
        logger.exception("查询会话消息失败")
        return SessionMessagesResponse(code="500", status=False, message=f"查询会话消息失败: {e}")

    if messages is None:
        return SessionMessagesResponse(
            code="404", status=False, message=f"session_id '{session_id}' 不存在"
        )

    return SessionMessagesResponse(
        code="0", status=True, message="success", data={"messages": messages}
    )
