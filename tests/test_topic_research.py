"""topic_research（六站 AGENT 图：preplan → plan/分主题 → search×N sends
扇出 → merge → report → polish）离线测试。

TopicScriptedProvider 与站点检测（请求特征锚点）自包含于本文件；机制覆盖：
1. 图结构 + AST 自动发现 + 六站点 executor 插件注册 + validate_pattern
2. 全流水线：PLAN 按主题 sends 扇出（search×N 并行 worker）→ merge 结构化
   合并（零 LLM 调用）→ report 草稿 → polish 流式交付 / 工具真实派发 /
   终态 trace / history 无 tool 行 / 图终止清空 graph_state
3. 主题规划自纠重试与降级（坏 JSON → 原问题单主题）
4. 单分支失败降级不阻塞 merge（failed 计数 + degraded 标记）
5. 流式：delta 只来自 POLISH；fanout_*/branch_* 事件 + join=tr_merge
"""

import json
import logging

import pytest
from unittest.mock import patch

from async_utils import arun

logging.basicConfig(level=logging.WARNING)

from apps.topic_research_agent import executor as tr_executor
from apps.topic_research_agent.prompts import (
    POLISH_ANCHOR,
    PREPLAN_ANCHOR,
    REPORT_ANCHOR,
    THEMES_ANCHOR,
)


# ============================================================================
# Fixtures / helpers
# ============================================================================

@pytest.fixture(scope="module")
def pattern():
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.topic_research_agent.route" in imported, (
        f"route 未被自动发现，已发现: {imported}"
    )
    return registry.get("topic_research")


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


class TopicScriptedProvider:
    """按站点特征脚本化的 provider(记录调用与请求特征供断言)。

    请求识别(与 executor 的 prompt 布局一一对应):
    - PREPLAN:最后一条 user 消息含 PREPLAN_ANCHOR（复用 deep_research 相位）
    - PLAN:最后一条 user 消息含 THEMES_ANCHOR
    - POLISH:最后一条 user 消息含 POLISH_ANCHOR
    - REPORT:最后一条 user 消息含 REPORT_ANCHOR
    - SEARCH:tools 参数非空（扇出 worker 的请求——分支以其工作区 user
      行(主题)标识，轮次按分支独立计数）
    """

    def __init__(self, themes_json=None, search_rounds=1,
                 draft="报告草稿正文。", polished="# 美化后研究报告",
                 themes_fail_first=False, themes_always_bad=False,
                 fail_sub_question=None):
        self.themes_json = themes_json or json.dumps(
            {"themes": ["主题A", "主题B"], "notes": "测试主题规划"},
            ensure_ascii=False)
        self.search_rounds = search_rounds
        self.draft = draft
        self.polished = polished
        self.themes_fail_first = themes_fail_first
        self.themes_always_bad = themes_always_bad
        self.fail_sub_question = fail_sub_question
        self.call_count = 0
        self.themes_calls = 0
        self.search_calls = 0
        self.search_branches = {}    # 主题 -> {"tool_rounds", "calls"}
        self.report_calls = 0
        self.polish_calls = 0
        self.preplan_calls = 0
        self.requests = []           # (kind, messages)

    def _kind(self, messages, tools):
        last_user = next((m["content"] for m in reversed(messages)
                          if m.get("role") == "user"), "")
        if PREPLAN_ANCHOR in (last_user or ""):
            return "preplan"
        if THEMES_ANCHOR in (last_user or ""):
            return "themes"
        if POLISH_ANCHOR in (last_user or ""):
            return "polish"
        if REPORT_ANCHOR in (last_user or ""):
            return "report"
        if tools:
            return "search"
        return "search"

    def _kind_of(self, messages, tools):
        kind = self._kind(messages, tools)
        self.requests.append((kind, [dict(m) for m in messages]))
        self.call_count += 1
        return kind

    def _branch_key(self, messages) -> str:
        return next((m["content"] for m in reversed(messages)
                     if m.get("role") == "user"), "")

    def _search_state(self, messages) -> dict:
        key = self._branch_key(messages)
        return self.search_branches.setdefault(
            key, {"tool_rounds": 0, "calls": 0})

    def _themes_reply(self):
        self.themes_calls += 1
        if self.themes_always_bad:
            return "我打算先研究一下再说。"
        if self.themes_fail_first and self.themes_calls == 1:
            return "好的,我的主题划分是:先看背景再看现状。"
        return self.themes_json

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        kind = self._kind_of(messages, tools)
        if kind == "preplan":
            self.preplan_calls += 1
            return {"content": "跳过预检索。", "tool_calls": [],
                    "finish_reason": "stop"}
        if kind == "themes":
            return {"content": self._themes_reply(), "tool_calls": [],
                    "finish_reason": "stop"}
        if kind == "report":
            self.report_calls += 1
            return {"content": self.draft, "tool_calls": [],
                    "finish_reason": "stop"}
        if kind == "polish":
            self.polish_calls += 1
            return {"content": self.polished, "tool_calls": [],
                    "finish_reason": "stop"}
        # search worker
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
                "function": {"name": "web_search_prime",
                             "arguments": json.dumps(
                                 {"query": f"{self._branch_key(messages)}资料检索"},
                                 ensure_ascii=False)},
            }], "finish_reason": "tool_calls"}
        return {"content": "信息已足够,开始合并。", "tool_calls": [],
                "finish_reason": "stop"}

    async def achat_completion_stream(self, messages, model, temperature=0.7,
                                      max_tokens=2048, **kwargs):
        from nexus.llm.types import LLMChunk

        kind = self._kind_of(messages, kwargs.get("tools"))
        if kind == "polish":
            self.polish_calls += 1
            yield LLMChunk(text=self.polished)
            yield LLMChunk(finish_reason="stop")
            return
        if kind == "report":
            self.report_calls += 1
            yield LLMChunk(text=self.draft)
            yield LLMChunk(finish_reason="stop")
            return
        if kind == "themes":
            yield LLMChunk(text=self._themes_reply())
            yield LLMChunk(finish_reason="stop")
            return
        if kind == "preplan":
            self.preplan_calls += 1
            yield LLMChunk(text="跳过预检索。", finish_reason="stop")
            return
        # search worker
        st = self._search_state(messages)
        self.search_calls += 1
        st["calls"] += 1
        if self.fail_sub_question and \
                self._branch_key(messages).startswith(self.fail_sub_question):
            raise RuntimeError(f"分支炸了: {self._branch_key(messages)}")
        if st["tool_rounds"] < self.search_rounds:
            st["tool_rounds"] += 1
            tc = {"index": 0, "id": f"c{self.search_calls}",
                  "type": "function",
                  "function": {"name": "web_search_prime",
                               "arguments": json.dumps(
                                   {"query": f"{self._branch_key(messages)}资料检索"},
                                   ensure_ascii=False)}}
            yield LLMChunk(tool_calls=[tc], finish_reason="tool_calls")
        else:
            yield LLMChunk(text="信息已足够,开始合并。",
                           finish_reason="stop")


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


