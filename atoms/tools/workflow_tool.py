"""run_workflow — multi-sub-agent workflows over six topologies (toolset:
workflow).

One tool call = one multi-sub-agent workflow orchestrated by a predefined
topology: the main agent plans (passing subtasks / categories / angles /
criteria explicitly), this tool executes deterministically; each "leaf"
step is a delegate-style sub ReAct loop (engine in
atoms/tools/_subagent_core.py) and "judge" steps (classifier / verifier /
tournament referee / done-checker / synthesizer) are tool-less single LLM
calls (temperature=0.0).

Division of labor with the existing mechanisms (docstring boundary
statement)::

    statically known topology  → graph orchestration (multi-node pattern /
                                 TurnResult.sends fanout)
    decided only at runtime    → this tool (LLM tool-call driven ad-hoc
                                 orchestration inside a node)
    one subtask                → delegate_task

Authorization (deny-by-default three-layer gate: registered toolset →
pattern.allow_toolset → node.use_tools)::

    pattern:
      allow_toolset: [workflow, knowledge]   # leaf pool = knowledge toolset
    node:
      use_tools: [run_workflow]

The leaf tool pool = the tools under each ``pattern.allow_toolset``
toolset, minus the ``subagent`` and ``workflow`` toolsets (structural
anti-recursion, depth always 1); the ``in_workflow`` flag is the second
line of defense. llm_config is injected via ``nexus.engine.tool_context``
(same model as the parent node); direct dispatch outside the agent loop
falls back to ``get_llm_config()`` with tool-less leaves.

Judge fault tolerance (Q8a): a verdict JSON parse failure or an illegal
bool/winner field type (the model passing ``"pass": "false"`` as a string
etc.) → one repair retry with an error hint → still failing takes the
conservative default and flags ``verdict_parsed: false``. A parse failure
takes the original conservative default (adversarial judges pass, loop
judges not-done, tournament winner A); an illegal field fails closed
(pass/done always false, tournament still winner A, never mapped to B),
and the payload carries a ``note``. LLM exceptions in
synthesize/add/filter stages degrade in place: direct concatenation /
passthrough superset (status=partial, with a note) — completed steps are
never dropped.

Guardrails (tunable via the config ``workflow_tool`` section): whole-run
timeout default 300s (args may only lower it), leaf round cap 8, parallel
width ≤8, adversarial/loop iteration caps 3/5, final-conclusion truncation
at 8000 chars, steps cap 32. In parallel stages a single failed leaf is
marked failed while siblings continue; total wipeout → status=error;
whole-run timeout → completed steps still come back (status=timeout);
adversarial/loop iteration exhaustion → status=partial.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

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

# The leaf pool excludes both orchestration toolsets (structural
# anti-recursion; the in_workflow flag is the second line of defense)
_SELF_TOOLSETS = frozenset({"subagent", "workflow"})
# Per-piece truncation for material fed to judge/synthesis calls (keeps a
# judge prompt from exploding)
_PER_SYNTH_CHARS = 3000

# ---------------------------------------------------------------------------
# Judge system prompts
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
# Schema (the workflow enum's description fully explains the six topologies
# and how to choose)
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
# Judge / synthesis calls and parsing
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


def _strict_bool(value: Any) -> Optional[bool]:
    """Strict boolean coercion: only a real bool (and int 0/1) passes;
    everything else is None — a model-provided JSON must never be truthy-
    coerced (the string "false" is truthy, letting adversarial content slip
    through and loop lie about completion)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _norm_winner(value: Any) -> Optional[str]:
    """Winner normalization: only "A"/"B" pass (strip + case-insensitive);
    anything else (None / "NONE" / "候选A") is None — an illegal value must
    never map to B."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    return normalized if normalized in ("A", "B") else None


async def _llm_once(rt: Dict[str, Any], system_prompt: str,
                    user_prompt: str) -> Tuple[str, Dict[str, Any]]:
    """One plain LLM call (judges / synthesize / add / filter), no tools."""
    result = await rt["provider"].achat_completion(
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_prompt}],
        model=rt["model"], temperature=0.0, max_tokens=rt["max_tokens"])
    return result.get("content", "") or "", result.get("usage") or {}


async def _judge(rt: Dict[str, Any], system_prompt: str,
                 user_prompt: str, validate: Optional[
                     Callable[[dict], Optional[str]]] = None,
                 ) -> Tuple[Optional[dict], bool, Dict[str, int], str]:
    """Judge call: JSON verdict with one repair retry.

    ``validate(verdict)`` does field-level strict typing (bool / winner);
    a failure goes through the SAME repair retry as unparseable JSON.
    Returns (verdict, parsed, usage, note). Unparseable twice (or provider
    errors twice) → (None, False, usage, "") — the caller applies its
    conservative default and marks verdict_parsed=False. A non-empty note
    means the JSON parsed but a field was invalid: the caller fails closed
    for that field (never truthy-coerced) and records a note.
    """
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}]
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    note = ""
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
        field_err = validate(verdict) if (verdict is not None
                                          and validate is not None) else None
        if verdict is not None and field_err is None:
            return verdict, True, usage, ""
        if field_err is not None:
            note = field_err   # illegal field (sticky): final failure takes fail-closed semantics
            repair = (f"上一次输出的 JSON 字段类型无效：{field_err}。"
                      "请重新输出 JSON，对应字段使用要求的类型，"
                      "不要包含任何其他文字。")
        else:
            repair = ("上一次输出无法解析为 JSON 对象。请只输出一个 JSON 对象，"
                      "不要包含任何其他文字或代码块标记。")
        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": repair},
        ]
    return None, False, usage, note


def _validate_pass(verdict: dict) -> Optional[str]:
    """Verifier verdict validation: pass must be a strict boolean."""
    if _strict_bool(verdict.get("pass")) is None:
        return 'The "pass" field must be a boolean true/false'
    return None


def _validate_done(verdict: dict) -> Optional[str]:
    """Done-check verdict validation: done must be a strict boolean."""
    if _strict_bool(verdict.get("done")) is None:
        return 'The "done" field must be a boolean true/false'
    return None


def _validate_winner(verdict: dict) -> Optional[str]:
    """Referee verdict validation: winner accepts only "A"/"B"."""
    if _norm_winner(verdict.get("winner")) is None:
        return 'The "winner" field must be "A" or "B"'
    return None


async def _llm_stage(rt: Dict[str, Any], step: str, system_prompt: str,
                     user_prompt: str) -> Tuple[str, bool]:
    """synthesize/add/filter stage call: an exception degrades in place
    (step recorded failed) instead of bubbling to the whole-run error path
    and discarding completed work — the topology concatenates/passes
    through and flags partial. Returns (text, ok)."""
    try:
        content, usage = await _llm_once(rt, system_prompt, user_prompt)
    except Exception as e:
        logger.warning("[run_workflow] %s 阶段调用失败: %s", step, e)
        rt["steps"].add(step, "failed", rounds=1)
        return "", False
    rt["steps"].add(step, "ok", rounds=1, usage=usage)
    return content, True


# ---------------------------------------------------------------------------
# Step recording and leaf/candidate helpers
# ---------------------------------------------------------------------------

class _Steps:
    """Topology-level step recorder (truncated to trace_cap at payload assembly)."""

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


def _add_note(rt: Dict[str, Any], text: str) -> None:
    """Degradation / invalidity notes (deduped), merged into the payload's note field."""
    if text not in rt["notes"]:
        rt["notes"].append(text)


