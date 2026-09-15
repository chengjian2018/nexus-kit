"""archify(九节点图表工程 AGENT 图)离线测试。

ArchifyScriptedProvider(相位特征锚点)与 bash 回执桩自包含于本文件;
覆盖:
1. 图结构 + AST 自动发现 + 九站 executor 插件注册 + validate_pattern +
   max_steps 预算(修复回路吃步数)
2. 全链路一轮:route → author(read schema → write 候选)→ probe(silent)
   → validate(showcase 通过)→ deliver → visual-check → percept →
   report;回复从回执组装、三级证明分离、感知审查按评审回执陈述;
   history 无 tool 行 / 图终止清空 graph_state / trace 落 metadata
3. 收敛诚实出口:验证连续失败不改进 → 修复站在第 3 次访问走确定性
   诚实出口(第 3 次不调 LLM),汇报含未解决诊断
4. 改进后通过:fail(2) → fail(1) → pass,冻结并交付,repair 2 轮
5. 探针通知:update_available → 汇报含紧凑通知 + eventKey ack 命令
6. 交付失败逃生边:非零退出 → 直达 af_report,visual-check 未运行,
   汇报明示"失败(非零退出,绝不称为成功)"
7. 感知评审站:多模态附图(passed/failed 如实进汇报)/ 无证据 skipped /
   截图文件缺失 skipped / 评审模型无图像能力 skipped(zai 注册表声明)/
   判定输出不可解析自纠后 skipped(绝不编造通过)
8. 仓库证据:architecture 候选声明 sources/meta.repository 时,
   validate/deliver/修复站自验命令条件化拼 --repo-root
   (repository-evidence/root-required 死锁的解锁口)
9. 单元:_trailing_stale 收敛语义 / 回执错误计数 / showcase 验收判定 /
   候选缺失的客观错误(不跑 bash)
10. Phase 3 迁移:app config bag 的 repair_rounds 覆盖代码默认预算;
    站点 dispatch 路径发布 pattern_code(app 护栏覆盖的定位键)
"""

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from async_utils import arun

logging.basicConfig(level=logging.WARNING)

# 宿主装载顺序是"工具先发现、pattern 后装载"(validate_tools 注册期校验);
# 离线测试进程同样先发现内置工具,再导入/发现 pattern
from nexus.registry.tools import discover_builtin_tools

discover_builtin_tools()

# 感知站的能力守卫查注册表(zai 声明 vision_models)——保证注册表里有 zai
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
    """工作区根指到 tmp(skill_dir 同理——bash 本就打桩,仅命令拼装引用)。"""
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
    # 引擎在轮末把 metadata["archify"] 捡进 trace trail 后摘除
    # （docs/design/session-persistence.md §6）——挂捕获 sink 以便断言取回。
    rows = []

    async def _trace_sink(ev):
        rows.append(ev)

    session.cxt.trace_sink = _trace_sink
    session._app_trace_rows = rows
    sessions[session_id] = session
    return session


def app_trace_of(session):
    """取该 session 最后一轮被引擎捡回的 archify 终态 trace。"""
    for row in reversed(getattr(session, "_app_trace_rows", [])):
        if row["kind"] == "app_trace":
            return row["payload"]["data"]["trace"]
    raise AssertionError("turn 未捡回 app_trace（metadata['archify'] 丢失?）")


def chat(sessions, session_id, query):
    from nexus.engine.chat import chat as chat_fn

    return arun(chat_fn(query=query, session_id=session_id,
                        all_sessions=sessions))


# ============================================================================
# Scripted provider (相位锚点识别;author 两轮:read → write → 收口)
# ============================================================================

_CANDIDATE_JSON = json.dumps(
    {"schema_version": 2, "diagram_type": "workflow", "meta": {
        "title": "测试工作流", "locale": "zh-CN",
        "quality_profile": "showcase"}},
    ensure_ascii=False)


class ArchifyScriptedProvider:
    """按相位特征脚本化:route(ROUTE 锚点)/ author(tools + AUTHOR 锚点,
    第 1 轮 find、第 2 轮 read、第 3 轮 write、第 4 轮收口)/ repair(
    tools + REPAIR 锚点,按 repair_edits 次数发 edit_file,之后空转——
    修复站内部微循环 ≤3 轮/访,空转轮即本访收束)/ percept(PERCEPT 锚点,
    无 tools;user content 是多模态 parts 数组,按 percept_outputs 队列
    出票,耗尽回落 percept_verdict 的 JSON)。"""

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
        self.write_path = write_path  # 模型实际写入的目标路径(可自选错路径)
        self.route_type = route_type  # 路由相位返回的图表类型
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
        """消息文本(感知站的 content 是 parts 数组:文本 part 取 text
        字段,图像 part 只留标记——锚点识别用,不搬 base64)。"""
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
        return "route"  # 兜底(route 是唯一无锚点的相位)

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
# bash 回执桩(validate/deliver/visual-check/check-update 按队列出票;
# read/write/edit 放行真实文件工具)
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


