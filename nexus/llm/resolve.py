"""Unified LLM provider resolution.

``build_provider(llm_config)`` is the single entry point that turns an
``llm`` config dict (as loaded from ``config/local_config.yaml``) into a
configured provider instance:

- provider selection: ``llm_config["code"]`` -> registered ``ProviderEntry``
  (built-in providers are auto-discovered on first miss)
- config overrides: the non-empty ``api_base`` / ``api_key`` / ``api_key_env``
  / ``timeout`` / ``max_retries`` / ``enable_thinking`` fields are forwarded
  to ``entry.instantiate(**overrides)``, so the yaml values take effect over
  the provider's registered defaults

Import chain (circular-import safe): resolve.py imports only from
``nexus.llm.provider`` and ``nexus.registry.providers``; nothing in those modules imports back.
"""

import logging
from typing import Any, Dict

from nexus.llm.provider import BaseLLMProvider
from nexus.registry.providers import discover_builtin_providers, registry

logger = logging.getLogger(__name__)

# llm_config fields forwarded to entry.instantiate() as overrides
_PROVIDER_OVERRIDE_FIELDS = (
    "api_base",
    "api_key",
    "api_key_env",
    "timeout",
    "max_retries",
    "enable_thinking",
)


def build_provider(llm_config: Dict[str, Any]) -> BaseLLMProvider:
    """Build a configured provider instance from an llm config dict.

    Args:
        llm_config: LLM config dict with at least ``code``. The non-empty
            ``api_base`` / ``api_key`` / ``api_key_env`` / ``timeout`` /
            ``max_retries`` / ``enable_thinking`` fields override the
            provider's registered defaults.

    Returns:
        A provider instance ready for ``chat_completion`` calls.

    Raises:
        ValueError: when the provider code is not registered.
    """
    code = llm_config["code"]

    # Ensure the provider is registered
    if not registry.is_registered(code):
        discover_builtin_providers()

    entry = registry.get(code)
    if entry is None:
        raise ValueError(
            f"LLM provider '{code}' 未注册，可用: {registry.list_codes()}"
        )

    # Forward the non-empty override fields from the yaml config
    overrides = {
        field: llm_config[field]
        for field in _PROVIDER_OVERRIDE_FIELDS
        if llm_config.get(field) not in (None, "")
    }

    logger.info(
        "构建 LLM provider: %s, model: %s, 配置覆盖: %s",
        code,
        llm_config.get("model"),
        sorted(overrides) or "无",
    )

    return entry.instantiate(**overrides)
