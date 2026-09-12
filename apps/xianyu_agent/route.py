"""
Xianyu seller customer service AGENT graph — replicates tmp_xianyu.XianyuReplyBot's
agent dialogue management (plan-⑧: the ROUTE pattern migrated to a two-layer
AGENT graph).

Replication source: the pre-migration dialogue/tmp_xianyu.py (XianyuReplyBot /
IntentRouter / the three domain Agents); prompt templates come from the XIANYU_*
series in apps/xianyu_agent/prompts.py.

Graph structure (pattern_type="agent", entry: xy_route_root; every user message
re-runs the whole graph from entry — the ROUTE era's "turn-end reset to root"
holds naturally, no extra code):

    xianyu_agent (Pattern)
    ├── xy_route_root      routing root ("xianyu_router"): intent detection
    │                      (local rules + LLM fallback) → conditional edge
    │                      TurnResult(next=<menu node>)，路由轮 content 为空
    ├── xy_menu_price      bargain menu ("xianyu_reply"): XIANYU_PRICE_NLG_PROMPT
    │                      + bargain-settings block + dynamic temperature
    ├── xy_menu_price_refuse  bargain refusal menu ("xianyu_rule_reply"):
    │                      cap reached → fixed script, zero LLM (the
    │                      answer_examples text is the script itself)
    ├── xy_menu_tech       tech Q&A menu ("xianyu_reply"): XIANYU_TECH_NLG_PROMPT
    └── xy_menu_default    general customer service menu ("xianyu_reply"):
                           XIANYU_DEFAULT_NLG_PROMPT (also short-circuits to an
                           empty reply on the no_reply intent)

Edges: xy_route_root.sub_nodes = the four menu codes (static adjacency); the
conditional edge actually taken each turn = the routing executor's
TurnResult.next. Menu nodes have no successors — their executors return no
`next`, the graph terminates there.

Dialogue-management mapping (tmp_xianyu → this framework, unchanged semantics
from the ROUTE era, now carried by node executors):
    IntentRouter three-tier routing (tech keywords/regex first → price
    keywords/regex → LLM fallback)
      → XianyuRouterExecutor: detect_intent local rule layer (keyword table +
        tmp_xianyu word lists and regexes) + XIANYU_NLU_PROMPT LLM fallback
        (four classes price/tech/no_reply/default; invalid output falls back to
        default); intent → menu node mapping (INTENT_TO_MENU) rides the
        conditional edge
    TimeAugQueryRewriter (was the pattern-skeleton query slot "time_aug_query")
      → runs inside the routing executor at turn start (zero LLM): relative
        times in buyer messages ("明天下午" etc.) are resolved into
        absolute-time annotations before entering the classification/NLG
        prompts; result lands in cxt.rewritten_queries
    ClassifyAgent classifies as no_reply (prompt flooding / unrelated to the
        item on sale)
      → routed to the default menu; XianyuReplyExecutor short-circuits to an
        empty reply; channel contract: empty reply = not sent
    PriceAgent dynamic temperature min(0.3 + 0.15×bargain round, 0.9),
        TechAgent 0.4, DefaultAgent 0.7, max_tokens 500
      → XianyuReplyExecutor._tuned_llm_config rewrites a copy of llm_config
        per intent (ec.cxt.llm_config is refreshed by the engine per node, R4)
    PriceAgent injects ▲current bargain round
      → the router writes the bargain params into filled_slots; the reply
        executor appends the bargain-settings block to the price prompt
    _safe_filter blocked-word filtering (WeChat/QQ/Alipay/bank card/offline)
      → XianyuReplyExecutor._safe_filter
    _extract_bargain_count (counts bargain rounds by tracing system messages)
      → the router counts user messages with metadata.intent=price (the
        framework has no system-side bargain messages)
    Bargain round count reaching the cap → fixed refusal script, zero LLM
      → the router's conditional edge lands on xy_menu_price_refuse; its rule
        executor ("xianyu_rule_reply") returns the answer_examples text
        verbatim (this pattern keeps the explicit threshold refusal so bargain
        behavior stays predictable and testable)

Known deliberate simplifications (unchanged from the ROUTE era):
    - TechAgent's enable_search (DashScope extra_body) and top_p=0.8 are not
      passed through: temperature/length are tuned via a llm_config copy

Message entry: apps/xianyu_agent/channel.py (default-reply API plugin decision
port); set XIANYU_CHANNEL_PATTERN=xianyu_agent to plug in.

Registration: module-level ``registry.register(Pattern(...))`` +
``plugin_registry.register("executor", ...)`` for the three node executors,
auto-discovered by AST scan (same idiom as before the migration).
"""

