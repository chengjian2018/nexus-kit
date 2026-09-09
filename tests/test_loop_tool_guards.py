"""Loop main-flow tool guards (migrated from test_agent_hooks_loop.py's
non-hooks cases, plan-④): hallucinated-name interception + protocol-paired
replay of synthetic/ordinary tool rows. These behaviors belong to the loop's
own validation/replay machinery, not to hooks — they stay pinned while the
hooks behavioral suite is retired.
"""

import json
from unittest.mock import patch

from nexus.engine.session import Session
from nexus.model.module import AgentModule
from nexus.model.pattern import Pattern
from nexus.registry.tools import registry as tool_registry

import atoms.executors  # noqa: F401 -- default_loop must be registered


def _echo_handler(args, **kwargs):
    return json.dumps({"ok": True, "tool": "guard_echo_tool", "args": args},
                      ensure_ascii=False)


tool_registry.register(
    name="guard_echo_tool",
    toolset="test_loop_guards",
    schema={
        "name": "guard_echo_tool",
        "description": "loop guard 测试回声工具",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市"}},
        },
    },
    handler=_echo_handler,
    allowed_patterns={"pg": ["main"]},
)


def _locked_handler(args, **kwargs):
    return json.dumps({"ok": True, "tool": "guard_locked_tool"},
                      ensure_ascii=False)


tool_registry.register(
    name="guard_locked_tool",
    toolset="test_loop_guards",
    schema={
        "name": "guard_locked_tool",
        "description": "已注册但仅授权其他 pattern 的工具（ACL 锁定）",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市"}},
        },
    },
    handler=_locked_handler,
    allowed_patterns={"other_pattern": ["main"]},
)


def _mk_session():
    main = AgentModule(
        module_code="main", module_name="主模块",
        module_description="主模块描述",
        use_tools=["guard_echo_tool"],
    )
    peer = AgentModule(module_code="peer", module_name="同侪",
                       module_description="同侪模块",
                       use_tools=["guard_echo_tool"])
    p = Pattern(code="pg", name="t", description="t",
                entry_module_code="main", modules=[main, peer])
    s = Session(session_id="sg", pattern_code="pg")
    s.pattern = p
    s.cxt.module_map = p.module_map
    s.cxt.node_map = p.node_map
    s.cxt.current_module_code = "main"
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    return s


class ScriptedProvider:
    """Returns scripted responses in order; records received messages."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def chat_completion(self, messages, model, temperature=0.7,
                        max_tokens=2048, tools=None, tool_choice=None):
        self.seen.append({"messages": list(messages), "tools": tools})
        return self.script.pop(0)


def _tool_call(cid="c1", name="guard_echo_tool", arguments="{}"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _run(session, provider):
    from nexus.engine.loop import run_agent
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        return run_agent(session, session.cxt.module_map["main"],
                         {"code": "x", "model": "m"})


def _tool_rows(cxt):
    return [m for m in cxt.history if m.role == "tool"]


# ---------------------------------------------------------------------------
# Hallucinated-name interception + ACL seal
# ---------------------------------------------------------------------------

def test_hallucinated_name_backfills_error_with_tools_list():
    """Unregistered hallucinated name: intercepted without execution, the
    fed-back error includes the available tool list, and the model
    self-corrects next round."""
    s = _mk_session()
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [
            _tool_call(name="no_such_tool", arguments="{}")]},
        {"content": "改好了，直接回答。", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.content == "改好了，直接回答。"
    rows = _tool_rows(s.cxt)
    error = json.loads(rows[0].content)["error"]
    assert "no_such_tool" in error
    assert "guard_echo_tool" in error
    assert rows[0].metadata.get("synthetic") is True
    # The string fed back to the model is the same one (the self-correct signal)
    tool_row = next(m for m in provider.seen[1]["messages"]
                    if m["role"] == "tool")
    assert "no_such_tool" in tool_row["content"]


def test_registered_but_unauthorized_name_intercepted():
    """Registered but unauthorized (ACL bypass sealed): intercepted, handler
    not executed (error JSON instead of output)."""
    s = _mk_session()
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [
            _tool_call(name="guard_locked_tool", arguments="{}")]},
        {"content": "直接回答。", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.content == "直接回答。"
    rows = _tool_rows(s.cxt)
    payload = json.loads(rows[0].content)
    assert "不存在或本轮不可用" in payload["error"]
    assert "ok" not in payload          # the locked tool's handler produced no output
    assert rows[0].metadata.get("synthetic") is True


# ---------------------------------------------------------------------------
# Protocol-paired replay
# ---------------------------------------------------------------------------

def test_synthetic_error_row_replays_paired():
    """Synthetic tool rows of the hallucinated round: replay pairing is
    complete, no degradation to untrusted wrapping."""
    from nexus.engine.messages import default_build_messages

    s = _mk_session()
    s.cxt.user_query = "查天气"
    s.cxt.add_message("user", "查天气", stage="chat")
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [
            _tool_call(name="no_such_tool", arguments="{}")]},
        {"content": "直接回答。", "tool_calls": []},
    ])
    _run(s, provider)
    s.cxt.turn_history_start = 0
    msgs = default_build_messages(s.cxt.module_map["main"], s.cxt)
    asst = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
    assert len(asst) == 1 and asst[0]["tool_calls"][0]["id"] == "c1"
    tool_rows = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_rows) == 1 and tool_rows[0]["tool_call_id"] == "c1"
    assert not any("untrusted" in (m.get("content") or "") for m in msgs)


def test_ordinary_round_replays_paired():
    """An executed tool round's history: protocol-shaped replay
    (assistant.tool_calls paired with tool rows)."""
    from nexus.engine.messages import default_build_messages

    s = _mk_session()
    s.cxt.user_query = "查天气"
    s.cxt.add_message("user", "查天气", stage="chat")
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [
            _tool_call(arguments='{"city": "北京"}')]},
        {"content": "done", "tool_calls": []},
    ])
    _run(s, provider)
    s.cxt.turn_history_start = 0
    msgs = default_build_messages(s.cxt.module_map["main"], s.cxt)
    asst = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
    assert len(asst) == 1
    assert json.loads(
        asst[0]["tool_calls"][0]["function"]["arguments"]) == {"city": "北京"}
    assert len([m for m in msgs if m["role"] == "tool"]) == 1
    assert not any("untrusted" in (m.get("content") or "") for m in msgs)
