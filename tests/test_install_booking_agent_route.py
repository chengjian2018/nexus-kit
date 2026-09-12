"""install_booking_agent (FSM mode) offline tests — transcription of the
hand-drawn FSM template with OUTBOUND-call semantics (the agent proactively
calls the customer to schedule the installation)
+ the supplemented scenarios (generic decline / callback / reschedule) and
the booking-time hard guard.

LLM output is simulated via the scripted FakeProvider (install branch in
_install_unified). Covers:
1. Pattern structure and AST auto-discovery registration (node graph mirrors
   the sketch + supplemented nodes; decline edges from every business node)
2. Outbound opening: the connect turn lands on the outbound-opening node and
   advances on the customer's first response
3. Happy path walk: opening → address confirmation → arrival (arrived) →
   time negotiation (specific date) → confirmation → call close
   (transitions + slot accumulation)
4. The sketch's lateral branches: not-arrived → ETA inquiry (doesn't know)
   → availability check → time negotiation; knows neither → schedule
   recommendation → picks one
5. Terminal edges fire is_end (conversation_end action): address mismatch /
   booking completed
6. Supplemented scenarios:
   - generic decline intents (booking unwanted / already installed / quality
     issue / returned / not the account holder) at any node →
     install_decline → install_end
   - busy now / not ready to book now → install_ask_callback → end
   - confirm-time reschedule → install_reschedule → back to negotiation
7. Booking-time hard guard (stages.InstallBookingUnifiedNLU):
   - bookable request annotated (bookable/matched_slot) and proceeds
   - unbookable request deterministically rerouted to install_recommend with
     a schedule-backed reply (zero extra LLM), slots annotated bookable=False
   - callback times NOT guarded (install_ask_callback exempt)
   - no available_slots injected → guard is a no-op (no_schedule annotation)
   - install_recommend NLG is schedule-backed and deterministic
8. time_aug_query wiring: relative visit times resolved before the prompt
9. validate_pattern passes (app-local stage codes resolve)
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
    "channel": "install_booking",
    "agent_name": "滨江商城售后客服",
    "user_name": "王先生",
    "product_name": "对开门冰箱",
    "order_id": "SO-20260909-001",
    "address": "杭州市滨江区XX路1号2幢301室",
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
    """Discover builtin patterns and return install_booking_agent."""
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.install_booking_agent.route" in imported, (
        f"install_booking_agent 未被自动发现，已发现: {imported}"
    )
    return registry.get("install_booking_agent")


@pytest.fixture()
def sessions():
    """A fresh session container per test."""
    return {}


def launch(pattern, sessions, session_id="s1", task_info=None):
    """Simulate main.py's launch flow: register the outbound-call session
    (task_info carries the order facts + installer schedule the call grounds
    in; available_slots omitted when task_info=None)."""
    from nexus.engine.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = dict(
        TASK_INFO if task_info is None else task_info)
    session.cxt.metadata["llm_override"] = fake_llm_config()
    sessions[session_id] = session
    return session


def chat(sessions, session_id, query):
    """Run one dialogue turn via nexus.engine.chat (one customer utterance)."""
    from async_utils import arun
    from nexus.engine.chat import chat as chat_fn

    return arun(chat_fn(query=query, session_id=session_id, all_sessions=sessions))


def end_actions(cxt):
    """The conversation_end actions accumulated in cxt.actions."""
    return [a for a in cxt.actions if "conversation_end" in a]


def reach_ask_time(sessions, with_arrival="到货了"):
    """Walk the happy prefix (greet → address → arrival) up to visit-time negotiation."""
    chat(sessions, "s1", "方便的，是要安装")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", with_arrival)


# ============================================================================
# Structure tests
# ============================================================================

def test_pattern_auto_discovered_and_structure(pattern):
    """Node graph mirrors the sketch + supplemented nodes; every business
    node carries a decline edge; terminals merge into install_end (is_end)."""
    assert pattern.code == "install_booking_agent"
    assert pattern.entry_node_code == "install_greet"
    assert pattern.pattern_type == "fsm"

    assert [n.code for n in pattern.nodes] == [
        "install_greet", "install_confirm_addr", "install_check_arrival",
        "install_ask_eta", "install_time_window", "install_available",
        "install_ask_time", "install_recommend", "install_specific_date",
        "install_nearest", "install_confirm_time", "install_reschedule",
        "install_ask_callback", "install_callback_default",
        "install_decline", "install_end",
    ]

    # The sketch's labeled edges + supplemented branches as sub_nodes
    sub = {n.code: set(n.sub_nodes) for n in pattern.nodes}
    assert sub["install_greet"] == {"install_confirm_addr", "install_end",
                                    "install_decline"}
    assert sub["install_confirm_addr"] == {"install_check_arrival",
                                           "install_end", "install_decline"}
    assert sub["install_check_arrival"] == {"install_ask_time",
                                            "install_ask_eta",
                                            "install_decline"}
    assert sub["install_ask_eta"] == {"install_time_window",
                                      "install_available", "install_decline"}
    assert sub["install_time_window"] == {"install_ask_time",
                                          "install_available",
                                          "install_decline"}
    assert sub["install_available"] == {"install_ask_time",
                                        "install_ask_callback",
                                        "install_decline"}
    assert sub["install_ask_time"] == {"install_recommend",
                                       "install_specific_date",
                                       "install_nearest", "install_decline"}
    assert sub["install_recommend"] == {"install_specific_date",
                                        "install_nearest", "install_ask_time",
                                        "install_decline"}
    assert sub["install_specific_date"] == {"install_confirm_time",
                                            "install_ask_time",
                                            "install_decline"}
    assert sub["install_nearest"] == {"install_confirm_time",
                                      "install_ask_time", "install_decline"}
    assert sub["install_confirm_time"] == {"install_end",
                                           "install_reschedule",
                                           "install_decline"}
    assert sub["install_reschedule"] == {"install_ask_time",
                                         "install_decline"}
    assert sub["install_ask_callback"] == {"install_end",
                                           "install_callback_default",
                                           "install_decline"}
    assert sub["install_callback_default"] == {"install_end"}
    assert sub["install_decline"] == {"install_end"}
    assert sub["install_end"] == set()

    # The terminal ovals merged into one is_end node
    assert pattern.node_map["install_end"].is_end is True
    assert not any(n.is_end for n in pattern.nodes[:-1])

    # Every sub_nodes target exists (dangling edges would fail Pattern init)
    for n in pattern.nodes:
        for target in n.sub_nodes:
            assert target in pattern.node_map


def test_stage_wiring(pattern):
    """Pattern-skeleton guarded unified pair + keyword clarify + builtin
    time_aug; node-level stages only carry the clarify admission switch
    (the recommend rewrite lives in the unified stage — see the timing note
    in route.py)."""
    skeleton_values = {slot: code for e in pattern.stages
                       for slot, code in e.items()}
    assert skeleton_values == {"query": "time_aug_query",
                                "nlu": "install_unified",
                                "clarify": "install_clarify",
                                "nlg": "nlg_pass_through"}

    for node in pattern.nodes:
        assert node.stages in ({}, {"clarify": "install_clarify"}), (
            f"node {node.code} 不应携带除 clarify 准入开关外的节点级 stages"
            "（推荐改写已并入 install_unified，节点级 nlg 会因 FSM 轮末转移"
            "时序晚一轮生效）"
        )
    assert skeleton_values.get("query") == "time_aug_query"
    assert skeleton_values.get("nlu") == "install_unified"
    assert skeleton_values.get("clarify") == "install_clarify"
    assert skeleton_values.get("nlg") == "nlg_pass_through"

    # All declared codes resolve (app-local codes registered by stages.py,
    # pulled in by route.py's bottom import)
    from nexus.registry.plugins import registry as plugin_registry
    assert plugin_registry.has("stage", "install_unified")
    assert plugin_registry.has("stage", "install_recommend_nlg")
    assert plugin_registry.has("stage", "install_clarify")
    assert plugin_registry.has("stage", "nlg_pass_through")
    assert plugin_registry.has("stage", "time_aug_query")


def test_module_prompt_override_carries_task_info(pattern, sessions):
    """The module-level template override adds the task-info section (with
    the schedule) and the special-intent routing guidance."""
    session = launch(pattern, sessions)

    # Spy on the unified stage's own _call_llm (it bypasses BaseNLU's)
    from apps.install_booking_agent.stages import InstallBookingUnifiedNLU
    original = InstallBookingUnifiedNLU._call_llm
    captured = {}

    def spy(self, prompt, llm_config=None):
        captured["prompt"] = prompt
        return original(self, prompt, llm_config)

    InstallBookingUnifiedNLU._call_llm = spy
    try:
        chat(sessions, "s1", "喂，你好")
    finally:
        InstallBookingUnifiedNLU._call_llm = original

    prompt = captured["prompt"]
    assert "### 任务信息" in prompt
    assert "对开门冰箱" in prompt
    assert "杭州市滨江区XX路1号2幢301室" in prompt
    assert "2026-09-10 09:00-12:00" in prompt       # available_slots injected
    assert "主动外呼" in prompt                      # outbound persona
    assert "特殊意图识别" in prompt                   # decline/callback guidance
    # The valid next_node set for the entry node
    assert "install_confirm_addr" in prompt
    assert "install_decline" in prompt


def test_pattern_passes_validation(pattern):
    """The transcription validates cleanly (base info + plugin declarations)."""
    from nexus.model.validation import validate_pattern

    validate_pattern(pattern)  # no raise


# ============================================================================
# Outbound opening tests
# ============================================================================

def test_connect_turn_opens_call_and_advances(pattern, sessions):
    """Outbound opening: the connect turn runs on the outbound-opening node
    (module_nodes[0]); the customer's first response advances to address
    confirmation."""
    session = launch(pattern, sessions)
    reply = chat(sessions, "s1", "喂，你好，方便的你说")

    assert session.cxt.current_node_code == "install_confirm_addr"
    assert reply == "外呼回复: 地址核对"


# ============================================================================
# Happy-path walk tests (sketch main flow)
# ============================================================================

def test_happy_path_full_walk(pattern, sessions):
    """Main flow: opening → address confirmation → arrival (arrived) → time
    negotiation (specific date) → confirmation → call close; slots
    accumulate in filled_slots; one LLM call per turn (unified).

    The picked date has no time annotation (no time_aug hit) → guard is a
    no-op pass-through; booking proceeds."""
    session = launch(pattern, sessions)

    steps = [
        ("方便的，是要安装", "install_confirm_addr", "service_needed"),
        ("对的是这个地址", "install_check_arrival", "address_confirmed"),
        ("已经到货了", "install_ask_time", "arrived"),
        ("10月1号下午3点吧", "install_specific_date", "visit_date"),
        ("可以的没问题", "install_confirm_time", "visit_date"),
        ("好的确认", "install_end", "visit_time"),
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

    # Terminal: entering install_end fired the conversation_end action
    assert end_actions(session.cxt)

    # Slot accumulation across the walk (incremental merge per transition)
    assert session.cxt.filled_slots.get("service_needed") == "方便的，是要安装"
    assert session.cxt.filled_slots.get("arrived") == "已经到货了"
    assert session.cxt.filled_slots.get("visit_time") == "已约定"


# ============================================================================
# Lateral-branch tests (the sketch's side edges)
# ============================================================================

def test_not_arrived_eta_unknown_path(pattern, sessions):
    """Sketch lateral path: not arrived → arrival time unknown → convenient → time negotiation (nearest) → confirm → close."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要安装")     # → address confirmation
    chat(sessions, "s1", "地址对的")             # → arrival check
    chat(sessions, "s1", "还没到货")             # → ETA inquiry (negative edge)
    assert session.cxt.current_node_code == "install_ask_eta"
    chat(sessions, "s1", "不知道什么时候到")      # does not know → availability check
    assert session.cxt.current_node_code == "install_available"
    chat(sessions, "s1", "方便的")               # → time negotiation
    assert session.cxt.current_node_code == "install_ask_time"
    reply = chat(sessions, "s1", "最近的就行")    # nearest branch
    assert session.cxt.current_node_code == "install_nearest"
    assert reply == "外呼回复: 最近档期安排"
    chat(sessions, "s1", "可以")                 # → time confirmation
    assert session.cxt.current_node_code == "install_confirm_time"
    chat(sessions, "s1", "确认")                 # → call close
    assert session.cxt.current_node_code == "install_end"
    assert end_actions(session.cxt)


