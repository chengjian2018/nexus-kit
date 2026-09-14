"""run_workflow（workflow tool）单测：六种拓扑、裁判容错、护栏（宽度/
嵌套/超时/settings 封顶）、无 ambient 回退，以及 default_loop 注入契约
（chat_turn 端到端）。叶子引擎（_subagent_core）的细粒度行为由
test_delegate_task.py 覆盖，这里只测拓扑编排层。
"""

import asyncio
import json

from async_utils import arun
from unittest.mock import patch

from atoms.tools import subagent_tool, workflow_tool  # import 即注册
from nexus.engine.chat import chat_turn
from nexus.engine.session import Session
from nexus.engine.tool_context import (
    current_tool_context,
    tool_call_context,
    workflow_scope,
)
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.tools import registry as tool_registry

import atoms.executors  # noqa: F401 -- default_loop 必须已注册


# ---------------------------------------------------------------------------
# 测试探针工具（toolset: test_wf_pool）
# ---------------------------------------------------------------------------

def _wf_probe_handler(args, **kwargs):
    return json.dumps({"ok": True, "echo": args.get("q", "")},
                      ensure_ascii=False)


tool_registry.register(
    name="wf_probe",
    toolset="test_wf_pool",
    schema={
        "name": "wf_probe",
        "description": "run_workflow 测试探针",
        "parameters": {"type": "object",
                       "properties": {"q": {"type": "string"}}},
    },
    handler=_wf_probe_handler,
)


# ---------------------------------------------------------------------------
# 脚本 provider 与运行辅助
# ---------------------------------------------------------------------------

_GUARD = {"timeout_seconds": 300, "max_rounds": 8, "max_width": 8,
          "max_cycles": 3, "max_iterations": 5, "max_result_chars": 8000,
          "trace_cap": 32}
_LLM = {"code": "x", "model": "m", "temperature": 0.7, "max_tokens": 512}


class FuncProvider:
    """fn(snapshot) -> 响应 dict（可含 "hang"）；按调用次序记录快照。

    叶子调用（授权池非空时）tools 非 None；裁判/汇总调用 tools 为 None——
    测试按 system prompt 内容进一步区分裁判类型。
    """

    def __init__(self, fn):
        self.fn = fn
        self.seen = []

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        snap = {"messages": [dict(m) for m in messages], "model": model,
                "temperature": temperature, "tools": tools}
        self.seen.append(snap)
        resp = self.fn(snap)
        if resp.get("hang"):
            await asyncio.sleep(30)
        return resp


def _sys(snap):
    return snap["messages"][0]["content"]


def _user(snap):
    return next(m["content"] for m in reversed(snap["messages"])
                if m.get("role") == "user")


def _leaf_calls(provider):
    return [s for s in provider.seen if s["tools"] is not None]


def _judge_calls(provider):
    return [s for s in provider.seen if s["tools"] is None]


class _no_patch:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _run_workflow(args, provider, allow=("test_wf_pool",),
                  in_workflow=False, no_ambient=False, llm_fallback=None,
                  guard=None):
    """经 tool_registry.dispatch 跑 run_workflow（覆盖 is_async 注册路径）。"""
    with patch.object(workflow_tool, "build_provider",
                      return_value=provider), \
         patch.object(workflow_tool, "get_workflow_tool_config",
                      return_value=dict(guard or _GUARD)), \
         (patch.object(workflow_tool, "get_llm_config",
                       return_value=dict(llm_fallback))
          if llm_fallback is not None else _no_patch()):

        async def _call():
            if no_ambient:
                return await tool_registry.dispatch("run_workflow", args)
            with tool_call_context(_LLM, allow):
                if in_workflow:
                    with workflow_scope(current_tool_context()):
                        return await tool_registry.dispatch("run_workflow",
                                                            args)
                return await tool_registry.dispatch("run_workflow", args)

        return json.loads(arun(_call()))


# ---------------------------------------------------------------------------
# classify_and_act
# ---------------------------------------------------------------------------

