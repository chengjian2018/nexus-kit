"""Loop main-flow tool guards (plan-⑧ node form): hallucinated-name
interception + protocol-paired replay of synthetic/ordinary tool rows +
max-rounds forced termination. These behaviors belong to the loop's own
validation/replay machinery — driven here through the AGENT graph runtime
with a scripted provider.
"""

import json
from async_utils import arun
from unittest.mock import patch

from nexus.engine.chat import chat_turn
from nexus.engine.session import Session
from nexus.model.node import BaseNode
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
)


def _locked_handler(args, **kwargs):
    return json.dumps({"ok": True, "tool": "guard_locked_tool"},
                      ensure_ascii=False)


tool_registry.register(
    name="guard_locked_tool",
    toolset="test_loop_guards",
    schema={
        "name": "guard_locked_tool",
        "description": "已注册但不在本节点 use_tools 授权内的工具",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市"}},
        },
    },
    handler=_locked_handler,
)


def _mk_session():
    # 节点只授权 guard_echo_tool；guard_locked_tool 越出 use_tools（新版 ACL 语义）
    node = BaseNode(code="main", name="主节点", use_tools=["guard_echo_tool"])
    p = Pattern(code="pg", name="t", description="t",
                allow_toolset=["test_loop_guards"], nodes=[node])
    s = Session(session_id="sg", pattern_code="pg")
    s.pattern = p
    s.cxt.node_map = p.node_map
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    return s


class ScriptedProvider:
    """Returns scripted responses in order; records received messages."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        self.seen.append({"messages": list(messages), "tools": tools})
        return self.script.pop(0)


def _tool_call(cid="c1", name="guard_echo_tool", arguments="{}"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _run(session, provider):
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider), \
         patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        return arun(chat_turn("查一下", session.session_id,
                              {session.session_id: session}))


def _tool_rows(cxt):
    return [m for m in cxt.history if m.role == "tool"]


# ---------------------------------------------------------------------------
# Hallucinated-name interception + authorization seal
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
    assert result.text == "改好了，直接回答。"
    rows = _tool_rows(s.cxt)
    error = json.loads(rows[0].content)["error"]
    assert "no_such_tool" in error
    assert "guard_echo_tool" in error
    assert rows[0].metadata.get("synthetic") is True
    # The string fed back to the model is the same one (the self-correct signal)
    tool_row = next(m for m in provider.seen[1]["messages"]
                    if m.get("role") == "tool")
    assert tool_row["content"] == rows[0].content


def test_registered_but_unauthorized_name_intercepted():
    """已注册但不在 use_tools 授权内的工具：handler 不执行，错误回填。"""
    s = _mk_session()
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [
            _tool_call(name="guard_locked_tool", arguments="{}")]},
        {"content": "好的，换个方式答。", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.text == "好的，换个方式答。"
    rows = _tool_rows(s.cxt)
    assert len(rows) == 1
    assert json.loads(rows[0].content).get("error")
    assert rows[0].metadata.get("synthetic") is True
    # 模型实际可见的工具列表里没有 guard_locked_tool
    names = {t["function"]["name"] for t in provider.seen[0]["tools"]}
    assert names == {"guard_echo_tool"}


# ---------------------------------------------------------------------------
# Protocol-paired replay of tool rows
# ---------------------------------------------------------------------------

def test_synthetic_error_row_replays_paired():
    s = _mk_session()
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [
            _tool_call(cid="c9", name="ghost_tool", arguments="{}")]},
        {"content": "已纠正。", "tool_calls": []},
    ])
    _run(s, provider)
    from nexus.engine.messages import _replay_segment
    replayed = _replay_segment(s.cxt.history)
    # assistant(tool_calls) + tool 行成对回放（合成错误行同样是协议行）
    assistant_rows = [m for m in replayed if m.get("role") == "assistant"]
    tool_rows_replay = [m for m in replayed if m.get("role") == "tool"]
    assert any(r.get("tool_calls") for r in assistant_rows)
    assert tool_rows_replay and tool_rows_replay[0]["tool_call_id"] == "c9"


def test_ordinary_round_replays_paired():
    s = _mk_session()
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [
            _tool_call(cid="c1", name="guard_echo_tool",
                       arguments='{"city": "北京"}')]},
        {"content": "查完了。", "tool_calls": []},
    ])
    _run(s, provider)
    from nexus.engine.messages import _replay_segment
    replayed = _replay_segment(s.cxt.history)
    tool_rows_replay = [m for m in replayed if m.get("role") == "tool"]
    assert len(tool_rows_replay) == 1
    payload = json.loads(tool_rows_replay[0]["content"])
    assert payload["ok"] is True and payload["tool"] == "guard_echo_tool"


# ---------------------------------------------------------------------------
# Max tool rounds forced termination
# ---------------------------------------------------------------------------

def test_max_tool_rounds_forced_termination():
    s = _mk_session()
    rounds = [{"content": None, "tool_calls": [
        _tool_call(cid=f"c{i}", name="guard_echo_tool", arguments="{}")]}
        for i in range(12)]
    provider = ScriptedProvider(rounds)
    result = _run(s, provider)
    assert result.text == "抱歉，处理超时，请稍后重试。"
    # 恰好执行 10 轮（第 11 个脚本未被消费）
    assert len(provider.seen) == 10
