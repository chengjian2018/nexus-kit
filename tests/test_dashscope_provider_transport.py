"""Transport-layer tests for OpenAICompatibleProvider (audit H-2 regression
net).

The repo's only real-network module previously had zero transport-layer
tests. Scripted responses are injected via httpx.MockTransport — no real
network requests are made. Note the public entry ``achat_completion`` routes
uniformly to the streaming implementation and then aggregates,
hence:

- Streaming retry / 4xx fail-fast / exhaustion — verified through the public
  ``achat_completion`` (production path)
- Non-streaming ``_achat_completion_impl`` retry — implementation tested
  directly (the default bridge's fallback path)
- SSE parsing (text deltas, tool_call fragments, finish_reason, usage-only
  tail chunk, broken JSON lines, non-data lines, [DONE] termination) —
  ``achat_completion_stream`` tested directly
"""

import asyncio
import json

import httpx
import pytest

from async_utils import arun
from atoms.providers.dashscope_provider import OpenAICompatibleProvider

MESSAGES = [{"role": "user", "content": "hi"}]


def _provider(max_retries: int = 2) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        code="t",
        api_base="http://t.local/v1",
        api_key="k",
        default_model="m",
        timeout=5,
        max_retries=max_retries,
    )


def _install(monkeypatch, handler):
    """Point the provider's AsyncClient at a MockTransport. Returns
    ``(calls, sleeps)`` — the request log and the recorded backoff delays
    (sleep is stubbed out so retry tests don't actually wait)."""
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("atoms.providers.dashscope_provider.httpx.AsyncClient", _factory)

    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return calls, sleeps


def _ok_body(content: str = "最终成功") -> dict:
    return {
        "choices": [{
            "message": {"content": content, "tool_calls": []},
            "finish_reason": "stop",
        }],
        "model": "m",
        "usage": {"total_tokens": 7},
    }


def _ok_sse(content: str = "最终成功") -> bytes:
    mid = content[:-1] if len(content) > 1 else content
    return "\n".join([
        f'data: {json.dumps({"choices": [{"delta": {"content": mid}}]})}',
        f'data: {json.dumps({"choices": [{"delta": {"content": content[-1]}, "finish_reason": "stop"}]})}',
        f'data: {json.dumps({"choices": [], "usage": {"total_tokens": 7}})}',
        "data: [DONE]",
    ]).encode() + b"\n"


class _Flaky:
    """Handler script: fails with `status` for the first `fails` calls, then
    succeeds with `ok`."""

    def __init__(self, status: int, fails: int, ok):
        self.status, self.fails, self.ok = status, fails, ok
        self.n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.n += 1
        if self.n <= self.fails:
            return httpx.Response(self.status, text="boom")
        return self.ok() if callable(self.ok) else self.ok


# ============================================================================
# Streaming path (production path: achat_completion → stream → aggregate)
# ============================================================================

def test_stream_retry_then_success(monkeypatch):
    handler = _Flaky(500, fails=2, ok=lambda: httpx.Response(200, content=_ok_sse()))
    calls, sleeps = _install(monkeypatch, handler)
    result = arun(_provider().achat_completion(MESSAGES))
    assert result["content"] == "最终成功"
    assert result["finish_reason"] == "stop"
    assert result["usage"] == {"total_tokens": 7}
    assert handler.n == 3                          # two failures + third succeeds
    assert sleeps == [1.0, 2.0]                    # linear backoff 1s, 2s


def test_stream_retry_exhausted(monkeypatch):
    handler = _Flaky(500, fails=99, ok=lambda: httpx.Response(200))
    calls, _ = _install(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        arun(_provider(max_retries=2).achat_completion(MESSAGES))
    assert len(calls) == 3                         # 1 + max_retries


def test_stream_429_retried(monkeypatch):
    handler = _Flaky(429, fails=1, ok=lambda: httpx.Response(200, content=_ok_sse()))
    calls, _ = _install(monkeypatch, handler)
    result = arun(_provider().achat_completion(MESSAGES))
    assert result["content"] == "最终成功"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_stream_non_retryable_4xx_fails_fast(monkeypatch, status):
    """4xx means the request itself is wrong (auth/params/path); backoff retries only postpone the inevitable failure."""
    calls, _ = _install(monkeypatch, lambda req: httpx.Response(status, text="nope"))
    with pytest.raises(httpx.HTTPStatusError):
        arun(_provider().achat_completion(MESSAGES))
    assert len(calls) == 1                         # fails immediately, consumes no retries


def test_stream_transport_error_retried(monkeypatch):
    """Connection-layer errors (not HTTPStatusError) should still go through the retry path."""
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] < 2:
            raise httpx.ConnectError("conn refused")
        return httpx.Response(200, content=_ok_sse())

    _install(monkeypatch, handler)
    result = arun(_provider().achat_completion(MESSAGES))
    assert result["content"] == "最终成功"
    assert state["n"] == 2


