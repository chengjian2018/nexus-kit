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
# Auth — Bearer key resolved from Z_AI_API_KEY
# ============================================================================

def test_api_key_resolved_from_env(monkeypatch):
    for var in ("Z_AI_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("Z_AI_API_KEY", "sk-zai")
    body, url, headers = {}, {}, {}
    _install(monkeypatch, _capture(body, url, headers))
    arun(_provider(api_key="", api_key_env="Z_AI_API_KEY").achat_completion(MESSAGES))
    assert headers["auth"] == "Bearer sk-zai"


# ============================================================================
# Registry metadata — module-level register() wired the entry correctly
# ============================================================================

def test_registry_entry():
    entry = registry.get("zai")
    assert entry is not None
    assert entry.provider_class is ZaiProvider
    assert entry.api_base == CODING_BASE
    assert entry.api_key_env == "Z_AI_API_KEY"
    assert entry.default_model == "glm-5.3-flash"
    assert entry.default_model in entry.models


def test_registry_declares_vision_models():
    """glm-5.3-flash 是视觉模型(注册表声明):supports_vision 三态语义,
    glm-5.3 不在视觉名单(未声明视觉能力的模型绝不收到图像)。"""
    entry = registry.get("zai")
    assert entry.vision_models == ["glm-5.3-flash"]
    assert entry.supports_vision("glm-5.3-flash") is True
    assert entry.supports_vision("glm-5.3") is False
    # 未声明 vision_models 的 provider → None(未知,调用方自行尝试)
    from nexus.llm.provider import ProviderEntry

    class _P:
        pass

    bare = ProviderEntry(code="b", name="b", description="",
                         provider_class=_P, models=["m1"])
    assert bare.supports_vision("m1") is None


def test_payload_carries_multimodal_content_parts_verbatim(monkeypatch):
    """视觉消息透传:OpenAI 风格 content parts 数组(text + image_url
    data URL)原样进请求体,thinking 翻译不影响多模态消息。"""
    body, url, headers = {}, {}, {}
    _install(monkeypatch, _capture(body, url, headers))
    messages = [{
        "role": "user",
        "content": [{"type": "text", "text": "评审这张图"},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64,aGVsbG8="}}],
    }]
    arun(_provider(default_model="glm-5.3-flash").achat_completion(messages))
    assert body["model"] == "glm-5.3-flash"
    assert body["messages"] == messages          # 字节级透传
    assert body["thinking"] == {"type": "disabled"}  # 翻译不碰消息
