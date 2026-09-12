"""Install-booking app-local stages — the booking-time hard guard on top of
the builtin FSM unified stage.

``InstallBookingUnifiedNLU`` (stage code ``install_unified``) subclasses
``FSMUnifiedNLU`` and post-processes its single-call output with deterministic,
zero-LLM slot arithmetic (slots.py):

1. the model picks install_specific_date / install_nearest with a visit time
   → extract the requested time from the time-augmented query annotation,
   match it against task_info["available_slots"];
     - bookable → annotate the slot (bookable / matched_slot) and let the
       transition proceed;
     - NOT bookable → reroute: next_node forced to install_recommend with a
       schedule-backed reply. Same spirit as the kernel's next_node hard
       guard: the model's illegal pick never reaches the node graph — the
       customer is never promised an unbookable time;
2. ANY transition into install_recommend (guarded reroute or the model's own
   都不知道 pick) gets its reply deterministically rewritten from the
   schedule (InstallRecommendNLG) — the recommendation the customer hears is
   always the real available_slots, never a model re-roll. This rewrite MUST
   live in the unified stage: FSM node transitions happen end-of-turn, so a
   node-level nlg on install_recommend would only fire on the NEXT turn
   (after the transition) and would clobber that turn's confirmation reply;
3. install_ask_callback (下次联系时间) answers are NOT visit times — the
   guard skips them (a "周二下午再打给我" callback time is none of the
   installer's business).

``InstallRecommendNLG`` (code ``install_recommend_nlg``) is the deterministic
zero-LLM NLG behind rule 2 — a plain class invoked by the unified stage (and
registered as a stage code so it stays independently mountable, e.g. as a
node-level nlg override elsewhere).
"""

import logging
from datetime import datetime
from typing import Any, Dict

from atoms.stages.clarify import ClarifyStage
from atoms.stages.recaller import MultiPathRecaller, WeightedScoreFusion
from atoms.stages.unified import FSMUnifiedNLU
from nexus.context import DialogueContext, fill_prompt_template
from nexus.registry.plugins import registry as plugin_registry

from apps.install_booking_agent.faq import match_faq
from apps.install_booking_agent.slots import (
    extract_requested_time,
    match_slot,
    parse_available_slots,
    suggest_slots,
)

logger = logging.getLogger(__name__)

# Node-code wiring constants now live as CLASS attributes on
# InstallBookingUnifiedNLU (BOOKING_TARGETS / RECOMMEND_NODE / CALLBACK_NODE /
# CALLBACK_DEFAULT_NODE / END_NODE) so scenario variants (e.g. the repair
# pattern) subclass the guard machinery and rebind only the codes + wording.


class InstallRecommendNLG:
    """Schedule-backed recommendation NLG (deterministic, zero LLM).

    Standalone-executable (execute(ctx) -> ctx); invoked by
    InstallBookingUnifiedNLU on every transition into install_recommend.
    No schedule injected → no-op (keeps whatever reply the unified stage
    wrote; the model's phrasing is the only option left).

    Scenario variants subclass and rebind the wording class attributes
    (RECOMMEND_LEAD / UNBOOKABLE_LEAD / RECOMMEND_TAIL).
    """

    stage_name = "install_recommend_nlg"

    # Wording pieces (subclass-rebindable)
    UNBOOKABLE_LEAD = "师傅档期排不开了，"
    RECOMMEND_LEAD = "最近可以约 "
    RECOMMEND_TAIL = "，您看哪个时间段合适？"

    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        task_info = ctx.task_basic_info or ctx.metadata.get("task_info") or {}
        available = parse_available_slots(task_info)
        if not available:
            return ctx  # no schedule: keep the unified reply as-is

        nlu_slots = (ctx.nlu_result or {}).get("slots", {})
        prefix = ""
        if nlu_slots.get("bookable") is False:
            prefix = (
                f"您说的 {nlu_slots.get('requested_time', '这个时间')} "
                f"{self.UNBOOKABLE_LEAD}"
            )
        ctx.nlg_result = {
            "content": (
                f"{prefix}{self.RECOMMEND_LEAD}{suggest_slots(available)}"
                f"{self.RECOMMEND_TAIL}"
            ),
            "deterministic": True,
        }
        return ctx


