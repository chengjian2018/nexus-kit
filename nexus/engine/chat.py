"""
Dialogue processing — the turn orchestrator.

Responsibility is narrowed to "orchestrating one turn": locate the session →
cxt turn lifecycle → same-turn hop loop → produce a ChatResult. Per-module
handling is inlined in this module by type:

- AGENT → loop.run_agent (inject answers directly / transfer writes a
  ModuleJumpEvent and returns)
- FSM   → _run_fsm_pipeline (stages execution + next_node jump)
- ROUTE → _run_route_pipeline (stages execution + end-of-turn reset to root)

Module jumps all go through ModuleJumpEvent (written to cxt.actions,
defined in dialogue/base.py). Events originate from only two entry points:

- ROUTE jumping to a new module: _run_stages detects after each stage
  execution (ROUTE modules only) — the jump_module field output by NLU, or
  the jump_module configured on the menu node after advancement. On a hit:
  merge slots, write the event, abort the remaining stages (the source
  module is suppressed and generates no reply).
- AGENT transfer tool call: loop.run_agent writes the event directly and
  returns.
- FSM produces no events: clarify is handled inside the loop by
  ClarifyStage (overwrites nlg_result without leaving the loop), and node
  jumps are handled by the end-of-turn _fsm_node_transition.
- Consumption: chat_turn's hop loop _jumps.pop → _jumps.reroute (writes
  current_module_code, clears current_node_code) → the target module
  continues the reply in the same turn; exceeding the hop budget forces a
  force_close close-out.
- No adjacency validation / bounce rejection / dispatch accounting — the
  target existing in module_map is legal; boundaries are deliberately thin,
  and agent and route jumps are isomorphic.

The cxt field lifecycle (per-turn reset / cross-turn retention / incremental
update) is managed exclusively by context_lifecycle.TurnLifecycle; this
module only calls it at the right timing points.

Entries:
- chat_turn() : full entry, returns ChatResult (text + reserved actions)
- chat()      : compat entry (main.py / cli.py / existing tests),
  equivalent to chat_turn().text
"""

import logging
from typing import TYPE_CHECKING, Dict, Optional

from nexus.settings import get_llm_config
from nexus.engine.compression import maybe_compress
from nexus.engine.context_lifecycle import TurnLifecycle
from nexus.engine.loop import TurnResult, run_agent  # noqa: F401 (compat re-export)
from nexus.engine.response import ChatResult, build_chat_result
from nexus.engine.session import Session
from nexus.context import ModuleJumpEvent
from nexus.model.module import ModuleType

if TYPE_CHECKING:
    from nexus.engine.store import SessionStore
from nexus.pipeline import (
    GenerateSlot,
    PostRecallSlot,
    PreRecallSlot,
    QuerySlot,
    resolve_stage,
)

logger = logging.getLogger(__name__)

# Sole manager of the cxt field lifecycle (stateless, shared module-level instance)
_lifecycle = TurnLifecycle()


# ============================================================================
# ModuleJumpEvent channel (cxt.actions is the sole carrier of jump events)
# ============================================================================

