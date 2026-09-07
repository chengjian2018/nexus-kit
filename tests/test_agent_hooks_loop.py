"""Integration tests for run_agent-side hooks: P1 injection / P2/P3 observation /
P6 transfer / P7 exits / no firing on ROUTE turns.

Idiom follows test_agent_inject_transfer.py: module-level registration of neutral
mock tools + ScriptedProvider + patch("nexus.engine.loop.build_provider").
"""

import json
from unittest.mock import patch

from nexus.engine.session import Session
from nexus.context import PipelineStage
from nexus.model.module import AgentModule, RouteModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.tools import registry as tool_registry


# ---------------------------------------------------------------------------
# Neutral mock tools (separate toolset / tool names, no interference with existing tests)
# ---------------------------------------------------------------------------

def _echo_handler(args, **kwargs):
    return json.dumps({"ok": True, "tool": "hook_echo_tool", "args": args},
                      ensure_ascii=False)


tool_registry.register(
    name="hook_echo_tool",
    toolset="test_hooks",
    schema={
        "name": "hook_echo_tool",
        "description": "hooks 测试回声工具",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市"}},
        },
    },
    handler=_echo_handler,
    allowed_patterns={"ph": ["main"]},
)


def _alt_handler(args, **kwargs):
    return json.dumps({"ok": True, "tool": "hook_alt_tool", "args": args},
                      ensure_ascii=False)


tool_registry.register(
    name="hook_alt_tool",
    toolset="test_hooks",
    schema={
        "name": "hook_alt_tool",
        "description": "hooks 测试改名目标工具",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市"}},
        },
    },
    handler=_alt_handler,
    allowed_patterns={"ph": ["main"]},
)


def _locked_handler(args, **kwargs):
    return json.dumps({"ok": True, "tool": "hook_locked_tool"},
                      ensure_ascii=False)


tool_registry.register(
    name="hook_locked_tool",
    toolset="test_hooks",
    schema={
        "name": "hook_locked_tool",
        "description": "已注册但仅授权其他 pattern 的工具（ACL 锁定）",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市"}},
        },
    },
    handler=_locked_handler,
    allowed_patterns={"other_pattern": ["main"]},
)


def _mk_hooks_session(pattern_hooks=None, module_hooks=None,
                      with_transfer=False):
    main = AgentModule(
        module_code="main",
        module_name="主模块",
        module_description="主模块描述",
        use_tools=["hook_echo_tool", "hook_alt_tool"],
        agent_hooks=module_hooks,
        sub_modules=["peer"] if with_transfer else None,
    )
    peer = AgentModule(module_code="peer", module_name="同侪",
                       module_description="同侪模块",
                       use_tools=["hook_echo_tool", "hook_alt_tool"])
    p = Pattern(code="ph", name="t", description="t",
                entry_module_code="main", modules=[main, peer],
                agent_hooks=pattern_hooks)
    s = Session(session_id="sh", pattern_code="ph")
    s.pattern = p
    s.cxt.module_map = p.module_map
    s.cxt.node_map = p.node_map
    s.cxt.current_module_code = "main"
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    return s


