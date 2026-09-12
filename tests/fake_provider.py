"""Scripted FakeProvider — fake LLM provider shared by offline tests (no real API access).

Distinguishes unified-stage / NLU / NLG / retry requests by prompt content and
returns fixed results, reused by the route pattern's logic and API tests.
"""

import json

from nexus.registry.providers import registry as llm_registry
from nexus.llm.provider import BaseLLMProvider

FAKE_PROVIDER_CODE = "fake_test_provider"


class FakeProvider(BaseLLMProvider):
    """Offline scripted LLM provider."""

    call_count = 0

    async def _achat_completion_impl(
        self,
        messages,
        model,
        temperature,
        max_tokens,
        stream=False,
        **kwargs,
    ):
        type(self).call_count += 1
        prompt = messages[0]["content"]
        return {"content": scripted_response(prompt)}


def register_fake_provider() -> None:
    """Register the scripted provider with the LLM registry (idempotent)."""
    if not llm_registry.is_registered(FAKE_PROVIDER_CODE):
        llm_registry.register(
            code=FAKE_PROVIDER_CODE,
            name="FakeProvider",
            description="offline scripted provider for route pattern tests",
            provider_class=FakeProvider,
            default_model="fake-model",
        )


def fake_llm_config() -> dict:
    """Return an llm_config that uses FakeProvider."""
    return {
        "code": FAKE_PROVIDER_CODE,
        "model": "fake-model",
        "temperature": 0.7,
        "max_tokens": 512,
    }


# ============================================================================
# Scripted response logic
# ============================================================================

def _extract_node_name(prompt: str) -> str:
    """Extract the node name from the current-node info in NLU/NLG prompts."""
    for line in prompt.split("\n"):
        line = line.strip()
        if line.startswith("节点名称:"):
            return line.split(":", 1)[1].strip()
    return ""


def _extract_query(prompt: str) -> str:
    """Extract the first line after the user-input section marker as the user query."""
    marker = "### 用户输入"
    idx = prompt.find(marker)
    if idx == -1:
        return ""
    segment = prompt[idx + len(marker):]
    lines = [l.strip() for l in segment.split("\n") if l.strip()]
    return lines[0] if lines else ""


def _route_nlu(query: str, retry: bool) -> str:
    """Scripted intent classification result for the route root node."""
    if "解析失败重试" in query and not retry:
        return "这不是合法的 JSON 输出"  # triggers the first parse failure
    if "永远解析失败" in query:
        return "这不是合法的 JSON 输出"
    if any(k in query for k in ("买车", "购车", "试驾", "看车", "询价", "车型")):
        return '{"next_node": "menu_sales", "slots": {}}'
    return '{"next_node": "", "slots": {}}'  # unknown-intent fallback


def _fsm_nlu(node_name: str, query: str) -> str:
    """Scripted intent/slot extraction result for FSM nodes."""
    # Off-topic input -> clarify intent (fixed topic/keywords slots)
    if any(k in query for k in ("收别的钱", "其他收费", "额外收费")):
        return json.dumps(
            {
                "next_node": "clarify",
                "slots": {"topic": "费用", "keywords": ["额外收费"]},
            },
            ensure_ascii=False,
        )
    mapping = {
        "询问品牌": {"next_node": "buy_ask_budget", "slots": {"brand": query}},
        "询问预算": {"next_node": "buy_ask_city", "slots": {"budget": query}},
        "询问城市": {"next_node": "buy_confirm", "slots": {"city": query}},
        "确认购车信息": {"next_node": "", "slots": {}},
    }
    result = mapping.get(node_name, {"next_node": "", "slots": {}})
    return json.dumps(result, ensure_ascii=False)


def _detect_pattern(prompt: str) -> str:
    """Which app's FSM the unified prompt belongs to (repair_/install_ node
    codes appear in the valid-values / candidate sections)."""
    if "repair_" in prompt:
        return "repair"
    return "install"