import logging
import re
from typing import Any, Dict

from nexus.context import fill_prompt_template
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.turn_result import TurnResult
from nexus.llm.resolve import build_provider
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry
from atoms.stages.query import TimeAugQueryRewriter
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

# Intent → menu node code (the routing executor's conditional-edge targets;
# refuse is re-decided separately via the bargain round count; no_reply lands
# on the default menu, where the reply executor short-circuits to an empty
# reply by intent)
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

# Fixed bargain refusal text (replicates the ai_reply_engine hardcode; doubles
# as the refusal node's answer_examples entry — the rule executor returns it
# verbatim, zero LLM)
REFUSE_TEXT = "抱歉，这个价格已经是最优惠的了，不能再便宜了哦！"

# Blocked-word list and replacement text, replicating XianyuReplyBot._safe_filter
BLOCKED_PHRASES = ("微信", "QQ", "支付宝", "银行卡", "线下")
SAFE_REMINDER = "[安全提醒]请通过平台沟通"

# Shared instance of the zero-LLM time-augmentation rewriter (stateless
# PipelineStage; the router runs it at turn start — the AGENT graph has no
# stages skeleton, so the former pattern-level "query" slot rides here)
_time_aug_rewriter = TimeAugQueryRewriter()


def detect_intent(message: str) -> str:
    """Local rule-based intent detection — replicates IntentRouter.detect's keyword/regex tiers.

    Purely local, zero LLM (the LLM fallback tier lives in
    XianyuRouterExecutor._classify_via_llm, which needs cxt.llm_config).
    Tech first: when amounts and tech words co-occur, classify as tech
    (consistent with the classification standard of XIANYU_NLU_PROMPT).

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
    """Get the rewritten buyer message: the router ran TimeAugQueryRewriter at
    turn start, so rewritten_queries[0] is the time-augmentation result; falls
    back to the original query when the rewrite is a no-op (or the router was
    skipped on a resumed turn)."""
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
    history": each turn's routing executor writes the intent into that turn's
    user message metadata; counting traces back through metadata (the current
    turn's user message is already stored and is included in the count).
    """
    count = 0
    for msg in cxt.history:
        if msg.role != "user":
            continue
        if (msg.metadata or {}).get("intent") == "price":
            count += 1
    return count


async def _call_llm(prompt: str, llm_config: Dict[str, Any]) -> str:
    """Single-round LLM call for the reply executor — the pre-merge
    BaseNLG._call_llm behavior, as executor-internal logic.

    When the provider streams natively AND a turn emitter is attached
    (chat_turn_stream path), the call runs streamed: the generated wording is
    user-visible text, so every chunk forwards as a delta while still
    aggregating into the full result. Non-streaming providers / plain
    chat_turn turns take the legacy path unchanged.
    """
    provider = build_provider(llm_config)

    messages = [{"role": "user", "content": prompt}]
    kwargs: Dict[str, Any] = dict(
        model=llm_config["model"],
        temperature=llm_config.get("temperature", 0.7),
        max_tokens=llm_config.get("max_tokens", 2048),
    )
    from nexus.engine.streaming import current_emitter, stream_llm_reply
    if (hasattr(provider, "achat_completion_stream")
            and current_emitter.get() is not None):
        result = await stream_llm_reply(
            provider.achat_completion_stream(messages=messages, **kwargs))
    else:
        result = await provider.achat_completion(messages=messages, **kwargs)

    content = result.get("content", "")
    logger.debug("Xianyu executor LLM 返回: %s", content[:200])
    return content


# ============================================================================
# Routing node executor — intent detection + conditional edge
# ============================================================================