# visual-check 截图侧车(visual-check.mjs sidecarPaths 的命名契约:
# <stem>.visual-check.<WxH>.<theme>.png × 4,与交付 HTML 同目录)
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
    """默认回执配套落盘假 PNG(感知站只做 base64,不解析像素)。
    显式传入 visual_receipt 的测试不落盘——正好覆盖"回执列出但文件
    缺失"的诚实 skipped 路径。"""
    root = ax._absolutize(ax._DEFAULT_WORKSPACE_ROOT) / "s1"
    root.mkdir(parents=True, exist_ok=True)
    for s in (receipt.get("captures") or {}).get("screenshots") or []:
        if isinstance(s, dict) and s.get("file"):
            (root / str(s["file"])).write_bytes(b"\x89PNG-rn-fake-bytes")


def make_cli_stub(validate_receipts, deliver_receipt=None,
                  visual_receipt=None, probe_receipt=None, calls=None):
    """返回 _execute_tool 桩:bash 按命令关键词出票(队列耗尽复用末张),
    其他工具走真实注册表(文件读写落 tmp 工作区)。__CAND__ 占位符替换。"""
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
    """确定式候选路径:<workspace_root>/<session_id>/<output_name>.json
    (scripted 路由回复 output_name=demo,会话 s1;与执行器同逻辑取绝对)。"""
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
    assert pattern.max_steps == 20  # 修复回路预算(stale-5 诚实退出全程 16 步)

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
    # 交付失败逃生边:非零退出直达汇报站(不跑浏览器检查)
    assert pattern.node_map["af_deliver"].sub_nodes == [
        "af_visual_check", "af_report"]
    # 感知评审:浏览器证据之后、汇报之前(零工具纯语义站)
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
    # 创作轮次预算(代码默认;app config bag 的 author_rounds 可覆盖):
    # find 示例 + 读×3 + 写 + 收口 + 一次容错(实跑证明 6 轮零余量,
    # 一次磕绊即耗尽 → 候选缺失)
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

    # 相位顺序:route → author(4 轮:find 示例→读 schema→写候选→收口)
    # → probe → validate → deliver → visual-check → percept(1 轮多模态);
    # report 不调 LLM(从回执组装)
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

    # 候选真实落盘(tmp 工作区)
    cand = _candidate_path()
    assert json.loads(Path(cand).read_text(encoding="utf-8"))["meta"][
        "quality_profile"] == "showcase"

    # 汇报从回执组装:三级证明分离 + 感知审查按评审回执陈述
    assert "showcase 验收通过" in reply
    assert "交付: 成功" in reply
    assert "浏览器证据: pass" in reply
    assert "感知审查: passed(图像能力评审 x/m,4 张截图" in reply
    assert "感知审查: 未执行" not in reply
    assert "更新探针" not in reply  # silent 不提及

    # 感知请求是 OpenAI 风格多模态 parts:1 个文本(锚点+清单)+ 4 个图像
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

    # 终态:trace 落 metadata;图终止清空 graph_state;位置在汇报站
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
    # 研究过程不入会话历史(私有工作区)
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

    # v1(2) 基线 → r1..r5 → v2..v6(2) → r6 = 收敛闸门拦截(不调 LLM)
    assert len([c for c in calls if "validate" in c]) == 6
    # 五次修复访问,每次内部微循环封顶 3 轮:
    # 3(edit×3)+ 3(edit×2+空转)+ 1+1+1(空转收束)
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
    assert "诊断0" in reply and "诊断1" in reply  # 未解决诊断如实呈报
    assert "交付" not in reply  # 未走到交付站,不得出现任何交付声明
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
    # visit1: edit×2 + 空转收束(3 轮);visit2: 首轮空转(1 轮)
    assert provider.repair_calls == 4
    trace = app_trace_of(session)
    assert trace["repair_rounds"] == 2
    assert trace["val_history"] == [2, 1, 0]
    assert trace["frozen"] is True
    assert trace["honest_exit"] is False
    assert "修复: 2 轮" in reply
    assert "showcase 验收通过" in reply


