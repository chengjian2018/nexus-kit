"""LLM streaming protocol tests — chunk shape, aggregation equivalence
(the aggregation safety net), tool_call fragment merging, and the default
bridge (legacy non-streaming providers wrapped as single-chunk streams)."""

from async_utils import arun
from nexus.llm.aggregate import acollect_stream, collect_stream
from nexus.llm.types import LLMChunk


# ============================================================================
# Aggregation: text / usage / finish_reason
# ============================================================================

def test_text_chunks_concatenate():
    result = collect_stream([
        LLMChunk(text="你好"),
        LLMChunk(text="，"),
        LLMChunk(text="世界", finish_reason="stop"),
        LLMChunk(usage={"prompt_tokens": 3, "completion_tokens": 5}),
    ])
    assert result["content"] == "你好，世界"
    assert result["finish_reason"] == "stop"
    assert result["usage"] == {"prompt_tokens": 3, "completion_tokens": 5}
    assert result["tool_calls"] == []


def test_usage_only_tail_chunk_with_empty_choices():
    """stream_options.include_usage: the usage chunk has choices=[] — it must
    still be consumed (text/tool_calls empty, usage carried)."""
    result = collect_stream([
        LLMChunk(text="ok", finish_reason="stop"),
        LLMChunk(usage={"total_tokens": 9}),
    ])
    assert result["usage"] == {"total_tokens": 9}
    assert result["content"] == "ok"


def test_last_finish_reason_and_usage_win():
    result = collect_stream([
        LLMChunk(finish_reason=""),
        LLMChunk(text="a", finish_reason="length"),
        LLMChunk(usage={"a": 1}),
        LLMChunk(usage={"b": 2}),
    ])
    assert result["finish_reason"] == "length"
    assert result["usage"] == {"b": 2}


# ============================================================================
# Aggregation: tool_calls fragment merging (OpenAI delta semantics)
# ============================================================================

def test_tool_call_fragments_merge_by_index():
    """One tool call split across chunks: id/name from first non-empty,
    arguments string-concatenated (NOT json-merged)."""
    result = collect_stream([
        LLMChunk(tool_calls=[
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "weather", "arguments": ""}},
        ]),
        LLMChunk(tool_calls=[
            {"index": 0, "function": {"arguments": '{"ci'}},
        ]),
        LLMChunk(tool_calls=[
            {"index": 0, "function": {"arguments": 'ty": '}},
        ]),
        LLMChunk(tool_calls=[
            {"index": 0, "function": {"arguments": '"杭州"}'}},
        ], finish_reason="tool_calls"),
    ])
    assert len(result["tool_calls"]) == 1
    tc = result["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["function"]["name"] == "weather"
    assert tc["function"]["arguments"] == '{"city": "杭州"}'
    assert result["finish_reason"] == "tool_calls"
    assert result["content"] == ""


def test_parallel_tool_calls_merge_independently():
    result = collect_stream([
        LLMChunk(tool_calls=[
            {"index": 0, "id": "a", "function": {"name": "t1",
                                                 "arguments": '{"x":'}},
            {"index": 1, "id": "b", "function": {"name": "t2",
                                                 "arguments": '{"y":'}},
        ]),
        LLMChunk(tool_calls=[
            {"index": 1, "function": {"arguments": '1}'}},
            {"index": 0, "function": {"arguments": '1}'}},
        ]),
    ])
    assert [tc["id"] for tc in result["tool_calls"]] == ["a", "b"]
    assert result["tool_calls"][0]["function"]["arguments"] == '{"x":1}'
    assert result["tool_calls"][1]["function"]["arguments"] == '{"y":1}'


def test_mixed_text_and_tool_calls():
    """Content and tool_calls can co-stream (some models emit both)."""
    result = collect_stream([
        LLMChunk(text="让我查一下"),
        LLMChunk(tool_calls=[{"index": 0, "id": "c1",
                              "function": {"name": "q", "arguments": "{}"}}],
                 finish_reason="tool_calls"),
    ])
    assert result["content"] == "让我查一下"
    assert result["tool_calls"][0]["function"]["name"] == "q"


# ============================================================================
# Aggregation equivalence: streamed assembly == legacy non-streaming dict
# (the aggregation safety net — the engine keeps consuming the same shape)
# ============================================================================

def test_aggregation_matches_legacy_shape():
    legacy = {
        "content": "答案文本",
        "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "calc", "arguments": '{"a": 1}'}},
        ],
        "finish_reason": "tool_calls",
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }
    streamed = collect_stream([
        LLMChunk(text="答案"),
        LLMChunk(text="文本"),
        LLMChunk(tool_calls=[
            {"index": 0, "id": "c1", "type": "function",
             "function": {"name": "calc", "arguments": '{"a":'}},
        ]),
        LLMChunk(tool_calls=[
            {"index": 0, "function": {"arguments": ' 1}'}},
        ], finish_reason="tool_calls"),
        LLMChunk(usage={"prompt_tokens": 10, "completion_tokens": 4}),
    ])
    assert streamed == legacy


# ============================================================================
# Default bridge: legacy non-streaming providers wrapped as one-chunk streams
# ============================================================================

async def _collect_chunks(agen):
    return [c async for c in agen]


def test_default_stream_bridge_wraps_nonstreaming():
    from tests.test_llm_streaming import _LegacyProvider

    p = _LegacyProvider()
    chunks = arun(_collect_chunks(p.achat_completion_stream(
        messages=[{"role": "user", "content": "q"}], model="m")))
    assert len(chunks) == 1
    assert chunks[0].text == "完整回复"
    assert chunks[0].finish_reason == "stop"
    assert chunks[0].tool_calls == []


# ============================================================================
# Test fixture: a legacy provider that only implements _achat_completion_impl
# (the FakeProvider situation — zero changes needed to survive the rewrite)
# ============================================================================

class _LegacyProvider:
    """Duck-typed stand-in exercising the base-class bridge without the ABC."""

    def __init__(self):
        self.code = "legacy"

    # The bridge lives on BaseLLMProvider; the test exercises it via a real
    # subclass below, this stub documents the legacy surface.
    async def achat_completion_stream(self, messages, model=None, **kw):
        from nexus.llm.types import LLMChunk
        yield LLMChunk(text="完整回复", finish_reason="stop")


def test_real_base_class_bridge_via_subclass():
    from nexus.llm.provider import BaseLLMProvider

    class _Sub(BaseLLMProvider):
        def __init__(self):
            super().__init__(code="sub", api_base="http://x",
                             api_key="k", default_model="m")

        async def _achat_completion_impl(self, messages, model, temperature,
                                         max_tokens, stream, **kwargs):
            return {"content": "完整回复", "tool_calls": [],
                    "finish_reason": "stop", "usage": {"t": 1}}

    async def _run():
        p = _Sub()
        chunks = [c async for c in p.achat_completion_stream(
            messages=[{"role": "user", "content": "q"}], model="m")]
        assert [c.text for c in chunks if c.text] == ["完整回复"]
        # and the non-streaming entry aggregates back identically
        result = await p.achat_completion(
            messages=[{"role": "user", "content": "q"}], model="m")
        assert result["content"] == "完整回复"
        assert result["finish_reason"] == "stop"
        assert result["usage"] == {"t": 1}
        # async aggregation of the async stream produces the same shape
        agen = p.achat_completion_stream(
            messages=[{"role": "user", "content": "q"}], model="m")
        agg = await acollect_stream(agen)
        assert agg["content"] == "完整回复"
        assert agg["usage"] == {"t": 1}

    arun(_run())
