"""repair_booking_agent (FSM mode) offline tests — the install-booking
outbound FSM adapted to a repair scenario, with the three deliberate
differences: no arrival subtree, repair-shaped decline intents (no
quality/return exits), and the fault-collection stage between time
confirmation and hang-up.

LLM output is simulated via the scripted FakeProvider (repair branch in
_repair_unified; the install/repair FSMs share node names, so the branch
is picked by the node-code prefix in the prompt). Covers:
1. Pattern structure and AST auto-discovery registration (14 nodes; no
   arrival nodes; confirm-time → ask_fault edge; fault → end edge)
2. Outbound opening + happy path: 开场→地址核对→时间协商(具体日期)→确认
   →故障信息询问→故障信息确认→通话结束 (fault slot accumulates before end)
3. Time negotiation branches: 最近 / 都不知道→推荐→选定
4. Repair-shaped decline intents at any node → repair_decline → repair_end;
   install-only intents (质量问题/退货) are NOT decline exits here
5. 现在没空 → 下次联系时间 → end; callback triage branches (valid / too far /
   past / vague → default 3-day proposal)
6. Booking-time hard guard (subclass-reused machinery, repair node codes):
   bookable annotated / unbookable rerouted to repair_recommend with the
   schedule-backed reply / callback times skipped / no-schedule no-op
7. time_aug_query wiring; keyword clarify (repair FAQ table) rounds
8. validate_pattern passes (app-local stage codes resolve)
"""

import logging

import pytest

from fake_provider import (
    FakeProvider,
    fake_llm_config,
    register_fake_provider,
)

logging.basicConfig(level=logging.WARNING)

TASK_INFO = {
    "channel": "repair_booking",
    "agent_name": "滨江商城售后客服",
    "user_name": "李女士",
    "product_name": "对开门冰箱",
    "order_id": "SO-20260909-002",
    "address": "杭州市滨江区YY路2号3幢502室",
    "available_slots": [
        "2026-09-10 09:00-12:00",
        "2026-09-11 14:00-17:00",
        "2026-09-12 09:00-12:00",
    ],
}


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="session", autouse=True)
def _fake_provider():
    """Register the scripted provider, reused for the whole test run."""
    register_fake_provider()


@pytest.fixture(scope="module")
def pattern():
    """Discover builtin patterns and return repair_booking_agent."""
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.repair_booking_agent.route" in imported, (
        f"repair_booking_agent 未被自动发现，已发现: {imported}"
    )
    return registry.get("repair_booking_agent")


@pytest.fixture()
def sessions():
    """A fresh session container per test."""
    return {}


def launch(pattern, sessions, session_id="s1", task_info=None):
    """Simulate main.py's launch flow (task_info carries the repair order
    facts + technician schedule)."""
    from nexus.engine.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = dict(
        TASK_INFO if task_info is None else task_info)
    session.cxt.metadata["llm_override"] = fake_llm_config()
    sessions[session_id] = session
    return session


def chat(sessions, session_id, query):
    """Run one dialogue turn via nexus.engine.chat."""
    from nexus.engine.chat import chat as chat_fn

    return chat_fn(query=query, session_id=session_id, all_sessions=sessions)


def end_actions(cxt):
    """The conversation_end actions accumulated in cxt.actions."""
    return [a for a in cxt.actions if "conversation_end" in a]


def reach_confirm_time(sessions):
    """Walk the happy prefix (greet → address → time) up to 上门时间确认."""
    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "10月1号下午3点吧")
    chat(sessions, "s1", "可以的没问题")


# ============================================================================
# Structure tests
# ============================================================================