def test_classify_and_act_routes():
    def fn(snap):
        if snap["tools"] is None:  # 分类裁判
            assert "任务分类器" in _sys(snap)
            return {"content": '{"category": "售后"}'}
        return {"content": "售后处理完成"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "classify_and_act", "task": "处理这条客户消息",
        "categories": [
            {"name": "售后", "instruction": "按售后工单流程处理"},
            {"name": "咨询", "instruction": "按知识库解答"},
        ]}, provider)

    assert payload["status"] == "ok"
    assert payload["content"] == "售后处理完成"
    assert [s["step"] for s in payload["steps"]] == ["classify", "act:售后"]
    # 叶子任务带类别指令 + 原始任务
    leaf_user = _user(_leaf_calls(provider)[0])
    assert "按售后工单流程处理" in leaf_user
    assert "处理这条客户消息" in leaf_user


def test_classify_invalid_category_errors():
    def fn(snap):
        if snap["tools"] is None:
            return {"content": '{"category": "不存在的类别"}'}
        return {"content": "x"}

    payload = _run_workflow({
        "workflow": "classify_and_act", "task": "t",
        "categories": [{"name": "A", "instruction": "a"},
                       {"name": "B", "instruction": "b"}]},
        FuncProvider(fn))
    assert payload["status"] == "error"
    assert "合法类别" in payload["error"]


# ---------------------------------------------------------------------------
# fanout_and_synthesize
# ---------------------------------------------------------------------------

def test_fanout_parallel_and_synthesize():
    def fn(snap):
        if snap["tools"] is None:  # 汇总
            assert "汇总合成器" in _sys(snap)
            return {"content": "合并结论"}
        user = _user(snap)
        if "A 的背景" in user:
            return {"content": "结果A"}
        if "B 的背景" in user:
            return {"content": "结果B"}
        return {"content": "结果C"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "fanout_and_synthesize", "task": "调研 X",
        "subtasks": ["调研 A 的背景", "调研 B 的背景", "调研 C 的背景"],
    }, provider)

    assert payload["status"] == "ok"
    assert payload["content"] == "合并结论"
    assert [s["step"] for s in payload["steps"]] == [
        "subtask#1", "subtask#2", "subtask#3", "synthesize"]
    # 每个叶子的任务就是对应子任务本身（自包含）
    leaf_users = [_user(s) for s in _leaf_calls(provider)]
    assert leaf_users == ["调研 A 的背景", "调研 B 的背景", "调研 C 的背景"]
    # 汇总拿到全部三份结果
    synth_user = _user(_judge_calls(provider)[0])
    for piece in ("结果A", "结果B", "结果C"):
        assert piece in synth_user


def test_fanout_partial_when_one_leaf_fails():
    def fn(snap):
        if snap["tools"] is None:
            return {"content": "合并结论"}
        if "坏" in _user(snap):
            raise RuntimeError("boom")
        return {"content": "结果OK"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "fanout_and_synthesize", "task": "t",
        "subtasks": ["好任务一", "坏任务", "好任务二"],
    }, provider)

    assert payload["status"] == "partial"
    statuses = {s["step"]: s["status"] for s in payload["steps"]}
    assert statuses["subtask#1"] == "ok"
    assert statuses["subtask#2"] == "failed"
    assert statuses["subtask#3"] == "ok"
    # 汇总材料里带失败标注
    assert "[此候选生成失败]" in _user(_judge_calls(provider)[0])


# ---------------------------------------------------------------------------
# adversarial_verification
# ---------------------------------------------------------------------------

def test_adversarial_passes_on_second_cycle():
    def fn(snap):
        if snap["tools"] is None:  # 审校裁判
            user = _user(snap)
            assert "审校者" in _sys(snap)
            if "草稿V2" in user:
                return {"content": '{"pass": true, "issues": []}'}
            return {"content": '{"pass": false, "issues": ["缺数据支撑"]}'}
        if "上一版草稿" in _user(snap):
            return {"content": "草稿V2"}
        return {"content": "草稿V1"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "adversarial_verification", "task": "写一份分析",
    }, provider)

    assert payload["status"] == "ok"
    assert payload["content"] == "草稿V2"
    assert payload["verdict"] == {"pass": True, "issues": []}
    assert payload["verdict_parsed"] is True
    assert [s["step"] for s in payload["steps"]] == [
        "cycle#1:generate", "cycle#1:verify",
        "cycle#2:generate", "cycle#2:verify"]
    # 第二轮生成叶子带前稿 + 审校意见
    rewrite_user = _user(_leaf_calls(provider)[1])
    assert "草稿V1" in rewrite_user and "缺数据支撑" in rewrite_user


