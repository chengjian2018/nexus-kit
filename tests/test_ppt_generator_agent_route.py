"""ppt_generator_agent (FSM mode) offline tests — the ai-ppt-generator
skill (Baidu Wenku AIPPT) transcribed into a declarative FSM pattern.

Fully offline: the Baidu API client is stubbed via monkeypatch (theme list
fixture + generate fixture / raise-fixture), and the LLM is a scripted
provider defined HERE (self-contained — the shared tests/fake_provider.py
stays untouched).

Covers:
1. Pattern structure and AST auto-discovery registration (6-node graph,
   decline edges from every business node, single is_end terminal)
2. Stage wiring: skeleton {nlu: ppt_unified, nlg: nlg_pass_through}, no
   node-level stages anywhere (FSM timing trap — deterministic rewrites
   ride the unified stage), all declared codes resolve, tools registered
3. validate_pattern passes (app-local stage code resolves)
4. Auto-match happy path: topic → auto (keyword category → real theme
   picked in code) → generation runs with the matched tpl_id → delivery
   reply carries the REAL title + ppt_url → end
5. Hand-pick path: theme-list turn shows the REAL list (deterministic
   rewrite, fetched exactly once) → pick resolves against the cached list
   → generation runs with the picked tpl_id
6. Template cycle + cache inheritance: 换个模板 re-enters the list node
   WITHOUT a second fetch (state-board cache), a different template
   generates
7. Pick resolution is anti-fabrication: an unresolvable pick deterministically
   stays with an honest re-ask; from the list node a pick is REQUIRED
8. Failure inheritance + anti-replay: failed attempts accumulate per
   template in graph_state.failed_tpl_ids; honest failure stay; the same
   template past the cap is refused deterministically (zero extra
   generation calls); switching templates is still allowed
9. Generic decline channel (two-beat close: decline → end,
   conversation_end action on terminal entry)
10. BAIDU_API_KEY missing → honest config-error stay (not counted as a
    template attempt, no fetch attempted)
11. suggest_category unit behavior (deterministic keyword table)
"""

import json
import logging

import pytest

from nexus.llm.provider import BaseLLMProvider
from nexus.registry.providers import registry as llm_registry

logging.basicConfig(level=logging.WARNING)

# ============================================================================
# Scripted provider — self-contained (tests/fake_provider.py untouched)
# ============================================================================

FAKE_PPT_PROVIDER_CODE = "fake_ppt_provider"


def _extract_node_name(prompt: str) -> str:
    """The current node's name from the unified prompt (same marker the
    shared fake provider uses: 「节点名称:」 lines)."""
    for line in prompt.split("\n"):
        line = line.strip()
        if line.startswith("节点名称:"):
            return line.split(":", 1)[1].strip()
    return ""


def _extract_query(prompt: str) -> str:
    """The user query (first line after the「### 用户输入」marker)."""
    marker = "### 用户输入"
    idx = prompt.find(marker)
    if idx == -1:
        return ""
    segment = prompt[idx + len(marker):]
    lines = [l.strip() for l in segment.split("\n") if l.strip()]
    return lines[0] if lines else ""