# ============================================================================
# 4a. Deterministic label-clearance solver (studio 回归:label-route-clearance
#     无建议坐标,LLM 六轮做不出像素避让 → 几何归工具,真验证器裁决)
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
    """studio 实跑的原样诊断:48px 标签 rect 挤在 x=665 竖直段旁,
    正确挪移是 labelDy +12(|delta| 排序的第一候选)。"""
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
    """求解器一发命中:闸门 fail(1) → 挪移 labelDy +12 经真验证器裁决
    更优(0)→ 直接回验证闸门冻结交付——修复站全程零 LLM 调用。"""
    provider = ArchifyScriptedProvider(candidate=_LABEL_CANDIDATE_JSON)
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_label_clearance_receipt(), _pass_receipt(),
                           _pass_receipt()],
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # 3 次 validate:闸门v1(fail) + 求解器试验1(pass,接受) + 闸门v2(pass)
    assert len([c for c in calls if "validate" in c]) == 3
    assert provider.repair_calls == 0   # 求解器清零,LLM 微循环未开启
    trace = app_trace_of(session)
    assert trace["val_history"] == [1, 0]
    assert trace["frozen"] is True
    assert trace["repair_rounds"] == 1
    # 挪移真实落盘:connections[2] 得到 labelDy 12(就近第一候选)
    data = json.loads(Path(trace["candidate_path"]).read_text(
        encoding="utf-8"))
    assert data["connections"][2]["labelDy"] == 12.0
    # 求解器动作进修复履历(下一访的 LLM 能看到几何已被工具处理过)
    assert "labelDy +12" in trace["repair_log"][0]["summary"]
    assert "showcase 验收通过" in reply
    assert "交付: 成功" in reply


# 声明仓库证据的同一张标签图(studio 会话 f2cae679 死锁形态:闸门带
# --repo-root 只剩 1 个 clearance 错,求解器验证却不带旗标 → 永远看到
# root-required,判"无改进"全回滚,几何可解的问题交给 LLM 越修越糟)
_label_evidence_data = json.loads(_LABEL_CANDIDATE_JSON)
_label_evidence_data["meta"]["repository"] = {
    "url": "https://github.com/o/nexus-kit.git", "revision": "0" * 40}
_EVIDENCE_LABEL_CANDIDATE_JSON = json.dumps(_label_evidence_data,
                                            ensure_ascii=False)


def test_solver_validate_carries_repo_root_for_evidence(pattern, workspace,
                                                         monkeypatch):
    """求解器的真验证守卫必须与闸门同源(同 --repo-root):声明证据的
    候选上,不带旗标的试验验证只会看到 root-required(1 错),对 1 错
    基线判"无改进"→ 全部回滚并记 solver_tried(跨访不再试),label
    避让死锁。带旗标后同一挪移一发命中,零 LLM 冻结交付。"""
    monkeypatch.setattr(ax, "_DEFAULT_REPO_ROOT", "/repo/ev")
    provider = ArchifyScriptedProvider(
        candidate=_EVIDENCE_LABEL_CANDIDATE_JSON, route_type="architecture")
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_label_clearance_receipt(), _pass_receipt(),
                           _pass_receipt()],
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # 闸门 + 求解器试验 + 复核闸门:三次 validate 全部同源带旗标。
    # 匹配用命令前缀而非 "validate" 子串:pytest 临时目录名取自测试函数
    # 名(含 "validate"),路径嵌进 deliver/visual-check 命令后子串误命中
    vc = [c for c in calls if "archify.mjs validate " in c]
    assert len(vc) == 3
    assert all('--repo-root "/repo/ev"' in c for c in vc)
    assert provider.repair_calls == 0   # 求解器清零,没有 LLM 越修越糟
    trace = app_trace_of(session)
    assert trace["val_history"] == [1, 0]
    assert trace["frozen"] is True
    data = json.loads(Path(trace["candidate_path"]).read_text(
        encoding="utf-8"))
    assert data["connections"][2]["labelDy"] == 12.0


def test_repair_rolls_back_regressed_candidate(pattern, workspace):
    """回归守卫(studio 会话 6e20f21d:val_history [1,1,1,13],LLM 微循环
    把候选从 1 错修到 13 错且带伤收场):验证回归后,修复站下一访先回滚
    到最优检查点字节再修;后续收敛照常(冻结交付的是回滚后的健康候选)。"""
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
    # 回滚注记进修复履历(下一访 LLM 能看到"从最优状态重修")
    assert any("回滚到最优检查点" in e["summary"]
               for e in trace["repair_log"])
    # 盘上候选是回滚后的健康体:visit1 的 edit(a→b)被撤销(diagram_type
    # 不再是 "dbagram_type"),v4 冻结的也是这份健康候选
    data = json.loads(Path(trace["candidate_path"]).read_text(
        encoding="utf-8"))
    assert data["diagram_type"] == "architecture"
    assert trace["frozen"] is True
    assert "showcase 验收通过" in reply


