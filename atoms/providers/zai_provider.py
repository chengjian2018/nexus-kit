"""Z.ai (Zhipu BigModel) GLM provider — GLM Coding Plan via the
OpenAI-compatible protocol.

GLM Coding Plan exposes three protocol endpoints:

    Anthropic Message        https://open.bigmodel.cn/api/anthropic
    OpenAI Chat Completion   https://open.bigmodel.cn/api/coding/paas/v4
    OpenAI Response          https://open.bigmodel.cn/api/v1

This framework speaks chat-completions, so the OpenAI Chat Completion
endpoint is the fit (``_build_url`` appends ``/chat/completions`` to the
``…/v4`` base directly). The registered ``api_base`` targets the Coding Plan
endpoint — Coding Plan quota is ONLY spendable there, not on the pay-as-you-go
``…/api/paas/v4``; override ``api_base`` / ``api_key_env`` in
host/config/local_config.yaml (``llm_providers.zai``) to point elsewhere.

Wire difference vs the dashscope provider: GLM has no DashScope
``enable_thinking`` extension — the flag is translated to GLM's native
``thinking.type`` switch instead (``disabled`` by default, matching the
dashscope provider's default).

glm-5.3-flash is a vision model: messages whose ``content`` is an
OpenAI-style parts array (``[{"type": "text", ...}, {"type": "image_url",
"image_url": {"url": "data:image/png;base64, ..."}}, ...]``) pass through
to the API verbatim — the chat-completions body carries content parts
untouched, so no payload translation is needed here (see
nexus/llm/vision.py for part builders).

Register pattern: call ``registry.register(...)`` at module level so
``discover_builtin_providers()`` picks it up automatically.
"""

from typing import Any, Dict, List

from atoms.providers.dashscope_provider import OpenAICompatibleProvider
from nexus.registry.providers import registry


class ZaiProvider(OpenAICompatibleProvider):
    """LLM provider for the GLM (Z.ai / BigModel) OpenAI-compatible API."""

    def _build_payload(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool,
        **kwargs,
    ) -> Dict[str, Any]:
        payload = super()._build_payload(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            **kwargs,
        )
        # GLM speaks its own thinking switch, not DashScope's enable_thinking;
        # an explicit ``thinking`` kwarg from the call site still wins.
        enabled = payload.pop("enable_thinking", False)
        payload.setdefault(
            "thinking", {"type": "enabled" if enabled else "disabled"}
        )
        return payload


# ---------------------------------------------------------------------------
# Self-register
# ---------------------------------------------------------------------------

registry.register(
    code="zai",
    name="Z.ai GLM (Coding Plan)",
    description="Zhipu GLM Coding Plan via the OpenAI-compatible protocol",
    provider_class=ZaiProvider,
    default_model="glm-5.3-flash",
    models=["glm-5.3", "glm-5.3-flash"],
    vision_models=["glm-5.3-flash"],
    api_base="https://open.bigmodel.cn/api/coding/paas/v4",
    api_key_env="Z_AI_API_KEY",
)
