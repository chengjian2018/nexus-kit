"""Tests for the visualize module -- fully offline, structural rendering assertions only, no LLM.

The fixture is an inline vis_demo pattern:
ROUTE (jump_module menu + no-jump reset edges) + 2 FSM modules (node chains + is_end),
covering every structural feature visualize supports.
"""

import pytest

from nexus import visualize
from nexus.model.module import AgentModule, FSMModule, RouteModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import discover_builtin_patterns, registry

# All node codes of vis_demo (see the pattern() fixture below)
ALL_NODE_CODES = [
    "vis_route_root", "vis_menu_sales", "vis_menu_after", "vis_menu_chitchat",
    "vis_ask_brand", "vis_ask_budget", "vis_confirm",
    "vis_after_ask_issue", "vis_after_confirm",
]


@pytest.fixture(scope="module")
def demo():
    discover_builtin_patterns()  # visualize.main's --list/--all depend on the global registry
    return Pattern(
        code="vis_demo",
        name="可视化测试助手",
        description="测试 pattern：路由 + 两个 FSM 子模块",
        entry_module_code="vis_root",
        modules=[
            RouteModule(
                module_code="vis_root",
                module_name="总路由",
                module_description="顶层路由",
                module_todo_description="意图分发",
                module_nodes=[
                    BaseNode(
                        node_code="vis_route_root",
                        node_name="路由根节点",
                        node_description="总入口",
                        node_todo_description="意图分类",
                        sub_nodes=["vis_menu_sales", "vis_menu_after",
                                   "vis_menu_chitchat"],
                    ),
                    BaseNode(
                        node_code="vis_menu_sales",
                        node_name="购车菜单",
                        node_description="购车入口",
                        node_todo_description="跳转到购车子模块",
                        sub_nodes=[],
                        jump_module="vis_buy",
                    ),
                    BaseNode(
                        node_code="vis_menu_after",
                        node_name="售后菜单",
                        node_description="售后入口",
                        node_todo_description="跳转到售后子模块",
                        sub_nodes=[],
                        jump_module="vis_after",
                    ),
                    BaseNode(
                        node_code="vis_menu_chitchat",
                        node_name="闲聊菜单",
                        node_description="闲聊承接",
                        node_todo_description="留在路由模块",
                        sub_nodes=[],
                    ),
                ],
            ),
            FSMModule(
                module_code="vis_buy",
                module_name="购车流程",
                module_description="品牌 → 预算 → 确认",
                module_todo_description="收集购车信息",
                module_nodes=[
                    BaseNode(
                        node_code="vis_ask_brand",
                        node_name="询问品牌",
                        node_description="收集品牌",
                        node_todo_description="抽取 brand 槽位",
                        node_slots={"brand": "品牌"},
                        sub_nodes=["vis_ask_budget"],
                    ),
                    BaseNode(
                        node_code="vis_ask_budget",
                        node_name="询问预算",
                        node_description="收集预算",
                        node_todo_description="抽取 budget 槽位",
                        node_slots={"budget": "预算"},
                        sub_nodes=["vis_confirm"],
                    ),
                    BaseNode(
                        node_code="vis_confirm",
                        node_name="确认购车",
                        node_description="最终确认",
                        node_todo_description="结束流程",
                        sub_nodes=[],
                        is_end=True,
                    ),
                ],
            ),
            FSMModule(
                module_code="vis_after",
                module_name="售后流程",
                module_description="问题 → 确认",
                module_todo_description="收集售后信息",
                module_nodes=[
                    BaseNode(
                        node_code="vis_after_ask_issue",
                        node_name="询问问题",
                        node_description="收集问题类型",
                        node_todo_description="抽取 issue 槽位",
                        node_slots={"issue": "问题类型"},
                        sub_nodes=["vis_after_confirm"],
                    ),
                    BaseNode(
                        node_code="vis_after_confirm",
                        node_name="确认售后",
                        node_description="最终确认",
                        node_todo_description="结束流程",
                        sub_nodes=[],
                        is_end=True,
                    ),
                ],
            ),
        ],
    )


@pytest.fixture(scope="module")
def mermaid(demo):
    return visualize.pattern_to_mermaid(demo)


# ============================================================================
# Mermaid structure assertions
# ============================================================================