def test_adversarial_exhausted_partial():
    def fn(snap):
        if snap["tools"] is None:
            return {"content": '{"pass": false, "issues": ["还不行"]}'}
        return {"content": "草稿"}

    payload = _run_workflow({
        "workflow": "adversarial_verification", "task": "t",
    }, FuncProvider(fn))

    assert payload["status"] == "partial"
    assert payload["verdict"]["pass"] is False
    assert len(payload["steps"]) == _GUARD["max_cycles"] * 2


def test_adversarial_verdict_unparseable_conservative_pass():
    calls = {"n": 0}

    def fn(snap):
        if snap["tools"] is None:
            calls["n"] += 1
            return {"content": "我不会输出 JSON"}
        return {"content": "草稿"}

    payload = _run_workflow({
        "workflow": "adversarial_verification", "task": "t",
    }, FuncProvider(fn))

    assert payload["status"] == "ok"
    assert payload["verdict"] == {"pass": True}
    assert payload["verdict_parsed"] is False
    # 一次修复重试：同一轮审校共 2 次调用后走保守默认
    assert calls["n"] == 2
    assert [s["step"] for s in payload["steps"]] == [
        "cycle#1:generate", "cycle#1:verify"]


# ---------------------------------------------------------------------------
# generate_add_filter
# ---------------------------------------------------------------------------

def test_gaf_three_stages():
    def fn(snap):
        system = _sys(snap)
        if "累积合并器" in system:
            return {"content": "超集草案"}
        if "筛选收敛器" in system:
            return {"content": "最终答案"}
        # 生成叶子
        user = _user(snap)
        if "激进视角" in user:
            return {"content": "激进候选"}
        if "保守视角" in user:
            return {"content": "保守候选"}
        return {"content": "均衡候选"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "generate_add_filter", "task": "给 X 提方案",
        "angles": ["激进视角", "保守视角", "均衡视角"],
        "criteria": "成本可控",
    }, provider)

    assert payload["status"] == "ok"
    assert payload["content"] == "最终答案"
    assert [s["step"] for s in payload["steps"]] == [
        "gen#1", "gen#2", "gen#3", "add", "filter"]
    add_user = _user(_judge_calls(provider)[0])
    for piece in ("激进候选", "保守候选", "均衡候选"):
        assert piece in add_user
    filter_snap = _judge_calls(provider)[1]
    assert "成本可控" in _sys(filter_snap)   # 筛选标准进了裁判 prompt
    assert "超集草案" in _user(filter_snap)


def test_gaf_n_fallback_width():
    def fn(snap):
        if "生成视角" not in _user(snap) and snap["tools"] is not None:
            pass
        if snap["tools"] is None:
            return {"content": "超集"}
        return {"content": "候选"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "generate_add_filter", "task": "t", "n": 2,
    }, provider)

    assert payload["status"] == "ok"
    gen_steps = [s for s in payload["steps"] if s["step"].startswith("gen#")]
    assert len(gen_steps) == 2
    # 未给 angles → 模板视角
    assert "第 1 种差异化视角" in _user(_leaf_calls(provider)[0])


# ---------------------------------------------------------------------------
# tournament
# ---------------------------------------------------------------------------

def test_tournament_bracket_with_bye():
    def fn(snap):
        if snap["tools"] is None:  # 评委
            user = _user(snap)
            if "方案丙" in user:
                return {"content": '{"winner": "B", "reason": "丙更优"}'}
            return {"content": '{"winner": "A", "reason": "甲胜"}'}
        user = _user(snap)
        if "第 1 种" in user:
            return {"content": "方案甲"}
        if "第 2 种" in user:
            return {"content": "方案乙"}
        return {"content": "方案丙"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "tournament", "task": "选出最优方案", "n": 3,
    }, provider)

    assert payload["status"] == "ok"
    assert payload["content"] == "方案丙"
    assert [s["step"] for s in payload["steps"]] == [
        "gen#1", "gen#2", "gen#3", "match:r1#1", "match:r2#1"]