def _unified(node_name: str, query: str, retry: bool, prompt: str = "") -> str:
    """Scripted result for the unified stage (single call + structured output).

    Output protocol: {"reply", "next_node", "slots"}; the reply embeds the
    target node name for easier assertions.
    """
    if "解析失败重试" in query and not retry:
        return "这不是合法的 JSON 输出"  # triggers the first parse failure
    if "永远解析失败" in query:
        return "这不是合法的 JSON 输出"
    if "跳到不存在节点" in query:
        # Simulates the model violating a transition-edge constraint, for the
        # code-level hard-guard test
        return json.dumps(
            {
                "reply": "统一回复: 非法节点",
                "next_node": "not_exist_node",
                "slots": {},
            },
            ensure_ascii=False,
        )
    if "硬造澄清意图" in query:
        # Simulates a module without clarify enabled emitting a clarify signal,
        # for the allowed-set hard-guard test
        return json.dumps(
            {
                "reply": "统一回复: 硬造澄清",
                "next_node": "clarify",
                "slots": {"topic": "费用", "keywords": ["硬造"]},
            },
            ensure_ascii=False,
        )

    # Off-topic input -> clarify intent (fixed topic/keywords slots, short
    # acknowledgment reply)
    if any(k in query for k in ("收别的钱", "其他收费", "额外收费")):
        return json.dumps(
            {
                "reply": "统一承接: 这个问题我帮您确认一下",
                "next_node": "clarify",
                "slots": {"topic": "费用", "keywords": ["额外收费"]},
            },
            ensure_ascii=False,
        )

    # Route root node: classify intent to a menu
    if node_name == "统一路由根节点":
        if any(k in query for k in ("买车", "购车", "车型", "试驾", "询价")):
            return json.dumps(
                {
                    "reply": "统一回复: 购车菜单",
                    "next_node": "u_menu_sales",
                    "slots": {},
                },
                ensure_ascii=False,
            )
        if any(k in query for k in ("你好", "谢谢", "再见")):
            return json.dumps(
                {
                    "reply": "统一回复: 闲聊菜单",
                    "next_node": "u_menu_chitchat",
                    "slots": {},
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"reply": "统一回复: 统一路由根节点", "next_node": "", "slots": {}},
            ensure_ascii=False,
        )

    # FSM nodes: advance the flow + extract slots; the reply is the chosen
    # next node's script
    mapping = {
        "询问品牌": ("u_ask_budget", "brand", "统一回复: 询问预算"),
        "询问预算": ("u_confirm", "budget", "统一回复: 确认购车信息"),
        "确认购车信息": ("", None, "统一回复: 确认购车信息"),
    }
    next_node, slot_key, reply = mapping.get(
        node_name, ("", None, "统一回复: 未知节点")
    )
    slots = {slot_key: query} if slot_key else {}

    # install_booking_agent / repair_booking_agent nodes: scripted by
    # (node name, customer-reply keyword) pairs. The two FSMs SHARE node
    # names (outbound opening / address confirmation / visit-time negotiation...), so the branch is picked by
    # the node-code prefix visible in the prompt (candidate-node-code / valid-values
    # sections), not by name alone.
    if _detect_pattern(prompt) == "repair":
        repair = _repair_unified(node_name, query)
        if repair is not None:
            return repair
    else:
        install = _install_unified(node_name, query)
        if install is not None:
            return install

    return json.dumps(
        {"reply": reply, "next_node": next_node, "slots": slots},
        ensure_ascii=False,
    )


