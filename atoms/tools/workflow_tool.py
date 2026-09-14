"""run_workflow — 六种拓扑的多子代理 workflow（toolset: workflow）。

一次 tool call = 一趟由预定义拓扑编排的多子代理 workflow：主 agent 负责
规划（显式传 subtasks / categories / angles / criteria），本 tool 负责确定
性执行；每个"叶子"步骤是一次 delegate 式子 ReAct 循环（引擎在
atoms/tools/_subagent_core.py），"裁判"步骤（分类器/审校者/评委/完成检查/
汇总）是无工具的单次 LLM 调用（temperature=0.0）。

与现有机制的分工（docstring 边界声明）::

    静态已知拓扑   → 图编排（多节点 pattern / TurnResult.sends fanout）
    运行时才决定   → 本 tool（LLM tool-call 驱动的节点内临时编排）
    单个子任务     → delegate_task

授权（deny-by-default，同 plan-⑧ §4）::

    pattern:
      allow_toolset: [workflow, knowledge]   # 叶子池 = knowledge 工具集
    node:
      use_tools: [run_workflow]

叶子可用工具池 = ``pattern.allow_toolset`` 各工具集下的工具，剔除
``subagent`` 与 ``workflow`` 两个工具集（结构性防递归，深度恒 1）；
``in_workflow`` 标志位是第二道防线。llm_config 经
``nexus.engine.tool_context`` 注入（与父节点同模型）；脱离 agent loop
直接 dispatch 时回退 ``get_llm_config()`` 且叶子无工具。

裁判容错（Q8a）：verdict JSON 解析失败 → 带错误提示重问 1 次 → 仍失败走
保守默认并标 ``verdict_parsed: false``（adversarial 判 pass、loop 判未完
成、tournament 前者胜）。

护栏（config ``workflow_tool`` 节可调）：整体超时默认 300s（args 只能调
小）、叶子轮次上限 8、并行宽度 ≤8、adversarial/loop 迭代上限 3/5、最终
结论截断 8000 字符、steps 上限 32 条。并行阶段单叶子失败标记 failed 继续
兄弟；全灭 → status=error；整体超时 → 已完成步骤照常带出（status=
timeout）；adversarial/loop 迭代耗尽 → status=partial。
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from atoms.tools._subagent_core import (
    _DEFAULT_SYSTEM_PROMPT,
    _accumulate_usage,
    _run_sub_agent,
    _truncate,
)
from nexus.engine.tool_context import (
    ToolCallContext,
    current_tool_context,
    workflow_scope,
)
from nexus.llm.resolve import build_provider
from nexus.registry.tools import registry, tool_error, tool_result
from nexus.settings import get_llm_config, get_workflow_tool_config

logger = logging.getLogger(__name__)

# 叶子池排除两个编排工具集（结构性防递归；in_workflow 标志是第二道防线）
_SELF_TOOLSETS = frozenset({"subagent", "workflow"})
# 喂给裁判/汇总调用的单份材料截断（防裁判 prompt 爆炸）
_PER_SYNTH_CHARS = 3000

# ---------------------------------------------------------------------------
# 裁判 system prompts
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM_PROMPT = (
    "你是任务分类器。从给定类别中选出最匹配任务的一个；必须选择最接近的"
    "类别，不存在\"都不匹配\"选项。只输出 JSON："
    '{"category": "<类别名>"}'
)

SYNTHESIZE_SYSTEM_PROMPT = (
    "你是汇总合成器。把多个子任务结果合并成一份完整、去重、结构清晰的"
    "最终答案。失败的任务在材料中以 [此候选生成失败] 标注：忽略其内容，"
    "并在答案中说明该部分缺失。直接输出合并后的答案。"
)

VERIFIER_SYSTEM_PROMPT = (
    "你是严格的审校者。检查产出是否正确、完整、可靠地完成了任务，"
    '只输出 JSON：{"pass": true/false, "issues": ["问题1", ...]}；'
    "pass 仅当不存在实质问题。"
)

ADD_SYSTEM_PROMPT = (
    "你是累积合并器。把多份候选中所有有价值、不重复的要点合并成一个"
    "超集草案（保留多样性，此阶段不做筛选）。直接输出超集草案。"
)

FILTER_SYSTEM_PROMPT = (
    "你是筛选收敛器。从超集草案中筛选出最终答案。筛选标准：{criteria}。"
    "直接输出筛选后的最终答案。"
)

JUDGE_SYSTEM_PROMPT = (
    "你是比赛评委。根据任务要求比较两个候选，选出更好地完成任务的胜者。"
    '只输出 JSON：{"winner": "A" 或 "B", "reason": "理由"}'
)

DONE_SYSTEM_PROMPT = (
    "你是完成检查员。对照完成条件逐条检查当前产出是否已达标，"
    '只输出 JSON：{"done": true/false, "missing": ["未满足项", ...]}'
)

# ---------------------------------------------------------------------------
# Schema（workflow 枚举的 description 完整解释六种拓扑的含义与选型）
# ---------------------------------------------------------------------------

RUN_WORKFLOW_SCHEMA = {
    "name": "run_workflow",
    "description": (
        "按六种预定义拓扑发起一趟多子代理 workflow 并取回最终结果。"
        "你负责规划（拆解子任务/定义类别/给视角/给标准），本工具负责执行；"
        "叶子子代理看不到当前对话，task 必须自包含。"
        "需要运行时才能确定的编排时用它；单个子任务用 delegate_task。"
        "workflow 内不可再嵌套委托或 workflow（深度=1）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow": {
                "type": "string",
                "enum": [
                    "classify_and_act", "fanout_and_synthesize",
                    "adversarial_verification", "generate_add_filter",
                    "tournament", "loop_until_done",
                ],
                "description": (
                    "编排拓扑，六选一：\n"
                    "- classify_and_act（分类后路由执行）：先把任务归入预定义"
                    "类别之一，再由该类别的专属子代理执行。适合不同类型任务"
                    "需要不同处理方式的路由场景。\n"
                    "- fanout_and_synthesize（并行扇出汇总）：把任务拆成多个"
                    "独立子任务并行执行，再合并成一份完整答案。适合调研、"
                    "检索、收集等可独立并行分解的任务。\n"
                    "- adversarial_verification（对抗校验）：生成答案后由审校"
                    "者挑错，未通过就带着问题清单重写，循环直到通过。适合"
                    "高正确性要求的产出。\n"
                    "- generate_add_filter（生成-累积-筛选）：多视角并行生成"
                    "候选，合并去重成超集，再按标准筛选收敛。适合创意、清单"
                    "类需要多角度产出的任务。\n"
                    "- tournament（锦标赛）：多份候选两两对决淘汰，由评委选出"
                    "最优。适合只要一个最佳答案的比较决策场景。\n"
                    "- loop_until_done（迭代到位）：反复改进同一份产出，直到"
                    "满足完成条件。适合需要多轮打磨的写作与分析。"
                ),
            },
            "task": {
                "type": "string",
                "description": (
                    "自包含的任务描述：目标、必要背景、期望的产出格式。"
                    "所有叶子与裁判都基于它，必要信息必须写全。"
                ),
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "叶子子代理可用的工具名列表（可选；必须在系统授权范围内，"
                    "缺省=授权范围内全部可用工具；裁判步骤永不带工具）"
                ),
            },
            "system_prompt": {
                "type": "string",
                "description": "叶子子代理的角色设定（可选；默认为结论导向的通用执行者）",
            },
            "temperature": {
                "type": "number",
                "description": "叶子子代理采样温度 0-2（可选，默认继承当前会话；裁判恒为 0）",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "整趟 workflow 的超时秒数（可选；只能调小，不能超过系统上限）",
            },
            "categories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "类别名"},
                        "instruction": {"type": "string",
                                        "description": "该类别专属子代理的任务指令"},
                    },
                    "required": ["name"],
                },
                "description": "classify_and_act 必填：2-8 个 {name, instruction}",
            },
            "subtasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "fanout_and_synthesize 必填：2-8 个自包含子任务",
            },
            "verifier_focus": {
                "type": "string",
                "description": "adversarial_verification 选填：审校的侧重方向",
            },
            "max_cycles": {
                "type": "integer",
                "description": "adversarial_verification 选填：生成-审校循环上限（默认 3，不可超过系统上限）",
            },
            "n": {
                "type": "integer",
                "description": "generate_add_filter / tournament 选填：候选数量（默认 3，2-8；给了 angles 则忽略）",
            },
            "angles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "generate_add_filter / tournament 选填：2-8 个候选生成视角（每个视角生成一份候选，优先于 n）",
            },
            "inputs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "tournament 选填：2-8 份现成候选文本（优先于内部生成，主 agent 已有候选方案时用）",
            },
            "criteria": {
                "type": "string",
                "description": "generate_add_filter 选填：筛选标准（缺省=去弱留强）",
            },
            "done_criteria": {
                "type": "string",
                "description": "loop_until_done 必填：完成条件（逐条可检查）",
            },
            "max_iterations": {
                "type": "integer",
                "description": "loop_until_done 选填：迭代上限（默认 5，不可超过系统上限）",
            },
        },
        "required": ["workflow", "task"],
    },
}


# ---------------------------------------------------------------------------
# 裁判/汇总调用与解析
# ---------------------------------------------------------------------------

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_object(text: str) -> Optional[dict]:
    """Parse a JSON object verdict; tolerate code-fence wrapping."""
    candidates = [text]
    m = _JSON_OBJECT_RE.search(text)
    if m:
        candidates.append(m.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


async def _llm_once(rt: Dict[str, Any], system_prompt: str,
                    user_prompt: str) -> Tuple[str, Dict[str, Any]]:
    """One plain LLM call (judges / synthesize / add / filter), no tools."""
    result = await rt["provider"].achat_completion(
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_prompt}],
        model=rt["model"], temperature=0.0, max_tokens=rt["max_tokens"])
    return result.get("content", "") or "", result.get("usage") or {}


async def _judge(rt: Dict[str, Any], system_prompt: str,
                 user_prompt: str) -> Tuple[Optional[dict], bool, Dict[str, int]]:
    """Judge call: JSON verdict with one repair retry.

    Returns (verdict, parsed, usage). Unparseable twice (or provider errors
    twice) → (None, False, usage) — the caller applies its conservative
    default and marks verdict_parsed=False in the payload.
    """
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}]
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    for attempt in range(2):
        try:
            result = await rt["provider"].achat_completion(
                messages=messages, model=rt["model"], temperature=0.0,
                max_tokens=rt["max_tokens"])
        except Exception as e:
            logger.warning("[run_workflow] 裁判调用失败（第 %d 次）: %s",
                           attempt + 1, e)
            continue
        text = result.get("content", "") or ""
        _accumulate_usage(usage, result.get("usage"))
        verdict = _parse_json_object(text)
        if verdict is not None:
            return verdict, True, usage
        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": (
                "上一次输出无法解析为 JSON 对象。请只输出一个 JSON 对象，"
                "不要包含任何其他文字或代码块标记。")},
        ]
    return None, False, usage


# ---------------------------------------------------------------------------
# 步骤记录与叶子/候选辅助
# ---------------------------------------------------------------------------

class _Steps:
    """拓扑级步骤记录器（payload 组装时按 trace_cap 截断）。"""

    def __init__(self):
        self.items: List[Dict[str, Any]] = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def add(self, step: str, status: str, rounds: int = 1,
            usage: Optional[Dict[str, Any]] = None) -> None:
        self.items.append({
            "step": step, "status": status, "rounds": rounds,
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
        })
        _accumulate_usage(self.usage, usage)


async def _run_leaf(rt: Dict[str, Any], label: str,
                    task_text: str) -> Dict[str, Any]:
    """Run one leaf sub-loop and record its step.

    A leaf that hit its own max_rounds still carries partial content —
    counted as ok; only LLM-level failures (status=error) mark failed.
    """
    state: Dict[str, Any] = {}
    payload = await _run_sub_agent(
        provider=rt["provider"], llm_config=rt["llm_config"],
        system_prompt=rt["leaf_system_prompt"], task=task_text,
        granted=rt["granted"], temperature=rt["temperature"],
        max_rounds=rt["max_rounds"], state=state)
    status = "failed" if payload.get("status") == "error" else "ok"
    rt["steps"].add(label, status,
                    rounds=payload.get("rounds", 0) or 0,
                    usage=payload.get("usage"))
    return payload


async def _gen_candidates(rt: Dict[str, Any],
                          parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Tournament/GAF candidates: inputs win, else one leaf per angle."""
    if parsed.get("_inputs") is not None:
        return [{"text": t, "ok": True} for t in parsed["_inputs"]]
    angles = parsed["_angles"]

    async def _one(i: int, angle: str) -> Dict[str, Any]:
        payload = await _run_leaf(
            rt, f"gen#{i + 1}", f"{rt['task']}\n\n【生成视角】{angle}")
        return {"text": payload.get("content") or "",
                "ok": payload.get("status") != "error"}

    return list(await asyncio.gather(
        *[_one(i, a) for i, a in enumerate(angles)]))