def test_pattern_auto_discovered_and_structure(pattern):
    """14 nodes; no arrival subtree; confirm-time routes to fault collection
    (not end); every business node carries a decline edge; repair_end is the
    sole is_end terminal."""
    from nexus.model.module import ModuleType

    assert pattern.code == "repair_booking_agent"
    assert pattern.entry_module_code == "repair_booking"

    module = pattern.module_map["repair_booking"]
    assert module.type == ModuleType.FSM

    assert [n.node_code for n in module.module_nodes] == [
        "repair_greet", "repair_confirm_addr", "repair_ask_time",
        "repair_recommend", "repair_specific_date", "repair_nearest",
        "repair_confirm_time", "repair_reschedule", "repair_ask_fault",
        "repair_confirm_fault", "repair_ask_callback",
        "repair_callback_default", "repair_decline", "repair_end",
    ]

    # The install arrival subtree is GONE in the repair variant
    codes = set(pattern.node_map)
    assert not any(c.startswith("repair_check_arrival") or
                   "eta" in c or "time_window" in c or "available" in c
                   for c in codes)

    sub = {n.node_code: set(n.sub_nodes) for n in module.module_nodes}
    assert sub["repair_greet"] == {"repair_confirm_addr", "repair_end",
                                   "repair_decline"}
    # No arrival hop: address ok → straight into time negotiation
    assert sub["repair_confirm_addr"] == {"repair_ask_time", "repair_end",
                                          "repair_decline"}
    # The callback branch rides the negotiation node (no separate available)
    assert sub["repair_ask_time"] == {"repair_recommend",
                                      "repair_specific_date",
                                      "repair_nearest", "repair_ask_callback",
                                      "repair_decline"}
    assert sub["repair_recommend"] == {"repair_specific_date",
                                       "repair_nearest", "repair_ask_time",
                                       "repair_decline"}
    assert sub["repair_specific_date"] == {"repair_confirm_time",
                                           "repair_ask_time",
                                           "repair_decline"}
    assert sub["repair_nearest"] == {"repair_confirm_time",
                                     "repair_ask_time", "repair_decline"}
    # Key repair difference: confirmed time → fault collection (NOT end)
    assert sub["repair_confirm_time"] == {"repair_ask_fault",
                                          "repair_reschedule",
                                          "repair_decline"}
    assert sub["repair_reschedule"] == {"repair_ask_time", "repair_decline"}
    assert sub["repair_ask_fault"] == {"repair_confirm_fault",
                                       "repair_decline"}
    # Fault confirmed → hang up
    assert sub["repair_confirm_fault"] == {"repair_end"}
    assert sub["repair_ask_callback"] == {"repair_end",
                                          "repair_callback_default",
                                          "repair_decline"}
    assert sub["repair_callback_default"] == {"repair_end"}
    assert sub["repair_decline"] == {"repair_end"}
    assert sub["repair_end"] == set()

    assert pattern.node_map["repair_end"].is_end is True
    assert not any(n.is_end for n in module.module_nodes[:-1])


def test_stage_wiring(pattern):
    """Module stages bind the app-local repair codes; the skeleton carries
    the clarify slot for the module layer to fill."""
    root = pattern.module_map["repair_booking"]
    assert root.stages == {"nlu": "repair_unified",
                           "clarify": "repair_clarify",
                           "nlg": "nlg_pass_through"}

    skeleton_values = {slot: code for e in pattern.stages
                       for slot, code in e.items()}
    assert skeleton_values.get("query") == "time_aug_query"
    assert skeleton_values.get("nlu") is None
    assert skeleton_values.get("clarify") is None
    assert skeleton_values.get("nlg") is None

    from nexus.registry.plugins import registry as plugin_registry
    for code in ("repair_unified", "repair_recommend_nlg", "repair_clarify"):
        assert plugin_registry.has("stage", code), code