class TestMermaid:
    def test_flowchart_header_and_start(self, mermaid):
        assert mermaid.startswith("flowchart TB")
        assert 'START(("⏵ 开始"))' in mermaid

    def test_modules_rendered_as_subgraphs(self, mermaid):
        for code in ("vis_root", "vis_buy", "vis_after"):
            assert f"subgraph m_{code} [" in mermaid
        # Entry module is rendered first
        assert mermaid.index("m_vis_root") < mermaid.index("m_vis_buy")

    def test_module_type_in_title(self, mermaid):
        assert "(ROUTE)" in mermaid
        assert "(FSM)" in mermaid

    def test_all_nodes_rendered(self, mermaid):
        for code in ALL_NODE_CODES:
            assert f"n_{code}[" in mermaid

    def test_entry_edge_from_start(self, mermaid):
        assert "START --> n_vis_route_root" in mermaid

    def test_fsm_edges(self, mermaid):
        expected = [
            "n_vis_route_root --> n_vis_menu_sales",
            "n_vis_route_root --> n_vis_menu_after",
            "n_vis_route_root --> n_vis_menu_chitchat",
            "n_vis_ask_brand --> n_vis_ask_budget",
            "n_vis_ask_budget --> n_vis_confirm",
            "n_vis_after_ask_issue --> n_vis_after_confirm",
        ]
        for edge in expected:
            assert edge in mermaid

    def test_jump_module_edges(self, mermaid):
        assert "n_vis_menu_sales -.->|jump_module| n_vis_ask_brand" in mermaid
        assert "n_vis_menu_after -.->|jump_module| n_vis_after_ask_issue" in mermaid

    def test_route_reset_edge(self, mermaid):
        # vis_menu_chitchat has no jump_module: resets back to the route root node
        assert "n_vis_menu_chitchat -.->|重置回根| n_vis_route_root" in mermaid

    def test_end_node_class(self, mermaid):
        assert "class n_vis_confirm nodeEnd" in mermaid
        assert "class n_vis_after_confirm nodeEnd" in mermaid
        assert "classDef nodeEnd" in mermaid

    def test_slots_in_label(self, mermaid):
        assert "slots: brand" in mermaid
        assert "slots: budget" in mermaid

    def test_module_styles_by_type(self, mermaid):
        assert "style m_vis_root fill:#eff6ff,stroke:#3b82f6,stroke-width:3px" in mermaid
        assert "style m_vis_buy fill:#f0fdf4,stroke:#16a34a" in mermaid

    def test_label_escaping(self, mermaid):
        # Label lines close their quotes in pairs, so mermaid syntax stays intact
        assert mermaid.count('["') == mermaid.count('"]')

    def test_escape_label_helper(self):
        assert visualize._escape_label('含"引号"') == "含#quot;引号#quot;"
        assert visualize._escape_label("a\nb") == "a<br/>b"
        assert visualize._escape_label(None) == ""


# ============================================================================
# HTML / Markdown renderer assertions
# ============================================================================

class TestRenderers:
    def test_html_basic(self, demo):
        out = visualize.render_pattern_html(demo)
        assert out.startswith("<!DOCTYPE html>")
        assert "可视化测试助手" in out
        assert "vis_demo" in out
        assert 'class="mermaid"' in out
        # The mermaid source is embedded after HTML escaping
        assert "flowchart TB" in out
        # Multi-source CDN fallback
        assert "cdn.jsdelivr.net" in out
        assert "registry.npmmirror.com" in out
        # Fallback block for render failures
        assert 'id="fallback"' in out

    def test_html_module_details(self, demo):
        out = visualize.render_pattern_html(demo)
        for code in ALL_NODE_CODES:
            assert code in out
        # Module cards and type badges
        assert '<span class="badge route">ROUTE</span>' in out
        assert '<span class="badge fsm">FSM</span>' in out
        # Node tables include slot and jump info
        assert "brand" in out
        assert "vis_buy" in out

    def test_markdown_basic(self, demo):
        out = visualize.render_pattern_markdown(demo)
        assert out.startswith("# Pattern: 可视化测试助手 (`vis_demo`)")
        assert "```mermaid" in out
        assert "## 模块与节点详情" in out
        for code in ALL_NODE_CODES:
            assert code in out

    def test_render_pattern_dispatch(self, demo):
        assert visualize.render_pattern(demo, "mermaid").startswith("flowchart TB")
        assert visualize.render_pattern(demo, "md").startswith("# Pattern:")
        assert visualize.render_pattern(demo, "html").startswith("<!DOCTYPE html>")
        with pytest.raises(ValueError):
            visualize.render_pattern(demo, "nope")


# ============================================================================
# AGENT module (no nodes) rendering
# ============================================================================

class TestAgentModule:
    def test_agent_module_renders_representative_node(self):
        pattern = Pattern(
            code="t_agent",
            name="agent 测试",
            description="agent only",
            entry_module_code="t_chat",
            modules=[
                AgentModule(module_code="t_chat", module_name="闲聊模块", base_prompt="你是客服"),
            ],
        )
        m = visualize.pattern_to_mermaid(pattern)
        # A node-less module renders an Agent representative node; the entry edge points to it
        assert "n_t_chat__agent[" in m
        assert "Agent 对话" in m
        assert "START --> n_t_chat__agent" in m
        assert "class n_t_chat__agent nodeAgent" in m
        assert "(AGENT)" in m


# ============================================================================
# CLI assertions (vis_demo is an inline fixture; registry assertions attach to the preserved builtin patterns)
# ============================================================================

class TestCli:
    def test_list(self, capsys):
        assert visualize.main(["--list"]) == 0
        assert "xianyu_agent" in capsys.readouterr().out

    def test_write_html_to_custom_path(self, demo, tmp_path):
        registry.register(demo)
        out_file = tmp_path / "diagram.html"
        assert visualize.main(["vis_demo", "-o", str(out_file)]) == 0
        content = out_file.read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>")
        assert "可视化测试助手" in content

    def test_write_mermaid_format(self, demo, tmp_path):
        registry.register(demo)
        out_file = tmp_path / "diagram.mmd"
        assert visualize.main(["vis_demo", "--format", "mermaid", "-o", str(out_file)]) == 0
        assert out_file.read_text(encoding="utf-8").startswith("flowchart TB")

    def test_default_output_path(self, demo, tmp_path, monkeypatch):
        registry.register(demo)
        monkeypatch.chdir(tmp_path)
        assert visualize.main(["vis_demo"]) == 0
        assert (tmp_path / "diagrams" / "vis_demo.html").exists()

    def test_unknown_pattern_returns_error(self, capsys):
        assert visualize.main(["no_such_pattern"]) == 1
        assert "未注册" in capsys.readouterr().out

    def test_all_generates_every_pattern(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert visualize.main(["--all", "--format", "md"]) == 0
        assert (tmp_path / "diagrams" / "xianyu_agent.md").exists()