def test_solver_reverts_and_defers_to_llm_loop(pattern, workspace):
    """求解器全败:每个挪移都被真验证器否决 → 字节回滚(候选原样)、
    失败挪移记履历(后续访问零重试)→ LLM 微循环照常接管,直到 stale-5
    诚实出口。"""
    provider = ArchifyScriptedProvider(candidate=_LABEL_CANDIDATE_JSON,
                                       repair_edits=2)
    calls = []
    cli = make_cli_stub(
        validate_receipts=[_label_clearance_receipt()] * 9,
        calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # 9 次 validate = 6 次闸门 + 3 次求解器试验(仅第一访;此后挪移键
    # 已在 solver_tried 里,后续访问不再消耗)
    assert len([c for c in calls if "validate" in c]) == 9
    # visit1: 求解器 3 试验全败 + LLM 3 轮(edit×2+空转);
    # visit2..5: 各 1 轮空转(repair_edits 已耗尽)
    assert provider.repair_calls == 7
    trace = app_trace_of(session)
    assert trace["val_history"] == [1] * 6
    assert trace["honest_exit"] is True
    assert trace["repair_rounds"] == 5
    # 字节回滚:候选没有留下任何被否决的挪移痕迹
    data = json.loads(Path(trace["candidate_path"]).read_text(
        encoding="utf-8"))
    assert "labelDy" not in data["connections"][2]
    assert "labelAt" not in data["connections"][2]
    # 履历首条 = 求解器失败注记 + LLM 收口摘要(同访合并)
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
    assert "由你决定" in reply            # 信息非许可
    ack = [c for c in calls if "--ack" in c]
    assert ack == ['node scripts/check-update.mjs --ack "evt-2026-09-15"']
    # 探针不改变主线:交付/浏览器证据照常
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

    # 交付失败 → 直达汇报站;浏览器检查绝不对失败交付路径运行
    assert "visual-check" not in "".join(calls)
    assert "交付: 失败(非零退出,绝不称为成功" in reply
    assert "快照校验失败" in reply
    assert "浏览器证据: 未收集(交付失败路径,按契约跳过)" in reply
    assert "感知审查: 未执行(交付失败逃生路径,按契约跳过)" in reply
    assert provider.percept_calls == 0   # 评审站不可达
    trace = app_trace_of(session)
    assert trace["deliver_failed"] is True
    assert trace["percept_receipt"] == {}
    assert session.cxt.current_node_code == "af_report"


# ============================================================================
# 6a. Percept station (感知评审:多模态附图 / 诚实 skipped 的全部形态)
# ============================================================================

def test_percept_failed_verdict_reported_honestly(pattern, workspace):
    """评审判 failed:缺陷逐条进汇报、correction_rounds 0(首版只如实
    上报不回环)——评审失败不拖垮已成功的交付,也不触发修复回路。"""
    provider = ArchifyScriptedProvider(percept_verdict={
        "status": "failed",
        "defects": [{"viewport": "2048x1320", "theme": "dark",
                     "issue": "下部出现整幅明显空带"}],
        "summary": "大视口构图失衡"})
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    assert provider.percept_calls == 1
    assert provider.repair_calls == 0   # failed 不回环修复
    assert "感知审查: failed(图像能力评审 x/m,4 张截图;correction_rounds 0)" \
        in reply
    assert "[2048x1320/dark] 下部出现整幅明显空带" in reply
    assert "(结论) 大视口构图失衡" in reply
    trace = app_trace_of(session)
    assert trace["percept_receipt"]["defects"][0]["viewport"] == "2048x1320"


def test_percept_skipped_without_evidence(pattern, workspace):
    """visual-check 环境缺失(无 Chrome,exit 2 skipped):无截图 → 感知
    审查诚实 skipped,评审模型零调用。"""
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
    """回执列出截图但磁盘缺失(显式回执桩不落盘):按文件存在性核对,
    全缺 → 诚实 skipped,绝不评审不存在的截图。"""
    provider = ArchifyScriptedProvider()
    cli = make_cli_stub(
        validate_receipts=[_pass_receipt()],
        visual_receipt=_visual_pass_receipt(), calls=[])
    session, reply = run_turn(pattern, provider, cli)

    assert provider.percept_calls == 0
    assert "感知审查: skipped(截图文件缺失(回执列出 4 张,磁盘 0 张))" \
        in reply


def test_percept_skipped_when_model_not_vision(pattern, workspace):
    """注册表声明评审模型无图像能力(zai/glm-5.3 非视觉):按契约不向
    纯文本模型发送图像,诚实 skipped(image reader unavailable 词表)。"""
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
    """判定 JSON 解析失败:坏输出+错误回填 → 自纠重试成功;始终不可解析
    → 诚实 skipped(绝不编造通过)。"""
    # 1) 首次坏输出,自纠成功
    provider = ArchifyScriptedProvider(percept_outputs=[
        "我觉得整体不错,没有明显问题。",
        json.dumps({"status": "passed", "defects": [],
                    "summary": "自纠后的判定"}, ensure_ascii=False)])
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=[])
    session, reply = run_turn(pattern, provider, cli)
    assert provider.percept_calls == 2
    assert "感知审查: passed" in reply

    # 2) 永远不可解析 → 自纠耗尽 → skipped
    provider2 = ArchifyScriptedProvider(percept_outputs=[
        "挺好的", "还是挺好"])
    cli2 = make_cli_stub(validate_receipts=[_pass_receipt()], calls=[])
    session2, reply2 = run_turn(pattern, provider2, cli2)
    assert provider2.percept_calls == 2  # 首败 + 自纠(均失败)
    assert "感知审查: skipped(评审输出不可解析为判定 JSON)" in reply2
    trace = app_trace_of(session2)
    assert trace["percept_receipt"]["status"] == "skipped"
    assert trace["phases"][-1] == "percept"


