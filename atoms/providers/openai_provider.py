"""OpenAI-compatible LLM provider.

Supports any OpenAI-compatible API endpoint (OpenAI, Azure, local vLLM, etc.).

Register pattern: call ``registry.register(...)`` at module level so
``discover_builtin_providers()`` picks it up automatically.
"""

import json
import logging
from typing import Any, Dict, Generator, List, Optional

import requests

from nexus.llm.provider import BaseLLMProvider
from nexus.registry.providers import registry

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(BaseLLMProvider):
    """LLM provider for any OpenAI-compatible chat-completion API."""

    def __init__(
        self,
        code: str,
        api_base: str = "",
        api_key: str = "",
        api_key_env: str = "",
        default_model: str = "",
        models: Optional[List[str]] = None,
        timeout: int = 60,
        max_retries: int = 2,
        enable_thinking: bool = False,
        **kwargs,
    ):
        super().__init__(
            code=code,
            api_base=api_base,
            api_key=api_key,
            api_key_env=api_key_env,
            default_model=default_model,
            models=models,
            **kwargs,
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self.enable_thinking = enable_thinking

    def _build_url(self) -> str:
        """Build the full chat-completions URL."""
        base = self.api_base.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def _chat_completion_impl(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call the OpenAI-compatible chat completions endpoint."""
        api_key = self.resolve_api_key()
        url = self._build_url()

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
            # Qwen3 thinking mode switch (DashScope compatible-mode extension parameter), disabled by default
            "enable_thinking": self.enable_thinking,
            **kwargs,
        }

        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()

                # Extract content from the OpenAI response shape
                choices = data.get("choices", [])
                content = ""
                tool_calls = []
                finish_reason = ""
                if choices:
                    msg = choices[0].get("message", {})
                    content = msg.get("content", "") or ""
                    tool_calls = msg.get("tool_calls", []) or []
                    finish_reason = choices[0].get("finish_reason", "")

                return {
                    "content": content,
                    "tool_calls": tool_calls,
                    "model": data.get("model", model),
                    "usage": data.get("usage", {}),
                    "finish_reason": finish_reason,
                    "raw": data,
                }

            except requests.exceptions.RequestException as e:
                last_exc = e
                logger.warning(
                    "Provider '%s' attempt %d/%d failed: %s",
                    self.code,
                    attempt + 1,
                    self.max_retries + 1,
                    e,
                )
                if attempt < self.max_retries:
                    import time
                    time.sleep(1 * (attempt + 1))  # linear backoff

        raise RuntimeError(
            f"Provider '{self.code}' failed after {self.max_retries + 1} attempts: {last_exc}"
        )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _chat_completion_stream_impl(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        **kwargs,
    ) -> Generator[str, None, None]:
        """Stream chat-completion response via SSE, yielding content chunks."""
        api_key = self.resolve_api_key()
        url = self._build_url()

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            # Qwen3 thinking mode switch (DashScope compatible-mode extension parameter), disabled by default
            "enable_thinking": self.enable_thinking,
            **kwargs,
        }

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=self.timeout,
            stream=True,
        )
        response.raise_for_status()

        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            # SSE format: "data: {...}"
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                    choices = data.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                except json.JSONDecodeError:
                    continue


# ---------------------------------------------------------------------------
# Self-register
# ---------------------------------------------------------------------------



registry.register(
    code="openai",
    name="OpenAI",
    description="OpenAI and OpenAI-compatible API provider",
    provider_class=OpenAICompatibleProvider,
    default_model="qwen3.8-max",
    models=["qwen3.7-plus", "qwen3.8-max", "qwen3.8-flash"],
    api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_key_env="DASHSCOPE_API_KEY",
)


