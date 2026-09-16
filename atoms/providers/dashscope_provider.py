"""OpenAI-compatible LLM provider — registered for Alibaba Cloud DashScope.

``OpenAICompatibleProvider`` itself is generic: any OpenAI-compatible chat
endpoint works (OpenAI, Azure, local vLLM, etc.); the module-level
registration below binds it to Alibaba Cloud DashScope's compatible mode.

Register pattern: call ``registry.register(...)`` at module level so
``discover_builtin_providers()`` picks it up automatically.

Async since the asyncio rewrite: httpx.AsyncClient, a fresh client per call
(the requests top-level API behaviour — no cross-loop pooled connections;
a loop-keyed client cache is a possible later optimization).
"""

import asyncio
import json
import logging
import re
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from nexus.llm.provider import BaseLLMProvider
from nexus.llm.types import LLMChunk
from nexus.registry.providers import registry

logger = logging.getLogger(__name__)

# Retryable HTTP statuses: request-timeout + rate-limit + all 5xx.
# Other 4xx (401/403/404/400…) mean the request itself is wrong — retrying
# with backoff just delays the inevitable failure.
_RETRYABLE_STATUSES = frozenset({408, 429})


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUSES or status_code >= 500


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
        # Versioned bases (…/v1 OpenAI/dashscope, …/v4 zhipu/zai api/paas)
        # already carry the version segment — append the endpoint directly
        if re.search(r"/v\d+$", base):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def _build_headers(self) -> Dict[str, str]:
        api_key = self.resolve_api_key()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _build_payload(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool,
        **kwargs,
    ) -> Dict[str, Any]:
        """Build the chat-completions request body (subclass hook: providers
        for vendors with different extension parameters override this —
        e.g. zai swaps DashScope's ``enable_thinking`` for GLM's ``thinking``).
        """
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
        if stream and "stream_options" not in payload:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def _achat_completion_impl(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call the OpenAI-compatible chat completions endpoint."""
        url = self._build_url()

        payload = self._build_payload(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            **kwargs,
        )

        last_exc = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    response = await client.post(
                        url,
                        headers=self._build_headers(),
                        json=payload,
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

                except httpx.HTTPError as e:
                    # Non-retryable 4xx (auth/bad request): fail fast instead
                    # of burning retries — HTTPStatusError carries .response,
                    # transport-level errors don't and stay retryable.
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    if status is not None and not _is_retryable_status(status):
                        raise
                    last_exc = e
                    logger.warning(
                        "Provider '%s' attempt %d/%d failed: %s",
                        self.code,
                        attempt + 1,
                        self.max_retries + 1,
                        e,
                    )
                    if attempt < self.max_retries:
                        await asyncio.sleep(1 * (attempt + 1))  # linear backoff

        raise RuntimeError(
            f"Provider '{self.code}' failed after {self.max_retries + 1} attempts: {last_exc}"
        )

    # ------------------------------------------------------------------
    # Streaming (native: yields structured LLMChunks)
    # ------------------------------------------------------------------

    async def _achat_completion_stream_impl(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        **kwargs,
    ) -> AsyncGenerator["LLMChunk", None]:
        """Stream chat-completion response via SSE, yielding LLMChunk
        objects (text deltas / tool_call fragments / finish_reason / usage /
        thinking-model reasoning deltas).

        Wire quirks handled:
        - ``stream_options.include_usage``: the usage arrives as a final
          chunk whose ``choices`` is an EMPTY array — consumed here, not
          skipped
        - ``delta.tool_calls``: id/name appear on the first fragment of a
          slot, arguments arrive as string fragments — passed through
          as-is; merging is the aggregator's job (llm/aggregate.py)
        - ``delta.reasoning_content``: GLM/Qwen thinking-mode deltas —
          surfaced as ``LLMChunk.reasoning``, never merged into ``text``
        - ``[DONE]`` sentinel terminates the stream

        Retry policy: transport errors and retryable statuses (408/429/5xx)
        are retried with linear backoff — but only until the first chunk is
        yielded; a mid-stream failure propagates instead (a retry would
        replay already-delivered content). Non-retryable 4xx fails fast.
        """
        url = self._build_url()

        payload = self._build_payload(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            **kwargs,
        )

        # Retry shares the non-streaming policy (transport errors + 408/429/5xx
        # with linear backoff), but ONLY until the first chunk is yielded —
        # past that point a retry would replay already-delivered content.
        streamed_any = False
        last_exc = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    async with client.stream(
                        "POST", url, headers=self._build_headers(), json=payload,
                    ) as response:
                        response.raise_for_status()

                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            # SSE format: "data: {...}"
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            usage = data.get("usage") or {}
                            choices = data.get("choices", [])
                            if not choices:
                                # usage-only tail chunk (include_usage): choices == []
                                if usage:
                                    streamed_any = True
                                    yield LLMChunk(usage=usage)
                                continue

                            choice = choices[0]
                            delta = choice.get("delta", {}) or {}
                            text = delta.get("content", "") or ""
                            tool_calls = delta.get("tool_calls", []) or []
                            finish_reason = choice.get("finish_reason", "") or ""
                            # thinking-model deltas (GLM/Qwen thinking mode):
                            # reasoning text travels in its own field and must
                            # NOT be folded into content
                            reasoning = (delta.get("reasoning_content", "") or ""
                                         or delta.get("reasoning", "") or "")
                            if text or tool_calls or finish_reason or reasoning:
                                streamed_any = True
                                yield LLMChunk(text=text, tool_calls=tool_calls,
                                               finish_reason=finish_reason,
                                               reasoning=reasoning)
                            elif usage:
                                streamed_any = True
                                yield LLMChunk(usage=usage)
                    return

                except httpx.HTTPError as e:
                    if streamed_any:
                        raise  # mid-stream failure: retry would duplicate content
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    if status is not None and not _is_retryable_status(status):
                        raise  # non-retryable 4xx: fail fast
                    last_exc = e
                    logger.warning(
                        "Provider '%s' stream attempt %d/%d failed: %s",
                        self.code,
                        attempt + 1,
                        self.max_retries + 1,
                        e,
                    )
                    if attempt < self.max_retries:
                        await asyncio.sleep(1 * (attempt + 1))  # linear backoff

        raise RuntimeError(
            f"Provider '{self.code}' failed after {self.max_retries + 1} attempts: {last_exc}"
        )


# ---------------------------------------------------------------------------
# Self-register
# ---------------------------------------------------------------------------



registry.register(
    code="dashscope",
    name="阿里云百炼 (DashScope)",
    description="阿里云百炼 DashScope Qwen via the OpenAI-compatible protocol",
    provider_class=OpenAICompatibleProvider,
    default_model="qwen3.8-max",
    models=["qwen3.7-plus", "qwen3.8-max", "qwen3.8-flash"],
    api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_key_env="DASHSCOPE_API_KEY",
)
