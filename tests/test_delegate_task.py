"""delegate_task（subagent tool）单测：子 ReAct 循环、工具池白名单、递归
防护（深度=1）、超时部分结果、轮次耗尽、结果截断、无 contextvar 回退，
以及 default_loop 执行器的 contextvar 注入契约（chat_turn 端到端）。
"""

import asyncio
import json

from async_utils import arun
from unittest.mock import patch

from atoms.tools import subagent_tool  # module import 即注册 delegate_task
from nexus.engine.chat import chat_turn
from nexus.engine.session import Session
from nexus.engine.tool_context import (
    current_tool_context,
    subagent_scope,
    tool_call_context,
)
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.tools import registry as tool_registry

import atoms.executors  # noqa: F401 -- default_loop 必须已注册


# ---------------------------------------------------------------------------
# 测试探针工具（toolset: test_delegate_pool）
# ---------------------------------------------------------------------------

def _dt_probe_handler(args, **kwargs):
    return json.dumps({"ok": True, "echo": args.get("q", "")},
                      ensure_ascii=False)


tool_registry.register(
    name="dt_probe",
    toolset="test_delegate_pool",
    schema={
        "name": "dt_probe",
        "description": "delegate_task 测试探针",
        "parameters": {"type": "object",
                       "properties": {"q": {"type": "string"}}},
    },
    handler=_dt_probe_handler,
)


def _dt_blob_handler(args, **kwargs):
    return json.dumps({"blob": "x" * 5000}, ensure_ascii=False)


