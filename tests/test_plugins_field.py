"""plugins 合并声明字段（nexus/model/plugins_field.py）测试：

- 构造期规范化：legacy 独立字段折入 / dict 值优先 / 未知槽位与非法值 fail-fast
- Pattern 与 BaseModule 的 property 读写兼容（旧字段名活在新 dict 上）
- executor 解析链：module.executor > module.plugins[family] >
  pattern.plugins[family] > 类型默认码（chat._resolve_executor_code）
- messages_builder / agent_hooks 两级解析吃到 plugins dict
- yml round-trip 只输出规范 plugins 形态；旧形状 dict 可加载
- validation：plugins 声明的 code 经 has() 可解析（pattern + module 两层）
"""

import pytest

import atoms.executors  # noqa: F401 -- warm executor codes
from nexus.context import DialogueContext
from nexus.engine.agent_hooks import resolve_agent_hooks
from nexus.engine.chat import _resolve_executor_code
from nexus.engine.messages import build_agent_messages
from nexus.model.module import AgentModule, BaseModule, FSMModule, RouteModule
from nexus.model.pattern import Pattern
from nexus.model.serialization import (
    pattern_from_dict,
    pattern_to_dict,
)
from nexus.model.validation import validate_plugin_declarations
from nexus.registry.plugins import registry as plugin_registry


def _mk_pattern(**kwargs):
    return Pattern(
        code="p", name="p", description="d", entry_module_code="m",
        modules=[AgentModule(module_code="m", module_name="m")],
        **kwargs,
    )


def _mk_session(pattern):
    from types import SimpleNamespace
    return SimpleNamespace(pattern=pattern)


# ---------------------------------------------------------------------------
# 构造期规范化
# ---------------------------------------------------------------------------

def test_legacy_params_fold_into_plugins():
    pattern = _mk_pattern(executor_loop="default_loop", executor_fsm="default_fsm",
                          executor_route="default_route",
                          messages_builder="default", agent_hooks="my_hooks")
    assert pattern.plugins == {
        "loop": "default_loop", "fsm": "default_fsm", "route": "default_route",
        "messages_builder": "default", "agent_hooks": "my_hooks",
    }


def test_dict_value_wins_over_legacy_param():
    pattern = _mk_pattern(plugins={"loop": "from_dict"},
                          executor_loop="from_legacy")
    assert pattern.plugins["loop"] == "from_dict"


def test_module_legacy_params_fold():
    module = AgentModule(module_code="m",
                         messages_builder="mb", agent_hooks="ah")
    assert module.plugins == {"messages_builder": "mb", "agent_hooks": "ah"}


def test_unknown_slot_raises():
    with pytest.raises(ValueError, match="槽位名非法"):
        _mk_pattern(plugins={"executor": "x"})
    with pytest.raises(ValueError, match="槽位名非法"):
        AgentModule(module_code="m", plugins={"nlu": "x"})


def test_illegal_value_raises():
    with pytest.raises(ValueError, match="必须是 str/None"):
        _mk_pattern(plugins={"loop": 123})
    # executor 族不收 callable
    with pytest.raises(ValueError, match="必须是 str/None"):
        _mk_pattern(plugins={"loop": lambda: "x"})
    # messages_builder / agent_hooks 收 transitional callable（同旧字段语义）
    builder = lambda module, cxt, extra: []  # noqa: E731
    assert _mk_pattern(plugins={"messages_builder": builder}).plugins[
        "messages_builder"] is builder


def test_none_values_allowed():
    pattern = _mk_pattern(plugins={"loop": None})
    assert pattern.plugins == {"loop": None}
    assert pattern.executor_loop is None


# ---------------------------------------------------------------------------
# property 读写兼容
# ---------------------------------------------------------------------------

def test_pattern_properties_read_plugins():
    pattern = _mk_pattern(plugins={"loop": "my_loop", "messages_builder": "mb",
                                   "agent_hooks": "ah"})
    assert pattern.executor_loop == "my_loop"
    assert pattern.executor_fsm is None
    assert pattern.executor_route is None
    assert pattern.messages_builder == "mb"
    assert pattern.agent_hooks == "ah"