def test_request_carries_auth_and_payload(monkeypatch):
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=_ok_sse())

    _install(monkeypatch, handler)
    arun(_provider().achat_completion(MESSAGES, model="m2"))
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "m2"
    assert seen["body"]["messages"] == MESSAGES
    assert seen["body"]["stream"] is True


# ============================================================================
# Non-streaming implementation (default-bridge fallback path; unreachable through the public entry, tested directly)
# ============================================================================

def _call_impl(p, **kw):
    return p._achat_completion_impl(
        messages=MESSAGES, model="m", temperature=0.7,
        max_tokens=100, stream=False, **kw,
    )


def test_impl_retry_then_success(monkeypatch):
    handler = _Flaky(500, fails=2, ok=lambda: httpx.Response(200, json=_ok_body()))
    calls, sleeps = _install(monkeypatch, handler)
    result = arun(_call_impl(_provider()))
    assert result["content"] == "最终成功"
    assert handler.n == 3
    assert sleeps == [1.0, 2.0]


def test_impl_non_retryable_4xx_fails_fast(monkeypatch):
    calls, _ = _install(monkeypatch, lambda req: httpx.Response(401, text="nope"))
    with pytest.raises(httpx.HTTPStatusError):
        arun(_call_impl(_provider()))
    assert len(calls) == 1


def test_impl_retry_exhausted(monkeypatch):
    calls, _ = _install(monkeypatch, lambda req: httpx.Response(500, text="down"))
    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        arun(_call_impl(_provider(max_retries=2)))
    assert len(calls) == 3


# ============================================================================
# SSE parsing (achat_completion_stream tested directly)
# ============================================================================

SSE_BODY = "\n".join([
    ": keepalive comment",                                     # non-data line → skipped
    'data: {"choices":[{"delta":{"reasoning_content":"让我"}}]}',   # thinking delta
    'data: {"choices":[{"delta":{"reasoning_content":"想想"}}]}',   # thinking delta
    'data: {"choices":[{"delta":{"content":"你"}}]}',
    'data: {"choices":[{"delta":{"content":"好"}}]}',
    "data: {broken json",                                      # broken JSON line → skipped
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
    '"id":"call_1","type":"function","function":{"name":"search",'
    '"arguments":"{\\"q\\": "}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":"\\"天气\\"}"}}]}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    'data: {"choices":[],"usage":{"prompt_tokens":3,'
    '"completion_tokens":9,"total_tokens":12}}',               # usage-only tail chunk
    "data: [DONE]",
    'data: {"choices":[{"delta":{"content":"after done"}}]}',  # after DONE → ignored
]) + "\n"


async def _collect(agen):
    return [c async for c in agen]


def test_sse_stream_parsing(monkeypatch):
    _install(monkeypatch, lambda req: httpx.Response(200, content=SSE_BODY.encode()))
    chunks = arun(_collect(_provider().achat_completion_stream(MESSAGES)))

    texts = [c.text for c in chunks if c.text]
    assert texts == ["你", "好"]

    # thinking-model deltas land in .reasoning, never folded into .text
    reasonings = [c.reasoning for c in chunks if c.reasoning]
    assert reasonings == ["让我", "想想"]
    assert all(not c.reasoning for c in chunks if c.text)

    tool_chunks = [c for c in chunks if c.tool_calls]
    assert len(tool_chunks) == 2
    first = tool_chunks[0].tool_calls[0]
    assert first["id"] == "call_1"
    assert first["function"]["name"] == "search"
    assert first["function"]["arguments"] == '{"q": '
    second = tool_chunks[1].tool_calls[0]
    assert second["function"]["arguments"] == '"天气"}'
    # the two fragments concatenated must form valid tool_call arguments JSON
    assert json.loads(first["function"]["arguments"] + second["function"]["arguments"]) == {"q": "天气"}

    finishes = [c.finish_reason for c in chunks if c.finish_reason]
    assert finishes == ["tool_calls"]

    usages = [c.usage for c in chunks if c.usage]
    assert usages == [{"prompt_tokens": 3, "completion_tokens": 9, "total_tokens": 12}]

    # content after [DONE] must not produce a chunk
    assert all(c.text != "after done" for c in chunks)
