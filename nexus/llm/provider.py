"""Base LLM provider abstract classes.

Defines the contract that every LLM provider must implement:
- ``BaseLLMProvider``: the core interface for chat-completion requests.
- ``ProviderEntry``: metadata record for a registered provider.
"""

import os
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Generator, List, Optional


class ProviderEntry:
    """Metadata for a single registered LLM provider."""

    __slots__ = (
        "code",
        "name",
        "description",
        "provider_class",
        "default_model",
        "models",
        "api_base",
        "api_key",
        "api_key_env",
        "extra_config",
    )

    def __init__(
        self,
        code: str,
        name: str,
        description: str,
        provider_class: type,
        default_model: str = "",
        models: Optional[List[str]] = None,
        api_base: str = "",
        api_key: str = "",
        api_key_env: str = "",
        extra_config: Optional[Dict[str, Any]] = None,
    ):
        self.code = code
        self.name = name
        self.description = description
        self.provider_class = provider_class
        self.default_model = default_model
        self.models = models or []
        self.api_base = api_base
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.extra_config = extra_config or {}

    def instantiate(self, **overrides) -> "BaseLLMProvider":
        """Create an instance of the provider with optional config overrides."""
        config = {
            "api_base": self.api_base,
            "api_key": self.api_key,
            "api_key_env": self.api_key_env,
            "default_model": self.default_model,
            "models": list(self.models),
            **self.extra_config,
            **overrides,
        }
        return self.provider_class(code=self.code, **config)

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        stream: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Convenience: instantiate the provider and call chat_completion in one shot.

        Each call creates a fresh provider instance (no state is retained).
        """
        provider = self.instantiate()
        return provider.chat_completion(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            **kwargs,
        )

    def chat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs,
    ) -> Generator[str, None, None]:
        """Convenience: instantiate and stream the response chunk by chunk."""
        provider = self.instantiate()
        yield from provider.chat_completion_stream(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    def check_connection(
        self,
        model: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Convenience: instantiate the provider and probe its connectivity."""
        provider = self.instantiate()
        return provider.check_connection(model=model, **kwargs)


class BaseLLMProvider(ABC):
    """Abstract base for all LLM providers.

    Subclasses must implement at least ``_chat_completion_impl``.

    API key resolution order (highest priority first):
        1. ``api_key`` kwarg passed directly to constructor
        2. ``api_key_env`` environment variable
        3. Common fallback env vars: ``OPENAI_API_KEY``, ``LLM_API_KEY``
    """

    def __init__(
        self,
        code: str,
        api_base: str = "",
        api_key: str = "",
        api_key_env: str = "",
        default_model: str = "",
        models: Optional[List[str]] = None,
        **kwargs,
    ):
        self.code = code
        self.api_base = api_base
        self._api_key = api_key
        self.api_key_env = api_key_env
        self.default_model = default_model
        self.models = models or []
        self.extra = kwargs

    def resolve_api_key(self) -> str:
        """Resolve the API key in priority order.

        Override in subclasses for custom resolution logic.
        """
        # 1. Direct value (highest priority)
        if self._api_key:
            return self._api_key

        # 2. Named environment variable
        if self.api_key_env:
            key = os.environ.get(self.api_key_env, "")
            if key:
                return key

        # 3. Common fallbacks
        for env_var in ("OPENAI_API_KEY", "LLM_API_KEY"):
            key = os.environ.get(env_var, "")
            if key:
                return key

        return ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        stream: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Public entry point for chat-completion requests.

        Validates inputs, resolves the model, and delegates to
        ``_chat_completion_impl``.
        """
        if not messages:
            raise ValueError("messages must be a non-empty list")

        resolved_model = model or self.default_model
        if not resolved_model:
            raise ValueError(
                f"No model specified and provider '{self.code}' has no default_model"
            )

        return self._chat_completion_impl(
            messages=messages,
            model=resolved_model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            **kwargs,
        )

    @abstractmethod
    def _chat_completion_impl(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool,
        **kwargs,
    ) -> Dict[str, Any]:
        """Provider-specific implementation of a chat-completion call.

        Must return a dict with at least ``{"content": str}``.
        May also include ``{"usage": {...}, "model": str, ...}``.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Streaming API
    # ------------------------------------------------------------------

    def chat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs,
    ) -> Generator[str, None, None]:
        """Stream chat-completion response, yielding content chunks as they arrive.

        Usage::

            for chunk in provider.chat_completion_stream(messages):
                print(chunk, end="", flush=True)
        """
        if not messages:
            raise ValueError("messages must be a non-empty list")

        resolved_model = model or self.default_model
        if not resolved_model:
            raise ValueError(
                f"No model specified and provider '{self.code}' has no default_model"
            )

        yield from self._chat_completion_stream_impl(
            messages=messages,
            model=resolved_model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    def _chat_completion_stream_impl(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        **kwargs,
    ) -> Generator[str, None, None]:
        """Provider-specific streaming implementation. Override in subclasses.

        Default: falls back to non-streaming, yielding the whole content at once.
        """
        result = self._chat_completion_impl(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            **kwargs,
        )
        yield result["content"]

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def check_connection(
        self,
        model: Optional[str] = None,
        prompt: str = "ping",
        max_tokens: int = 16,
        **kwargs,
    ) -> Dict[str, Any]:
        """Probe the provider with a minimal request to verify it is reachable.

        Never raises — any failure (missing model, bad key, network error,
        HTTP error) is reported in the returned dict instead.

        Returns a dict with:
            ``ok``          -- True when the provider answered
            ``provider``    -- provider code
            ``model``       -- the model actually probed
            ``has_api_key`` -- whether an API key could be resolved
            ``latency_ms``  -- round-trip time in milliseconds
            ``content``     -- the reply (truncated), empty on failure
            ``error``       -- ``"<ExcType>: <message>"``, empty on success

        Override in subclasses that expose a cheaper health endpoint
        (e.g. ``GET /v1/models``).
        """
        resolved_model = model or self.default_model
        result: Dict[str, Any] = {
            "ok": False,
            "provider": self.code,
            "model": resolved_model,
            "has_api_key": bool(self.resolve_api_key()),
            "latency_ms": 0.0,
            "content": "",
            "error": "",
        }

        started = time.perf_counter()
        try:
            response = self.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=resolved_model,
                temperature=0.0,
                max_tokens=max_tokens,
                stream=False,
                **kwargs,
            )
            result["ok"] = True
            result["model"] = response.get("model", resolved_model)
            result["content"] = (response.get("content") or "")[:200]
        except Exception as e:  # connectivity probe must never propagate
            result["error"] = f"{type(e).__name__}: {e}"
        finally:
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)

        return result

    def is_available(self, model: Optional[str] = None, **kwargs) -> bool:
        """Return True when the provider answers a minimal probe request."""
        return self.check_connection(model=model, **kwargs)["ok"]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def list_models(self) -> List[str]:
        """Return the list of models advertised by this provider."""
        return list(self.models)

    def supports_model(self, model: str) -> bool:
        """Check whether *model* is in the advertised model list."""
        return model in self.models

    def __repr__(self) -> str:
        return f"<{type(self).__name__} code={self.code!r} model={self.default_model!r}>"