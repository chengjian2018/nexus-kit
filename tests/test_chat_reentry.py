"""Chat-layer graph semantics: the module hop loop is dead —
same-turn handoff / cross-turn relay / budget close-out are all expressed by
the AGENT graph runtime:

- same-turn relay   : a node executor's routing output (TurnResult.next,
                      must be within sub_nodes) continues to the target in
                      the SAME turn — the user only hears the target's reply
- cross-turn relay  : a metadata flag written by one turn's node drives the
                      next turn's routing (the former defer/projection
                      semantics, now a conditional edge reading cxt state)
- budget close-out  : a routing cycle exhausts config.max_steps → the
                      force-close reply ends the run (no empty replies)

Custom executors express the routing (no LLM needed for the routing nodes);
the default_loop node consumes a ScriptedProvider.
"""

import json
from unittest.mock import patch

import atoms.executors  # noqa: F401 -- default executors registered
from async_utils import arun
from nexus.engine.chat import chat_turn
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.session import Session
from nexus.engine.turn_result import TurnResult
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.plugins import registry as plugin_registry


class ScriptedProvider:
    """Returns scripted responses in order; records received messages/tools."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def achat_completion(self, messages, model, temperature, max_tokens,
                               tools=None, tool_choice=None, **kw):
        self.seen.append({"messages": messages, "tools": tools})
        return self.script.pop(0)


# ---------------------------------------------------------------------------
# Custom executors (routing logic without LLM)
# ---------------------------------------------------------------------------

class _RouterExecutor(NodeExecutor):
    """root: routes to the node named in the query (same-turn relay)."""

    async def execute(self, ec: ExecutionContext) -> TurnResult:
        target = "target" if "转" in (ec.cxt.user_query or "") else ""
        if target:
            return TurnResult(next=target)
        return TurnResult(content="root 直答")


class _FlagReceptionExecutor(NodeExecutor):
    """reception: the former defer semantics — turn 1 answers here and
    raises the handoff flag; turn 2 (graph rerun from entry) sees the flag
    and routes to after_sales in the same turn (no reception detour)."""

    async def execute(self, ec: ExecutionContext) -> TurnResult:
        if ec.cxt.metadata.get("handoff_to") == "after_sales":
            ec.cxt.metadata.pop("handoff_to", None)
            return TurnResult(next="after_sales")   # conditional edge on cxt state
        ec.cxt.metadata["handoff_to"] = "after_sales"
        return TurnResult(content="好的，为您登记（本轮先答复，下轮售后接力）")


class _WaitExecutor(NodeExecutor):
    async def execute(self, ec: ExecutionContext) -> TurnResult:
        if ec.resume_input is None:
            return TurnResult(content="请确认是否继续", wait_human=True)
        return TurnResult(content=f"已按 {ec.resume_input} 处理")


plugin_registry.register("executor", "re_router", _RouterExecutor)
plugin_registry.register("executor", "re_flag_reception", _FlagReceptionExecutor)
plugin_registry.register("executor", "re_wait", _WaitExecutor)


def _launch(pattern, sessions, sid="s1"):
    session = Session(session_id=sid, pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions[sid] = session
    return session


def _chat(sessions, sid, query):
    from async_utils import arun
    with patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        result = arun(chat_turn(query=query, session_id=sid,
                                all_sessions=sessions))
    return result if isinstance(result, str) else result.text


# ---------------------------------------------------------------------------
# Same-turn relay: routing output continues to the target in the same turn
# ---------------------------------------------------------------------------

def test_route_target_replies_same_turn():
    """root routes to target (within sub_nodes) → the target (default_loop
    with a scripted provider) takes over in the same turn; the user only
    hears the target's reply."""
    pattern = Pattern(code="p2", name="t", description="t", nodes=[
        BaseNode(code="root", name="路由", sub_nodes=["target"],
                 plugins={"loop": "re_router"}),
        BaseNode(code="target", name="目标",
                 base_prompt="目标节点人设"),
    ])
    sessions = {}
    _launch(pattern, sessions)
    provider = ScriptedProvider([
        {"content": "看到您的需求，我先了解一下细节。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        reply = _chat(sessions, "s1", "帮我转过去")
    assert reply == "看到您的需求，我先了解一下细节。"
    # the target's node material (base_prompt from node.config) served the call
    assert provider.seen[0]["messages"][0]["content"].startswith("目标节点人设")
    # the graph ran root → target and terminated on the target's empty routing
    assert sessions["s1"].cxt.current_node_code == "target"
    assert sessions["s1"].cxt.graph_state == {}


def test_undeclared_route_terminates_with_reply():
    """A routing output outside sub_nodes terminates the run (the declared
    graph is authoritative) — the run's last non-empty reply is kept."""
    class _HalluExecutor(NodeExecutor):
        async def execute(self, ec: ExecutionContext) -> TurnResult:
            return TurnResult(next="ghost_node", content="路由中")

    plugin_registry.register("executor", "re_hallu", _HalluExecutor)
    pattern = Pattern(code="ph", name="t", description="t", nodes=[
        BaseNode(code="only", name="唯一", sub_nodes=["declared"],
                 plugins={"loop": "re_hallu"}),
        BaseNode(code="declared", name="声明后继"),
    ])
    sessions = {}
    _launch(pattern, sessions, sid="sh")
    reply = _chat(sessions, "sh", "跑")
    assert reply == "路由中"
    assert sessions["sh"].cxt.graph_state == {}


# ---------------------------------------------------------------------------
# Cross-turn relay: a metadata flag drives the next turn's routing
# ---------------------------------------------------------------------------

def test_flag_relay_next_turn_runs_on_target_node():
    """Turn 1 answers on reception and raises the handoff flag; turn 2 reruns
    the graph from entry, reception routes straight to after_sales (one LLM
    call, on the target node's material) and clears the flag."""
    pattern = Pattern(code="pd", name="t", description="t", nodes=[
        BaseNode(code="reception", name="前台", sub_nodes=["after_sales"],
                 plugins={"loop": "re_flag_reception"}),
        BaseNode(code="after_sales", name="售后",
                 base_prompt="售后维保人设"),
    ])
    sessions = {}
    _launch(pattern, sessions, sid="sd")

    # turn 1: reception answers here (no LLM) and raises the flag
    r1 = _chat(sessions, "sd", "帮我全程处理售后")
    assert r1 == "好的，为您登记（本轮先答复，下轮售后接力）"
    assert sessions["sd"].cxt.metadata["handoff_to"] == "after_sales"

    # turn 2: graph reruns from entry; reception routes by the flag → the
    # after_sales node (default_loop) replies — exactly one LLM call
    provider2 = ScriptedProvider([
        {"content": "已在新底座为您处理。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider2):
        r2 = _chat(sessions, "sd", "继续")
    assert r2 == "已在新底座为您处理。"
    assert len(provider2.seen) == 1                    # straight to the target node
    system_row = provider2.seen[0]["messages"][0]
    assert system_row["role"] == "system"
    assert system_row["content"].startswith("售后维保人设")
    assert sessions["sd"].cxt.metadata.get("handoff_to") is None  # flag consumed
    assert sessions["sd"].cxt.current_node_code == "after_sales"


# ---------------------------------------------------------------------------
# Budget close-out: max_steps cycle guard
# ---------------------------------------------------------------------------

def test_max_steps_exhaustion_force_close_reply():
    """A self-routing cycle exhausts max_steps → the force-close reply ends
    the run (never an empty reply)."""
    class _CycleExecutor(NodeExecutor):
        async def execute(self, ec: ExecutionContext) -> TurnResult:
            return TurnResult(next="loop_node")

    plugin_registry.register("executor", "re_cycle", _CycleExecutor)
    pattern = Pattern(code="pc", name="t", description="t", max_steps=2,
                      nodes=[BaseNode(code="loop_node", name="环",
                                      sub_nodes=["loop_node"],
                                      plugins={"loop": "re_cycle"})])
    sessions = {}
    _launch(pattern, sessions, sid="sc")
    reply = _chat(sessions, "sc", "我想买车")
    assert reply == "抱歉，处理超时，请稍后重试。"   # force-close fallback
    assert sessions["sc"].cxt.graph_state == {}      # state board cleared

    # with intermediate content: the LAST non-empty reply is kept
    class _TalkativeCycle(NodeExecutor):
        async def execute(self, ec: ExecutionContext) -> TurnResult:
            return TurnResult(content=f"第{ec.step}步", next="loop_node")

    plugin_registry.register("executor", "re_talk_cycle", _TalkativeCycle)
    pattern2 = Pattern(code="pc2", name="t", description="t", max_steps=3,
                       nodes=[BaseNode(code="loop_node", name="环",
                                       sub_nodes=["loop_node"],
                                       plugins={"loop": "re_talk_cycle"})])
    sessions2 = {}
    _launch(pattern2, sessions2, sid="sc2")
    reply2 = _chat(sessions2, "sc2", "跑")
    assert reply2 == "第2步"


# ---------------------------------------------------------------------------
# Suspension observability: wait action snapshots as JSON (the former
# DeferredModuleSwitch JSON-export slot)
# ---------------------------------------------------------------------------

def test_wait_turn_actions_json_snapshot():
    """A wait_human turn's ChatResult.actions carries the graph_wait
    observation dict, JSON-serializable for the API layer."""
    pattern = Pattern(code="pw", name="t", description="t", nodes=[
        BaseNode(code="n1", name="审批", plugins={"loop": "re_wait"}),
    ])
    sessions = {}
    _launch(pattern, sessions, sid="sw")
    with patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        result = arun(chat_turn("开始", "sw", sessions))
    assert result.text == "请确认是否继续"
    assert result.actions and result.actions[0].get("graph_wait")
    payload = result.actions[0]["graph_wait"]
    assert payload["node"] == "n1"
    json.dumps(result.actions, ensure_ascii=False)   # wire-safe

    # resume turn: the user message reaches the waiting node and the graph ends
    result2 = _chat(sessions, "sw", "同意")
    assert result2 == "已按 同意 处理"
    assert sessions["sw"].cxt.graph_state == {}