def test_eta_known_time_window_path(pattern, sessions):
    """Sketch lateral path: knows arrival time → provides a time window → time negotiation → recommendation → picks one."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要安装")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "还没到货")
    chat(sessions, "s1", "大概周五到")           # knows → time-window inquiry
    assert session.cxt.current_node_code == "install_time_window"
    chat(sessions, "s1", "周末上午都行")         # provides a window → time negotiation
    assert session.cxt.current_node_code == "install_ask_time"
    chat(sessions, "s1", "你们看着安排吧")       # knows neither → schedule recommendation
    assert session.cxt.current_node_code == "install_recommend"
    reply = chat(sessions, "s1", "第一个不错")   # picks one → specific-date booking
    assert session.cxt.current_node_code == "install_specific_date"
    assert reply == "外呼回复: 具体日期约定"


def test_time_window_not_provided_falls_to_available(pattern, sessions):
    """Sketch edge: no time window provided → availability check (instead of going straight into time negotiation)."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要安装")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "还没到货")
    chat(sessions, "s1", "大概周五到")
    chat(sessions, "s1", "说不好时间段")         # not provided → availability check
    assert session.cxt.current_node_code == "install_available"


# ============================================================================
# Terminal-edge tests
# ============================================================================

def test_address_mismatch_ends(pattern, sessions):
    """Sketch terminal edge: address mismatch → call close (conversation_end action fired)."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要安装")
    reply = chat(sessions, "s1", "地址不对")      # negative → call close

    assert session.cxt.current_node_code == "install_end"
    assert reply == "外呼回复: 通话结束语"
    assert end_actions(session.cxt)


# ============================================================================
# Supplemented-scenario tests: generic decline / callback / reschedule
# ============================================================================

@pytest.mark.parametrize("decline_query", [
    "不需要安装了，别约了",       # does not want to book
    "已经装过了",                # already installed
    "冰箱有质量问题",            # quality issue
    "我已经退货了",              # returned
    "我不是本人，打错了",        # not the account holder
])
def test_generic_decline_at_any_node(pattern, sessions, decline_query):
    """Supplemented: the five decline intents, heard mid-flow, land on
    install_decline (empathetic beat) then install_end (goodbye beat)."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要安装")
    chat(sessions, "s1", "地址对的")

    reply = chat(sessions, "s1", decline_query)
    assert session.cxt.current_node_code == "install_decline", decline_query
    assert reply == "外呼回复: 通用拒绝承接"
    assert session.cxt.nlu_result["slots"]["decline_reason"] == decline_query

    reply = chat(sessions, "s1", "嗯好的")
    assert session.cxt.current_node_code == "install_end"
    assert reply == "外呼回复: 通话结束语"
    assert end_actions(session.cxt)