def test_pattern_property_setters_write_through():
    pattern = _mk_pattern()
    pattern.executor_fsm = "f"
    pattern.messages_builder = "mb"
    pattern.agent_hooks = {"on_agent_start": []}  # legacy dict 内联形态
    assert pattern.plugins["fsm"] == "f"
    assert pattern.plugins["messages_builder"] == "mb"
    assert pattern.plugins["agent_hooks"] == {"on_agent_start": []}
    assert pattern.executor_fsm == "f"


def test_module_properties():
    module = AgentModule(module_code="m", messages_builder="mb")
    assert module.messages_builder == "mb"
    assert module.agent_hooks is None
    module.agent_hooks = "ah"
    assert module.plugins["agent_hooks"] == "ah"


# ---------------------------------------------------------------------------
# executor 解析链（chat._resolve_executor_code）
# ---------------------------------------------------------------------------

def test_resolve_default_when_nothing_declared():
    session = _mk_session(_mk_pattern())
    assert _resolve_executor_code(session, AgentModule()) == "default_loop"
    assert _resolve_executor_code(session, FSMModule()) == "default_fsm"
    assert _resolve_executor_code(session, RouteModule()) == "default_route"


def test_resolve_pattern_plugins_overrides_default():
    session = _mk_session(_mk_pattern(plugins={
        "loop": "custom_loop", "fsm": "custom_fsm", "route": "custom_route"}))
    assert _resolve_executor_code(session, AgentModule()) == "custom_loop"
    assert _resolve_executor_code(session, FSMModule()) == "custom_fsm"
    assert _resolve_executor_code(session, RouteModule()) == "custom_route"


def test_resolve_module_plugins_beats_pattern():
    session = _mk_session(_mk_pattern(plugins={"loop": "pattern_loop"}))
    module = AgentModule(plugins={"loop": "module_loop"})
    assert _resolve_executor_code(session, module) == "module_loop"


def test_resolve_module_executor_direct_field_still_highest():
    session = _mk_session(_mk_pattern(plugins={"loop": "pattern_loop"}))
    module = AgentModule(executor="direct", plugins={"loop": "module_loop"})
    assert _resolve_executor_code(session, module) == "direct"


def test_resolve_legacy_pattern_field_still_works():
    session = _mk_session(_mk_pattern(executor_loop="legacy_loop"))
    assert _resolve_executor_code(session, AgentModule()) == "legacy_loop"


# ---------------------------------------------------------------------------
# messages_builder / agent_hooks 两级解析
# ---------------------------------------------------------------------------

def _mk_cxt():
    return DialogueContext(session_id="s", user_query="q")


def test_messages_builder_module_plugins_beats_pattern():
    mod_builder = lambda module, cxt, extra: [{"role": "user", "content": "mod"}]  # noqa: E731
    pat_builder = lambda module, cxt, extra: [{"role": "user", "content": "pat"}]  # noqa: E731
    module = AgentModule(module_code="m",
                         plugins={"messages_builder": mod_builder})
    pattern = _mk_pattern(plugins={"messages_builder": pat_builder})
    msgs = build_agent_messages(module, _mk_cxt(), pattern=pattern)
    assert msgs == [{"role": "user", "content": "mod"}]


def test_messages_builder_pattern_plugins_used_when_module_silent():
    pat_builder = lambda module, cxt, extra: [{"role": "user", "content": "pat"}]  # noqa: E731
    module = AgentModule(module_code="m")
    pattern = _mk_pattern(plugins={"messages_builder": pat_builder})
    msgs = build_agent_messages(module, _mk_cxt(), pattern=pattern)
    assert msgs == [{"role": "user", "content": "pat"}]


def test_messages_builder_pattern_plugin_code_resolves_via_registry():
    captured = {}

    def _builder(module, cxt, extra_blocks):
        captured["called"] = True
        return [{"role": "user", "content": "resolved"}]

    plugin_registry.register("messages_builder", "pf_test_builder",
                             lambda: _builder)
    try:
        module = AgentModule(module_code="m")
        pattern = _mk_pattern(plugins={"messages_builder": "pf_test_builder"})
        msgs = build_agent_messages(module, _mk_cxt(), pattern=pattern)
        assert msgs == [{"role": "user", "content": "resolved"}]
        assert captured["called"]
    finally:
        plugin_registry.deregister("messages_builder", "pf_test_builder")