def _render_candidates(items: List[Dict[str, Any]]) -> str:
    """Render candidates for judge/synthesis prompts (per-piece truncated,
    failures marked so downstream stages know a piece is missing)."""
    parts = []
    for i, item in enumerate(items):
        marker = "[此候选生成失败] " if not item["ok"] else ""
        parts.append(f"【候选 {i + 1}】{marker}"
                     f"{_truncate(item['text'], _PER_SYNTH_CHARS, '候选')}")
    return "\n\n".join(parts)


def _all_failed(items: List[Dict[str, Any]]) -> bool:
    return all(not item["ok"] for item in items)


# ---------------------------------------------------------------------------
# 六种拓扑
# ---------------------------------------------------------------------------

async def _wf_classify_and_act(rt, parsed) -> Dict[str, Any]:
    categories = parsed["_categories"]
    listing = "\n".join(
        f"- {c['name']}：{c['instruction']}" for c in categories)
    verdict, parsed_ok, usage = await _judge(
        rt, CLASSIFY_SYSTEM_PROMPT,
        f"任务：{rt['task']}\n\n可选类别：\n{listing}")
    rt["steps"].add("classify", "ok", rounds=1, usage=usage)
    picked = verdict.get("category") if (parsed_ok and verdict) else None
    matched = next((c for c in categories if c["name"] == picked), None)
    if matched is None:
        return {"status": "error",
                "error": (f"分类结果无效（{picked!r}）；合法类别："
                          f"{[c['name'] for c in categories]}")}
    instruction = matched["instruction"]
    leaf_task = (f"{instruction}\n\n【原始任务】{rt['task']}"
                 if instruction else rt["task"])
    payload = await _run_leaf(rt, f"act:{matched['name']}", leaf_task)
    rt["content"] = payload.get("content") or ""
    if payload.get("status") == "error":
        return {"status": "error", "error": payload.get("error", "执行叶子失败")}
    return {"status": "ok"}