def test_tournament_inputs_no_generation():
    def fn(snap):
        assert snap["tools"] is None  # 只有评委调用，无生成叶子
        return {"content": '{"winner": "A", "reason": "甲好"}'}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "tournament", "task": "二选一",
        "inputs": ["甲方案全文", "乙方案全文"],
    }, provider)

    assert payload["status"] == "ok"
    assert payload["content"] == "甲方案全文"
    assert [s["step"] for s in payload["steps"]] == ["match:r1#1"]
    assert len(provider.seen) == 1


# ---------------------------------------------------------------------------
# loop_until_done
# ---------------------------------------------------------------------------

def test_loop_done_on_second_iteration():
    def fn(snap):
        if snap["tools"] is None:  # 完成检查
            assert "完成检查员" in _sys(snap)
            if "终稿" in _user(snap):
                return {"content": '{"done": true, "missing": []}'}
            return {"content": '{"done": false, "missing": ["缺结论段"]}'}
        if "上一版产出" in _user(snap):
            return {"content": "终稿"}
        return {"content": "初稿"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "loop_until_done", "task": "写一篇总结",
        "done_criteria": "有结论段；不超过 500 字",
    }, provider)

    assert payload["status"] == "ok"
    assert payload["content"] == "终稿"
    assert payload["verdict"] == {"done": True, "missing": []}
    assert [s["step"] for s in payload["steps"]] == [
        "iteration#1:work", "iteration#1:check",
        "iteration#2:work", "iteration#2:check"]
    rewrite_user = _user(_leaf_calls(provider)[1])
    assert "初稿" in rewrite_user and "缺结论段" in rewrite_user


def test_loop_exhausted_partial():
    def fn(snap):
        if snap["tools"] is None:
            return {"content": '{"done": false, "missing": ["还差一点"]}'}
        return {"content": "草稿"}

    payload = _run_workflow({
        "workflow": "loop_until_done", "task": "t",
        "done_criteria": "完成",
    }, FuncProvider(fn))

    assert payload["status"] == "partial"
    assert payload["verdict"]["done"] is False
    assert len(payload["steps"]) == _GUARD["max_iterations"] * 2


# ---------------------------------------------------------------------------
# 护栏：宽度 / 嵌套 / 超时 / settings 封顶
# ---------------------------------------------------------------------------

def test_width_and_param_guards():
    assert "subtasks" in _run_workflow({
        "workflow": "fanout_and_synthesize", "task": "t",
        "subtasks": [f"s{i}" for i in range(9)],
    }, FuncProvider(lambda s: {}))["error"]

    assert "angles" in _run_workflow({
        "workflow": "generate_add_filter", "task": "t",
        "angles": [f"a{i}" for i in range(9)],
    }, FuncProvider(lambda s: {}))["error"]

    assert "n 需要" in _run_workflow({
        "workflow": "tournament", "task": "t", "n": 1,
    }, FuncProvider(lambda s: {}))["error"]

    assert "workflow 必须" in _run_workflow({
        "workflow": "no_such_topology", "task": "t",
    }, FuncProvider(lambda s: {}))["error"]

    assert "done_criteria" in _run_workflow({
        "workflow": "loop_until_done", "task": "t",
    }, FuncProvider(lambda s: {}))["error"]


def test_nesting_refused():
    provider = FuncProvider(lambda s: {"content": "x"})
    payload = _run_workflow({
        "workflow": "loop_until_done", "task": "t", "done_criteria": "完成",
    }, provider, in_workflow=True)
    assert "嵌套" in payload["error"]
    assert provider.seen == []

    # delegate_task 在 workflow 作用域内同样拒绝
    async def _delegate_nested():
        with tool_call_context(_LLM, ("test_wf_pool",)):
            with workflow_scope(current_tool_context()):
                return await tool_registry.dispatch(
                    "delegate_task", {"task": "x"})

    assert "嵌套" in json.loads(
        arun(_delegate_nested()))["error"]


def test_timeout_returns_completed_steps():
    def fn(snap):
        if snap["tools"] is None:  # 汇总阶段挂起
            return {"hang": True}
        return {"content": "部分结果"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "fanout_and_synthesize", "task": "t",
        "subtasks": ["子任务一", "子任务二"],
        "timeout_seconds": 1,
    }, provider)

    assert payload["status"] == "timeout"
    assert [s["step"] for s in payload["steps"]] == ["subtask#1", "subtask#2"]
    assert payload["elapsed_seconds"] < 10