class XianyuRouterExecutor(NodeExecutor):
    """Executor of xy_route_root — the ROUTE era's XianyuIntentNLU stage as a
    graph router (plugin code "xianyu_router").

    Per user message (the graph always starts here):

    1. Time-augmentation query rewrite (TimeAugQueryRewriter, zero LLM) — the
       former pattern-skeleton query slot; result lands in
       cxt.rewritten_queries.
    2. Intent detection: detect_intent local rule tier (tech first) →
       XIANYU_NLU_PROMPT LLM fallback (four classes price/tech/no_reply/
       default; call failure or invalid label falls back to default).
    3. The intent is backfilled onto the current turn's user message metadata
       (bargain counting traces back through this).
    4. Bargain round-count control: count >= max_bargain_rounds redirects the
       price intent to xy_menu_price_refuse (count includes the current round;
       the max-th haggle gets the fixed refusal script).
    5. Bargain params merge into filled_slots (bargain_count / max_*) for the
       price prompt's bargain-settings block; cxt.nlu_result keeps the
       {next_node, intent, slots} shape so later nodes / trace consumers read
       the routing decision.

    Routing output: TurnResult(content="", next=<hit menu node code>) — the
    routing turn produces no text (the branch node does), and next is the
    conditional edge (engine-validated against the node's sub_nodes).
    """

    # Valid labels for LLM output (no_reply is the most specific, matched first)
    _VALID_INTENTS = ("no_reply", "price", "tech", "default")

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt

        # 1. Query rewrite (time augmentation, zero LLM) — runs ahead of every
        #    classification/generation this turn
        await _time_aug_rewriter.execute(cxt)

        # 2. Local rule layer (tech first, zero LLM) → LLM fallback on miss
        intent = detect_intent(_effective_query(cxt))
        if intent == "default":
            intent = await self._classify_via_llm(cxt)

        # 3. Backfill intent onto the current turn's user message (already in
        #    history: chat() adds the message before dispatching the graph) —
        #    bargain counting traces back through this
        for msg in reversed(cxt.history):
            if msg.role == "user":
                msg.metadata["intent"] = intent
                break

        next_node = INTENT_TO_MENU.get(intent, "xy_menu_default")
        settings = _get_bargain_settings(cxt)

        bargain_count = 0
        if intent == "price":
            # Replicates the bargain round count control: count includes the
            # current round; refuse once >= max_bargain_rounds (the max-th
            # haggle gets the fixed refusal script)
            bargain_count = _count_bargain_rounds(cxt)
            if bargain_count >= settings["max_bargain_rounds"]:
                next_node = "xy_menu_price_refuse"

        # Bargain params merge into filled_slots for the price prompt's
        # bargain-settings block (non-price turns carry bargain_count=0 so
        # templates can reference the keys uniformly)
        bargain_slots = {
            "bargain_count": bargain_count,
            "max_bargain_rounds": settings["max_bargain_rounds"],
            "max_discount_percent": settings["max_discount_percent"],
            "max_discount_amount": settings["max_discount_amount"],
        }
        cxt.filled_slots.update(bargain_slots)
        cxt.nlu_result = {
            "next_node": next_node,
            "intent": intent,
            "slots": bargain_slots,
        }

        return TurnResult(content="", next=next_node)

    # ------------------------------------------------------------------
    # LLM fallback — replicates ClassifyAgent (incl. the no_reply anti-flooding class)
    # ------------------------------------------------------------------

    async def _classify_via_llm(self, cxt) -> str:
        llm_config = cxt.llm_config or {}
        if not llm_config:
            logger.warning("llm_config 为空，意图 LLM 兜底跳过，回落 default")
            return "default"
        try:
            raw = await _call_llm(self._build_prompt(cxt), llm_config)
        except Exception as e:
            logger.warning("LLM 意图兜底失败，回落 default: %s", e)
            return "default"
        return self._sanitize_intent(raw)

    def _build_prompt(self, cxt) -> str:
        """Build the LLM fallback classification prompt (the pre-merge
        XianyuIntentNLU.prompt_build).

        XIANYU_NLU_PROMPT keeps only the {__task_info__}/{__history__} slots;
        the buyer's current message replicates ClassifyAgent._build_messages
        as a standalone section, not relying on the last history line.
        """
        prompt = fill_prompt_template(XIANYU_NLU_PROMPT, {
            "task_info": cxt.format_task_info(),
            "history": cxt.format_history(),
        })
        prompt += "\n### 买家消息\n" + _effective_query(cxt)
        return prompt

    @classmethod
    def _sanitize_intent(cls, raw: str) -> str:
        """Sanitize LLM classification output: only the four labels are accepted; invalid output falls back to default."""
        text = (raw or "").strip().lower()
        for label in cls._VALID_INTENTS:
            if label in text:
                return label
        return "default"


