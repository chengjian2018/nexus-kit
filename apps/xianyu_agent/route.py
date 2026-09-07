"""
Xianyu seller customer service Route pattern — replicates tmp_xianyu.XianyuReplyBot's
agent dialogue management.

Replication source: dialogue/tmp_xianyu.py (XianyuReplyBot / IntentRouter / the three
domain Agents); prompt templates come from the XIANYU_* series in prompt.py.

Pattern structure (ROUTE pattern):

    xianyu_agent (Pattern, entry: xianyu_root, query=TimeAugQueryRewriter)
    └── xianyu_root (RouteModule)   all nodes stay in the routing module, no jump_module
        ├── xy_route_root      routing root node (sub_nodes = intent menu)
        ├── xy_menu_price      bargain menu (bargain round count below the cap)
        ├── xy_menu_price_refuse  bargain refusal menu (cap reached → fixed script, zero LLM)
        ├── xy_menu_tech       tech Q&A menu
        └── xy_menu_default    general customer service menu

Dialogue-management mapping (tmp_xianyu → this framework):
    IntentRouter three-tier routing (tech keywords/regex first → price keywords/regex → LLM fallback)
      → XianyuIntentNLU: detect_intent local rule layer (original keyword table merged with
        tmp_xianyu's word lists and regexes) + XIANYU_NLU_PROMPT LLM fallback (outputs the four
        classes price/tech/no_reply/default; invalid output falls back to default)
    ClassifyAgent classifies as no_reply (prompt flooding / unrelated to the item on sale)
      → FixedNLG outputs an empty reply; channel contract: empty reply = not sent
        (the original implementation returned "-" and let the plugin side filter it)
    PriceAgent dynamic temperature min(0.3 + 0.15×bargain round, 0.9), TechAgent 0.4,
    DefaultAgent 0.7, max_tokens 500
      → FixedNLG._tuned_llm_config rewrites a copy of llm_config per intent
    PriceAgent injects ▲current bargain round
      → NLU writes the bargain params into filled_slots; the NLG prompt appends the
        bargain-settings block
    _safe_filter blocked-word filtering (WeChat/QQ/Alipay/bank card/offline)
      → FixedNLG._safe_filter
    _extract_bargain_count (counts bargain rounds by tracing system messages)
      → NLU counts user messages with metadata.intent=price (the framework has no
        system-side bargain messages)
    Bargain round count reaching the cap → fixed refusal script, zero LLM (tmp_xianyu
    has no such mechanism and relied on the rising-temperature policy to softly hold
    the line; this pattern keeps the explicit threshold refusal so bargain behavior
    stays predictable and testable)
      → xy_menu_price_refuse + answer_examples text marker short-circuit

Known deliberate simplifications:
    - TechAgent's enable_search (DashScope extra_body) and top_p=0.8 are not passed
      through: the framework BaseNLG._call_llm signature is fixed; temperature/length
      are tuned via a llm_config copy

Message entry: channel/xianyu.py (default-reply API plugin decision port);
set XIANYU_CHANNEL_PATTERN=xianyu_agent to plug in.

Registration: module-level ``registry.register(Pattern(...))``, auto-discovered by AST scan.
"""

import logging
import re

from atoms.stages.nlg import BaseNLG
from atoms.stages.nlu import BaseNLU
from nexus.model.module import RouteModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from atoms.stages.query import TimeAugQueryRewriter
from nexus.registry.patterns import registry
from apps.xianyu_agent.prompts import (
    XIANYU_DEFAULT_NLG_PROMPT,
    XIANYU_NLU_PROMPT,
    XIANYU_PRICE_NLG_PROMPT,
    XIANYU_TECH_NLG_PROMPT,
)

logger = logging.getLogger(__name__)

# ============================================================================
# Local intent detection — replicates the IntentRouter rule layer (tech first)
# ============================================================================

# Tech keywords (original keyword table + tmp_xianyu IntentRouter tech word list)
TECH_KEYWORDS = [
    "怎么用", "参数", "坏了", "故障", "设置", "说明书",
    "功能", "用法", "教程", "驱动",
    "规格", "型号", "连接", "对比",
]

# Price keywords (original keyword table, including Xianyu-context haggle slang such
# as the "dao" price-cut term and free-shipping requests, + tmp_xianyu IntentRouter
# price word list)
PRICE_KEYWORDS = [
    "便宜", "优惠", "刀", "降价", "价格", "多少钱",
    "能少", "还能", "最低", "底价", "实诚价", "到100", "能到",
    "包个邮", "砍价", "价",
]

