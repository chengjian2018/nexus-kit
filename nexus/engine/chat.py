"""
Dialogue processing — the turn orchestrator (plan-⑧: node+pattern 二层模型).

Responsibility is narrowed to "orchestrating one turn": locate the session →
cxt turn lifecycle → **dispatch by pattern_type** → produce a ChatResult.

- ``pattern_type == "fsm"``   : the FSM pipeline executor (plugins["fsm"] >
  default_fsm) runs the stages sequence (two-layer resolution node > pattern
  skeleton) and advances ONE node per turn via next_node (clarify turns
  skip; the terminal node writes conversation_end). No budget — cycles are
  natural semantics.
- ``pattern_type == "agent"`` : the graph runtime runs the WHOLE graph per
  user message from entry (a fresh run), or resumes from the suspended node
  when ``cxt.graph_state`` carries a wait_human cursor:
    - conditional edges = the node executor's routing output
      (``TurnResult.next``, mapped back onto the node's sub_nodes);
    - node executor resolution: node.plugins["loop"] >
      pattern.plugins["loop"] > default_loop (the ReAct tool loop);
    - step budget ``pattern.max_steps`` (one node execution per step; on
      exhaustion a force-close reply ends the run);
    - suspension: ``TurnResult.wait_human`` persists the cursor + step into
      cxt.graph_state (sessions-table graph_state column — process-restart
      safe) and ends the turn; the NEXT user message re-executes the paused
      node with the message as ``ec.resume_input`` (langgraph-interrupt
      style; side-effect idempotency is the executor's documented duty).
      A graph that never suspends completes within the single turn —
      "full re-run per message" and "cross-turn resumable workflow" are the
      same engine's emergent behaviors, no config switch.

This module keeps the kernel toolbox the executors import: the R1/R3/R4
_refresh_llm_config, _resolve_entry_node, _run_stages, and
_fsm_node_transition (patch anchors — tests patch
"nexus.engine.chat.get_llm_config").

The cxt field lifecycle (per-turn reset / cross-turn retention / incremental
update) is managed exclusively by context_lifecycle.TurnLifecycle; this
module only calls it at the right timing points.

Entries:
- chat_turn_stream() : async generator (protocol entry), yields
  ChatStreamEvent and terminates on done
- chat_turn() : aggregate of chat_turn_stream, returns ChatResult
- chat()      : compat entry (main.py / cli.py), equivalent to
  chat_turn().text
"""

import logging
from typing import TYPE_CHECKING, Dict, Optional

from nexus.settings import get_llm_config
from nexus.engine.compression import maybe_compress
from nexus.engine.context_lifecycle import TurnLifecycle
from nexus.engine.execution import ExecutionContext
from nexus.engine.loop import TurnResult  # noqa: F401 (compat re-export)
from nexus.engine.response import ChatResult, build_chat_result
from nexus.engine.session import Session
from nexus.registry.plugins import registry as plugin_registry
from nexus.pipeline import resolve_execution_sequence

if TYPE_CHECKING:
    from nexus.engine.store import SessionStore

logger = logging.getLogger(__name__)

# Sole manager of the cxt field lifecycle (stateless, shared module-level instance)
_lifecycle = TurnLifecycle()

# graph_state reserved keys (engine-managed; everything else is free for
# node executors' workflow data)
PAUSED_NODE_KEY = "__paused_node__"
STEP_KEY = "__step__"


# ============================================================================
# LLM config refresh (R1 turn-level / R3 FSM-node-level / R4 AGENT-node-level)
# ============================================================================

