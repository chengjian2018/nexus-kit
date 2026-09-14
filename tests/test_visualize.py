"""Tests for the visualize module (two-layer form) — fully offline,
structural rendering assertions only, no LLM.

Fixtures: an FSM pattern (node chain + slots + is_end) and an AGENT graph
pattern (adjacency + use_tools + allow_toolset + plugins), covering every
structural feature visualize supports.
"""

import pytest

from nexus import visualize
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import discover_builtin_patterns, registry


@pytest.fixture(scope="module")
def fsm_demo():
    discover_builtin_patterns()  # visualize.main's --list/--all depend on the global registry
    return Pattern(
        code="vis_demo",
        name="可视化测试助手",
        description="测试 pattern：FSM 节点链",
        pattern_type="fsm",
        entry_node_code="vis_ask_brand",
        nodes=[
            BaseNode(code="vis_ask_brand", name="询问品牌",
                     description="收集品牌", task_description="抽取 brand 槽位",
                     slots={"brand": "品牌"}, sub_nodes=["vis_ask_budget"]),
            BaseNode(code="vis_ask_budget", name="询问预算",
                     description="收集预算", task_description="抽取 budget 槽位",
                     slots={"budget": "预算"}, sub_nodes=["vis_confirm"]),
            BaseNode(code="vis_confirm", name="确认购车",
                     description="最终确认", task_description="结束流程",
                     sub_nodes=[], is_end=True),
        ],
        stages=[{"nlu": None}, {"nlg": None}],
    )


@pytest.fixture(scope="module")
def agent_demo():
    return Pattern(
        code="vis_agent_demo",
        name="图应用",
        description="AGENT 图测试",
        pattern_type="agent",
        entry_node_code="vis_root",
        nodes=[
            BaseNode(code="vis_root", name="路由根", description="分发",
                     sub_nodes=["vis_worker", "vis_refuse"],
                     use_tools=["kb_search"]),
            BaseNode(code="vis_worker", name="工作节点",
                     sub_nodes=["vis_refuse"]),
            BaseNode(code="vis_refuse", name="拒绝话术",
                     answer_examples=["抱歉啦"], is_end=True),
        ],
        allow_toolset=["knowledge"],
        plugins={"loop": "vis_router_exec"},
        max_steps=6,
    )


@pytest.fixture(scope="module")
def fsm_mermaid(fsm_demo):
    return visualize.pattern_to_mermaid(fsm_demo)


# ============================================================================
# Mermaid structure assertions
# ============================================================================

class TestMermaid:
    def test_flowchart_header_and_start(self, fsm_mermaid):
        assert fsm_mermaid.startswith("flowchart TB")
        assert 'START(("⏵ 开始"))' in fsm_mermaid

    def test_all_nodes_rendered(self, fsm_mermaid):
        for code in ("vis_ask_brand", "vis_ask_budget", "vis_confirm"):
            assert f"n_{code}[" in fsm_mermaid

    def test_no_subgraph_left(self, fsm_mermaid):
        # 三层时代的 module subgraph 全部消失
        assert "subgraph" not in fsm_mermaid

    def test_entry_edge_from_start(self, fsm_mermaid):
        assert "START --> n_vis_ask_brand" in fsm_mermaid

    def test_sub_nodes_edges(self, fsm_mermaid):
        assert "n_vis_ask_brand --> n_vis_ask_budget" in fsm_mermaid
        assert "n_vis_ask_budget --> n_vis_confirm" in fsm_mermaid

    def test_end_node_class_and_entry_highlight(self, fsm_mermaid):
        assert "class n_vis_confirm nodeEnd" in fsm_mermaid
        assert "classDef nodeEnd" in fsm_mermaid
        assert "class n_vis_ask_brand nodeEntry" in fsm_mermaid

    def test_slots_in_label(self, fsm_mermaid):
        assert "slots: brand" in fsm_mermaid
        assert "slots: budget" in fsm_mermaid

    def test_terminal_marker_in_label(self, fsm_mermaid):
        assert "终态" in fsm_mermaid

    def test_label_escaping(self, fsm_mermaid):
        # Label lines close their quotes in pairs, so mermaid syntax stays intact
        assert fsm_mermaid.count('["') == fsm_mermaid.count('"]')

    def test_escape_label_helper(self):
        assert visualize._escape_label('含"引号"') == "含#quot;引号#quot;"
        assert visualize._escape_label("a\nb") == "a<br/>b"
        assert visualize._escape_label(None) == ""

    def test_agent_graph_edges(self, agent_demo):
        m = visualize.pattern_to_mermaid(agent_demo)
        assert "n_vis_root --> n_vis_worker" in m
        assert "n_vis_root --> n_vis_refuse" in m
        assert "START --> n_vis_root" in m
        assert "class n_vis_refuse nodeEnd" in m