async def _wf_fanout_and_synthesize(rt, parsed) -> Dict[str, Any]:
    subtasks = parsed["_subtasks"]

    async def _one(i: int, subtask: str) -> Dict[str, Any]:
        payload = await _run_leaf(rt, f"subtask#{i + 1}", subtask)
        return {"text": payload.get("content") or "",
                "ok": payload.get("status") != "error"}

    items = list(await asyncio.gather(
        *[_one(i, st) for i, st in enumerate(subtasks)]))
    if _all_failed(items):
        return {"status": "error", "error": "全部子任务失败"}
    content, usage = await _llm_once(
        rt, SYNTHESIZE_SYSTEM_PROMPT,
        f"任务：{rt['task']}\n\n各子任务结果：\n{_render_candidates(items)}")
    rt["steps"].add("synthesize", "ok", rounds=1, usage=usage)
    rt["content"] = content
    return {"status": "partial" if any(not it["ok"] for it in items) else "ok"}


async def _wf_adversarial_verification(rt, parsed) -> Dict[str, Any]:
    focus = parsed.get("verifier_focus", "")
    system = VERIFIER_SYSTEM_PROMPT + (f"\n审校侧重：{focus}" if focus else "")
    issues_text = ""
    for cycle in range(1, parsed["_max_cycles"] + 1):
        gen_task = rt["task"]
        if cycle > 1:
            gen_task = (f"{rt['task']}\n\n【上一版草稿，请在此基础上修订】\n"
                        f"{rt['content']}\n\n【审校意见，逐条修正】\n{issues_text}")
        payload = await _run_leaf(rt, f"cycle#{cycle}:generate", gen_task)
        rt["content"] = payload.get("content") or rt["content"]
        if payload.get("status") == "error":
            return {"status": "error", "error": payload.get("error", "生成叶子失败")}
        draft = rt["content"]
        verdict, parsed_ok, usage = await _judge(
            rt, system,
            f"任务：{rt['task']}\n\n待审产出：\n"
            f"{_truncate(draft, _PER_SYNTH_CHARS, '产出')}")
        rt["steps"].add(f"cycle#{cycle}:verify", "ok", rounds=1, usage=usage)
        rt["verdict_parsed"] = parsed_ok
        if not parsed_ok:
            rt["verdict"] = {"pass": True}   # 保守默认：判 pass，避免无意义循环
            return {"status": "ok"}
        rt["verdict"] = verdict
        if bool(verdict.get("pass")):
            return {"status": "ok"}
        issues = verdict.get("issues") or []
        issues_text = ("\n".join(f"- {i}" for i in issues)
                       or "-（审校未列出具体问题）")
    return {"status": "partial"}   # 轮次耗尽仍未通过