def _refresh_llm_config(session: Session, node_code: str = "") -> None:
    """Resolve the LLM config for the current position and write it to
    cxt.llm_config (R1-R4 shared).

    Resolution: the explicit plugins["llm"] declaration (node layer over
    pattern layer — the value is an llm_providers code, resolved as an
    override) > the settings layered lookup (llm_default ⊕ pattern_llm ⊕
    pattern_llm.nodes[node_code]) > the session's llm_override metadata
    (CLI-side explicit pick, highest).

    This function must stay in the chat module: R1-R4 resolve
    get_llm_config through this namespace (tests anchor on
    patch("nexus.engine.chat.get_llm_config")).
    """
    cxt = session.cxt
    pattern = session.pattern
    override = cxt.metadata.get("llm_override")

    llm_code = None
    if pattern is not None:
        node = pattern.node_map.get(node_code) if node_code else None
        if node is not None:
            llm_code = (node.plugins or {}).get("llm")
        if not llm_code:
            llm_code = (pattern.plugins or {}).get("llm")
    if llm_code and override is None:
        override = {"code": llm_code}

    cxt.llm_config = get_llm_config(
        pattern_code=session.pattern_code or cxt.metadata.get("pattern_code", ""),
        node_code=node_code or cxt.current_node_code or "",
        override=override,
    )


# ============================================================================
# FSM kernel toolbox (imported by the FSM executor atom)
# ============================================================================

def _resolve_entry_node(cxt, pattern) -> None:
    """Determine the current node (pattern entry on first entry), written to
    cxt.current_node_code."""
    if cxt.current_node_code is None:
        cxt.current_node_code = pattern.entry_node_code
        logger.info("首次进入 FSM，使用入口节点: %s", pattern.entry_node_code)

    if cxt.current_node_code not in pattern.node_map:
        raise ValueError(
            f"节点 '{cxt.current_node_code}' 不存在于 node_map 中"
        )


def _fsm_node_transition(cxt, pattern) -> None:
    """FSM end-of-turn node transition.

    Jumps per next_node from the NLU result; clarify turns skip slot
    merging and jumping (topic/keywords stay out of filled_slots, node
    unchanged). Also merges the slots extracted by NLU incrementally into
    filled_slots.
    """
    # Clarify turn: skip slot merging (topic/keywords stay out of filled_slots), node unchanged
    if (cxt.metadata.get("clarify") or {}).get("triggered"):
        logger.info("澄清轮，跳过槽位合并与节点跳转: node=%s", cxt.current_node_code)
        return

    nlu_result = cxt.nlu_result or {}
    slots = nlu_result.get("slots", {})

    # Merge slots (incremental: via the lifecycle entry point)
    _lifecycle.merge_slots(cxt, slots)

    # Jump according to next_node in the NLU result
    next_node_code = nlu_result.get("next_node", "")

    if not next_node_code:
        logger.info("NLU 未返回 next_node，保持当前节点: %s", cxt.current_node_code)
        return

    if next_node_code not in cxt.node_map:
        logger.warning(
            "NLU 返回的 next_node '%s' 不在 node_map 中，保持当前节点: %s",
            next_node_code,
            cxt.current_node_code,
        )
        return

    logger.info(
        "FSM 节点跳转: %s → %s",
        cxt.current_node_code,
        next_node_code,
    )
    cxt.current_node_code = next_node_code


async def _run_stages(cxt, node, pattern, force_close: bool = False) -> None:
    """Execute the FSM pipeline stages in order (slots resolved lazily as
    node > pattern skeleton — the two-layer successor of the pre-merge
    node > module > skeleton).

    force_close (budget close-out — kept for the FSM executor's terminal
    guard parity) simply runs the stages through.

    Returns:
        None (FSM produces no control-flow events; clarify is handled
        inside the loop by ClarifyStage, node jumps by the end-of-turn
        transition).
    """
    from nexus.engine.streaming import reset_streamed_reply

    # Zero the stage-streaming marker per execution
    reset_streamed_reply()

    sequence = resolve_execution_sequence(cxt, node, pattern)

    logger.info(
        "Pipeline 开始: session=%s, node=%s, stages=%s",
        cxt.session_id,
        cxt.current_node_code,
        [f"{slot}:{getattr(stage, 'stage_name', type(stage).__name__)}"
         for slot, stage in sequence],
    )

    for slot, concrete in sequence:
        try:
            cxt = await concrete.execute(cxt)
            logger.debug("Stage '%s'（slot=%s）执行完成",
                         concrete.stage_name, slot)
        except Exception as e:
            logger.error(
                "Stage '%s' 执行异常: %s", concrete.stage_name, e,
                exc_info=True
            )
            raise