class InstallBookingUnifiedNLU(FSMUnifiedNLU):
    """FSM unified stage + booking-time hard guard + callback-time close
    (see module docstring).

    Scenario variants (e.g. the repair pattern) subclass this class and
    rebind the node-code class attributes plus the wording pieces — the
    guard machinery itself is node-graph agnostic.
    """

    stage_name = "install_unified"

    # Nodes whose next_node means "a visit time was given / the nearest slot
    # was asked" — the booking-time guard applies exactly on these transitions
    BOOKING_TARGETS = {"install_specific_date", "install_nearest"}

    # The recommend node: transitions into it get the deterministic reply
    RECOMMEND_NODE = "install_recommend"

    # The callback node: its times are next-contact times, not visit times
    CALLBACK_NODE = "install_ask_callback"

    # The default-callback node: unusable times reroute here (3 days later)
    CALLBACK_DEFAULT_NODE = "install_callback_default"

    # The end node: the callback close fires on callback → end transitions
    END_NODE = "install_end"

    # Default callback delay when the customer's answer is unusable (too far
    # beyond 2 weeks / in the past / vague / not given)
    CALLBACK_DEFAULT_DAYS = 3

    # Wording pieces for the deterministic (zero-LLM) replies
    UNBOOKABLE_PREFIX = "师傅档期排不开了，"
    CALLBACK_CLOSE_TEXT = "感谢您的接听，祝您生活愉快，再见！"
    CALLBACK_DEFAULT_PROPOSAL = "您说的时间有点远呢，那我们先约 {day} 左右再给您来电话确认，您看可以吗？"

    def __init__(self):
        super().__init__()
        self._recommend_nlg = InstallRecommendNLG()

    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        await super().execute(ctx)  # the builtin single call (reply/next_node/slots)
        self._apply_booking_guard(ctx)
        await self._apply_recommend_rewrite(ctx)
        self._apply_callback_close(ctx)
        return ctx

    # ------------------------------------------------------------------
    # Booking-time guard — deterministic post-processing, zero extra LLM
    # ------------------------------------------------------------------

    def _apply_booking_guard(self, ctx: DialogueContext) -> None:
        nlu_result = ctx.nlu_result or {}
        next_node = nlu_result.get("next_node", "")
        slots_out = dict(nlu_result.get("slots") or {})

        # 1. Not a booking transition (or a callback time) → untouched
        if next_node not in self.BOOKING_TARGETS:
            return
        if ctx.current_node_code == self.CALLBACK_NODE:
            return

        task_info = ctx.task_basic_info or ctx.metadata.get("task_info") or {}
        available = parse_available_slots(task_info)
        if not available:
            # No schedule injected: nothing to enforce, let the model's pick
            # through (declared wiring: guard is opt-in via task_info)
            slots_out["bookable"] = "no_schedule"
            self._write_back(ctx, slots_out)
            return

        requested = self._requested_slot(ctx)
        if requested is None:
            # No time entity in the utterance: not guardable, model's pick
            # stands (e.g. "就要最近的" — install_nearest picks the schedule's
            # head by construction)
            self._write_back(ctx, slots_out)
            return

        _rs, _re, display = requested
        matched = match_slot(requested, available)
        if matched is not None:
            slots_out["bookable"] = True
            slots_out["matched_slot"] = matched
            slots_out["requested_time"] = display
            self._write_back(ctx, slots_out)
            return

        # 2. NOT bookable → deterministic reroute to the recommend node
        # (reply rewritten by _apply_recommend_rewrite below)
        slots_out["bookable"] = False
        slots_out["requested_time"] = display
        ctx.nlu_result = {
            "next_node": self.RECOMMEND_NODE,
            "slots": slots_out,
        }
        meta = dict(ctx.metadata.get("unified") or {})
        meta["booking_guard"] = {
            "requested": display,
            "bookable": False,
            "rerouted_to": self.RECOMMEND_NODE,
        }
        ctx.metadata["unified"] = meta
        logger.info(
            "[install_booking] 可约时间守卫改道: 请求 %s 不可约 → 推荐 %s",
            display, suggest_slots(available),
        )

    async def _apply_recommend_rewrite(self, ctx: DialogueContext) -> None:
        """Deterministic recommend reply on EVERY transition into the
        recommend node (guarded reroute or the model's own pick)."""
        next_node = (ctx.nlu_result or {}).get("next_node", "")
        if next_node == self.RECOMMEND_NODE:
            await self._recommend_nlg.execute(ctx)

    def _requested_slot(self, ctx: DialogueContext):
        """The customer's requested time from the time-augmented query.

        Preference: the rewritten query's annotations (absolute times); a
        time phrase the model echoed into slots (visit_date/visit_hour)
        without any annotation falls back to raw matching.
        """
        from datetime import datetime

        rewritten = (ctx.rewritten_queries or [""])[0] or ""
        today = datetime.now().strftime("%Y-%m-%d")
        time_base = ctx.metadata.get("time_base")
        if time_base:
            today = datetime.fromtimestamp(time_base).strftime("%Y-%m-%d")
        slot = extract_requested_time(rewritten, today)
        if slot is not None:
            return slot
        echoed = " ".join(
            str((ctx.nlu_result or {}).get("slots", {}).get(k) or "")
            for k in ("visit_date", "visit_hour", "visit_time")
        )
        if echoed.strip():
            return extract_requested_time(echoed, today)
        return None

    def _write_back(self, ctx: DialogueContext, slots_out: Dict[str, Any]) -> None:
        """Rewrite nlu_result.slots with the guard's annotations."""
        nlu_result = dict(ctx.nlu_result or {})
        nlu_result["slots"] = slots_out
        ctx.nlu_result = nlu_result

    # ------------------------------------------------------------------
    # Callback-time close — next-contact time triage, deterministic (zero LLM)
    # ------------------------------------------------------------------
    # The customer answered install_ask_callback with a next-CONTACT time.
    # Branches (aligned with the time_augment 2-week annotation window, and
    # carried by the node graph — ask_callback's sub_nodes):
    #   annotated time in the rewritten query  → a valid future time within
    #     2 weeks: keep the transition to install_end and restate the
    #     customer's time in the goodbye (branch ②, one beat);
    #   no annotation                          → too far (beyond 2 weeks) /
    #     in the past / vague ("都行") / not given: REROUTE to
    #     install_callback_default (branch ①③) — the reply proposes the
    #     default 3-days-later callback and asks; the customer's answer on
    #     that node closes the call (two beats, same shape as the decline
    #     channel). The time_aug_query slot only annotates future times
    #     ending within the 2-week window, so "annotated vs not" IS the
    #     branch decision — no second parsing layer needed.

    def _apply_callback_close(self, ctx: DialogueContext) -> None:
        if ctx.current_node_code != self.CALLBACK_NODE:
            # On the default-callback node: the customer answered the
            # proposal — close with the default time restated (the slots
            # were recorded on the rerouting turn)
            self._close_default_callback(ctx)
            return
        next_node = (ctx.nlu_result or {}).get("next_node", "")
        if next_node != self.END_NODE:
            return  # not closing yet (e.g. clarify signal): untouched

        from datetime import timedelta

        now = self._now_datetime(ctx)
        annotated = extract_requested_time(
            (ctx.rewritten_queries or [""])[0] or "",
            now.strftime("%Y-%m-%d"))
        slots_out = dict((ctx.nlu_result or {}).get("slots") or {})

        if annotated is not None:
            # Branch ②: valid customer time — close on it directly
            _s, _e, display = annotated
            slots_out["callback_time"] = display
            slots_out["callback_source"] = "customer"
            ctx.nlg_result = {"content": (
                f"好的，那我们就 {display} 再联系您～"
                f"{self.CALLBACK_CLOSE_TEXT}")}
            self._write_back(ctx, slots_out)
            return

        # Branch ①③: unusable time — reroute to the default-callback node
        # with the deterministic proposal (zero extra LLM)
        default_day = (now + timedelta(
            days=self.CALLBACK_DEFAULT_DAYS)).strftime("%Y-%m-%d")
        slots_out["callback_time"] = default_day
        slots_out["callback_source"] = "default"
        ctx.nlu_result = {
            "next_node": self.CALLBACK_DEFAULT_NODE,
            "slots": slots_out,
        }
        ctx.nlg_result = {"content": self.CALLBACK_DEFAULT_PROPOSAL.format(
            day=default_day)}
        meta = dict(ctx.metadata.get("unified") or {})
        meta["callback_guard"] = {
            "requested": (ctx.rewritten_queries or [ctx.user_query])[0],
            "usable": False,
            "default_day": default_day,
            "rerouted_to": self.CALLBACK_DEFAULT_NODE,
        }
        ctx.metadata["unified"] = meta
        logger.info(
            "[install_booking] 联系时间不可用，改道默认改约三天: %s",
            default_day,
        )

    def _close_default_callback(self, ctx: DialogueContext) -> None:
        """On install_callback_default heading for end: restate the default
        callback time recorded on the rerouting turn (deterministic)."""
        if ctx.current_node_code != self.CALLBACK_DEFAULT_NODE:
            return
        next_node = (ctx.nlu_result or {}).get("next_node", "")
        if next_node != self.END_NODE:
            return
        default_day = ctx.filled_slots.get("callback_time")
        if default_day:
            ctx.nlg_result = {"content": (
                f"好的，那我们就 {default_day} 再联系您～"
                f"{self.CALLBACK_CLOSE_TEXT}")}

    @staticmethod
    def _now_datetime(ctx) -> "datetime":
        """The turn's time base (tests inject metadata.time_base)."""
        from datetime import datetime

        tb = ctx.metadata.get("time_base")
        if tb:
            return datetime.fromtimestamp(tb)
        return datetime.now()


