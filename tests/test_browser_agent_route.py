"""Offline tests for browser_agent (the browser-automation-toolbox skill
transcribed into a four-station AGENT graph).

Covers (all offline — the orchestrator subprocess is a scripted bash stub,
no network, no browser):

1. Graph structure: four stations, adjacency, executor plugin bindings,
   deny-by-default tool grants (only ba_run carries bash), validate_pattern,
   the max_steps budget; four executor plugins registered; zero tool
   registrations from this app.
2. App config overlay: the repo config.yaml's loop budgets / free bag
   (install_missing default off) reach the settings layer.
3. Full-turn success: plan → run (cloak ok on attempt 1) → report; reply
   assembled from the receipt (winning engine, outputs, ABSOLUTE artifact
   paths), trace in metadata, graph_state cleared.
4. Dependency gap across all engines: every engine reports
   dependency_missing → skip (no retry, no repair) → honest failure report
   listing every engine + install hints.
5. Selector drift → repair → success: run fails selector_drift → repair
   station revises the plan (plan.json on disk really changes) → same
   engine retried with the new plan → success; anti-replay log on the
   board; selector-fix lesson recorded to the experience file.
6. Login wall takeover (wait_human): run fails login_required → turn reply
   is the takeover guidance and the graph suspends AT ba_run; the next user
   message resumes the same node, retries the SAME engine once → success.
7. Network retry ladder: two network_or_timeout attempts on cloak
   (self-edge retry) then engine switch to browser-act succeeds; fallback
   lesson recorded.
8. Units: plan validation / platform detection / engine-order resolution /
   receipt-line parsing / lesson dedup.
"""

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from async_utils import arun

logging.basicConfig(level=logging.WARNING)

# Host load order is "tools discovered first, patterns loaded later"
from nexus.registry.tools import discover_builtin_tools

discover_builtin_tools()

import apps.browser_agent.executor as ba
from apps.browser_agent.prompts import PLAN_ANCHOR, REPAIR_ANCHOR


# ============================================================================
# Fixtures / helpers
# ============================================================================

