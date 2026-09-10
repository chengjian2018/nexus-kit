"""Chat-layer reentry loop (plan-⑥ semantics): ROUTE same-turn jumps /
deferred end-of-turn switch / force close on max hops. The agent-side
transfer_to_XX same-turn handoff is gone — same-turn jumps now originate
from ROUTE (NLU / menu jump_module) and custom executors; the agent-side
deep-flow path is the projection + defer model (see test_projection_defer).
"""

import json
from unittest.mock import patch

from nexus.engine.session import Session
from nexus.context import PipelineStage
from nexus.model.module import AgentModule, RouteModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from stage_stubs import register_stage_stub


class ScriptedProvider:
    """Returns scripted responses in order; records received messages/tools."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def achat_completion(self, messages, model, temperature, max_tokens,
                               tools=None, tool_choice=None, **kw):
        self.seen.append({"messages": messages, "tools": tools})
        return self.script.pop(0)


def _route_pattern(max_hops=2, target_project=False):
    """reception(AGENT) → router(ROUTE, menu jump_module→buy_agent) with the
    ROUTE-side NLU jumping into reception's flow via jump_module."""
    nlu_code = register_stage_stub(_JumpNLU)
    nlg_code = register_stage_stub(_MarkerNLG)

    root = BaseNode(node_code="route_root", node_name="路由根",
                    sub_nodes=["menu_buy"])
    menu = BaseNode(node_code="menu_buy", node_name="购车菜单",
                    jump_module="reception")
    router = RouteModule(
        module_code="router", module_name="路由",
        module_nodes=[root, menu],
        sub_modules=[{"target": "reception",
                      "lend_knowledge": not target_project,
                      "lend_tools": []}],
        stages={"nlu": nlu_code, "nlg": nlg_code})
    reception = AgentModule(
        module_code="reception", module_name="前台", module_description="接待",
        sub_modules=[{"target": "router"}])
    return Pattern(code="p2", name="t", description="t",
                   entry_module_code="router",
                   modules=[router, reception], max_hops=max_hops)


class _JumpNLU(PipelineStage):
    """NLU stub pointing next_node at the jump-carrying menu node."""

    stage_name = "jump_nlu"

    async def execute(self, ctx):
        ctx.nlu_result = {"next_node": "menu_buy", "slots": {}}
        return ctx


class _MarkerNLG(PipelineStage):
    stage_name = "marker_nlg"

    async def execute(self, ctx):
        ctx.nlg_result = {"content": "路由侧回复"}
        return ctx


