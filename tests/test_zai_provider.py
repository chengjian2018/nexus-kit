"""ZaiProvider wiring tests — URL shape, GLM thinking translation, auth and
registry metadata. Scripted responses via httpx.MockTransport, no real
network (same harness as test_dashscope_provider_transport.py)."""

import json

import httpx

from async_utils import arun
from atoms.providers.zai_provider import ZaiProvider  # imports register the "zai" entry
from nexus.registry.providers import registry

MESSAGES = [{"role": "user", "content": "hi"}]

CODING_BASE = "https://open.bigmodel.cn/api/coding/paas/v4"


def _install(monkeypatch, handler):
    """Point the provider's AsyncClient at a MockTransport."""
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("atoms.providers.dashscope_provider.httpx.AsyncClient", _factory)


def _ok_sse(content: str = "好的") -> bytes:
    mid = content[:-1] if len(content) > 1 else content
    return "\n".join([
        f'data: {json.dumps({"choices": [{"delta": {"content": mid}}]})}',
        f'data: {json.dumps({"choices": [{"delta": {"content": content[-1]}, "finish_reason": "stop"}]})}',
        f'data: {json.dumps({"choices": [], "usage": {"total_tokens": 7}})}',
        "data: [DONE]",
    ]).encode() + b"\n"


def _provider(**kw) -> ZaiProvider:
    defaults = dict(
        code="t",
        api_base=CODING_BASE,
        api_key="k",
        default_model="glm-4.7",
        timeout=5,
    )
    defaults.update(kw)
    return ZaiProvider(**defaults)


def _capture(body: dict, url: dict, headers: dict):
    def handler(req: httpx.Request) -> httpx.Response:
        body.update(json.loads(req.content))
        url["value"] = str(req.url)
        # read via httpx.Headers (case-insensitive) before it hits a plain dict
        headers["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, content=_ok_sse())
    return handler


# ============================================================================
# URL shape — coding-plan base carries /v4, endpoint appends directly
# ============================================================================

def test_url_appends_to_versioned_coding_base(monkeypatch):
    body, url, headers = {}, {}, {}
    _install(monkeypatch, _capture(body, url, headers))
    result = arun(_provider().achat_completion(MESSAGES))
    assert url["value"] == f"{CODING_BASE}/chat/completions"
    assert result["content"] == "好的"


# ============================================================================
# Thinking translation — enable_thinking → GLM native thinking.type
# ============================================================================

def test_payload_swaps_enable_thinking_for_glm_thinking(monkeypatch):
    body, url, headers = {}, {}, {}
    _install(monkeypatch, _capture(body, url, headers))
    arun(_provider().achat_completion(MESSAGES))
    assert "enable_thinking" not in body          # DashScope param must not leak
    assert body["thinking"] == {"type": "disabled"}  # default: thinking off


def test_payload_thinking_enabled_flag(monkeypatch):
    body, url, headers = {}, {}, {}
    _install(monkeypatch, _capture(body, url, headers))
    arun(_provider(enable_thinking=True).achat_completion(MESSAGES))
    assert body["thinking"] == {"type": "enabled"}


def test_payload_explicit_thinking_kwarg_wins(monkeypatch):
    body, url, headers = {}, {}, {}
    _install(monkeypatch, _capture(body, url, headers))
    arun(_provider().achat_completion(
        MESSAGES, thinking={"type": "enabled"},
    ))
    assert body["thinking"] == {"type": "enabled"}  # call-site kwarg not clobbered


def test_payload_keeps_stream_options(monkeypatch):
    body, url, headers = {}, {}, {}
    _install(monkeypatch, _capture(body, url, headers))
    arun(_provider().achat_completion(MESSAGES))
    assert body["stream_options"] == {"include_usage": True}


# ============================================================================
# Auth — Bearer key resolved from ZAI_API_KEY
# ============================================================================

def test_api_key_resolved_from_env(monkeypatch):
    for var in ("ZAI_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ZAI_API_KEY", "sk-zai")
    body, url, headers = {}, {}, {}
    _install(monkeypatch, _capture(body, url, headers))
    arun(_provider(api_key="", api_key_env="ZAI_API_KEY").achat_completion(MESSAGES))
    assert headers["auth"] == "Bearer sk-zai"


# ============================================================================
# Registry metadata — module-level register() wired the entry correctly
# ============================================================================

def test_registry_entry():
    entry = registry.get("zai")
    assert entry is not None
    assert entry.provider_class is ZaiProvider
    assert entry.api_base == CODING_BASE
    assert entry.api_key_env == "ZAI_API_KEY"
    assert entry.default_model == "glm-5.3"
    assert entry.default_model in entry.models