def run_research(pattern, provider, session_id="s1", query="量子计算的最新进展是什么?"):
    sessions = {}
    launch(pattern, sessions, session_id=session_id)
    with patch.object(tr_executor, "build_provider", return_value=provider):
        reply = chat(sessions, session_id, query)
    return sessions[session_id], reply


# ============================================================================
# 1. Pattern structure and registration
# ============================================================================

def test_pattern_structure_and_executor_binding(pattern):
    assert pattern.code == "topic_research"
    assert pattern.pattern_type == "agent"
    assert pattern.entry_node_code == "tr_preplan"

    codes = [n.code for n in pattern.nodes]
    assert codes == ["tr_preplan", "tr_plan", "tr_search",
                     "tr_merge", "tr_report", "tr_polish"]
    # 静态邻接 = 流水线拓扑；美化站终态
    nm = pattern.node_map
    assert nm["tr_preplan"].sub_nodes == ["tr_plan"]
    assert nm["tr_plan"].sub_nodes == ["tr_search", "tr_merge"]  # 含孤儿逃生边
    assert nm["tr_search"].sub_nodes == ["tr_merge"]             # join 唯一后继
    assert nm["tr_merge"].sub_nodes == ["tr_report"]
    assert nm["tr_report"].sub_nodes == ["tr_polish"]
    assert nm["tr_polish"].sub_nodes == []
    assert nm["tr_polish"].is_end is True

    from nexus.model.validation import validate_pattern
    assert validate_pattern(pattern) is None

    from nexus.registry.plugins import registry as plugin_registry
    for code in codes:
        assert plugin_registry.resolve("executor", code) is not None


# ============================================================================
# 2. Full pipeline: 分主题 sends 扇出 → merge(零 LLM) → report → polish
# ============================================================================

def test_full_pipeline_fanout_merge_report_polish(pattern, fake_mcp_tools):
    provider = TopicScriptedProvider()
    session, reply = run_research(pattern, provider)

    # 最终回复 = 美化站产物（草稿不是回复）
    assert reply == provider.polished

    # LLM 调用清单：preplan(跳过) + themes + search×2分支×2轮 + report + polish
    # （merge 是纯结构合并，零 LLM 调用——不在清单中出现）
    kinds = [k for k, _ in provider.requests]
    assert kinds == ["preplan", "themes", "search", "search",
                     "search", "search", "report", "polish"]
    assert provider.report_calls == 1 and provider.polish_calls == 1
    assert fake_mcp_tools["n"] == 2  # 每分支一轮工具调用

    # 工具真实派发过（两分支的查询来自 worker 的 ReAct 决策）
    assert len(fake_mcp_tools["queries"]) == 2

    # report 请求吃到两分支的合并资料；polish 请求吃到草稿
    report_req = next(m for k, m in provider.requests if k == "report")[-1]
    polish_req = next(m for k, m in provider.requests if k == "polish")[-1]
    assert "主题A资料检索" in report_req["content"]
    assert "主题B资料检索" in report_req["content"]
    assert provider.draft in polish_req["content"]

    # 终态 trace
    trace = session.cxt.metadata["topic_research"]
    assert trace["phases"] == ["plan", "search", "merge", "report", "polish"]
    assert trace["themes"] == ["主题A", "主题B"]
    assert trace["branches"] == {"total": 2, "failed": 0}
    assert trace["degraded"] is False
    assert trace["tool_call_count"] == 2
    assert set(trace["per_theme"]) == {"主题A", "主题B"}
    assert len(trace["sources"]) == 2  # 每分支一条 finding

    # history 只有 user → 最终报告，无 tool 行；图终止清空状态板
    roles = [m.role for m in session.cxt.history]
    assert "tool" not in roles
    assert session.cxt.graph_state == {}