def test_decline_heard_at_opening(pattern, sessions):
    """Supplemented: the decline intent at the very first turn also routes
    through install_decline (greet has the edge)."""
    session = launch(pattern, sessions)
    reply = chat(sessions, "s1", "已经安装过了不用来了")

    assert session.cxt.current_node_code == "install_decline"
    assert reply == "外呼回复: 通用拒绝承接"


def test_not_available_now_books_callback(pattern, sessions):
    """Supplemented: not available (busy now, install not declined) → callback
    time → the customer gives a valid future time (Friday afternoon, within
    the 2-week window) → close on the customer's time."""
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))
    chat(sessions, "s1", "方便的，是要安装")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "还没到货")
    chat(sessions, "s1", "不知道到货时间")
    reply = chat(sessions, "s1", "最近都不方便")  # negative edge → callback time
    assert session.cxt.current_node_code == "install_ask_callback"
    assert reply == "外呼回复: 下次联系时间"

    reply = chat(sessions, "s1", "周五下午再打给我")  # (2026-09-11) valid future time
    assert session.cxt.current_node_code == "install_end"
    slots = session.cxt.filled_slots
    assert slots.get("callback_source") == "customer"
    assert "2026-09-11" in slots.get("callback_time", "")
    assert "2026-09-11" in reply          # the spoken reply restates the customer time
    assert end_actions(session.cxt)