class ModuleJumpChannel:
    """Jump event channel operations — the single entry for channel
    read/write, production detection, and consumption rerouting.

    Stateless (all state lives on cxt.actions), reused at module level in
    the same pattern as TurnLifecycle. This class must stay in the chat
    module: the R4 refresh resolves ``get_llm_config`` in this namespace
    (tests/test_llm_refresh.py anchors on
    patch("nexus.engine.chat.get_llm_config")).

    Division of duties: producers (detect_after_stage / stages writing
    events themselves / run_agent) only append and never remove;
    cxt.actions is the cross-function carrier. Consumption (pop + reroute)
    happens only in chat_turn's hop loop — if a producer popped, the
    consumer would find nothing and treat it as no jump, yielding an empty
    reply.
    """

    # ------------------------------------------------------------------
    # Channel read/write
    # ------------------------------------------------------------------

    @staticmethod
    def peek(cxt) -> Optional[ModuleJumpEvent]:
        """Check whether cxt.actions already holds a jump event (without removing it)."""
        for item in cxt.actions:
            if isinstance(item, ModuleJumpEvent):
                return item
        return None

    @staticmethod
    def pop(cxt) -> Optional[ModuleJumpEvent]:
        """Take the first jump event out of cxt.actions (consumption removes it).

        Non-jump actions (dict-shaped, e.g. conversation_end) stay in
        actions untouched and are snapshotted into ChatResult by the
        end-of-turn build_chat_result.
        """
        for i, item in enumerate(cxt.actions):
            if isinstance(item, ModuleJumpEvent):
                return cxt.actions.pop(i)
        return None

    # ------------------------------------------------------------------
    # Consumption: reroute
    # ------------------------------------------------------------------

    @staticmethod
    def reroute(cxt, event: ModuleJumpEvent) -> None:
        """Consume a jump event: reroute to the target module (deliberately
        thin boundary, only existence is validated).

        If the target does not exist, stay in place (the pattern already
        fail-fasts on jump_module configuration at registration time; this
        guards against hallucinated NLU output).
        """
        if event.target_module_code not in cxt.module_map:
            logger.warning(
                "[jump] 目标模块 '%s' 不存在，保持原模块: %s",
                event.target_module_code, cxt.current_module_code,
            )
            return
        logger.info(
            "[jump] %s → %s (source=%s)",
            cxt.current_module_code, event.target_module_code, event.source,
        )
        cxt.current_module_code = event.target_module_code
        # Node cleared: the target module's _resolve_entry_node picks its own first node
        cxt.current_node_code = None

    @staticmethod
    def detect_after_stage(cxt, module, before_nlu) -> Optional[ModuleJumpEvent]:
        """Jump detection after each stage execution for ROUTE modules;
        FSM/AGENT always return None.

        Events are produced by ROUTE only: FSM's clarify is handled inside
        the loop by ClarifyStage (overwrites nlg_result without leaving the
        loop) and node jumps are handled by the end-of-turn
        _fsm_node_transition; AGENT transfers go through the transfer tool
        (run_agent writes the event).

        Before detecting, ROUTE first advances the menu node (the former
        _RouteNodeAdvance duty was merged in here): next_node hitting one
        of this module's nodes → switch the current node + R4 node-level
        LLM config takes effect this turn, so the subsequent NLG part
        generates per the menu node's configuration.

        Module jump sources (in priority order):
        1. ``nlu_result.jump_module``: the module-level jump field output
           directly by NLU
        2. the advanced node's ``jump_module`` configuration: the module
           the menu node itself declares to jump to (advancement already
           happened above, so just read the current node)

        Detection runs only when nlu_result was (over)written during this
        stage's execution — prevents false detection on a stale nlu_result
        during the hop continuation phase. Targets missing from module_map
        / self-jumps are ignored and the remaining stages keep executing
        (LLM hallucination tolerance).
        """
        if module.type != ModuleType.ROUTE:
            return None
        if cxt.nlu_result is None or cxt.nlu_result is before_nlu:
            return None

        nlu_result = cxt.nlu_result
        target = ""
        source = ""

        # Menu node advancement
        next_node_code = nlu_result.get("next_node", "")
        module_node_codes = {n.node_code for n in module.module_nodes}
        if next_node_code and next_node_code in module_node_codes:
            logger.info(
                "ROUTE 命中菜单节点: %s → %s",
                cxt.current_node_code, next_node_code,
            )
            cxt.current_node_code = next_node_code
            # R4: the menu node's node-level LLM config takes effect this
            # turn (spec §4; pattern_code comes from the metadata R1 wrote —
            # ROUTE departs from root every turn and never dwells on a menu
            # node)
            cxt.llm_config = get_llm_config(
                pattern_code=cxt.metadata.get("pattern_code", ""),
                module_code=cxt.current_module_code or "",
                node_code=next_node_code,
                override=cxt.metadata.get("llm_override"),
            )

        # 1) NLU directly outputs the module-level jump field
        jump_field = nlu_result.get("jump_module", "")
        if isinstance(jump_field, str) and jump_field:
            target, source = jump_field, "nlu_jump"

        # 2) the advanced (or current) node has jump_module configured (menu dispatch)
        if not target:
            cur_node = cxt.node_map.get(cxt.current_node_code)
            node_jump = getattr(cur_node, "jump_module", None) if cur_node else None
            if node_jump:
                target, source = node_jump, "route_menu"

        if not target or target == cxt.current_module_code:
            return None
        if target not in cxt.module_map:
            logger.warning(
                "[jump] NLU 指示跳转目标 '%s' 不在 module_map 中，忽略", target,
            )
            return None

        return ModuleJumpEvent(
            target_module_code=target,
            reason=str(nlu_result.get("reason", "") or ""),
            source=source,
        )



