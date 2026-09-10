"""Transport-layer tests for OpenAICompatibleProvider（审查 H-2 回归网）.

全仓唯一真实网络模块此前零传输层测试。用 httpx.MockTransport 注入脚本化
响应，不发出任何真实网络请求。注意公共入口 ``achat_completion`` 统一路由
到流式实现再聚合（plan-⑤），因此：

- 流式重试 / 4xx fail-fast / 耗尽 —— 经公共 ``achat_completion`` 验证（生产路径）
- 非流式 ``_achat_completion_impl`` 的重试 —— 直测实现（默认桥接的兜底路径）
- SSE 解析（text 增量、tool_call 分片、finish_reason、usage-only 尾 chunk、
  坏 JSON 行、非 data 行、[DONE] 终止）—— 直测 ``achat_completion_stream``
"""

import asyncio
import json

import httpx
import pytest

from async_utils import arun
from atoms.providers.openai_provider import OpenAICompatibleProvider

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

    monkeypatch.setattr("atoms.providers.openai_provider.httpx.AsyncClient", _factory)

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
# 流式路径（生产路径：achat_completion → stream → aggregate）
# ============================================================================

def test_stream_retry_then_success(monkeypatch):
    handler = _Flaky(500, fails=2, ok=lambda: httpx.Response(200, content=_ok_sse()))
    calls, sleeps = _install(monkeypatch, handler)
    result = arun(_provider().achat_completion(MESSAGES))
    assert result["content"] == "最终成功"
    assert result["finish_reason"] == "stop"
    assert result["usage"] == {"total_tokens": 7}
    assert handler.n == 3                          # 两次失败 + 第三次成功
    assert sleeps == [1.0, 2.0]                    # 线性退避 1s, 2s


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
    """4xx 表示请求本身错误（鉴权/参数/路径），退避重试只会推迟必然的失败。"""
    calls, _ = _install(monkeypatch, lambda req: httpx.Response(status, text="nope"))
    with pytest.raises(httpx.HTTPStatusError):
        arun(_provider().achat_completion(MESSAGES))
    assert len(calls) == 1                         # 立即失败，不消耗重试


def test_stream_transport_error_retried(monkeypatch):
    """连接层错误（非 HTTPStatusError）仍应走重试路径。"""
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
# 非流式实现（默认桥接兜底路径，此类经公共入口不可达，直测）
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
# SSE 解析（achat_completion_stream 直测）
# ============================================================================

SSE_BODY = "\n".join([
    ": keepalive comment",                                     # 非 data 行 → 跳过
    'data: {"choices":[{"delta":{"content":"你"}}]}',
    'data: {"choices":[{"delta":{"content":"好"}}]}',
    "data: {broken json",                                      # 坏 JSON 行 → 跳过
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
    '"id":"call_1","type":"function","function":{"name":"search",'
    '"arguments":"{\\"q\\": "}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":"\\"天气\\"}"}}]}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    'data: {"choices":[],"usage":{"prompt_tokens":3,'
    '"completion_tokens":9,"total_tokens":12}}',               # usage-only 尾 chunk
    "data: [DONE]",
    'data: {"choices":[{"delta":{"content":"after done"}}]}',  # DONE 后 → 忽略
]) + "\n"


async def _collect(agen):
    return [c async for c in agen]


def test_sse_stream_parsing(monkeypatch):
    _install(monkeypatch, lambda req: httpx.Response(200, content=SSE_BODY.encode()))
    chunks = arun(_collect(_provider().achat_completion_stream(MESSAGES)))

    texts = [c.text for c in chunks if c.text]
    assert texts == ["你", "好"]

    tool_chunks = [c for c in chunks if c.tool_calls]
    assert len(tool_chunks) == 2
    first = tool_chunks[0].tool_calls[0]
    assert first["id"] == "call_1"
    assert first["function"]["name"] == "search"
    assert first["function"]["arguments"] == '{"q": '
    second = tool_chunks[1].tool_calls[0]
    assert second["function"]["arguments"] == '"天气"}'
    # 两片拼接应为合法 tool_call 参数 JSON
    assert json.loads(first["function"]["arguments"] + second["function"]["arguments"]) == {"q": "天气"}

    finishes = [c.finish_reason for c in chunks if c.finish_reason]
    assert finishes == ["tool_calls"]

    usages = [c.usage for c in chunks if c.usage]
    assert usages == [{"prompt_tokens": 3, "completion_tokens": 9, "total_tokens": 12}]

    # [DONE] 之后的内容不得产出 chunk
    assert all(c.text != "after done" for c in chunks)