def test_confirm_time_reschedule_loops(pattern, sessions):
    """Supplemented: reschedule after confirmation → reschedule renegotiation → back to time negotiation to rebook."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "方便的，是要安装")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "到货了")
    chat(sessions, "s1", "10月1号下午3点")       # → specific-date booking
    chat(sessions, "s1", "可以的没问题")          # → time confirmation
    assert session.cxt.current_node_code == "install_confirm_time"

    reply = chat(sessions, "s1", "时间想改一下")  # reschedule → reschedule renegotiation
    assert session.cxt.current_node_code == "install_reschedule"
    assert reply == "外呼回复: 改约重协商"

    chat(sessions, "s1", "嗯重新约")             # → time negotiation
    assert session.cxt.current_node_code == "install_ask_time"
    chat(sessions, "s1", "10月2号上午10点")       # rebooked
    assert session.cxt.current_node_code == "install_specific_date"


# ============================================================================
# Booking-time guard tests (stages.InstallBookingUnifiedNLU)
# ============================================================================

def _walk_to_specific_date(sessions, query):
    """Greet → address → arrival → time negotiation → specific date."""
    reach_ask_time(sessions)
    return chat(sessions, "s1", query)


def test_guard_bookable_time_annotated(pattern, sessions):
    """Guard: a bookable request (inside a window) is annotated
    (bookable=True + matched_slot) and the transition proceeds; the recommend
    NLG does not interfere (specific-date node uses pass-through NLG)."""
    session = launch(pattern, sessions)

    import time as _time
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))

    _walk_to_specific_date(sessions, "明天上午10点")   # (2026-09-10 10:00)
    assert session.cxt.current_node_code == "install_specific_date"

    slots = session.cxt.filled_slots
    assert slots.get("bookable") is True
    assert slots.get("matched_slot") == "2026-09-10 09:00-12:00"
    assert slots.get("requested_time") == "2026-09-10 10:00"


def test_guard_unbookable_time_reroutes_to_recommend(pattern, sessions):
    """Guard: an unbookable request is deterministically rerouted to
    install_recommend with a schedule-backed reply (zero extra LLM) and
    bookable=False annotations."""
    session = launch(pattern, sessions)

    import time as _time
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))

    reach_ask_time(sessions)                        # walk to time negotiation (3 turns)
    before = FakeProvider.call_count
    reply = chat(sessions, "s1", "明天下午3点")       # (2026-09-10 15:00)
    assert FakeProvider.call_count - before == 1     # only the unified call

    # Rerouted to the recommend node (NOT specific_date)
    assert session.cxt.current_node_code == "install_recommend"
    # Deterministic schedule-backed reply (the recommend NLG ran, zero LLM)
    assert "2026-09-10 09:00-12:00" in reply
    assert "2026-09-11 14:00-17:00" in reply
    assert session.cxt.nlg_result.get("deterministic") is True

    slots = session.cxt.filled_slots
    assert slots.get("bookable") is False
    assert slots.get("requested_time") == "2026-09-10 15:00"

    # Observability: the guard wrote its trace into metadata["unified"]
    guard = session.cxt.metadata["unified"].get("booking_guard")
    assert guard == {"requested": "2026-09-10 15:00", "bookable": False,
                     "rerouted_to": "install_recommend"}


def test_guard_partial_window_still_unbookable(pattern, sessions):
    """Guard: a request overlapping but exceeding a window (9:00~13:00 vs
    09:00-12:00) is unbookable — the installer cannot stay past the window."""
    session = launch(pattern, sessions)

    import time as _time
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))

    reply = _walk_to_specific_date(sessions, "明天上午9点到下午1点")
    assert session.cxt.current_node_code == "install_recommend"
    assert session.cxt.filled_slots.get("bookable") is False


def test_guard_skips_callback_times(pattern, sessions):
    """Guard: install_ask_callback answers are next-CONTACT times, not visit
    times — never matched against the installer's schedule (no bookable
    annotations even though the time misses every window). The far time
    (>2 weeks, unannotated) still rides the callback-default reroute."""
    session = launch(pattern, sessions)
    import time as _time
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))

    chat(sessions, "s1", "方便的，是要安装")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "还没到货")
    chat(sessions, "s1", "不知道到货时间")
    chat(sessions, "s1", "现在没空，晚点再说")    # → callback time
    # A callback time FAR outside the installer's windows — the booking
    # guard never fires (no bookable/matched_slot annotations); the
    # callback triage reroutes it to the default-callback node instead
    chat(sessions, "s1", "明年1月再说")           # > 2 weeks out
    assert session.cxt.current_node_code == "install_callback_default"
    slots = session.cxt.nlu_result.get("slots", {})
    assert "bookable" not in slots
    assert "matched_slot" not in slots


def test_guard_no_schedule_injected_is_noop(pattern, sessions):
    """Guard: without available_slots in task_info the guard steps aside
    (no_schedule annotation; the model's pick stands)."""
    task_info = {k: v for k, v in TASK_INFO.items()
                 if k != "available_slots"}
    session = launch(pattern, sessions, task_info=task_info)

    import time as _time
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))

    _walk_to_specific_date(sessions, "明天下午3点")
    assert session.cxt.current_node_code == "install_specific_date"
    assert session.cxt.filled_slots.get("bookable") == "no_schedule"