# ============================================================================
# Generating branch executor — intent-level prompt + tuned LLM + safe filter
# ============================================================================

class XianyuReplyExecutor(NodeExecutor):
    """Executor of the generating menu nodes (xy_menu_price / xy_menu_tech /
    xy_menu_default; plugin code "xianyu_reply") — the ROUTE era's FixedNLG
    stage paths as executor-internal logic.

    Three paths (the first short-circuits with zero LLM):
        1. intent=no_reply  → empty reply (channel contract: empty reply =
           not sent; replicates the original "-" return)
        2. otherwise → node config's base_nlg_prompt (XIANYU_*_NLG_PROMPT)
           + bargain context (price intent) + buyer message → single LLM call
           with the per-intent tuned llm_config + blocked-word filtering

    Reads this turn's routing outputs from cxt (written by the router earlier
    in the same graph run): nlu_result.intent and the filled_slots bargain
    params. Terminal by construction: returns no `next` and the menu nodes
    declare no successors, so the graph ends here.
    """

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        node = ec.node
        intent = (cxt.nlu_result or {}).get("intent")

        # 1. no_reply: empty reply, the channel does not send it (zero LLM)
        if intent == "no_reply":
            return TurnResult(content="")

        # 2. Intent menu node: single LLM generation + blocked-word filtering
        prompt = self._build_prompt(cxt, node, intent)
        llm_config = self._tuned_llm_config(cxt, intent)
        raw = await _call_llm(prompt, llm_config)
        return TurnResult(content=self._safe_filter(raw.strip()))

    def _build_prompt(self, cxt, node, intent: str) -> str:
        """Build the intent-level NLG prompt.

        The template keeps only the {__task_info__}/{__history__} slots; the
        bargain context and buyer message replicate tmp_xianyu's assembly
        (bargain round appended to the system tail, user message as a
        standalone section) and are appended here.
        """
        template = (node.get_prompt("base_nlg_prompt")
                    if node is not None else None) or XIANYU_DEFAULT_NLG_PROMPT
        prompt = fill_prompt_template(template, {
            "task_info": cxt.format_task_info(),
            "history": cxt.format_history(),
        })

        if intent == "price":
            prompt += self._bargain_block(cxt)

        # Replicates BaseAgent._build_messages: the buyer's current message as
        # a standalone section (user role)
        prompt += "\n### 买家消息\n" + _effective_query(cxt)
        return prompt

    # ------------------------------------------------------------------
    # Equivalent implementations of the tmp_xianyu per-Agent generation strategies
    # ------------------------------------------------------------------

    @staticmethod
    def _bargain_block(cxt) -> str:
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

    @staticmethod
    def _tuned_llm_config(cxt, intent: str) -> Dict[str, Any]:
        """Tune temperature by intent — replicates the three Agents' temperature
        policies, over the engine-refreshed per-node llm_config (R4).

        PriceAgent dynamic temperature min(0.3 + 0.15×bargain round, 0.9),
        TechAgent 0.4, DefaultAgent 0.7, max_tokens uniformly 500. Rewrites an
        llm_config copy (never mutates cxt.llm_config).
        """
        cfg = cxt.llm_config
        if not cfg:
            return cfg
        tuned = dict(cfg)
        if intent == "price":
            count = int(cxt.filled_slots.get("bargain_count", 0) or 0)
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
        if any(p in text for p in BLOCKED_PHRASES):
            return SAFE_REMINDER
        return text


# ============================================================================
# Rule branch executor — fixed script from answer_examples, zero LLM
# ============================================================================

class XianyuRuleReplyExecutor(NodeExecutor):
    """Executor of xy_menu_price_refuse (plugin code "xianyu_rule_reply") —
    the zero-LLM fixed-reply branch: returns the node's first non-empty
    answer_examples entry verbatim as TurnResult(content=...).

    answer_examples doubles as the script carrier (the ROUTE era used it as
    FixedNLG's hit marker; the AGENT form consumes it directly). Terminal by
    construction like the other menu nodes.
    """

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        examples = (getattr(ec.node, "answer_examples", None) or [])
        for example in examples:
            if example:
                return TurnResult(content=example)
        logger.warning("规则回复节点 %s 无可用 answer_examples，返回空回复",
                       getattr(ec.node, "code", "?"))
        return TurnResult(content="")


