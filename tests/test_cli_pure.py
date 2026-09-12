"""cli.py pure-function unit tests: slash parsing / verbose rendering / menu rendering / short flag expansion.

No network, no LLM; REPL interaction and fire dispatch are accepted manually elsewhere.
"""

import unittest.mock
from pathlib import Path

import pytest

import host.cli as cli


# ============================================================================
# parse_slash_command
# ============================================================================

class TestParseSlashCommand:
    def test_plain_text_returns_none(self):
        assert cli.parse_slash_command("你好") is None
        assert cli.parse_slash_command("  你好") is None
        assert cli.parse_slash_command("") is None

    def test_known_command_no_arg(self):
        assert cli.parse_slash_command("/help") == {"name": "help", "arg": ""}
        assert cli.parse_slash_command("/exit") == {"name": "exit", "arg": ""}
        assert cli.parse_slash_command("/slots") == {"name": "slots", "arg": ""}

    def test_known_command_with_arg(self):
        assert cli.parse_slash_command("/new xianyu_agent") == {
            "name": "new", "arg": "xianyu_agent"}
        assert cli.parse_slash_command("/llm openai") == {
            "name": "llm", "arg": "openai"}

    def test_whitespace_normalized(self):
        assert cli.parse_slash_command("  /help  ") == {"name": "help", "arg": ""}

    def test_case_insensitive_command(self):
        assert cli.parse_slash_command("/HELP") == {"name": "help", "arg": ""}

    def test_unknown_command_flagged(self):
        assert cli.parse_slash_command("/foobar") == {
            "name": "unknown", "arg": "foobar"}

    def test_arg_keeps_inner_spaces(self):
        assert cli.parse_slash_command("/new  multi word arg") == {
            "name": "new", "arg": "multi word arg"}

    @pytest.mark.parametrize("cmd", cli._SLASH_COMMANDS)
    def test_all_commands_parse(self, cmd):
        assert cli.parse_slash_command(f"/{cmd}")["name"] == cmd


# ============================================================================
# render_verbose_summary
# ============================================================================

class TestRenderVerboseSummary:
    def _snap(self, node="n1", module="m1", slots=None, intent=None,
              next_node=None):
        return {
            "current_module_code": module,
            "current_node_code": node,
            "filled_slots": slots or {},
            "intent": intent,
            "next_node": next_node,
        }

    def test_no_change_empty(self):
        out = cli.render_verbose_summary(self._snap(), self._snap())
        assert out == ""

    def test_node_transition_rendered(self):
        before, after = self._snap(node="n1"), self._snap(node="n2")
        out = cli.render_verbose_summary(before, after)
        assert "n1" in out and "n2" in out and "→" in out

    def test_node_transition_renders_both_ends(self):
        before, after = self._snap(node="n1"), self._snap(node="n2")
        out = cli.render_verbose_summary(before, after)
        assert "n1" in out and "n2" in out

    def test_intent_rendered(self):
        after = self._snap(intent="buy_car")
        out = cli.render_verbose_summary(self._snap(), after)
        assert "buy_car" in out

    def test_slot_change_rendered(self):
        before = self._snap(slots={"budget": "10万"})
        after = self._snap(slots={"budget": "20万", "car_type": "SUV"})
        out = cli.render_verbose_summary(before, after)
        assert "budget" in out and "20万" in out and "car_type" in out

    def test_unchanged_slots_not_rendered(self):
        same = {"budget": "20万"}
        out = cli.render_verbose_summary(self._snap(slots=same),
                                         self._snap(slots=dict(same)))
        assert "budget" not in out

    def test_none_fields_tolerated(self):
        before = {"current_module_code": None, "current_node_code": None,
                  "filled_slots": None, "intent": None, "next_node": None}
        after = self._snap()
        out = cli.render_verbose_summary(before, after)
        assert isinstance(out, str)


# ============================================================================
# render_pattern_menu
# ============================================================================

class _FakePattern:
    def __init__(self, code, name, description=""):
        self.code, self.name, self.description = code, name, description


class TestRenderPatternMenu:
    def test_renders_numbered_entries(self):
        out = cli.render_pattern_menu(
            "选择 pattern",
            [_FakePattern("a", "Pattern A"), _FakePattern("b", "Pattern B", "desc")],
        )
        assert "1. a — Pattern A" in out
        assert "2. b — Pattern B" in out
        assert "desc" in out

    def test_empty_description_omitted(self):
        out = cli.render_pattern_menu(
            "t", [_FakePattern("a", "A", "")])
        lines = [l for l in out.split("\n") if l.strip()]
        assert len(lines) == 2  # header + one entry line