class ScriptedProvider:
    """Returns scripted responses in order; records received messages/tools for assertions."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def chat_completion(self, messages, model, temperature, max_tokens,
                        tools=None, tool_choice=None, **kw):
        self.seen.append({"messages": messages, "tools": tools})
        return self.script.pop(0)


def _run(s, provider):
    from nexus.engine.loop import run_agent
    with patch("nexus.engine.loop.build_provider", return_value=provider):
        return run_agent(s, s.cxt.module_map["main"],
                         s.cxt.metadata["llm_override"])


def _tool_call(cid="c1", name="hook_echo_tool", arguments="{}"):
    return {"id": cid, "function": {"name": name, "arguments": arguments}}


# ---------------------------------------------------------------------------
# P1: injected fragments into the system prompt
# ---------------------------------------------------------------------------

def test_p1_fragments_injected_into_system_prompt():
    """Multiple hook fragments are joined in declaration order into the extension-context injected block."""
    s = _mk_hooks_session(pattern_hooks={
        "on_agent_start": [lambda e: "店铺在售：A、B",
                           lambda e: "当前时段：午间"],
    })
    provider = ScriptedProvider([{"content": "好的。", "tool_calls": []}])
    result = _run(s, provider)
    assert result.reply == "好的。"
    system = provider.seen[0]["messages"][0]["content"]
    assert system.strip().startswith("## 扩展上下文")
    assert "店铺在售：A、B" in system and "当前时段：午间" in system
    assert system.index("店铺在售") < system.index("当前时段")


def test_p1_hook_failure_degrades_silently():
    """P1 hook raising: the conversation proceeds as usual, the missing fragment causes no error."""
    def boom(e):
        raise RuntimeError("取数失败")

    s = _mk_hooks_session(pattern_hooks={"on_agent_start": [boom]})
    provider = ScriptedProvider([{"content": "ok", "tool_calls": []}])
    result = _run(s, provider)
    assert result.reply == "ok"
    assert all("扩展上下文" not in (m.get("content") or "")
               for m in provider.seen[0]["messages"])


def test_p1_injection_precedes_force_close_suffix():
    """force_close: the injected block precedes the force-close suffix."""
    from nexus.engine.loop import run_agent
    s = _mk_hooks_session(pattern_hooks={
        "on_agent_start": [lambda e: "店铺在售：A"],
    })
    provider = ScriptedProvider([{"content": "直接答", "tool_calls": []}])
    with patch("nexus.engine.loop.build_provider", return_value=provider):
        run_agent(s, s.cxt.module_map["main"],
                  s.cxt.metadata["llm_override"], force_close=True)
    system = provider.seen[0]["messages"][0]["content"]
    assert system.index("店铺在售") < system.index("勿再移交")


# ---------------------------------------------------------------------------
# P2 / P3: observation points
# ---------------------------------------------------------------------------

def test_p2_p3_observer_events():
    calls, responses = [], []
    s = _mk_hooks_session(pattern_hooks={
        "on_llm_call": [calls.append],
        "on_llm_response": [responses.append],
    })
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [_tool_call()]},
        {"content": "done", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.reply == "done"
    assert len(calls) == 2 and len(responses) == 2
    # P2: messages is the real outbound object (same list by reference), plus model and round_idx
    assert calls[0].messages is provider.seen[0]["messages"]
    assert calls[0].model == "m"
    assert [c.round_idx for c in calls] == [0, 1]
    assert responses[0].tool_calls[0]["function"]["name"] == "hook_echo_tool"
    assert responses[1].content == "done"


# ---------------------------------------------------------------------------
# P6 / P7: transfer and the three exits
# ---------------------------------------------------------------------------

def test_p6_p7_transfer_outcome():
    seen = []
    s = _mk_hooks_session(with_transfer=True, pattern_hooks={
        "on_transfer": [lambda e: seen.append(("transfer", e.target, e.reason))],
        "on_agent_end": [lambda e: seen.append(("end", e.outcome,
                                                e.transfer_target))],
    })
    provider = ScriptedProvider([
        {"content": "转接", "tool_calls": [_tool_call(
            name="transfer_to_peer", arguments='{"reason": "深入流程"}')]},
    ])
    result = _run(s, provider)
    assert result.reply in (None, "")
    assert ("transfer", "peer", "深入流程") in seen
    assert ("end", "transfer", "peer") in seen


def test_p7_reply_and_max_rounds_outcomes():
    ends = []
    # Direct-reply exit
    s = _mk_hooks_session(pattern_hooks={"on_agent_end": [ends.append]})
    provider = ScriptedProvider([{"content": "答案", "tool_calls": []}])
    result = _run(s, provider)
    assert result.reply == "答案"
    assert ends[-1].outcome == "reply" and ends[-1].reply == "答案"
    assert ends[-1].rounds == 1

    # Max-rounds-exceeded exit: 10 consecutive tool-call rounds
    ends.clear()
    s2 = _mk_hooks_session(pattern_hooks={"on_agent_end": [ends.append]})
    script = [{"content": None, "tool_calls": [_tool_call(cid=f"c{i}")]}
              for i in range(10)]
    result2 = _run(s2, ScriptedProvider(script))
    assert result2.reply == "抱歉，处理超时，请稍后重试。"
    assert ends[-1].outcome == "max_rounds" and ends[-1].rounds == 10


def test_module_hooks_replace_pattern_hooks_in_loop():
    """module.agent_hooks replaces wholesale: pattern-level hooks do not fire on that module's turn."""
    fired = []
    s = _mk_hooks_session(
        pattern_hooks={"on_agent_start": [lambda e: fired.append("pat")]},
        module_hooks={"on_agent_start": [lambda e: fired.append("mod")]},
    )
    provider = ScriptedProvider([{"content": "ok", "tool_calls": []}])
    _run(s, provider)
    assert fired == ["mod"]