# Sole manager of the jump event channel (stateless, shared module-level instance)
_jumps = ModuleJumpChannel()


# ============================================================================
# Pipeline execution
# ============================================================================

def _default_skeleton(module) -> list:
    """Default pipeline skeleton (slots resolved lazily, not bound to a node).

    [PreRecallSlot, QuerySlot, PostRecallSlot, GenerateSlot]; the FSM/ROUTE
    differences (advance / clarify insertion) are handled when GenerateSlot
    expands (stage_slots.resolve_stage) — the skeleton itself is identical
    across all module types.
    """
    return [PreRecallSlot(), QuerySlot(), PostRecallSlot(), GenerateSlot()]


def _refresh_llm_config(session: Session, module_code: str = "",
                        node_code: str = "") -> None:
    """Resolve the LLM config for the current position and write it to
    cxt.llm_config (spec §4, shared by R1-R4).

    This function must stay in the chat module: R1-R3 resolve
    get_llm_config through this namespace (tests/test_llm_refresh.py et al.
    anchor on patch("nexus.engine.chat.get_llm_config")), avoiding a handlers
    self-import.
    """
    cxt = session.cxt
    cxt.llm_config = get_llm_config(
        pattern_code=session.pattern_code or cxt.metadata.get("pattern_code", ""),
        module_code=module_code or cxt.current_module_code or "",
        node_code=node_code or cxt.current_node_code or "",
        override=cxt.metadata.get("llm_override"),
    )


def _fsm_node_transition(cxt, module) -> None:
    """FSM end-of-turn node transition.

    Jumps per next_node from the NLU result; clarify turns skip slot
    merging and jumping (topic/keywords stay out of filled_slots, node
    unchanged). Also merges the slots extracted by NLU incrementally into
    filled_slots.

    Args:
        cxt: dialogue context
        module: current module object
    """
    # Clarify turn: skip slot merging (topic/keywords stay out of filled_slots), node unchanged
    if (cxt.metadata.get("clarify") or {}).get("triggered"):
        logger.info("澄清轮，跳过槽位合并与节点跳转: node=%s", cxt.current_node_code)
        return

    nlu_result = cxt.nlu_result or {}
    slots = nlu_result.get("slots", {})

    # Merge slots (incremental: via the lifecycle entry point)
    _lifecycle.merge_slots(cxt, slots)

    # FSM type: jump according to next_node in the NLU result
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


def _resolve_entry_node(cxt, module) -> None:
    """Determine the current node (first node of the module on first entry), written to cxt.current_node_code."""
    if cxt.current_node_code is None:
        if module.module_nodes:
            first_node = module.module_nodes[0]
            cxt.current_node_code = first_node.node_code
            logger.info(
                "首次进入模块 %s，使用首节点: %s",
                module.module_code,
                first_node.node_code,
            )
        else:
            raise ValueError(f"模块 '{module.module_code}' 无可用节点")

    cur_node = cxt.node_map.get(cxt.current_node_code)
    if cur_node is None:
        raise ValueError(
            f"节点 '{cxt.current_node_code}' 不存在于 node_map 中"
        )


