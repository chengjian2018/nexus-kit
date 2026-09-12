"""deep_research（plan-⑧ 四节点 AGENT 图）离线测试。

ScriptedProvider 与相位检测（请求特征锚点）自包含于本文件；图版机制覆盖：
1. 图结构 + AST 自动发现 + 四相位 executor 插件注册 + validate_pattern
2. 全研究轮：四节点同轮接力（node_start 顺序即流水线拓扑）/ 工具真实派发 /
   终态 trace 与旧版同构 / history 无 tool 行 / 图终止清空 graph_state
3. 预检索相位：工具结果进入 PLAN 请求；findings 汇入 SEARCH
4. PLAN 降级 / 自纠重试（相位级回归锚点）
5. SEARCH 轮次封顶后综合仍完成
6. 流式：delta 只来自 SYNTHESIZE；round 事件含 plan/search/synthesize
7. 第二轮从首站重跑（每轮全图重跑语义）
8. 孤儿入口防御：挂起游标落在 dr_plan（无在途状态）→ 跳过规划检索直奔综合
   （引擎 resume 机制 + 相位孤儿防御的复合回归）
"""

import json
import logging

import pytest
from unittest.mock import patch

from async_utils import arun

logging.basicConfig(level=logging.WARNING)

from apps.deep_research_agent import executor_multi
from apps.deep_research_agent.executor_multi import _MAX_SEARCH_ROUNDS, _STATE_KEY
from apps.deep_research_agent.prompts import PLAN_ANCHOR, PREPLAN_ANCHOR


# ============================================================================
# Fixtures / helpers
# ============================================================================

@pytest.fixture(scope="module")
def pattern():
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.deep_research_agent.route_multi" in imported, (
        f"route_multi 未被自动发现，已发现: {imported}"
    )
    return registry.get("deep_research")


@pytest.fixture()
def fake_mcp_tools():
    """Register the fake MCP search tool under the pattern's declared name
    (web_search_prime, toolset mcp-websearch——节点的 use_tools 声明名）。"""
    from nexus.registry.tools import registry as tool_registry

    calls = {"n": 0, "queries": []}

    def _search_handler(args):
        calls["n"] += 1
        query = args.get("query", "")
        calls["queries"].append(query)
        results = [{"title": f"MCP 测试结果 {i}",
                    "snippet": f"关于「{query}」的测试检索内容 {i}",
                    "url": f"https://example.com/{i}"}
                   for i in (1, 2)]
        return json.dumps({"results": results}, ensure_ascii=False)

    tool_registry.register(
        name="web_search_prime", toolset="mcp-websearch",
        schema={"name": "web_search_prime", "description": "测试检索工具",
                "parameters": {"type": "object",
                               "properties": {"query": {"type": "string"}},
                               "required": ["query"]}},
        handler=_search_handler,
    )
    yield calls
    calls["n"], calls["queries"] = 0, []