# tmp_xianyu IntentRouter regex tier (matched against the cleaned text)
TECH_PATTERNS = [r"和.+比"]
PRICE_PATTERNS = [r"\d+元", r"能少\d+"]

# Intent → menu node code (refuse is re-decided separately via the bargain round count;
# no_reply lands on the default menu, where NLG short-circuits to an empty reply by intent)
INTENT_TO_MENU = {
    "price": "xy_menu_price",
    "tech": "xy_menu_tech",
    "default": "xy_menu_default",
}

# Bargain defaults (replicates _get_default_settings; account-level config is injected
# via ctx.metadata["bargain_settings"]; this default applies when not injected)
DEFAULT_BARGAIN_SETTINGS = {
    "max_bargain_rounds": 3,
    "max_discount_percent": 10,
    "max_discount_amount": 100,
}


def detect_intent(message: str) -> str:
    """Local rule-based intent detection — replicates IntentRouter.detect's keyword/regex tiers.

    Purely local, zero LLM (the LLM fallback tier lives in XianyuIntentNLU, which
    needs ctx.llm_config). Tech first: when amounts and tech words co-occur, classify
    as tech (consistent with the classification standard of XIANYU_NLU_PROMPT).

    Args:
        message: buyer message

    Returns:
        intent: price / tech / default (default means no local hit; the upper layer falls back)
    """
    # Replicates IntentRouter: match after stripping emoji/punctuation
    # (\w keeps alphanumerics and underscore)
    text_clean = re.sub(r"[^\w一-龥]", "", message.lower())

    if any(kw in text_clean for kw in TECH_KEYWORDS):
        return "tech"
    if any(re.search(p, text_clean) for p in TECH_PATTERNS):
        return "tech"
    if any(kw in text_clean for kw in PRICE_KEYWORDS):
        return "price"
    if any(re.search(p, text_clean) for p in PRICE_PATTERNS):
        return "price"
    return "default"


def _effective_query(cxt) -> str:
    """Get the rewritten buyer message: the query slot (TimeAugQueryRewriter) runs
    earlier this turn before generate, so rewritten_queries[0] is the time-augmentation
    result; falls back to the original query when the slot is a no-op or unconfigured."""
    return (cxt.rewritten_queries or [cxt.user_query])[0]


def _get_bargain_settings(cxt) -> dict:
    """Get bargain settings: metadata injection takes priority, defaulting to DEFAULT_BARGAIN_SETTINGS."""
    settings = dict(DEFAULT_BARGAIN_SETTINGS)
    injected = cxt.metadata.get("bargain_settings") or {}
    settings.update({k: v for k, v in injected.items() if v is not None})
    return settings


def _count_bargain_rounds(cxt) -> int:
    """Count the bargain rounds that have already happened in the current session.

    Replicates the original "count user messages with intent=price in the chat
    history": each turn's NLU writes the intent into that turn's user message
    metadata; counting traces back through metadata (the current turn's user
    message is already stored and is included in the count).
    """
    count = 0
    for msg in cxt.history:
        if msg.role != "user":
            continue
        if (msg.metadata or {}).get("intent") == "price":
            count += 1
    return count


# ============================================================================
# NLU stage — local rule classification + LLM fallback (ClassifyAgent)
# ============================================================================