def _run_stages(cxt, module, pattern, force_close: bool = False
                ) -> Optional[ModuleJumpEvent]:
    """Execute the pipeline stages in order (slots resolved lazily as
    node > module > pattern).

    After each stage executes, run jump detection (ROUTE only:
    _jumps.detect_after_stage + events written by stages themselves): on a
    hit, merge slots, write the ModuleJumpEvent to cxt.actions, and abort
    the remaining stages — the source module is suppressed this turn (NLG
    does not run) and the chat layer's hop loop reroutes to the target
    module to continue the reply in the same turn. FSM produces no events
    (clarify is handled inside the loop by ClarifyStage without leaving it;
    node jumps are handled by the end-of-turn transition). force_close
    (max-hops close-out) skips detection and lets the stages run through;
    events written by stages during that window do not participate in
    control flow (they do not trigger suppression) and stay in actions for
    observation only.

    Returns:
        The jump event awaiting consumption (already written to
        cxt.actions, popped by chat_turn's hop loop); None means no jump.
    """
    stages = pattern.stages or _default_skeleton(module)

    logger.info(
        "Pipeline 开始: session=%s, module=%s, node=%s, stages=%s",
        cxt.session_id,
        module.module_code,
        cxt.current_node_code,
        [s.stage_name for s in stages],
    )

    # Execute each stage in order; slots resolve against the *current* node
    # at execution time
    for stage in stages:
        for concrete in resolve_stage(stage, cxt, module, pattern):
            before_nlu = cxt.nlu_result
            try:
                cxt = concrete.execute(cxt)
                logger.debug("Stage '%s' 执行完成", concrete.stage_name)
            except Exception as e:
                logger.error(
                    "Stage '%s' 执行异常: %s", concrete.stage_name, e,
                    exc_info=True
                )
                raise

            if force_close:
                continue

            # Detection 1: the stage itself wrote a jump event (custom stage channel)
            direct = _jumps.peek(cxt)
            if direct is not None:
                logger.info(
                    "Stage '%s' 写入跳转事件: → %s",
                    concrete.stage_name, direct.target_module_code,
                )
                return direct

            # Detection 2: nlu_result was updated and indicates a jump (NLU
            # jump_module field / advanced node's jump_module config)
            event = _jumps.detect_after_stage(cxt, module, before_nlu)
            if event is not None:
                # Incremental slot merge travels with the jump (the target module inherits the context)
                _lifecycle.merge_slots(
                    cxt, (cxt.nlu_result or {}).get("slots", {}))
                cxt.actions.append(event)
                logger.info(
                    "Stage '%s' 后检测到模块跳转: %s → %s (source=%s)",
                    concrete.stage_name, module.module_code,
                    event.target_module_code, event.source,
                )
                return event

    # Stages running through naturally means no jump: events written
    # directly by a stage were already caught by detection 1 right after
    # that stage and returned early; under force_close detection is skipped,
    # so events written in the meantime stay in actions for observation
    # only (end-of-turn snapshot) and do not trigger source-module
    # suppression
    return None


def _run_fsm_pipeline(session: Session, module, force_close: bool = False
                      ) -> TurnResult:
    """One FSM module turn: node resolution → R3 refresh → stages →
    next_node jump.

    FSM produces no jump events (clarify handled inside the loop, node
    jumps at end of turn), so _run_stages always returns None.
    """
    cxt = session.cxt
    pattern = session.pattern

    _resolve_entry_node(cxt, module)

    # R3: after node resolution, refresh the LLM config by module+node (spec §4)
    _refresh_llm_config(session, module_code=module.module_code,
                        node_code=cxt.current_node_code)

    _run_stages(cxt, module, pattern, force_close=force_close)

    # FSM: next_node jump (the clarify-turn guard lives inside the transition function)
    _fsm_node_transition(cxt, module)

    # Terminal node action (reserved channel)
    next_node = pattern.node_map.get(cxt.current_node_code)
    if next_node is not None and getattr(next_node, "is_end", False):
        cxt.actions.append({"conversation_end": True})

    nlg_result = cxt.nlg_result or {}
    return TurnResult(reply=nlg_result.get("content", ""))