# ============================================================================
# Phone-call-shaped clarify prompts (kb / fallback — no mixed zone in
# keyword gating)
# ============================================================================

INSTALL_CLARIFY_KB_PROMPT = """## 任务描述
你是一位家具/电器品牌的售后客服，正在与客户进行上门安装预约的电话沟通。
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

INSTALL_CLARIFY_FALLBACK_PROMPT = """## 任务描述
你是一位家具/电器品牌的售后客服，正在与客户进行上门安装预约的电话沟通。
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

INSTALL_CLARIFY_PROMPTS = {
    "kb": INSTALL_CLARIFY_KB_PROMPT,
    "fallback": INSTALL_CLARIFY_FALLBACK_PROMPT,
}


# ============================================================================
# Custom clarify stage — keyword-only business detection (keyword gating)
# ============================================================================

class KeywordClarifyStage(ClarifyStage):
    """Dual-track clarify with the recall layer replaced by pure keyword
    gating (业务检测只使用关键词来卡控).

    What changes vs the builtin ClarifyStage:

    - Detection: no MultiPathRecaller / ClarifyRouteRule — the assembled
      search text (user query + topic + keywords) goes through faq.match_faq
      (specific-first keyword containment). A hit ⇒ "kb" mode with the FAQ
      entry as the single recall item; a miss ⇒ "fallback" mode. The
      "mixed" ambiguous zone never fires (keyword matching is binary).
    - Generation: keeps the parent's LLM call but with phone-call-shaped
      templates (clarify prompts below) — the kb template gets the FAQ
      answer pre-filled ({__faq_answer__}), so the model only phrases the
      acknowledge + pull-back around a fixed fact, never re-answers.
    - Contract preserved: trigger protocol (next_node == "clarify" +
      topic/keywords slots), per-turn metadata["clarify"] reset/write, and
      the clarify-turn guard downstream (_fsm_node_transition skips node
      jumps & slot merging on triggered=True) all ride the parent behavior.
    """

    stage_name = "install_clarify"

    # The assembled search text also runs through the FAQ matcher (the
    # unify stage's clarify signal slots carry the topic/keywords the model
    # already extracted — they widen the hit surface)
    SEARCH_EXTRA_SLOTS = ("topic", "keywords")

    # Scenario variants (e.g. the repair pattern) subclass and rebind:
    # the FAQ keyword table, the kb/fallback prompt templates, and the
    # generate-failure fallback line (phone-call shaped per scenario)
    FAQ_MATCHER = staticmethod(match_faq)
    CLARIFY_PROMPTS = INSTALL_CLARIFY_PROMPTS
    CLARIFY_FALLBACK_REPLY = (
        "抱歉，这个问题我这边确认一下。咱们继续约安装时间"
        "好吗？您什么时间方便？"
    )

    def _keyword_route(self, ctx: DialogueContext, open_slots: Dict[str, Any]):
        """Keyword-only gating: returns (mode, recall_items).

        A FAQ hit becomes the single recall item carrying the entry's
        answer; a miss returns fallback with an empty list — the shapes the
        parent's prompt assembly already understands.
        """
        search_query = self._build_search_query(ctx, open_slots)
        entry = self.FAQ_MATCHER(search_query)
        if entry is None:
            return "fallback", [], search_query
        answer = str(entry["answer"])
        item = {
            "id": f"faq:{entry['topic']}",
            "content": answer,
            "score": 1.0,  # keyword hit is binary; score only for observability
            "metadata": {"keywords": list(entry["keywords"])},  # type: ignore[arg-type]
        }
        return "kb", [item], search_query

    def _fill_faq_answer(self, prompt: str, ctx: DialogueContext) -> str:
        """Substitute task_info fields into the FAQ answer's {field}
        placeholders ({product_name} etc.), then inject it into the prompt's
        {__recall_info__} slot (the kb template carries {__faq_answer__}
        instead — this fills it; plain replace, same as fill_prompt_template).
        """
        task_info = ctx.task_basic_info or ctx.metadata.get("task_info") or {}
        recall = (ctx.metadata.get("clarify") or {}).get("recall_results") or []
        answer = str(recall[0].get("content", "")) if recall else ""
        for key, value in task_info.items():
            answer = answer.replace("{" + str(key) + "}", str(value))
        return prompt.replace("{__faq_answer__}", answer)

    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        # 1. Per-turn reset (same contract as the parent)
        ctx.metadata["clarify"] = {"triggered": False}

        if not self._is_triggered(ctx):
            return ctx

        open_slots = self._extract_open_slots(ctx)

        # 2-3. Keyword-only detection (replaces recall + score gating)
        mode, recall_items, search_query = self._keyword_route(ctx, open_slots)
        logger.info(
            "澄清关键词卡控: session=%s, mode=%s, query=%r",
            ctx.session_id, mode, search_query,
        )

        # 4. Generate by mode (the only NLG call this turn) with the FAQ
        # answer pre-filled — write metadata first so _fill_faq_answer reads it
        ctx.metadata["clarify"] = {
            "triggered": True,
            "mode": mode,
            "recall_results": recall_items,
            "open_slots": open_slots,
            "query": search_query,
        }
        template = self.CLARIFY_PROMPTS.get(
            mode, self.CLARIFY_PROMPTS["fallback"])
        prompt = self._build_custom_prompt(ctx, mode, open_slots,
                                           recall_items, template)
        prompt = self._fill_faq_answer(prompt, ctx)
        try:
            content = (await self._generate(prompt, ctx.llm_config)).strip()
        except Exception as e:
            logger.warning("澄清生成异常，使用兜底话术: %s", e, exc_info=True)
            content = self.CLARIFY_FALLBACK_REPLY
        ctx.nlg_result = {"content": content}
        return ctx

    def _build_custom_prompt(self, ctx: DialogueContext, mode: str,
                             open_slots: Dict[str, Any],
                             recall_items: list, template: str) -> str:
        """Assemble the custom template with the parent's slot vocabulary."""
        slots = {
            "query": ctx.user_query,
            "topic": open_slots["topic"] or "（无）",
            "keywords": "、".join(open_slots["keywords"]) or "（无）",
            "recall_info": self._format_recall_for_prompt(recall_items),
            "cur_node": ctx.format_cur_node(stage="nlg"),
            "history": ctx.format_history(),
            "task_info": ctx.format_task_info(),
        }
        return fill_prompt_template(template, slots)


# ============================================================================
# Plugin registration (kind="stage") — string codes referenced by the
# pattern stages skeleton / node.stages declarations in route.py;
# install_recommend_nlg stays independently mountable
# ============================================================================

plugin_registry.register("stage", "install_unified", InstallBookingUnifiedNLU)
plugin_registry.register("stage", "install_recommend_nlg", InstallRecommendNLG)


def _keyword_clarify_factory():
    """Build the keyword-gated clarify stage.

    The parent constructor demands a recaller (the builtin recall layer);
    keyword gating never invokes it, so a bare MultiPathRecaller with no
    paths is passed as a placeholder — a no-op even if accidentally run.
    """
    return KeywordClarifyStage(recaller=MultiPathRecaller(
        recall_paths=[], filters=[], fusion=WeightedScoreFusion()))


plugin_registry.register("stage", "install_clarify", _keyword_clarify_factory)