def test_recommend_nlg_schedule_backed(pattern, sessions):
    """The recommend node's NLG always speaks the real schedule (task_info's
    available_slots), zero LLM — including a fresh model-driven landing
    (knows neither → recommendation)."""
    session = launch(pattern, sessions)
    reach_ask_time(sessions)

    before = FakeProvider.call_count
    reply = chat(sessions, "s1", "你们看着安排吧")   # → schedule recommendation
    assert session.cxt.current_node_code == "install_recommend"
    assert FakeProvider.call_count - before == 1     # only the unified call

    assert "2026-09-10 09:00-12:00" in reply
    assert session.cxt.nlg_result.get("deterministic") is True


# ============================================================================
# Time-augmentation end-to-end test (query slot -> unified prompt)
# ============================================================================

def test_time_augmented_query_flows_into_prompt(pattern, sessions):
    """A visit-time reply carrying relative time is augmented by
    TimeAugQueryRewriter and lands in the unified prompt's rewrite-result section."""
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))

    chat(sessions, "s1", "方便的，是要安装")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "到货了")

    from apps.install_booking_agent.stages import InstallBookingUnifiedNLU
    original = InstallBookingUnifiedNLU._call_llm
    captured = {}

    def spy(self, prompt, llm_config=None):
        captured["prompt"] = prompt
        return original(self, prompt, llm_config)

    InstallBookingUnifiedNLU._call_llm = spy
    try:
        chat(sessions, "s1", "明天下午3点方便吗")
    finally:
        InstallBookingUnifiedNLU._call_llm = original

    # The rewrite resolved the relative time (tomorrow -> 2026-09-10) before the
    # unified prompt; the prompt's rewrite-result section carries the annotation
    assert "2026-09-10" in session.cxt.rewritten_queries[0]
    rewrite_section = captured["prompt"].split("### 改写结果", 1)[1]
    assert "2026-09-10" in rewrite_section


