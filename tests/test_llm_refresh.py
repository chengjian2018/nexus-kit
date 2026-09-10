"""R1-R4 injection refresh: per-turn resolution by current position + override priority (spec §4)."""

from unittest.mock import patch

from nexus.engine.session import Session
from nexus.model.module import FSMModule, RouteModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern


def _fsm_pattern():
    n1 = BaseNode(node_code="f1", node_name="节点一")
    n2 = BaseNode(node_code="f2", node_name="节点二")
    m = FSMModule(module_code="m1", module_name="m1", module_description="d",
                  module_todo_description="t", sub_modules=[],
                  module_nodes=[n1, n2])
    return Pattern(code="pf", name="t", description="t",
                   entry_module_code="m1", modules=[m])


def _launch(pattern, sessions, sid="s1"):
    session = Session(session_id=sid, pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions[sid] = session
    return session


def _chat(sessions, sid, query):
    from async_utils import arun
    from nexus.engine.chat import chat as chat_fn
    return arun(chat_fn(query=query, session_id=sid, all_sessions=sessions))


def _record_calls(calls):
    import nexus.engine.chat as chat_mod
    real = chat_mod.get_llm_config

    def spy(pattern_code="", module_code="", node_code="", override=None, config_path=""):
        calls.append(dict(pattern_code=pattern_code, module_code=module_code,
                          node_code=node_code, override=override))
        return real(pattern_code=pattern_code, module_code=module_code,
                    node_code=node_code, override=override,
                    config_path=config_path)
    return spy


def test_r1_passes_position_and_override():
    """R1: pattern/module/node + override all passed through, and metadata pattern_code written."""
    sessions = {}
    _launch(_fsm_pattern(), sessions)
    calls = []
    with patch("atoms.executors.loop_executor.build_provider"), \
         patch("nexus.engine.chat.get_llm_config", side_effect=_record_calls(calls)):
        _chat(sessions, "s1", "你好")
    assert calls, "R1 应调用 get_llm_config"
    first = calls[0]
    assert first["pattern_code"] == "pf"
    assert first["override"] == {"code": "x", "model": "m"}
    assert sessions["s1"].cxt.metadata["pattern_code"] == "pf"
    # once module/node are located within the session, R1 carries them (empty on the first turn)
    assert first["module_code"] in ("", "m1")


def test_r2_agent_module_chat_path_uses_module_code():
    """R2: an AGENT module going through the chat() path triggers AgentHandler,
    with get_llm_config called as module_code=<agent module code>, node_code=\"\"."""
    from nexus.model.module import AgentModule
    agent_m = AgentModule(module_code="reception", module_name=" reception",
                          module_description="d", module_todo_description="t",
                          sub_modules=[])
    pattern = Pattern(code="pa", name="t", description="t",
                      entry_module_code="reception", modules=[agent_m])
    sessions = {}
    _launch(pattern, sessions, sid="s3")
    calls = []

    class _Scripted:
        def chat_completion(self, messages, model, temperature, max_tokens,
                            tools=None, tool_choice=None, **kw):
            return {"content": "ok", "tool_calls": []}

    with patch("atoms.executors.loop_executor.build_provider", return_value=_Scripted()), \
         patch("nexus.engine.chat.get_llm_config",
               side_effect=_record_calls(calls)):
        _chat(sessions, "s3", "你好")
    r2 = [c for c in calls if c["module_code"] == "reception"
          and c["node_code"] == ""]
    assert r2, f"R2 应以 module_code=reception、node_code='' 解析，实际: {calls}"
    assert r2[0]["override"] == {"code": "x", "model": "m"}


def test_r3_refresh_after_node_resolution():
    """R3: the pipeline handler refreshes by module+node after node resolution."""
    sessions = {}
    _launch(_fsm_pattern(), sessions)
    calls = []
    with patch("atoms.executors.loop_executor.build_provider"), \
         patch("nexus.engine.chat.get_llm_config", side_effect=_record_calls(calls)):
        _chat(sessions, "s1", "你好")
    r3 = [c for c in calls if c["module_code"] == "m1" and c["node_code"] == "f1"]
    assert r3, f"R3 应按 module=m1 node=f1 解析，实际调用: {calls}"


def test_r4_route_menu_node_takes_effect_same_turn():
    """R4: after a ROUTE menu hit switches the node, the refresh takes effect that same turn
    (menu-node config drives that turn's NLG).

    The refresh point lives in chat._detect_jump_after_stage (the former _RouteNodeAdvance
    duty was merged in). After NLU updates nlu_result: advance the menu node + R4 node-level
    refresh first, then judge jumps.
    """
    menu = BaseNode(node_code="menu_a", node_name="菜单A",
                    base_nlg_prompt="回答A")
    root = BaseNode(node_code="root", node_name="根")
    route = RouteModule(module_code="r1", module_name="r", module_description="d",
                        module_todo_description="t", sub_modules=[],
                        module_nodes=[root, menu])
    fsm_m = _fsm_pattern().module_map["m1"]
    # RouteNLU/FSMNLU stubbed to return a menu-hit intent (bypassing the real LLM protocol)
    class _StubNLU:
        stage_name = "nlu"
        async def execute(self, ctx):
            ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
            return ctx
    class _StubNLG:
        stage_name = "nlg"
        async def execute(self, ctx):
            ctx.nlg_result = {"content": "ok"}
            return ctx
    from stage_stubs import register_stage_stub
    nlu_code = register_stage_stub(_StubNLU)
    nlg_code = register_stage_stub(_StubNLG)
    pattern = Pattern(code="pr", name="t", description="t",
                      entry_module_code="r1", modules=[route, fsm_m],
                      stages=[{"nlu": nlu_code}, {"nlg": nlg_code}])
    sessions = {}
    _launch(pattern, sessions, sid="s2")
    calls = []
    # R1-R3 and the R4 refresh all go through the chat namespace (R4 inside _detect_jump_after_stage)
    with patch("atoms.executors.loop_executor.build_provider"), \
         patch("nexus.engine.chat.get_llm_config", side_effect=_record_calls(calls)):
        _chat(sessions, "s2", "选A")
    r4 = [c for c in calls if c["node_code"] == "menu_a"]
    assert r4, f"R4 应在菜单命中后按 node=menu_a 刷新，实际调用: {calls}"
    # menu has no jump_module config -> no module jump, stays in the routing module
    assert sessions["s2"].cxt.current_node_code == "root"  # turn-end reset back to root
    assert sessions["s2"].cxt.current_module_code == "r1"


def test_override_wins_and_survives_turns():
    """The override lands in cxt.llm_config and is not washed away across turns."""
    sessions = {}
    _launch(_fsm_pattern(), sessions)
    with patch("atoms.executors.loop_executor.build_provider"):
        _chat(sessions, "s1", "你好")
        _chat(sessions, "s1", "继续")
    assert sessions["s1"].cxt.llm_config["model"] == "m"
