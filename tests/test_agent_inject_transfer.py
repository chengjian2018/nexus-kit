"""run_agent tests: projection injection / transfer jump events / tool round-trip persistence."""

import json
from unittest.mock import patch

from nexus.engine.session import Session
from nexus.context import DialogueContext, ModuleJumpEvent, SessionMessage
from nexus.model.module import AgentModule
from nexus.model.pattern import Pattern
from nexus.registry.tools import registry as tool_registry


# ---------------------------------------------------------------------------
# Neutral mock tools: module-level self-registration, no interference with built-in tools
# ---------------------------------------------------------------------------

def _mock_lent_tool_handler(args, **kwargs):
    return json.dumps({"ok": True, "tool": "mock_lent_tool", "args": args},
                      ensure_ascii=False)


tool_registry.register(
    name="mock_lent_tool",
    toolset="test_lent",
    schema={
        "name": "mock_lent_tool",
        "description": "测试用借出工具",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "查询内容"}},
        },
    },
    handler=_mock_lent_tool_handler,
    allowed_patterns={"p": ["after_sales"]},
)


def _acl_locked_tool_handler(args, **kwargs):
    return json.dumps({"ok": True, "tool": "acl_locked_tool"},
                      ensure_ascii=False)


tool_registry.register(
    name="acl_locked_tool",
    toolset="test_lent",
    schema={
        "name": "acl_locked_tool",
        "description": "仅授权给其他 pattern 的工具（ACL 锁定）",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "查询内容"}},
        },
    },
    handler=_acl_locked_tool_handler,
    allowed_patterns={"other_pattern": ["after_sales"]},
)


def _mk_session():
    after_sales = AgentModule(
        module_code="after_sales",
        module_name="售后维保",
        module_description="保养预约、维修工单办理",
        module_todo_description="查改保养预约",
        answer_examples=["已为您改到{时间}。"],
        use_tools=["mock_lent_tool"],
        sub_modules=["reception"],
    )
    reception = AgentModule(
        module_code="reception",
        module_name="前台接待",
        module_description="接待与分诊",
        sub_modules=[
            {"target": "after_sales", "lend_tools": ["mock_lent_tool"]},
        ],
    )
    p = Pattern(code="p", name="t", description="t",
                entry_module_code="reception",
                modules=[reception, after_sales])
    s = Session(session_id="s", pattern_code="p")
    s.pattern = p
    s.cxt.module_map = p.module_map
    s.cxt.node_map = p.node_map
    s.cxt.current_module_code = "reception"
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
        item = self.script.pop(0)
        return item


def test_projection_block_contains_knowledge_and_tools():
    from nexus.engine.messages import build_projection_block
    s = _mk_session()
    block = build_projection_block(
        s.cxt.module_map["reception"], s.cxt.module_map)
    assert "售后维保" in block
    assert "保养预约" in block
    assert "mock_lent_tool" in block


def test_transfer_tools_generated_per_link():
    from nexus.engine.loop import build_transfer_tools
    s = _mk_session()
    tools = build_transfer_tools(
        s.cxt.module_map["reception"], s.cxt.module_map)
    names = [t["function"]["name"] for t in tools]
    assert names == ["transfer_to_after_sales"]
    desc = tools[0]["function"]["description"]
    assert "售后维保" in desc


def test_run_agent_direct_reply_with_lent_tool():
    """inject path: A borrows a tool and answers -> TurnResult(reply) plus lent_by bookkeeping."""
    from nexus.engine.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "查下我的工单", stage="chat")
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [{"id": "c1", "function": {
            "name": "mock_lent_tool", "arguments": "{}"}}]},
        {"content": "您的工单已查到，预计明天完工。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert result.content == "您的工单已查到，预计明天完工。"
    assert not [a for a in s.cxt.actions if isinstance(a, ModuleJumpEvent)]
    assert s.cxt.metadata["served_by_projection"] == {
        "module": "reception", "source": "after_sales"}
    tool_msgs = [m for m in s.cxt.history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].metadata.get("lent_by") == "after_sales"


def test_run_agent_transfer_writes_jump_event():
    """transfer path: A calls the transfer tool → writes a ModuleJumpEvent to cxt.actions,
    reply stays empty and is not emitted; state transition is left to the chat layer's hop loop (run_agent does not change state)."""
    from nexus.engine.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "我要投诉整个售后流程", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的这就为您处理", "tool_calls": [{
            "id": "c1", "function": {"name": "transfer_to_after_sales",
                                     "arguments": '{"reason": "售后投诉"}'}}]},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert result.content is None or result.content == ""
    events = [a for a in s.cxt.actions if isinstance(a, ModuleJumpEvent)]
    assert len(events) == 1
    assert events[0].target_module_code == "after_sales"
    assert events[0].reason == "售后投诉"
    assert events[0].source == "handoff_tool"
    # state is not transitioned by run_agent (the chat layer transitions when it consumes the event)
    assert s.cxt.current_module_code == "reception"
    # A's content is not emitted but is kept in history (suppressed)
    suppressed = [m for m in s.cxt.history
                  if m.role == "assistant" and m.metadata.get("suppressed")]
    assert len(suppressed) == 1