async def _wf_generate_add_filter(rt, parsed) -> Dict[str, Any]:
    items = await _gen_candidates(rt, parsed)
    if _all_failed(items):
        return {"status": "error", "error": "全部候选生成失败"}
    superset, usage = await _llm_once(
        rt, ADD_SYSTEM_PROMPT,
        f"任务：{rt['task']}\n\n各候选：\n{_render_candidates(items)}")
    rt["steps"].add("add", "ok", rounds=1, usage=usage)
    criteria = parsed.get("criteria", "")
    final, usage2 = await _llm_once(
        rt, FILTER_SYSTEM_PROMPT.format(
            criteria=criteria or "去弱留强，保留最有价值、最相关的部分，控制篇幅"),
        f"任务：{rt['task']}\n\n超集草案：\n"
        f"{_truncate(superset, _PER_SYNTH_CHARS * 2, '草案')}")
    rt["steps"].add("filter", "ok", rounds=1, usage=usage2)
    rt["content"] = final
    return {"status": "partial" if any(not it["ok"] for it in items) else "ok"}


async def _wf_tournament(rt, parsed) -> Dict[str, Any]:
    items = await _gen_candidates(rt, parsed)
    if _all_failed(items):
        return {"status": "error", "error": "全部候选生成失败"}
    live = list(items)
    round_no = 0
    while len(live) > 1:
        round_no += 1
        nxt = []
        match_idx = 0
        for i in range(0, len(live) - 1, 2):
            a, b = live[i], live[i + 1]
            match_idx += 1
            verdict, parsed_ok, usage = await _judge(
                rt, JUDGE_SYSTEM_PROMPT,
                f"任务：{rt['task']}\n\n{_render_candidates([a, b])}")
            rt["steps"].add(f"match:r{round_no}#{match_idx}", "ok",
                            rounds=1, usage=usage)
            winner_is_a = True   # 保守默认：解析失败前者胜
            if parsed_ok and str(verdict.get("winner", "A")).strip().upper() \
                    not in ("A", "1"):
                winner_is_a = False
            nxt.append(a if winner_is_a else b)
        if len(live) % 2:
            nxt.append(live[-1])   # 奇数轮空：末位直接晋级
        live = nxt
    rt["content"] = live[0]["text"]
    return {"status": "ok"}


