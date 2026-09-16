"""Offline tests for deep_research (fan-out, four-node AGENT graph).

ScriptedProvider and phase detection (request-feature anchors) are
self-contained in this file; coverage of the graph-based mechanism:
1. Graph structure + AST auto-discovery + four-phase executor plugin
   registration + validate_pattern
2. Full research turn: PLAN fans out sends per sub-question (search x N
   parallel workers) -> join synthesis / real tool dispatch / final trace
   isomorphic with the legacy version (+ additive branches summary) /
   no tool rows in history / graph termination clears graph_state
3. Pre-retrieval phase: tool results feed the PLAN request; findings
   flow into join
4. PLAN degradation / self-correction retry (phase-level regression
   anchors); sub-question count beyond max_fanout is truncated
5. SEARCH: synthesis still completes after per-branch round capping;
   single-branch failure degrades without blocking join
6. Streaming: deltas come only from SYNTHESIZE; fanout_*/branch_* events
   + branch_id tagging
7. Second turn reruns from the entry node (full-graph rerun semantics)
8. Orphan entry defense: a paused cursor landing on dr_plan (no in-flight
   state) -> skips planning/pre-retrieval and goes straight to synthesis
   (compound regression of engine resume + phase orphan defense)
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

@pytest.fixture(autouse=True)
def _no_query_interval(monkeypatch):
    """Zero out the query rate limit: the sleep after each real query in
    _dispatch_research_round only applies in production; offline tests
    do not wait."""
    monkeypatch.setattr(executor_multi, "_QUERY_INTERVAL_SECONDS", 0.0)


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
    (web_search_prime, toolset mcp-websearch — the node's use_tools
    declared name)."""
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