tool_registry.register(
    name="dt_blob",
    toolset="test_delegate_pool",
    schema={
        "name": "dt_blob",
        "description": "返回超长结果，验证子循环内工具结果截断",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_dt_blob_handler,
)


# ---------------------------------------------------------------------------
# 脚本 provider 与运行辅助
# ---------------------------------------------------------------------------

_GUARD = {"timeout_seconds": 120, "max_rounds": 8, "max_result_chars": 8000}
_LLM = {"code": "x", "model": "m", "temperature": 0.7, "max_tokens": 512}


class SubScriptedProvider:
    """子代理侧脚本 provider：按序吐响应；脚本项含 "hang" 时挂起（超时用）。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        self.seen.append({
            "messages": [dict(m) for m in messages],
            "model": model,
            "temperature": temperature,
            "tools": tools,
        })
        item = self.script.pop(0) if self.script else {"content": "done"}
        if item.get("hang"):
            await asyncio.sleep(30)
        return item


def _tc(cid, name, args_dict):
    return {"id": cid, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args_dict, ensure_ascii=False)}}


def _run_delegate(args, provider, allow=("test_delegate_pool",),
                  in_subagent=False, no_ambient=False, llm_fallback=None):
    """经 tool_registry.dispatch 跑 delegate_task（覆盖 is_async 注册路径）。"""
    with patch.object(subagent_tool, "build_provider",
                      return_value=provider), \
         patch.object(subagent_tool, "get_subagent_tool_config",
                      return_value=dict(_GUARD)), \
         (patch.object(subagent_tool, "get_llm_config",
                       return_value=dict(llm_fallback))
          if llm_fallback is not None else _no_patch()):

        async def _call():
            if no_ambient:
                return await tool_registry.dispatch("delegate_task", args)
            with tool_call_context(_LLM, allow):
                if in_subagent:
                    with subagent_scope(current_tool_context()):
                        return await tool_registry.dispatch(
                            "delegate_task", args)
                return await tool_registry.dispatch("delegate_task", args)

        return json.loads(arun(_call()))


class _no_patch:
    """占位：让 with 表达式在无需 patch 时是透明 no-op。"""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# 正常委托流：工具调用 + 最终结论
# ---------------------------------------------------------------------------

def test_delegate_task_normal_flow():
    provider = SubScriptedProvider([
        {"content": "我先查一下",
         "tool_calls": [_tc("c1", "dt_probe", {"q": "hi"})],
         "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        {"content": "结论：查到了",
         "usage": {"prompt_tokens": 50, "completion_tokens": 30}},
    ])
    payload = _run_delegate({"task": "帮我查 X"}, provider)

    assert payload["status"] == "ok"
    assert payload["content"] == "结论：查到了"
    assert payload["rounds"] == 2
    assert payload["truncated"] is False
    assert payload["trace"] == [{"tool": "dt_probe", "ok": True}]
    assert payload["usage"] == {"prompt_tokens": 150, "completion_tokens": 50}
    assert payload["elapsed_seconds"] >= 0

    # 子代理 messages：内置默认 system prompt + 自包含 task；不注入父对话
    first = provider.seen[0]
    assert first["messages"][0]["role"] == "system"
    assert "子任务" in first["messages"][0]["content"]
    assert first["messages"][1] == {"role": "user", "content": "帮我查 X"}
    # 工具行按协议配对追加
    tool_rows = [m for m in provider.seen[1]["messages"]
                 if m.get("role") == "tool"]
    assert tool_rows[0]["tool_call_id"] == "c1"
    assert json.loads(tool_rows[0]["content"])["echo"] == "hi"


def test_delegate_task_pool_excludes_subagent_toolset():
    """pattern 授权含 subagent 工具集，子代理池也必须剔除 delegate_task。"""
    provider = SubScriptedProvider([{"content": "done"}])
    _run_delegate({"task": "t"}, provider,
                  allow=("test_delegate_pool", "subagent"))

    names = {t["function"]["name"] for t in provider.seen[0]["tools"]}
    assert names == {"dt_probe", "dt_blob"}


def test_delegate_task_inner_result_truncated():
    provider = SubScriptedProvider([
        {"content": None, "tool_calls": [_tc("c1", "dt_blob", {})]},
        {"content": "ok"},
    ])
    _run_delegate({"task": "t"}, provider)

    tool_row = next(m for m in provider.seen[1]["messages"]
                    if m.get("role") == "tool")
    assert len(tool_row["content"]) <= 4100  # 4000 上限 + 截断标注


def test_delegate_task_temperature_clamped():
    provider = SubScriptedProvider([{"content": "done"}])
    _run_delegate({"task": "t", "temperature": 5}, provider)
    assert provider.seen[0]["temperature"] == 2.0


# ---------------------------------------------------------------------------
# 授权拒绝与参数校验
# ---------------------------------------------------------------------------

def test_delegate_task_rejects_tools_outside_pool():
    provider = SubScriptedProvider([])
    payload = _run_delegate(
        {"task": "t", "tools": ["dt_probe", "nope", "delegate_task"]},
        provider)
    assert "不在子代理可用池中" in payload["error"]
    assert "nope" in payload["error"] and "delegate_task" in payload["error"]
    assert "dt_probe" in payload["error"]  # 错误回填可用列表供自纠
    assert provider.seen == []  # 未起子循环


def test_delegate_task_requires_task():
    provider = SubScriptedProvider([])
    payload = _run_delegate({"task": "   "}, provider)
    assert "task 必填" in payload["error"]


def test_delegate_task_rejects_bad_timeout():
    payload = _run_delegate({"task": "t", "timeout_seconds": 0},
                            SubScriptedProvider([]))
    assert "timeout_seconds" in payload["error"]


def test_delegate_task_rejects_bad_temperature():
    payload = _run_delegate({"task": "t", "temperature": "hot"},
                            SubScriptedProvider([]))
    assert "temperature" in payload["error"]


# ---------------------------------------------------------------------------
# 递归防护（深度=1）
# ---------------------------------------------------------------------------

def test_delegate_task_recursion_refused():
    provider = SubScriptedProvider([])
    payload = _run_delegate({"task": "t"}, provider, in_subagent=True)
    assert "嵌套" in payload["error"]
    assert provider.seen == []


# ---------------------------------------------------------------------------
# 超时部分结果 / 轮次耗尽 / 结论截断
# ---------------------------------------------------------------------------

def test_delegate_task_timeout_returns_partial():
    provider = SubScriptedProvider([
        {"content": "阶段结论",
         "tool_calls": [_tc("c1", "dt_probe", {"q": "x"})]},
        {"hang": True},
    ])
    payload = _run_delegate({"task": "t", "timeout_seconds": 1}, provider)

    assert payload["status"] == "timeout"
    assert payload["content"] == "阶段结论"  # 取消前最后一轮的结论
    assert payload["rounds"] == 1
    assert payload["trace"] == [{"tool": "dt_probe", "ok": True}]
    assert payload["elapsed_seconds"] < 10


def test_delegate_task_max_rounds_forced_close():
    rounds = [{"content": None,
               "tool_calls": [_tc(f"c{i}", "dt_probe", {})]}
              for i in range(12)]
    provider = SubScriptedProvider(rounds)
    payload = _run_delegate({"task": "t"}, provider)

    assert payload["status"] == "max_rounds"
    assert payload["rounds"] == _GUARD["max_rounds"]
    assert len(provider.seen) == _GUARD["max_rounds"]
    assert len(payload["trace"]) == _GUARD["max_rounds"]


def test_delegate_task_content_truncated():
    provider = SubScriptedProvider([{"content": "x" * 9000}])
    payload = _run_delegate({"task": "t"}, provider)

    assert payload["truncated"] is True
    assert len(payload["content"]) <= 8100  # 8000 上限 + 截断标注


# ---------------------------------------------------------------------------
# 无 ambient contextvar：回退全局 llm 配置、子代理无工具（纯推理）
# ---------------------------------------------------------------------------

def test_delegate_task_fallback_without_ambient():
    provider = SubScriptedProvider([{"content": "纯推理结论"}])
    payload = _run_delegate({"task": "t"}, provider, no_ambient=True,
                            llm_fallback=_LLM)

    assert payload["status"] == "ok"
    assert payload["content"] == "纯推理结论"
    assert provider.seen[0]["tools"] is None  # 无授权边界 → 无工具
    assert provider.seen[0]["model"] == "m"


# ---------------------------------------------------------------------------
# default_loop 注入契约（chat_turn 端到端）
# ---------------------------------------------------------------------------

class ParentScriptedProvider:
    """主循环侧脚本 provider（与 test_loop_tool_guards 同款鸭子类型）。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        self.seen.append({"messages": list(messages), "tools": tools,
                          "model": model})
        return self.script.pop(0)


def test_loop_executor_injects_context_e2e():
    """主 agent 经 default_loop 调 delegate_task：executor 注入的 llm_config
    与 pattern 授权边界一路传到子代理 provider。"""
    node = BaseNode(code="main", name="主节点", use_tools=["delegate_task"])
    p = Pattern(code="pg-dt", name="t", description="t",
                allow_toolset=["subagent", "test_delegate_pool"], nodes=[node])
    s = Session(session_id="s-dt-e2e", pattern_code="pg-dt")
    s.pattern = p
    s.cxt.node_map = p.node_map
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}

    parent = ParentScriptedProvider([
        {"content": None, "tool_calls": [
            _tc("c1", "delegate_task", {"task": "查 X 的资料"})]},
        {"content": "已完成汇总", "tool_calls": []},
    ])
    sub = SubScriptedProvider([
        {"content": "子代理结论",
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}}])

    with patch("atoms.executors.loop_executor.build_provider",
               return_value=parent), \
         patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}), \
         patch.object(subagent_tool, "build_provider", return_value=sub), \
         patch.object(subagent_tool, "get_subagent_tool_config",
                      return_value=dict(_GUARD)):
        result = arun(chat_turn("帮我查一下", s.session_id,
                                {s.session_id: s}))

    assert result.text == "已完成汇总"

    # 主 agent 的 tool 行 = delegate_task 的返回 payload
    tool_rows = [m for m in s.cxt.history if m.role == "tool"]
    payload = json.loads(tool_rows[0].content)
    assert payload["status"] == "ok"
    assert payload["content"] == "子代理结论"

    # 注入契约：子代理用父节点同款 model，工具池剔除 subagent 工具集
    assert sub.seen[0]["model"] == "m"
    assert {t["function"]["name"] for t in sub.seen[0]["tools"]} == {
        "dt_probe", "dt_blob"}
    assert sub.seen[0]["messages"][1] == {"role": "user",
                                          "content": "查 X 的资料"}

    # 主循环侧：节点只暴露 delegate_task
    assert {t["function"]["name"] for t in parent.seen[0]["tools"]} == {
        "delegate_task"}