# ============================================================================
# Graph declarations — routing root + intent menu (all nodes in one flat graph)
# ============================================================================

xy_route_root = BaseNode(
    code="xy_route_root",
    name="闲鱼路由根节点",
    description="闲鱼卖家客服总入口，覆盖议价、技术问答与通用咨询三大场景",
    task_description="识别买家消息意图（本地规则 + LLM 兜底），分发到议价/技术/通用菜单节点",
    # AGENT 静态邻接：根节点 → 四个菜单分支；实际走的条件边 = 路由执行器
    # 的 TurnResult.next（必须在 sub_nodes 内，引擎校验）
    sub_nodes=["xy_menu_price", "xy_menu_price_refuse", "xy_menu_tech",
               "xy_menu_default"],
    plugins={"loop": "xianyu_router"},
    answer_examples=[
        "您好，在的。关于商品的问题都可以问我哦。",
    ],
)

xy_menu_price = BaseNode(
    code="xy_menu_price",
    name="议价",
    description="买家在砍价/询问优惠，需按议价策略让利但守住底线",
    task_description="命中议价意图（未达轮数上限），生成阶梯让利回复",
    sub_nodes=[],
    plugins={"loop": "xianyu_reply"},
    base_nlg_prompt=XIANYU_PRICE_NLG_PROMPT,
    answer_examples=[
        "亲，价格已经很实惠啦，可以包邮哦。",
    ],
)

xy_menu_price_refuse = BaseNode(
    code="xy_menu_price_refuse",
    name="议价拒绝",
    description="议价轮数已达上限，礼貌坚持底价",
    task_description="命中议价意图且轮数达上限，输出固定拒绝话术",
    sub_nodes=[],
    plugins={"loop": "xianyu_rule_reply"},
    # The fixed refusal script itself — the rule executor returns it verbatim
    # (zero LLM)
    answer_examples=[REFUSE_TEXT],
)

xy_menu_tech = BaseNode(
    code="xy_menu_tech",
    name="技术问答",
    description="买家咨询商品功能、用法、参数、故障等技术问题",
    task_description="命中技术意图，基于商品信息简短作答",
    sub_nodes=[],
    plugins={"loop": "xianyu_reply"},
    base_nlg_prompt=XIANYU_TECH_NLG_PROMPT,
    answer_examples=[
        "支持蓝牙连接，说明书里有详细教程。",
    ],
)

xy_menu_default = BaseNode(
    code="xy_menu_default",
    name="通用客服",
    description="商品介绍、物流、售后等常规咨询",
    task_description="未命中议价/技术关键词，按通用客服作答",
    sub_nodes=[],
    plugins={"loop": "xianyu_reply"},
    base_nlg_prompt=XIANYU_DEFAULT_NLG_PROMPT,
    answer_examples=[
        "亲，现货的，拍下后 48 小时内发货。",
    ],
)


# ============================================================================
# Pattern registration — module-level registry.register, auto-discovered by AST
# scan. AGENT 图：每条买家消息从 entry 全图重跑（原 ROUTE 的"轮末回根"语义
# 天然成立）；stages 不再声明（AGENT 节点行为走 plugins）。
# ============================================================================

xianyu_agent_pattern = Pattern(
    code="xianyu_agent",
    name="闲鱼卖家客服助手",
    description="对话管理：AGENT 图每轮从路由根全图重跑——本地规则 + LLM 兜底意图分类、条件边分发议价/技术/通用菜单、议价轮数控制（达上限走零 LLM 固定拒绝话术）与意图级 prompt",
    pattern_type="agent",
    entry_node_code="xy_route_root",
    nodes=[xy_route_root, xy_menu_price, xy_menu_price_refuse,
           xy_menu_tech, xy_menu_default],
)

registry.register(xianyu_agent_pattern)


# ============================================================================
# App-local node executors — module-level plugin registration
# (kind="executor"), same idiom as the pattern registration above; referenced
# by string codes in the nodes' plugins={"loop": ...} declarations
# ============================================================================

from nexus.registry.plugins import registry as plugin_registry  # noqa: E402

plugin_registry.register("executor", "xianyu_router", XianyuRouterExecutor)
plugin_registry.register("executor", "xianyu_reply", XianyuReplyExecutor)
plugin_registry.register("executor", "xianyu_rule_reply", XianyuRuleReplyExecutor)
