"""Projection / defer semantics tests (plan-⑥; successor of
test_agent_inject_transfer.py — the transfer_to_XX same-turn handoff is
gone; git history has the pre-plan-⑥ suite).

Coverage:
- projection block filters by effective enable_project (jump targets
  invisible; force-projection honored)
- the generic defer_to_module tool: generated from projection adjacency,
  writes a DeferredModuleSwitch (NOT a same-turn jump), keeps answering
  this turn, end-of-turn base switch applies
- hallucinated defer target: error backfill, loop continues (protocol
  pairing preserved)
- force_close: no defer tool, close-out suffix present
- lent tools / projection recall / tool-trajectory pairing (retained from
  the transfer-era suite — unchanged behaviors)
"""

import json
from unittest.mock import patch

from nexus.engine.session import Session
from nexus.context import DeferredModuleSwitch, ModuleJumpEvent
from nexus.model.module import AgentModule
from nexus.model.pattern import Pattern
from nexus.registry.tools import registry as tool_registry

import atoms.executors  # noqa: F401 -- default executors registered


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


def _mk_session(after_sales_project=True):
    after_sales = AgentModule(
        module_code="after_sales",
        module_name="售后维保",
        module_description="保养预约、维修工单办理",
        module_todo_description="查改保养预约",
        answer_examples=["已为您改到{时间}。"],
        use_tools=["mock_lent_tool"],
        sub_modules=["reception"],
        enable_project=after_sales_project,
    )
    reception = AgentModule(
        module_code="reception",
        module_name="前台接待",
        module_description="接待与分诊",
        sub_modules=[
            {"target": "after_sales", "lend_knowledge": True,
             "lend_tools": ["mock_lent_tool"]},
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
    """Returns scripted responses in order; records received messages/tools."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def achat_completion(self, messages, model, temperature, max_tokens,
                               tools=None, tool_choice=None, **kw):
        self.seen.append({"messages": messages, "tools": tools})
        return self.script.pop(0)


def _run(s, provider, module_code="reception", force_close=False):
    from async_utils import arun
    from nexus.engine.loop import run_agent
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        return arun(run_agent(s, s.cxt.module_map[module_code],
                              s.cxt.metadata["llm_override"],
                              force_close=force_close))


# ---------------------------------------------------------------------------
# Projection block (enable_project filtering)
# ---------------------------------------------------------------------------

def test_projection_block_contains_knowledge_and_tools():
    from nexus.engine.messages import build_projection_block
    s = _mk_session()
    block = build_projection_block(
        s.cxt.module_map["reception"], s.cxt.module_map, cxt=s.cxt)
    assert "售后维保" in block
    assert "保养预约" in block
    assert "mock_lent_tool" in block
    assert "defer_to_module" in block  # plan-⑥ guidance line


def test_projection_block_hides_jump_targets():
    """enable_project=False adjacency is a same-turn jump target: no
    projection block for it."""
    from nexus.engine.messages import build_projection_block
    s = _mk_session(after_sales_project=False)
    block = build_projection_block(
        s.cxt.module_map["reception"], s.cxt.module_map, cxt=s.cxt)
    assert "售后维保" not in block


def test_projection_block_honors_force_projection():
    """A force-projected module (jumped away earlier) projects even when it
    declared enable_project=False (anti-ping-pong)."""
    from nexus.engine.messages import build_projection_block
    s = _mk_session(after_sales_project=False)
    s.cxt.metadata["forced_projection"] = ["after_sales"]
    block = build_projection_block(
        s.cxt.module_map["reception"], s.cxt.module_map, cxt=s.cxt)
    assert "售后维保" in block


# ---------------------------------------------------------------------------
# The generic defer tool
# ---------------------------------------------------------------------------

def test_defer_tool_generated_from_projection_adjacency():
    from atoms.executors.loop_executor import _build_defer_tool
    s = _mk_session()
    tools = _build_defer_tool(s.cxt.module_map["reception"], s.pattern, s.cxt)
    assert len(tools) == 1
    fn = tools[0]["function"]
    assert fn["name"] == "defer_to_module"
    assert fn["parameters"]["properties"]["module_code"]["enum"] == ["after_sales"]


def test_defer_tool_empty_for_jump_only_adjacency():
    from atoms.executors.loop_executor import _build_defer_tool
    s = _mk_session(after_sales_project=False)
    assert _build_defer_tool(s.cxt.module_map["reception"], s.pattern,
                             s.cxt) == []


def test_defer_writes_deferred_switch_and_keeps_answering():
    """defer hit: the turn KEEPS answering (no empty reply, no ModuleJumpEvent);
    a DeferredModuleSwitch lands in actions; other tool_calls of the same
    response execute for real."""
    s = _mk_session()
    s.cxt.add_message("user", "售后流程太复杂，帮我全程处理", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的，先帮您查工单", "tool_calls": [
            {"id": "c1", "function": {"name": "mock_lent_tool",
                                      "arguments": "{}"}},
            {"id": "c2", "function": {
                "name": "defer_to_module",
                "arguments": '{"module_code": "after_sales",'
                             ' "reason": "售后深入流程"}'}},
        ]},
        {"content": "已查到工单并登记转接，稍后由售后专员为您全程跟进。",
         "tool_calls": []},
    ])
    result = _run(s, provider)
    # this turn answered (unlike the transfer era's silent handoff)
    assert result.content == "已查到工单并登记转接，稍后由售后专员为您全程跟进。"
    # no same-turn jump; the deferred switch is pending in actions
    assert not [a for a in s.cxt.actions if isinstance(a, ModuleJumpEvent)]
    switches = [a for a in s.cxt.actions
                if isinstance(a, DeferredModuleSwitch)]
    assert len(switches) == 1
    assert switches[0].target_module_code == "after_sales"
    assert switches[0].source == "projection"
    # the normal tool of the same response executed for real (not synthetic)
    hist_tools = [m for m in s.cxt.history if m.role == "tool"]
    assert len(hist_tools) == 2  # mock_lent_tool row + defer ack row
    assert not hist_tools[0].metadata.get("synthetic")
    # state not transitioned mid-turn
    assert s.cxt.current_module_code == "reception"


def test_chat_applies_deferred_switch_end_of_turn():
    """The chat layer applies the switch at END of turn (after the hop loop):
    the NEXT turn's base is the target; the source is force-projected."""
    from nexus.engine.chat import chat as chat_fn

    s = _mk_session()
    sessions = {"s": s}
    provider = ScriptedProvider([
        # reception: defer + answer this turn
        {"content": "好的，我先答您并登记切换", "tool_calls": [{
            "id": "c1", "function": {
                "name": "defer_to_module",
                "arguments": '{"module_code": "after_sales",'
                             ' "reason": "深入流程"}'}}]},
        {"content": "本轮答复完成，后续由售后底座承接。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        from async_utils import arun
        text = arun(chat_fn(query="帮我全程处理售后", session_id="s",
                            all_sessions=sessions))
    assert text == "本轮答复完成，后续由售后底座承接。"
    # end-of-turn switch applied: next turn runs on after_sales
    assert s.cxt.current_module_code == "after_sales"
    # source force-projected (anti-ping-pong record)
    assert "reception" in (s.cxt.metadata.get("forced_projection") or [])
    # the switch stays observable in the turn's actions snapshot? (consumed
    # then re-appended — check via the still-present event object)
    assert any(isinstance(a, DeferredModuleSwitch) for a in s.cxt.actions)


def test_defer_hallucinated_target_backfills_and_continues():
    """A defer target outside the projection adjacency: error backfill, the
    loop continues, the model self-corrects to a direct reply."""
    s = _mk_session()
    s.cxt.add_message("user", "我要办个神奇业务", stage="chat")
    provider = ScriptedProvider([
        {"content": "尝试登记切换", "tool_calls": [{
            "id": "c1", "function": {
                "name": "defer_to_module",
                "arguments": '{"module_code": "ghost", "reason": "x"}'}}]},
        {"content": "好的，我直接为您处理。", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.content == "好的，我直接为您处理。"
    assert s.cxt.current_module_code == "reception"
    assert not [a for a in s.cxt.actions
                if isinstance(a, DeferredModuleSwitch)]
    tool_rows = [m for m in s.cxt.history if m.role == "tool"]
    assert "无效" in tool_rows[0].content


def test_defer_rejection_backfills_all_tool_calls():
    """Same-response normal tool + invalid defer: both get tool rows (every
    tool_call_id answered — API 400 guard), the normal one executed."""
    s = _mk_session()
    s.cxt.add_message("user", "查工单顺便办个神奇业务", stage="chat")
    provider = ScriptedProvider([
        {"content": "查询并尝试登记", "tool_calls": [
            {"id": "c1", "function": {"name": "mock_lent_tool",
                                      "arguments": '{"query": "工单"}'}},
            {"id": "c2", "function": {
                "name": "defer_to_module",
                "arguments": '{"module_code": "ghost", "reason": "x"}'}},
        ]},
        {"content": "好的，为您处理完毕。", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.content == "好的，为您处理完毕。"
    second = provider.seen[1]["messages"]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}
    assert "无效" in tool_msgs[1]["content"]


def test_force_close_no_defer_tool_and_prompt():
    """force_close: no defer tool injected, the close-out suffix present."""
    s = _mk_session()
    s.cxt.add_message("user", "帮我处理售后", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的，我直接处理。", "tool_calls": []},
    ])
    _run(s, provider, force_close=True)
    first = provider.seen[0]
    tool_names = [t["function"]["name"] for t in (first["tools"] or [])]
    assert "defer_to_module" not in tool_names
    assert "勿再移交" in first["messages"][0]["content"]


# ---------------------------------------------------------------------------
# Retained behaviors (unchanged by plan-⑥)
# ---------------------------------------------------------------------------

def test_run_agent_direct_reply_with_lent_tool():
    """inject path: A borrows a tool and answers -> TurnResult(content) plus lent_by bookkeeping."""
    s = _mk_session()
    s.cxt.add_message("user", "查下我的工单", stage="chat")
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [{"id": "c1", "function": {
            "name": "mock_lent_tool", "arguments": "{}"}}]},
        {"content": "您的工单已查到，预计明天完工。", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.content == "您的工单已查到，预计明天完工。"
    assert s.cxt.metadata["served_by_projection"] == {
        "module": "reception", "source": "after_sales"}
    tool_msgs = [m for m in s.cxt.history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].metadata.get("lent_by") == "after_sales"


def test_lent_tools_respect_pattern_acl():
    from nexus.engine.loop import _resolve_lent_tools
    s = _mk_session()
    reception = s.cxt.module_map["reception"]
    schemas, lent_by = _resolve_lent_tools(reception, s.pattern)
    names = [t["function"]["name"] for t in schemas]
    assert "acl_locked_tool" not in names
    assert "mock_lent_tool" in names
    assert lent_by["mock_lent_tool"] == "after_sales"


def test_projection_recall_scoped_to_borrower():
    """The look-back block is injected only on the borrower's own turns."""
    s = _mk_session()
    s.cxt.metadata["served_by_projection"] = {
        "module": "reception", "source": "after_sales"}
    s.cxt.add_message("user", "继续", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的，继续为您处理。", "tool_calls": []},
    ])
    _run(s, provider)
    assert "上一轮提示" in provider.seen[0]["messages"][0]["content"]


def test_tool_round_ids_paired_in_history():
    """Normal tool round: assistant payload tool_calls pair one-to-one with
    tool row metadata ids."""
    from nexus.context import decode_tool_call_content
    s = _mk_session()
    s.cxt.add_message("user", "查下我的工单", stage="chat")
    provider = ScriptedProvider([
        {"content": "查询中", "tool_calls": [{"id": "c1", "function": {
            "name": "mock_lent_tool", "arguments": '{}'}}]},
        {"content": "查到了。", "tool_calls": []},
    ])
    _run(s, provider)

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