def test_module_prompt_override_carries_fault_section(pattern, sessions):
    """The module-level template override carries the repair persona, the
    fault-collection special case, and the repair-shaped decline list."""
    session = launch(pattern, sessions)

    from apps.repair_booking_agent.stages import RepairBookingUnifiedNLU
    original = RepairBookingUnifiedNLU._call_llm
    captured = {}

    def spy(self, prompt, llm_config=None):
        captured["prompt"] = prompt
        return original(self, prompt, llm_config)

    RepairBookingUnifiedNLU._call_llm = spy
    try:
        chat(sessions, "s1", "喂，你好")
    finally:
        RepairBookingUnifiedNLU._call_llm = original

    prompt = captured["prompt"]
    assert "### 任务信息" in prompt
    assert "上门维修" in prompt
    assert "故障信息" in prompt                    # fault-collection guidance
    assert "已经自己修好了" in prompt              # repair-shaped decline list
    assert "2026-09-10 09:00-12:00" in prompt      # available_slots injected


def test_pattern_passes_validation(pattern):
    from nexus.model.validation import validate_pattern

    validate_pattern(pattern)  # no raise


# ============================================================================
# Happy-path walk tests
# ============================================================================

def test_connect_turn_opens_call_and_advances(pattern, sessions):
    """Outbound opening: the connect turn runs on 外呼开场; the customer's
    first response advances to 地址核对."""
    session = launch(pattern, sessions)
    reply = chat(sessions, "s1", "喂，你好，方便的你说")

    assert session.cxt.current_node_code == "repair_confirm_addr"
    assert reply == "外呼回复: 地址核对"


def test_happy_path_full_walk_collects_fault_before_end(pattern, sessions):
    """Main flow: 开场→地址核对→时间协商(具体日期)→确认→故障询问→故障确认→
    通话结束; the fault slot is collected BETWEEN time confirmation and the
    hang-up; one LLM call per turn (unified)."""
    session = launch(pattern, sessions)

    steps = [
        ("方便的，是要维修", "repair_confirm_addr", "service_needed"),
        ("对的是这个地址", "repair_ask_time", "address_confirmed"),
        ("10月1号下午3点吧", "repair_specific_date", "visit_date"),
        ("可以的没问题", "repair_confirm_time", "visit_time"),
        ("好的确认", "repair_ask_fault", "visit_time"),
        ("冰箱不制冷了，还有异响", "repair_confirm_fault",
         "fault_description"),
        ("对的没问题", "repair_end", None),
    ]
    for query, expected_node, _ in steps:
        before = FakeProvider.call_count
        reply = chat(sessions, "s1", query)
        assert session.cxt.current_node_code == expected_node, (
            f"{query!r} 后应停在 {expected_node}，"
            f"实际 {session.cxt.current_node_code}"
        )
        assert reply.startswith("外呼回复:")
        assert FakeProvider.call_count - before == 1  # unified: one call/turn

    # Terminal: entering repair_end fired the conversation_end action
    assert end_actions(session.cxt)

    # The fault description landed BEFORE the close (the repair call's goal)
    assert session.cxt.filled_slots.get(
        "fault_description") == "冰箱不制冷了，还有异响"
    assert session.cxt.filled_slots.get("visit_time") == "已约定"


def test_fault_unclear_stays_asking(pattern, sessions):
    """Fault collection: 说不清 → stays on 故障信息询问 (empty next_node),
    a clear description on the retry advances."""
    session = launch(pattern, sessions)
    reach_confirm_time(sessions)
    assert session.cxt.current_node_code == "repair_confirm_time"

    chat(sessions, "s1", "好的确认")                # → 故障信息询问
    assert session.cxt.current_node_code == "repair_ask_fault"
    reply = chat(sessions, "s1", "说不清楚什么问题")  # 说不清 → 留在原节点
    assert session.cxt.current_node_code == "repair_ask_fault"
    assert reply == "外呼回复: 故障信息询问"

    reply = chat(sessions, "s1", "就是有异响，嗡嗡的")  # 描述清楚 → 故障确认
    assert session.cxt.current_node_code == "repair_confirm_fault"
    chat(sessions, "s1", "对的")
    assert session.cxt.current_node_code == "repair_end"
    assert end_actions(session.cxt)