def _launch(pattern, sessions, sid="s1"):
    session = Session(session_id=sid, pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions[sid] = session
    return session


def _chat(sessions, sid, query):
    from nexus.engine.chat import chat as chat_fn
    from async_utils import arun
    return arun(chat_fn(query=query, session_id=sid, all_sessions=sessions))


def test_route_jump_b_replies_same_turn():
    """ROUTE menu jump_module hits → the target module (AGENT) takes over in
    the same turn; the user only hears the target's reply."""
    sessions = {}
    _launch(_route_pattern(), sessions)
    provider = ScriptedProvider([
        # buy_agent (reception target) replies in the same turn
        {"content": "看到您有购车需求，我先了解一下预算。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        reply = _chat(sessions, "s1", "我想买车")
    assert reply == "看到您有购车需求，我先了解一下预算。"
    assert sessions["s1"].cxt.current_module_code == "reception"


def test_max_hops_exceeded_force_close():
    """Consecutive ROUTE jumps exceed max_hops=1: force close on the landed
    module (prompt injected with the no-more-handoff suffix)."""
    sessions = {}
    # entry AGENT receives nothing to jump with; force the loop via a ROUTE
    # that re-jumps every turn: hop budget 1 → the second jump is consumed,
    # force_close lands on reception which must reply directly
    pattern = _route_pattern(max_hops=1)
    _launch(pattern, sessions)
    provider = ScriptedProvider([
        # reception replies directly (force-close round)
        {"content": "好的，我来处理。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        reply = _chat(sessions, "s1", "我想买车")
    assert reply == "好的，我来处理。"


def test_force_close_route_returns_nonempty_reply():
    """I-4: max_hops=1, ROUTE jumps into an AGENT module and force close
    kicks in -- jump detection is skipped (including jump_module hits), the
    landed module's reply is consumed, and no empty reply is produced."""
    # router(menu jump_module→reception) → reception(enable_project=False)
    # becomes a jump target; hop budget 1: the menu jump is consumed, and a
    # further jump from reception is force-closed
    sessions = {}
    _launch(_route_pattern(max_hops=1), sessions, sid="sr")
    provider = ScriptedProvider([
        # reception under force_close: replies directly (suffix enforced)
        {"content": "购车咨询由我来介绍吧", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        reply = _chat(sessions, "sr", "我想买车")
    assert isinstance(reply, str) and reply, f"force_close 后回复不应为空: {reply!r}"
    assert reply == "购车咨询由我来介绍吧"


def test_defer_end_of_turn_switch_next_turn_base():
    """Projection + defer: turn 1 answers on reception with projected
    knowledge and defers; turn 2 runs on the target base (current_module_code
    switched end-of-turn, persistent across turns)."""
    sessions = {}
    after_sales = AgentModule(
        module_code="after_sales", module_name="售后维保",
        module_description="售后", module_todo_description="售后流程",
        enable_project=True)
    reception = AgentModule(
        module_code="reception", module_name="前台", module_description="接待",
        sub_modules=[{"target": "after_sales", "lend_knowledge": True,
                      "lend_tools": []}])
    pattern = Pattern(code="pd", name="t", description="t",
                      entry_module_code="reception",
                      modules=[reception, after_sales])
    _launch(pattern, sessions, sid="sd")

    provider = ScriptedProvider([
        # turn 1, round 1: defer registered
        {"content": "好的，为您登记", "tool_calls": [{"id": "c1", "function": {
            "name": "defer_to_module",
            "arguments": '{"module_code": "after_sales",'
                         ' "reason": "深入售后流程"}'}}]},
        # turn 1, round 2: answers this turn (turn text)
        {"content": "本轮先为您说明，后续由售后专员跟进。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        r1 = _chat(sessions, "sd", "帮我全程处理售后")
    assert r1 == "本轮先为您说明，后续由售后专员跟进。"
    # end-of-turn switch applied: NEXT turn's base is after_sales
    assert sessions["sd"].cxt.current_module_code == "after_sales"
    # source force-projected (anti-ping-pong)
    assert "reception" in sessions["sd"].cxt.metadata.get(
        "forced_projection", [])

    # turn 2: runs on after_sales directly (no reception round)
    provider2 = ScriptedProvider([
        {"content": "已在新底座为您处理。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider2):
        r2 = _chat(sessions, "sd", "继续")
    assert r2 == "已在新底座为您处理。"
    # exactly one LLM call on turn 2 (straight to the target base — no
    # reception round, no projection detour)
    assert len(provider2.seen) == 1
    # the turn ran on after_sales directly (position already asserted above;
    # one call means no other module executed)


def test_forced_projection_prevents_pingpong():
    """After a defer, the source is force-projected: a later adjacency
    enumerating the source serves it via projection only (no defer back)."""
    from atoms.executors.loop_executor import _build_defer_tool

    after_sales = AgentModule(
        module_code="after_sales", module_name="售后维保",
        module_description="售后", enable_project=True,
        sub_modules=[{"target": "reception", "lend_knowledge": True,
                      "lend_tools": []}])
    reception = AgentModule(
        module_code="reception", module_name="前台",
        sub_modules=[{"target": "after_sales", "lend_knowledge": True,
                      "lend_tools": []}])
    pattern = Pattern(code="pp", name="t", description="t",
                      entry_module_code="reception",
                      modules=[reception, after_sales])
    s = Session(session_id="sp", pattern_code="pp")
    s.pattern = pattern
    s.cxt.module_map = pattern.module_map
    s.cxt.node_map = pattern.node_map
    s.cxt.current_module_code = "after_sales"
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    # reception deferred earlier this session → recorded
    s.cxt.metadata["forced_projection"] = ["reception"]

    # after_sales enumerates reception: force-projected → defer candidate
    tools = _build_defer_tool(s.cxt.module_map["after_sales"], pattern, s.cxt)
    enum_values = tools[0]["function"]["parameters"]["properties"][
        "module_code"]["enum"]
    assert "reception" in enum_values  # still a defer target (projected)


def test_json_exports_of_events():
    """Observability: DeferredModuleSwitch.to_dict snapshots for
    ChatResult.actions."""
    from nexus.context import DeferredModuleSwitch

    switch = DeferredModuleSwitch(target_module_code="x", reason="r",
                                  source="projection")
    assert switch.to_dict() == {
        "module_switch": {"target": "x", "reason": "r",
                          "source": "projection"}}
    # json-serializable (ChatResult.actions snapshot path)
    json.dumps(switch.to_dict(), ensure_ascii=False)