class XianyuIntentNLU(BaseNLU):
    """Xianyu intent classification stage — replicates the IntentRouter rule layer + ClassifyAgent fallback.

    Contract matches the framework NLU (execute(ctx) -> ctx, writes ctx.nlu_result);
    mounted at the nlu position of the RouteModule module-level generate dict
    (takes effect when the node has no override).

    Routing tiers (replicates IntentRouter.detect's three-tier strategy, tech first):
        1. Local tech keywords/regex (detect_intent, zero LLM)
        2. Local price keywords/regex (detect_intent, zero LLM)
        3. LLM fallback (XIANYU_NLU_PROMPT, four classes price/tech/no_reply/default;
           falls back to default on call failure or invalid output label)

    nlu_result structure (aligned with the framework NLU contract):
        {"next_node": <menu node code>, "slots": {...}, "intent": <raw intent>}

    slots replicate generate_reply's bargain parameter injection:
        bargain_count / max_bargain_rounds / max_discount_percent /
        max_discount_amount — carried into the NLG prompt via filled_slots
    """

    stage_name = "xianyu_intent_nlu"

    # Valid labels for LLM output (no_reply is the most specific, matched first)
    _VALID_INTENTS = ("no_reply", "price", "tech", "default")

    def _default_prompt_template(self) -> str:
        return XIANYU_NLU_PROMPT

    def prompt_build(self, cxt) -> str:
        """Build the LLM fallback classification prompt.

        XIANYU_NLU_PROMPT keeps only the {__task_info__}/{__history__} slots
        (BaseNLU's kwargs vocabulary lacks task_info, so it is assembled here); the
        buyer's current message replicates ClassifyAgent._build_messages as a
        standalone section, not relying on the last history line.
        """
        slots = {
            "task_info": cxt.format_task_info(),
            "history": cxt.format_history(),
        }
        prompt = self._fill_template(self._default_prompt_template(), slots)
        prompt += "\n### 买家消息\n" + _effective_query(cxt)
        return prompt

    def execute(self, ctx):
        # 1-2. Local rule layer (tech first, zero LLM)
        intent = detect_intent(_effective_query(ctx))

        # 3. LLM fallback: run ClassifyAgent when the local layer misses (default)
        if intent == "default":
            intent = self._classify_via_llm(ctx)

        # Backfill intent onto the current turn's user message (already in history:
        # chat() adds the message before running the pipeline) — bargain counting
        # traces back through this
        for msg in reversed(ctx.history):
            if msg.role == "user":
                msg.metadata["intent"] = intent
                break

        next_node = INTENT_TO_MENU.get(intent, "xy_menu_default")
        settings = _get_bargain_settings(ctx)

        bargain_count = 0
        if intent == "price":
            # Replicates the bargain round count control: count includes the current
            # round; refuse once >= max_bargain_rounds (the max-th haggle gets the
            # fixed refusal script)
            bargain_count = _count_bargain_rounds(ctx)
            if bargain_count >= settings["max_bargain_rounds"]:
                next_node = "xy_menu_price_refuse"

        # Bargain params merge into filled_slots as slots for NLG prompt injection.
        # Note the ROUTE path's framework only merges slots → filled_slots after the
        # stages, while NLG runs inside the stages — so this also writes filled_slots
        # directly (same keys, the framework's later merge is idempotent), guaranteeing
        # NLG visibility within the turn
        bargain_slots = {
            "bargain_count": bargain_count,
            "max_bargain_rounds": settings["max_bargain_rounds"],
            "max_discount_percent": settings["max_discount_percent"],
            "max_discount_amount": settings["max_discount_amount"],
        }
        ctx.filled_slots.update(bargain_slots)
        ctx.nlu_result = {
            "next_node": next_node,
            "intent": intent,
            "slots": bargain_slots,
        }
        return ctx

    def _classify_via_llm(self, ctx) -> str:
        """LLM intent fallback — replicates ClassifyAgent (including the no_reply anti-flooding class)."""
        try:
            raw = self._call_llm(self.prompt_build(ctx), ctx.llm_config)
        except Exception as e:
            logger.warning("LLM 意图兜底失败，回落 default: %s", e)
            return "default"
        return self._sanitize_intent(raw)

    @classmethod
    def _sanitize_intent(cls, raw: str) -> str:
        """Sanitize LLM classification output: only the four labels are accepted; invalid output falls back to default."""
        text = (raw or "").strip().lower()
        for label in cls._VALID_INTENTS:
            if label in text:
                return label
        return "default"


# ============================================================================
# NLG stage — no_reply empty reply / bargain refusal fixed script / intent-level prompt generation
# ============================================================================