def test_run_agent_transfer_rejected_backfills_error_and_continues():
    """transfer target absent from module_map → the error is backfilled as the tool result and the loop continues with a normal reply."""
    from nexus.engine.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "我要办个神奇业务", stage="chat")
    provider = ScriptedProvider([
        {"content": "尝试移交", "tool_calls": [{
            "id": "c1", "function": {"name": "transfer_to_ghost",
                                     "arguments": '{"reason": "不存在"}'}}]},
        {"content": "好的，我直接为您处理。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert result.content == "好的，我直接为您处理。"
    assert s.cxt.current_module_code == "reception"
    assert not [a for a in s.cxt.actions if isinstance(a, ModuleJumpEvent)]
    tool_msgs = [m for m in s.cxt.history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].metadata.get("tool_name") == "transfer_to_ghost"
    assert "转移目标不存在" in tool_msgs[0].content
    second = provider.seen[1]["messages"]
    assert second[-1]["role"] == "tool"
    assert "转移目标不存在" in second[-1]["content"]
    assistant_msgs = [m for m in s.cxt.history
                      if m.role == "assistant" and m.metadata.get("suppressed")]
    assert not assistant_msgs


def test_lent_tools_respect_pattern_acl():
    """I-1: the lend path is bound by the same pattern-level tool ACL (deny-by-default is not bypassed)."""
    from nexus.engine.loop import _resolve_lent_tools
    s = _mk_session()
    reception = s.cxt.module_map["reception"]
    p = s.pattern
    schemas, lent_by = _resolve_lent_tools(reception, p)
    names = [t["function"]["name"] for t in schemas]
    # tools not ACL-authorized for p/after_sales cannot be borrowed
    assert "acl_locked_tool" not in names
    assert "acl_locked_tool" not in lent_by
    # ACL-authorized tools can still be borrowed
    assert "mock_lent_tool" in names
    assert lent_by["mock_lent_tool"] == "after_sales"


def test_projection_recall_scoped_to_borrower():
    """I-3: the look-back block is injected only on the borrower's own turns (served_by_projection resets at turn start)."""
    from nexus.engine.loop import run_agent
    s = _mk_session()
    s.cxt.metadata["served_by_projection"] = {
        "module": "reception", "source": "after_sales"}
    s.cxt.add_message("user", "继续", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的，继续为您处理。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert "上一轮提示" in provider.seen[0]["messages"][0]["content"]


def test_rejected_transfer_backfills_all_tool_calls():
    """M-5: same-turn normal tool + invalid transfer; when rejected, both are backfilled (avoids an API 400)."""
    from nexus.engine.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "查工单顺便办个神奇业务", stage="chat")
    provider = ScriptedProvider([
        {"content": "查询并尝试移交", "tool_calls": [
            {"id": "c1", "function": {"name": "mock_lent_tool",
                                      "arguments": '{"query": "工单"}'}},
            {"id": "c2", "function": {"name": "transfer_to_ghost",
                                      "arguments": '{"reason": "不存在"}'}},
        ]},
        {"content": "好的，为您处理完毕。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert result.content == "好的，为您处理完毕。"
    # the second round's messages end with two role=tool rows (every tool_call_id gets a response)
    second = provider.seen[1]["messages"]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}
    assert "转移目标不存在" in tool_msgs[1]["content"]
    hist_tools = [m for m in s.cxt.history if m.role == "tool"]
    assert len(hist_tools) == 2


def test_force_close_no_transfer_tools_and_prompt():
    """M-6(b): under force_close no transfer tools are injected and the prompt contains the force-close suffix."""
    from nexus.engine.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "帮我处理售后", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的，我直接处理。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"],
                  force_close=True)
    first = provider.seen[0]
    assert first["tools"] is not None
    tool_names = [t["function"]["name"] for t in first["tools"]]
    assert not any(n.startswith("transfer_to_") for n in tool_names)
    assert "勿再移交" in first["messages"][0]["content"]


def test_chat_hop_consumes_transfer_event_same_turn():
    """The transfer event is consumed by the chat layer's hop loop: the target module answers in the same turn."""
    from nexus.engine.chat import chat as chat_fn

    s = _mk_session()
    sessions = {"s": s}
    provider = ScriptedProvider([
        # A (reception): transfer
        {"content": "转接中", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_after_sales",
            "arguments": '{"reason": "售后深入"}'}}]},
        # B (after_sales) answers in the same turn
        {"content": "看到您有售后需求，已为您登记。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        text = chat_fn(query="帮我处理售后", session_id="s", all_sessions=sessions)
    assert text == "看到您有售后需求，已为您登记。"
    assert s.cxt.current_module_code == "after_sales"
    assert not [a for a in s.cxt.actions if isinstance(a, ModuleJumpEvent)]
    # B's reply enters via the agent loop (two LLM calls: A's transfer + B's answer)
    assert len(provider.seen) == 2


# ---------------------------------------------------------------------------
# tool trajectory recorded completely (payload form: assistant tool-round content JSON + tool row metadata id)
# ---------------------------------------------------------------------------

def test_tool_round_ids_paired_in_history():
    """Normal tool round: assistant payload tool_calls pair one-to-one with tool row metadata ids."""
    from nexus.engine.loop import run_agent
    from nexus.context import decode_tool_call_content
    s = _mk_session()
    s.cxt.add_message("user", "查下我的工单", stage="chat")
    provider = ScriptedProvider([
        {"content": "查询中", "tool_calls": [{"id": "c1", "function": {
            "name": "mock_lent_tool", "arguments": '{}'}}]},
        {"content": "查到了。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])

    hist = [m for m in s.cxt.history if m.stage == "agent"]
    assistant_calls = [m for m in hist
                       if m.role == "assistant"
                       and decode_tool_call_content(m.content) is not None]
    assert len(assistant_calls) == 1
    text, calls = decode_tool_call_content(assistant_calls[0].content)
    assert text == "查询中"
    call_ids = {tc["id"] for tc in calls}
    tool_ids = {m.metadata.get("tool_call_id")
                for m in hist if m.role == "tool"}
    assert call_ids == tool_ids == {"c1"}


def test_transfer_turn_synthesizes_all_tool_results():
    """transfer round: the same response mixes normal tools + transfer; all are synthesized into tool rows with ids fully paired."""
    from nexus.engine.loop import run_agent
    from nexus.context import decode_tool_call_content
    s = _mk_session()
    s.cxt.add_message("user", "查完给我转售后", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的，查完就转", "tool_calls": [
            {"id": "c1", "function": {"name": "mock_lent_tool",
                                      "arguments": '{}'}},
            {"id": "c2", "function": {"name": "transfer_to_after_sales",
                                      "arguments": '{"reason": "售后深入"}'}},
        ]},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"],
                           s.cxt.metadata["llm_override"])
    assert result.content in (None, "")

    suppressed = [m for m in s.cxt.history
                  if m.role == "assistant" and m.metadata.get("suppressed")]
    assert len(suppressed) == 1
    text, calls = decode_tool_call_content(suppressed[0].content)
    assert text == "好的，查完就转"
    assert len(calls) == 2

    synthetic = [m for m in s.cxt.history if m.role == "tool"]
    assert len(synthetic) == 2
    assert {m.metadata.get("tool_call_id") for m in synthetic} == {"c1", "c2"}
    assert all(m.metadata.get("synthetic") for m in synthetic)
    moved = [m for m in synthetic if m.content.startswith("[已移交至模块")]
    skipped = [m for m in synthetic if m.content.startswith("[未执行")]
    assert len(moved) == 1 and len(skipped) == 1


def test_rejected_transfer_records_tool_calls_on_assistant():
    """Hallucinated-target error-backfill path: the assistant payload carries tool_calls and the tool row carries the id."""
    from nexus.engine.loop import run_agent
    from nexus.context import decode_tool_call_content
    s = _mk_session()
    s.cxt.add_message("user", "我要办个神奇业务", stage="chat")
    provider = ScriptedProvider([
        {"content": "尝试移交", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_ghost", "arguments": '{}'}}]},
        {"content": "好的，我直接处理。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider", return_value=provider):
        run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])

    agent_hist = [m for m in s.cxt.history if m.stage == "agent"]
    call_assistants = [m for m in agent_hist
                       if m.role == "assistant"
                       and decode_tool_call_content(m.content) is not None]
    assert len(call_assistants) == 1
    text, calls = decode_tool_call_content(call_assistants[0].content)
    assert text == "尝试移交"
    assert calls[0]["id"] == "c1"
    tools = [m for m in agent_hist if m.role == "tool"]
    assert tools[0].metadata["tool_call_id"] == "c1"