# ---------------------------------------------------------------------------
# Scope guard: non-AGENT modules do not fire hooks
# ---------------------------------------------------------------------------

class _StaticNLG(PipelineStage):
    """Static stage that writes nlg_result itself (bypasses the LLM, verifying zero hook firing on ROUTE turns)."""

    stage_name = "static_nlg"

    def execute(self, ctx):
        ctx.nlg_result = {"content": "静态回复"}
        return ctx


def test_route_module_turn_does_not_fire_agent_hooks():
    from nexus.engine.chat import chat_turn
    fired = []
    route = RouteModule(
        module_code="root", module_name="路由", module_description="",
        module_nodes=[BaseNode(node_code="root", node_name="路由")],
    )
    p = Pattern(code="phr", name="t", description="t",
                entry_module_code="root", modules=[route],
                stages=[_StaticNLG()],
                agent_hooks={pt: [fired.append] for pt in
                             ("on_agent_start", "on_llm_call", "on_tool_call",
                              "on_tool_result", "on_transfer", "on_agent_end")})
    s = Session(session_id="shr", pattern_code="phr")
    s.pattern = p
    s.cxt.module_map = p.module_map
    s.cxt.node_map = p.node_map
    with patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        result = chat_turn("你好", "shr", {"shr": s})
    assert result.text == "静态回复"
    assert fired == []


# ---------------------------------------------------------------------------
# P4: tool-call rewrite (args / name) and three-way consistency
# ---------------------------------------------------------------------------

def _tool_rows(cxt):
    return [m for m in cxt.history if m.role == "tool"]


def _assistant_rows(cxt):
    return [m for m in cxt.history if m.role == "assistant"]


def test_p4_args_rewrite_consistent_across_execution_payload_feed():
    """Args rewrite consistent in three places: execution arguments / history assistant payload / fed-back messages;
    the tool row audits original_call; tool_call_id unchanged."""
    from nexus.engine.agent_hooks import RewriteToolCall

    def fix(e):
        return RewriteToolCall(args={"city": "杭州"})

    s = _mk_hooks_session(pattern_hooks={"on_tool_call": [fix]})
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [_tool_call(arguments='{"city": "北京"}')]},
        {"content": "done", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.reply == "done"

    # Execution side: the tool receives the rewritten args
    rows = _tool_rows(s.cxt)
    assert json.loads(rows[0].content)["args"] == {"city": "杭州"}

    # Payload side: the assistant row records the rewritten args, id untouched (rule 3)
    payload = json.loads(_assistant_rows(s.cxt)[0].content)
    call = payload["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"city": "杭州"}
    assert call["id"] == "c1"

    # Feed-back side: next round's in-loop messages match the payload
    asst_row = next(m for m in provider.seen[1]["messages"]
                    if m["role"] == "assistant" and m.get("tool_calls"))
    assert json.loads(
        asst_row["tool_calls"][0]["function"]["arguments"]) == {"city": "杭州"}

    assert rows[0].metadata.get("rewritten") is True
    assert rows[0].metadata.get("original_call") == {
        "name": "hook_echo_tool", "args": {"city": "北京"}}


def test_p4_name_rewrite_to_allowed_tool_executes_target():
    from nexus.engine.agent_hooks import RewriteToolCall

    def rename(e):
        return RewriteToolCall(name="hook_alt_tool")

    s = _mk_hooks_session(pattern_hooks={"on_tool_call": [rename]})
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [_tool_call(arguments="{}")]},
        {"content": "done", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.reply == "done"
    rows = _tool_rows(s.cxt)
    # The renamed tool is the one actually executed; metadata provenance follows the final name
    assert json.loads(rows[0].content)["tool"] == "hook_alt_tool"
    assert rows[0].metadata["tool_name"] == "hook_alt_tool"
    payload = json.loads(_assistant_rows(s.cxt)[0].content)
    assert payload["tool_calls"][0]["function"]["name"] == "hook_alt_tool"


def test_p4_rename_to_locked_tool_rejected():
    """Rename target registered but not authorized for this pattern (ACL-locked) → rename rejected, executes under the original name."""
    from nexus.engine.agent_hooks import RewriteToolCall

    def rename(e):
        return RewriteToolCall(name="hook_locked_tool")

    s = _mk_hooks_session(pattern_hooks={"on_tool_call": [rename]})
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [_tool_call(arguments="{}")]},
        {"content": "done", "tool_calls": []},
    ])
    _run(s, provider)
    rows = _tool_rows(s.cxt)
    assert json.loads(rows[0].content)["tool"] == "hook_echo_tool"
    assert rows[0].metadata["tool_name"] == "hook_echo_tool"
    payload = json.loads(_assistant_rows(s.cxt)[0].content)
    assert payload["tool_calls"][0]["function"]["name"] == "hook_echo_tool"