# ============================================================================
# _expand_short_verbose
# ============================================================================

class TestExpandShortVerbose:
    def test_v_levels(self):
        assert cli._expand_short_verbose(["-v"]) == ["--verbose=1"]
        assert cli._expand_short_verbose(["-vv"]) == ["--verbose=2"]
        assert cli._expand_short_verbose(["-vvv"]) == ["--verbose=3"]

    def test_mixed_args_passthrough(self):
        argv = ["chat", "--pattern", "p1", "-vv", "--session-id", "s"]
        assert cli._expand_short_verbose(argv) == [
            "chat", "--pattern", "p1", "--verbose=2", "--session-id", "s"]

    def test_verbose_long_form_untouched(self):
        assert cli._expand_short_verbose(["--verbose=2"]) == ["--verbose=2"]

    def test_unrelated_dash_flag_untouched(self):
        assert cli._expand_short_verbose(["--pattern", "-v-like"]) == [
            "--pattern", "-v-like"]


# ============================================================================
# render_verbose_full (stubbed context object; engine not required)
# ============================================================================

class _FakeCxt:
    def __init__(self, nlu=None, nlg=None, agent=None, recall=None,
                 actions=None):
        self.nlu_result, self.nlg_result, self.agent_result = nlu, nlg, agent
        self._recall = recall
        self.actions = actions or []

    def format_recall_info(self):
        return self._recall


class TestRenderVerboseFull:
    def test_empty_cxt_renders_header_only(self):
        out = cli.render_verbose_full(_FakeCxt())
        assert "context" in out

    def test_nlu_nlg_json(self):
        cxt = _FakeCxt(nlu={"next_node": "n2"}, nlg={"content": "hi"})
        out = cli.render_verbose_full(cxt)
        assert '"next_node": "n2"' in out
        assert '"content": "hi"' in out

    def test_actions_rendered(self):
        cxt = _FakeCxt(actions=[{"conversation_end": True}])
        out = cli.render_verbose_full(cxt)
        assert "actions" in out and "conversation_end" in out

    def test_non_serializable_falls_back_to_repr(self):
        class Weird:
            pass
        out = cli.render_verbose_full(_FakeCxt(agent=Weird()))
        assert "agent_result" in out


# ============================================================================
# parse_task_info / prompt_task_info
# ============================================================================

class TestParseTaskInfo:
    def test_empty_returns_none(self):
        assert cli.parse_task_info("") is None
        assert cli.parse_task_info("   ") is None
        assert cli.parse_task_info(None) is None

    def test_valid_json_object(self):
        out = cli.parse_task_info('{"channel": "xianyu", "item_id": "123"}')
        assert out == {"channel": "xianyu", "item_id": "123"}

    def test_values_coerced_to_str(self):
        out = cli.parse_task_info('{"price": 9900, "count": 2}')
        assert out == {"price": "9900", "count": "2"}
        assert all(isinstance(v, str) for v in out.values())

    def test_invalid_json_raises_system_exit(self):
        with pytest.raises(SystemExit):
            cli.parse_task_info("{not json")

    def test_non_object_rejected(self):
        with pytest.raises(SystemExit):
            cli.parse_task_info('["a", "b"]')
        with pytest.raises(SystemExit):
            cli.parse_task_info('"str"')


class TestPromptTaskInfo:
    def test_preset_parsed_without_prompt(self):
        with unittest.mock.patch("builtins.input") as inp:
            out = cli.prompt_task_info("p1", '{"k": "v"}')
        assert out == {"k": "v"}
        inp.assert_not_called()

    def test_enter_skips(self):
        with unittest.mock.patch("builtins.input", return_value=""):
            assert cli.prompt_task_info("p1") is None

    def test_preset_wins_over_mock(self):
        """An explicit --task-info wins over the pattern's mock preset."""
        with unittest.mock.patch("builtins.input") as inp:
            out = cli.prompt_task_info("customer_agent", '{"k": "v"}')
        assert out == {"k": "v"}
        inp.assert_not_called()