# ============================================================================
# 6b. Repository evidence (仓库证据:--repo-root 条件化拼装)
# ============================================================================

# 声明仓库证据的 architecture 候选(studio 实跑死锁形态:声明 sources →
# validate 要求 --repo-root,不传则 6 轮全卡 root-required 不可修复)
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
    """_repo_root_flag 判定:仅 architecture 且候选声明证据时非空
    (CLI 对非 architecture 拒绝该旗标;无证据时核验器直接跳过)。"""
    monkeypatch.setattr(ax, "_DEFAULT_REPO_ROOT", "/ev/root")
    cand = workspace[0] / "s1" / "c.json"
    cand.parent.mkdir(parents=True, exist_ok=True)
    state = {"diagram_type": "architecture", "candidate_path": str(cand)}

    cand.write_text(_EVIDENCE_CANDIDATE_JSON, encoding="utf-8")
    assert ax._repo_root_flag(state) == ' --repo-root "/ev/root"'

    # 仅组件 sources(无 meta.repository)同样算声明证据
    data = json.loads(_EVIDENCE_CANDIDATE_JSON)
    del data["meta"]["repository"]
    cand.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert ax._repo_root_flag(state) == ' --repo-root "/ev/root"'

    # architecture 无证据 / 非 architecture(即使带证据字段)/ 候选缺失
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
    """声明证据的 architecture 候选:validate/deliver 命令拼 --repo-root
    (root-required 死锁的解锁口,核验器得以用真 git 裁决并给出可修复
    诊断);workflow 候选不带旗标。"""
    monkeypatch.setattr(ax, "_DEFAULT_REPO_ROOT", "/repo/ev")
    provider = ArchifyScriptedProvider(
        candidate=_EVIDENCE_CANDIDATE_JSON, route_type="architecture")
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    run_turn(pattern, provider, cli)

    vcmd = next(c for c in calls if "archify.mjs validate " in c)
    dcmd = next(c for c in calls if "archify.mjs deliver " in c)
    assert '--repo-root "/repo/ev"' in vcmd
    assert '--repo-root "/repo/ev"' in dcmd

    # 对照:workflow 候选(默认)不拼旗标
    provider2 = ArchifyScriptedProvider()
    calls2 = []
    cli2 = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls2)
    run_turn(pattern, provider2, cli2)
    assert not any("--repo-root" in c for c in calls2)


def test_repair_self_validate_carries_repo_root(pattern, workspace,
                                                monkeypatch):
    """修复站的站内自验命令同步带旗标:模型自验看到的回执与验证闸门
    同源(否则闸门 root-required、自验却过,修复站原地打转)。"""
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
    # 证据诊断的修复指引也在 framing 里(修 meta.repository 或删 sources)
    assert any("repository-evidence" in f for f in frames)


# ============================================================================
# 7. Unit contracts
# ============================================================================