# ============================================================================
# Time-negotiation branch tests
# ============================================================================

def test_nearest_slot_path(pattern, sessions):
    """最近 branch: 时间协商→最近档期安排→时间确认→故障询问→结束."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")
    reply = chat(sessions, "s1", "最近的就行")
    assert session.cxt.current_node_code == "repair_nearest"
    assert reply == "外呼回复: 最近档期安排"
    chat(sessions, "s1", "可以")
    assert session.cxt.current_node_code == "repair_confirm_time"
    chat(sessions, "s1", "确认")
    assert session.cxt.current_node_code == "repair_ask_fault"
    chat(sessions, "s1", "门关不严")
    assert session.cxt.current_node_code == "repair_confirm_fault"
    chat(sessions, "s1", "对")
    assert session.cxt.current_node_code == "repair_end"
    assert end_actions(session.cxt)


def test_recommend_pick_path(pattern, sessions):
    """都不知道→档期推荐→选定→具体日期约定."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "你们看着安排吧")           # 都不知道 → 推荐
    assert session.cxt.current_node_code == "repair_recommend"
    reply = chat(sessions, "s1", "第一个不错")        # 选定 → 具体日期
    assert session.cxt.current_node_code == "repair_specific_date"
    assert reply == "外呼回复: 具体日期约定"


def test_confirm_time_reschedule_loops(pattern, sessions):
    """确认后改约 → 改约重协商 → 回到时间协商重新约定."""
    session = launch(pattern, sessions)
    reach_confirm_time(sessions)
    assert session.cxt.current_node_code == "repair_confirm_time"

    reply = chat(sessions, "s1", "时间想改一下")
    assert session.cxt.current_node_code == "repair_reschedule"
    assert reply == "外呼回复: 改约重协商"

    chat(sessions, "s1", "嗯重新约")
    assert session.cxt.current_node_code == "repair_ask_time"
    chat(sessions, "s1", "10月2号上午10点")
    assert session.cxt.current_node_code == "repair_specific_date"


def test_address_mismatch_ends(pattern, sessions):
    """地址不一致 → 通话结束."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要维修")
    reply = chat(sessions, "s1", "地址不对")

    assert session.cxt.current_node_code == "repair_end"
    assert reply == "外呼回复: 通话结束语"
    assert end_actions(session.cxt)


# ============================================================================
# Decline-channel tests (repair-shaped)
# ============================================================================

@pytest.mark.parametrize("decline_query", [
    "不需要维修了，别约了",       # 不想维修
    "已经自己修好了",            # 已自修
    "已经找别人修过了",          # 已找别人修
    "我不是本人，打错了",        # 非本人
])
def test_generic_decline_at_any_node(pattern, sessions, decline_query):
    """The repair decline intents, heard mid-flow, land on repair_decline
    (empathetic beat) then repair_end (goodbye beat)."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")

    reply = chat(sessions, "s1", decline_query)
    assert session.cxt.current_node_code == "repair_decline", decline_query
    assert reply == "外呼回复: 通用拒绝承接"
    assert session.cxt.nlu_result["slots"]["decline_reason"] == decline_query

    reply = chat(sessions, "s1", "嗯好的")
    assert session.cxt.current_node_code == "repair_end"
    assert reply == "外呼回复: 通话结束语"
    assert end_actions(session.cxt)