# ============================================================================
# Custom clarify stage tests (keyword-gated, install_clarify)
# ============================================================================

def test_clarify_kb_hit_answers_and_stays_on_node(pattern, sessions):
    """Clarify (kb): an off-flow fee question mid-negotiation triggers the
    clarify signal → keyword gate hits the FAQ entry → the LLM answer rides
    the kb template with the FAQ answer pre-filled; the clarify-turn guard
    keeps the node (no jump, no slot pollution)."""
    session = launch(pattern, sessions)
    reach_ask_time(sessions)

    before = FakeProvider.call_count
    reply = chat(sessions, "s1", "安装完成之后要收钱吗")
    assert FakeProvider.call_count - before == 2  # unify call + clarify call

    meta = session.cxt.metadata["clarify"]
    assert meta["triggered"] is True
    assert meta["mode"] == "kb"
    assert meta["recall_results"][0]["id"] == "faq:费用"
    assert reply == "澄清解答: 收费问题已答复，咱们继续约时间。"

    # Clarify-turn guard: node unchanged, clarify slots never landed in
    # filled_slots (topic/keywords stay out)
    assert session.cxt.current_node_code == "install_ask_time"
    assert "topic" not in session.cxt.filled_slots
    assert "keywords" not in session.cxt.filled_slots