def _run_route_pipeline(session: Session, module, force_close: bool = False
                        ) -> TurnResult:
    """One ROUTE module turn: node resolution → R3 refresh → stages →
    end-of-turn reset to root.

    Module jumps are detected inside _run_stages (NLU jump_module field /
    menu node jump_module config); this function does no jump_module
    dispatch of its own. On a jump turn, return silently (the hop loop
    reroutes and the target module resolves its own entry node); after
    jumping to an AGENT/FSM module, the target carries the following turns
    across turns (agent via history, FSM via node position) without
    returning to routing — the end-of-turn reset to root happens only while
    still parked in the ROUTE module (menu nodes have no sub_nodes; without
    the reset the next turn's routing candidates would be empty). ROUTE
    does not assemble a ClarifyStage (only FSM+enable_clarify gets one
    inserted, see stage_slots.resolve_stage), and begin_turn already
    cleared clarify at start of turn, so there is no clarify-turn branch.
    """
    cxt = session.cxt
    pattern = session.pattern

    _resolve_entry_node(cxt, module)

    # R3: after node resolution, refresh the LLM config by module+node (spec §4)
    _refresh_llm_config(session, module_code=module.module_code,
                        node_code=cxt.current_node_code)

    jump_event = _run_stages(cxt, module, pattern, force_close=force_close)

    # Jump turn: slots were already merged at the detection point; the hop
    # loop reroutes to the target module to continue in the same turn
    if jump_event is not None:
        return TurnResult()

    # Slot merge (incremental: via the lifecycle entry point)
    slots = (cxt.nlu_result or {}).get("slots", {})
    _lifecycle.merge_slots(cxt, slots)

    # End-of-turn reset to root (including force_close: jump detection is
    # skipped, but menu nodes have no sub_nodes — without the reset the
    # next turn's routing candidates would be empty)
    root_code = module.module_nodes[0].node_code if module.module_nodes else None
    cxt.current_node_code = root_code
    logger.info("ROUTE 模块保持 root 节点: %s", root_code)

    nlg_result = cxt.nlg_result or {}
    return TurnResult(reply=nlg_result.get("content", ""))


def _run_agent_pipeline(session: Session, module, force_close: bool = False
                        ) -> TurnResult:
    """One AGENT module turn: R2 refresh → loop.run_agent.

    transfer tool calls are written by run_agent as a ModuleJumpEvent to
    cxt.actions; this function does nothing further with them (consumed
    uniformly by the chat layer's hop loop).
    """
    logger.info("Agent 模块处理: module=%s", module.module_code)
    _refresh_llm_config(session, module_code=module.module_code)  # R2
    return run_agent(session, module, session.cxt.llm_config,
                     force_close=force_close)


def _handle_module(session: Session, module, force_close: bool = False
                   ) -> TurnResult:
    """Dispatch single-module single-turn handling by module type."""
    if module.type == ModuleType.AGENT:
        return _run_agent_pipeline(session, module, force_close=force_close)
    if module.type == ModuleType.ROUTE:
        return _run_route_pipeline(session, module, force_close=force_close)
    return _run_fsm_pipeline(session, module, force_close=force_close)


