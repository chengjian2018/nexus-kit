"""Offline tests for session_reviewer (会话评审与应用优化, 八站 AGENT 图).

Covers (all offline — the reviewed sessions DB is a tmp SQLite, the target
app is a tmp fake tree under a pinned apps root, pytest runs ride a scripted
bash stub; no network, no repo-file writes):

1. Graph structure: eight stations, adjacency (incl. escape/self edges),
   executor plugin bindings, deny-by-default tool grants (only apply/fix
   carry bash), validate_pattern, the max_steps budget; eight executor
   plugins registered; zero tool registrations from this app.
2. App config overlay: the repo config.yaml's loop budgets / free bag
   (auto_approve / fix_rounds / reload_url / workspace_root) reach the
   settings layer.
3. Full walk: review → gate suspends (wait_human, reply = suggestion
   listing, __paused_node__ pinned) → resume「通过」→ backup + edit lands
   on the fake app's prompts.py (route.py-level advice untouched) →
   whitelisted pytest ok → report file + chat summary + trace; state board
   cleared.
4. Gate reject: resume「取消」→ report-only, zero bash calls, file
   untouched.
5. Test failure → one fix round → pass: apply's pytest fails, the fix
   station edits further, the rerun passes (fix_history anti-replay).
6. Test failure → rounds exhausted → byte rollback: file bytes restored,
   rolled_back trace, honest reply.
7. auto_approve: the gate passes without asking (single-turn full walk).
8. Honest escapes: missing session id (ask-and-end), DB missing
   (data_error report-only), degraded review output (no fabricated
   suggestions).
9. Units: session_id extraction / app-dir mapping / edit-pair application /
   approval parsing / metrics computation.
"""

import json
import logging
import time
import types
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest

from async_utils import arun

logging.basicConfig(level=logging.WARNING)

# Host load order is "tools discovered first, patterns loaded later"
from nexus.registry.tools import discover_builtin_tools

discover_builtin_tools()

import apps.session_reviewer_agent.executor as sr
from apps.session_reviewer_agent.prompts import (
    EDIT_ANCHOR,
    FIX_ANCHOR,
    REVIEW_ANCHOR,
)

_REVIEWED = "sess-demo-1234"
_ORIG_PROMPT = 'PROMPT = "你是一个安装预约助理"\n'


# ============================================================================
# Fixtures / helpers
# ============================================================================

@pytest.fixture(scope="module")
def pattern():
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.session_reviewer_agent.route" in imported, (
        f"route 未被自动发现,已发现: {imported}")
    return registry.get("session_reviewer")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, pattern_code TEXT NOT NULL,
    launch_epoch INTEGER NOT NULL DEFAULT 0, request_id TEXT,
    task_info TEXT NOT NULL DEFAULT '{}', current_node_code TEXT,
    graph_state TEXT NOT NULL DEFAULT '{}',
    filled_slots TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, last_active_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    launch_epoch INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL,
    content TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS trace_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '', launch_epoch INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
    truncated INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