def test_clarify_fallback_no_hit(pattern, sessions):
    """Clarify (fallback): an off-flow question the keyword table misses →
    honest-acknowledge template; node still unchanged."""
    session = launch(pattern, sessions)
    reach_ask_time(sessions)

    reply = chat(sessions, "s1", "你们公司股票代码是多少")
    meta = session.cxt.metadata["clarify"]
    assert meta["triggered"] is True
    assert meta["mode"] == "fallback"
    assert meta["recall_results"] == []
    assert reply == "澄清兜底: 这个问题稍后核实，咱们继续约时间。"
    assert session.cxt.current_node_code == "install_ask_time"


@pytest.mark.parametrize("query,topic", [
    ("装完要收费吗", "费用"),
    ("保修多久啊", "保修"),
    ("装一次要几个小时", "安装时长"),
    ("我自己装可以吗", "自装咨询"),
    ("我要换个地址安装", "改地址"),
    ("物流怎么这么慢", "催物流"),
])
def test_clarify_keyword_table_coverage(pattern, sessions, query, topic):
    """The FAQ keyword table covers the six outbound-call question families;
    each lands on its entry (specific-first containment)."""
    session = launch(pattern, sessions)
    reach_ask_time(sessions)

    chat(sessions, "s1", query)
    meta = session.cxt.metadata["clarify"]
    assert meta["triggered"] is True, query
    assert meta["mode"] == "kb", query
    assert meta["recall_results"][0]["id"] == f"faq:{topic}", query


def test_clarify_kb_answer_grounds_in_task_info(pattern, sessions):
    """The FAQ answer's {product_name} placeholder is substituted with the
    task_info value before the LLM call (code owns the facts)."""
    session = launch(pattern, sessions)
    reach_ask_time(sessions)

    from apps.install_booking_agent.stages import KeywordClarifyStage
    original = KeywordClarifyStage._generate
    captured = {}

    def spy(self, prompt, llm_config=None):
        captured["prompt"] = prompt
        return original(self, prompt, llm_config)

    KeywordClarifyStage._generate = spy
    try:
        chat(sessions, "s1", "安装要收费吗")
    finally:
        KeywordClarifyStage._generate = original

    prompt = captured["prompt"]
    assert "FAQ 答案" in prompt
    assert "对开门冰箱" in prompt          # {product_name} substituted
    assert "{product_name}" not in prompt  # no unsubstituted placeholder left