def test_p4_rename_to_transfer_prefix_rejected():
    from nexus.engine.agent_hooks import RewriteToolCall
    from nexus.context import ModuleJumpEvent

    def rename(e):
        return RewriteToolCall(name="transfer_to_peer")

    s = _mk_hooks_session(pattern_hooks={"on_tool_call": [rename]})
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [_tool_call(arguments="{}")]},
        {"content": "done", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.reply == "done"
    assert not [a for a in s.cxt.actions if isinstance(a, ModuleJumpEvent)]
    assert json.loads(_tool_rows(s.cxt)[0].content)["tool"] == "hook_echo_tool"


def test_p4_hook_failure_executes_original():
    def boom(e):
        raise RuntimeError("fix bug")

    s = _mk_hooks_session(pattern_hooks={"on_tool_call": [boom]})
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [_tool_call(arguments='{"city": "北京"}')]},
        {"content": "done", "tool_calls": []},
    ])
    _run(s, provider)
    rows = _tool_rows(s.cxt)
    assert json.loads(rows[0].content)["args"] == {"city": "北京"}
    assert "rewritten" not in rows[0].metadata


# ---------------------------------------------------------------------------
# P5: tool result rewrite
# ---------------------------------------------------------------------------

def test_p5_result_rewrite_feeds_llm_and_history():
    def redact(e):
        return e.result.replace("北京", "***")

    s = _mk_hooks_session(pattern_hooks={"on_tool_result": [redact]})
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [_tool_call(arguments='{"city": "北京"}')]},
        {"content": "done", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.reply == "done"
    rows = _tool_rows(s.cxt)
    assert "北京" not in rows[0].content and "***" in rows[0].content
    # The model is fed back the same redacted result (no divergence)
    tool_row = next(m for m in provider.seen[1]["messages"]
                    if m["role"] == "tool")
    assert "北京" not in tool_row["content"]
    assert rows[0].metadata.get("rewritten") is True
    assert "北京" in rows[0].metadata.get("original_result", "")


def test_p5_hook_failure_keeps_original_result():
    def boom(e):
        raise RuntimeError("redact bug")

    s = _mk_hooks_session(pattern_hooks={"on_tool_result": [boom]})
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [_tool_call(arguments='{"city": "北京"}')]},
        {"content": "done", "tool_calls": []},
    ])
    _run(s, provider)
    assert "北京" in _tool_rows(s.cxt)[0].content
    assert "rewritten" not in _tool_rows(s.cxt)[0].metadata


# ---------------------------------------------------------------------------
# Main-flow tool-name validation: hallucinated-name self-correct loop
# ---------------------------------------------------------------------------

