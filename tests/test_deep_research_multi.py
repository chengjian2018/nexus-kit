"""deep_research_multi offline tests —— 相位即模块的多模块研究流水线。

与 test_deep_research_agent.py(单模块版)同一套 ScriptedProvider 设施
(相位识别基于请求特征,与模块接线无关),覆盖多模块版特有的机制:

1. pattern 结构 + AST 自动发现 + 四个相位 executor 插件注册 +
   validate_pattern + max_hops=4
2. 完整研究轮:四模块同轮接力(module_jump trace 顺序即流水线拓扑)/
   工具被调 / trace 同键同构 / 历史无 tool 行 / 瞬态 state 收尾弹出 /
   底座复位到流水线首站
3. 预检索:工具结果进 PLAN 请求、findings 并入 SEARCH
4. PLAN 降级 / 自纠重试(与单模块版同相位方法,回归锚)
5. SEARCH 轮次截断后仍完成综合
6. 流式:delta 仅来自 SYNTHESIZE;round 事件含 plan/search/synthesize
7. 第二轮从首站重新进入(底座复位的回归锚)
8. 异常入口防御:直接落 dr_plan(无在途状态)→ 跳过规划检索直送综合
9. begin_turn 出清 deep_research_state(中途异常不留陈旧状态)
"""

import logging

import pytest
from unittest.mock import patch

from async_utils import arun

logging.basicConfig(level=logging.WARNING)

from apps.deep_research_agent import executor_multi
from apps.deep_research_agent.executor import _MAX_SEARCH_ROUNDS
from apps.deep_research_agent.executor_multi import _STATE_KEY
from test_deep_research_agent import (
    DeepResearchScriptedProvider,
    chat,
    launch,
)


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
    return registry.get("deep_research_multi")


@pytest.fixture()
def fake_mcp_tools():
    """注册伪 MCP 工具(授权给多模块版 pattern;与单模块版 fixture 同构)。"""
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
        import json
        return json.dumps({"results": results}, ensure_ascii=False)

    tool_registry.register(
        name="mcp_fake_search", toolset="mcp-fake",
        schema={"name": "mcp_fake_search", "description": "测试检索工具",
                "parameters": {"type": "object",
                               "properties": {"query": {"type": "string"}},
                               "required": ["query"]}},
        handler=_search_handler,
        allowed_patterns={"deep_research_multi": True},
    )
    yield calls
    calls["n"], calls["queries"] = 0, []


def run_research(pattern, provider, session_id="s1"):
    """launch + 一轮完整研究(注入 scripted provider;返回 (session, reply))。"""
    sessions = {}
    launch(pattern, sessions, session_id=session_id)
    with patch.object(executor_multi, "build_provider",
                      return_value=provider):
        reply = chat(sessions, session_id, "量子计算的最新进展是什么?")
    return sessions[session_id], reply


# ============================================================================
# 1. pattern 结构与注册
# ============================================================================

def test_pattern_structure_and_executor_binding(pattern):
    assert pattern.code == "deep_research_multi"
    assert pattern.entry_module_code == "dr_preplan"
    assert pattern.max_hops == 4  # 3 跳接力 + 最终模块

    codes = [m.module_code for m in pattern.modules]
    assert codes == ["dr_preplan", "dr_plan", "dr_search", "dr_synthesize"]
    for module in pattern.modules:
        assert module.executor == module.module_code  # 相位码即插件码
        assert not module.use_tools  # 空 = pattern ACL 决定工具面
        assert module.enable_project is False  # 跳转目标,不参与投影/defer

    from nexus.model.validation import validate_pattern
    validate_pattern(pattern)  # 不抛即通过


def test_phase_executor_plugins_registered():
    from nexus.registry.plugins import registry as plugins

    for code in ("dr_preplan", "dr_plan", "dr_search", "dr_synthesize"):
        assert plugins.has("executor", code), f"executor 插件 {code} 未注册"