def test_agent_hooks_pattern_plugins_callable():
    hooks_pkg = lambda: {"on_llm_call": [lambda e: None]}  # noqa: E731
    pattern = _mk_pattern(plugins={"agent_hooks": hooks_pkg})
    module = AgentModule(module_code="m")
    assert "on_llm_call" in resolve_agent_hooks(module, pattern)


def test_agent_hooks_module_plugins_replaces_wholesale():
    pattern = _mk_pattern(plugins={
        "agent_hooks": lambda: {"on_llm_call": [lambda e: None]}})
    module = AgentModule(module_code="m", plugins={
        "agent_hooks": lambda: {"on_agent_end": [lambda e: None]}})
    hooks = resolve_agent_hooks(module, pattern)
    assert set(hooks) == {"on_agent_end"}


# ---------------------------------------------------------------------------
# 序列化：规范 plugins 形态输出 + 旧形状加载
# ---------------------------------------------------------------------------

def test_to_dict_emits_plugins_only():
    pattern = _mk_pattern(executor_loop="default_loop",
                          messages_builder="default")
    d = pattern_to_dict(pattern)
    assert d["plugins"] == {"loop": "default_loop",
                            "messages_builder": "default"}
    for legacy in ("executor_loop", "executor_fsm", "executor_route",
                   "messages_builder", "agent_hooks"):
        assert legacy not in d


def test_round_trip_dict_stable_with_plugins():
    pattern = _mk_pattern(plugins={"loop": "default_loop",
                                   "messages_builder": "default"},
                          max_hops=3)
    d1 = pattern_to_dict(pattern)
    loaded = pattern_from_dict(d1)
    assert pattern_to_dict(loaded) == d1
    assert loaded.plugins == pattern.plugins
    assert loaded.executor_loop == "default_loop"


def test_from_dict_legacy_shape_folds_into_plugins():
    d = {
        "code": "old", "name": "t", "description": "t",
        "entry_module_code": "m",
        "executor_loop": "default_loop", "executor_fsm": "default_fsm",
        "messages_builder": "default", "agent_hooks": "my_hooks",
        "modules": [{"type": "agent", "module_code": "m",
                     "messages_builder": "default",
                     "agent_hooks": "my_hooks"}],
        "stages": [],
    }
    loaded = pattern_from_dict(d)
    assert loaded.plugins == {"loop": "default_loop", "fsm": "default_fsm",
                              "messages_builder": "default",
                              "agent_hooks": "my_hooks"}
    module = loaded.module_map["m"]
    assert module.plugins == {"messages_builder": "default",
                              "agent_hooks": "my_hooks"}
    # 规范形态再输出：legacy 键不再出现
    d2 = pattern_to_dict(loaded)
    assert d2["plugins"] == loaded.plugins
    assert d2["modules"][0]["plugins"] == module.plugins


def test_empty_plugins_not_emitted():
    d = pattern_to_dict(_mk_pattern())
    assert "plugins" not in d


# ---------------------------------------------------------------------------
# 校验：plugins 声明可解析性
# ---------------------------------------------------------------------------

def test_validation_pattern_plugins_ghost_code():
    pattern = _mk_pattern(plugins={"fsm": "ghost_fsm",
                                   "messages_builder": "ghost_mb"})
    errors = validate_plugin_declarations(pattern)
    assert any("executor_fsm" in e and "ghost_fsm" in e for e in errors)
    assert any("messages_builder" in e and "ghost_mb" in e for e in errors)


def test_validation_module_plugins_ghost_code():
    module = AgentModule(module_code="m", plugins={"loop": "ghost_loop",
                                                   "agent_hooks": "ghost_ah"})
    pattern = Pattern(code="p", name="p", description="d",
                      entry_module_code="m", modules=[module])
    errors = validate_plugin_declarations(pattern)
    assert any("plugins[executor_loop]" in e and "ghost_loop" in e
               for e in errors)
    assert any("plugins[agent_hooks]" in e and "ghost_ah" in e for e in errors)


def test_validation_registered_codes_pass():
    pattern = _mk_pattern(plugins={"loop": "default_loop",
                                   "messages_builder": "default"})
    assert validate_plugin_declarations(pattern) == []
