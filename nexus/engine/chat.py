"""
Dialogue processing — the turn orchestrator (node+pattern 二层模型).

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
    - runtime fan-out = the node executor's dispatch output
      (``TurnResult.sends``): N worker instances run
      concurrently (asyncio.gather — same-loop interleaving, no locks),
      each targeting its OWN declared worker node (heterogeneous fan-out:
      a send names any declared sub_node), in a structurally isolated
      private workspace (own history / message_sink cut / task payload as
      the explicit query); every instance settles into the graph_state
      results board (completion order), then the merge node — the single
      node common to every targeted worker's sub_nodes (set intersection;
      exactly one outlier worker without a common merge is dropped with a
      warning, an unresolvable merge raises a template-correctness error)
      — executes with the board readable. A failed branch settles as an
      error entry and never kills the run (wait-for-all,
      failure-tolerant); branches may not suspend or nest
      (wait_human/sends inside a branch = that branch fails);
    - node executor resolution: node.plugins["loop"] >
      pattern.plugins["loop"] > default_loop (the ReAct tool loop);
    - step budget ``pattern.max_steps`` (one node execution per step;
      worker instances do NOT consume graph steps — fan-out node and join
      each count one, width is bounded by ``pattern.max_fanout`` and
      branch-internal rounds by each executor's own guards; on exhaustion
      a force-close reply ends the run);
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
- chat()      : compat entry (main.py), equivalent to
  chat_turn().text
"""

import asyncio
import copy
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from nexus.settings import get_llm_config, resolve_max_fanout, resolve_max_steps
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
# The fan-out results board — rebuilt (overwritten) on every
# fanout_start; join/later nodes read it until the graph terminates
FANOUT_RESULTS_KEY = "__fanout_results__"

# node_end / branch_end trace 里执行结果摘要的截断长度（过程展示不搬全量；
# 消费端想看全文走 done.result / 会话审计）
_RESULT_TRACE_MAX = 400

# App 级终态 trace 的 metadata 键（docs/design/session-persistence.md §6）：
# app 在 executor 收尾写 cxt.metadata[key]（如 archify 的完整过程 trace），
# turn 落定点由引擎统一捡回一条 app_trace 事件进 trace_events 后摘除该键
# （防止后续轮次重复捡旧值）——app 侧零改动。
_APP_TRACE_KEYS = ("archify",)


def _result_brief(text: str) -> str:
    """节点/分支执行结果的单行摘要（换行拍平 + 截断）。"""
    return " ".join((text or "").split())[:_RESULT_TRACE_MAX]


# ============================================================================
# LLM config refresh (R1 turn-level / R3 FSM-node-level / R4 AGENT-node-level)
# ============================================================================

def _refresh_llm_config(session: Session, node_code: str = "") -> None:
    """Resolve the LLM config for the current position and write it to
    cxt.llm_config (R1-R4 shared).

    Resolution: the session's llm_override metadata (CLI-side explicit pick,
    highest, an integral override that skips the layered lookup) > the
    settings layered lookup (llm_default ⊕ app llm ⊕ app nodes[node_code].llm,
    resolved inside get_llm_config by pattern_code / node_code).

    This function must stay in the chat module: R1-R4 resolve
    get_llm_config through this namespace (tests anchor on
    patch("nexus.engine.chat.get_llm_config")).
    """
    cxt = session.cxt
    override = cxt.metadata.get("llm_override")

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
# Runtime fan-out (sends -> N worker instances -> barrier join;
# heterogeneous targets, merge = common successor intersection)
# ============================================================================

def _branch_cxt(cxt, branch_input: Any):
    """Structurally isolated worker context.

    Shallow copy with the mutable channels replaced: a private messages
    workspace (own history; message_sink cut — branch rows never persist to
    the session), own actions / graph_state scratch (write-discard; results
    travel back exclusively via TurnResult), and the task payload as the
    explicit query (default messages builders need no special casing).
    Read-shared fields (llm_config / node_map / metadata / filled_slots)
    pass by reference — worker executors treat them read-only.
    """
    bcxt = copy.copy(cxt)
    bcxt.history = []
    bcxt.message_sink = None
    bcxt.actions = []
    bcxt.graph_state = {}
    bcxt.turn_history_start = 0
    if isinstance(branch_input, str):
        bcxt.user_query = branch_input
    elif branch_input is not None:
        bcxt.user_query = json.dumps(branch_input, ensure_ascii=False)
    return bcxt