def test_hallucinated_name_backfills_error_with_tools_list():
    """Unregistered hallucinated name: intercepted without execution, the fed-back error includes the available tool list, and the model self-corrects next round."""
    s = _mk_hooks_session()
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [
            _tool_call(name="no_such_tool", arguments="{}")]},
        {"content": "改好了，直接回答。", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.reply == "改好了，直接回答。"
    rows = _tool_rows(s.cxt)
    error = json.loads(rows[0].content)["error"]
    assert "no_such_tool" in error
    assert "hook_echo_tool" in error and "hook_alt_tool" in error
    assert rows[0].metadata.get("synthetic") is True
    # The string fed back to the model is the same one (the self-correct signal)
    tool_row = next(m for m in provider.seen[1]["messages"]
                    if m["role"] == "tool")
    assert "no_such_tool" in tool_row["content"]


def test_registered_but_unauthorized_name_intercepted():
    """Registered but unauthorized (ACL bypass sealed): intercepted, handler not executed (error JSON instead of output)."""
    s = _mk_hooks_session()
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [
            _tool_call(name="hook_locked_tool", arguments="{}")]},
        {"content": "直接回答。", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.reply == "直接回答。"
    rows = _tool_rows(s.cxt)
    payload = json.loads(rows[0].content)
    assert "不存在或本轮不可用" in payload["error"]
    assert "ok" not in payload          # the locked tool's handler produced no output
    assert rows[0].metadata.get("synthetic") is True


def test_synthetic_error_row_replays_paired():
    """Synthetic tool rows of the hallucinated round: replay pairing is complete, no degradation to untrusted wrapping."""
    from nexus.engine.messages import default_build_messages

    s = _mk_hooks_session()
    s.cxt.user_query = "查天气"
    s.cxt.add_message("user", "查天气", stage="chat")
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [
            _tool_call(name="no_such_tool", arguments="{}")]},
        {"content": "直接回答。", "tool_calls": []},
    ])
    _run(s, provider)
    s.cxt.turn_history_start = 0  # user row index (history starts empty; in-hop rows begin at 1)
    msgs = default_build_messages(s.cxt.module_map["main"], s.cxt)
    asst = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
    assert len(asst) == 1 and asst[0]["tool_calls"][0]["id"] == "c1"
    tool_rows = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_rows) == 1 and tool_rows[0]["tool_call_id"] == "c1"
    assert not any("untrusted" in (m.get("content") or "") for m in msgs)


def test_rewritten_round_replays_paired():
    """Rewritten round's history: protocol-shaped replay (assistant.tool_calls paired with tool rows)."""
    from nexus.engine.agent_hooks import RewriteToolCall
    from nexus.engine.messages import default_build_messages

    def fix(e):
        return RewriteToolCall(args={"city": "杭州"})

    s = _mk_hooks_session(pattern_hooks={"on_tool_call": [fix]})
    s.cxt.user_query = "查天气"
    s.cxt.add_message("user", "查天气", stage="chat")
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [_tool_call(arguments='{"city": "北京"}')]},
        {"content": "done", "tool_calls": []},
    ])
    _run(s, provider)
    s.cxt.turn_history_start = 0
    msgs = default_build_messages(s.cxt.module_map["main"], s.cxt)
    asst = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
    assert len(asst) == 1
    assert json.loads(
        asst[0]["tool_calls"][0]["function"]["arguments"]) == {"city": "杭州"}
    assert len([m for m in msgs if m["role"] == "tool"]) == 1
    assert not any("untrusted" in (m.get("content") or "") for m in msgs)


# ---------------------------------------------------------------------------
# transfer error-backfill branch: normal calls still go through P4/P5, the transfer call does not
# ---------------------------------------------------------------------------

def test_invalid_transfer_branch_fires_p4_p5_on_normal_tools_only():
    calls = []
    s = _mk_hooks_session(with_transfer=True, pattern_hooks={
        "on_tool_call": [lambda e: calls.append(("p4", e.tool_name))],
        "on_tool_result": [lambda e: calls.append(("p5", e.tool_name))],
    })
    provider = ScriptedProvider([
        {"content": "尝试移交", "tool_calls": [
            _tool_call(cid="t1", name="transfer_to_ghost",
                       arguments='{"reason": "不存在"}'),
            _tool_call(cid="t2", name="hook_echo_tool"),
        ]},
        {"content": "直接处理。", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.reply == "直接处理。"
    assert ("p4", "hook_echo_tool") in calls
    assert ("p5", "hook_echo_tool") in calls
    assert all(n != "transfer_to_ghost" for _, n in calls)
    by_id = {m.metadata["tool_call_id"]: m for m in _tool_rows(s.cxt)}
    assert "转移目标不存在" in by_id["t1"].content
    assert json.loads(by_id["t2"].content)["ok"] is True
    # P5 does not fire on the backfilled string: the transfer call has no P4/P5 record at all
    assert calls.count(("p4", "hook_echo_tool")) == 1
