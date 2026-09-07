"""SessionMessage -- tool-trail JSON payload + summary role + sink pass-through."""

from nexus.context import (
    DialogueContext,
    SessionMessage,
    decode_tool_call_content,
    encode_tool_call_content,
)


# ---------------------------------------------------------------------------
# Payload encoding/decoding
# ---------------------------------------------------------------------------

TOOL_CALLS = [{
    "id": "call_1",
    "type": "function",
    "function": {"name": "weather", "arguments": "{\"city\": \"北京\"}"},
}]


def test_encode_decode_roundtrip():
    payload = encode_tool_call_content("查询中", TOOL_CALLS)
    decoded = decode_tool_call_content(payload)
    assert decoded == ("查询中", TOOL_CALLS)


def test_decode_plain_content_returns_none():
    assert decode_tool_call_content("你好") is None
    assert decode_tool_call_content("") is None
    assert decode_tool_call_content("[已移交至模块 X]") is None  # non-tool-turn text


def test_decode_json_without_tool_calls_returns_none():
    # JSON without a tool_calls key (a plain JSON reply is not misdetected)
    assert decode_tool_call_content('{"content": "你好"}') is None
    # an empty list counts as plain text (the loop only encodes when tool_calls is non-empty)
    assert decode_tool_call_content('{"content": "你好", "tool_calls": []}') is None


def test_decode_malformed_json_returns_none():
    assert decode_tool_call_content('{"content": "截断...') is None
    assert decode_tool_call_content(None) is None


def test_summary_role_is_valid():
    msg = SessionMessage(role="summary", content="对话要点...", stage="compress")
    assert msg.to_dict()["role"] == "summary"


def test_to_from_dict_shape_unchanged():
    """Under the payload scheme, SessionMessage serialization keeps its four fields (the trail lives inside content)."""
    msg = SessionMessage(role="assistant",
                         content=encode_tool_call_content("", TOOL_CALLS),
                         stage="agent")
    d = msg.to_dict()
    assert set(d) == {"role", "content", "stage", "metadata"}
    restored = SessionMessage.from_dict(d)
    assert decode_tool_call_content(restored.content) == ("", TOOL_CALLS)


# ---------------------------------------------------------------------------
# add_message + sink
# ---------------------------------------------------------------------------

def test_message_sink_receives_appended_message():
    received = []
    cxt = DialogueContext(session_id="s1", user_query="q",
                          message_sink=received.append)
    cxt.add_message("user", "你好", stage="chat")
    assert len(received) == 1
    assert received[0] is cxt.history[0]


def test_message_sink_failure_does_not_break_dialogue():
    def bad_sink(msg):
        raise RuntimeError("db down")

    cxt = DialogueContext(session_id="s1", user_query="q", message_sink=bad_sink)
    cxt.add_message("user", "你好", stage="chat")
    assert len(cxt.history) == 1
    assert cxt.history[0].content == "你好"


# ---------------------------------------------------------------------------
# format_history: tool-turn JSON payloads render the inner text
# ---------------------------------------------------------------------------

def test_format_history_decodes_tool_payload():
    cxt = DialogueContext(session_id="s1", user_query="q")
    cxt.add_message("user", "你好", stage="chat")
    cxt.add_message(
        "assistant", encode_tool_call_content("", TOOL_CALLS), stage="agent")
    cxt.add_message("tool", "晴 22 度", stage="agent",
                    metadata={"tool_call_id": "call_1"})
    cxt.add_message(
        "assistant",
        encode_tool_call_content("先查一下天气", TOOL_CALLS),
        stage="agent")
    cxt.add_message("assistant", "北京晴 22 度", stage="chat")

    formatted = cxt.format_history()
    assert "你好" in formatted
    assert "北京晴 22 度" in formatted
    assert "先查一下天气" in formatted      # tool-turn inner text is visible
    assert "tool_calls" not in formatted     # the raw payload does not leak
    lines = formatted.split("\n")
    assert "tool: 晴 22 度" not in lines     # tool rows stay out of the business template
    assert formatted.count("assistant:") == 2  # a tool turn with empty inner text takes no line
