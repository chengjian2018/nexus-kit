"""note_polish pattern — the minimal AGENT app with a review loop.

Four stations (one NodeExecutor each, plugin code = node code):

    np_draft ──> np_review ──┬─ pass / max_rounds / stale ──> np_deliver (is_end)
     entry: init state,       │        ↑        ↑
     one LLM call writes      └──> np_revise ──┘
     the draft memo                 prompt = request + memo + FULL critique
                                    history + revision log (the experience
                                    inheritance), one LLM call, executor
                                    writes the revised memo

Design notes (the teaching points):

- The review/revise pair is a design→verify→fix→verify loop: one round
  costs 2 graph steps, `max_steps=10` leaves headroom for 3 rounds, and a
  DETERMINISTIC convergence gate inside np_review (never model-judged)
  stops earlier — pass, budget, or stale (two rounds without the top issue
  changing → honest exit, mirroring archify's `_trailing_stale`).
- Every station but np_deliver returns content="" — the graph reply is the
  LAST non-empty content along the run (np_deliver's report).
- No tools: file placement is deterministic executor code (absolute paths
  pinned on the state board), so allow_toolset stays empty — deny-by-default
  in its cleanest form.
- Inter-station state travels via cxt.graph_state["np_state"] (cleared at
  graph termination by the runtime); final summary goes to the reply only.

Registration: module-level registry.register(pattern); host/main.py's AST
scan discovers this file once it lives under apps/. The bottom import
closes the executor binding loop (same convention as archify).
"""

from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

from apps.note_polish_agent.prompts import NOTE_POLISH_BASE_PROMPT

np_draft = BaseNode(
    code="np_draft",
    name="笔记·初稿",
    description="入口站：初始化运行状态板，一次 LLM 调用产出结构化备忘初稿，"
                "由执行器确定性地落盘（模型出内容，代码定位置）",
    task_description="把用户的粗糙笔记整理成结构化备忘初稿",
    sub_nodes=["np_review"],
    plugins={"loop": "np_draft"},
    base_prompt=NOTE_POLISH_BASE_PROMPT,
)

np_review = BaseNode(
    code="np_review",
    name="笔记·评审闸门",
    description="读当前备忘，一次 LLM 调用按 JSON 协议给出评审"
                "（verdict=pass|fail + issues）；通过/超轮数/连续无新问题"
                "三条出口均为确定性判定（收敛闸门绝不交给模型决定）",
    task_description="评审当前备忘，判定继续修订或收敛收尾",
    sub_nodes=["np_revise", "np_deliver"],
    plugins={"loop": "np_review"},
    base_prompt=NOTE_POLISH_BASE_PROMPT,
)

np_revise = BaseNode(
    code="np_revise",
    name="笔记·修订",
    description="继承全部历史：原始请求 + 当前备忘 + 历轮评审"
                "（critique_log）+ 已做修订记录（revision_log），一次 LLM "
                "调用产出修订稿；执行器落盘并追记修订摘要——没有这两份"
                "历史，第 3 轮会重演第 1 轮已失败的修改",
    task_description="按评审意见修订备忘，携带全部历史经验",
    sub_nodes=["np_review"],
    plugins={"loop": "np_revise"},
    base_prompt=NOTE_POLISH_BASE_PROMPT,
)

np_deliver = BaseNode(
    code="np_deliver",
    name="笔记·交付",
    description="终点站：确定性组装（无 LLM）——最终备忘 + 诚实的过程摘要"
                "（轮数、收敛原因 pass/max_rounds/stale），未收敛时如实说明",
    task_description="交付最终备忘与过程摘要",
    sub_nodes=[],
    is_end=True,
    plugins={"loop": "np_deliver"},
    base_prompt=NOTE_POLISH_BASE_PROMPT,
)

note_polish_pattern = Pattern(
    code="note_polish",
    name="笔记打磨助手",
    description=(
        "四站 AGENT 图：初稿→评审⇄修订循环→交付；评审站内确定性收敛闸门"
        "（通过/轮数上限/连续无新问题诚实退出），修订站继承全量评审与修订"
        "历史；状态走 graph_state['np_state']，文件落 data/note_polish/"
        "<session>/"
    ),
    pattern_type="agent",
    entry_node_code="np_draft",
    nodes=[np_draft, np_review, np_revise, np_deliver],
    # One review+revise round = 2 steps; 3 rounds + draft + deliver = 8,
    # 10 leaves headroom (the semantic gate usually stops earlier).
    config={"max_steps": 10},
)

registry.register(note_polish_pattern)

# The four station executors self-register at the bottom of
# apps/note_polish_agent/executor.py (plugin code = node code); this import
# closes the binding loop (the same convention as archify / deep_research).
import apps.note_polish_agent.executor  # noqa: E402,F401
