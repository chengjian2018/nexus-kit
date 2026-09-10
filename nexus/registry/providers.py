"""Central registry for all LLM providers.

Each provider file calls ``registry.register()`` at module level to declare
its provider class, default model, API endpoint, and metadata.  The rest of
the system queries the registry instead of hard-coding provider references.

Import chain (circular-import safe):
    llm/register.py  (no imports from provider files)
           ^
    llm/*_provider.py  (import from nexus.registry.providers at module level)
           ^
    chat/chat.py, main.py, etc.
"""

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional

from nexus.llm.provider import BaseLLMProvider, ProviderEntry
from nexus.registry.discovery import import_modules, module_registers

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auto-discovery helpers
# ---------------------------------------------------------------------------

def discover_builtin_providers(providers_dir: Optional[Path] = None) -> List[str]:
    """Import self-registering provider modules under atoms/providers/ and return their names."""
    providers_path = (
        Path(providers_dir) if providers_dir is not None
        else Path(__file__).resolve().parents[2] / "atoms" / "providers"
    )
    module_names = [
        f"atoms.providers.{path.stem}"
        for path in sorted(providers_path.glob("*.py"))
        if path.name != "__init__.py"
        and module_registers(path)
    ]
    return import_modules(module_names, what="provider module")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class LLMProviderRegistry:
    """Singleton registry that collects LLM provider metadata."""

    def __init__(self):
        self._providers: Dict[str, ProviderEntry] = {}
        self._active_provider_code: Optional[str] = None
        self._lock = threading.RLock()
        self._generation: int = 0

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
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
    ) -> None:
        """Register an LLM provider.

        Called at module-import time by each provider file.

        Parameters
        ----------
        code:
            Unique provider code (e.g. ``"openai"``, ``"azure"``).
        name:
            Human-readable name (e.g. ``"OpenAI"``).
        description:
            Short description of the provider.
        provider_class:
            A ``BaseLLMProvider`` subclass.
        default_model:
            Default model name when none is specified at call time.
        models:
            List of model names this provider supports.
        api_base:
            Base URL for the provider's API endpoint.
        api_key_env:
            Environment variable name that holds the API key.
        extra_config:
            Arbitrary extra configuration passed to the provider constructor.
        """
        with self._lock:
            if code in self._providers:
                logger.warning(
                    "Provider '%s' is already registered; overwriting.", code
                )

            self._providers[code] = ProviderEntry(
                code=code,
                name=name,
                description=description,
                provider_class=provider_class,
                default_model=default_model,
                models=models,
                api_base=api_base,
                api_key=api_key,
                api_key_env=api_key_env,
                extra_config=extra_config,
            )
            self._generation += 1
            logger.info("Registered LLM provider: %s (%s)", code, name)

    def deregister(self, code: str) -> None:
        """Remove a provider from the registry."""
        with self._lock:
            if code not in self._providers:
                return
            del self._providers[code]
            if self._active_provider_code == code:
                self._active_provider_code = None
            self._generation += 1
        logger.info("Deregistered LLM provider: %s", code)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get(self, code: str) -> Optional[ProviderEntry]:
        """Return a registered provider entry by code, or None."""
        with self._lock:
            return self._providers.get(code)

    def list_providers(self) -> List[ProviderEntry]:
        """Return all registered provider entries."""
        with self._lock:
            return list(self._providers.values())

    def list_codes(self) -> List[str]:
        """Return sorted list of registered provider codes."""
        with self._lock:
            return sorted(self._providers.keys())

    def is_registered(self, code: str) -> bool:
        """Check whether a provider with *code* is registered."""
        with self._lock:
            return code in self._providers

    # ------------------------------------------------------------------
    # Active provider
    # ------------------------------------------------------------------

    @property
    def active_provider_code(self) -> Optional[str]:
        """The currently active provider code, or None."""
        with self._lock:
            return self._active_provider_code

    def set_active(self, code: str) -> None:
        """Set the active provider by code.

        Raises ``ValueError`` if *code* is not registered.
        """
        with self._lock:
            if code not in self._providers:
                raise ValueError(
                    f"Provider '{code}' is not registered. "
                    f"Available: {sorted(self._providers.keys())}"
                )
            self._active_provider_code = code
            self._generation += 1
        logger.info("Active LLM provider set to: %s", code)

    def get_active(self) -> Optional[ProviderEntry]:
        """Return the entry for the active provider, or None."""
        with self._lock:
            if self._active_provider_code is None:
                return None
            return self._providers.get(self._active_provider_code)

    def get_active_provider(self) -> Optional[BaseLLMProvider]:
        """Return an *instantiated* active provider, or None."""
        entry = self.get_active()
        if entry is None:
            return None
        return entry.instantiate()

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def instantiate(self, code: str, **overrides) -> BaseLLMProvider:
        """Instantiate a registered provider by code.

        Raises ``ValueError`` if *code* is not registered.
        """
        entry = self.get(code)
        if entry is None:
            raise ValueError(
                f"Provider '{code}' is not registered. "
                f"Available: {self.list_codes()}"
            )
        return entry.instantiate(**overrides)

    async def achat_completion(
        self,
        messages: List[Dict[str, Any]],
        code: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        stream: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Dispatch a chat-completion request to the active provider (or *code*).

        If *code* is omitted the currently active provider is used.
        Raises ``ValueError`` when no provider can be resolved.
        """
        resolved_code = code or self._active_provider_code
        if resolved_code is None:
            raise ValueError(
                "No provider specified and no active provider is set. "
                "Call registry.set_active(...) first or pass code=..."
            )
        entry = self.get(resolved_code)
        if entry is None:
            raise ValueError(
                f"Provider '{resolved_code}' is not registered. "
                f"Available: {self.list_codes()}"
            )
        return await entry.achat_completion(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            **kwargs,
        )

    async def achat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        code: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs,
    ):
        """Dispatch a streaming chat request to the active provider (or *code*)."""
        resolved_code = code or self._active_provider_code
        if resolved_code is None:
            raise ValueError(
                "No provider specified and no active provider is set. "
                "Call registry.set_active(...) first or pass code=..."
            )
        entry = self.get(resolved_code)
        if entry is None:
            raise ValueError(
                f"Provider '{resolved_code}' is not registered. "
                f"Available: {self.list_codes()}"
            )
        agen = entry.achat_completion_stream(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        async for chunk in agen:
            yield chunk




# Module-level singleton
registry = LLMProviderRegistry()