def _install_unified(node_name: str, query: str):
    """Scripted results for the install_booking_agent FSM (outbound call,
    hand-drawn template transcription + supplemented scenarios).

    Branch selection is (current node, customer reply) driven, mirroring the
    sketch's labeled edges (yes/no, knows/doesn't-know, convenient/not-
    convenient, specific-date/nearest/knows-neither) plus the supplemented
    intents (generic decline / callback / reschedule); returns None when
    the node is not an install node.
    """
    install_mapping = {
        "外呼开场": (["需要", "是的", "对", "方便"], "install_confirm_addr",
                    "service_needed", "外呼回复: 地址核对"),
        "地址核对": (["对", "一致", "是的"], "install_check_arrival",
                    "address_confirmed", "外呼回复: 到货确认"),
        "到货确认": (["到了", "到货", "收到", "签收"], "install_ask_time",
                    "arrived", "外呼回复: 上门时间协商"),
        "到货时间询问": (["不知道", "不清楚"], "install_available",
                        "eta_known", "外呼回复: 上门方便确认"),
        "时间段询问": (["上午", "下午", "点"], "install_ask_time",
                      "time_window", "外呼回复: 上门时间协商"),  # neg first below
        "上门方便确认": (["方便", "可以"], "install_ask_time",
                        "available", "外呼回复: 上门时间协商"),
        "上门时间协商": ([], None, None, ""),  # decided below (three-way)
        "档期推荐": ([], None, None, ""),      # decided below (two-way)
        "具体日期约定": ([], None, None, ""),  # decided below (confirm/loop back)
        "最近档期安排": ([], None, None, ""),  # decided below (confirm/loop back)
        "上门时间确认": ([], None, None, ""),  # decided below (confirm/reschedule)
        "改约重协商": ([], "install_ask_time", None,
                      "外呼回复: 上门时间协商"),
        "下次联系时间": ([], "install_end", "callback_time",
                        "外呼回复: 通话结束语"),
        "默认改约三天": ([], "install_end", None,
                        "外呼回复: 通话结束语"),
        "通用拒绝承接": ([], "install_end", "decline_reason",
                        "外呼回复: 通话结束语"),
        "通话结束语": ([], "", None, "外呼回复: 通话结束语"),
    }
    if node_name not in install_mapping:
        return None

    # Generic decline intents (supplemented): heard at ANY node → generic decline handling
    if any(k in query for k in ("不想预约", "不需要安装", "不用安装",
                                "已安装", "装过了", "装好了",
                                "质量问题", "有质量问题", "有问题",
                                "退货", "退了", "不是本人", "打错")):
        return json.dumps(
            {"reply": "外呼回复: 通用拒绝承接", "next_node": "install_decline",
             "slots": {"decline_reason": query}},
            ensure_ascii=False)
    # Callback intent (supplemented): busy now / not ready to book (install not declined) → callback time
    if any(k in query for k in ("现在没空", "现在不方便", "晚点再说",
                                "以后再约", "改天再打", "再说吧")):
        return json.dumps(
            {"reply": "外呼回复: 下次联系时间", "next_node": "install_ask_callback",
             "slots": {}},
            ensure_ascii=False)
    # Off-flow business questions (clarify): mid-flow FAQ-type asks trigger
    # the clarify signal — fee / warranty / duration / self-install / address change / shipping
    _FAQ_TOPIC_RULES = [
        ("费用", ("收费", "要钱吗", "多少钱", "免费吗", "收钱")),
        ("保修", ("保修", "质保", "三包")),
        ("安装时长", ("装多久", "多长时间", "几个小时", "要几个小时")),
        ("自装咨询", ("自己装", "自装", "不用师傅")),
        ("改地址", ("换个地址", "换地址", "改地址", "另一个地址")),
        ("催物流", ("物流", "还没到货吗", "什么时候发货")),
        ("其他", ("股票", "公司信息")),  # miss-the-table probe → fallback track
    ]
    for topic, kws in _FAQ_TOPIC_RULES:
        hit = next((kw for kw in kws if kw in query), None)
        if hit:
            return json.dumps(
                {"reply": "外呼承接: 这个问题我说一下",
                 "next_node": "clarify",
                 "slots": {"topic": topic, "keywords": [hit]}},
                ensure_ascii=False)

    # Visit-time negotiation three-way branch (sketch: specific date / nearest / knows-neither → recommendation)
    if node_name == "上门时间协商":
        if "最近" in query:
            return json.dumps(
                {"reply": "外呼回复: 最近档期安排", "next_node": "install_nearest",
                 "slots": {"visit_time": query}},
                ensure_ascii=False)
        if any(k in query for k in ("不知道", "随便", "都行", "你们定",
                                    "看着安排", "看着办", "你推荐")):
            return json.dumps(
                {"reply": "外呼回复: 档期推荐", "next_node": "install_recommend",
                 "slots": {}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "外呼回复: 具体日期约定", "next_node": "install_specific_date",
             "slots": {"visit_date": query}},
            ensure_ascii=False)
    # Schedule recommendation: customer picks one of the recommended slots -> specific-date booking
    if node_name == "档期推荐":
        if any(k in query for k in ("第一个", "上午", "明天", "后天", "点")):
            return json.dumps(
                {"reply": "外呼回复: 具体日期约定",
                 "next_node": "install_specific_date",
                 "slots": {"visit_date": query, "visit_hour": ""}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "外呼回复: 上门时间协商", "next_node": "install_ask_time",
             "slots": {}},
            ensure_ascii=False)
    # Specific-date booking / nearest-slot arrangement: bookable → confirm, not bookable → loop back
    if node_name in ("具体日期约定", "最近档期安排"):
        if any(k in query for k in ("不行", "不可以", "没空", "换")):
            return json.dumps(
                {"reply": "外呼回复: 上门时间协商", "next_node": "install_ask_time",
                 "slots": {}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "外呼回复: 上门时间确认", "next_node": "install_confirm_time",
             "slots": {"visit_date": query, "visit_hour": ""}},
            ensure_ascii=False)
    # Visit-time confirmation: confirmed → close script; reschedule (supplemented) → reschedule renegotiation
    if node_name == "上门时间确认":
        if any(k in query for k in ("改", "换", "不行", "再想想")):
            return json.dumps(
                {"reply": "外呼回复: 改约重协商", "next_node": "install_reschedule",
                 "slots": {"rescheduled": "是"}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "外呼回复: 通话结束语", "next_node": "install_end",
             "slots": {"visit_time": "已约定"}},
            ensure_ascii=False)

    keywords, next_node, slot_key, reply = install_mapping[node_name]
    # Negative-first overrides: a negative reply containing a positive
    # keyword ("not-arrived-yet" contains "arrived"; "cannot-say-a-window" contains "time")
    negatives = {
        "到货确认": ("还没", "没到", "没有"),
        "时间段询问": ("说不好", "不好说", "不确定", "没有", "没啥"),
        "地址核对": ("不对", "不对的", "错了", "不是这个"),
        "上门方便确认": ("不方便", "没时间", "不在", "近期都"),
        "外呼开场": ("不需要", "不用", "别打了"),
    }
    if node_name in negatives and any(k in query for k in negatives[node_name]):
        hit = False
    else:
        hit = any(k in query for k in keywords) if keywords else True
    if not hit:
        # negative branch: address mismatch → call-close script,
        # except the sketch's in-flow negative edges: arrival check + not-arrived → ETA
        # inquiry; ETA inquiry + knows → time-window inquiry; time-window inquiry + not provided →
        # availability check; availability check + not convenient → callback time (supplemented)
        in_flow_negatives = {
            "到货确认": ("install_ask_eta", "外呼回复: 到货时间询问",
                         {"arrived": "否"}),
            "到货时间询问": ("install_time_window", "外呼回复: 时间段询问",
                             {"eta": query}),
            "时间段询问": ("install_available", "外呼回复: 上门方便确认",
                           {}),
            "上门方便确认": ("install_ask_callback", "外呼回复: 下次联系时间",
                             {}),
        }
        if node_name in in_flow_negatives:
            neg_node, neg_reply, neg_slots = in_flow_negatives[node_name]
            return json.dumps(
                {"reply": neg_reply, "next_node": neg_node, "slots": neg_slots},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "外呼回复: 通话结束语", "next_node": "install_end",
             "slots": {}},
            ensure_ascii=False)
    slots = {slot_key: query} if slot_key else {}
    return json.dumps(
        {"reply": reply, "next_node": next_node, "slots": slots},
        ensure_ascii=False)


