"""Unit tests for the run_workflow workflow tool: six topologies, judge
fault tolerance, guards (width / nesting / timeout / settings caps), the
no-ambient fallback, and the default_loop injection contract (end-to-end
via chat_turn). Fine-grained leaf-engine (_subagent_core) behavior is
covered by test_delegate_task.py; only the topology-orchestration layer
is tested here.
"""

import asyncio
import json

from async_utils import arun
from unittest.mock import patch

from atoms.tools import subagent_tool, workflow_tool  # importing registers them
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

import atoms.executors  # noqa: F401 -- default_loop must be registered


# ---------------------------------------------------------------------------
# Test probe tool (toolset: test_wf_pool)
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
# Scripted provider and run helpers
# ---------------------------------------------------------------------------

_GUARD = {"timeout_seconds": 300, "max_rounds": 8, "max_width": 8,
          "max_cycles": 3, "max_iterations": 5, "max_result_chars": 8000,
          "trace_cap": 32}
_LLM = {"code": "x", "model": "m", "temperature": 0.7, "max_tokens": 512}


class FuncProvider:
    """fn(snapshot) -> response dict (may contain "hang"); snapshots are
    recorded in call order.

    Leaf calls (when the allowed pool is non-empty) have tools not None;
    judge/synthesize calls have tools None — tests further distinguish the
    judge type by system-prompt content.
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
    """Run run_workflow via tool_registry.dispatch (covers the is_async registration path)."""
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
        if snap["tools"] is None:  # classify judge
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
    # Leaf task carries the category instruction + the original task
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
        if snap["tools"] is None:  # synthesize
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
    # Each leaf's task is the subtask itself (self-contained)
    leaf_users = [_user(s) for s in _leaf_calls(provider)]
    assert leaf_users == ["调研 A 的背景", "调研 B 的背景", "调研 C 的背景"]
    # Synthesize receives all three results
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
    # Synthesis material carries the failure annotation
    assert "[此候选生成失败]" in _user(_judge_calls(provider)[0])


# ---------------------------------------------------------------------------
# adversarial_verification
# ---------------------------------------------------------------------------

def test_adversarial_passes_on_second_cycle():
    def fn(snap):
        if snap["tools"] is None:  # review judge
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
    # The cycle-2 generation leaf carries the previous draft + review issues
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
    # One repair retry: after 2 review calls in the same round, take the conservative default
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
        # generation leaf
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
    assert "成本可控" in _sys(filter_snap)   # filter criteria reached the judge prompt
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
    # no angles given -> template angles
    assert "第 1 种差异化视角" in _user(_leaf_calls(provider)[0])


# ---------------------------------------------------------------------------
# tournament
# ---------------------------------------------------------------------------

def test_tournament_bracket_with_bye():
    def fn(snap):
        if snap["tools"] is None:  # judge panel
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
        assert snap["tools"] is None  # only judge calls, no generation leaves
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
        if snap["tools"] is None:  # completion check
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
# Guards: width / nesting / timeout / settings caps
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

    # delegate_task is likewise refused inside a workflow scope
    async def _delegate_nested():
        with tool_call_context(_LLM, ("test_wf_pool",)):
            with workflow_scope(current_tool_context()):
                return await tool_registry.dispatch(
                    "delegate_task", {"task": "x"})

    assert "嵌套" in json.loads(
        arun(_delegate_nested()))["error"]


def test_timeout_returns_completed_steps():
    def fn(snap):
        if snap["tools"] is None:  # synthesize stage hangs
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
        "max_cycles": 5,   # args wants 5 cycles; settings caps at 1
    }, FuncProvider(fn), guard={**_GUARD, "max_cycles": 1})

    assert payload["status"] == "partial"
    assert [s["step"] for s in payload["steps"]] == [
        "cycle#1:generate", "cycle#1:verify"]


# ---------------------------------------------------------------------------
# No ambient contextvar: fall back to the global llm config; leaves get no tools (pure reasoning)
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
    assert all(s["tools"] is None for s in provider.seen)  # no authorization boundary -> no tools


# ---------------------------------------------------------------------------
# default_loop injection contract (end-to-end via chat_turn)
# ---------------------------------------------------------------------------

class ParentScriptedProvider:
    """Main-loop-side scripted provider (same duck-typing as test_loop_tool_guards)."""

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
    """The main agent calls run_workflow via default_loop: the executor's
    injected llm_config and the pattern authorization boundary flow all the
    way to the workflow's leaves and judge."""
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

    # Injection contract: leaves use the parent node's model; the leaf tool pool excludes the orchestration toolset
    leaves = _leaf_calls(wf_provider)
    assert {t["function"]["name"] for t in leaves[0]["tools"]} == {"wf_probe"}
    assert all(leaf["model"] == "m" for leaf in leaves)
    leaf_users = [_user(leaf) for leaf in leaves]
    assert leaf_users == ["子任务甲", "子任务乙"]
    # Main-loop side: the node only exposes run_workflow
    assert {t["function"]["name"]
            for t in parent.seen[0]["tools"]} == {"run_workflow"}


# ---------------------------------------------------------------------------
# Strict judge-field typing / winner validation / LLM-stage exception degradation
# ---------------------------------------------------------------------------

def test_adversarial_pass_string_fails_closed():
    """pass given as the string "false" (truthy) -> invalid type -> the
    repair retry is still invalid -> fail-closed rejection (adversarial
    content rejected); cycles exhausted -> partial."""
    calls = {"n": 0}

    def fn(snap):
        if snap["tools"] is None:
            calls["n"] += 1
            return {"content": '{"pass": "false", "issues": ["假值字符串"]}'}
        return {"content": "草稿"}

    payload = _run_workflow({
        "workflow": "adversarial_verification", "task": "t",
    }, FuncProvider(fn))

    assert payload["status"] == "partial"            # not ok: judged as failed
    assert payload["verdict"]["pass"] is False       # fail-closed
    assert payload["verdict_parsed"] is False
    assert "无效" in payload["note"]
    # each cycle's review carries one type-repair retry (2 calls); all 3 cycles exhausted
    assert calls["n"] == _GUARD["max_cycles"] * 2