def _ppt_unified(node_name: str, query: str):
    """Scripted unified-stage outputs for the ppt FSM (branch selection by
    current node + user reply, mirroring the source skill's workflow)."""
    # Generic cancel heard at ANY node → decline channel (two beats)
    if any(k in query for k in ("不做了", "取消", "算了", "以后再说")):
        if node_name == "通用取消承接":
            return json.dumps(
                {"reply": "PPT回复: 结束语", "next_node": "ppt_end",
                 "slots": {"decline_reason": query}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "PPT回复: 通用取消承接", "next_node": "ppt_decline",
             "slots": {"decline_reason": query}},
            ensure_ascii=False)

    if node_name == "开场收集主题":
        return json.dumps(
            {"reply": "PPT回复: 模板选择询问", "next_node": "ppt_ask_template",
             "slots": {"ppt_topic": query}},
            ensure_ascii=False)

    if node_name == "模板选择询问":
        if any(k in query for k in ("自己挑", "我要选", "我自己选", "挑一个")):
            return json.dumps(
                {"reply": "PPT回复: 模板列表展示", "next_node": "ppt_show_themes",
                 "slots": {"want_choose": "是"}},
                ensure_ascii=False)
        if any(k in query for k in ("不用", "你选", "帮我选", "自动", "随便")):
            return json.dumps(
                {"reply": "PPT回复: PPT生成交付", "next_node": "ppt_generate",
                 "slots": {"want_choose": "否"}},
                ensure_ascii=False)
        if "再试" in query:  # retry after a failed auto generation
            return json.dumps(
                {"reply": "PPT回复: PPT生成交付", "next_node": "ppt_generate",
                 "slots": {}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "PPT回复: 模板选择询问", "next_node": "", "slots": {}},
            ensure_ascii=False)

    if node_name == "模板列表展示":
        if any(k in query for k in ("返回", "你帮我选", "还是自动")):
            return json.dumps(
                {"reply": "PPT回复: 模板选择询问",
                 "next_node": "ppt_ask_template", "slots": {}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "PPT回复: PPT生成交付", "next_node": "ppt_generate",
             "slots": _pick_slots(query)},
            ensure_ascii=False)

    if node_name == "PPT生成交付":
        if any(k in query for k in ("换个模板", "重新挑", "换一个")):
            return json.dumps(
                {"reply": "PPT回复: 模板列表展示", "next_node": "ppt_show_themes",
                 "slots": {}},
                ensure_ascii=False)
        if any(k in query for k in ("再生成", "再来一份")):
            return json.dumps(
                {"reply": "PPT回复: PPT生成交付", "next_node": "ppt_generate",
                 "slots": {}},
                ensure_ascii=False)
        return json.dumps(
            {"reply": "PPT回复: 结束语", "next_node": "ppt_end", "slots": {}},
            ensure_ascii=False)

    if node_name == "通用取消承接":
        return json.dumps(
            {"reply": "PPT回复: 结束语", "next_node": "ppt_end", "slots": {}},
            ensure_ascii=False)

    # Closing line ("结束语") / unknown -> stay
    return json.dumps(
        {"reply": "PPT回复: 结束语", "next_node": "", "slots": {}},
        ensure_ascii=False)


def _pick_slots(query: str) -> dict:
    """Slots echoing the user's pick (the NLU's extraction)."""
    if "106" in query or "未来科技" in query:
        return {"tpl_id": "106", "style_name": "未来科技"}
    if "201" in query or "企业商务" in query:
        return {"tpl_id": "201", "style_name": "企业商务"}
    if "梦幻" in query:
        return {"style_name": "梦幻模板"}  # unresolvable probe
    return {}


class PPTFakeProvider(BaseLLMProvider):
    """Offline scripted LLM provider for the ppt route tests."""

    call_count = 0

    async def _achat_completion_impl(self, messages, model, temperature,
                                     max_tokens, stream=False, **kwargs):
        type(self).call_count += 1
        prompt = messages[0]["content"]
        return {"content": _ppt_unified(
            _extract_node_name(prompt), _extract_query(prompt))}


def register_ppt_fake_provider() -> None:
    if not llm_registry.is_registered(FAKE_PPT_PROVIDER_CODE):
        llm_registry.register(
            code=FAKE_PPT_PROVIDER_CODE,
            name="PPTFakeProvider",
            description="offline scripted provider for ppt route tests",
            provider_class=PPTFakeProvider,
            default_model="fake-model",
        )


def ppt_fake_llm_config() -> dict:
    return {
        "code": FAKE_PPT_PROVIDER_CODE,
        "model": "fake-model",
        "temperature": 0.7,
        "max_tokens": 512,
    }


# ============================================================================
# Baidu API stubs (monkeypatched onto apps.ppt_generator_agent.tools)
# ============================================================================

FAKE_THEMES = [
    {"style_name_list": ["未来科技"], "style_id": 11, "tpl_id": 106},
    {"style_name_list": ["企业商务"], "style_id": 22, "tpl_id": 201},
    {"style_name_list": ["默认"], "style_id": 33, "tpl_id": 300},
]