def test_clarify_turn_then_flow_resumes(pattern, sessions):
    """After a clarify turn the booking main line resumes: the next customer
    answer transitions normally (the clarify signal is per-turn)."""
    session = launch(pattern, sessions)
    reach_ask_time(sessions)

    chat(sessions, "s1", "保修多久啊")            # clarify turn (stays)
    assert session.cxt.current_node_code == "install_ask_time"

    reply = chat(sessions, "s1", "10月2号上午10点")   # normal negotiation
    assert session.cxt.current_node_code == "install_specific_date"
    assert reply.startswith("外呼回复:")
    meta = session.cxt.metadata["clarify"]
    assert meta["triggered"] is False          # reset on the non-clarify turn


# ============================================================================
# Callback-time close tests (three branches after the callback-time node)
# ============================================================================

def _reach_callback(sessions):
    """Walk to the callback node: greet → address → not-arrived → eta
    unknown → not available now."""
    chat(sessions, "s1", "方便的，是要安装")
    chat(sessions, "s1", "地址对的")
    chat(sessions, "s1", "还没到货")
    chat(sessions, "s1", "不知道到货时间")
    chat(sessions, "s1", "最近都不方便")


def test_callback_valid_time_closes_on_customer_time(pattern, sessions):
    """Branch 2: a valid future time within 2 weeks (next Monday, annotated) →
    close restating the customer's time; callback_source=customer."""
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))
    _reach_callback(sessions)

    reply = chat(sessions, "s1", "下周一再打给我")   # (2026-09-14)
    assert session.cxt.current_node_code == "install_end"
    slots = session.cxt.filled_slots
    assert slots.get("callback_source") == "customer"
    assert "2026-09-14" in slots.get("callback_time", "")
    assert "2026-09-14" in reply
    assert "再联系您" in reply
    assert end_actions(session.cxt)


def test_callback_too_far_falls_back_to_default(pattern, sessions):
    """Branch 1: a time beyond 2 weeks (the 20th of next month — outside the
    annotation window, unannotated) → rerouted to install_callback_default
    (default 3-days proposal, far date never announced); the customer's
    answer closes the call (two beats)."""
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))
    _reach_callback(sessions)

    reply = chat(sessions, "s1", "下个月20号再打给我")   # > 2 weeks out
    assert session.cxt.current_node_code == "install_callback_default"
    slots = session.cxt.filled_slots
    assert slots.get("callback_source") == "default"
    assert slots.get("callback_time") == "2026-09-12"   # 2026-09-09 + 3d
    assert "2026-09-12" in reply                        # the default proposal
    assert "2026-10" not in reply                       # far date never announced
    guard = session.cxt.metadata["unified"]["callback_guard"]
    assert guard["rerouted_to"] == "install_callback_default"
    assert not end_actions(session.cxt)                 # not closed yet (beat 1)

    reply = chat(sessions, "s1", "行吧就这样")          # beat 2: answer → close
    assert session.cxt.current_node_code == "install_end"
    assert "2026-09-12" in reply                        # default restated
    assert end_actions(session.cxt)


def test_callback_past_time_falls_back_to_default(pattern, sessions):
    """Branch 1 (past): a past time (last Friday, unannotated) → default node."""
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))
    _reach_callback(sessions)

    reply = chat(sessions, "s1", "上周五下午再打")       # past → unannotated
    assert session.cxt.current_node_code == "install_callback_default"
    assert session.cxt.filled_slots.get("callback_time") == "2026-09-12"
    assert "2026-09-12" in reply

    chat(sessions, "s1", "好的可以")
    assert session.cxt.current_node_code == "install_end"
    assert end_actions(session.cxt)


def test_callback_vague_or_missing_falls_back_to_default(pattern, sessions):
    """Branch 3: any time / no time given → default node → close on answer."""
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-09 10:00:00", "%Y-%m-%d %H:%M:%S"))
    _reach_callback(sessions)

    reply = chat(sessions, "s1", "都行你们看着打")       # vague
    assert session.cxt.current_node_code == "install_callback_default"
    assert session.cxt.filled_slots.get("callback_source") == "default"

    chat(sessions, "s1", "嗯没问题")
    assert session.cxt.current_node_code == "install_end"
    assert end_actions(session.cxt)