def test_settings_cap_wins_over_args():
    def fn(snap):
        if snap["tools"] is None:
            return {"content": '{"pass": false, "issues": ["不行"]}'}
        return {"content": "草稿"}

    payload = _run_workflow({
        "workflow": "adversarial_verification", "task": "t",
        "max_cycles": 5,   # args 想跑 5 轮，settings 封顶 1
    }, FuncProvider(fn), guard={**_GUARD, "max_cycles": 1})

    assert payload["status"] == "partial"
    assert [s["step"] for s in payload["steps"]] == [
        "cycle#1:generate", "cycle#1:verify"]


# ---------------------------------------------------------------------------
# 无 ambient contextvar：回退全局 llm 配置、叶子无工具（纯推理）
# ---------------------------------------------------------------------------

def test_fallback_without_ambient():
    def fn(snap):
        if "任务分类器" in _sys(snap):
            return {"content": '{"category": "A类"}'}
        return {"content": "A类处理完成"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "classify_and_act", "task": "t",
        "categories": [{"name": "A类", "instruction": "处理 A"},
                       {"name": "B类", "instruction": "处理 B"}],
    }, provider, no_ambient=True, llm_fallback=_LLM)

    assert payload["status"] == "ok"
    assert payload["content"] == "A类处理完成"
    assert provider.seen[0]["model"] == "m"
    assert all(s["tools"] is None for s in provider.seen)  # 无授权边界 → 无工具


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


def _tc(cid, name, args_dict):
    return {"id": cid, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args_dict, ensure_ascii=False)}}


def test_loop_executor_injects_context_e2e():
    """主 agent 经 default_loop 调 run_workflow：executor 注入的 llm_config
    与 pattern 授权边界一路传到 workflow 的叶子与裁判。"""
    node = BaseNode(code="main", name="主节点", use_tools=["run_workflow"])
    p = Pattern(code="pg-wf", name="t", description="t",
                allow_toolset=["workflow", "test_wf_pool"], nodes=[node])
    s = Session(session_id="s-wf-e2e", pattern_code="pg-wf")
    s.pattern = p
    s.cxt.node_map = p.node_map
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}

    def wf_fn(snap):
        if snap["tools"] is None:
            return {"content": "汇总完成"}
        return {"content": "叶子结果"}

    wf_provider = FuncProvider(wf_fn)
    parent = ParentScriptedProvider([
        {"content": None, "tool_calls": [_tc("c1", "run_workflow", {
            "workflow": "fanout_and_synthesize", "task": "查 X 的资料",
            "subtasks": ["子任务甲", "子任务乙"]})]},
        {"content": "已完成汇总", "tool_calls": []},
    ])

    with patch("atoms.executors.loop_executor.build_provider",
               return_value=parent), \
         patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}), \
         patch.object(workflow_tool, "build_provider",
                      return_value=wf_provider), \
         patch.object(workflow_tool, "get_workflow_tool_config",
                      return_value=dict(_GUARD)):
        result = arun(chat_turn("帮我查一下", s.session_id,
                                {s.session_id: s}))

    assert result.text == "已完成汇总"

    tool_rows = [m for m in s.cxt.history if m.role == "tool"]
    payload = json.loads(tool_rows[0].content)
    assert payload["status"] == "ok"
    assert payload["workflow"] == "fanout_and_synthesize"
    assert payload["content"] == "汇总完成"
    assert [st["step"] for st in payload["steps"]] == [
        "subtask#1", "subtask#2", "synthesize"]

    # 注入契约：叶子用父节点同款 model，叶子工具池剔除编排工具集
    leaves = _leaf_calls(wf_provider)
    assert {t["function"]["name"] for t in leaves[0]["tools"]} == {"wf_probe"}
    assert all(leaf["model"] == "m" for leaf in leaves)
    leaf_users = [_user(leaf) for leaf in leaves]
    assert leaf_users == ["子任务甲", "子任务乙"]
    # 主循环侧：节点只暴露 run_workflow
    assert {t["function"]["name"]
            for t in parent.seen[0]["tools"]} == {"run_workflow"}
