"""deep_research_agent offline tests — PLAN→SEARCH→SYNTHESIZE 结构化研究循环。

两层 mock(全程离线,不真连 MCP server):
- LLM:DeepResearchScriptedProvider 按相位特征脚本化(PLAN 请求含
  ``research_plan`` 锚文本 → 计划 JSON;SEARCH 轮带 tools → 先 tool_calls
  后收敛;SYNTHESIZE 请求含``撰写最终研究报告`` → 报告)。patch 目标是
  apps.deep_research_agent.executor.build_provider(FakeProvider 不能产
  tool_calls,参照 test_loop_tool_guards 的 ScriptedProvider 先例)。
- 工具:直接往 ToolRegistry 注册 toolset="mcp-fake" 的伪 MCP 工具
  (mcp- 前缀使重复注册豁免,测试重跑安全),不碰 McpManager 连接。

覆盖:
1. pattern 结构 + AST 自动发现 + executor 插件注册 + validate_pattern
2. 完整研究轮:报告含 [S1] 引用 / 工具被调 / metadata trace 完整 /
   cxt.history 无 tool 行(私有工作区决策的回归锚)
3. PLAN JSON 解析失败 → 降级单问题,整轮仍出报告
4. SEARCH 持续产 tool_calls → 恰在 _MAX_SEARCH_ROUNDS 截断
5. 流式:delta 仅来自 SYNTHESIZE 相位;round 事件含 plan/search/synthesize
6. mcp_servers 配置校验(_validate_mcp_servers 的 fail-fast 分支)
"""

import json
import logging

import pytest
from unittest.mock import patch

from async_utils import arun

logging.basicConfig(level=logging.WARNING)

from apps.deep_research_agent import executor as dr_executor
from apps.deep_research_agent.prompts import PLAN_ANCHOR


# ============================================================================
# Fixtures / helpers
# ============================================================================

@pytest.fixture(scope="module")
def pattern():
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.deep_research_agent.route" in imported, (
        f"deep_research_agent 未被自动发现，已发现: {imported}"
    )
    return registry.get("deep_research")


@pytest.fixture()
def fake_mcp_tools():
    """注册伪 MCP 工具(toolset=mcp-fake;yield 前注册,teardown 复位计数)。"""
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
        name="mcp_fake_search", toolset="mcp-fake",
        schema={"name": "mcp_fake_search", "description": "测试检索工具",
                "parameters": {"type": "object",
                               "properties": {"query": {"type": "string"}},
                               "required": ["query"]}},
        handler=_search_handler,
        allowed_patterns={"deep_research": True},
    )
    yield calls
    calls["n"], calls["queries"] = 0, []