class TestMockTaskInfo:
    def test_customer_agent_preset_present(self):
        mock = cli.mock_task_info_for("customer_agent")
        assert mock == {"channel": "xianyu", "account_id": "demo"}

    def test_unknown_pattern_returns_none(self):
        assert cli.mock_task_info_for("xianyu_agent") is None
        assert cli.mock_task_info_for("no_such") is None

    def test_returns_copy_not_table_entry(self):
        mock = cli.mock_task_info_for("customer_agent")
        mock["account_id"] = "mutated"
        assert cli.mock_task_info_for("customer_agent")["account_id"] == "demo"

    def test_enter_applies_mock_for_preset_pattern(self):
        with unittest.mock.patch("builtins.input", return_value=""):
            assert cli.prompt_task_info("customer_agent") == {
                "channel": "xianyu", "account_id": "demo"}

    def test_eof_applies_mock(self):
        """Non-interactive (piped) scenario: EOF also falls back to the mock, keeping demos scriptable."""
        with unittest.mock.patch("builtins.input", side_effect=EOFError):
            assert cli.prompt_task_info("customer_agent") == {
                "channel": "xianyu", "account_id": "demo"}

    def test_typed_json_wins_over_mock(self):
        with unittest.mock.patch("builtins.input",
                                 return_value='{"account_id": "acct_9"}'):
            assert cli.prompt_task_info("customer_agent") == {
                "account_id": "acct_9"}

    def test_preset_account_matches_seed_scope(self):
        """The mock's account_id must match the knowledge-seed default scope,
        so catalog prefetch / knowledge tools can reach the seeded data."""
        assert f"xianyu:{cli.mock_task_info_for('customer_agent')['account_id']}" \
            == cli.knowledge_seed.__defaults__[0]

    def test_typed_json_parsed(self):
        with unittest.mock.patch("builtins.input",
                                 return_value='{"item_id": "9"}'):
            assert cli.prompt_task_info("p1") == {"item_id": "9"}

    def test_eof_returns_none(self):
        with unittest.mock.patch("builtins.input", side_effect=EOFError):
            assert cli.prompt_task_info("p1") is None


class TestBuildSessionTaskInfo:
    def test_build_session_writes_task_info_both_places(self, monkeypatch):
        """task_info is written to two places, matching main._launch_session_core:
        session.task_info (persisted) + metadata (prompt slot)."""
        from nexus.registry.patterns import discover_builtin_patterns

        discover_builtin_patterns()
        session = cli.build_session(
            "t-task", "xianyu_agent",
            task_info={"channel": "xianyu", "item_id": "1"},
        )
        assert session.task_info == {"channel": "xianyu", "item_id": "1"}
        assert session.cxt.metadata["task_info"] == {"channel": "xianyu",
                                                    "item_id": "1"}
        assert "item_id: 1" in session.cxt.format_task_info()

    def test_build_session_without_task_info_no_metadata_key(self, monkeypatch):
        from nexus.registry.patterns import discover_builtin_patterns

        discover_builtin_patterns()
        session = cli.build_session("t-no-task", "xianyu_agent")
        assert session.task_info == {}
        assert "task_info" not in session.cxt.metadata


# ============================================================================
# KEEP_CONFIG menu + llm_override wiring
# ============================================================================

class TestKeepConfigMenu:
    def test_provider_menu_includes_keep_config(self):
        """The provider menu carries the fixed "keep config settings" entry, listed first."""
        entries = cli._provider_menu_entries()
        assert entries[0]["value"] == cli.KEEP_CONFIG
        assert "维持" in entries[0]["label"]

    def test_pick_keep_config_in_provider_menu_returns_empty(self):
        with cli._patch_select(cli.KEEP_CONFIG):
            assert cli.resolve_llm_choice("", "") == {"code": "", "model": ""}

    def test_pick_keep_config_in_model_menu_keeps_code(self):
        """Selecting "keep config settings" in the model menu: keep the chosen code, leave model empty (fall back to the global default)."""
        from fake_provider import FAKE_PROVIDER_CODE, register_fake_provider

        register_fake_provider()
        # the fake provider declares no models list -> the input() manual-entry branch runs; the "keep config settings" option is typed in
        with unittest.mock.patch("builtins.input", return_value=cli.KEEP_CONFIG):
            result = cli.resolve_llm_choice(FAKE_PROVIDER_CODE, "")
        assert result == {"code": FAKE_PROVIDER_CODE, "model": ""}