def _concat_ok(items: List[Dict[str, Any]]) -> str:
    """Concatenate the ok candidates (the degraded body when synthesize/add fails)."""
    return "\n\n".join(it["text"] for it in items
                       if it["ok"] and it["text"].strip())


# ---------------------------------------------------------------------------
# The six topologies
# ---------------------------------------------------------------------------

async def _wf_classify_and_act(rt, parsed) -> Dict[str, Any]:
    categories = parsed["_categories"]
    listing = "\n".join(
        f"- {c['name']}：{c['instruction']}" for c in categories)
    verdict, parsed_ok, usage, _ = await _judge(
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
    content, synth_ok = await _llm_stage(
        rt, "synthesize", SYNTHESIZE_SYSTEM_PROMPT,
        f"任务：{rt['task']}\n\n各子任务结果：\n{_render_candidates(items)}")
    if not synth_ok:
        # Degraded: synthesis failed → concatenate subtask results directly,
        # never dropping completed work
        rt["content"] = _concat_ok(items)
        _add_note(rt, "synthesize 阶段失败，已直接拼接子任务结果")
        return {"status": "partial"}
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
        verdict, parsed_ok, usage, note = await _judge(
            rt, system,
            f"任务：{rt['task']}\n\n待审产出：\n"
            f"{_truncate(draft, _PER_SYNTH_CHARS, '产出')}",
            validate=_validate_pass)
        rt["steps"].add(f"cycle#{cycle}:verify", "ok", rounds=1, usage=usage)
        rt["verdict_parsed"] = parsed_ok
        if not parsed_ok:
            if note:
                # Illegal field type: fail-closed (judged not-pass), never
                # waved through by truthy coercion
                rt["verdict"] = {"pass": False}
                _add_note(rt, f"审校 verdict 无效（{note}），按未通过兜底")
                issues_text = f"-（verdict 字段无效：{note}）"
                continue
            rt["verdict"] = {"pass": True}   # conservative default: pass — avoid a meaningless loop
            return {"status": "ok"}
        rt["verdict"] = verdict
        if _strict_bool(verdict.get("pass")):
            return {"status": "ok"}
        issues = verdict.get("issues") or []
        issues_text = ("\n".join(f"- {i}" for i in issues)
                       or "-（审校未列出具体问题）")
    return {"status": "partial"}   # rounds exhausted without passing


async def _wf_generate_add_filter(rt, parsed) -> Dict[str, Any]:
    items = await _gen_candidates(rt, parsed)
    if _all_failed(items):
        return {"status": "error", "error": "全部候选生成失败"}
    superset, add_ok = await _llm_stage(
        rt, "add", ADD_SYSTEM_PROMPT,
        f"任务：{rt['task']}\n\n各候选：\n{_render_candidates(items)}")
    add_degraded = not add_ok
    if add_degraded:
        # Degraded: accumulation failed → concatenate candidates as the
        # draft; the filter stage proceeds as usual
        superset = _concat_ok(items)
        _add_note(rt, "add 阶段失败，已直接拼接候选充当草案")
    criteria = parsed.get("criteria", "")
    final, filter_ok = await _llm_stage(
        rt, "filter", FILTER_SYSTEM_PROMPT.format(
            criteria=criteria or "去弱留强，保留最有价值、最相关的部分，控制篇幅"),
        f"任务：{rt['task']}\n\n超集草案：\n"
        f"{_truncate(superset, _PER_SYNTH_CHARS * 2, '草案')}")
    if not filter_ok:
        # Degraded: filtering failed → pass the superset through (unfiltered), candidates not dropped
        rt["content"] = superset
        _add_note(rt, "filter 阶段失败未过滤")
        return {"status": "partial"}
    rt["content"] = final
    degraded = add_degraded or any(not it["ok"] for it in items)
    return {"status": "partial" if degraded else "ok"}


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
            verdict, parsed_ok, usage, note = await _judge(
                rt, JUDGE_SYSTEM_PROMPT,
                f"任务：{rt['task']}\n\n{_render_candidates([a, b])}",
                validate=_validate_winner)
            rt["steps"].add(f"match:r{round_no}#{match_idx}", "ok",
                            rounds=1, usage=usage)
            winner_is_a = True   # conservative default: a parse failure keeps A
            if parsed_ok and _norm_winner(verdict.get("winner")) == "B":
                winner_is_a = False
            if not parsed_ok and note:
                # Illegal winner: degrade to the same fallback as a judge
                # failure (A wins), never mapped to B
                _add_note(rt, f"比赛 verdict 无效（{note}），按前者胜兜底")
            nxt.append(a if winner_is_a else b)
        if len(live) % 2:
            nxt.append(live[-1])   # odd count bye: the last entry advances directly
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
        verdict, parsed_ok, usage, note = await _judge(
            rt, DONE_SYSTEM_PROMPT,
            f"完成条件：\n{criteria}\n\n当前产出：\n"
            f"{_truncate(rt['content'], _PER_SYNTH_CHARS, '产出')}",
            validate=_validate_done)
        rt["steps"].add(f"iteration#{iteration}:check", "ok",
                        rounds=1, usage=usage)
        rt["verdict_parsed"] = parsed_ok
        if not parsed_ok:
            rt["verdict"] = {"done": False}   # conservative default: not done, keep iterating
            if note:
                # Illegal type: also fail-closed (judged not-done), with a note
                _add_note(rt, f"完成 verdict 无效（{note}），按未完成兜底")
                missing_text = f"-（verdict 字段无效：{note}）"
            continue
        rt["verdict"] = verdict
        if _strict_bool(verdict.get("done")):
            return {"status": "ok"}
        missing = verdict.get("missing") or []
        missing_text = ("\n".join(f"- {m}" for m in missing)
                        or "-（未列出具体缺口）")
    return {"status": "partial"}   # iterations exhausted without meeting the criteria


_TOPOLOGIES = {
    "classify_and_act": _wf_classify_and_act,
    "fanout_and_synthesize": _wf_fanout_and_synthesize,
    "adversarial_verification": _wf_adversarial_verification,
    "generate_add_filter": _wf_generate_add_filter,
    "tournament": _wf_tournament,
    "loop_until_done": _wf_loop_until_done,
}


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

def _opt_count(args: Dict[str, Any], key: str, default: int, cap: int
               ) -> Tuple[Optional[str], Optional[int]]:
    """Optional count arg: default when absent, must be ≥1, args may only narrow within cap."""
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
    """GAF/tournament candidate source: inputs (tournament only) > angles > the n-template angles."""
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

    guard = get_workflow_tool_config(
        ambient.pattern_code if ambient is not None else "")
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

    # Leaf tool pool: pattern-granted toolsets − orchestration toolsets (structural anti-recursion)
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
        "notes": [],
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
    if rt["notes"]:
        payload["note"] = "; ".join(rt["notes"])   # degradation / invalid-verdict notes
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