async def _run_fanout(session: Session, node, result: TurnResult,
                      stream, step: int) -> Optional[str]:
    """Execute one fan-out declaration: validate -> resolve the common
    merge node -> run N worker instances concurrently (heterogeneous
    targets allowed — each send names its own declared worker node) ->
    settle each into the results board (completion order) -> return the
    join node code.

    Merge (join) resolution = the set intersection of every targeted
    worker's sub_nodes, with a template-correctness tolerance ladder:
    - exactly one common node  -> join resolved, all sends run;
    - no overlap and exactly ONE outlier worker (removing it pins the
      merge down) -> that worker's sends are dropped with a warning, the
      remaining workers execute against their common merge;
    - anything else (0 or 2+ outliers, or several common nodes, or a
      single worker without exactly one successor) -> ValueError: the
      template's merge shape is wrong, refuse to execute.

    Concurrency: asyncio.gather on the turn's event loop — same-loop
    interleaving means the shared emitter queue and the board appends need
    no locks (deliberate scope: no engine-wide concurrency
    rewrite; executors' async signatures unchanged).

    Failure semantics: a branch exception — including the forbidden
    wait_human / nested sends — settles that branch as an error entry; the
    run continues and the join still fires (wait-for-all,
    failure-tolerant). Returns None ONLY on the tolerant undeclared-target
    termination (the next-routing guard family); every other contract
    violation raises ValueError.
    """
    from nexus.engine.streaming import BranchStreamEmitter

    cxt = session.cxt
    pattern = session.pattern
    graph_state = cxt.graph_state
    sends = list(result.sends or [])
    _emit = getattr(stream, "emit_trace", None)
    # 扇出宽度守卫上界：app loop.max_fanout 赢，缺省回退 pattern.max_fanout
    max_fanout = resolve_max_fanout(pattern)

    if result.next is not None:
        raise ValueError(
            f"节点 {node.code!r} 同时返回 next 与 sends"
            f"（二者互斥）"
        )
    # Declared-edge guard: every DISTINCT target must be a declared edge
    # of the dispatching node (hallucinated node codes terminate
    # tolerantly — same guard family as next-routing)
    undeclared = sorted({s.node_code for s in sends} - set(node.sub_nodes))
    if undeclared:
        logger.warning(
            "[graph] 节点 %s 的扇出目标 %s 不在其 sub_nodes %s 中"
            "（未声明边），图终止",
            node.code, undeclared, node.sub_nodes,
        )
        if _emit is not None:
            _emit("graph_done", reason="undeclared_edge", step=step)
        graph_state.clear()
        return None

    # ---- merge (join) resolution across the targeted workers ----------
    worker_codes: List[str] = []          # first-seen order, deduped
    for s in sends:
        if s.node_code not in worker_codes:
            worker_codes.append(s.node_code)
    merge_sets = {wc: set(pattern.node_map[wc].sub_nodes)
                  for wc in worker_codes}

    def _common(codes: List[str]) -> set:
        # Intersection over the given workers (a lone worker intersects
        # against nothing — its own successors ARE the merge candidates)
        if len(codes) == 1:
            return set(merge_sets[codes[0]])
        return set.intersection(*(merge_sets[wc] for wc in codes))

    dropped: List[str] = []
    common = _common(worker_codes)
    if len(common) != 1 and len(worker_codes) > 1:
        # Outlier tolerance: exactly one worker without a common merge is
        # ignorable — the code whose removal leaves a unique intersection
        outliers = [wc for wc in worker_codes
                    if len(_common([w for w in worker_codes if w != wc])) == 1]
        if len(outliers) == 1:
            dropped = outliers
            sends = [s for s in sends if s.node_code not in dropped]
            worker_codes = [wc for wc in worker_codes
                            if wc not in dropped]
            for wc in dropped:
                merge_sets.pop(wc, None)
            common = _common(worker_codes)
            logger.warning(
                "[graph] 节点 %s 的扇出 worker %s 与其余 worker 无共同 "
                "merge 节点，忽略该 worker（执行剩余 %d 个实例，join=%s）",
                node.code, dropped, len(sends), sorted(common),
            )
    if len(common) == 0:
        merge_desc = {wc: sorted(merge_sets[wc]) for wc in worker_codes}
        raise ValueError(
            f"节点 {node.code!r} 扇出的各 worker 后继无重叠的 merge 节点"
            f"且无法忽略单一异常 worker（workers={worker_codes} 的 "
            f"sub_nodes={merge_desc}），不执行——请检查模板正确性"
        )
    if len(common) > 1:
        raise ValueError(
            f"节点 {node.code!r} 扇出的 merge 节点不唯一（workers="
            f"{worker_codes} 的共同后继={sorted(common)}，有且仅有一个共同 "
            f"merge 节点才合法），不执行——请检查模板正确性"
        )
    join_code = common.pop()

    if len(sends) > max_fanout:
        raise ValueError(
            f"节点 {node.code!r} 扇出宽度 {len(sends)} 超过 "
            f"max_fanout={max_fanout}（扇出宽度守卫）"
        )

    # ---- dispatch: per-worker executor + LLM config, then gather -------
    worker_nodes = {wc: pattern.node_map[wc] for wc in worker_codes}
    worker_executors = {
        wc: plugin_registry.resolve(
            "executor",
            _resolve_node_executor_code(pattern, worker_nodes[wc]))
        for wc in worker_codes}
    # Worker-level LLM configs BEFORE the copies — each branch inherits
    # its own worker's resolved config through the shared reference
    worker_llm = {}
    for wc in worker_codes:
        _refresh_llm_config(session, node_code=wc)
        worker_llm[wc] = cxt.llm_config

    board: List[Dict[str, Any]] = []
    graph_state[FANOUT_RESULTS_KEY] = board
    branch_ids = [f"{s.node_code}#{i + 1}" for i, s in enumerate(sends)]
    cxt.actions.append({"fanout_start": {
        "node": node.code, "branches": len(sends), "join": join_code,
        **({"dropped": dropped} if dropped else {})}})
    logger.info("[graph] 节点 %s 扇出 %d 个实例（workers=%s, join=%s）",
                node.code, len(sends), worker_codes, join_code)
    if _emit is not None:
        _emit("fanout_start", node_code=node.code,
              branch_ids=branch_ids, join_node=join_code,
              **({"dropped_workers": dropped} if dropped else {}))

    async def _run_branch(branch_id: str, send) -> None:
        worker_code = send.node_code
        if _emit is not None:
            _emit("branch_start", node_code=worker_code, branch_id=branch_id)
        branch_stream = (BranchStreamEmitter(stream, branch_id)
                         if stream is not None else None)
        bcxt = _branch_cxt(cxt, send.input)
        bcxt.current_node_code = worker_code
        bcxt.llm_config = worker_llm[worker_code]
        ec = ExecutionContext(
            cxt=bcxt,
            pattern=pattern,
            node=worker_nodes[worker_code],
            stream=branch_stream,
            step=step,
            branch_id=branch_id,
            branch_input=send.input,
        )
        entry: Dict[str, Any]
        try:
            bres = await worker_executors[worker_code].execute(ec)
            if bres.wait_human or bres.sends:
                # v1 guards: no suspension, no nested fan-out inside a
                # branch — the branch fails loudly, the run continues
                raise ValueError(
                    f"扇出分支 {branch_id!r} 返回了 wait_human/sends"
                    f"（v1 禁止分支内挂起/嵌套扇出）"
                )
            entry = {"branch_id": branch_id, "node_code": worker_code,
                     "ok": True, "content": bres.content or "",
                     "extra": bres.extra or {}}
        except Exception as e:
            logger.exception("[graph] 扇出分支 %s 失败", branch_id)
            entry = {"branch_id": branch_id, "node_code": worker_code,
                     "ok": False, "error": str(e)}
        board.append(entry)  # completion order (single event loop)
        if _emit is not None:
            _emit("branch_end", node_code=worker_code, branch_id=branch_id,
                  ok=entry["ok"], error=entry.get("error", ""),
                  **({"content": _result_brief(entry["content"])}
                     if entry.get("content") else {}))

    await asyncio.gather(*[
        _run_branch(bid, s) for bid, s in zip(branch_ids, sends)])

    failed = sum(1 for e in board if not e["ok"])
    cxt.actions.append({"fanout_join": {
        "node": join_code, "total": len(board), "failed": failed}})
    logger.info("[graph] 扇出汇聚: join=%s, total=%d, failed=%d",
                join_code, len(board), failed)
    if _emit is not None:
        _emit("fanout_join", node_code=join_code,
              total=len(board), failed=failed)
    return join_code


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
    # 图级步数预算：app loop.max_steps 赢，缺省回退 pattern.max_steps
    max_steps = resolve_max_steps(pattern)
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
        if _emit is not None:
            _emit("graph_compile", node_code=pattern.entry_node_code,
                  pattern=pattern.code, nodes=len(pattern.node_map),
                  entry=pattern.entry_node_code)

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

        # R4: node-level LLM config (app nodes[node].llm over app llm)
        _refresh_llm_config(session, node_code=node.code)

        if _emit is not None:
            _emit("node_start", node_code=node.code, step=step)

        result = await _handle_node(
            session, node, stream=stream,
            resume_input=resume_input, step=step)
        resume_input = None  # only the resumed turn's first execution

        if _emit is not None:
            # node_end carries a result brief (content / routing verdict) —
            # real-time consumers show WHAT the node produced, not just that
            # it finished
            end_data: Dict[str, Any] = {}
            if result.content:
                end_data["content"] = _result_brief(result.content)
            if result.wait_human:
                end_data["wait_human"] = True
            if result.sends:
                end_data["sends"] = len(result.sends)
            if result.next:
                nxt = result.next
                end_data["next"] = nxt if isinstance(nxt, str) else str(list(nxt)[:1])
            _emit("node_end", node_code=node.code, step=step, **end_data)

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

        # ---- Fan-out (sends: N homogeneous instances + barrier join) --
        if result.sends:
            join_code = await _run_fanout(session, node, result,
                                          stream, step)
            if join_code is None:
                # Undeclared dispatch target — tolerant termination
                # (same guard family as the next-routing branch below)
                return TurnResult(content=content, extra=result.extra)
            step += 1
            current = pattern.node_map[join_code]
            continue

        # ---- Routing (conditional edge via TurnResult.next) ---------
        nxt = result.next
        if isinstance(nxt, (list, tuple)):
            if not nxt:
                nxt = None
            else:
                if len(nxt) > 1:
                    logger.warning(
                        "[graph] next 的 list 形态为遗留容忍（运行时扇出"
                        "请用 sends），仅消费首个目标: %r", nxt)
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
    result = await executor.execute(ec)
    if result.sends:
        raise ValueError(
            "FSM 路径不支持扇出 sends（仅 AGENT 图运行时消费）"
        )
    return result