# ============================================================================
# Executor resolution
# ============================================================================

def _resolve_node_executor_code(pattern, node) -> str:
    """Resolve the AGENT node's executor code (plugin kind="executor").

    node.plugins["loop"] > pattern.plugins["loop"] > default_loop.
    """
    node_decl = (getattr(node, "plugins", None) or {}).get("loop")
    if node_decl:
        return node_decl
    pattern_decl = (getattr(pattern, "plugins", None) or {}).get("loop")
    if pattern_decl:
        return pattern_decl
    return plugin_registry.default_executor_code("agent")


async def _handle_node(session: Session, node, force_close: bool = False,
                       stream=None, resume_input: Optional[str] = None,
                       step: int = 0,
                       ) -> TurnResult:
    """Dispatch one node execution via the executor plugin."""
    ec = ExecutionContext(
        cxt=session.cxt,
        pattern=session.pattern,
        node=node,
        force_close=force_close,
        stream=stream,
        resume_input=resume_input,
        step=step,
    )
    executor = plugin_registry.resolve(
        "executor", _resolve_node_executor_code(session.pattern, node))
    return await executor.execute(ec)


# ============================================================================
# AGENT graph runtime (whole-graph run per message + suspension/resumption)
# ============================================================================

async def _run_agent_graph(session: Session, pattern, stream=None) -> TurnResult:
    """Run the AGENT graph for this turn.

    Fresh turn: start from entry, clear the state board. Resumed turn
    (graph_state carries a paused cursor): re-execute the paused node with
    the user message as resume_input, then continue to the terminal node.

    Termination: a node returns no routing output (and has no declared
    successors / is_end), or the max_steps budget is exhausted (force-close
    reply). Suspension: a node returns wait_human — persist the cursor and
    end the turn with that node's content as the reply.
    """
    cxt = session.cxt
    graph_state = cxt.graph_state
    max_steps = pattern.max_steps
    _emit = getattr(stream, "emit_trace", None)

    paused = graph_state.get(PAUSED_NODE_KEY)
    resume_input: Optional[str] = None
    if paused is not None and paused in pattern.node_map:
        current = pattern.node_map[paused]
        step = int(graph_state.get(STEP_KEY, 0))
        resume_input = cxt.user_query
        graph_state.pop(PAUSED_NODE_KEY, None)
        logger.info("[graph] 恢复挂起图: node=%s, step=%d", paused, step)
        if _emit is not None:
            _emit("graph_resume", node_code=current.code, step=step)
    else:
        if paused is not None:
            logger.warning(
                "[graph] 挂起节点 %r 不在 node_map 中，丢弃游标从 entry 重跑", paused,
            )
            graph_state.clear()
        current = pattern.node_map[pattern.entry_node_code]
        step = 0
        graph_state.clear()

    content = ""
    while True:
        if step >= max_steps:
            logger.warning(
                "[graph] 达到 max_steps=%d，强制收尾: session=%s",
                max_steps, cxt.session_id,
            )
            if _emit is not None:
                _emit("graph_done", reason="max_steps", step=step)
            graph_state.clear()
            if not content:
                content = "抱歉，处理超时，请稍后重试。"
            return TurnResult(content=content)

        node = current
        # Observability: cxt.current_node_code mirrors the graph position
        # (store snapshot / trace consumers read it)
        cxt.current_node_code = node.code

        # R4: node-level LLM config (plugins["llm"] node layer over pattern)
        _refresh_llm_config(session, node_code=node.code)

        if _emit is not None:
            _emit("node_start", node_code=node.code, step=step)

        result = await _handle_node(
            session, node, stream=stream,
            resume_input=resume_input, step=step)
        resume_input = None  # only the resumed turn's first execution

        if _emit is not None:
            _emit("node_end", node_code=node.code, step=step)

        if result.content:
            content = result.content

        # ---- Suspension (wait_human) --------------------------------
        if result.wait_human:
            graph_state[PAUSED_NODE_KEY] = node.code
            graph_state[STEP_KEY] = step + 1
            cxt.actions.append({"graph_wait": {
                "node": node.code, "step": step,
                "message": (result.extra or {}).get("wait_message", ""),
            }})
            logger.info("[graph] 节点 %s 等待人工输入，图挂起（step=%d）",
                        node.code, step)
            if _emit is not None:
                _emit("graph_wait", node_code=node.code, step=step)
            return result

        # ---- Terminal markers ---------------------------------------
        if getattr(node, "is_end", False):
            if _emit is not None:
                _emit("graph_done", reason="is_end", step=step)
            graph_state.clear()
            return TurnResult(content=content, extra=result.extra)

        # ---- Routing (conditional edge via TurnResult.next) ---------
        nxt = result.next
        if isinstance(nxt, (list, tuple)):
            if not nxt:
                nxt = None
            else:
                if len(nxt) > 1:
                    logger.warning(
                        "[graph] 运行时扇出未实现，仅消费首个目标: %r", nxt)
                nxt = nxt[0]

        if nxt is None:
            if node.sub_nodes:
                logger.warning(
                    "[graph] 节点 %s 声明了后继 %s 但执行器未返回 next，图终止",
                    node.code, node.sub_nodes,
                )
            if _emit is not None:
                _emit("graph_done", reason="terminal", step=step)
            graph_state.clear()
            return TurnResult(content=content, extra=result.extra)

        if nxt not in node.sub_nodes:
            # Hallucination tolerance: an undeclared edge terminates the
            # run (the declared sub_nodes graph is authoritative)
            logger.warning(
                "[graph] 节点 %s 的路由输出 %r 不在其 sub_nodes %s 中"
                "（未声明边），图终止",
                node.code, nxt, node.sub_nodes,
            )
            if _emit is not None:
                _emit("graph_done", reason="undeclared_edge", step=step)
            graph_state.clear()
            return TurnResult(content=content, extra=result.extra)

        step += 1
        current = pattern.node_map[nxt]