FAKE_URL = "https://example.com/fake.pptx"
FAKE_TITLE = "人工智能发展趋势报告"


class ThemeFetchSpy:
    """Records fetch calls; returns the fixture list."""

    def __init__(self):
        self.calls = 0

    def __call__(self, api_key):
        self.calls += 1
        return [dict(t) for t in FAKE_THEMES]


class GenerateSpy:
    """Records generation calls; returns the fixture result or raises."""

    def __init__(self, result=None, error=None):
        self.calls = []  # (query, style_id, tpl_id)
        self.result = result
        self.error = error

    def __call__(self, api_key, query, style_id=0, tpl_id=None,
                 web_content=None, progress_hook=None):
        self.calls.append({"query": query, "style_id": style_id,
                           "tpl_id": tpl_id})
        if self.error is not None:
            raise self.error
        return {
            "is_end": True,
            "title": FAKE_TITLE,
            "data": {"ppt_url": FAKE_URL},
        }


@pytest.fixture()
def ppt_env(monkeypatch):
    """Offline environment: fake API key + stubbed Baidu client + scripted
    provider. Returns the spies so walks can assert call counts/args."""
    register_ppt_fake_provider()
    monkeypatch.setenv("BAIDU_API_KEY", "fake-key")
    fetch_spy = ThemeFetchSpy()
    gen_spy = GenerateSpy()
    from apps.ppt_generator_agent import tools

    monkeypatch.setattr(tools, "fetch_ppt_themes", fetch_spy)
    monkeypatch.setattr(tools, "generate_ppt_blocking", gen_spy)
    return {"fetch": fetch_spy, "gen": gen_spy, "tools": tools}


# ============================================================================
# Fixtures (install_booking idiom)
# ============================================================================

@pytest.fixture(scope="session", autouse=True)
def _register_provider():
    register_ppt_fake_provider()


@pytest.fixture(scope="module")
def pattern():
    """Discover builtin patterns and return ppt_generator_agent."""
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.ppt_generator_agent.route" in imported, (
        f"ppt_generator_agent 未被自动发现，已发现: {imported}")
    return registry.get("ppt_generator_agent")


@pytest.fixture()
def sessions():
    return {}


def launch(pattern, sessions, session_id="s1"):
    """Simulate main.py's launch flow (no task_info facts needed — the
    business data comes from the Baidu API at runtime)."""
    from nexus.engine.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = ppt_fake_llm_config()
    sessions[session_id] = session
    return session


def chat(sessions, session_id, query):
    """Run one dialogue turn via nexus.engine.chat."""
    from async_utils import arun
    from nexus.engine.chat import chat as chat_fn

    return arun(chat_fn(query=query, session_id=session_id,
                        all_sessions=sessions))


def end_actions(cxt):
    return [a for a in cxt.actions if "conversation_end" in a]


def state_of(session):
    return session.cxt.graph_state.get("ppt_gen_state", {})


def reach_show_themes(sessions, session_id="s1"):
    """Walk topic → want-choose (lands on the theme-list node)."""
    chat(sessions, session_id, "帮我做一份人工智能发展趋势的PPT")
    return chat(sessions, session_id, "我要自己挑一个")


# ============================================================================
# Structure / wiring / validation
# ============================================================================