def _repair_unified(node_name: str, query: str):
    """Scripted results for the repair_booking_agent FSM (install variant:
    no arrival subtree, confirm-time → fault collection → close).

    Same (node name, customer-reply keyword) scripting shape as
    _install_unified, with the repair-specific branches:
    - decline intents drop already-installed/quality-issue/return, add
      fixed-it-themselves / repaired-elsewhere;
    - visit-time negotiation carries the callback branch inline (no separate
      availability node in the repair FSM);
    - visit-time confirmation → fault-info inquiry → fault-info confirmation
      → call close.
    Returns None when the node is not a repair node.
    """
    repair_mapping = {
        "外呼开场": (["需要", "是的", "对", "方便"], "repair_confirm_addr",
                    "service_needed", "外呼回复: 地址核对"),
        "地址核对": (["对", "一致", "是的"], "repair_ask_time",
                    "address_confirmed", "外呼回复: 上门时间协商"),
        "上门时间协商": ([], None, None, ""),  # decided below (four-way)
        "档期推荐": ([], None, None, ""),      # decided below (two-way)
        "具体日期约定": ([], None, None, ""),  # decided below (confirm/loop back)
        "最近档期安排": ([], None, None, ""),  # decided below (confirm/loop back)
        "上门时间确认": ([], None, None, ""),  # decided below (fault/reschedule)
        "故障信息询问": ([], None, None, ""),  # decided below (describe/cannot-describe)
        "改约重协商": ([], "repair_ask_time", None,
                      "外呼回复: 上门时间协商"),
        "故障信息确认": ([], "repair_end", None,
                      "外呼回复: 通话结束语"),
        "下次联系时间": ([], "repair_end", "callback_time",
                      "外呼回复: 通话结束语"),
        "默认改约三天": ([], "repair_end", None,
                      "外呼回复: 通话结束语"),
        "通用拒绝承接": ([], "repair_end", "decline_reason",
                      "外呼回复: 通话结束语"),
        "通话结束语": ([], "", None, "外呼回复: 通话结束语"),
    }
    if node_name not in repair_mapping:
        return None

    # Generic decline intents (repair variant): heard at ANY node → generic decline handling
    if any(k in query for k in ("不想维修", "不需要维修", "不用维修",
                                "自己修好了", "修好了", "已经修好",
                                "别人修", "找人修了", "修过了",
                                "不是本人", "打错")):
        return json.dumps(
            {"reply": "外呼回复: 通用拒绝承接", "next_node": "repair_decline",
             "slots": {"decline_reason": query}},
            ensure_ascii=False)
    # Callback intent: busy now / not ready to book (repair not declined) → callback time
    if any(k in query for k in ("现在没空", "现在不方便", "晚点再说",
                                "以后再约", "改天再打", "再说吧")):
        return json.dumps(
            {"reply": "外呼回复: 下次联系时间", "next_node": "repair_ask_callback",
             "slots": {}},
            ensure_ascii=False)
    # Off-flow business questions (clarify): repair FAQ families
    _FAQ_TOPIC_RULES = [
        ("费用", ("收费", "要钱吗", "多少钱", "免费吗", "收钱", "上门费")),
        ("保修", ("保修", "质保", "三包", "过保")),
        ("维修时长", ("修多久", "多长时间", "几个小时", "要几个小时")),
        ("配件", ("带配件", "带零件", "换零件", "有配件吗", "原厂件")),
        ("自修咨询", ("自己修", "自修", "不用师傅", "指导一下怎么修")),
        ("进度查询", ("什么时候来", "几点到", "师傅到哪了")),
        ("其他", ("股票", "公司信息")),  # miss-the-table probe → fallback track
    ]
    for topic, kws in _FAQ_TOPIC_RULES:
        hit = next((kw for kw in kws if kw in query), None)
        if hit:
            return json.dumps(
                {"reply": "外呼承接: 这个问题我说一下",
                 "next_node": "clarify",
                 "slots": {"topic": topic, "keywords": [hit]}},
                ensure_ascii=False)

    # Visit-time negotiation four-way branch (specific date / nearest / knows-neither → recommendation / busy → callback)
    if node_name == "上门时间协商":
        if "最近" in query:
            return json.dumps(
                {"reply": "外呼回复: 最近档期安排", "next_node": "repair_nearest",
                 "slots": {"visit_time": query}},
                ensure_ascii=False)
        if any(k in query for k in ("不知道", "随便", "都行", "你们定",
                                    "看着安排", "看着办", "你推荐")):
            return json.dumps(
                {"reply": "外呼回复: 档期推荐", "next_node": "repair_recommend",
                 "slots": {}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "外呼回复: 具体日期约定", "next_node": "repair_specific_date",
             "slots": {"visit_date": query}},
            ensure_ascii=False)
    # Schedule recommendation: customer picks one of the recommended slots -> specific-date booking
    if node_name == "档期推荐":
        if any(k in query for k in ("第一个", "上午", "明天", "后天", "点")):
            return json.dumps(
                {"reply": "外呼回复: 具体日期约定",
                 "next_node": "repair_specific_date",
                 "slots": {"visit_date": query, "visit_hour": ""}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "外呼回复: 上门时间协商", "next_node": "repair_ask_time",
             "slots": {}},
            ensure_ascii=False)
    # Specific-date booking / nearest-slot arrangement: bookable → confirm, not bookable → loop back
    if node_name in ("具体日期约定", "最近档期安排"):
        if any(k in query for k in ("不行", "不可以", "没空", "换")):
            return json.dumps(
                {"reply": "外呼回复: 上门时间协商", "next_node": "repair_ask_time",
                 "slots": {}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "外呼回复: 上门时间确认", "next_node": "repair_confirm_time",
             "slots": {"visit_date": query, "visit_hour": ""}},
            ensure_ascii=False)
    # Visit-time confirmation: confirmed → fault-info inquiry; reschedule → reschedule renegotiation
    if node_name == "上门时间确认":
        if any(k in query for k in ("改", "换", "不行", "再想想")):
            return json.dumps(
                {"reply": "外呼回复: 改约重协商", "next_node": "repair_reschedule",
                 "slots": {"rescheduled": "是"}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "外呼回复: 故障信息询问", "next_node": "repair_ask_fault",
             "slots": {"visit_time": "已约定"}},
            ensure_ascii=False)
    # Fault-info inquiry: cannot describe → hold (next_node empty, stay on the node and keep prompting)
    if node_name == "故障信息询问":
        if any(k in query for k in ("说不清", "不知道哪", "说不出来")):
            return json.dumps(
                {"reply": "外呼回复: 故障信息询问", "next_node": "",
                 "slots": {}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "外呼回复: 故障信息确认",
             "next_node": "repair_confirm_fault",
             "slots": {"fault_description": query}},
            ensure_ascii=False)

    keywords, next_node, slot_key, reply = repair_mapping[node_name]
    # Negative-first overrides (same reason as install's table)
    negatives = {
        "地址核对": ("不对", "不对的", "错了", "不是这个"),
        "外呼开场": ("不需要", "不用", "别打了"),
    }
    if node_name in negatives and any(k in query for k in negatives[node_name]):
        hit = False
    else:
        hit = any(k in query for k in keywords) if keywords else True
    if not hit:
        return json.dumps(
            {"reply": "外呼回复: 通话结束语", "next_node": "repair_end",
             "slots": {}},
            ensure_ascii=False)
    slots = {slot_key: query} if slot_key else {}
    return json.dumps(
        {"reply": reply, "next_node": next_node, "slots": slots},
        ensure_ascii=False)