def test_query_budget_and_rate_limit(monkeypatch):
    """Query budget and rate-limit contract: SEARCH rounds are capped at 5;
    from the 2nd real query onward within a round, sleep
    _QUERY_INTERVAL_SECONDS before executing (synthetic error backfill
    does not count as a query and does not sleep; no sleep after a single
    query — removes the dead 5s tail latency before the closing round)."""
    import asyncio as _asyncio

    import apps.deep_research_agent.executor_multi as em

    assert em._MAX_SEARCH_ROUNDS == 5

    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    async def _fake_execute_tool(name, args):
        return json.dumps({"results": [{"title": "r", "snippet": "s"}]},
                          ensure_ascii=False)

    monkeypatch.setattr(_asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(em, "_execute_tool", _fake_execute_tool)
    # What this test cares about is the production interval value — cancels out the autouse zeroing fixture
    monkeypatch.setattr(em, "_QUERY_INTERVAL_SECONDS", 5.0)

    class _Cxt:
        session_id = "s-rate-limit"

    class _Node:
        code = "dr_search"

    messages = []
    tool_calls = [
        {"id": "c1", "function": {"name": "web_search_prime",
                                  "arguments": '{"query": "第一条"}'}},
        {"id": "c2", "function": {"name": "ghost_tool",
                                          "arguments": "{}"}},  # intercepted and backfilled, no sleep
        {"id": "c3", "function": {"name": "web_search_prime",
                                  "arguments": '{"query": "第二条"}'}},
    ]
    findings = arun(em.DeepResearchExecutor()._dispatch_research_round(
        messages, tool_calls, hooks=None,
        allowed_names={"web_search_prime"}, round_idx=0,
        cxt=_Cxt(), node=_Node(), findings=[], tool_stats={}, stream=None))

    assert len(findings) == 2            # two real queries enter findings
    assert sleeps == [5.0]               # one rate-limit pause, before the second query only

    # single-query close-out: no tail-latency sleep
    sleeps.clear()
    arun(em.DeepResearchExecutor()._dispatch_research_round(
        [], [tool_calls[0]], hooks=None,
        allowed_names={"web_search_prime"}, round_idx=0,
        cxt=_Cxt(), node=_Node(), findings=[], tool_stats={}, stream=None))
    assert sleeps == []


class DeepResearchScriptedProvider:
    """Provider scripted by phase signature (records calls and request
    signatures for assertions).

    Request identification (one-to-one with the executor's prompt layout):
    - PREPLAN: the last user message contains PREPLAN_ANCHOR
    - PLAN: the last user message contains PLAN_ANCHOR
    - SEARCH: the tools argument is non-empty (fan-out worker requests —
      a branch is identified by its workspace user line (sub-question);
      rounds are counted per branch independently, concurrency does not
      interfere)
    - SYNTHESIZE: system or user contains ``撰写最终研究报告``
    """

    def __init__(self, plan_json=None, search_rounds=1, report="# 研究报告",
                 preplan_rounds=0, plan_fail_first=False,
                 fail_sub_question=None, tool_name="web_search_prime"):
        self.plan_json = plan_json or json.dumps(
            {"sub_questions": ["子问题A", "子问题B"],
             "notes": "测试计划"}, ensure_ascii=False)
        self.search_rounds = search_rounds
        self.report = report
        self.preplan_rounds = preplan_rounds
        self.plan_fail_first = plan_fail_first
        self.fail_sub_question = fail_sub_question
        # Tool name the model emits (may deliberately use the generic name
        # web_search to test pre-dispatch normalization)
        self.tool_name = tool_name
        self.call_count = 0
        self.search_calls = 0
        self.search_branches = {}   # sub-question -> {"tool_rounds", "calls"}
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
                "function": {"name": self.tool_name,
                             "arguments": json.dumps(
                                 {"query": f"预检索查询{self.preplan_calls}"},
                                 ensure_ascii=False)},
            }], "finish_reason": "tool_calls"}
        return {"content": "跳过预检索。", "tool_calls": [],
                "finish_reason": "stop"}

    def _branch_key(self, messages) -> str:
        return next((m["content"] for m in reversed(messages)
                     if m.get("role") == "user"), "")

    def _search_state(self, messages) -> dict:
        key = self._branch_key(messages)
        return self.search_branches.setdefault(
            key, {"tool_rounds": 0, "calls": 0})

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
            st = self._search_state(messages)
            self.search_calls += 1
            st["calls"] += 1
            if self.fail_sub_question and \
                    self._branch_key(messages).startswith(self.fail_sub_question):
                raise RuntimeError(f"分支炸了: {self._branch_key(messages)}")
            if st["tool_rounds"] < self.search_rounds:
                st["tool_rounds"] += 1
                return {"content": None, "tool_calls": [{
                    "id": f"c{self.search_calls}", "type": "function",
                    "function": {"name": self.tool_name,
                                 "arguments": json.dumps(
                                     {"query": f"测试查询{st['calls']}"},
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
        st = self._search_state(messages)
        self.search_calls += 1
        st["calls"] += 1
        if self.fail_sub_question and \
                self._branch_key(messages).startswith(self.fail_sub_question):
            raise RuntimeError(f"分支炸了: {self._branch_key(messages)}")
        if st["tool_rounds"] < self.search_rounds:
            st["tool_rounds"] += 1
            tc = {"index": 0, "id": f"c{self.search_calls}", "type": "function",
                  "function": {"name": self.tool_name,
                               "arguments": json.dumps(
                                   {"query": f"测试查询{st['calls']}"},
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
    # static adjacency = pipeline topology; the synthesis node is terminal
    assert pattern.node_map["dr_preplan"].sub_nodes == ["dr_plan"]
    assert pattern.node_map["dr_plan"].sub_nodes == ["dr_search",
                                                    "dr_synthesize"]  # includes the orphan escape edge
    assert pattern.node_map["dr_search"].sub_nodes == ["dr_synthesize"]
    assert pattern.node_map["dr_synthesize"].sub_nodes == []
    assert pattern.node_map["dr_synthesize"].is_end is True

    for node in pattern.nodes:
        assert node.plugins["loop"] == node.code  # phase code doubles as the executor code
    # tool authorization surface: toolset level + retrieval nodes' static name lists
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
# 1b. Tool-name alias normalization (normalized ahead of the generic-name
# guardrail for flash-tier models)
# ============================================================================

def test_normalize_tool_name_unit():
    from apps.deep_research_agent.executor_multi import _normalize_tool_name

    allowed = {"web_search_prime"}
    # canonical name allowed / alias hit
    assert _normalize_tool_name("web_search_prime", allowed) == \
        ("web_search_prime", False)
    assert _normalize_tool_name("web_search", allowed) == \
        ("web_search_prime", True)
    # no alias hit, or the alias target is not in this round's set -> return
    # as-is (still goes through the interception guardrail)
    assert _normalize_tool_name("no_such_tool", allowed) == ("no_such_tool", False)
    assert _normalize_tool_name("web_search", {"other"}) == ("web_search", False)


def test_tool_name_alias_dispatches_canonical_tool(pattern, fake_mcp_tools):
    """The model writes web_search_prime as the generic name web_search
    throughout: it is normalized in place before dispatch, the tool really
    executes, and findings/stats land under the canonical name — no longer
    wasting an "intercept -> feed-back -> self-correct" round (if
    normalization broke, this would show up as zero calls to the fake tool
    and degraded research)."""
    provider = DeepResearchScriptedProvider(
        report="# 研究报告\n归一检索生效 [S1]。",
        tool_name="web_search")            # the model gets the name wrong every round
    session, reply = run_research(pattern, provider)

    assert "研究报告" in reply
    assert fake_mcp_tools["n"] >= 2         # fake tool really dispatched (>=1 per branch)
    trace = session.cxt.metadata["deep_research"]
    assert trace["tool_stats"].get("web_search_prime") == 2   # stats under the canonical name
    assert trace["sources"][0]["tool"] == "web_search_prime"
    assert not trace["degraded"]


# ============================================================================
# 2. Full research turn: four-node relay
# ============================================================================

def test_full_research_turn_relay(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(
        report="# 研究报告\n量子计算进展显著 [S1]。更多细节 [S2]。")
    session, reply = run_research(pattern, provider)

    assert "研究报告" in reply
    assert "[S1]" in reply

    # all four phases in place: preplan once (skipped) + plan once + two
    # concurrent search branches (1 tool round + 1 convergence round each)
    # + synth once
    assert provider.preplan_calls == 1
    assert provider.plan_calls == 1
    assert provider.search_calls == 4
    assert provider.synth_calls == 1
    assert len(provider.search_branches) == 2   # one workspace per sub-question

    # phase request order: pipeline topology (the 4 search calls of the two
    # concurrent branches may interleave in any order between plan and synth)
    kinds = [kind for kind, _ in provider.requests]
    assert kinds[:2] == ["preplan", "plan"]
    assert kinds[-1] == "synth"
    assert kinds.count("search") == 4

    # the fake MCP tool is genuinely dispatched (once per branch round)
    assert fake_mcp_tools["n"] >= 2
    assert fake_mcp_tools["queries"]

    # the final-state trace keeps the legacy key shape (+ branches summary)
    trace = session.cxt.metadata["deep_research"]
    assert trace["question"] == "量子计算的最新进展是什么?"
    assert trace["sub_questions"] == ["子问题A", "子问题B"]
    assert trace["phases"] == ["plan", "search", "synthesize"]
    assert trace["tool_stats"].get("web_search_prime") == 2  # merged, 1/branch
    assert trace["sources"][0]["tool"] == "web_search_prime"
    assert trace["rounds"] == 4   # 2 rounds/branch (tool + convergence)
    assert trace["branches"] == {"total": 2, "failed": 0}
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
    """Streaming: fan-out vocabulary + branch tagging; worker deltas never
    leak (only SYNTHESIZE streams)."""
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

    # main-path nodes only (workers surface as branch_*, not node_start):
    # preplan → plan → (fanout: 2 branches) → synthesize
    node_order = [e.trace.node_code for e in events
                  if e.kind == "trace" and e.trace.event == "node_start"]
    assert node_order == ["dr_preplan", "dr_plan", "dr_synthesize"]

    traces = [e.trace for e in events if e.kind == "trace"]
    fanout = [t for t in traces if t.event == "fanout_start"]
    assert len(fanout) == 1
    assert fanout[0].data["branch_ids"] == ["dr_search#1", "dr_search#2"]
    assert fanout[0].data["join_node"] == "dr_synthesize"
    assert sum(1 for t in traces if t.event == "branch_start") == 2
    ends = [t for t in traces if t.event == "branch_end"]
    assert len(ends) == 2 and all(t.data.get("ok") for t in ends)
    joins = [t for t in traces if t.event == "fanout_join"]
    assert len(joins) == 1
    assert joins[0].data == {"total": 2, "failed": 0}

    # tool_call / tool_result stream per dispatch (kernel vocabulary, same
    # data keys): 1 tool round per branch → 2+2, branch-tagged, canonical
    # tool name, real (non-synthetic) results carrying the fake MCP content
    tc_traces = [t for t in traces if t.event == "tool_call"]
    tr_traces = [t for t in traces if t.event == "tool_result"]
    assert len(tc_traces) == 2 and len(tr_traces) == 2
    assert all(t.data["tool_name"] == "web_search_prime" for t in tc_traces)
    assert all(t.data["args"].get("query") for t in tc_traces)
    assert all(t.branch_id.startswith("dr_search#") for t in tc_traces)
    assert all(t.data["tool_name"] == "web_search_prime"
               and not t.data["synthetic"] for t in tr_traces)
    assert all("测试检索内容" in t.data["result"] for t in tr_traces)

    # all deltas come from the report: plan/search intermediate text does not leak
    deltas = "".join(e.text for e in events if e.kind == "delta")
    assert "分段报告" in deltas
    assert "子问题A" not in deltas
    assert "测试查询" not in deltas
    assert "信息已足够" not in deltas

    # round events: all plan / search / synthesize phase boundaries present
    # (search rounds are branch-tagged)
    rounds = [e for e in events if e.kind == "round"]
    outcomes = [e.round_info["outcome"] for e in rounds]
    assert "plan" in outcomes
    assert "search" in outcomes
    assert "synthesize" in outcomes
    assert outcomes[-1] == "final"
    search_rounds = [e for e in rounds
                     if e.round_info["outcome"] == "search"]
    assert all(e.branch_id for e in search_rounds)

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
# 5. SEARCH per-branch round capping / branch failure tolerance
# ============================================================================

def test_search_rounds_capped_per_branch(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(
        search_rounds=_MAX_SEARCH_ROUNDS + 5)  # never converges
    session, reply = run_research(pattern, provider)

    trace = session.cxt.metadata["deep_research"]
    # two branches, each capped at the per-branch ceiling
    assert trace["rounds"] == _MAX_SEARCH_ROUNDS * 2
    assert provider.search_calls == _MAX_SEARCH_ROUNDS * 2
    assert provider.synth_calls == 1  # synthesis still completes after the cap
    assert reply


def test_branch_failure_degrades_but_completes(pattern, fake_mcp_tools):
    """A branch whose LLM calls blow up settles as an error entry — the join
    still fires, the report is produced from the surviving branch's
    material, and the trace marks degraded."""
    provider = DeepResearchScriptedProvider(fail_sub_question="子问题B")
    session, reply = run_research(pattern, provider)

    assert provider.synth_calls == 1
    assert reply

    trace = session.cxt.metadata["deep_research"]
    assert trace["branches"] == {"total": 2, "failed": 1}
    assert trace["degraded"] is True
    # findings from the surviving branch still made it into the report
    assert trace["sources"]
    assert trace["tool_stats"].get("web_search_prime") == 1


# ============================================================================
# 5b. PLAN dispatch width capping
# ============================================================================

def test_plan_overflow_caps_fanout_width(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(plan_json=json.dumps(
        {"sub_questions": [f"子问题{i}" for i in range(1, 11)],
         "notes": "超宽计划"}, ensure_ascii=False))
    session, reply = run_research(pattern, provider)

    trace = session.cxt.metadata["deep_research"]
    # dispatch capped at the pattern's max_fanout (default 8)
    assert trace["branches"]["total"] == 8
    assert len(provider.search_branches) == 8
    # the trace keeps the FULL plan (the report explains the study scope;
    # the cap note lives in the plan's notes field)
    assert len(trace["sub_questions"]) == 10
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
    """A paused cursor lands on dr_plan with no in-flight state (left over
    from an exception) -> the engine resumes that node, and the phase
    orphan defense skips planning/pre-retrieval and goes straight to
    synthesis; synthesis degrades into an "insufficient evidence" report."""
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
    # state board cleared after graph termination (the paused cursor no longer exists)
    assert session.cxt.graph_state == {}
    assert session.cxt.current_node_code == "dr_synthesize"


def test_tool_call_trace_carries_rewrite_audit_keys():
    """Branch trace aligns with the kernel-path contract: for calls
    rewritten by P4, the tool_call event carries rewritten=True and
    original_call (previously rewrite_audits were collected and then
    dropped, leaving the branch-side rewrite audit with no data)."""
    import apps.deep_research_agent.executor_multi as em

    emitted = []

    class _Stream:
        def emit_trace(self, event, **data):
            emitted.append((event, data))

    def _rewrite(event):                       # P4 rewrite: name + args
        return {"name": "web_search_prime", "args": {"query": "改写后"}}

    class _Cxt:
        session_id = "s-audit"

    class _Node:
        code = "dr_search"

    async def _fake_execute_tool(name, args):
        return json.dumps({"results": []}, ensure_ascii=False)

    with patch.object(em, "_execute_tool", _fake_execute_tool):
        arun(em.DeepResearchExecutor()._dispatch_research_round(
            [], [{"id": "c1", "function": {
                "name": "web_search", "arguments": '{"query": "原始"}'}}],
            hooks={"on_tool_call": [_rewrite]},
            allowed_names={"web_search_prime"}, round_idx=0,
            cxt=_Cxt(), node=_Node(), findings=[], tool_stats={},
            stream=_Stream()))

    calls = [d for ev, d in emitted if ev == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["rewritten"] is True
    # original_call = the call as it was before the hook rewrote it (alias
    # normalization already happened before this point)
    assert calls[0]["original_call"]["name"] == "web_search_prime"
    assert calls[0]["original_call"]["args"] == {"query": "原始"}
    assert calls[0]["args"] == {"query": "改写后"}