def test_pattern_auto_discovered_and_structure(pattern):
    """6-node graph mirrors the skill workflow; every business node carries
    a decline edge; single is_end terminal; the generate node owns the
    delivery branches (thanks → end / switch → list / regenerate → self)."""
    assert pattern.code == "ppt_generator_agent"
    assert pattern.entry_node_code == "ppt_start"
    assert pattern.pattern_type == "fsm"

    assert [n.code for n in pattern.nodes] == [
        "ppt_start", "ppt_ask_template", "ppt_show_themes",
        "ppt_generate", "ppt_decline", "ppt_end",
    ]

    sub = {n.code: set(n.sub_nodes) for n in pattern.nodes}
    assert sub["ppt_start"] == {"ppt_ask_template", "ppt_decline"}
    assert sub["ppt_ask_template"] == {"ppt_show_themes", "ppt_generate",
                                       "ppt_decline"}
    assert sub["ppt_show_themes"] == {"ppt_generate", "ppt_ask_template",
                                      "ppt_decline"}
    assert sub["ppt_generate"] == {"ppt_end", "ppt_show_themes",
                                   "ppt_generate", "ppt_decline"}
    assert sub["ppt_decline"] == {"ppt_end"}
    assert sub["ppt_end"] == set()

    assert pattern.node_map["ppt_end"].is_end is True
    assert not any(n.is_end for n in pattern.nodes[:-1])

    # Decline edge from every non-terminal business node
    for node in pattern.nodes[:-2]:
        assert "ppt_decline" in node.sub_nodes, node.code

    for n in pattern.nodes:
        for target in n.sub_nodes:
            assert target in pattern.node_map


def test_stage_wiring(pattern):
    """Skeleton: guarded unified pair only (no time_aug / no clarify track);
    ZERO node-level stages (the deterministic rewrites ride ppt_unified —
    a node-level nlg would resolve one turn late); all codes resolve;
    the ppt_gen tools are registered."""
    skeleton_values = {slot: code for e in pattern.stages
                       for slot, code in e.items()}
    assert skeleton_values == {"nlu": "ppt_unified",
                               "nlg": "nlg_pass_through"}

    for node in pattern.nodes:
        assert node.stages == {}, (
            f"node {node.code} 不应携带节点级 stages（确定性改写已并入 "
            "ppt_unified，节点级 nlg 会因 FSM 轮末转移时序晚一轮生效）"
        )

    from nexus.registry.plugins import registry as plugin_registry
    assert plugin_registry.has("stage", "ppt_unified")
    assert plugin_registry.has("stage", "nlg_pass_through")

    from nexus.registry.tools import registry as tool_registry
    # Tool registry visibility (grants stay CLOSED — pattern declares no
    # allow_toolset)
    assert pattern.allow_toolset in (None, [])
    assert "ppt_gen_list_themes" in tool_registry.get_tool_names_for_toolset(
        "ppt_gen")
    assert "ppt_gen_generate" in tool_registry.get_tool_names_for_toolset(
        "ppt_gen")


def test_validate_pattern(pattern):
    from nexus.model.validation import validate_pattern

    validate_pattern(pattern)  # no raise — app-local codes resolve


# ============================================================================
# Walk A — auto-match happy path (skill workflow: "If no → auto select")
# ============================================================================

def test_auto_match_happy_path(pattern, sessions, ppt_env):
    session = launch(pattern, sessions)

    # Turn 1: topic lands on the ask node, topic slot filled
    chat(sessions, "s1", "帮我做一份人工智能发展趋势的PPT")
    assert session.cxt.current_node_code == "ppt_ask_template"
    assert "人工智能" in session.cxt.filled_slots["ppt_topic"]
    assert PPTFakeProvider.call_count == 1  # exactly one LLM call per turn

    # Turn 2: auto → the guard runs the REAL generation and rewrites the
    # reply with the real title + URL (never the model's hand-off line)
    reply = chat(sessions, "s1", "不用了，你帮我选吧")
    assert session.cxt.current_node_code == "ppt_generate"
    assert FAKE_URL in reply
    assert FAKE_TITLE in reply

    # The keyword categorizer matched 未来科技; the theme was picked in
    # code from the REAL (stubbed) list; generation got tpl_id 106
    state = state_of(session)
    assert state["topic"].startswith("帮我做一份人工智能")
    assert state["selected"] == {"tpl_id": 106, "style_id": 11,
                                 "style_name": "未来科技",
                                 "category": "未来科技"}
    assert state["last_result"]["status"] == "success"
    assert state["last_result"]["ppt_url"] == FAKE_URL
    assert state["failed_tpl_ids"] == {}
    assert ppt_env["fetch"].calls == 1
    assert ppt_env["gen"].calls == [
        {"query": state["topic"], "style_id": 11, "tpl_id": 106}]

    # Turn 3: thanks → terminal (conversation ends)
    chat(sessions, "s1", "谢谢")
    assert session.cxt.current_node_code == "ppt_end"
    assert end_actions(session.cxt)