"""


async def _make_db(path: Path, session_id: str = _REVIEWED,
                   pattern_code: str = "fake_target"):
    conn = await aiosqlite.connect(str(path))
    await conn.executescript(_SCHEMA)
    now = time.time()
    await conn.execute(
        """INSERT INTO sessions (session_id, pattern_code, launch_epoch,
           request_id, task_info, current_node_code, graph_state,
           filled_slots, created_at, last_active_at)
           VALUES (?, ?, 0, 'req-1', '{}', 'ft_end', '{}', '{}', ?, ?)""",
        (session_id, pattern_code, now, now))
    await conn.executemany(
        """INSERT INTO messages (session_id, launch_epoch, role, content,
           stage, metadata, created_at) VALUES (?, 0, ?, ?, ?, '{}', ?)""",
        [(session_id, "user", "帮我约明天下午三点安装", "", now),
         (session_id, "assistant", "好的，已为您预约明天 15:00", "nlg", now),
         (session_id, "assistant", "请问约上午还是下午？", "clarify", now)])
    events = [
        ("node_start", {"node_code": "ft_route"}),
        ("node_start", {"node_code": "ft_ask"}),
        ("node_start", {"node_code": "ft_ask"}),      # revisit
        ("tool_call", {"tool_name": "calendar"}),
        ("tool_result", {"error": "calendar busy"}),  # failed tool
        ("turn_error", {"error": "boom"}),
    ]
    await conn.executemany(
        """INSERT INTO trace_events (session_id, turn_id, launch_epoch, kind,
           payload, truncated, created_at) VALUES (?, 't1', 0, ?, ?, 0, ?)""",
        [(session_id, kind, json.dumps(p, ensure_ascii=False), now)
         for kind, p in events])
    await conn.commit()
    await conn.close()


@pytest.fixture()
def env(pattern, tmp_path, monkeypatch):
    """A fully pinned world: workspace / fake apps tree / fake tests root /
    tmp sessions DB — nothing under the real repo is read for data or
    written."""
    root = tmp_path / "world"
    ws = root / "ws"
    apps = root / "apps"
    tests = root / "tests"
    app = apps / "fake_target"
    app.mkdir(parents=True)
    (app / "config.yaml").write_text("pattern: fake_target\n", encoding="utf-8")
    (app / "prompts.py").write_text(_ORIG_PROMPT, encoding="utf-8")
    (app / "route.py").write_text(
        'from nexus.model.pattern import Pattern\n'
        'pattern = Pattern(code="fake_target", name="f", nodes=[])\n',
        encoding="utf-8")
    tests.mkdir(parents=True)
    (tests / "test_fake_target_route.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    db = root / "dialogue.db"
    arun(_make_db(db))

    monkeypatch.setattr(sr, "_DEFAULT_WORKSPACE_ROOT", str(ws))
    monkeypatch.setattr(sr, "_DEFAULT_APPS_ROOT", str(apps))
    monkeypatch.setattr(sr, "_DEFAULT_TESTS_ROOT", str(tests))
    monkeypatch.setattr(sr, "_db_path", lambda settings: str(db))
    return types.SimpleNamespace(root=root, ws=ws, apps=apps, tests=tests,
                                 app=app, db=db)


def launch(pattern, sessions, session_id="s1", task_info=None):
    from nexus.engine.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = task_info or {}
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions[session_id] = session
    return session


def chat(sessions, session_id, query):
    from nexus.engine.chat import chat as chat_fn

    return arun(chat_fn(query=query, session_id=session_id,
                        all_sessions=sessions))


# ============================================================================
# Scripted provider (anchor detection) + pytest bash stub
# ============================================================================

_SUGGESTIONS = {
    "summary": "回复越权承诺 + 节点回环两个根因",
    "suggestions": [
        {"id": "S1", "dimension": "prompt", "severity": "high",
         "problem": "提示词缺少拒绝/复核约束，出现越权承诺",
         "evidence": "turn t1 assistant「已为您预约明天 15:00」先于工具成功",
         "suggestion": "在 prompts.py 的 PROMPT 中追加复核约束",
         "target_file": "prompts.py", "target_kind": "prompt"},
        {"id": "S2", "dimension": "routing", "severity": "medium",
         "problem": "ft_ask 节点一轮内重访",
         "evidence": "node_start ft_ask ×2 (t1)",
         "suggestion": "收紧守卫避免重复澄清",
         "target_file": "route.py", "target_kind": "pattern"},
    ],
}

_NEW_RULE = "；不得承诺未经确认的时间，必须先复核日历"

_EDITS = {
    "summary": "追加复核约束",
    "edits": [
        {"old_string": "你是一个安装预约助理",
         "new_string": "你是一个安装预约助理" + _NEW_RULE,
         "rationale": "S1"},
        {"old_string": "这个锚点不存在",
         "new_string": "x", "rationale": "坏编辑（应被跳过）"},
    ],
}

_FIX_EDITS = {
    "summary": "按测试失败修补",
    "edits": [
        {"old_string": _NEW_RULE,
         "new_string": _NEW_RULE + "（v2）",
         "rationale": "修复断言"},
    ],
}


class ReviewerScriptedProvider:
    """Answers review/edit/fix phase prompts by anchor; records requests."""

    def __init__(self, review_reply=None, edit_reply=None, fix_reply=None):
        self.review_reply = review_reply \
            or json.dumps(_SUGGESTIONS, ensure_ascii=False)
        self.edit_reply = edit_reply \
            or json.dumps(_EDITS, ensure_ascii=False)
        self.fix_reply = fix_reply \
            or json.dumps(_FIX_EDITS, ensure_ascii=False)
        self.requests = []

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        self.requests.append([dict(m) for m in messages])
        last_user = next((m["content"] for m in reversed(messages)
                          if m.get("role") == "user"), "")
        if REVIEW_ANCHOR in last_user:
            return {"content": self.review_reply}
        if EDIT_ANCHOR in last_user:
            return {"content": self.edit_reply}
        if FIX_ANCHOR in last_user:
            return {"content": self.fix_reply}
        return {"content": "{}"}


def make_bash_stub(outcomes, calls):
    """bash stub: pytest commands get scripted envelopes (popped in order);
    other commands fall through to the real registry."""
    real = sr._execute_tool  # captured before the patch replaces it

    async def fake(name, args):
        if name != "bash":
            return await real(name, args)
        calls.append(str(args.get("command") or ""))
        outcome = outcomes.pop(0) if outcomes else {"exit_code": 0}
        return json.dumps({
            "exit_code": outcome.get("exit_code", 0),
            "stdout": outcome.get("stdout", "3 passed in 0.01s"),
            "stderr": outcome.get("stderr", ""),
            "timed_out": False,
        }, ensure_ascii=False)

    return fake


def run_turn(pattern, provider, bash_outcomes, query,
             session_id="s1", sessions=None):
    sessions = sessions if sessions is not None else {}
    launch(pattern, sessions, session_id)
    calls: list = []
    stub = make_bash_stub(bash_outcomes, calls)
    with patch.object(sr, "build_provider", return_value=provider), \
            patch.object(sr, "_execute_tool", stub):
        reply = chat(sessions, session_id, query)
    return sessions[session_id], reply, calls


# ============================================================================
# 1. Graph structure and validation
# ============================================================================

def test_pattern_structure_and_validation(pattern):
    assert pattern.code == "session_reviewer"
    assert pattern.pattern_type == "agent"
    assert pattern.entry_node_code == "sr_route"
    assert [n.code for n in pattern.nodes] == [
        "sr_route", "sr_collect", "sr_metrics", "sr_review",
        "sr_wait_human", "sr_apply", "sr_fixloop", "sr_report"]
    node_map = pattern.node_map
    assert node_map["sr_route"].sub_nodes == ["sr_collect"]
    assert set(node_map["sr_collect"].sub_nodes) == {"sr_metrics", "sr_report"}
    assert node_map["sr_metrics"].sub_nodes == ["sr_review"]
    assert set(node_map["sr_review"].sub_nodes) == {"sr_wait_human", "sr_report"}
    assert set(node_map["sr_wait_human"].sub_nodes) == {"sr_apply", "sr_report"}
    assert set(node_map["sr_apply"].sub_nodes) == {"sr_fixloop", "sr_report"}
    assert set(node_map["sr_fixloop"].sub_nodes) == {"sr_fixloop", "sr_report"}
    assert node_map["sr_report"].sub_nodes == []
    assert node_map["sr_report"].is_end is True
    for code in ("sr_route", "sr_collect", "sr_metrics", "sr_review",
                 "sr_wait_human", "sr_apply", "sr_fixloop", "sr_report"):
        assert node_map[code].plugins == {"loop": code}, code
    # deny-by-default grants: pattern toolset shell; only apply/fix narrow to bash
    assert pattern.allow_toolset == ["shell"]
    assert node_map["sr_apply"].use_tools == ["bash"]
    assert node_map["sr_fixloop"].use_tools == ["bash"]
    for code in ("sr_route", "sr_collect", "sr_metrics", "sr_review",
                 "sr_wait_human", "sr_report"):
        assert node_map[code].use_tools == [], code
    assert pattern.max_steps == 16

    from nexus.model.validation import validate_pattern
    validate_pattern(pattern)

    from nexus.registry.plugins import registry as plugin_registry
    for code in ("sr_route", "sr_collect", "sr_metrics", "sr_review",
                 "sr_wait_human", "sr_apply", "sr_fixloop", "sr_report"):
        assert plugin_registry.has("executor", code), f"executor 插件 {code} 未注册"


def test_zero_tool_registrations(pattern):
    """The app registers no new tools — pytest rides the builtin bash tool."""
    app_dir = Path(__file__).resolve().parents[1] / "apps" / "session_reviewer_agent"
    for src in app_dir.glob("*.py"):
        text = src.read_text(encoding="utf-8")
        assert "nexus.registry.tools" not in text, src.name
    assert (app_dir / "route.py").read_text(encoding="utf-8").count(
        "registry.register(") == 1


# ============================================================================
# 2. App config overlay (the repo apps/session_reviewer_agent/config.yaml)
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
        assert resolve_max_steps(pattern) == 16
        assert get_loop_limits(
            "session_reviewer", "sr_apply")["max_tool_rounds"] == 6
        bag = get_pattern_custom_config("session_reviewer")
        assert bag["auto_approve"] is False
        assert bag["fix_rounds"] == 2
        assert bag["run_tests"] is True
        assert bag["reload_url"] == "http://127.0.0.1:8000/api/v1/reload"
        assert bag["workspace_root"] == "data/session_reviewer_agent"
    finally:
        settings_mod.invalidate_config_cache()


# ============================================================================
# 3. Full walk: review → wait_human → resume「通过」→ apply → report
# ============================================================================

def test_full_walk_gate_approve_apply(pattern, env):
    provider = ReviewerScriptedProvider()
    sessions = {}
    calls: list = []
    stub = make_bash_stub([{"exit_code": 0}], calls)
    launch(pattern, sessions)
    with patch.object(sr, "build_provider", return_value=provider), \
            patch.object(sr, "_execute_tool", stub):
        first = chat(sessions, "s1", f"评审会话 {_REVIEWED}")

    # suspension: the turn reply IS the suggestion listing; cursor pinned
    assert "评审建议清单" in first
    assert "S1" in first and "S2" in first
    assert "仅建议" in first          # S2 (route.py) is advice-only
    state = sessions["s1"].cxt.graph_state["session_reviewer_state"]
    assert len(state["suggestions"]) == 2
    assert sessions["s1"].cxt.graph_state["__paused_node__"] == "sr_wait_human"
    # the review prompt really carried the deterministic metrics
    assert any("turn_errors" in m["content"]
               for m in provider.requests[-1])
    assert calls == []                # no shell before the gate

    with patch.object(sr, "build_provider", return_value=provider), \
            patch.object(sr, "_execute_tool", stub):
        second = chat(sessions, "s1", "通过，但 S2 不改")

    # the edit really landed on the fake app; the bad edit was skipped
    text = (env.app / "prompts.py").read_text(encoding="utf-8")
    assert _NEW_RULE in text
    assert "这个锚点不存在" not in text
    # byte snapshot exists and holds the original
    backups = list((env.ws / "s1").glob("backup_*/prompts.py"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == _ORIG_PROMPT
    # one whitelisted pytest run with only the globbed file
    assert len(calls) == 1 and "test_fake_target_route.py" in calls[0]
    assert calls[0].startswith("python -m pytest") and "-x -q" in calls[0]
    # report + summary + trace; state board cleared on termination
    reports = list((env.ws / "s1").glob("report_*.md"))
    assert len(reports) == 1
    report_text = reports[0].read_text(encoding="utf-8")
    assert "## 规则指标" in report_text and "## 实施记录" in report_text
    assert "```diff" in report_text
    assert "评审汇报" in second and "已修改 prompts.py" in second
    assert str(reports[0]) in second
    assert sessions["s1"].cxt.graph_state == {}
    trace = sessions["s1"].cxt.metadata["session_reviewer"]
    assert trace["applied_files"] == ["prompts.py"]
    assert trace["test_ok"] is True
    assert trace["decision"]["approved"] is True
    assert "gate_approved" in trace["phases"]
    # input.json landed (the fuller collection dump)
    assert (env.ws / "s1" / "input.json").is_file()


# ============================================================================
# 4. Gate reject: report-only, zero shell, file untouched
# ============================================================================

def test_gate_reject_report_only(pattern, env):
    provider = ReviewerScriptedProvider()
    sessions = {}
    calls: list = []
    stub = make_bash_stub([], calls)
    launch(pattern, sessions)
    with patch.object(sr, "build_provider", return_value=provider), \
            patch.object(sr, "_execute_tool", stub):
        chat(sessions, "s1", f"评审会话 {_REVIEWED}")
        second = chat(sessions, "s1", "取消")

    assert "评审汇报" in second
    assert (env.app / "prompts.py").read_text(encoding="utf-8") == _ORIG_PROMPT
    assert calls == []
    trace = sessions["s1"].cxt.metadata["session_reviewer"]
    assert trace["applied_files"] == []
    assert trace["decision"]["approved"] is False
    assert trace["test_skipped"] is True
    assert "gate_rejected" in trace["phases"]


# ============================================================================
# 5. Test failure → one fix round → pass
# ============================================================================

def test_apply_testfail_fix_pass(pattern, env):
    provider = ReviewerScriptedProvider()
    outcomes = [{"exit_code": 1, "stdout": "1 failed in 0.02s"},
                {"exit_code": 0}]
    sessions = {}
    calls: list = []
    stub = make_bash_stub(outcomes, calls)
    launch(pattern, sessions)
    with patch.object(sr, "build_provider", return_value=provider), \
            patch.object(sr, "_execute_tool", stub):
        chat(sessions, "s1", f"评审会话 {_REVIEWED}")
        second = chat(sessions, "s1", "通过")

    assert len(calls) == 2            # apply run + fix rerun
    text = (env.app / "prompts.py").read_text(encoding="utf-8")
    assert _NEW_RULE + "（v2）" in text
    assert "自修: 1 轮" in second
    trace = sessions["s1"].cxt.metadata["session_reviewer"]
    assert trace["test_ok"] is True
    assert trace["fix_rounds"] == 1
    assert trace["rolled_back"] is False


# ============================================================================
# 6. Test failure → rounds exhausted → byte rollback
# ============================================================================

def test_apply_fail_rollback(pattern, env):
    provider = ReviewerScriptedProvider(
        fix_reply=json.dumps({"summary": "无能为力", "edits": []},
                             ensure_ascii=False))
    outcomes = [{"exit_code": 1, "stdout": "1 failed"}] * 3
    sessions = {}
    calls: list = []
    stub = make_bash_stub(outcomes, calls)
    launch(pattern, sessions)
    with patch.object(sr, "build_provider", return_value=provider), \
            patch.object(sr, "_execute_tool", stub):
        chat(sessions, "s1", f"评审会话 {_REVIEWED}")
        second = chat(sessions, "s1", "通过")

    # apply + 2 fix rounds = 3 pytest runs, then rollback
    assert len(calls) == 3
    assert (env.app / "prompts.py").read_text(encoding="utf-8") == _ORIG_PROMPT
    assert "已回滚" in second
    trace = sessions["s1"].cxt.metadata["session_reviewer"]
    assert trace["rolled_back"] is True
    assert trace["test_ok"] is False
    assert trace["fix_rounds"] == 3   # 2 fix rounds + the rollback entry
    report = list((env.ws / "s1").glob("report_*.md"))[0].read_text("utf-8")
    assert "## 回滚" in report


# ============================================================================
# 7. auto_approve: the gate passes without asking
# ============================================================================

def test_auto_approve_single_turn(pattern, env):
    provider = ReviewerScriptedProvider()
    sessions = {}
    calls: list = []
    stub = make_bash_stub([{"exit_code": 0}], calls)
    launch(pattern, sessions)
    with patch.object(sr, "build_provider", return_value=provider), \
            patch.object(sr, "_execute_tool", stub), \
            patch.object(sr, "get_pattern_custom_config",
                         lambda code: {"auto_approve": True}):
        reply = chat(sessions, "s1", f"评审会话 {_REVIEWED}")

    assert "评审汇报" in reply and "已修改 prompts.py" in reply
    assert sessions["s1"].cxt.graph_state == {}     # no suspension at all
    trace = sessions["s1"].cxt.metadata["session_reviewer"]
    assert trace["decision"] == {"approved": True, "mode": "auto",
                                 "note": "(auto_approve=true 自动通过)"}
    assert "gate_auto" in trace["phases"]
    assert _NEW_RULE in (env.app / "prompts.py").read_text(encoding="utf-8")


# ============================================================================
# 8. Honest escapes
# ============================================================================

def test_missing_session_id_asks_and_ends(pattern, env):
    provider = ReviewerScriptedProvider()
    sessions = {}
    launch(pattern, sessions)
    with patch.object(sr, "build_provider", return_value=provider):
        reply = chat(sessions, "s1", "帮我评审一下")
    assert "会话 ID" in reply
    assert sessions["s1"].cxt.graph_state == {}


def test_db_missing_report_only(pattern, env, monkeypatch):
    provider = ReviewerScriptedProvider()
    monkeypatch.setattr(sr, "_db_path",
                        lambda settings: str(env.root / "nope.db"))
    sessions = {}
    launch(pattern, sessions)
    with patch.object(sr, "build_provider", return_value=provider):
        reply = chat(sessions, "s1", f"评审会话 {_REVIEWED}")
    assert "取数失败" in reply
    trace = sessions["s1"].cxt.metadata["session_reviewer"]
    assert trace["data_error"] and trace["suggestions"] == 0
    assert "collect_failed" in trace["phases"]


def test_degraded_review_output(pattern, env):
    provider = ReviewerScriptedProvider(review_reply="这不是 JSON")
    sessions = {}
    launch(pattern, sessions)
    with patch.object(sr, "build_provider", return_value=provider):
        reply = chat(sessions, "s1", f"评审会话 {_REVIEWED}")
    assert "降级" in reply
    trace = sessions["s1"].cxt.metadata["session_reviewer"]
    assert trace["review_degraded"] is True
    assert trace["suggestions"] == 0      # never fabricated
    assert "review_degraded" in trace["phases"]
    # the retry really happened (review prompt seen twice + a retry prompt)
    review_calls = [m for req in provider.requests for m in req
                    if REVIEW_ANCHOR in str(m.get("content"))]
    assert len(review_calls) >= 2


# ============================================================================
# 9. Units
# ============================================================================

def _cxt(metadata=None):
    return types.SimpleNamespace(metadata=metadata or {})


def test_unit_session_id_extraction():
    sid, src = sr._extract_session_id(_cxt(), f"评审会话 {_REVIEWED}")
    assert (sid, src) == (_REVIEWED, "message")
    sid, _ = sr._extract_session_id(_cxt(), "session_id: abc12345 谢谢")
    assert sid == "abc12345"
    sid, src = sr._extract_session_id(
        _cxt({"task_info": {"session_id": "from-launch"}}), "随便说点什么")
    assert (sid, src) == ("from-launch", "task_info")
    assert sr._extract_session_id(_cxt(), "帮我看看这个应用") == ("", "")


def test_unit_locate_app_dir(tmp_path):
    apps = tmp_path / "apps"
    (apps / "a").mkdir(parents=True)
    (apps / "a" / "config.yaml").write_text("pattern: pa\n", encoding="utf-8")
    (apps / "b").mkdir()
    (apps / "b" / "route.py").write_text(
        'Pattern(code="pb", name="x")\n', encoding="utf-8")
    (apps / "c").mkdir()
    assert sr._locate_app_dir("pa", apps) == apps / "a"       # config binding
    assert sr._locate_app_dir("pb", apps) == apps / "b"       # declaration regex
    assert sr._locate_app_dir("pc", apps) is None
    assert sr._locate_app_dir("", apps) is None


def test_unit_apply_edit_pairs():
    text = "line1\nline2\nline2\n"
    new, applied, skipped = sr._apply_edit_pairs(text, [
        {"old_string": "line1", "new_string": "LINE1", "rationale": "r"},
        {"old_string": "line2", "new_string": "LINE2", "rationale": "r"},
        {"old_string": "missing", "new_string": "x", "rationale": "r"},
        {"old_string": "", "new_string": "y", "rationale": "r"},
    ])
    assert new == "LINE1\nLINE2\nline2\n"   # first occurrence only
    assert len(applied) == 2 and applied[1]["occurrences"] == 2
    assert len(skipped) == 2


def test_unit_parse_approval():
    assert sr._parse_approval("通过") == "approve"
    assert sr._parse_approval("不通过") == "reject"
    assert sr._parse_approval("通过，但 S2 不改") == "approve"
    assert sr._parse_approval("ok, apply") == "approve"
    assert sr._parse_approval("取消") == "reject"
    assert sr._parse_approval("这周五有空吗") == "unknown"
    assert sr._parse_approval("") == "unknown"


def test_unit_compute_metrics():
    messages = [
        {"role": "user", "content": "q", "stage": ""},
        {"role": "assistant", "content": "a", "stage": "clarify"},
    ]
    events = [
        {"turn_id": "t1", "kind": "node_start", "payload": {"node_code": "n1"}},
        {"turn_id": "t1", "kind": "node_start", "payload": {"node_code": "n1"}},
        {"turn_id": "t1", "kind": "tool_call", "payload": {"tool_name": "x"}},
        {"turn_id": "t1", "kind": "tool_result", "payload": {"error": "e"}},
        {"turn_id": "t2", "kind": "turn_error", "payload": {}},
    ]
    m = sr._compute_metrics(messages, events)
    assert m["turns"] == 2
    assert m["turn_errors"] == 1
    assert m["tool_calls"] == 1 and m["tool_failures"] == 1
    assert m["clarify_total"] == 1
    assert m["revisited_nodes"] == {"n1": 2}
    assert m["user_messages"] == 1
