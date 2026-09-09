"""Chat-layer reentry loop: same-turn transfer / ROUTE jumps / force close on max hops."""

from unittest.mock import patch

from nexus.engine.session import Session
from nexus.model.module import AgentModule, ModuleLink, RouteModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from test_agent_inject_transfer import ScriptedProvider, _mk_session


def _agent_pattern(**kw):
    """Two agents (reception -> after_sales) with optional max_hops."""
    after_sales = AgentModule(
        module_code="after_sales", module_name="售后维保",
        module_description="售后", module_todo_description="售后流程",
        sub_modules=["reception"])
    reception = AgentModule(
        module_code="reception", module_name="前台", module_description="接待",
        sub_modules=[ModuleLink(target="after_sales")])
    return Pattern(code="p2", name="t", description="t",
                   entry_module_code="reception",
                   modules=[reception, after_sales], **kw)


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
    return chat_fn(query=query, session_id=sid, all_sessions=sessions)


def test_same_turn_transfer_b_replies():
    """A transfers -> B takes over in the same turn; the user only hears B."""
    sessions = {}
    _launch(_agent_pattern(), sessions)
    provider = ScriptedProvider([
        # A: decides to transfer (content suppressed)
        {"content": "转接中", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_after_sales",
            "arguments": '{"reason": "售后深入"}'}}]},
        # B: takes over and replies
        {"content": "看到您有售后需求，我先了解一下具体情况。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        reply = _chat(sessions, "s1", "帮我处理售后")
    assert reply == "看到您有售后需求，我先了解一下具体情况。"
    assert sessions["s1"].cxt.current_module_code == "after_sales"


def test_max_hops_exceeded_force_close():
    """Consecutive transfers exceed max_hops=1: force close on the current
    module (prompt injected with the no-more-handoff suffix)."""
    sessions = {}
    _launch(_agent_pattern(max_hops=1), sessions)
    provider = ScriptedProvider([
        {"content": "转接中", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_after_sales", "arguments": "{}"}}]},
        # Force-close round: B still wants to transfer back but the limit is
        # reached -> it must reply directly
        {"content": "好的，我来处理您的售后问题。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        reply = _chat(sessions, "s1", "帮我处理售后")
    assert reply == "好的，我来处理您的售后问题。"


def test_force_close_route_returns_nonempty_reply():
    """I-4: max_hops=1, agent transfers into a ROUTE module and force close
    kicks in -- jump detection is skipped (including jump_module hits), the
    NLG reply is consumed, and no empty reply is produced."""
    from nexus.model.module import RouteModule
    from nexus.model.node import BaseNode
    from nexus.context import PipelineStage

    class _FakeRouteNLU(PipelineStage):
        stage_name = "fake_route_nlu"

        def execute(self, ctx):
            ctx.nlu_result = {"next_node": "menu_buy", "slots": {}}
            return ctx

    class _FakeRouteNLG(PipelineStage):
        stage_name = "fake_route_nlg"

        def execute(self, ctx):
            ctx.nlg_result = {"content": "购车咨询由我来介绍吧"}
            return ctx

    root = BaseNode(node_code="route_root", node_name="路由根",
                    sub_nodes=["menu_buy"])
    menu = BaseNode(node_code="menu_buy", node_name="购车咨询菜单",
                    jump_module="buy_agent")
    router = RouteModule(
        module_code="router", module_name="路由",
        module_nodes=[root, menu], sub_modules=["buy_agent"],
        generate={"nlu": _FakeRouteNLU(), "nlg": _FakeRouteNLG()})
    reception = AgentModule(
        module_code="reception", module_name="前台", module_description="接待",
        sub_modules=[ModuleLink(target="router")])
    buy_agent = AgentModule(
        module_code="buy_agent", module_name="购车专员", module_description="购车")
    pattern = Pattern(code="p_route", name="t", description="t",
                      entry_module_code="reception",
                      modules=[reception, router, buy_agent], max_hops=1)

    sessions = {}
    _launch(pattern, sessions, sid="sr")
    provider = ScriptedProvider([
        # hop 0: reception transfers into the ROUTE router module (same-turn reentry)
        {"content": "转接中", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_router",
            "arguments": '{"reason": "购车"}'}}]},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        reply = _chat(sessions, "sr", "我想买车")
    # force_close lands on ROUTE: no jump is triggered (otherwise menu_buy
    # would hit buy_agent -> empty reply)
    assert isinstance(reply, str) and reply, f"force_close 后回复不应为空: {reply!r}"
    assert reply == "购车咨询由我来介绍吧"
    assert sessions["sr"].cxt.current_module_code == "router"
    # force_close skips jump detection but still resets to root: menu nodes
    # have no sub_nodes; staying on menu_buy would leave the next turn's
    # RouteNLU with no routing candidates -> routing deadlock
    assert sessions["sr"].cxt.current_node_code == "route_root"
