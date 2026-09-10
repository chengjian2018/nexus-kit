"""Repair-booking app-local stages — subclass reuse of the install app's
guard machinery, rebound to the repair node graph and wording.

The booking-time hard guard / schedule-backed recommend rewrite /
callback-time triage / keyword-gated clarify are node-graph-agnostic
mechanisms (see apps/install_booking_agent/stages.py). This module only
declares what differs:

- ``RepairBookingUnifiedNLU`` (stage code ``repair_unified``): rebinds the
  node-code class attributes (BOOKING_TARGETS → repair_specific_date /
  repair_nearest, RECOMMEND_NODE → repair_recommend, ...) and the
  deterministic reply wording (师傅 → 维修师傅, 约安装 → 约维修);
- ``RepairRecommendNLG`` (code ``repair_recommend_nlg``): the recommend
  reply speaks the 维修档期;
- ``RepairKeywordClarifyStage`` (code ``repair_clarify``): rebinds the FAQ
  keyword table (repair question families), the kb/fallback prompt
  templates and the generate-failure fallback line.

No mechanism is re-implemented — everything else rides the install
classes' execute paths (apps-layer cross-package import, importlinter
layers contract unaffected).
"""

import logging

from atoms.stages.recaller import MultiPathRecaller, WeightedScoreFusion
from nexus.registry.plugins import registry as plugin_registry

from apps.install_booking_agent.stages import (
    InstallBookingUnifiedNLU,
    InstallRecommendNLG,
    KeywordClarifyStage,
)
from apps.repair_booking_agent.faq import match_faq

logger = logging.getLogger(__name__)


# ============================================================================
# Unified stage — the install guard machinery rebound to repair node codes
# ============================================================================

class RepairBookingUnifiedNLU(InstallBookingUnifiedNLU):
    """Install booking guard + schedule rewrite + callback triage, on the
    repair node graph (see module docstring)."""

    stage_name = "repair_unified"

    # Rebound node codes (the repair FSM's sketch)
    BOOKING_TARGETS = {"repair_specific_date", "repair_nearest"}
    RECOMMEND_NODE = "repair_recommend"
    CALLBACK_NODE = "repair_ask_callback"
    CALLBACK_DEFAULT_NODE = "repair_callback_default"
    END_NODE = "repair_end"

    # Repair wording for the deterministic replies
    UNBOOKABLE_PREFIX = "维修师傅档期排不开了，"
    CALLBACK_CLOSE_TEXT = "感谢您的接听，祝您生活愉快，再见！"
    CALLBACK_DEFAULT_PROPOSAL = (
        "您说的时间有点远呢，那我们先约 {day} 左右再给您来电话确认，"
        "您看可以吗？"
    )

    def __init__(self):
        super().__init__()
        # The recommend rewrite rides the repair-flavored NLG below
        self._recommend_nlg = RepairRecommendNLG()


class RepairRecommendNLG(InstallRecommendNLG):
    """Schedule-backed recommendation NLG for the repair scenario
    (deterministic, zero LLM)."""

    stage_name = "repair_recommend_nlg"

    UNBOOKABLE_LEAD = "维修师傅档期排不开了，"
    RECOMMEND_LEAD = "最近可以约 "
    RECOMMEND_TAIL = "，您看哪个时间段合适？"


# ============================================================================
# Custom clarify stage — repair FAQ table + phone-call-shaped prompts
# ============================================================================

REPAIR_CLARIFY_KB_PROMPT = """## 任务描述
你是一位家具/电器品牌的售后客服，正在与客户进行上门维修预约的电话沟通。
客户在流程中问了一个业务问题，关键词卡控已命中常见问题答案（【FAQ 答案】）。
请先用一两句话把 FAQ 答案口语化地讲给客户，然后把对话拉回预约主线，重新询问当前节点待办的问题。

## 人设描述
亲切利落的电话客服：先把客户的疑问答清楚，再自然地继续推进预约。
每轮回复两句话以内（电话节奏），不编造 FAQ 答案之外的信息。

## 输入内容
### 客户问题
{__query__}

### 问题主题
{__topic__}

### 问题关键词
{__keywords__}

### FAQ 答案（唯一事实源，禁止编造补充）
{__faq_answer__}

### 当前节点信息（预约主线）
{__cur_node__}

### 对话历史
{__history__}

### 任务信息
{__task_info__}

## 输出内容
以纯文本格式输出，直接念给客户的回复话术。
要求：
1. 第一句基于 FAQ 答案回应客户问题，不添加 FAQ 之外的承诺。
2. 第二句拉回预约主线，重新询问当前节点待办的问题（一次只问一件事）。
"""

REPAIR_CLARIFY_FALLBACK_PROMPT = """## 任务描述
你是一位家具/电器品牌的售后客服，正在与客户进行上门维修预约的电话沟通。
客户在流程中问了一个与预约无关、关键词卡控未命中的问题。
请先礼貌承接并诚实告知这个问题稍后核实，然后把对话拉回预约主线，重新询问当前节点待办的问题。

## 人设描述
亲切利落的电话客服：不冷落客户的问题，也不编造答案，尽快温和地回到预约主线。
每轮回复两句话以内（电话节奏）。

## 输入内容
### 客户问题
{__query__}

### 问题主题
{__topic__}

### 问题关键词
{__keywords__}

### 当前节点信息（预约主线）
{__cur_node__}

### 对话历史
{__history__}

### 任务信息
{__task_info__}

## 输出内容
以纯文本格式输出，直接念给客户的回复话术。
要求：
1. 第一句简短承接客户的问题并诚实告知稍后核实，不要编造答案。
2. 第二句拉回预约主线，重新询问当前节点待办的问题（一次只问一件事）。
"""

REPAIR_CLARIFY_PROMPTS = {
    "kb": REPAIR_CLARIFY_KB_PROMPT,
    "fallback": REPAIR_CLARIFY_FALLBACK_PROMPT,
}


class RepairKeywordClarifyStage(KeywordClarifyStage):
    """The install keyword-gated clarify, rebound to the repair FAQ table
    and prompts."""

    stage_name = "repair_clarify"

    FAQ_MATCHER = staticmethod(match_faq)
    CLARIFY_PROMPTS = REPAIR_CLARIFY_PROMPTS
    CLARIFY_FALLBACK_REPLY = (
        "抱歉，这个问题我这边确认一下。咱们继续约维修时间"
        "好吗？您什么时间方便？"
    )


# ============================================================================
# Plugin registration (kind="stage") — string codes referenced by the
# module stages declaration in route.py
# ============================================================================

plugin_registry.register("stage", "repair_unified", RepairBookingUnifiedNLU)
plugin_registry.register("stage", "repair_recommend_nlg", RepairRecommendNLG)


def _repair_keyword_clarify_factory():
    """Build the keyword-gated clarify stage (same placeholder recaller
    contract as the install factory — keyword gating never invokes it)."""
    return RepairKeywordClarifyStage(recaller=MultiPathRecaller(
        recall_paths=[], filters=[], fusion=WeightedScoreFusion()))


plugin_registry.register("stage", "repair_clarify",
                         _repair_keyword_clarify_factory)
