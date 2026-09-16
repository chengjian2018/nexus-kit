"""ppt_generator_agent pattern — the ai-ppt-generator skill transcribed
into a declarative FSM pattern for AI PPT generation chats.

Business background: the user hands over a PPT topic; the assistant asks
whether they want to hand-pick a template style; "yes" shows the REAL
Baidu theme list and waits for a pick, "no" lets the deterministic keyword
categorizer match a style; then the Baidu AIPPT service generates the deck
(2-5 minutes) and the assistant delivers the download link.

Source skill: /ai-ppt-generator (SKILL.md "Smart Workflow"):
    1. User provides PPT topic
    2. Agent asks: "Want to choose a template style?"
    3. If yes → show styles → user picks (tpl_id) → generate with it
    4. If no  → smart auto selection by topic keywords → generate
    5. Wait for is_end:true, deliver data.ppt_url

Transcription mapping (skill step → node code → branches):

    collect the topic          ppt_start          topic given→ask / cancel→decline
    "want to choose a style?"  ppt_ask_template   yes→theme list / no/auto→generate / cancel→decline
    show styles, take the pick ppt_show_themes    pick→generate / re-ask→ask_template / cancel→decline
    generate + deliver the URL ppt_generate       thanks→end / switch template→theme list / regenerate→self / cancel→decline
    generic cancel             ppt_decline        → end (two-beat close)
    end                        ppt_end            is_end (terminal)

Where the work happens (the fsm one-beat-per-turn rule): ALL deterministic
work rides the app-local unified stage ``ppt_unified`` (stages.py) —
theme-pick resolution against the real cached list, the blocking Baidu
generation on every transition into ppt_generate (2-5 min, off the event
loop via asyncio.to_thread), and the theme-list rewrite on every
transition into ppt_show_themes. Node-level NLG would resolve one turn
late (the documented FSM timing trap) and is therefore not used anywhere.
The user never sees a fabricated tpl_id or URL: every id and link in a
reply comes from tools.py API data matched/formatted in code.

Wiring:

    pattern.stages      [{"nlu": "ppt_unified"}, {"nlg": "nlg_pass_through"}]
    node.stages         {} everywhere (no clarify track — off-flow input is
                        handled by the unified stage's empty next_node stay,
                        same deliberate simplification as install_booking)

Known deliberate simplifications:
    - No dual-track clarify: the source skill has no FAQ branch; off-topic
      turns stay on the current node and re-confirm.
    - Generation retries are same-template-capped (2 attempts, tracked in
      graph_state.failed_tpl_ids) instead of uncapped: the honest-exit
      reply lists what was tried and suggests switching templates.
    - The auto path degrades to the API-side random template when the
      theme list cannot be fetched (source skill's fallback logic).
    - web_content (reference web page material) is accepted by the API
      client but not yet wired to a collection node.

Registration: module-level ``registry.register(Pattern(...))``, auto-
discovered by AST scan (apps/ppt_generator_agent/route.py).
"""

import logging

from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry
from apps.ppt_generator_agent.prompts import PPT_UNIFIED_PROMPT

logger = logging.getLogger(__name__)


# ============================================================================
# Nodes — the skill workflow's steps in flow order
# (nodes[0] / entry_node_code is the entry)
# ============================================================================

ppt_start = BaseNode(
    code="ppt_start",
    name="开场收集主题",
    description=(
        "开场并收集本次要生成的 PPT 主题（一句话主题或内容描述），"
        "拿到主题后进入模板选择询问"
    ),
    task_description="收集 PPT 主题，确认后进入模板选择询问",
    slots={
        "ppt_topic": "用户要生成的 PPT 主题/内容描述",
    },
    sub_nodes=["ppt_ask_template", "ppt_decline"],
    answer_examples=[
        "好嘞～请把 PPT 的主题告诉我（比如「人工智能发展趋势报告」），"
        "我来帮你生成。",
        "收到！你想做一份什么主题的 PPT？一句话描述就行。",
    ],
    base_nlu_prompt=PPT_UNIFIED_PROMPT,
)

ppt_ask_template = BaseNode(
    code="ppt_ask_template",
    name="模板选择询问",
    description=(
        "拿着已收集的主题询问用户是否要自己挑选模板风格：要 → 展示真实"
        "模板列表；不要 → 系统按主题关键词智能匹配模板直接生成"
    ),
    task_description="询问是否自己挑选模板风格，分发到列表选择或自动匹配",
    slots={
        "want_choose": "是否要自己挑选模板风格（是/否）",
    },
    sub_nodes=["ppt_show_themes", "ppt_generate", "ppt_decline"],
    answer_examples=[
        "好嘞～那你想自己挑一个模板风格吗？我可以把可用的模板列给你选；"
        "也可以由我根据主题帮你智能匹配。",
        "要自己选模板风格吗？选一个喜欢的做出来更合心意，不想选的话我"
        "帮你自动匹配～",
    ],
    base_nlu_prompt=PPT_UNIFIED_PROMPT,
)