# ============================================================================
# Walk B — hand-pick path + template cycle with cache inheritance
# ============================================================================

def test_hand_pick_path_and_template_cycle(pattern, sessions, ppt_env):
    session = launch(pattern, sessions)

    # Topic → want-choose: the theme-list reply is the REAL list,
    # deterministically rewritten (fetch #1, cached)
    reply = reach_show_themes(sessions, "s1")
    assert session.cxt.current_node_code == "ppt_show_themes"
    assert "模板风格如下" in reply
    assert "未来科技" in reply and "106" in reply
    assert "企业商务" in reply and "201" in reply
    assert ppt_env["fetch"].calls == 1
    assert len(state_of(session)["themes"]) == 3

    # Pick by name: resolves against the cached REAL list, generates 106
    reply = chat(sessions, "s1", "就要未来科技那个")
    assert session.cxt.current_node_code == "ppt_generate"
    assert FAKE_URL in reply
    assert ppt_env["fetch"].calls == 1  # pick resolution reused the cache
    assert ppt_env["gen"].calls[-1] == {"query": state_of(session)["topic"],
                                        "style_id": 11, "tpl_id": 106}

    # Template cycle: 换个模板 → back to the list WITHOUT a second fetch
    chat(sessions, "s1", "换个模板重新挑一下")
    assert session.cxt.current_node_code == "ppt_show_themes"
    assert ppt_env["fetch"].calls == 1  # cache inheritance across the loop

    # A different template generates (201)
    reply = chat(sessions, "s1", "用企业商务那个201")
    assert session.cxt.current_node_code == "ppt_generate"
    assert FAKE_URL in reply
    assert state_of(session)["selected"]["tpl_id"] == 201
    assert ppt_env["gen"].calls[-1]["tpl_id"] == 201

    chat(sessions, "s1", "好的谢谢")
    assert session.cxt.current_node_code == "ppt_end"
    assert end_actions(session.cxt)


# ============================================================================
# Walk C — anti-fabrication pick resolution
# ============================================================================

def test_unresolvable_pick_stays_and_reasks(pattern, sessions, ppt_env):
    session = launch(pattern, sessions)
    reach_show_themes(sessions, "s1")

    # A pick that matches nothing in the REAL list → deterministic stay
    # with an honest re-ask (the valid options are listed from data)
    reply = chat(sessions, "s1", "给我来个梦幻模板")
    assert session.cxt.current_node_code == "ppt_show_themes"
    assert "没有找到" in reply
    assert "梦幻" in reply
    assert "106" in reply  # the honest options list from real data
    assert ppt_env["gen"].calls == []  # nothing was generated

    # A resolvable pick then proceeds
    reply = chat(sessions, "s1", "就要106")
    assert session.cxt.current_node_code == "ppt_generate"
    assert FAKE_URL in reply


def test_pick_required_from_list_node(pattern, sessions, ppt_env):
    """From the theme-list node, generation without a resolvable pick never
    proceeds (the auto path only exists from the ask node)."""
    session = launch(pattern, sessions)
    reach_show_themes(sessions, "s1")

    reply = chat(sessions, "s1", "行，就按你说的办")
    assert session.cxt.current_node_code == "ppt_show_themes"
    assert "选" in reply
    assert ppt_env["gen"].calls == []


# ============================================================================
# Walk D — failure inheritance + anti-replay (tried-and-failed memory)
# ============================================================================

