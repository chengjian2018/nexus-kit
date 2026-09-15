"""build_provider unified-entry tests -- verify that local_config.yaml provider fields actually take effect.

Fully offline: no real API access; only asserts that config overrides reach the provider instance.
"""

import pytest

import nexus.settings
from fake_provider import FAKE_PROVIDER_CODE, register_fake_provider
from nexus.registry.providers import registry as llm_registry
from atoms.providers.dashscope_provider import OpenAICompatibleProvider
from nexus.llm.resolve import build_provider


# ============================================================================
# Override fields take effect
# ============================================================================

def test_yaml_overrides_reach_provider():
    """Non-empty api_base/api_key/api_key_env/timeout/max_retries in yaml override the registered defaults."""
    provider = build_provider({
        "code": "dashscope",
        "model": "qwen3.8-max",
        "api_base": "https://example.com/compatible-mode/v1",
        "api_key": "sk-from-yaml",
        "api_key_env": "MY_KEY_ENV",
        "timeout": 11,
        "max_retries": 3,
    })

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.api_base == "https://example.com/compatible-mode/v1"
    assert provider.resolve_api_key() == "sk-from-yaml"
    assert provider.api_key_env == "MY_KEY_ENV"
    assert provider.timeout == 11
    assert provider.max_retries == 3


def test_blank_fields_fall_back_to_registered_defaults():
    """Empty strings / None / missing fields do not override; fall back to the defaults declared at provider registration."""
    provider = build_provider({
        "code": "dashscope",
        "model": "qwen3.8-max",
        "api_base": "",
        "api_key": None,
    })

    entry = llm_registry.get("dashscope")
    assert provider.api_base == entry.api_base
    assert provider.api_key_env == entry.api_key_env
    assert provider.timeout == 60   # OpenAICompatibleProvider constructor default
    assert provider.max_retries == 2


def test_zero_max_retries_is_kept():
    """max_retries=0 is a legal value (no retries), not to be discarded as "unset"."""
    provider = build_provider({"code": "dashscope", "model": "m", "max_retries": 0})
    assert provider.max_retries == 0


def test_unknown_code_raises():
    with pytest.raises(ValueError, match="未注册"):
        build_provider({"code": "no-such-provider", "model": "m"})


def test_dashscope_provider_discovered_on_first_use():
    """When the provider is unregistered, build_provider triggers auto-discovery internally and completes registration."""
    import importlib
    import sys

    llm_registry.deregister("dashscope")
    # When a module is already cached in sys.modules, import_module will not re-run its
    # registration code; pop the cache to simulate a first import in a fresh process
    sys.modules.pop("atoms.providers.dashscope_provider", None)
    try:
        provider = build_provider({"code": "dashscope", "model": "m"})
        assert provider.code == "dashscope"
        assert llm_registry.is_registered("dashscope")
    finally:
        # Restore state: re-run the module registration code so the registry and the module cache stay consistent
        llm_registry.deregister("dashscope")
        sys.modules.pop("atoms.providers.dashscope_provider", None)
        importlib.import_module("atoms.providers.dashscope_provider")


# ============================================================================
# _call_llm(llm_config=None) fallback path
# ============================================================================

def test_call_llm_with_none_config_uses_loaded_config(monkeypatch):
    """With llm_config=None, fall back to loading local_config.yaml; model comes from the loaded config, not the argument.

    Regression: the old implementation raised ``TypeError: 'NoneType' object is not subscriptable`` here.
    """
    from atoms.stages.nlu import FSMNLU

    register_fake_provider()
    monkeypatch.setattr(
        nexus.settings,
        "get_llm_config",
        lambda: {"code": FAKE_PROVIDER_CODE, "model": "fake-model"},
    )

    from async_utils import arun
    out = arun(FSMNLU()._call_llm("ping", None))  # must not raise TypeError
    assert isinstance(out, str)


def test_call_llm_with_none_config_loads_real_yaml(monkeypatch):
    """When the fallback path reads the real local_config.yaml it builds a provider with the config applied."""
    from atoms.stages.nlg import FSMNLG
    import atoms.stages.nlg.nlg as nlg_module

    built = {}

    class _StubProvider:
        async def achat_completion(self, **kwargs):
            return {"content": "stub"}

    def spy(cfg):
        built.update(cfg)
        return _StubProvider()

    monkeypatch.setattr(nlg_module, "build_provider", spy)
    from async_utils import arun
    arun(FSMNLG()._call_llm("ping", None))

    from nexus.settings import load_config
    expected_code = load_config()["llm_default"]["code"]
    assert built.get("code") == expected_code  # the code from local_config.yaml
    assert built.get("api_base")  # yaml's api_base flows into build_provider with the config