def test_quality_complaint_is_not_a_decline(pattern, sessions):
    """A quality complaint IS the repair reason — it feeds fault collection,
    never the decline channel (the install app's decline exit is gone)."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "10月1号下午3点")
    chat(sessions, "s1", "可以")

    reply = chat(sessions, "s1", "冰箱有质量问题，不制冷")  # → 故障询问
    assert session.cxt.current_node_code == "repair_ask_fault"
    assert session.cxt.current_node_code != "repair_decline"


# ============================================================================
# Callback-channel tests (inherited triage machinery)
# ============================================================================

def _reach_callback(sessions):
    """Walk to the callback node: greet → address → 没空."""
    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "最近都没空，晚点再说")      # → 下次联系时间


def test_callback_valid_time_closes_on_customer_time(pattern, sessions):
    """Branch 2: a valid future time within 2 weeks → close restating the
    customer's time; callback_source=customer."""
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))
    _reach_callback(sessions)

    reply = chat(sessions, "s1", "下周一再打给我")   # (2026-09-14)
    assert session.cxt.current_node_code == "repair_end"
    slots = session.cxt.filled_slots
    assert slots.get("callback_source") == "customer"
    assert "2026-09-14" in slots.get("callback_time", "")
    assert "2026-09-14" in reply
    assert end_actions(session.cxt)


def test_callback_too_far_falls_back_to_default(pattern, sessions):
    """Branch 1: beyond 2 weeks → repair_callback_default (default 3-day
    proposal); the customer's answer closes the call (two beats)."""
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))
    _reach_callback(sessions)

    reply = chat(sessions, "s1", "下个月20号再打给我")   # > 2 weeks out
    assert session.cxt.current_node_code == "repair_callback_default"
    slots = session.cxt.filled_slots
    assert slots.get("callback_source") == "default"
    assert slots.get("callback_time") == "2026-09-12"   # 2026-09-09 + 3d
    assert "2026-09-12" in reply
    assert "2026-10" not in reply                       # far date never announced
    assert not end_actions(session.cxt)                 # not closed yet (beat 1)

    reply = chat(sessions, "s1", "行吧就这样")          # beat 2: answer → close
    assert session.cxt.current_node_code == "repair_end"
    assert "2026-09-12" in reply
    assert end_actions(session.cxt)


def test_callback_vague_falls_back_to_default(pattern, sessions):
    """Branch 3: 都行/没给时间 → default node → close on answer."""
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))
    _reach_callback(sessions)

    reply = chat(sessions, "s1", "都行你们看着打")       # vague
    assert session.cxt.current_node_code == "repair_callback_default"
    assert session.cxt.filled_slots.get("callback_source") == "default"

    chat(sessions, "s1", "嗯没问题")
    assert session.cxt.current_node_code == "repair_end"
    assert end_actions(session.cxt)


# ============================================================================
# Booking-time guard tests (install machinery, repair node codes)
# ============================================================================

def test_guard_bookable_time_annotated(pattern, sessions):
    """Guard: a bookable repair request is annotated and proceeds."""
    session = launch(pattern, sessions)
    import time as _time
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))

    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "明天上午10点")   # (2026-09-10 10:00) 可约
    assert session.cxt.current_node_code == "repair_specific_date"

    slots = session.cxt.filled_slots
    assert slots.get("bookable") is True
    assert slots.get("matched_slot") == "2026-09-10 09:00-12:00"
    assert slots.get("requested_time") == "2026-09-10 10:00"


def test_guard_unbookable_time_reroutes_to_recommend(pattern, sessions):
    """Guard: an unbookable request is deterministically rerouted to
    repair_recommend with a schedule-backed reply (zero extra LLM)."""
    session = launch(pattern, sessions)
    import time as _time
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))

    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")
    before = FakeProvider.call_count
    reply = chat(sessions, "s1", "明天下午3点")       # (2026-09-10 15:00) 不可约
    assert FakeProvider.call_count - before == 1

    assert session.cxt.current_node_code == "repair_recommend"
    assert "2026-09-10 09:00-12:00" in reply          # schedule-backed
    assert session.cxt.nlg_result.get("deterministic") is True
    slots = session.cxt.filled_slots
    assert slots.get("bookable") is False
    assert slots.get("requested_time") == "2026-09-10 15:00"

    guard = session.cxt.metadata["unified"].get("booking_guard")
    assert guard == {"requested": "2026-09-10 15:00", "bookable": False,
                     "rerouted_to": "repair_recommend"}