def test_failure_retry_inheritance_and_cap(pattern, sessions, ppt_env,
                                           monkeypatch):
    session = launch(pattern, sessions)

    # Generation fails → honest stay, the template enters the tried log
    fail_spy = GenerateSpy(error=RuntimeError("服务超时"))
    monkeypatch.setattr(ppt_env["tools"], "generate_ppt_blocking", fail_spy)
    chat(sessions, "s1", "帮我做一份人工智能发展趋势的PPT")
    reply = chat(sessions, "s1", "不用了，你帮我选吧")
    assert session.cxt.current_node_code == "ppt_ask_template"  # stayed
    assert "生成失败" in reply and "服务超时" in reply
    assert state_of(session)["failed_tpl_ids"] == {"106": 1}

    # Retry re-runs the SAME auto match (topic/category inherited), fails
    # again → attempt 2 recorded
    chat(sessions, "s1", "再试一次")
    assert state_of(session)["failed_tpl_ids"] == {"106": 2}
    assert fail_spy.calls[-1]["tpl_id"] == 106

    # Past the cap: deterministic refusal — honest, zero extra generation
    calls_before = len(fail_spy.calls)
    reply = chat(sessions, "s1", "再试试看")
    assert session.cxt.current_node_code == "ppt_ask_template"
    assert "已连续失败 2 次" in reply
    assert "106" in reply  # what was tried is listed from the log
    assert len(fail_spy.calls) == calls_before

    # Switching templates is still allowed: hand-pick 201 → success
    monkeypatch.setattr(ppt_env["tools"], "generate_ppt_blocking",
                        ppt_env["gen"])
    chat(sessions, "s1", "那我自己挑一个")
    assert session.cxt.current_node_code == "ppt_show_themes"
    reply = chat(sessions, "s1", "用企业商务201")
    assert session.cxt.current_node_code == "ppt_generate"
    assert FAKE_URL in reply
    assert state_of(session)["selected"]["tpl_id"] == 201
    # The tried-and-failed log is preserved (experience inheritance)
    assert state_of(session)["failed_tpl_ids"] == {"106": 2}


# ============================================================================
# Walk E — decline channel (two-beat close)
# ============================================================================

def test_generic_decline_channel(pattern, sessions, ppt_env):
    session = launch(pattern, sessions)

    chat(sessions, "s1", "帮我做一份人工智能发展趋势的PPT")
    reply = chat(sessions, "s1", "不做了，取消吧")
    assert session.cxt.current_node_code == "ppt_decline"
    assert "取消" in reply or "不生成" in reply or "先" in reply

    chat(sessions, "s1", "嗯，以后再说")
    assert session.cxt.current_node_code == "ppt_end"
    assert end_actions(session.cxt)
    assert ppt_env["gen"].calls == []


# ============================================================================
# Walk F — missing BAIDU_API_KEY: honest config-error stay
# ============================================================================

def test_missing_api_key_honest_stay(pattern, sessions, ppt_env,
                                     monkeypatch):
    monkeypatch.delenv("BAIDU_API_KEY", raising=False)
    session = launch(pattern, sessions)

    chat(sessions, "s1", "帮我做一份人工智能发展趋势的PPT")
    reply = chat(sessions, "s1", "不用了，你帮我选吧")
    assert session.cxt.current_node_code == "ppt_ask_template"  # stayed
    assert "BAIDU_API_KEY" in reply
    # Config errors are NOT template attempts (retrying cannot help), and
    # no fetch was attempted
    assert state_of(session).get("failed_tpl_ids", {}) == {}
    assert ppt_env["fetch"].calls == 0


# ============================================================================
# Deterministic categorizer (ported keyword table)
# ============================================================================

def test_suggest_category():
    """Deterministic keyword table, ported faithfully from the source skill
    (table order = priority: 企业商务's formal-content keywords win first,
    e.g.「报告」→ business — the hand-pick path is the override)."""
    from apps.ppt_generator_agent.tools import suggest_category

    assert suggest_category("人工智能与机器学习趋势") == "未来科技"
    assert suggest_category("公司季度营销方案") == "企业商务"
    assert suggest_category("季度业绩报告") == "企业商务"  # priority semantics
    assert suggest_category("儿童英语课件") == "卡通手绘"
    assert suggest_category("2024年终回顾与述职") == "年终总结"
    assert suggest_category("水墨山水与宋词赏析") == "中国风"
    assert suggest_category("我的旅行游记") == "文艺清新"
    assert suggest_category("无关联的输入") == "默认"