class FixedNLG(BaseNLG):
    """Xianyu NLG stage — replicates the three domain Agents' reply generation + safety filtering.

    Three paths (the first two short-circuit with zero LLM):
        1. intent=no_reply  → empty reply (channel contract: empty reply = not sent;
           replicates the original "-" return)
        2. bargain refusal node → fixed refusal script (answer_examples carry the
           marker text)
        3. intent menu node → node base_nlg_prompt (XIANYU_*_NLG_PROMPT)
           + bargain context (price intent) + buyer message → single LLM call +
           blocked-word filtering

    Mounted at the nlg position of the module-level generate dict (takes effect when
    the node has no override; the chat layer's jump detection runs after the nlu
    component and has already advanced the menu node and refreshed node-level LLM
    config, so the current node this stage reads is the matched menu). Node-level
    generate's nlg takes priority over this stage (node > module, stage_slots.py
    three-tier resolution).
    """

    stage_name = "fixed_nlg"

    # Fixed bargain refusal text (replicates the ai_reply_engine hardcode)
    REFUSE_TEXT = "抱歉，这个价格已经是最优惠的了，不能再便宜了哦！"
    # Hit marker: NLG short-circuits when the current node's answer_examples match this text
    _MARKER = REFUSE_TEXT

    # Blocked-word list and replacement text, replicating XianyuReplyBot._safe_filter
    BLOCKED_PHRASES = ("微信", "QQ", "支付宝", "银行卡", "线下")
    SAFE_REMINDER = "[安全提醒]请通过平台沟通"

    def _default_prompt_template(self) -> str:
        return XIANYU_DEFAULT_NLG_PROMPT

    def prompt_build(self, cxt) -> str:
        """Build the intent-level NLG prompt.

        The template keeps only the {__task_info__}/{__history__} slots; the bargain
        context and buyer message replicate tmp_xianyu's assembly (bargain round
        appended to the system tail, user message as a standalone section) and are
        appended here.
        """
        template = self._resolve_prompt_template(cxt)
        kwargs = self._build_template_kwargs(cxt)
        if template is None:
            template = self._default_prompt_template()
        prompt = self._fill_template(template, kwargs)

        if (cxt.nlu_result or {}).get("intent") == "price":
            prompt += self._bargain_block(cxt)

        # Replicates BaseAgent._build_messages: the buyer's current message as a
        # standalone section (user role)
        prompt += "\n### 买家消息\n" + _effective_query(cxt)
        return prompt

    def execute(self, ctx):
        intent = (ctx.nlu_result or {}).get("intent")

        # 1. no_reply: empty reply, the channel does not send it (zero LLM)
        if intent == "no_reply":
            ctx.nlg_result = {"content": ""}
            return ctx

        # 2. Bargain refusal node: fixed script (zero LLM)
        node = ctx.get_current_node()
        if node is not None and any(
            self._MARKER in (ex or "") for ex in (node.answer_examples or [])
        ):
            ctx.nlg_result = {"content": self.REFUSE_TEXT}
            return ctx

        # 3. Intent menu node: single LLM generation + blocked-word filtering
        prompt = self.prompt_build(ctx)
        raw = self._call_llm(prompt, self._tuned_llm_config(ctx))
        ctx.nlg_result = {"content": self._safe_filter(raw.strip())}
        return ctx

    # ------------------------------------------------------------------
    # Equivalent implementations of the tmp_xianyu per-Agent generation strategies
    # ------------------------------------------------------------------

    def _bargain_block(self, cxt) -> str:
        """Bargain context block — replicates PriceAgent's ▲current bargain round injection."""
        slots = cxt.filled_slots
        defaults = DEFAULT_BARGAIN_SETTINGS
        count = slots.get("bargain_count", 0)
        return (
            "\n【议价设置】\n"
            f"bargain_count: {count}\n"
            f"max_bargain_rounds: {slots.get('max_bargain_rounds', defaults['max_bargain_rounds'])}\n"
            f"max_discount_percent: {slots.get('max_discount_percent', defaults['max_discount_percent'])}\n"
            f"max_discount_amount: {slots.get('max_discount_amount', defaults['max_discount_amount'])}\n"
            f"▲当前议价轮次：{count}"
        )

    def _tuned_llm_config(self, ctx):
        """Tune temperature by intent — replicates the three Agents' temperature policies.

        PriceAgent dynamic temperature min(0.3 + 0.15×bargain round, 0.9), TechAgent
        0.4, DefaultAgent 0.7, max_tokens uniformly 500. Rewrites an llm_config copy
        rather than overriding _call_llm (the latter's signature is fixed, and test
        spies are attached to BaseNLG._call_llm).
        """
        cfg = ctx.llm_config
        if not cfg:
            return cfg
        tuned = dict(cfg)
        intent = (ctx.nlu_result or {}).get("intent")
        if intent == "price":
            count = int(ctx.filled_slots.get("bargain_count", 0) or 0)
            tuned["temperature"] = min(0.3 + count * 0.15, 0.9)
        elif intent == "tech":
            tuned["temperature"] = 0.4
        else:
            tuned["temperature"] = 0.7
        tuned["max_tokens"] = 500
        return tuned

    @classmethod
    def _safe_filter(cls, text: str) -> str:
        """Safety filtering — replicates XianyuReplyBot._safe_filter."""
        if any(p in text for p in cls.BLOCKED_PHRASES):
            return cls.SAFE_REMINDER
        return text