# ============================================================================
# Turn entries
# ============================================================================

async def chat_turn_stream(
        query: str,
        session_id: str,
        all_sessions: Dict[str, Session],
        store: Optional["SessionStore"] = None,
        *,
        detach_on_close: bool = False,
        on_settled: Optional[Any] = None,
        turn_task_out: Optional[Dict[str, Any]] = None,
):
    """Async generator form of chat_turn: yields ChatStreamEvent objects
    (delta / round / trace / done), the final done event carrying the
    complete ChatResult. See nexus/engine/streaming.py for the protocol.

    REAL-TIME bridge (not step-drained): the whole turn orchestration runs
    in a background task; every emit is pushed onto an asyncio.Queue and
    re-yielded here the moment it happens. The turn's last push is always
    the done event; if the task dies early the bridge pushes an error done
    so the consumer never hangs.

    PERSISTENCE plumbing (docs/design/session-persistence.md): kind="trace"
    events additionally fan out to the session's trace_sink (append-only
    trail, written as they happen via a per-turn serialized writer — FIFO by
    construction); app-level final-state traces (``_APP_TRACE_KEYS``) are
    picked up at turn end and removed from metadata.

    TURN OWNERSHIP: the per-session turn_lock is held by the turn task
    itself (not the consumer), so:

    - ``detach_on_close=True`` — a consumer that closes the generator early
      (SSE client disconnect) only detaches consumption: the turn keeps
      running in the background, trace/message sinks keep writing, and
      ``on_settled`` still fires at the real end of the turn;
    - ``detach_on_close=False`` (default, tests / aggregation) — closing the
      generator early cancels the turn task, exactly the pre-detach
      semantics.

    ``on_settled``: async callback invoked (once, inside the turn task,
    under the lock) after the turn settles with a done — success or handled
    failure — but NOT on cancellation; the host uses it for the end-of-turn
    snapshot. ``turn_task_out``: dict that receives ``{"task": ...}`` right
    after the turn task is created (the host registers it in the turn
    registry; it owns the strong reference, eviction shield and shutdown
    cancel list).

    The turn steps:
    1. locate session → begin_turn; 2. R1 refresh → compression → record
    user; 3. dispatch by pattern_type (FSM pipeline / AGENT graph runtime,
    resuming a suspended graph when present); 4. app trace pickup →
    end_turn → done.
    """
    import asyncio

    from nexus.engine.streaming import (
        ChatStreamEvent,
        StreamEmitter,
        current_emitter,
    )

    queue: "asyncio.Queue[ChatStreamEvent]" = asyncio.Queue()
    live = [True]            # consumer attached → queue accepts events
    trace_q: "asyncio.Queue" = asyncio.Queue()   # serialized trace writes
    trace_writer: Optional[Any] = None
    # set once the session is located: the per-turn trace enqueue closure
    state: Dict[str, Any] = {"enqueue": None}

    def _sink(ev: ChatStreamEvent) -> None:
        # Trace fan-out is independent of the live queue: persistence keeps
        # flowing after a consumer detaches (facts, not stream).
        enqueue = state["enqueue"]
        if enqueue is not None and ev.kind == "trace" and ev.trace is not None:
            try:
                enqueue(ev.trace)
            except Exception:
                logger.exception("trace 入队失败（不影响对话）: session=%s",
                                 session_id)
        if live[0]:
            queue.put_nowait(ev)

    emitter = StreamEmitter(sink=_sink)

    def _trace_row(session: Session, t: Any) -> Dict[str, Any]:
        """Flatten a TraceEvent into the store's trace_sink row shape."""
        payload: Dict[str, Any] = {"module_code": t.module_code}
        if t.node_code:
            payload["node_code"] = t.node_code
        if t.branch_id:
            payload["branch_id"] = t.branch_id
        if t.data:
            payload["data"] = t.data
        return {
            "session_id": session.session_id,
            "turn_id": str((session.cxt.metadata or {}).get("request_id")
                           or ""),
            "kind": t.event,
            "payload": payload,
        }

    async def _trace_writer_loop(session: Session) -> None:
        """Single per-turn writer: sinks stay serialized (FIFO rows), write
        failures never block the dialogue (same contract as message_sink)."""
        failures = 0
        while True:
            item = await trace_q.get()
            if item is None:
                return
            try:
                await item
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.exception(
                    "trace_sink 写入失败（不影响对话）: session=%s 累计=%d",
                    session_id, failures)

    async def _run_turn_body(session: Session) -> None:
        async def _finish(text: str) -> ChatResult:
            await _lifecycle.end_turn(session.cxt, text)
            return build_chat_result(text, session.cxt)

        # ----------------------------------------------------------------
        # 1. Start-of-turn reset
        # ----------------------------------------------------------------
        _lifecycle.begin_turn(session.cxt, query)

        pattern = session.pattern
        if pattern is None:
            logger.warning("会话 %s 未绑定对话模板", session_id)
            queue.put_nowait(ChatStreamEvent(
                kind="done", result=await _finish("对话模板未配置")))
            return

        session.cxt.metadata["pattern_code"] = session.pattern_code

        # R1: resolve the LLM config for the turn (settings layered lookup;
        # the session's llm_override metadata takes precedence)
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
            # Real-time consumers (SSE / CLI events) flag the failed turn via
            # this trace instead of string-matching the generic done text;
            # aggregate consumers ignore it (done stays authoritative)
            emitter.emit_trace("turn_error")
            # External sanitization: exception details may carry
            # path/config information — return a uniform message only
            response = "对话处理异常，请稍后重试"

        # App-level final-state traces (e.g. archify): pick up at turn end,
        # persist as one app_trace event per key, then pop (a stale value
        # must not leak into later turns' trails).
        for key in _APP_TRACE_KEYS:
            app_trace = session.cxt.metadata.get(key)
            if app_trace:
                emitter.emit_trace("app_trace", node_code=session.cxt.current_node_code or "",
                                   app=key, trace=app_trace)
                session.cxt.metadata.pop(key, None)

        queue.put_nowait(ChatStreamEvent(kind="done",
                                         result=await _finish(response)))

    async def _run_turn() -> None:
        nonlocal trace_writer
        token = current_emitter.set(emitter)
        session = all_sessions.get(session_id)
        settled = False
        try:
            if session is None:
                logger.warning("会话不存在: %s", session_id)
                queue.put_nowait(ChatStreamEvent(kind="done", result=ChatResult(
                    text="会话不存在，请先发起对话任务")))
                return

            # Per-turn trace writer: started only when a trace_sink is wired
            # (store attached); a single task keeps the trail's FIFO order.
            if session.cxt.trace_sink is not None:
                state["enqueue"] = lambda t: trace_q.put_nowait(
                    session.cxt.trace_sink(_trace_row(session, t)))
                trace_writer = asyncio.create_task(
                    _trace_writer_loop(session))

            # The lock spans the whole turn INCLUDING the settled callback:
            # a queued same-session turn must never interleave with the
            # end-of-turn snapshot.
            async with session.turn_lock:
                await _run_turn_body(session)
                settled = True
        except Exception:
            # safety net: the consumer loop terminates on done only — an
            # escaped exception must still produce one (details already
            # logged by the body's own handling; this is a last resort)
            logger.exception("流式轮次异常: session=%s", session_id)
            emitter.emit_trace("turn_error")
            queue.put_nowait(ChatStreamEvent(kind="done", result=ChatResult(
                text="对话处理异常，请稍后重试")))
            settled = True
        finally:
            current_emitter.reset(token)
            # Drain the trail writer first so the settled snapshot never
            # races pending trace rows; on cancellation this still flushes
            # what already happened (facts survive, state does not).
            if trace_writer is not None and not trace_writer.done():
                trace_q.put_nowait(None)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(trace_writer), 10)
                except asyncio.TimeoutError:
                    trace_writer.cancel()
                except asyncio.CancelledError:
                    trace_writer.cancel()
                    raise
                except Exception:
                    logger.exception("trace writer 收尾失败: session=%s",
                                     session_id)
            if settled and on_settled is not None:
                try:
                    await on_settled()
                except Exception:
                    logger.exception("轮末回调失败: session=%s", session_id)

    task = asyncio.create_task(_run_turn())
    if turn_task_out is not None:
        turn_task_out["task"] = task
    saw_done = False
    try:
        while True:
            ev = await queue.get()
            yield ev
            if ev.kind == "done":
                saw_done = True
                break
    finally:
        if detach_on_close:
            if not task.done():
                # consumer went away (SSE disconnect): detach, don't cancel.
                # Events stop being queued (the queue would otherwise grow
                # for the rest of a possibly minutes-long turn); sinks and
                # the settled callback keep the trail + snapshot complete.
                live[0] = False
                while True:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
        elif not saw_done and not task.done():
            # consumer closed the generator early — don't leak the turn task
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        else:
            # ran to done: stay for the tail (trace-writer flush + settled
            # callback) — cancelling here would cut the end-of-turn audit
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def chat_turn(
        query: str,
        session_id: str,
        all_sessions: Dict[str, Session],
        store: Optional["SessionStore"] = None,
        *,
        on_settled: Optional[Any] = None,
        turn_task_out: Optional[Dict[str, Any]] = None,
) -> ChatResult:
    """Process one user dialogue turn, returning the full output (text +
    reserved actions). Aggregates chat_turn_stream — behavior identical to
    the pre-streaming implementation; see that generator's docstring for
    the turn steps. ``on_settled`` / ``turn_task_out`` pass through to the
    stream entry (host-side snapshot callback / turn registry capture)."""
    from nexus.engine.streaming import aggregate_turn
    return await aggregate_turn(chat_turn_stream(
        query, session_id, all_sessions, store=store,
        on_settled=on_settled, turn_task_out=turn_task_out))


async def chat(
        query: str,
        session_id: str,
        all_sessions: Dict[str, Session],
        store: Optional["SessionStore"] = None,
        *,
        on_settled: Optional[Any] = None,
        turn_task_out: Optional[Dict[str, Any]] = None,
) -> str:
    """Compat entry: process one dialogue turn, returning the reply text (equivalent to chat_turn(...).text)."""
    return (await chat_turn(query, session_id, all_sessions, store=store,
                            on_settled=on_settled,
                            turn_task_out=turn_task_out)).text