async def _wf_loop_until_done(rt, parsed) -> Dict[str, Any]:
    criteria = parsed["_done_criteria"]
    missing_text = ""
    for iteration in range(1, parsed["_max_iterations"] + 1):
        work_task = rt["task"]
        if iteration > 1:
            work_task = (f"{rt['task']}\n\n【上一版产出，请在此基础上改进】\n"
                         f"{rt['content']}\n\n【尚未满足的完成条件】\n{missing_text}")
        payload = await _run_leaf(rt, f"iteration#{iteration}:work", work_task)
        rt["content"] = payload.get("content") or rt["content"]
        if payload.get("status") == "error":
            return {"status": "error", "error": payload.get("error", "工作叶子失败")}
        verdict, parsed_ok, usage = await _judge(
            rt, DONE_SYSTEM_PROMPT,
            f"完成条件：\n{criteria}\n\n当前产出：\n"
            f"{_truncate(rt['content'], _PER_SYNTH_CHARS, '产出')}")
        rt["steps"].add(f"iteration#{iteration}:check", "ok",
                        rounds=1, usage=usage)
        rt["verdict_parsed"] = parsed_ok
        if not parsed_ok:
            rt["verdict"] = {"done": False}   # 保守默认：未完成，继续迭代
            continue
        rt["verdict"] = verdict
        if bool(verdict.get("done")):
            return {"status": "ok"}
        missing = verdict.get("missing") or []
        missing_text = ("\n".join(f"- {m}" for m in missing)
                        or "-（未列出具体缺口）")
    return {"status": "partial"}   # 迭代耗尽仍未达标