def chat_turn(
        query: str,
        session_id: str,
        all_sessions: Dict[str, Session],
        store: Optional["SessionStore"] = None,
) -> ChatResult:
    """Process one user dialogue turn, returning the full output (text +
    reserved actions).

    1. Locate the session by session_id, reset cxt at start of turn
       (lifecycle.begin_turn)
    2. Validate the pattern / entry module, resolve the LLM config (R1)
    3. Same-turn hop loop (max_hops): dispatch handling by module type;
       consume the ModuleJumpEvents in cxt.actions → reroute to the target
       module to continue the reply in the same turn; exceeding the hop
       budget forces a force_close close-out
    4. End of turn: append to history (lifecycle.end_turn), snapshot the
       output into ChatResult

    ``store`` is consumed by history compression (None skips it); message
    persistence is done per message by the message_sink attached at
    launch/restore time and does not go through this parameter.
    """
    # ------------------------------------------------------------------
    # 1. Locate the session; start-of-turn reset (user_query overwrite +
    #    per-turn fields zeroed — exactly once, before hopping)
    # ------------------------------------------------------------------
    session = all_sessions.get(session_id)
    if session is None:
        logger.warning("会话不存在: %s", session_id)
        return ChatResult(text="会话不存在，请先发起对话任务")

    _lifecycle.begin_turn(session.cxt, query)

    pattern = session.pattern
    if pattern is None:
        logger.warning("会话 %s 未绑定对话模板", session_id)
        return ChatResult(text="对话模板未配置")

    # ------------------------------------------------------------------
    # 2. Locate the entry module (cxt.current_module_code first, fall back
    #    to the entry)
    # ------------------------------------------------------------------
    current_module_code = session.cxt.current_module_code or pattern.entry_module_code
    if not current_module_code:
        logger.warning("会话 %s 未找到入口模块", session_id)
        return ChatResult(text="入口模块未配置")

    # Write back to cxt: stages and transitions (jump detection /
    # _fsm_node_transition) both read the current position from cxt
    session.cxt.current_module_code = current_module_code

    current_module = pattern.module_map.get(current_module_code)
    if current_module is None:
        logger.warning("模块不存在: %s", current_module_code)
        return ChatResult(text=f"模块 '{current_module_code}' 不存在")

    session.cxt.metadata["pattern_code"] = session.pattern_code

    # R1: resolve the LLM config by current position each turn, override takes precedence (spec §4)
    try:
        _refresh_llm_config(session)
    except Exception as e:
        logger.error("加载 LLM 配置失败: %s", e)
        return ChatResult(text=f"LLM 配置加载失败: {e}")

    # History compression (silently skipped when the store is disabled /
    # threshold is 0 / too few messages; summarizes with the llm_config R1
    # just refreshed; failure never blocks the dialogue). Must run before
    # add user — compression rebuilds history and fixes turn_history_start
    maybe_compress(session, store)

    # Record user message
    session.cxt.add_message("user", query, stage="chat")

    # ------------------------------------------------------------------
    # 3. Reentry loop: consume same-turn jump events (cxt.actions channel)
    # ------------------------------------------------------------------
    max_hops = getattr(pattern, "max_hops", 2)
    try:
        for hop in range(max_hops):
            current_module = pattern.module_map[
                session.cxt.current_module_code or pattern.entry_module_code
            ]
            result = _handle_module(session, current_module)

            event = _jumps.pop(session.cxt)
            if event is None:
                response = result.reply or ""
                break
            logger.info(
                "same-turn jump 第 %d 跳: → %s (source=%s)",
                hop + 1, event.target_module_code, event.source,
            )
            _jumps.reroute(session.cxt, event)
        else:
            # Max hops exceeded: first consume the leftover event to land on
            # the final target, then force-close with that module
            logger.warning("达到 max_hops=%d，强制收尾", max_hops)
            pending = _jumps.pop(session.cxt)
            if pending is not None:
                _jumps.reroute(session.cxt, pending)
            current_module = pattern.module_map[
                session.cxt.current_module_code or pattern.entry_module_code
            ]
            result = _handle_module(session, current_module, force_close=True)
            response = result.reply or ""
    except Exception as e:
        logger.exception("对话处理异常: session=%s", session_id)
        # 对外脱敏：异常细节可能含路径/配置信息，只回统一话术（细节已进日志）
        response = "对话处理异常，请稍后重试"

    # ------------------------------------------------------------------
    # 4. End of turn: append the assistant message to history
    #    (incremental), snapshot the output
    # ------------------------------------------------------------------
    _lifecycle.end_turn(session.cxt, response)

    return build_chat_result(response, session.cxt)


# ---------------------------------------------------------------------------
# Compat re-exports (test anchors, signatures unchanged)
# ---------------------------------------------------------------------------

_handle_node_transition = _fsm_node_transition  # noqa: F401 (clarify test anchor)


def chat(
        query: str,
        session_id: str,
        all_sessions: Dict[str, Session],
        store: Optional["SessionStore"] = None,
) -> str:
    """Compat entry: process one dialogue turn, returning the reply text (equivalent to chat_turn(...).text)."""
    return chat_turn(query, session_id, all_sessions, store=store).text