def _extract_xianyu_section(prompt: str, marker: str) -> str:
    """Extract the first line of the given section (heading starting with ###) from a Xianyu NLG prompt."""
    idx = prompt.find(marker)
    if idx == -1:
        return ""
    segment = prompt[idx + len(marker):]
    lines = [l.strip() for l in segment.split("\n") if l.strip()]
    return lines[0] if lines else ""


def _xianyu_nlg(prompt: str) -> str:
    """Xianyu intent NLG prompt (XIANYU_*_NLG_PROMPT, contains the buyer-message section).

    The reply carries the intent persona keyword from the task description,
    so assertions can tell which menu-node template was hit.
    """
    task = ""
    for line in prompt.split("\n"):
        line = line.strip()
        if line.startswith("## 任务描述"):
            idx = prompt.find(line)
            after = prompt[idx + len(line):].lstrip("\n").split("\n", 1)
            task = after[0].strip() if after else ""
            break
    if "议价" in task:
        return "闲鱼回复: 议价"
    if "技术" in task:
        return "闲鱼回复: 技术"
    return "闲鱼回复: 通用"


def scripted_response(prompt: str) -> str:
    """Return the scripted LLM output for the prompt type."""
    node_name = _extract_node_name(prompt)

    # Xianyu intent-classification prompt (the graph router's LLM fallback):
    # unmatched messages fall to the default label (price/tech hits are
    # caught by the local rule tier and never reach here)
    if "通用意图分类器" in prompt:
        return "default"

    # Xianyu intent NLG prompt: no node-name line (uses node-level nlg
    # intent templates); identified by the buyer-message section header
    # (checked before the generic NLG fallback)
    if "### 买家消息" in prompt:
        return _xianyu_nlg(prompt)

    # NLU retry/repair prompt (contains the repair-requirements section)
    # -> return the correct format per protocol
    if "修正要求" in prompt:
        query = _extract_query(prompt).replace("解析失败重试", "")
        if '"reply"' in prompt:
            return _unified(node_name, query, retry=True, prompt=prompt)
        return _route_nlu(query, retry=True)

    # Unified-stage prompt: three-field JSON protocol with reply + next_node
    # (checked before the NLU branch)
    if '"reply"' in prompt and '"next_node"' in prompt:
        return _unified(node_name, _extract_query(prompt), retry=False,
                        prompt=prompt)

    # NLU prompt: requires next_node JSON output
    if '"next_node"' in prompt:
        query = _extract_query(prompt)
        if node_name == "路由根节点":
            return _route_nlu(query, retry=False)
        return _fsm_nlu(node_name, query)

    # Clarify prompt (contains the KB recall-content section and is not an
    # NLU JSON protocol) -> return per mode
    if "知识库召回内容" in prompt and '"next_node"' not in prompt:
        if "召回内容为空或无相关内容" in prompt or "（无相关知识库内容）" in prompt:
            return "承接：该问题暂无法详细解答。请问您的预算大概是多少呢？"
        return "解答：除车价外仅收取上牌费与服务费。请问您的预算大概是多少呢？"

    # Install clarify prompt (custom keyword-gated stage; carries the FAQ
    # answer section or the fallback miss wording)
    if "FAQ 答案" in prompt:
        return "澄清解答: 收费问题已答复，咱们继续约时间。"
    if "关键词卡控未命中" in prompt:
        return "澄清兜底: 这个问题稍后核实，咱们继续约时间。"

    # NLG prompt: the reply text carries the current node name, so assertions
    # can tell which node NLG used
    if node_name:
        return f"回复: {node_name}"
    return "回复: 无当前节点"