# ============================================================================
# HTML / Markdown renderer assertions
# ============================================================================

class TestRenderers:
    def test_html_basic(self, fsm_demo):
        out = visualize.render_pattern_html(fsm_demo)
        assert out.startswith("<!DOCTYPE html>")
        assert "可视化测试助手" in out
        assert "vis_demo" in out
        assert 'class="mermaid"' in out
        assert "flowchart TB" in out
        # Multi-source CDN fallback
        assert "cdn.jsdelivr.net" in out
        assert "registry.npmmirror.com" in out
        # Fallback block for render failures
        assert 'id="fallback"' in out

    def test_html_node_details(self, fsm_demo):
        out = visualize.render_pattern_html(fsm_demo)
        for code in ("vis_ask_brand", "vis_ask_budget", "vis_confirm"):
            assert code in out
        assert "brand" in out  # 槽位进详情

    def test_markdown_basic(self, fsm_demo):
        out = visualize.render_pattern_markdown(fsm_demo)
        assert out.startswith("# Pattern: 可视化测试助手 (`vis_demo`)")
        assert "```mermaid" in out
        assert "## 节点详情" in out
        for code in ("vis_ask_brand", "vis_ask_budget", "vis_confirm"):
            assert code in out

    def test_markdown_summary_fields(self, fsm_demo, agent_demo):
        fsm_md = visualize.render_pattern_markdown(fsm_demo)
        assert "FSM" in fsm_md
        assert "`vis_ask_brand`" in fsm_md  # 入口节点
        agent_md = visualize.render_pattern_markdown(agent_demo)
        assert "AGENT" in agent_md
        assert "knowledge" in agent_md        # allow_toolset
        assert "vis_router_exec" in agent_md  # plugins
        assert "6" in agent_md                # max_steps
        assert "kb_search" in agent_md        # 节点 use_tools
        assert "抱歉啦" in agent_md            # answer_examples

    def test_render_pattern_dispatch(self, fsm_demo):
        assert visualize.render_pattern(fsm_demo, "mermaid").startswith("flowchart TB")
        assert visualize.render_pattern(fsm_demo, "md").startswith("# Pattern:")
        assert visualize.render_pattern(fsm_demo, "html").startswith("<!DOCTYPE html>")
        with pytest.raises(ValueError):
            visualize.render_pattern(fsm_demo, "nope")


# ============================================================================
# CLI assertions (vis_demo is an inline fixture; registry assertions attach to the preserved builtin patterns)
# ============================================================================

class TestCli:
    def test_list(self, capsys):
        assert visualize.main(["--list"]) == 0
        assert "xianyu_agent" in capsys.readouterr().out

    def test_write_html_to_custom_path(self, fsm_demo, tmp_path):
        registry.register(fsm_demo)
        out_file = tmp_path / "diagram.html"
        assert visualize.main(["vis_demo", "-o", str(out_file)]) == 0
        content = out_file.read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>")
        assert "可视化测试助手" in content

    def test_write_mermaid_format(self, fsm_demo, tmp_path):
        registry.register(fsm_demo)
        out_file = tmp_path / "diagram.mmd"
        assert visualize.main(["vis_demo", "--format", "mermaid", "-o", str(out_file)]) == 0
        assert out_file.read_text(encoding="utf-8").startswith("flowchart TB")

    def test_default_output_path(self, fsm_demo, tmp_path, monkeypatch):
        registry.register(fsm_demo)
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