# ============================================================================
# FSM turn (single executor dispatch; the atom owns stages + transition)
# ============================================================================

async def _run_fsm_turn(session: Session, pattern, stream=None) -> TurnResult:
    """Dispatch the FSM pattern's executor (pattern.plugins["fsm"] >
    default_fsm). The executor atom owns entry resolution / R3 / stages /
    end-of-turn transition."""
    cxt = session.cxt
    code = (pattern.plugins or {}).get("fsm") or \
        plugin_registry.default_executor_code("fsm")
    executor = plugin_registry.resolve("executor", code)
    ec = ExecutionContext(
        cxt=cxt, pattern=pattern, node=None, stream=stream)
    return await executor.execute(ec)


# ============================================================================
# Turn entries
# ============================================================================

async def chat_turn_stream(
        query: str,
        session_id: str,
        all_sessions: Dict[str, Session],
        store: Optional["SessionStore"] = None,
):
    """Async generator form of chat_turn: yields ChatStreamEvent objects
    (delta / round / trace / done), the final done event carrying the
    complete ChatResult. See nexus/engine/streaming.py for the protocol.

    REAL-TIME bridge (not step-drained): the whole turn orchestration runs
    in a background task; every emit is pushed onto an asyncio.Queue and
    re-yielded here the moment it happens. The turn's last push is always
    the done event; if the task dies early the bridge pushes an error done
    so the consumer never hangs.

    The turn steps:
    1. locate session → begin_turn; 2. R1 refresh → compression → record
    user; 3. dispatch by pattern_type (FSM pipeline / AGENT graph runtime,
    resuming a suspended graph when present); 4. end_turn → done.
    """
    import asyncio

    from nexus.engine.streaming import (
        ChatStreamEvent,
        StreamEmitter,
        current_emitter,
    )

    queue: "asyncio.Queue[ChatStreamEvent]" = asyncio.Queue()
    emitter = StreamEmitter(sink=queue.put_nowait)

    async def _run_turn() -> None:
        token = current_emitter.set(emitter)
        try:
            await _run_turn_body()
        except Exception:
            # safety net: the consumer loop terminates on done only — an
            # escaped exception must still produce one (details already
            # logged by the body's own handling; this is a last resort)
            logger.exception("流式轮次异常: session=%s", session_id)
            queue.put_nowait(ChatStreamEvent(kind="done", result=ChatResult(
                text="对话处理异常，请稍后重试")))
        finally:
            current_emitter.reset(token)

    async def _run_turn_body() -> None:
        async def _finish(text: str) -> ChatResult:
            await _lifecycle.end_turn(session.cxt, text)
            return build_chat_result(text, session.cxt)

        # ----------------------------------------------------------------
        # 1. Locate the session; start-of-turn reset
        # ----------------------------------------------------------------
        session = all_sessions.get(session_id)
        if session is None:
            logger.warning("会话不存在: %s", session_id)
            queue.put_nowait(ChatStreamEvent(kind="done", result=ChatResult(
                text="会话不存在，请先发起对话任务")))
            return

        _lifecycle.begin_turn(session.cxt, query)

        pattern = session.pattern
        if pattern is None:
            logger.warning("会话 %s 未绑定对话模板", session_id)
            queue.put_nowait(ChatStreamEvent(
                kind="done", result=await _finish("对话模板未配置")))
            return

        session.cxt.metadata["pattern_code"] = session.pattern_code

        # R1: resolve the LLM config for the turn (plugins["llm"] /
        # settings layered lookup; override takes precedence)
        try:
            _refresh_llm_config(session)
        except Exception as e:
            logger.error("加载 LLM 配置失败: %s", e)
            queue.put_nowait(ChatStreamEvent(
                kind="done", result=await _finish(f"LLM 配置加载失败: {e}")))
            return

        # History compression (silently skipped when the store is disabled /
        # threshold is 0 / too few messages; failure never blocks). Must run
        # before add user — compression rebuilds history and fixes
        # turn_history_start
        await maybe_compress(session, store)

        # Record user message
        await session.cxt.add_message("user", query, stage="chat")

        # ----------------------------------------------------------------
        # 2. Dispatch by pattern_type
        # ----------------------------------------------------------------
        try:
            if pattern.pattern_type == "fsm":
                result = await _run_fsm_turn(session, pattern, stream=emitter)
            else:
                result = await _run_agent_graph(session, pattern,
                                                stream=emitter)
            response = result.content or ""
        except Exception:
            logger.exception("对话处理异常: session=%s", session_id)
            # External sanitization: exception details may carry
            # path/config information — return a uniform message only
            response = "对话处理异常，请稍后重试"

        queue.put_nowait(ChatStreamEvent(kind="done",
                                         result=await _finish(response)))

    task = asyncio.create_task(_run_turn())
    try:
        while True:
            ev = await queue.get()
            yield ev
            if ev.kind == "done":
                break
    finally:
        if not task.done():
            # consumer closed the generator early — don't leak the turn task
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def chat_turn(
        query: str,
        session_id: str,
        all_sessions: Dict[str, Session],
        store: Optional["SessionStore"] = None,
) -> ChatResult:
    """Process one user dialogue turn, returning the full output (text +
    reserved actions). Aggregates chat_turn_stream — behavior identical to
    the pre-streaming implementation; see that generator's docstring for
    the turn steps."""
    from nexus.engine.streaming import aggregate_turn
    return await aggregate_turn(chat_turn_stream(query, session_id, all_sessions,
                                                 store=store))


async def chat(
        query: str,
        session_id: str,
        all_sessions: Dict[str, Session],
        store: Optional["SessionStore"] = None,
) -> str:
    """Compat entry: process one dialogue turn, returning the reply text (equivalent to chat_turn(...).text)."""
    return (await chat_turn(query, session_id, all_sessions,
                            store=store)).text