_TOPOLOGIES = {
    "classify_and_act": _wf_classify_and_act,
    "fanout_and_synthesize": _wf_fanout_and_synthesize,
    "adversarial_verification": _wf_adversarial_verification,
    "generate_add_filter": _wf_generate_add_filter,
    "tournament": _wf_tournament,
    "loop_until_done": _wf_loop_until_done,
}


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------

def _opt_count(args: Dict[str, Any], key: str, default: int, cap: int
               ) -> Tuple[Optional[str], Optional[int]]:
    """选填计数参数：缺省 default，必须 ≥1，args 只能收窄到 cap 内。"""
    raw = args.get(key)
    if raw is None:
        return None, int(default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return f"{key} 应为正整数", None
    if value < 1:
        return f"{key} 必须 ≥ 1", None
    return None, min(value, int(cap))


def _resolve_angles(args: Dict[str, Any], cap: int, allow_inputs: bool
                    ) -> Tuple[Optional[str], Dict[str, Any]]:
    """GAF/tournament 候选来源：inputs（仅 tournament）> angles > n 模板视角。"""
    if allow_inputs and args.get("inputs") is not None:
        inputs = args["inputs"]
        if not isinstance(inputs, list) or not (2 <= len(inputs) <= cap):
            return f"inputs 需要 2-{cap} 份非空候选文本", {}
        if any(not str(t).strip() for t in inputs):
            return "inputs 内不能有空文本", {}
        return None, {"_inputs": [str(t).strip() for t in inputs]}
    if args.get("angles") is not None:
        angles = args["angles"]
        if not isinstance(angles, list) or not (2 <= len(angles) <= cap):
            return f"angles 需要 2-{cap} 个非空视角", {}
        if any(not str(a).strip() for a in angles):
            return "angles 内不能有空视角", {}
        return None, {"_angles": [str(a).strip() for a in angles]}
    err, n = _opt_count(args, "n", 3, cap)
    if err:
        return err, {}
    if n < 2:
        return f"n 需要 2-{cap}（单个候选没有比较/合并意义）", {}
    return None, {"_angles": [f"第 {i + 1} 种差异化视角" for i in range(n)]}


def _validate_args(args: Dict[str, Any], guard: Dict[str, Any]
                   ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Validate + normalize per-workflow params into ``parsed`` (``_``-keys
    carry resolved structures the topologies read)."""
    wf = str(args.get("workflow") or "")
    if wf not in _TOPOLOGIES:
        return (f"workflow 必须是以下之一：{sorted(_TOPOLOGIES)}", None)
    parsed: Dict[str, Any] = {"workflow": wf}
    cap = int(guard["max_width"])

    if wf == "classify_and_act":
        categories = args.get("categories")
        if not isinstance(categories, list) or not (2 <= len(categories) <= cap):
            return (f"classify_and_act 需要 categories（2-{cap} 个 "
                    f"{{name, instruction}}）", None)
        normalized = []
        for category in categories:
            if not isinstance(category, dict) \
                    or not str(category.get("name") or "").strip():
                return "每个 category 需要非空 name（instruction 选填）", None
            normalized.append({
                "name": str(category["name"]).strip(),
                "instruction": str(category.get("instruction") or "").strip(),
            })
        parsed["_categories"] = normalized
    elif wf == "fanout_and_synthesize":
        subtasks = args.get("subtasks")
        if not isinstance(subtasks, list) or not (2 <= len(subtasks) <= cap):
            return (f"fanout_and_synthesize 需要 subtasks（2-{cap} 个"
                    f"自包含子任务）", None)
        if any(not str(s).strip() for s in subtasks):
            return "subtasks 内不能有空任务", None
        parsed["_subtasks"] = [str(s).strip() for s in subtasks]
    elif wf == "adversarial_verification":
        err, max_cycles = _opt_count(args, "max_cycles",
                                     guard["max_cycles"], guard["max_cycles"])
        if err:
            return err, None
        parsed["_max_cycles"] = max_cycles
        parsed["verifier_focus"] = str(args.get("verifier_focus") or "").strip()
    elif wf == "generate_add_filter":
        err, resolved = _resolve_angles(args, cap, allow_inputs=False)
        if err:
            return err, None
        parsed.update(resolved)
        parsed["criteria"] = str(args.get("criteria") or "").strip()
    elif wf == "tournament":
        err, resolved = _resolve_angles(args, cap, allow_inputs=True)
        if err:
            return err, None
        parsed.update(resolved)
    elif wf == "loop_until_done":
        criteria = str(args.get("done_criteria") or "").strip()
        if not criteria:
            return "loop_until_done 需要 done_criteria（逐条可检查的完成条件）", None
        err, max_iterations = _opt_count(
            args, "max_iterations", guard["max_iterations"],
            guard["max_iterations"])
        if err:
            return err, None
        parsed["_done_criteria"] = criteria
        parsed["_max_iterations"] = max_iterations
    return None, parsed


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def _handle_run_workflow(args: Dict[str, Any]) -> str:
    ambient = current_tool_context()
    if ambient is not None and (ambient.in_subagent or ambient.in_workflow):
        return tool_error(
            "run_workflow 不允许嵌套调用（v1 深度=1）："
            "子代理/workflow 内无法再发起 workflow")

    guard = get_workflow_tool_config()
    err, parsed = _validate_args(args, guard)
    if err:
        return tool_error(err)
    wf = parsed["workflow"]

    task = str(args.get("task") or "").strip()
    if not task:
        return tool_error("task 必填：请提供自包含的任务描述")

    llm_config = (ambient.llm_config if ambient is not None else None) \
        or get_llm_config()

    temperature = llm_config.get("temperature", 0.7)
    if args.get("temperature") is not None:
        try:
            temperature = max(0.0, min(2.0, float(args["temperature"])))
        except (TypeError, ValueError):
            return tool_error("temperature 应为 0-2 的数字")

    timeout = float(guard["timeout_seconds"])
    if args.get("timeout_seconds") is not None:
        try:
            requested = float(args["timeout_seconds"])
        except (TypeError, ValueError):
            return tool_error("timeout_seconds 应为正数（秒）")
        if requested <= 0:
            return tool_error("timeout_seconds 必须大于 0")
        timeout = min(requested, timeout)

    # 叶子工具池：pattern 授权工具集 − 编排工具集（结构性防递归）
    pool: Set[str] = set()
    if ambient is not None and ambient.allow_toolsets:
        pool = registry.names_in_toolsets(
            set(ambient.allow_toolsets) - _SELF_TOOLSETS)
    granted = set(pool)
    if args.get("tools"):
        requested_tools = {str(t) for t in args["tools"]}
        invalid = sorted(requested_tools - pool)
        if invalid:
            return tool_error(
                f"以下工具不在 workflow 叶子可用池中: {invalid}。"
                f"可用工具：{sorted(pool)}。请从中选择，或省略 tools 使用全部可用工具。")
        granted = requested_tools

    rt: Dict[str, Any] = {
        "workflow": wf, "task": task, "llm_config": llm_config,
        "provider": build_provider(llm_config),
        "model": llm_config.get("model") or "",
        "max_tokens": llm_config.get("max_tokens", 2048),
        "granted": granted, "temperature": temperature,
        "max_rounds": int(guard["max_rounds"]),
        "leaf_system_prompt": str(args.get("system_prompt") or "").strip()
        or _DEFAULT_SYSTEM_PROMPT,
        "steps": _Steps(), "content": "",
        "verdict": None, "verdict_parsed": True,
    }
    logger.info("[run_workflow] 开始: workflow=%s, tools=%s, timeout=%.0fs, "
                "task=%r", wf, sorted(granted), timeout, task[:80])

    started = time.monotonic()
    base = ambient or ToolCallContext(
        llm_config=dict(llm_config), allow_toolsets=frozenset())

    async def _guarded() -> Dict[str, Any]:
        with workflow_scope(base):
            return await _TOPOLOGIES[wf](rt, parsed)

    try:
        outcome = await asyncio.wait_for(_guarded(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("[run_workflow] 整体超时（%.0fs），返回部分结果", timeout)
        outcome = {"status": "timeout"}
    except Exception as e:
        logger.exception("[run_workflow] 执行异常")
        outcome = {"status": "error",
                   "error": f"workflow 执行失败: {type(e).__name__}: {e}"}

    content = rt["content"] or ""
    limit = int(guard["max_result_chars"])
    truncated = len(content) > limit
    if truncated:
        content = _truncate(content, limit, "结论")

    payload: Dict[str, Any] = {
        "status": outcome.get("status", "ok"),
        "workflow": wf,
        "content": content,
        "truncated": truncated,
        "steps": rt["steps"].items[:int(guard["trace_cap"])],
        "usage": rt["steps"].usage,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }
    if outcome.get("error"):
        payload["error"] = outcome["error"]
    if wf in ("adversarial_verification", "loop_until_done"):
        payload["verdict"] = rt["verdict"]
        payload["verdict_parsed"] = rt["verdict_parsed"]

    logger.info("[run_workflow] 结束: workflow=%s, status=%s, steps=%d, "
                "elapsed=%.1fs", wf, payload["status"], len(payload["steps"]),
                payload["elapsed_seconds"])
    return tool_result(payload)


registry.register(
    name="run_workflow",
    toolset="workflow",
    schema=RUN_WORKFLOW_SCHEMA,
    handler=_handle_run_workflow,
    is_async=True,
    description=RUN_WORKFLOW_SCHEMA["description"],
    emoji="⚙️",
    max_result_size_chars=10000,
)