# ============================================================================
# 2. 完整研究轮:四模块接力
# ============================================================================

def test_full_research_turn_relay(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(
        report="# 研究报告\n量子计算进展显著 [S1]。更多细节 [S2]。")
    session, reply = run_research(pattern, provider)

    # 报告成为本轮回复,引用标记来自 findings 编号
    assert "研究报告" in reply
    assert "[S1]" in reply

    # 四个相位各就各位:preplan 1 次(跳过)+ plan 1 次 + search(1 轮工具
    # + 1 轮收敛)+ synth 1 次
    assert provider.preplan_calls == 1
    assert provider.plan_calls == 1
    assert provider.search_calls == 2
    assert provider.synth_calls == 1

    # 相位请求顺序 = 流水线拓扑(preplan → plan → search → synth)
    kinds = [kind for kind, _ in provider.requests]
    assert kinds == ["preplan", "plan", "search", "search", "synth"]

    # 伪 MCP 工具被真实分派
    assert fake_mcp_tools["n"] >= 1
    assert fake_mcp_tools["queries"]

    # 终态 trace 与单模块版同键同构
    trace = session.cxt.metadata["deep_research"]
    assert trace["question"] == "量子计算的最新进展是什么?"
    assert trace["sub_questions"] == ["子问题A", "子问题B"]
    assert trace["phases"] == ["plan", "search", "synthesize"]
    assert trace["tool_stats"].get("mcp_fake_search") >= 1
    assert trace["sources"][0]["tool"] == "mcp_fake_search"
    assert not trace["degraded"]

    # 轮内瞬态工作区已弹出(不随会话留存)
    assert _STATE_KEY not in session.cxt.metadata

    # 底座复位:下一轮从流水线首站重新进入
    assert session.cxt.current_module_code == "dr_preplan"

    # 回归锚:研究过程不落对话历史(私有工作区)——无 tool 行
    roles = [m.role for m in session.cxt.history]
    assert "tool" not in roles


def test_streaming_relay_traces(pattern, fake_mcp_tools):
    """流式:module_jump trace 顺序即接力拓扑;delta 仅来自 SYNTHESIZE。"""
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

    # 同轮接力链:preplan→plan→search→synthesize(hop 循环发 module_jump)
    jumps = [e.trace.data.get("to_module")
             for e in events
             if e.kind == "trace" and e.trace.event == "module_jump"]
    assert jumps == ["dr_plan", "dr_search", "dr_synthesize"]

    # delta 全部来自报告:计划/搜索中间文本不外流
    deltas = "".join(e.text for e in events if e.kind == "delta")
    assert "分段报告" in deltas
    assert "子问题A" not in deltas
    assert "测试查询" not in deltas
    assert "信息已足够" not in deltas

    # round 事件:plan / search / synthesize 相位边界齐(final 来自综合)
    outcomes = [e.round_info["outcome"] for e in events if e.kind == "round"]
    assert "plan" in outcomes
    assert "search" in outcomes
    assert "synthesize" in outcomes
    assert outcomes[-1] == "final"

    # done 权威回复 == 报告全文;瞬态 state 已弹出
    assert events[-1].result.text == "# 分段报告\n第一段。\n第二段。"
    assert _STATE_KEY not in sessions["s1"].cxt.metadata


# ============================================================================
# 3. 预检索相位
# ============================================================================

def test_preplan_search_feeds_plan_and_search(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(preplan_rounds=1)
    session, reply = run_research(pattern, provider)

    assert provider.preplan_calls == 1
    assert fake_mcp_tools["n"] >= 2

    # 预检索的 tool 结果行进了 PLAN 请求
    plan_requests = [m for kind, m in provider.requests if kind == "plan"]
    assert any(m.get("role") == "tool" for m in plan_requests[0])

    trace = session.cxt.metadata["deep_research"]
    assert trace["phases"] == ["preplan_search", "plan", "search",
                               "synthesize"]
    assert trace["sources"][0]["round"] == 0  # round=0 标记预检索
    assert not trace["degraded"]


# ============================================================================
# 4. PLAN 降级 / 自纠
# ============================================================================

def test_plan_json_degraded(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(plan_json="这不是JSON")
    session, reply = run_research(pattern, provider)

    trace = session.cxt.metadata["deep_research"]
    assert trace["degraded"] is True
    assert trace["sub_questions"] == ["量子计算的最新进展是什么?"]
    assert reply  # 整轮仍产出报告


def test_plan_json_self_correct(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(plan_fail_first=True)
    session, reply = run_research(pattern, provider)

    assert provider.plan_calls == 2  # 失败一次 + 自纠成功

    # 重试请求包含:坏输出回填(assistant)+ 自纠指令(user)
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
# 5. SEARCH 轮次截断
# ============================================================================

def test_search_rounds_capped(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(
        search_rounds=_MAX_SEARCH_ROUNDS + 5)  # 永不收敛
    session, reply = run_research(pattern, provider)

    trace = session.cxt.metadata["deep_research"]
    assert trace["rounds"] == _MAX_SEARCH_ROUNDS
    assert provider.search_calls == _MAX_SEARCH_ROUNDS
    assert provider.synth_calls == 1  # 截断后仍完成综合
    assert reply


# ============================================================================
# 6. 第二轮从首站重新进入(底座复位)
# ============================================================================

def test_second_turn_restarts_pipeline(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider()
    session, _ = run_research(pattern, provider)
    assert session.cxt.current_module_code == "dr_preplan"

    # 第二轮续问:整个流水线重跑(preplan 再次被调),历史只剩 Q/A 对
    provider2 = DeepResearchScriptedProvider()
    with patch.object(executor_multi, "build_provider",
                      return_value=provider2):
        chat({session.session_id: session}, session.session_id, "再深入讲讲")

    assert provider2.preplan_calls == 1
    assert provider2.synth_calls == 1
    assert _STATE_KEY not in session.cxt.metadata
    for m in session.cxt.history:
        assert m.role in ("user", "assistant"), f"意外的历史角色: {m.role}"


# ============================================================================
# 7. 异常入口防御:无在途状态直送综合
# ============================================================================

def test_orphan_entry_bails_to_synthesize(pattern, fake_mcp_tools):
    """直接落 dr_plan(无 deep_research_state)→ 跳过规划检索,综合兜底
    出「证据不足」报告,流水线不卡死。"""
    sessions = {}
    session = launch(pattern, sessions)
    session.cxt.current_module_code = "dr_plan"  # 模拟异常底座残留

    provider = DeepResearchScriptedProvider()
    with patch.object(executor_multi, "build_provider",
                      return_value=provider):
        reply = chat(sessions, "s1", "孤儿入口测试")

    # plan/search 相位被跳过,只有综合一次调用
    assert provider.plan_calls == 0
    assert provider.search_calls == 0
    assert provider.synth_calls == 1
    assert reply

    trace = session.cxt.metadata["deep_research"]
    assert trace["phases"] == ["orphan_plan", "synthesize"]
    assert trace["degraded"] is True
    # 收尾后底座已复位
    assert session.cxt.current_module_code == "dr_preplan"


def test_begin_turn_clears_stale_state():
    """中途异常(相位抛错被对话层兜住)留下的 deep_research_state 在下一轮
    begin_turn 出清——陈旧工作区绝不泄进新一轮。"""
    from nexus.engine.context_lifecycle import TurnLifecycle
    from nexus.context import DialogueContext

    cxt = DialogueContext(session_id="s1", user_query="q")
    cxt.metadata[_STATE_KEY] = {"question": "陈旧状态", "findings": ["x"]}
    TurnLifecycle().begin_turn(cxt, "新问题")
    assert _STATE_KEY not in cxt.metadata