def test_trailing_stale_semantics():
    f = ax._trailing_stale
    assert f([]) == 0
    assert f([5]) == 0            # 首轮是基线
    assert f([5, 5]) == 1
    assert f([5, 5, 5]) == 2      # 触发诚实出口
    assert f([5, 3, 4]) == 1      # 4 未刷新 min(5,3)=3
    assert f([5, 3, 4, 2]) == 0   # 新下限重置
    assert f([3, 3, 2]) == 0


def test_clearance_moves_geometry():
    """求解器几何:四向挪移按 |delta| 升序(就近优先),超限丢弃。

    studio 实跑回归数字:48px 标签 rect [640.8, 233, 48.4, 14] 挤在
    x=665 竖直路由段(y 110→239)旁——上移 -143 超限丢弃,正确解
    labelDy +12 恰好排第一(左移 -30.2 会撞组件,由真验证器否决)。"""
    # 竖直线段:左右让出 x + 上下出段 y 覆盖
    moves = ax._clearance_moves((640.8, 233.0, 48.4, 14.0),
                                (665.0, 110.0, 665.0, 239.0), 4)
    assert moves == [{"dy": 12.0}, {"dx": -30.2}, {"dx": 30.2}]

    # 水平线段:上下让出 y + 左右出段 x 覆盖(对称语义)
    moves = ax._clearance_moves((100.0, 200.0, 40.0, 14.0),
                                (90.0, 240.0, 300.0, 240.0), 4)
    assert {"dy": 20.0} in moves and {"dy": -60.0} not in moves

    # 已净空 <1px 的方向过滤为噪声;全部超限 → 空(交布局级杠杆)
    assert ax._clearance_moves((0.0, 0.0, 40.0, 14.0),
                               (1000.0, -500.0, 1000.0, 500.0), 4) == []


def test_label_solver_targets_forms():
    """目标提取的两种形态:label-route-clearance 走结构化 evidence(消息
    文本兜底),组件重叠解析 Suggested fix 的 below/above 两个绝对点;
    无关诊断零目标。"""
    # 形态 1:结构化 evidence + subject.index
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

    # 形态 1 兜底:只有消息文本(旧回执/摘要降级),正则同样解出几何
    state = {"last_receipt": {"diagnostics": [{
        "code": "composition/label-route-clearance",
        "severity": "error",
        "message": ('label "读写邮件" on connections[2] ... segment 1 '
                    '[665, 110] -> [665, 239] (label rect [641, 233, 48,'
                    ' 14]; minimum 4px)')}]}}
    targets = ax._label_solver_targets(state)
    assert len(targets) == 1
    assert targets[0]["moves"] == [{"dy": 12.0}, {"dx": -30.0}, {"dx": 30.0}]

    # 形态 2:组件重叠的两个建议点(below 在前,渲染器建议序)
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

    # 无建议坐标 / 无关诊断 → 零目标(LLM 微循环的领地)
    state = {"last_receipt": {"diagnostics": [
        {"code": "layout/constraint", "severity": "error",
         "message": 'Label "CLI 命令" overlaps component "cli"'},
        {"code": "composition/proper-crossing", "severity": "error",
         "message": "交叉"}]}}
    assert ax._label_solver_targets(state) == []


def test_apply_label_move_semantics():
    """挪移落位:绝对点写 labelAt;增量优先折进已有 labelAt,否则累加
    labelDx/labelDy(与 supportedFixes 的字段语义一致)。"""
    conns = [{"from": "a", "to": "b", "label": "L"},
             {"from": "c", "to": "d", "label": "M", "labelAt": [552.0, 154.0]},
             {"from": "e", "to": "f", "label": "N", "labelDx": 5.0}]
    assert ax._apply_label_move(conns, {"index": 0, "label": "L"},
                                {"abs": [180, 258]})
    assert conns[0]["labelAt"] == [180.0, 258.0]
    assert ax._apply_label_move(conns, {"index": 1, "label": "M"},
                                {"dy": 12.0})
    assert conns[1]["labelAt"] == [552.0, 166.0]  # 折进 labelAt
    assert ax._apply_label_move(conns, {"index": 2, "label": "N"},
                                {"dx": -30.0})
    assert conns[2]["labelDx"] == -25.0           # 累加
    # 越界 index / 标签失配 → 拒绝落位
    assert not ax._apply_label_move(conns, {"index": 9, "label": "X"},
                                    {"dy": 1.0})