@pytest.fixture(scope="module")
def pattern():
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.browser_agent.route" in imported, (
        f"route 未被自动发现,已发现: {imported}")
    return registry.get("browser_agent")


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Workspace root pinned at tmp (receipts / plan.json / experience files
    land here; bash is stubbed anyway)."""
    root = tmp_path / "ws"
    monkeypatch.setattr(ba, "_DEFAULT_WORKSPACE_ROOT", str(root))
    return root


def launch(pattern, sessions, session_id="s1"):
    from nexus.engine.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions[session_id] = session
    return session


def chat(sessions, session_id, query):
    from nexus.engine.chat import chat as chat_fn

    return arun(chat_fn(query=query, session_id=session_id,
                        all_sessions=sessions))


# ============================================================================
# Scripted provider (phase-anchor detection) + orchestrator bash stub
# ============================================================================

_PLAN_JSON = json.dumps({
    "url": "https://example.com/search?q=nexus",
    "headless": False,
    "actions": [
        {"type": "goto", "url": "https://example.com/search?q=nexus"},
        {"type": "wait", "seconds": 1},
        {"type": "evaluate", "name": "items",
         "script": "Array.from(document.querySelectorAll('a.card')).map(a=>a.href)"},
        {"type": "screenshot", "path": "after.png"},
    ],
    "notes": "首屏采集",
}, ensure_ascii=False)

_REPAIRED_JSON = json.dumps({
    "url": "https://example.com/search?q=nexus",
    "headless": False,
    "actions": [
        {"type": "goto", "url": "https://example.com/search?q=nexus"},
        {"type": "wait", "seconds": 2},
        {"type": "evaluate", "name": "items",
         "script": "Array.from(document.querySelectorAll('[data-feed] a')).map(a=>a.href)"},
        {"type": "screenshot", "path": "after.png"},
    ],
    "notes": "换用 data-feed 稳定选择器",
}, ensure_ascii=False)


class BrowserScriptedProvider:
    """Answers plan/repair phase prompts by anchor; records requests."""

    def __init__(self, plan_reply=_PLAN_JSON, repair_reply=_REPAIRED_JSON):
        self.plan_reply = plan_reply
        self.repair_reply = repair_reply
        self.requests = []

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        self.requests.append([dict(m) for m in messages])
        last_user = next((m["content"] for m in reversed(messages)
                          if m.get("role") == "user"), "")
        if PLAN_ANCHOR in last_user:
            return {"content": self.plan_reply}
        if REPAIR_ANCHOR in last_user:
            return {"content": self.repair_reply}
        return {"content": "{}"}


def _receipt(engine, ok, kind=None, error=None, outputs=None, artifacts=None):
    return {
        "ok": ok, "engine": engine, "attempt": 1,
        "url": "https://example.com/search?q=nexus",
        "current_url": "https://example.com/search?q=nexus" if ok else None,
        "outputs": outputs or {}, "artifacts": artifacts or [],
        "error": error, "failure_kind": None if ok else kind,
    }


def make_orch_stub(script, calls):
    """bash stub: commands targeting the embedded orchestrator get scripted
    EngineResult receipts (per engine, popped in order); other commands fall
    through to the real registry. Receipts ride stdout as JSON lines."""
    real = ba._execute_tool  # captured before the patch replaces it

    async def fake(name, args):
        command = str(args.get("command") or "")
        if name != "bash" or "/browser_agent/orchestrator.py" not in command:
            return await real(name, args)
        command = str(args.get("command"))
        calls.append(command)
        engine = "cloak"
        for idx, token in enumerate(command.split()):
            if token == "--engine-order":
                engine = command.split()[idx + 1]
                break
        queue = script.setdefault(engine, [])
        receipt = queue.pop(0) if queue else _receipt(engine, True)
        stdout = json.dumps(receipt, ensure_ascii=False) + "\n"
        return json.dumps({"exit_code": 0 if receipt.get("ok") else 1,
                           "stdout": stdout, "stderr": "",
                           "timed_out": False}, ensure_ascii=False)

    return fake


def run_turn(pattern, provider, script, query="帮我采集 example.com 搜索页的链接并截图"):
    sessions = {}
    launch(pattern, sessions)
    calls = []
    stub = make_orch_stub(script, calls)
    with patch.object(ba, "build_provider", return_value=provider), \
            patch.object(ba, "_execute_tool", stub):
        reply = chat(sessions, "s1", query)
    return sessions["s1"], reply, calls


# ============================================================================
# 1. Graph structure and validation
# ============================================================================

def test_pattern_structure_and_validation(pattern):
    assert pattern.code == "browser_agent"
    assert pattern.pattern_type == "agent"
    assert pattern.entry_node_code == "ba_plan"
    assert [n.code for n in pattern.nodes] == [
        "ba_plan", "ba_run", "ba_repair", "ba_report"]
    node_map = pattern.node_map
    assert node_map["ba_plan"].sub_nodes == ["ba_run"]
    assert set(node_map["ba_run"].sub_nodes) == {"ba_report", "ba_repair", "ba_run"}
    assert node_map["ba_repair"].sub_nodes == ["ba_run"]
    assert node_map["ba_report"].sub_nodes == []
    assert node_map["ba_report"].is_end is True
    # executor binding per station
    for code in ("ba_plan", "ba_run", "ba_repair", "ba_report"):
        assert node_map[code].plugins == {"loop": code}, code
    # deny-by-default grants: pattern toolset shell; only ba_run narrows to bash
    assert pattern.allow_toolset == ["shell"]
    assert node_map["ba_run"].use_tools == ["bash"]
    for code in ("ba_plan", "ba_repair", "ba_report"):
        assert node_map[code].use_tools == [], code
    # repair loop consumes steps: declared budget
    assert pattern.max_steps == 20

    from nexus.model.validation import validate_pattern
    validate_pattern(pattern)

    from nexus.registry.plugins import registry as plugin_registry
    for code in ("ba_plan", "ba_run", "ba_repair", "ba_report"):
        assert plugin_registry.has("executor", code), f"executor 插件 {code} 未注册"


def test_zero_tool_registrations(pattern):
    """The app registers no new tools — the bash substrate is builtin; the
    orchestrator runs as a subprocess, not a registry tool."""
    app_dir = Path(__file__).resolve().parents[1] / "apps" / "browser_agent"
    for src in app_dir.glob("*.py"):
        if src.name == "orchestrator.py":  # embedded verbatim copy (CLI)
            continue
        text = src.read_text(encoding="utf-8")
        assert "nexus.registry.tools" not in text, src.name
    assert (app_dir / "route.py").read_text(encoding="utf-8").count(
        "registry.register(") == 1


# ============================================================================
# 2. App config overlay (the repo apps/browser_agent/config.yaml)
# ============================================================================

def test_app_config_overrides(monkeypatch, pattern):
    import nexus.settings as settings_mod
    from nexus.settings import (
        get_loop_limits,
        get_pattern_custom_config,
        resolve_max_steps,
    )

    monkeypatch.delenv("NEXUS_APPS_DIR", raising=False)
    settings_mod.invalidate_config_cache()
    try:
        assert get_loop_limits(
            "browser_agent", "ba_run")["max_tool_rounds"] == 6
        assert resolve_max_steps(pattern) == 20
        bag = get_pattern_custom_config("browser_agent")
        assert bag["install_missing"] is False
        assert bag["max_attempts_per_engine"] == 2
        assert bag["repair_rounds"] == 2
        assert bag["workspace_root"] == "data/browser_agent"
    finally:
        settings_mod.invalidate_config_cache()


# ============================================================================
# 3. Full-turn success (cloak ok on attempt 1)
# ============================================================================

def test_full_turn_first_engine_success(pattern, workspace):
    provider = BrowserScriptedProvider()
    script = {"cloak": [_receipt(
        "cloak", True, outputs={"title": "Example Search",
                                "items": ["https://example.com/1"]},
        artifacts=[str(workspace / "s1" / "run_01_cloak" / "after.png")])]}
    session, reply, calls = run_turn(pattern, provider, script)

    assert len(calls) == 1  # plan → run → report, one engine attempt
    assert "--engine-order cloak" in calls[0]
    assert "--max-attempts-per-engine 1" in calls[0]
    # reply assembled from the receipt: winning engine + outputs + artifacts
    assert "✅" in reply and "cloak" in reply
    assert "Example Search" in reply
    assert str(workspace / "s1" / "run_01_cloak" / "after.png") in reply
    # single-turn agent semantics: silent stations, terminal owns the reply
    assert session.cxt.graph_state == {}
    trace = session.cxt.metadata.get("browser_agent")
    assert trace and trace["success"] is True
    assert trace["selected_engine"] == "cloak"
    assert trace["done_reason"] == "pass"
    # the plan really landed on disk
    plan = json.loads(
        (workspace / "s1" / "plan.json").read_text(encoding="utf-8"))
    assert plan["url"] == "https://example.com/search?q=nexus"


# ============================================================================
# 4. Dependency gap across all engines → honest failure report
# ============================================================================

def test_dependency_gap_all_engines(pattern, workspace):
    provider = BrowserScriptedProvider()
    script = {engine: [_receipt(engine, False, kind="dependency_missing",
                                error=f"{engine} package missing")]
              for engine in ("cloak", "browser-act", "kimi", "playwright")}
    session, reply, calls = run_turn(pattern, provider, script)

    # skip semantics: one visit per engine, no retries, no repair
    assert len(calls) == 4
    assert [c.split("--engine-order ")[1].split()[0] for c in calls] == [
        "cloak", "browser-act", "kimi", "playwright"]
    assert "❌" in reply
    for engine in ("cloak", "browser-act", "kimi", "playwright"):
        assert engine in reply
    assert "dependency_missing" in reply
    assert "cloakbrowser" in reply  # install hints
    trace = session.cxt.metadata["browser_agent"]
    assert trace["success"] is False
    assert trace["done_reason"] == "dependency_gap_all_engines"
    assert len(trace["attempts"]) == 4
    # all-engines-failed lesson recorded under the general platform file
    lesson = workspace / "experience" / "general.md"
    assert lesson.exists() and "全部引擎失败" in lesson.read_text(encoding="utf-8")


# ============================================================================
# 5. Selector drift → repair → same-engine retry with the new plan
# ============================================================================

def test_selector_drift_repair_loop(pattern, workspace):
    provider = BrowserScriptedProvider()
    script = {
        "cloak": [
            _receipt("cloak", False, kind="selector_drift",
                     error="strict mode violation: a.card resolved to 0 elements"),
            _receipt("cloak", True, outputs={"items": ["https://example.com/1"]},
                     artifacts=[str(workspace / "s1" / "run_02_cloak" / "after.png")]),
        ],
    }
    session, reply, calls = run_turn(pattern, provider, script)

    assert len(calls) == 2
    # both attempts ran cloak: repair retries the SAME engine (attempt reset)
    assert all("--engine-order cloak" in c for c in calls)
    # the revised plan really landed (the stable data-feed selector)
    plan = json.loads(
        (workspace / "s1" / "plan.json").read_text(encoding="utf-8"))
    assert "data-feed" in plan["actions"][2]["script"]
    assert "a.card" not in plan["actions"][2]["script"]
    # reply: success after one repair round, anti-replay log on the board…
    assert "✅" in reply and "修复 1 轮" in reply
    trace = session.cxt.metadata["browser_agent"]
    assert trace["repair_rounds"] == 1
    # …and the selector-fix lesson went to the experience file
    lesson = workspace / "experience" / "general.md"
    assert lesson.exists() and "selector 修复有效" in lesson.read_text(encoding="utf-8")


# ============================================================================
# 6. Login wall takeover: wait_human suspension + same-engine resume
# ============================================================================

def test_login_wall_wait_human_resume(pattern, workspace):
    provider = BrowserScriptedProvider()
    script = {
        "cloak": [
            _receipt("cloak", False, kind="login_required",
                     error="page redirected to passport login"),
            _receipt("cloak", True, outputs={"title": "Logged In"},
                     artifacts=[str(workspace / "s1" / "run_02_cloak" / "after.png")]),
        ],
    }
    sessions = {}
    launch(pattern, sessions)
    calls = []
    stub = make_orch_stub(script, calls)
    with patch.object(ba, "build_provider", return_value=provider), \
            patch.object(ba, "_execute_tool", stub):
        first = chat(sessions, "s1", "帮我采集 example.com 首页")

    # suspension: the turn reply is the takeover guidance; cursor pinned at ba_run
    assert "登录" in first and "浏览器" in first
    state = sessions["s1"].cxt.graph_state["browser_agent_state"]
    assert state["wait_reason"] == "login_required"
    assert state["human_takeovers"] == 1
    assert sessions["s1"].cxt.graph_state["__paused_node__"] == "ba_run"

    with patch.object(ba, "build_provider", return_value=provider), \
            patch.object(ba, "_execute_tool", stub):
        second = chat(sessions, "s1", "登录好了")

    # resume retried the SAME engine (not the next one) and succeeded
    assert len(calls) == 2
    assert "--engine-order cloak" in calls[1]
    assert "✅" in second and "cloak" in second
    assert sessions["s1"].cxt.graph_state == {}  # terminated cleanly
    trace = sessions["s1"].cxt.metadata["browser_agent"]
    assert trace["success"] is True
    assert [a["failure_kind"] for a in trace["attempts"]] == [
        "login_required", ""]  # compact trail normalizes success to ""


# ============================================================================
# 7. Network retry ladder then engine switch (fallback lesson recorded)
# ============================================================================

def test_network_retry_then_engine_switch(pattern, workspace):
    provider = BrowserScriptedProvider()
    script = {
        "cloak": [
            _receipt("cloak", False, kind="network_or_timeout",
                     error="Timeout 30000ms exceeded"),
            _receipt("cloak", False, kind="network_or_timeout",
                     error="net::ERR_TIMED_OUT"),
        ],
        "browser-act": [
            _receipt("browser-act", True, outputs={"items": ["https://x.co/1"]},
                     artifacts=[str(workspace / "s1" / "run_03_browser-act" / "x.png")]),
        ],
    }
    session, reply, calls = run_turn(pattern, provider, script)

    # cloak attempt 1 → self-edge retry attempt 2 → switch → browser-act ok
    assert [c.split("--engine-order ")[1].split()[0] for c in calls] == [
        "cloak", "cloak", "browser-act"]
    assert "✅" in reply and "browser-act" in reply
    trace = session.cxt.metadata["browser_agent"]
    assert trace["selected_engine"] == "browser-act"
    assert len(trace["attempts"]) == 3
    # fallback lesson: cloak failed → browser-act worked (goes under general
    # — example.com is not a known platform domain)
    lesson = workspace / "experience" / "general.md"
    text = lesson.read_text(encoding="utf-8")
    assert "engine fallback 命中" in text and "browser-act" in text


# ============================================================================
# 8. Units: validation / platform / order / receipt parsing / lesson dedup
# ============================================================================

def test_unit_validate_plan():
    ok, err = ba._validate_plan({"url": "https://a.co", "actions": [
        {"type": "wait", "seconds": 1}]})
    assert ok, err
    ok, _ = ba._validate_plan({"url": "ftp://a.co", "actions": [
        {"type": "wait"}]})
    assert not ok
    ok, err = ba._validate_plan({"url": "https://a.co", "actions": [
        {"type": "teleport"}]})
    assert not ok and "非法" in err
    ok, err = ba._validate_plan({"url": "https://a.co", "actions": [
        {"type": "fill", "selector": "input"}]})
    assert ok, err  # fill's text is optional (empty fill is legal)
    ok, err = ba._validate_plan({"url": "https://a.co", "actions": [
        {"type": "click"}]})
    assert not ok and "selector" in err
    ok, _ = ba._validate_plan({"url": "https://a.co", "actions": []})
    assert not ok


def test_unit_platform_detection_and_order():
    assert ba._detect_platform("https://www.xiaohongshu.com/explore?x=1") == "xhs"
    assert ba._detect_platform("https://example.com") == ""
    # platform table (shared with the embedded orchestrator's CLI)
    assert ba._resolve_engine_order("xhs", "")[0] == "browser-act"
    assert ba._resolve_engine_order("ai", "") == [
        "cloak", "playwright", "browser-act", "kimi"]
    assert ba._resolve_engine_order("", "") == [
        "cloak", "browser-act", "kimi", "playwright"]
    # explicit engine preference wins (the source skill's precedence)
    assert ba._resolve_engine_order("xhs", "playwright") == ["playwright"]
    assert ba._resolve_engine_order("", "not-an-engine") == [
        "cloak", "browser-act", "kimi", "playwright"]


def test_unit_last_receipt_line():
    stdout = ("[fallback] noise line\n"
              '{"ok": false, "engine": "cloak", "failure_kind": "selector_drift"}\n'
              '{"ok": true, "engine": "cloak", "outputs": {"title": "T"}}\n')
    receipt = ba._last_receipt_line(stdout)
    assert receipt and receipt["ok"] is True
    assert ba._last_receipt_line("no json here") is None


def test_unit_plan_degrades_without_url():
    assert ba._minimal_plan("没有链接的请求") is None
    plan = ba._minimal_plan("帮我看看 https://example.com/page, 谢谢")
    assert plan and plan["url"] == "https://example.com/page"
    ok, _ = ba._validate_plan(plan)
    assert ok


def test_unit_lesson_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr(ba, "_DEFAULT_WORKSPACE_ROOT", str(tmp_path))
    first = ba._record_lesson("douyin", "减少滚动轮次到 2 轮可降低验证码概率")
    assert first and Path(first).exists()
    assert ba._record_lesson("douyin", "减少滚动轮次到 2 轮可降低验证码概率") is None
    assert ba._record_lesson("", "no platform") is None