def test_loop_done_string_invalid_until_valid_bool():
    def fn(snap):
        if snap["tools"] is None:
            return {"content": '{"done": "true", "missing": []}'}  # string truthy value
        return {"content": "草稿"}

    payload = _run_workflow({
        "workflow": "loop_until_done", "task": "t", "done_criteria": "完成",
    }, FuncProvider(fn))

    assert payload["status"] == "partial"            # while invalid, always judged not done
    assert payload["verdict"]["done"] is False
    assert payload["verdict_parsed"] is False
    assert "无效" in payload["note"]


def test_loop_done_bool_semantics():
    """Real booleans keep the original semantics: false -> not done, keep
    iterating; true -> finish immediately."""
    def fn_false(snap):
        if snap["tools"] is None:
            return {"content": '{"done": false, "missing": ["还差"]}'}
        return {"content": "草稿"}

    payload = _run_workflow({
        "workflow": "loop_until_done", "task": "t", "done_criteria": "完成",
    }, FuncProvider(fn_false))
    assert payload["status"] == "partial"
    assert payload["verdict"]["done"] is False
    assert "note" not in payload                     # a valid verdict adds no note

    def fn_true(snap):
        if snap["tools"] is None:
            return {"content": '{"done": true, "missing": []}'}
        return {"content": "终稿"}

    payload = _run_workflow({
        "workflow": "loop_until_done", "task": "t", "done_criteria": "完成",
    }, FuncProvider(fn_true))
    assert payload["status"] == "ok"
    assert payload["verdict"] == {"done": True, "missing": []}


def test_tournament_winner_null_falls_back_to_first():
    def fn(snap):
        assert snap["tools"] is None                 # only judge calls
        return {"content": '{"winner": null, "reason": "无"}'}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "tournament", "task": "二选一",
        "inputs": ["甲方案全文", "乙方案全文"],
    }, provider)

    assert payload["status"] == "ok"
    assert payload["content"] == "甲方案全文"        # never silently falls to B
    assert "无效" in payload["note"]
    assert len(provider.seen) == 2                   # one repair retry


def test_tournament_chinese_winner_invalid():
    def fn(snap):
        return {"content": '{"winner": "候选A", "reason": "偏好"}'}

    payload = _run_workflow({
        "workflow": "tournament", "task": "二选一",
        "inputs": ["甲", "乙"],
    }, FuncProvider(fn))

    assert payload["content"] == "甲"                # invalid -> fallback to the former
    assert "无效" in payload["note"]


def test_tournament_winner_normalized():
    def fn(snap):
        return {"content": '{"winner": "b"}'}        # lowercase is normalized too

    payload = _run_workflow({
        "workflow": "tournament", "task": "二选一",
        "inputs": ["甲", "乙"],
    }, FuncProvider(fn))

    assert payload["content"] == "乙"
    assert "note" not in payload


def test_gaf_filter_failure_keeps_superset():
    def fn(snap):
        system = _sys(snap)
        if "累积合并器" in system:
            return {"content": "超集草案"}
        if "筛选收敛器" in system:
            raise RuntimeError("filter boom")        # filter-stage provider exception
        return {"content": "候选"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "generate_add_filter", "task": "t",
        "angles": ["视角一", "视角二"],
    }, provider)

    # not error + empty content: degrade to partial, pass the superset through
    assert payload["status"] == "partial"
    assert payload["content"] == "超集草案"
    assert "filter 阶段失败未过滤" in payload["note"]
    statuses = {s["step"]: s["status"] for s in payload["steps"]}
    assert statuses["gen#1"] == "ok"
    assert statuses["add"] == "ok"
    assert statuses["filter"] == "failed"


def test_fanout_synthesize_failure_concatenates():
    def fn(snap):
        if snap["tools"] is None:
            raise RuntimeError("synth boom")         # synthesize-stage provider exception
        user = _user(snap)
        if user == "子任务一":
            return {"content": "结果一"}
        return {"content": "结果二"}

    provider = FuncProvider(fn)
    payload = _run_workflow({
        "workflow": "fanout_and_synthesize", "task": "t",
        "subtasks": ["子任务一", "子任务二"],
    }, provider)

    assert payload["status"] == "partial"
    assert "结果一" in payload["content"] and "结果二" in payload["content"]
    assert "synthesize 阶段失败" in payload["note"]
    statuses = {s["step"]: s["status"] for s in payload["steps"]}
    assert statuses["synthesize"] == "failed"


def test_gaf_add_failure_concatenates_then_filters():
    def fn(snap):
        system = _sys(snap)
        if "累积合并器" in system:
            raise RuntimeError("add boom")           # add-stage provider exception
        if "筛选收敛器" in system:
            return {"content": "筛选后"}
        return {"content": "候选"}

    payload = _run_workflow({
        "workflow": "generate_add_filter", "task": "t",
        "angles": ["视角一", "视角二"],
    }, FuncProvider(fn))

    # after add degrades to a concatenated draft, the filter stage still converges
    assert payload["status"] == "partial"
    assert payload["content"] == "筛选后"
    assert "add 阶段失败" in payload["note"]
    statuses = {s["step"]: s["status"] for s in payload["steps"]}
    assert statuses["add"] == "failed"
    assert statuses["filter"] == "ok"