class DeepResearchScriptedProvider:
    """按相位特征脚本化的 provider(记录调用与请求特征供断言)。

    请求识别(与 executor 的 prompt 布局一一对应):
    - PREPLAN:最后一条 user 消息含 PREPLAN_ANCHOR
    - PLAN:最后一条 user 消息含 PLAN_ANCHOR
    - SEARCH:tools 参数非空
    - SYNTHESIZE:system 或 user 含 ``撰写最终研究报告``
    """

    def __init__(self, plan_json=None, search_rounds=1, report="# 研究报告",
                 preplan_rounds=0, plan_fail_first=False):
        self.plan_json = plan_json or json.dumps(
            {"sub_questions": ["子问题A", "子问题B"],
             "notes": "测试计划"}, ensure_ascii=False)
        self.search_rounds = search_rounds
        self.report = report
        self.preplan_rounds = preplan_rounds
        self.plan_fail_first = plan_fail_first
        self.call_count = 0
        self.search_calls = 0
        self.plan_calls = 0
        self.synth_calls = 0
        self.preplan_calls = 0
        self.requests = []

    def _kind(self, messages, tools):
        last_user = next((m["content"] for m in reversed(messages)
                          if m.get("role") == "user"), "")
        if PREPLAN_ANCHOR in (last_user or ""):
            return "preplan"
        if PLAN_ANCHOR in (last_user or ""):
            return "plan"
        if tools:
            return "search"
        if "撰写最终研究报告" in "".join(
                str(m.get("content", "")) for m in messages):
            return "synth"
        return "search"

    def _record(self, kind, messages):
        self.requests.append((kind, [dict(m) for m in messages]))

    def _preplan_reply(self):
        self.preplan_calls += 1
        if self.preplan_calls <= self.preplan_rounds:
            return {"content": None, "tool_calls": [{
                "id": f"p{self.preplan_calls}", "type": "function",
                "function": {"name": "web_search_prime",
                             "arguments": json.dumps(
                                 {"query": f"预检索查询{self.preplan_calls}"},
                                 ensure_ascii=False)},
            }], "finish_reason": "tool_calls"}
        return {"content": "跳过预检索。", "tool_calls": [],
                "finish_reason": "stop"}

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        self.call_count += 1
        kind = self._kind(messages, tools)
        self._record(kind, messages)
        if kind == "preplan":
            return self._preplan_reply()
        if kind == "plan":
            self.plan_calls += 1
            if self.plan_fail_first and self.plan_calls == 1:
                return {"content": "好的,我的计划是:先看子问题A再看子问题B。",
                        "tool_calls": [], "finish_reason": "stop"}
            return {"content": self.plan_json, "tool_calls": [],
                    "finish_reason": "stop"}
        if kind == "search":
            self.search_calls += 1
            if self.search_calls <= self.search_rounds:
                return {"content": None, "tool_calls": [{
                    "id": f"c{self.search_calls}", "type": "function",
                    "function": {"name": "web_search_prime",
                                 "arguments": json.dumps(
                                     {"query": f"测试查询{self.search_calls}"},
                                     ensure_ascii=False)},
                }], "finish_reason": "tool_calls"}
            return {"content": "信息已足够,开始综合。", "tool_calls": [],
                    "finish_reason": "stop"}
        self.synth_calls += 1
        return {"content": self.report, "tool_calls": [],
                "finish_reason": "stop"}

    async def achat_completion_stream(self, messages, model, temperature=0.7,
                                      max_tokens=2048, **kwargs):
        from nexus.llm.types import LLMChunk

        self.call_count += 1
        kind = self._kind(messages, kwargs.get("tools"))
        self._record(kind, messages)
        if kind == "synth":
            self.synth_calls += 1
            text = self.report
            cuts = max(1, len(text) // 3)
            pieces = [text[i:i + cuts] for i in range(0, len(text), cuts)]
            for p in pieces:
                yield LLMChunk(text=p)
            yield LLMChunk(finish_reason="stop")
            return
        if kind == "preplan":
            reply = self._preplan_reply()
            if reply["tool_calls"]:
                tc = dict(reply["tool_calls"][0], index=0)
                yield LLMChunk(tool_calls=[tc], finish_reason="tool_calls")
            else:
                yield LLMChunk(text=reply["content"], finish_reason="stop")
            return
        if kind == "plan":
            self.plan_calls += 1
            if self.plan_fail_first and self.plan_calls == 1:
                yield LLMChunk(text="好的,我的计划是:先看子问题A再看子问题B。",
                               finish_reason="stop")
                return
            yield LLMChunk(text=self.plan_json)
            yield LLMChunk(finish_reason="stop")
            return
        self.search_calls += 1
        if self.search_calls <= self.search_rounds:
            tc = {"index": 0, "id": f"c{self.search_calls}", "type": "function",
                  "function": {"name": "web_search_prime",
                               "arguments": json.dumps(
                                   {"query": f"测试查询{self.search_calls}"},
                                   ensure_ascii=False)}}
            yield LLMChunk(tool_calls=[tc], finish_reason="tool_calls")
        else:
            yield LLMChunk(text="信息已足够,开始综合。", finish_reason="stop")


def launch(pattern, sessions, session_id="s1"):
    from nexus.engine.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions[session_id] = session
    return session


def chat(sessions, session_id, query):
    from async_utils import arun
    from nexus.engine.chat import chat as chat_fn

    return arun(chat_fn(query=query, session_id=session_id,
                        all_sessions=sessions))


def run_research(pattern, provider, session_id="s1"):
    sessions = {}
    launch(pattern, sessions, session_id=session_id)
    with patch.object(executor_multi, "build_provider",
                      return_value=provider):
        reply = chat(sessions, session_id, "量子计算的最新进展是什么?")
    return sessions[session_id], reply


# ============================================================================
# 1. Pattern structure and registration
# ============================================================================

def test_pattern_structure_and_executor_binding(pattern):
    assert pattern.code == "deep_research"
    assert pattern.pattern_type == "agent"
    assert pattern.entry_node_code == "dr_preplan"

    codes = [n.code for n in pattern.nodes]
    assert codes == ["dr_preplan", "dr_plan", "dr_search", "dr_synthesize"]
    # 静态邻接 = 流水线拓扑；综合站终态
    assert pattern.node_map["dr_preplan"].sub_nodes == ["dr_plan"]
    assert pattern.node_map["dr_plan"].sub_nodes == ["dr_search",
                                                    "dr_synthesize"]  # 含孤儿逃生边
    assert pattern.node_map["dr_search"].sub_nodes == ["dr_synthesize"]
    assert pattern.node_map["dr_synthesize"].sub_nodes == []
    assert pattern.node_map["dr_synthesize"].is_end is True

    for node in pattern.nodes:
        assert node.plugins["loop"] == node.code  # 相位 code 即执行器 code
    # 工具授权面：toolset 级 + 检索节点静态列名
    assert pattern.allow_toolset == ["mcp-websearch", "mcp-zai"]
    assert pattern.node_map["dr_preplan"].use_tools == ["web_search_prime"]
    assert pattern.node_map["dr_search"].use_tools == ["web_search_prime"]
    assert not pattern.node_map["dr_plan"].use_tools
    assert not pattern.node_map["dr_synthesize"].use_tools

    from nexus.model.validation import validate_pattern
    validate_pattern(pattern)  # passes if no exception is raised


def test_phase_executor_plugins_registered():
    from nexus.registry.plugins import registry as plugins

    for code in ("dr_preplan", "dr_plan", "dr_search", "dr_synthesize"):
        assert plugins.has("executor", code), f"executor 插件 {code} 未注册"


# ============================================================================
# 2. Full research turn: four-node relay
# ============================================================================

def test_full_research_turn_relay(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(
        report="# 研究报告\n量子计算进展显著 [S1]。更多细节 [S2]。")
    session, reply = run_research(pattern, provider)

    assert "研究报告" in reply
    assert "[S1]" in reply

    # all four phases in place: preplan once (skipped) + plan once + search
    # (1 tool round + 1 convergence round) + synth once
    assert provider.preplan_calls == 1
    assert provider.plan_calls == 1
    assert provider.search_calls == 2
    assert provider.synth_calls == 1

    # phase request order = pipeline topology
    kinds = [kind for kind, _ in provider.requests]
    assert kinds == ["preplan", "plan", "search", "search", "synth"]

    # the fake MCP tool is genuinely dispatched
    assert fake_mcp_tools["n"] >= 1
    assert fake_mcp_tools["queries"]

    # the final-state trace keeps the legacy key shape
    trace = session.cxt.metadata["deep_research"]
    assert trace["question"] == "量子计算的最新进展是什么?"
    assert trace["sub_questions"] == ["子问题A", "子问题B"]
    assert trace["phases"] == ["plan", "search", "synthesize"]
    assert trace["tool_stats"].get("web_search_prime") >= 1
    assert trace["sources"][0]["tool"] == "web_search_prime"
    assert not trace["degraded"]

    # the graph terminated: the state board is cleared entirely
    assert session.cxt.graph_state == {}
    assert _STATE_KEY not in session.cxt.graph_state

    # the graph position mirrors the terminal node
    assert session.cxt.current_node_code == "dr_synthesize"

    # regression anchor: the research process stays out of conversation history
    roles = [m.role for m in session.cxt.history]
    assert "tool" not in roles


def test_streaming_relay_traces(pattern, fake_mcp_tools):
    """Streaming: node_start order is the pipeline topology; deltas come only from SYNTHESIZE."""
    from nexus.engine.chat import chat_turn_stream

    sessions = {}
    launch(pattern, sessions)
    provider = DeepResearchScriptedProvider(
        report="# 分段报告\n第一段。\n第二段。")

    async def _collect():
        return [e async for e in chat_turn_stream("量子计算进展", "s1", sessions)]

    with patch.object(executor_multi, "build_provider",
                      return_value=provider):
        events = arun(_collect())

    kinds = [e.kind for e in events]
    assert kinds[-1] == "done"

    # 同轮接力链：node_start 顺序 = preplan→plan→search→synthesize
    node_order = [e.trace.node_code for e in events
                  if e.kind == "trace" and e.trace.event == "node_start"]
    assert node_order == ["dr_preplan", "dr_plan", "dr_search",
                          "dr_synthesize"]

    # all deltas come from the report: plan/search intermediate text does not leak
    deltas = "".join(e.text for e in events if e.kind == "delta")
    assert "分段报告" in deltas
    assert "子问题A" not in deltas
    assert "测试查询" not in deltas
    assert "信息已足够" not in deltas

    # round events: all plan / search / synthesize phase boundaries present
    outcomes = [e.round_info["outcome"] for e in events if e.kind == "round"]
    assert "plan" in outcomes
    assert "search" in outcomes
    assert "synthesize" in outcomes
    assert outcomes[-1] == "final"

    assert events[-1].result.text == "# 分段报告\n第一段。\n第二段。"
    assert sessions["s1"].cxt.graph_state == {}


# ============================================================================
# 3. Pre-retrieval phase
# ============================================================================

def test_preplan_search_feeds_plan_and_search(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(preplan_rounds=1)
    session, reply = run_research(pattern, provider)

    assert provider.preplan_calls == 1
    assert fake_mcp_tools["n"] >= 2

    plan_requests = [m for kind, m in provider.requests if kind == "plan"]
    assert any(m.get("role") == "tool" for m in plan_requests[0])

    trace = session.cxt.metadata["deep_research"]
    assert trace["phases"] == ["preplan_search", "plan", "search",
                               "synthesize"]
    assert trace["sources"][0]["round"] == 0  # round=0 marks pre-retrieval
    assert not trace["degraded"]


# ============================================================================
# 4. PLAN degradation / self-correction
# ============================================================================

def test_plan_json_degraded(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(plan_json="这不是JSON")
    session, reply = run_research(pattern, provider)

    trace = session.cxt.metadata["deep_research"]
    assert trace["degraded"] is True
    assert trace["sub_questions"] == ["量子计算的最新进展是什么?"]
    assert reply


def test_plan_json_self_correct(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(plan_fail_first=True)
    session, reply = run_research(pattern, provider)

    assert provider.plan_calls == 2  # one failure + successful self-correction

    plan_requests = [m for kind, m in provider.requests if kind == "plan"]
    retry_msgs = plan_requests[1]
    assert any(m.get("role") == "assistant"
               and "先看子问题A" in str(m.get("content", ""))
               for m in retry_msgs)
    assert any(m.get("role") == "user"
               and "无法解析为研究计划" in str(m.get("content", ""))
               for m in retry_msgs)

    trace = session.cxt.metadata["deep_research"]
    assert not trace["degraded"]
    assert reply


# ============================================================================
# 5. SEARCH round capping
# ============================================================================

def test_search_rounds_capped(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(
        search_rounds=_MAX_SEARCH_ROUNDS + 5)  # never converges
    session, reply = run_research(pattern, provider)

    trace = session.cxt.metadata["deep_research"]
    assert trace["rounds"] == _MAX_SEARCH_ROUNDS
    assert provider.search_calls == _MAX_SEARCH_ROUNDS
    assert provider.synth_calls == 1  # synthesis still completes after the cap
    assert reply


# ============================================================================
# 6. Second turn reruns the whole graph (full-rerun semantics)
# ============================================================================

def test_second_turn_restarts_pipeline(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider()
    session, _ = run_research(pattern, provider)

    # second-turn follow-up: the whole graph reruns from entry
    provider2 = DeepResearchScriptedProvider()
    with patch.object(executor_multi, "build_provider",
                      return_value=provider2):
        chat({session.session_id: session}, session.session_id, "再深入讲讲")

    assert provider2.preplan_calls == 1
    assert provider2.synth_calls == 1
    assert session.cxt.graph_state == {}
    for m in session.cxt.history:
        assert m.role in ("user", "assistant"), f"意外的历史角色: {m.role}"


# ============================================================================
# 7. Orphan defense via a paused cursor landing on dr_plan without state
# ============================================================================

def test_orphan_paused_plan_bails_to_synthesize(pattern, fake_mcp_tools):
    """挂起游标落在 dr_plan 且无在途状态（异常遗留）→ 引擎 resume 该节点，
    相位孤儿防御跳过规划检索直奔综合；综合降级产出「证据不足」报告。"""
    sessions = {}
    session = launch(pattern, sessions)
    session.cxt.graph_state["__paused_node__"] = "dr_plan"
    session.cxt.graph_state["__step__"] = 1

    provider = DeepResearchScriptedProvider()
    with patch.object(executor_multi, "build_provider",
                      return_value=provider):
        reply = chat(sessions, "s1", "孤儿入口测试")

    assert provider.plan_calls == 0
    assert provider.search_calls == 0
    assert provider.synth_calls == 1
    assert reply

    trace = session.cxt.metadata["deep_research"]
    assert "orphan" in " ".join(trace["phases"])
    assert trace["degraded"] is True
    # 图终止后状态板清空（挂起游标不复存在）
    assert session.cxt.graph_state == {}
    assert session.cxt.current_node_code == "dr_synthesize"