class DeepResearchScriptedProvider:
    """按相位特征脚本化的 provider(记录调用与请求特征供断言)。

    请求识别(与 executor 的 prompt 布局一一对应):
    - PLAN:最后一条 user 消息含 PLAN_ANCHOR("research_plan")
    - SEARCH:tools 参数非空
    - SYNTHESIZE:system 或 user 含 ``撰写最终研究报告``
    """

    def __init__(self, plan_json=None, search_rounds=1, report="# 研究报告"):
        self.plan_json = plan_json or json.dumps(
            {"sub_questions": ["子问题A", "子问题B"],
             "notes": "测试计划"}, ensure_ascii=False)
        self.search_rounds = search_rounds  # 产 tool_calls 的轮数,之后收敛
        self.report = report
        self.call_count = 0
        self.search_calls = 0
        self.plan_calls = 0
        self.synth_calls = 0

    def _kind(self, messages, tools):
        last_user = next((m["content"] for m in reversed(messages)
                          if m.get("role") == "user"), "")
        if PLAN_ANCHOR in (last_user or ""):
            return "plan"
        if tools:
            return "search"
        if "撰写最终研究报告" in "".join(
                str(m.get("content", "")) for m in messages):
            return "synth"
        return "search"  # 兜底按 search 处理

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        self.call_count += 1
        kind = self._kind(messages, tools)
        if kind == "plan":
            self.plan_calls += 1
            return {"content": self.plan_json, "tool_calls": [],
                    "finish_reason": "stop"}
        if kind == "search":
            self.search_calls += 1
            if self.search_calls <= self.search_rounds:
                return {"content": None, "tool_calls": [{
                    "id": f"c{self.search_calls}", "type": "function",
                    "function": {"name": "mcp_fake_search",
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
        """流式形态:按相位把同一脚本切成 LLMChunk 序列(SYNTHESIZE 才有
        文本 delta;PLAN/SEARCH 中间相位零 delta——恰好回归 executor 的
        「中间轮不转发」决策)。"""
        from nexus.llm.types import LLMChunk

        self.call_count += 1
        kind = self._kind(messages, kwargs.get("tools"))
        if kind == "synth":
            self.synth_calls += 1
            text = self.report
            # 报告切成 3 段 delta
            cuts = max(1, len(text) // 3)
            pieces = [text[i:i + cuts] for i in range(0, len(text), cuts)]
            for p in pieces:
                yield LLMChunk(text=p)
            yield LLMChunk(finish_reason="stop")
            return
        if kind == "plan":
            self.plan_calls += 1
            yield LLMChunk(text=self.plan_json)
            yield LLMChunk(finish_reason="stop")
            return
        # search
        self.search_calls += 1
        if self.search_calls <= self.search_rounds:
            tc = {"index": 0, "id": f"c{self.search_calls}", "type": "function",
                  "function": {"name": "mcp_fake_search",
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
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions[session_id] = session
    return session


def chat(sessions, session_id, query):
    from async_utils import arun
    from nexus.engine.chat import chat as chat_fn

    return arun(chat_fn(query=query, session_id=session_id,
                        all_sessions=sessions))


def run_research(pattern, provider):
    """launch + 一轮完整研究(使用注入的 scripted provider)。

    返回 (session, reply_text);chat() 兼容入口直接返回 str。
    """
    sessions = {}
    launch(pattern, sessions)
    with patch.object(dr_executor, "build_provider",
                      return_value=provider):
        reply = chat(sessions, "s1", "量子计算的最新进展是什么?")
    return sessions["s1"], reply


# ============================================================================
# 1. pattern 结构与注册
# ============================================================================

def test_pattern_structure_and_executor_binding(pattern):
    assert pattern.code == "deep_research"
    assert pattern.entry_module_code == "deep_research"
    module = pattern.module_map["deep_research"]
    assert module.executor == "deep_research"
    assert not module.use_tools  # 空 = pattern ACL 决定工具面

    from nexus.model.validation import validate_pattern
    validate_pattern(pattern)  # 不抛即通过


def test_executor_plugin_registered():
    from nexus.registry.plugins import registry as plugins

    assert plugins.has("executor", "deep_research")


def test_ensure_mcp_ready_is_noop_without_servers():
    """时序闸回归锚:未配置 MCP server 时 ensure_mcp_ready 立即返回、
    绝不阻塞对话(启动竞态修复的守卫;真实竞态窗口的行为已在线下用
    真实 server 验证:闸门阻塞到工具注册完成,拦截不再发生)。"""
    from unittest.mock import patch as _patch

    from atoms.mcp.manager import McpManager
    from atoms.tools.mcp_tool import ensure_mcp_ready

    with _patch("atoms.mcp.manager.get_mcp_manager") as gm:
        gm.return_value.wait_ready.return_value = None
        ensure_mcp_ready(timeout=0.1)
        gm.assert_called_once()
        gm.return_value.wait_ready.assert_called_once_with(timeout=0.1)

    # 异常吞没契约:manager 抛错也绝不向对话层传播
    with _patch("atoms.mcp.manager.get_mcp_manager") as gm:
        gm.return_value.wait_ready.side_effect = RuntimeError("boom")
        ensure_mcp_ready()  # 不抛即通过


# ============================================================================
# 2. 完整研究轮
# ============================================================================

def test_full_research_turn(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(
        report="# 研究报告\n量子计算进展显著 [S1]。更多细节 [S2]。")
    session, reply = run_research(pattern, provider)

    # 报告成为本轮回复,引用标记来自 findings 编号
    assert "研究报告" in reply
    assert "[S1]" in reply

    # 相位都走到:plan 1 次 + search(1 轮工具 + 1 轮收敛)+ synth 1 次
    assert provider.plan_calls == 1
    assert provider.synth_calls == 1
    assert provider.search_calls == 2

    # 伪 MCP 工具被真实分派
    assert fake_mcp_tools["n"] >= 1
    assert fake_mcp_tools["queries"]

    # trace 完整落在 cxt.metadata
    trace = session.cxt.metadata["deep_research"]
    assert trace["question"] == "量子计算的最新进展是什么?"
    assert trace["sub_questions"] == ["子问题A", "子问题B"]
    assert trace["phases"] == ["plan", "search", "synthesize"]
    assert trace["tool_call_count"] >= 1
    assert trace["tool_stats"].get("mcp_fake_search") >= 1
    assert len(trace["sources"]) >= 1
    assert trace["sources"][0]["tool"] == "mcp_fake_search"
    assert not trace["degraded"]

    # 回归锚:研究过程不落对话历史(私有工作区)——无 tool 行、无中间
    # assistant 工具调用行
    roles = [m.role for m in session.cxt.history]
    assert "tool" not in roles


def test_history_only_qa_pairs(pattern, fake_mcp_tools):
    """多轮研究后历史只剩 Q/A 对(第二轮续问仍正常)。"""
    provider = DeepResearchScriptedProvider()
    session, _ = run_research(pattern, provider)
    with patch.object(dr_executor, "build_provider",
                      return_value=DeepResearchScriptedProvider()):
        chat({session.session_id: session}, session.session_id, "再深入讲讲")
    for m in session.cxt.history:
        assert m.role in ("user", "assistant"), f"意外的历史角色: {m.role}"


# ============================================================================
# 3. PLAN 降级
# ============================================================================

def test_plan_json_degraded(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(plan_json="这不是JSON")
    session, reply = run_research(pattern, provider)

    trace = session.cxt.metadata["deep_research"]
    assert trace["degraded"] is True
    # 降级为单问题(原问题)
    assert trace["sub_questions"] == ["量子计算的最新进展是什么?"]
    # 整轮仍产出报告
    assert reply


# ============================================================================
# 4. SEARCH 轮次截断
# ============================================================================

def test_search_rounds_capped(pattern, fake_mcp_tools):
    provider = DeepResearchScriptedProvider(
        search_rounds=dr_executor._MAX_SEARCH_ROUNDS + 5)  # 永不收敛
    session, reply = run_research(pattern, provider)

    trace = session.cxt.metadata["deep_research"]
    assert trace["rounds"] == dr_executor._MAX_SEARCH_ROUNDS
    assert provider.search_calls == dr_executor._MAX_SEARCH_ROUNDS
    # 截断后仍完成综合
    assert reply
    assert provider.synth_calls == 1


# ============================================================================
# 5. 流式:delta 仅来自 SYNTHESIZE
# ============================================================================

def test_streaming_deltas_only_synthesize(pattern, fake_mcp_tools):
    from nexus.engine.chat import chat_turn_stream

    sessions = {}
    launch(pattern, sessions)
    provider = DeepResearchScriptedProvider(
        report="# 分段报告\n第一段。\n第二段。")

    async def _collect():
        return [e async for e in chat_turn_stream("量子计算进展", "s1", sessions)]

    with patch.object(dr_executor, "build_provider",
                      return_value=provider):
        events = arun(_collect())

    kinds = [e.kind for e in events]
    assert "done" in kinds and kinds[-1] == "done"
    deltas = "".join(e.text for e in events if e.kind == "delta")
    # delta 全部来自报告:分段报告全文都在,计划/搜索中间文本不在
    assert "分段报告" in deltas
    assert "子问题A" not in deltas          # PLAN JSON 不外流
    assert "测试查询" not in deltas          # SEARCH 中间内容不外流
    assert "信息已足够" not in deltas        # 反思小结不外流
    assert sessions["s1"].cxt.metadata["deep_research"]["phases"] == [
        "plan", "search", "synthesize"]
    # done 权威回复 == 报告全文
    assert events[-1].result.text == "# 分段报告\n第一段。\n第二段。"


# ============================================================================
# 6. mcp_servers 配置校验
# ============================================================================

def test_validate_mcp_servers_branches():
    from nexus.settings import _validate_mcp_servers

    # 空 / 缺省 → 空 dict(全链路 no-op)
    assert _validate_mcp_servers(None) == {}
    assert _validate_mcp_servers({}) == {}

    # 合法 stdio 配置规范化(补 tool_name_prefix 默认)
    ok = _validate_mcp_servers({
        "web": {"transport": "stdio", "command": "npx",
                "args": ["-y", "@mcp/server"],
                "allowed_patterns": ["deep_research"]}})
    assert ok["web"]["tool_name_prefix"] == ""
    assert ok["web"]["allowed_patterns"] == ["deep_research"]

    # 非法 transport
    with pytest.raises(ValueError, match="transport"):
        _validate_mcp_servers({"w": {"transport": "grpc"}})
    # stdio 缺 command
    with pytest.raises(ValueError, match="command"):
        _validate_mcp_servers({"w": {"transport": "stdio"}})
    # http 类缺 url
    with pytest.raises(ValueError, match="url"):
        _validate_mcp_servers(
            {"w": {"transport": "streamable_http"}})
    # allowed_patterns 非法
    with pytest.raises(ValueError, match="allowed_patterns"):
        _validate_mcp_servers(
            {"w": {"transport": "stdio", "command": "x",
                   "allowed_patterns": "deep_research"}})
    # 条目非字典
    with pytest.raises(ValueError, match="应为字典"):
        _validate_mcp_servers({"w": "stdio"})


def test_load_config_carries_mcp_servers(tmp_path):
    from nexus.settings import load_config

    cfg_file = tmp_path / "local_config.yaml"
    cfg_file.write_text(
        "llm_default:\n  code: openai\n  model: m1\n"
        "mcp_servers:\n"
        "  web:\n"
        "    transport: stdio\n"
        "    command: npx\n"
        "    args: ['-y', '@mcp/server']\n"
        "    allowed_patterns: ['*']\n",
        encoding="utf-8")
    cfg = load_config(str(cfg_file))
    assert cfg["mcp_servers"]["web"]["transport"] == "stdio"
    assert cfg["mcp_servers"]["web"]["allowed_patterns"] == ["*"]

    # 未配置 → 空 dict
    cfg_file.write_text(
        "llm_default:\n  code: openai\n  model: m1\n", encoding="utf-8")
    assert load_config(str(cfg_file))["mcp_servers"] == {}