class TestBuildSessionOverride:
    def test_build_session_writes_override_not_llm_config(self, monkeypatch):
        """--llm/--model presets write metadata.llm_override instead of writing llm_config directly."""
        from fake_provider import fake_llm_config, register_fake_provider

        register_fake_provider()
        monkeypatch.setattr(cli, "get_llm_config", lambda *a, **k: fake_llm_config())
        from nexus.registry.patterns import discover_builtin_patterns

        discover_builtin_patterns()

        session = cli.build_session(
            "t1", "xianyu_agent",
            llm_overrides={"code": "fake_test_provider", "model": "fake-model"},
        )
        assert session.cxt.metadata["llm_override"]["model"] == "fake-model"
        assert session.cxt.llm_config is None


# ============================================================================
# Final-review regression fixes (Final review C1 / C2)
# ============================================================================

_YAML = """\
llm_providers:
  openai:
    api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
  deepseek:
    api_base: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
llm_default:
  code: openai
  model: qwen3.8-max
pattern_llm:
  p_cli_test:
    model: pattern-layer-model
"""


def _minimal_pattern(code):
    from nexus.model.node import BaseNode
    from nexus.model.pattern import Pattern

    return Pattern(code=code, name="t", description="t",
                   pattern_type="fsm",
                   nodes=[BaseNode(code="f1", name="节点一")])


def _run_chat_turn(tmp_path, session):
    """Run one turn via chat(): get_llm_config stubbed onto a tmp yaml for real three-tier resolution."""
    from unittest.mock import patch as _patch
    import nexus.settings as cfg_mod
    import nexus.engine.chat as chat_mod
    from nexus.engine.chat import chat as chat_fn

    config_path = str(tmp_path / "local_config.yaml")
    Path(config_path).write_text(_YAML, encoding="utf-8")
    real = cfg_mod.get_llm_config

    def spy(**kw):
        kw.setdefault("config_path", config_path)
        return real(**kw)

    sessions = {session.session_id: session}
    with _patch("atoms.executors.loop_executor.build_provider"), \
         _patch.object(chat_mod, "get_llm_config", side_effect=spy):
        from async_utils import arun
        arun(chat_fn(query="你好", session_id=session.session_id,
                     all_sessions=sessions))


class TestEmptyOverrideNotPinned:
    def test_empty_override_skips_metadata_and_layered_resolution(
            self, tmp_path, monkeypatch):
        """An empty override writes no llm_override via build_session;
        later turns use three-tier resolution and model comes from the pattern layer (C1 regression)."""
        from nexus.engine.session import Session

        pattern = _minimal_pattern("p_cli_test")
        monkeypatch.setattr(cli, "pattern_registry",
                            type("R", (), {"get": staticmethod(
                                lambda c: pattern if c == "p_cli_test" else None),
                                "list_codes": staticmethod(lambda: ["p_cli_test"])})())

        s = cli.build_session("t-empty", "p_cli_test",
                              llm_overrides={"code": "", "model": ""})
        assert "llm_override" not in s.cxt.metadata

        # End-to-end: no override -> resolution goes through the three tiers, pattern-layer model wins
        _run_chat_turn(tmp_path, s)
        assert s.cxt.llm_config["model"] == "pattern-layer-model"

    def test_cross_provider_override_connection_from_providers_section(
            self, tmp_path):
        """/llm provider switch: the override holds only explicitly chosen fields; connection fields come
        from the llm_providers.deepseek section, not cross-wired into the openai connection (C2 regression)."""
        from nexus.engine.session import Session

        pattern = _minimal_pattern("p_cli_test")
        session = Session(session_id="t-cross", pattern_code="p_cli_test")
        session.pattern = pattern
        session.cxt.node_map = pattern.node_map
        # Simulate the _do_llm write semantics: only explicitly chosen fields are written
        session.cxt.metadata["llm_override"] = {"code": "deepseek",
                                                "model": "deepseek-chat"}
        _run_chat_turn(tmp_path, session)
        assert session.cxt.llm_config["model"] == "deepseek-chat"
        assert session.cxt.llm_config["api_base"] == "https://api.deepseek.com/v1"
        assert session.cxt.llm_config["api_key_env"] == "DEEPSEEK_API_KEY"