ppt_show_themes = BaseNode(
    code="ppt_show_themes",
    name="模板列表展示",
    description=(
        "展示系统拉取的真实模板列表（风格名 + 模板编号），等待用户按编号"
        "或风格名称选定；列表内容由 ppt_unified 守卫确定性注入，模型不"
        "参与编造。用户拿不定主意可返回上一歩改为自动匹配"
    ),
    task_description="展示真实模板列表并收集用户选定的模板编号/名称",
    slots={
        "tpl_id": "用户选定的模板编号",
        "style_name": "或用户选定的模板风格名称",
    },
    sub_nodes=["ppt_generate", "ppt_ask_template", "ppt_decline"],
    answer_examples=[
        "好的，列表已经发给你了，回复模板编号或风格名称就能选用～",
        "这些都是当前可用的模板，选一个回复我编号就行；拿不定主意也可以"
        "让我自动匹配。",
    ],
    base_nlu_prompt=PPT_UNIFIED_PROMPT,
)

ppt_generate = BaseNode(
    code="ppt_generate",
    name="PPT生成交付",
    description=(
        "生成与交付节点：转入本节点时由 ppt_unified 守卫执行真实生成"
        "（选定模板或按主题自动匹配，调用百度文库 AI，约 2-5 分钟）并把"
        "回复改写为生成结果（标题 + 下载链接）。落位后的后续分支：感谢/"
        "确认 → 结束；换个模板 → 回模板列表（自然循环）；重新生成 → "
        "自环重跑；取消 → 通用取消承接"
    ),
    task_description="执行 PPT 生成并交付下载链接，处理交付后的收尾分支",
    slots={
        "tpl_id": "本次生成使用的模板编号（自动匹配时由守卫回填）",
        "generation_status": "本次生成结果状态（success/failed）",
    },
    sub_nodes=["ppt_end", "ppt_show_themes", "ppt_generate", "ppt_decline"],
    answer_examples=[
        "PPT 已经交付啦～有什么要调整的随时说，比如换个模板重新生成。",
        "链接已发你，祝演示顺利！需要换模板或改主题再告诉我。",
    ],
    base_nlu_prompt=PPT_UNIFIED_PROMPT,
)

ppt_decline = BaseNode(
    code="ppt_decline",
    name="通用取消承接",
    description=(
        "通用退出通道：用户在流程任何阶段表示不想做了/取消/以后再说，"
        "按场景共情回应，然后转入结束语"
    ),
    task_description="识别取消意图并共情回应，转结束语",
    slots={
        "decline_reason": "取消原因（不想做了/稍后再说/其他）",
    },
    sub_nodes=["ppt_end"],
    answer_examples=[
        "好的，这次的 PPT 先不生成了，需要的时候随时来找我～",
        "没问题，那就先到这里，下次想做 PPT 直接把主题发给我就行。",
    ],
    base_nlu_prompt=PPT_UNIFIED_PROMPT,
)

ppt_end = BaseNode(
    code="ppt_end",
    name="结束语",
    description=(
        "收尾：交付完成、用户取消等所有终止路径的礼貌收尾（is_end 终"
        "节点，进入即结束会话）"
    ),
    task_description="礼貌收尾，结束会话",
    slots={},
    sub_nodes=[],
    is_end=True,
    answer_examples=[
        "感谢使用，祝演示顺利，再见～",
        "好的，那我们下次见，随时欢迎回来生成 PPT～",
    ],
    base_nlu_prompt=PPT_UNIFIED_PROMPT,
)


# ============================================================================
# Pattern registration — the whole workflow is one FSM pattern
# ============================================================================

ppt_generator_agent_pattern = Pattern(
    code="ppt_generator_agent",
    name="AI PPT 生成助手（skill 转写）",
    description=(
        "对话管理：FSM 统一阶段推进 AI PPT 生成——收集主题、询问模板"
        "选择意向、展示真实模板列表（确定性注入）、按选定或智能匹配的"
        "模板执行真实生成（确定性守卫承载 2-5 分钟调用）并交付下载"
        "链接；通用取消通道；模板编号与链接全部来自 API 数据，模型零"
        "编造"
    ),
    pattern_type="fsm",
    entry_node_code="ppt_start",
    nodes=[
        ppt_start,
        ppt_ask_template,
        ppt_show_themes,
        ppt_generate,
        ppt_decline,
        ppt_end,
    ],
    # Stages skeleton: the app-local guarded unified stage writes
    # reply/next_node/slots in one call (with the three deterministic
    # guards riding it); pass-through NLG keeps the reply (no second LLM
    # call). No time augmentation and no clarify track in this domain.
    stages=[
        {"nlu": "ppt_unified"},
        {"nlg": "nlg_pass_through"},
    ],
)

registry.register(ppt_generator_agent_pattern)


# ============================================================================
# App-local stage import — registers ppt_unified (kind="stage") at module
# level. Imported AFTER the pattern registration so the pattern object
# graph (whose stages declaration references the code) exists even if a
# stale registry is inspected mid-import; the code resolves at execution
# time, and validation (host startup / CLI) runs after this whole module
# has been imported.
# ============================================================================

import apps.ppt_generator_agent.stages  # noqa: E402,F401