def test_receipt_metrics():
    assert ax._receipt_error_count(_pass_receipt()) == 0
    assert ax._receipt_error_count(_fail_receipt(2)) == 2
    # ok:false 无诊断/检查 → 下限 1(绝不无中生有地"通过")
    assert ax._receipt_error_count({"ok": False}) == 1
    assert ax._receipt_error_count({"ok": False, "error": "boom"}) == 1
    # showcase 判定:ok + 恰 9 项全过 + 无警告;4 项回执不算验收
    assert ax._is_showcase_pass(_pass_receipt())
    assert not ax._is_showcase_pass(
        {"ok": True, "checks": [{"ok": True}] * 4, "warnings": []})
    assert not ax._is_showcase_pass(
        {"ok": True, "checks": [{"ok": True}] * 8 + [{"ok": False}],
         "warnings": []})
    assert not ax._is_showcase_pass(
        {"ok": True, "checks": [{"ok": True}] * 9, "warnings": ["w"]})


def test_missing_candidate_recorded_without_bash(pattern, workspace):
    """创作站未写出候选:验证闸门记客观错误(不跑 bash),修复回路接管;
    修复兜不住时按收敛契约诚实退出(绝不伪造候选或宣称成功)。"""
    provider = ArchifyScriptedProvider(candidate_write=False, repair_edits=5)
    calls = []
    cli = make_cli_stub(validate_receipts=[], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # 候选缺失 → validate 六次都不执行 bash,直接记 author/missing-candidate
    assert not any("validate" in c for c in calls)
    trace = app_trace_of(session)
    assert trace["val_history"] == [1] * 6
    assert trace["last_receipt"]["diagnostics"][0][
        "code"] == "author/missing-candidate"
    # 修复站五次访问均未能写出候选(edit_file 对不存在文件报错回填),
    # 第六访被收敛闸门拦截 → 诚实出口
    assert provider.repair_calls == 9
    assert trace["repair_rounds"] == 5
    assert trace["honest_exit"] is True
    assert "候选规范文件不存在" in reply


def test_route_parse_failure_self_corrects(pattern, workspace):
    """路由 JSON 首次解析失败:坏输出+错误回填 → 自纠重试成功(不降级)。"""
    provider = ArchifyScriptedProvider(route_fail_first=True)
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    assert provider.route_calls == 2  # 首败 + 自纠重试
    trace = app_trace_of(session)
    assert trace["degraded"] is False
    assert trace["diagram_type"] == "workflow"


def test_route_parse_failure_degrades(pattern, workspace):
    """路由 JSON 永远解析失败:自纠耗尽 → 降级 workflow(degraded 标记),
    流程不因路由失败死锁。"""
    provider = ArchifyScriptedProvider(route_fail_always=True)
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    assert provider.route_calls == 2  # 首败 + 自纠重试(均失败)
    trace = app_trace_of(session)
    assert trace["degraded"] is True
    assert trace["diagram_type"] == "workflow"


def test_author_write_path_drift_adopted(pattern, workspace):
    """创作站写入路径漂移收编(studio 实跑回归):

    模型把候选写到了自选路径而非 candidate_path——执行器从本轮
    write_text 调用中收编最后一个可解析内容,钉回 candidate_path
    (内容归模型、落位归执行器),流程继续走验证。
    """
    root, skill = workspace
    wrong = str(root / "my-own-choice.json")   # 模型自选的错路径(绝对)
    provider = ArchifyScriptedProvider(write_path=wrong)
    calls = []
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=calls)
    session, reply = run_turn(pattern, provider, cli)

    # 错路径真实落盘;收编后 candidate_path 同样有货且可解析
    assert Path(wrong).exists()
    cand = _candidate_path()
    data = json.loads(Path(cand).read_text(encoding="utf-8"))
    assert data["meta"]["quality_profile"] == "showcase"
    # 命中的是错路径的内容(validate 命令内嵌 candidate_path 绝对路径)
    vcmd = next(c for c in calls if "validate" in c)
    assert cand in vcmd
    assert "showcase 验收通过" in reply
    trace = app_trace_of(session)
    assert "author" in trace["phases"]  # 未标记 author_failed


def test_relative_workspace_root_pinned_absolute(pattern, workspace,
                                                 monkeypatch, tmp_path):
    """相对工作区根必须钉成绝对路径(studio 实跑回归):

    file 工具按服务启动目录解析相对路径,archify CLI 经 bash 以
    workdir=skill_dir 运行按技能目录解析——同一相对串两个上下文解析到
    不同文件,验证站 ENOENT、修复站修到验证看不到的文件。状态板路径
    一律绝对后,write_text 落盘与 validate 命令内嵌的是同一个文件。
    """
    # 相对根(带 .. 指回 tmp,不 chdir——file 工具的配置探测依赖 cwd)
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
    assert cand.exists()  # write_text 真实落盘的就是这个绝对路径
    # validate 命令内嵌同一绝对路径(CLI 在 skill_dir 下运行也能读到)
    vcmd = next(c for c in calls if "validate" in c)
    assert str(cand) in vcmd
    assert "showcase 验收通过" in reply


