"""TurnLifecycle / ChatResult offline unit tests (no LLM dependency)."""

from nexus.engine.context_lifecycle import TurnLifecycle
from nexus.engine.response import ChatResult, build_chat_result
from nexus.context import DialogueContext, ModuleJumpEvent


def _make_cxt() -> DialogueContext:
    """Build a cxt in a "leftover from the previous turn" state: every kind of field non-empty."""
    cxt = DialogueContext(session_id="s1", user_query="旧问题")
    cxt.current_module_code = "m1"
    cxt.current_node_code = "n1"
    cxt.filled_slots = {"price": "100"}
    cxt.task_basic_info = {"city": "杭州"}
    cxt.nlu_result = {"intent": "old"}
    cxt.nlg_result = {"content": "旧回复"}
    cxt.agent_result = {"reply": "旧agent回复"}
    cxt.pre_recall_results = [{"doc": "旧"}]
    cxt.rewritten_queries = ["旧改写"]
    cxt.post_recall_results = [{"doc": "旧2"}]
    cxt.actions = [{"type": "old_action"}]
    cxt.metadata = {
        "bargain_settings": {"max_rounds": 3},
        "task_info": {"order_id": "o1"},
        "llm_override": {"model": "x"},
        "pattern_code": "p1",
        "unified": {"used": True},
        "clarify": {"triggered": True, "topic": "旧主题"},
        "served_by_projection": {"module": "m1", "source": "m0"},
    }
    cxt.add_message("user", "旧问题", stage="chat")
    return cxt


class TestBeginTurn:
    def test_user_query_overwritten(self):
        cxt = _make_cxt()
        TurnLifecycle().begin_turn(cxt, "新问题")
        assert cxt.user_query == "新问题"

    def test_per_turn_result_fields_reset(self):
        cxt = _make_cxt()
        TurnLifecycle().begin_turn(cxt, "新问题")
        assert cxt.nlu_result is None
        assert cxt.nlg_result is None
        assert cxt.agent_result is None

    def test_per_turn_list_fields_reset(self):
        cxt = _make_cxt()
        TurnLifecycle().begin_turn(cxt, "新问题")
        assert cxt.pre_recall_results == []
        assert cxt.rewritten_queries == []
        assert cxt.post_recall_results == []
        assert cxt.actions == []

    def test_per_turn_metadata_keys_popped(self):
        cxt = _make_cxt()
        TurnLifecycle().begin_turn(cxt, "新问题")
        for key in ("unified", "clarify", "served_by_projection"):
            assert key not in cxt.metadata

    def test_persistent_fields_survive(self):
        cxt = _make_cxt()
        history_len = len(cxt.history)
        TurnLifecycle().begin_turn(cxt, "新问题")
        assert cxt.current_module_code == "m1"
        assert cxt.current_node_code == "n1"
        assert cxt.filled_slots == {"price": "100"}
        assert cxt.task_basic_info == {"city": "杭州"}
        assert len(cxt.history) == history_len
        assert cxt.node_map == {} and cxt.module_map == {}  # original references kept

    def test_persistent_metadata_keys_survive(self):
        cxt = _make_cxt()
        TurnLifecycle().begin_turn(cxt, "新问题")
        for key in ("bargain_settings", "task_info",
                    "llm_override", "pattern_code"):
            assert key in cxt.metadata

    def test_idempotent_between_turns(self):
        """Two consecutive begin_turn calls (simulating two turns) behave consistently."""
        cxt = _make_cxt()
        lc = TurnLifecycle()
        lc.begin_turn(cxt, "q1")
        lc.end_turn(cxt, "回复1")
        lc.begin_turn(cxt, "q2")
        assert cxt.user_query == "q2"
        assert cxt.nlu_result is None
        assert len(cxt.history) == 2  # old user + assistant reply 1; q2's user row is added by the chat layer

    def test_turn_history_start_snapshots_history_length(self):
        """begin_turn snapshots turn_history_start (the history length before the user row is added)."""
        cxt = _make_cxt()  # already contains 1 user row
        lc = TurnLifecycle()
        lc.begin_turn(cxt, "q1")
        assert cxt.turn_history_start == 1
        cxt.add_message("user", "q1", stage="chat")
        lc.end_turn(cxt, "回复1")
        lc.begin_turn(cxt, "q2")
        assert cxt.turn_history_start == 3  # old user + q1 + reply 1


class TestEndTurn:
    def test_appends_assistant_message(self):
        cxt = _make_cxt()
        TurnLifecycle().end_turn(cxt, "最终回复")
        last = cxt.history[-1]
        assert last.role == "assistant"
        assert last.content == "最终回复"
        assert last.stage == "chat"


class TestMergeSlots:
    def test_merge_overwrites_same_key(self):
        cxt = _make_cxt()
        TurnLifecycle().merge_slots(cxt, {"price": "200", "color": "红"})
        assert cxt.filled_slots == {"price": "200", "color": "红"}

    def test_empty_slots_noop(self):
        cxt = _make_cxt()
        TurnLifecycle().merge_slots(cxt, {})
        assert cxt.filled_slots == {"price": "100"}


class TestChatResult:
    def test_build_snapshots_actions(self):
        cxt = _make_cxt()
        result = build_chat_result("回复文本", cxt)
        assert result.text == "回复文本"
        assert result.actions == [{"type": "old_action"}]

    def test_build_snapshots_jump_event_as_dict(self):
        """A leftover ModuleJumpEvent (unconsumed after exceeding the hop limit) is snapshotted as an observation dict."""
        cxt = _make_cxt()
        cxt.actions.append(ModuleJumpEvent(
            target_module_code="m2", reason="r", source="nlu_jump"))
        result = build_chat_result("t", cxt)
        assert result.actions == [
            {"type": "old_action"},
            {"module_jump": {"target": "m2", "reason": "r",
                             "source": "nlu_jump"}},
        ]

    def test_build_with_empty_cxt(self):
        cxt = DialogueContext(session_id="s", user_query="q")
        result = build_chat_result("t", cxt)
        assert result == ChatResult(text="t", actions=[])

    def test_snapshot_is_copy_not_reference(self):
        """The snapshot must be a copy: resetting cxt.actions at turn start must not affect an already-built ChatResult."""
        cxt = _make_cxt()
        result = build_chat_result("t", cxt)
        cxt.actions.clear()
        assert result.actions == [{"type": "old_action"}]