def test_fanout_actions_snapshot(pattern, fake_mcp_tools):
    from nexus.engine.chat import chat_turn

    provider = TopicScriptedProvider()
    sessions = {}
    launch(pattern, sessions)
    with patch.object(tr_executor, "build_provider", return_value=provider):
        result = arun(chat_turn("研究一下", "s1", sessions))
    starts = [a.get("fanout_start", {}) for a in result.actions
              if a.get("fanout_start")]
    assert starts and starts[0]["branches"] == 2
    assert starts[0]["join"] == "tr_merge"
    assert any(a.get("fanout_join", {}).get("total") == 2
               for a in result.actions)
    assert result.text == provider.polished


# ============================================================================
# 3. 主题规划：自纠重试 / 降级
# ============================================================================

def test_themes_self_correcting_retry(pattern, fake_mcp_tools):
    provider = TopicScriptedProvider(themes_fail_first=True)
    session, reply = run_research(pattern, provider)

    assert provider.themes_calls == 2  # 坏输出 → 带错误反馈重试 → 成功
    trace = session.cxt.metadata["topic_research"]
    assert trace["themes"] == ["主题A", "主题B"]
    assert trace["degraded"] is False
    assert reply == provider.polished


def test_themes_degrade_to_single_topic(pattern, fake_mcp_tools):
    provider = TopicScriptedProvider(themes_always_bad=True)
    session, reply = run_research(pattern, provider,
                                  query="固态电池的产业化进展?")

    assert provider.themes_calls == 2  # 初次 + 自纠重试均失败
    trace = session.cxt.metadata["topic_research"]
    assert trace["themes"] == ["固态电池的产业化进展?"]  # 降级为原问题单主题
    assert trace["degraded"] is True
    assert trace["branches"]["total"] == 1
    assert reply == provider.polished  # 降级不阻塞流水线


# ============================================================================
# 4. 分支失败：error 条目进板，merge 降级不阻塞
# ============================================================================

def test_branch_failure_degrades_but_completes(pattern, fake_mcp_tools):
    provider = TopicScriptedProvider(fail_sub_question="主题B")
    session, reply = run_research(pattern, provider)

    assert reply == provider.polished
    trace = session.cxt.metadata["topic_research"]
    assert trace["branches"] == {"total": 2, "failed": 1}
    assert trace["degraded"] is True
    assert set(trace["per_theme"]) == {"主题A"}  # 失败分支无资料进合并
    assert len(trace["sources"]) == 1
    # report 仍执行且只吃到存活分支的资料
    report_req = next(m for k, m in provider.requests if k == "report")[-1]
    assert "主题A资料检索" in report_req["content"]
    assert "主题B资料检索" not in report_req["content"]


# ============================================================================
# 5. 流式：delta 只来自 POLISH；fanout 事件词汇
# ============================================================================

def test_streaming_deltas_only_from_polish(pattern, fake_mcp_tools):
    from nexus.engine.chat import chat_turn_stream

    provider = TopicScriptedProvider()
    sessions = {}
    launch(pattern, sessions)

    async def _collect():
        events = []
        async for ev in chat_turn_stream("研究一下", "s1", sessions):
            events.append(ev)
        return events

    with patch.object(tr_executor, "build_provider", return_value=provider):
        events = arun(_collect())

    deltas = [ev for ev in events if ev.kind == "delta"]
    assert deltas and "".join(d.text for d in deltas) == provider.polished

    traces = [ev.trace.to_dict() for ev in events
              if ev.kind == "trace" and ev.trace is not None]
    names = [t["event"] for t in traces]
    assert names.count("fanout_start") == 1
    assert names.count("branch_start") == 2
    assert names.count("branch_end") == 2
    assert names.count("fanout_join") == 1
    starts = [t for t in traces if t["event"] == "fanout_start"]
    assert starts[0]["data"]["join_node"] == "tr_merge"
    assert {t["branch_id"] for t in traces
            if t["event"] == "branch_start"} == {"tr_search#1", "tr_search#2"}

    done = [ev for ev in events if ev.kind == "done"]
    assert done and done[-1].result.text == provider.polished