def test_guard_no_schedule_injected_is_noop(pattern, sessions):
    """Guard: without available_slots the guard steps aside."""
    task_info = {k: v for k, v in TASK_INFO.items()
                 if k != "available_slots"}
    session = launch(pattern, sessions, task_info=task_info)
    import time as _time
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))

    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "明天下午3点")
    assert session.cxt.current_node_code == "repair_specific_date"
    assert session.cxt.filled_slots.get("bookable") == "no_schedule"


# ============================================================================
# Time-augmentation end-to-end test
# ============================================================================

def test_time_augmented_query_flows_into_prompt(pattern, sessions):
    """A relative visit time is augmented before the unified prompt."""
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))
    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")

    from apps.repair_booking_agent.stages import RepairBookingUnifiedNLU
    original = RepairBookingUnifiedNLU._call_llm
    captured = {}

    def spy(self, prompt, llm_config=None):
        captured["prompt"] = prompt
        return original(self, prompt, llm_config)

    RepairBookingUnifiedNLU._call_llm = spy
    try:
        chat(sessions, "s1", "明天下午3点方便吗")
    finally:
        RepairBookingUnifiedNLU._call_llm = original

    assert "2026-09-10" in session.cxt.rewritten_queries[0]
    rewrite_section = captured["prompt"].split("### 改写结果", 1)[1]
    assert "2026-09-10" in rewrite_section


# ============================================================================
# Keyword clarify tests (repair FAQ table)
# ============================================================================

def test_clarify_kb_hit_answers_and_stays_on_node(pattern, sessions):
    """Clarify (kb): a repair-fee question mid-negotiation → keyword gate
    hits the repair FAQ entry; the clarify-turn guard keeps the node."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")

    before = FakeProvider.call_count
    reply = chat(sessions, "s1", "维修要收上门费吗")
    assert FakeProvider.call_count - before == 2  # unify + clarify

    meta = session.cxt.metadata["clarify"]
    assert meta["triggered"] is True
    assert meta["mode"] == "kb"
    assert meta["recall_results"][0]["id"] == "faq:费用"
    assert session.cxt.current_node_code == "repair_ask_time"
    assert "topic" not in session.cxt.filled_slots


@pytest.mark.parametrize("query,topic", [
    ("修一次要多少钱", "费用"),
    ("过保了还保修吗", "保修"),
    ("修一次要几个小时", "维修时长"),
    ("师傅带配件吗", "配件"),
    ("我自己修可以吗", "自修咨询"),
    ("师傅什么时候来", "进度查询"),
])
def test_clarify_keyword_table_coverage(pattern, sessions, query, topic):
    """The repair FAQ keyword table covers the six repair question families."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")

    chat(sessions, "s1", query)
    meta = session.cxt.metadata["clarify"]
    assert meta["triggered"] is True, query
    assert meta["mode"] == "kb", query
    assert meta["recall_results"][0]["id"] == f"faq:{topic}", query


def test_clarify_fallback_no_hit(pattern, sessions):
    """Clarify (fallback): an off-flow question the table misses."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")

    reply = chat(sessions, "s1", "你们公司股票代码是多少")
    meta = session.cxt.metadata["clarify"]
    assert meta["triggered"] is True
    assert meta["mode"] == "fallback"
    assert meta["recall_results"] == []
    assert session.cxt.current_node_code == "repair_ask_time"


def test_clarify_turn_then_flow_resumes(pattern, sessions):
    """After a clarify turn the repair main line resumes normally."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要维修")
    chat(sessions, "s1", "地址对的")

    chat(sessions, "s1", "保修多久啊")
    assert session.cxt.current_node_code == "repair_ask_time"

    reply = chat(sessions, "s1", "10月2号上午10点")
    assert session.cxt.current_node_code == "repair_specific_date"
    assert reply.startswith("外呼回复:")