# ============================================================================
# 8. 思考流式上屏 + 修复站的创作上下文(本轮新增契约)
# ============================================================================

class _ThinkingStreamProvider(ArchifyScriptedProvider):
    """相位脚本不变,改为流式供给:首个 chunk 携带思考增量,正文拆两个
    文本 chunk,tool_calls 原样作收尾 chunk(聚合器按 OpenAI 语义合并)。"""

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
    """三个 LLM 站的思考增量以 thinking 事件流入 UI(此前发射器传 None,
    UI 完全收不到);站内正文是协议 JSON/工作话术,forward_text=False
    不上屏——done 的权威回复仍是回执组装的汇报。"""
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
    assert not [e for e in events if e.kind == "delta"]  # 正文不上屏
    done = events[-1]
    assert done.kind == "done"
    assert "showcase 验收通过" in done.result.text  # 汇报组装不受影响


def test_repair_framing_carries_authoring_context(pattern, workspace):
    """修复站提示词补回创作上下文(原 skill 的修复发生在创作同会话,拆站
    后由状态板代偿):原始需求/创作备忘/类型放置纪律/结构化诊断(subject +
    supportedFixes)/schema 与 authoring-contract 路径/--layout-json;第二
    次访问能看到错误轨迹与第一次的动作摘要(防原样重演已失败动作)。"""
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

    # 每次修复访问的首轮请求才是完整 framing(仅 system+framing 两条);
    # stale-5 下闸门前共五访
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
    assert "(首轮修复" not in first  # 已有轨迹,无占位符

    # 第二访:轨迹叠加 + 第一访摘要可见;stale=1 未达最后机会阈值
    assert "客观错误数轨迹: [1, 1]" in second
    assert "最后机会" not in second
    # 第五访:stale=4 = stale_limit-1 → 明示最后机会
    assert ("客观错误数轨迹: [1, 1, 1, 1, 1]"
            "——已连续 4 轮未刷新下限") in last
    assert "最后机会" in last
    assert "第 1 轮已试: 信息已足够" in second

    trace = app_trace_of(session)
    assert [e["summary"] for e in trace["repair_log"]] == [
        "信息已足够"] * 5
    assert trace["design_notes"] == "候选已写入"


# ============================================================================
# 8. Phase 3 迁移:config bag 驱动预算 + 站点发布 pattern_code
# ============================================================================

def test_repair_budget_follows_app_config_bag(pattern, workspace, tmp_path,
                                              monkeypatch):
    """apps/<name>/config.yaml 的 config.repair_rounds 覆盖代码默认(3):
    同一 stale-5 场景,每次修复访问的内部微循环被截到 1 轮 LLM(对照
    test_repair_honest_exit_after_stale_rounds 的 9 次 = 3+3+1+1+1)。"""
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

        assert provider.author_calls == 4   # 未覆盖的键维持代码默认(10 不截)
        assert len([c for c in calls if "validate" in c]) == 6
        assert provider.repair_calls == 5   # 5 次访问 × 每访 1 轮(覆盖生效)
        trace = app_trace_of(session)
        assert trace["repair_rounds"] == 5
        assert trace["honest_exit"] is True
        assert "连续 5 轮未刷新错误数下限" in reply  # stale_limit 未覆盖 → 默认 5
    finally:
        nexus_settings.invalidate_config_cache()


def test_stations_publish_pattern_code(pattern, workspace):
    """自定义站点绕过默认 loop executor,须自行发布工具调用位置:
    _dispatch_tool_calls(语义站文件工具)与 _run_cli(确定性站 bash)两条
    dispatch 路径里,处理器侧 ambient_pattern_code() 均应读到 "archify"——
    它是 app 护栏覆盖的定位键(漏发 = 永远全局护栏)。"""
    from nexus.engine.tool_context import ambient_pattern_code

    seen = []
    provider = ArchifyScriptedProvider()
    cli = make_cli_stub(validate_receipts=[_pass_receipt()], calls=[])

    async def spy(name, args):
        seen.append(ambient_pattern_code())
        return await cli(name, args)

    session, reply = run_turn(pattern, provider, spy)

    # author 的 find/read/write(语义站)+ probe/validate/deliver/
    # visual-check 的 bash(确定性站)全部命中且唯一为 archify
    assert len(seen) >= 7
    assert set(seen) == {"archify"}
    assert "showcase 验收通过" in reply  # 全链路未受影响