# ============================================================================
# RouteModule — top-level routing: root node + intent menu (all nodes stay in this module)
# ============================================================================

xy_route_root = BaseNode(
    node_code="xy_route_root",
    node_name="闲鱼路由根节点",
    node_description="闲鱼卖家客服总入口，覆盖议价、技术问答与通用咨询三大场景",
    node_todo_description="识别买家消息意图（本地规则 + LLM 兜底），分发到议价/技术/通用菜单节点",
    sub_nodes=["xy_menu_price", "xy_menu_price_refuse", "xy_menu_tech", "xy_menu_default"],
    answer_examples=[
        "您好，在的。关于商品的问题都可以问我哦。",
    ],
)

xy_menu_price = BaseNode(
    node_code="xy_menu_price",
    node_name="议价",
    node_description="买家在砍价/询问优惠，需按议价策略让利但守住底线",
    node_todo_description="命中议价意图（未达轮数上限），生成阶梯让利回复",
    sub_nodes=[],
    base_nlg_prompt=XIANYU_PRICE_NLG_PROMPT,
    answer_examples=[
        "亲，价格已经很实惠啦，可以包邮哦。",
    ],
)

xy_menu_price_refuse = BaseNode(
    node_code="xy_menu_price_refuse",
    node_name="议价拒绝",
    node_description="议价轮数已达上限，礼貌坚持底价",
    node_todo_description="命中议价意图且轮数达上限，输出固定拒绝话术",
    sub_nodes=[],
    # The fixed text doubles as FixedNLG's hit marker (marker match short-circuits
    # with zero LLM)
    answer_examples=[
        "抱歉，这个价格已经是最优惠的了，不能再便宜了哦！",
    ],
)

xy_menu_tech = BaseNode(
    node_code="xy_menu_tech",
    node_name="技术问答",
    node_description="买家咨询商品功能、用法、参数、故障等技术问题",
    node_todo_description="命中技术意图，基于商品信息简短作答",
    sub_nodes=[],
    base_nlg_prompt=XIANYU_TECH_NLG_PROMPT,
    answer_examples=[
        "支持蓝牙连接，说明书里有详细教程。",
    ],
)

xy_menu_default = BaseNode(
    node_code="xy_menu_default",
    node_name="通用客服",
    node_description="商品介绍、物流、售后等常规咨询",
    node_todo_description="未命中议价/技术关键词，按通用客服作答",
    sub_nodes=[],
    base_nlg_prompt=XIANYU_DEFAULT_NLG_PROMPT,
    answer_examples=[
        "亲，现货的，拍下后 48 小时内发货。",
    ],
)

xianyu_root = RouteModule(
    module_code="xianyu_root",
    module_name="闲鱼卖家客服总路由",
    module_description="复刻 tmp_xianyu.XianyuReplyBot 的意图路由与回复生成：本地规则 + LLM 兜底意图分类、意图级 prompt、议价轮数控制与动态温度",
    module_todo_description="对每条买家消息做意图检测，分发到议价/技术/通用菜单节点生成回复",
    module_nodes=[xy_route_root, xy_menu_price, xy_menu_price_refuse,
                  xy_menu_tech, xy_menu_default],
    generate={
        "nlu": XianyuIntentNLU(),
        # Module-level NLG: zero-LLM short-circuit for no_reply/refusal nodes; other
        # menu nodes generate from the node.base_nlg_prompt intent template
        # (temperature/length tuned per intent)
        "nlg": FixedNLG(),
    },
)


# ============================================================================
# Pattern registration — module-level registry.register, auto-discovered by AST scan
# ============================================================================

xianyu_agent_pattern = Pattern(
    code="xianyu_agent",
    name="闲鱼卖家客服助手",
    description="对话管理：ROUTE 每轮独立意图检测（本地规则 + LLM 兜底）+ 议价轮数控制 + 意图级 prompt",
    entry_module_code="xianyu_root",
    modules=[xianyu_root],
    # Query rewrite slot: time augmentation (zero LLM) — relative times in buyer
    # messages ("tomorrow afternoon" etc.) are resolved into absolute-time
    # annotations before entering the NLU/NLG prompts
    query=TimeAugQueryRewriter(),
)

registry.register(xianyu_agent_pattern)
