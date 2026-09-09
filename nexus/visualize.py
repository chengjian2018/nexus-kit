"""
Pattern visualization -- renders the structural relationships of a Pattern / Module / Node as a diagram.

Three output formats (all sharing the same Mermaid generator, zero third-party dependencies):
- html    : self-contained HTML with mermaid.js loaded via multi-CDN fallback, opens directly in a browser
- md      : Mermaid Markdown, renders directly on GitHub / in IDEs
- mermaid : plain mermaid source text

Diagram-to-structure mapping conventions:
- Module -> subgraph, filled with a distinct color per ROUTE / FSM / AGENT type
- Node   -> node, labeled with node_code, name, and slots; terminal nodes colored separately
- node.sub_nodes   -> solid arrow (FSM node jumps; may cross modules, consistent with node_map's global lookup semantics)
- node.jump_module -> dashed arrow jump_module (cross-module dispatch, pointing at the target module's first node)
- ROUTE menu node without jump_module -> dashed arrow 「重置回根」 back to the routing root node
- AGENT module (no nodes) -> renders a representative 「Agent 对话」 node inside the subgraph
- entry_module_code -> a 「⏵ 开始」 virtual node pointing at the entry module's first node

Usage:
    python -m dialogue.visualize --list
    python -m dialogue.visualize xianyu_agent                   # diagrams/xianyu_agent.html
    python -m dialogue.visualize xianyu_agent --format md      # Mermaid Markdown
    python -m dialogue.visualize xianyu_agent --format mermaid -o graph.mmd
    python -m dialogue.visualize --all
"""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional, Tuple

from nexus.model.module import BaseModule, ModuleType
from nexus.model.pattern import Pattern
from nexus.registry.patterns import discover_builtin_patterns, registry


# ============================================================================
# Basic utilities
# ============================================================================

def _sanitize_id(raw: Any, prefix: str) -> str:
    """Convert an arbitrary code into a valid mermaid ID (alphanumerics and underscores)."""
    ident = re.sub(r"\W", "_", str(raw or "anonymous"))
    return f"{prefix}{ident}"


def _escape_label(text: Any) -> str:
    """Escape special characters in mermaid labels (quotes / backslashes / newlines)."""
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace('"', "#quot;")
        .replace("\n", "<br/>")
    )


def _slot_keys(node: Any) -> List[str]:
    """The node's slot key list (used for graph labels and the details table)."""
    slots = getattr(node, "node_slots", None) or {}
    return list(slots.keys()) if isinstance(slots, dict) else []


def _module_type(module: Any) -> ModuleType:
    """Module type (unknown types fall back to AGENT so rendering always works)."""
    mtype = getattr(module, "type", None)
    return mtype if isinstance(mtype, ModuleType) else ModuleType.AGENT


def _ordered_modules(pattern: Pattern) -> List[Any]:
    """Module list with the entry module first, keeping the diagram's reading order aligned with dialogue entry order."""
    modules = list(getattr(pattern, "modules", None) or [])
    entry = pattern.module_map.get(pattern.entry_module_code) if getattr(pattern, "entry_module_code", None) else None
    if entry is not None and entry in modules:
        modules.remove(entry)
        modules.insert(0, entry)
    return modules


# ============================================================================
# Mermaid generation
# ============================================================================

def pattern_to_mermaid(pattern: Pattern) -> str:
    """Render a Pattern as mermaid flowchart source.

    Args:
        pattern: a registered Pattern object

    Returns:
        str: mermaid flowchart source
    """
    modules = _ordered_modules(pattern)

    # node_code -> mermaid node ID; module_code -> Agent representative node ID
    node_ids: Dict[str, str] = {}
    agent_ids: Dict[str, str] = {}
    for module in modules:
        if module.module_nodes:
            for node in module.module_nodes:
                if node.node_code and node.node_code not in node_ids:
                    node_ids[node.node_code] = _sanitize_id(node.node_code, "n_")
        else:
            agent_ids[module.module_code] = _sanitize_id(f"{module.module_code}__agent", "n_")

    def module_entry_id(module: Any) -> Optional[str]:
        """The module's entry ID: its first node; node-less modules use the Agent representative node."""
        if module.module_nodes:
            return node_ids.get(module.module_nodes[0].node_code)
        return agent_ids.get(module.module_code)

    lines: List[str] = ["flowchart TB", '    START(("⏵ 开始"))']
    seen_edges = set()

    def add_edge(edge: str) -> None:
        if edge not in seen_edges:
            seen_edges.add(edge)
            lines.append(f"    {edge}")

    end_nodes: List[str] = []
    agent_nodes: List[str] = []

    # ------------------------------------------------------------------
    # 1. One subgraph per module, with edges among the module's sub_nodes
    # ------------------------------------------------------------------
    for module in modules:
        mid = _sanitize_id(module.module_code, "m_")
        title = _escape_label(
            f"{module.module_code or '?'} · {module.module_name or ''} ({_module_type(module).value.upper()})"
        )
        lines.append(f'    subgraph {mid} ["{title}"]')

        for node in module.module_nodes:
            nid = node_ids[node.node_code]
            lines.append(f'        {nid}["{_node_label(node)}"]')
            if getattr(node, "is_end", False):
                end_nodes.append(nid)

        if not module.module_nodes:
            aid = agent_ids[module.module_code]
            lines.append(f'        {aid}["{_agent_node_label(module)}"]')
            agent_nodes.append(aid)

        # In-module node jump edges (a sub_nodes target may live in another module; node_map is a global lookup)
        for node in module.module_nodes:
            for sub_code in node.sub_nodes or []:
                if sub_code in node_ids:
                    lines.append(f"        {node_ids[node.node_code]} --> {node_ids[sub_code]}")

        lines.append("    end")

    # ------------------------------------------------------------------
    # 2. Cross-module edges: entry, jump_module dispatch, ROUTE reset-to-root
    # ------------------------------------------------------------------
    entry_module = (
        pattern.module_map.get(pattern.entry_module_code)
        if getattr(pattern, "entry_module_code", None)
        else None
    )
    if entry_module is not None:
        entry_id = module_entry_id(entry_module)
        if entry_id:
            add_edge(f"START --> {entry_id}")

    for module in modules:
        root_id = module_entry_id(module)
        for node in module.module_nodes:
            nid = node_ids[node.node_code]
            jump_code = getattr(node, "jump_module", None)
            target = pattern.module_map.get(jump_code) if jump_code else None
            if target is not None:
                target_id = module_entry_id(target)
                if target_id:
                    add_edge(f"{nid} -.->|jump_module| {target_id}")
            elif (
                _module_type(module) == ModuleType.ROUTE
                and root_id is not None
                and node.node_code != module.module_nodes[0].node_code
            ):
                # ROUTE menu node without jump_module -> reset back to the routing root node
                add_edge(f"{nid} -.->|重置回根| {root_id}")

        # plan-⑥ adjacency edges: projection (defer, end-of-turn switch)
        # vs jump target — distinct edge styles per module-traffic model
        mod_id = module_entry_id(module)
        for link in getattr(module, "sub_modules", None) or []:
            target = pattern.module_map.get(link.get("target"))
            target_id = module_entry_id(target) if target is not None else None
            if target_id is None:
                continue
            if getattr(target, "enable_project", True):
                add_edge(f"{mod_id} ..>|投影·defer| {target_id}")
            else:
                add_edge(f"{mod_id} -.->|跳转目标| {target_id}")

    # ------------------------------------------------------------------
    # 3. Styling: modules colored by type (entry module bolded), terminal / Agent nodes, start node
    # ------------------------------------------------------------------
    module_style = {
        ModuleType.ROUTE: "fill:#eff6ff,stroke:#3b82f6",
        ModuleType.FSM: "fill:#f0fdf4,stroke:#16a34a",
        ModuleType.AGENT: "fill:#fff7ed,stroke:#ea580c",
    }
    lines.append("    classDef nodeEnd fill:#f5f3ff,stroke:#7c3aed,stroke-width:2px")
    lines.append("    classDef nodeAgent fill:#ecfdf5,stroke:#059669,stroke-width:2px")
    for nid in end_nodes:
        lines.append(f"    class {nid} nodeEnd")
    for aid in agent_nodes:
        lines.append(f"    class {aid} nodeAgent")
    lines.append("    style START fill:#fffbeb,stroke:#d97706,stroke-width:2px")
    for module in modules:
        mid = _sanitize_id(module.module_code, "m_")
        style = module_style[_module_type(module)]
        if module is entry_module:
            style += ",stroke-width:3px"
        lines.append(f"    style {mid} {style}")

    return "\n".join(lines) + "\n"


def _node_label(node: Any) -> str:
    """Node label: code + name (terminal marker appended) + slots."""
    parts = [str(node.node_code or "?"), str(node.node_name or "")]
    if getattr(node, "is_end", False):
        parts[1] = f"{parts[1]} · 终态"
    slots = _slot_keys(node)
    if slots:
        parts.append("slots: " + ", ".join(slots))
    return "<br/>".join(_escape_label(p) for p in parts if p)


def _agent_node_label(module: Any) -> str:
    """Label for the AGENT module's representative node."""
    parts = [str(module.module_code or "?"), str(module.module_name or ""), "Agent 对话"]
    return "<br/>".join(_escape_label(p) for p in parts if p)


# ============================================================================
# Module / node details (data preparation shared by HTML and Markdown)
# ============================================================================

def _module_summary(pattern: Pattern) -> Tuple[int, int]:
    """Count modules and nodes (Agent representative nodes are not counted)."""
    modules = list(getattr(pattern, "modules", None) or [])
    node_count = sum(len(m.module_nodes or []) for m in modules)
    return len(modules), node_count


def _escape_cell(text: Any) -> str:
    """Escape Markdown table cell text (pipes and newlines)."""
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


# ============================================================================
# Markdown rendering
# ============================================================================

def render_pattern_markdown(pattern: Pattern) -> str:
    """Render a Pattern as a Markdown document with the mermaid diagram and details tables."""
    module_count, node_count = _module_summary(pattern)
    lines: List[str] = [
        f"# Pattern: {pattern.name} (`{pattern.code}`)",
        "",
    ]
    if pattern.description:
        lines += [f"> {pattern.description}", ""]
    lines += [
        f"- 入口模块: `{pattern.entry_module_code or '-'}`",
        f"- 模块数: {module_count}　节点数: {node_count}",
        "",
        "## 结构图",
        "",
        "```mermaid",
        pattern_to_mermaid(pattern).rstrip("\n"),
        "```",
        "",
        "## 图例",
        "",
        "- **subgraph** = 模块（蓝 ROUTE / 绿 FSM / 橙 AGENT，入口模块边框加粗）",
        "- **实线箭头** = 节点跳转（`sub_nodes`）",
        "- **虚线箭头 jump_module** = 菜单节点跨模块分发（指向目标模块首节点）",
        "- **虚线箭头 重置回根** = ROUTE 菜单节点未声明 jump_module，回到路由根节点",
        "- **⏵ 开始** = 会话入口（入口模块首节点）",
        "- **终态** = `is_end` 节点",
        "",
        "## 模块与节点详情",
        "",
    ]

    for module in _ordered_modules(pattern):
        mtype = _module_type(module).value.upper()
        lines += [
            f"### {module.module_code} · {module.module_name or ''}（{mtype}）",
            "",
        ]
        if module.module_description:
            lines += [module.module_description, ""]

        if module.module_nodes:
            lines += [
                "| 节点 | 名称 | 描述 | 槽位 | 后继 | 跳转模块 | 终态 |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
            for node in module.module_nodes:
                lines.append(
                    "| {code} | {name} | {desc} | {slots} | {sub} | {jump} | {end} |".format(
                        code=_escape_cell(node.node_code),
                        name=_escape_cell(node.node_name),
                        desc=_escape_cell(node.node_description),
                        slots=_escape_cell(", ".join(_slot_keys(node)) or "-"),
                        sub=_escape_cell(", ".join(node.sub_nodes or []) or "-"),
                        jump=_escape_cell(getattr(node, "jump_module", "") or "-"),
                        end="✓" if getattr(node, "is_end", False) else "",
                    )
                )
            lines.append("")

            examples = [(n, getattr(n, "answer_examples", None) or []) for n in module.module_nodes]
            if any(exs for _, exs in examples):
                lines += ["**回答示例**", ""]
                for node, exs in examples:
                    for ex in exs:
                        lines.append(f"- `{node.node_code}`: {ex}")
                lines.append("")
        else:
            use_tools = ", ".join(str(t) for t in (module.use_tools or [])) or "-"
            lines += [
                f"- 类型: AGENT（无节点，走 Agent 对话循环）",
                f"- use_tools: {use_tools}",
                "",
            ]
            if module.base_prompt:
                prompt = module.base_prompt.strip()
                lines += ["<details><summary>base_prompt</summary>", "", "```text", prompt, "```", "", "</details>", ""]

    return "\n".join(lines) + "\n"


# ============================================================================
# HTML rendering
# ============================================================================

_HTML_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pattern 可视化 · $code</title>
<style>
:root { --border:#e5e7eb; --text:#1f2937; --muted:#6b7280; --bg:#f9fafb; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; margin:0; padding:24px; background:var(--bg); color:var(--text); }
.wrap { max-width: 1100px; margin: 0 auto; }
header h1 { margin:0 0 4px; font-size:22px; }
header .sub { color:var(--muted); font-size:13px; margin-bottom:12px; }
.meta span { display:inline-block; background:#fff; border:1px solid var(--border); border-radius:6px; padding:2px 10px; font-size:12px; margin:0 8px 8px 0; }
.legend { display:flex; flex-wrap:wrap; gap:8px; margin:16px 0; font-size:12px; }
.legend .chip { border-radius:999px; padding:3px 10px; border:1px solid var(--border); background:#fff; }
.legend .c-route { background:#eff6ff; border-color:#3b82f6; }
.legend .c-fsm { background:#f0fdf4; border-color:#16a34a; }
.legend .c-agent { background:#fff7ed; border-color:#ea580c; }
.legend .c-end { background:#f5f3ff; border-color:#7c3aed; }
.legend .c-start { background:#fffbeb; border-color:#d97706; }
.diagram { background:#fff; border:1px solid var(--border); border-radius:10px; padding:16px; overflow:auto; }
#loading { color:var(--muted); font-size:13px; padding:8px; }
#fallback { display:none; margin-top:16px; }
#fallback pre { background:#0b1020; color:#e5e7eb; padding:14px; border-radius:8px; overflow:auto; font-size:12px; }
.notice { color:#b45309; font-size:13px; }
.card { background:#fff; border:1px solid var(--border); border-radius:10px; padding:16px 20px; margin-top:16px; }
.card h2 { font-size:16px; margin:0 0 4px; }
.card .desc { color:var(--muted); font-size:13px; margin:4px 0 12px; }
.badge { font-size:11px; border-radius:4px; padding:1px 8px; margin-left:8px; vertical-align:middle; font-weight:normal; }
.badge.route { background:#eff6ff; color:#1d4ed8; border:1px solid #3b82f6; }
.badge.fsm { background:#f0fdf4; color:#15803d; border:1px solid #16a34a; }
.badge.agent { background:#fff7ed; color:#c2410c; border:1px solid #ea580c; }
table { border-collapse: collapse; width:100%; font-size:13px; }
th, td { border:1px solid var(--border); padding:6px 10px; text-align:left; vertical-align:top; }
th { background:#f3f4f6; white-space:nowrap; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:12px; background:#f3f4f6; padding:1px 5px; border-radius:4px; }
details { margin-top:10px; font-size:13px; }
details summary { cursor:pointer; color:var(--muted); }
dl.examples { margin:8px 0; }
dl.examples dt { font-weight:600; margin-top:8px; }
dl.examples dd { margin:2px 0 0; color:var(--muted); }
ul.info { font-size:13px; margin:8px 0; padding-left:20px; }
ul.info pre { background:#f3f4f6; padding:10px; border-radius:6px; overflow:auto; font-size:12px; }
</style>
</head>
<body>
<div class="wrap">
<header>
<h1>$name</h1>
<div class="sub">$description</div>
<div class="meta">
<span><strong>code</strong>: <code>$code</code></span>
<span><strong>入口模块</strong>: <code>$entry</code></span>
<span><strong>模块</strong>: $module_count</span>
<span><strong>节点</strong>: $node_count</span>
</div>
</header>

<div class="legend">
<span class="chip c-route">ROUTE 模块</span>
<span class="chip c-fsm">FSM 模块</span>
<span class="chip c-agent">AGENT 模块</span>
<span class="chip c-start">⏵ 开始（入口）</span>
<span class="chip c-end">终态节点</span>
<span class="chip">实线 → 节点跳转 sub_nodes</span>
<span class="chip">虚线 ⇢ jump_module 跨模块分发 / 重置回根</span>
</div>

<section class="diagram">
<div id="loading">图表加载中（mermaid.js 通过 CDN 加载）…</div>
<pre class="mermaid">
$mermaid
</pre>
</section>

<section id="fallback">
<p class="notice">图表渲染失败（CDN 不可用或渲染出错）。可复制下面的 mermaid 源码到 <a href="https://mermaid.live" target="_blank" rel="noopener">mermaid.live</a> 查看：</p>
<p id="fallback-error" class="notice"></p>
<pre>$mermaid</pre>
</section>

$details
</div>

<script>
(function () {
  var SOURCES = [
    "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js",
    "https://unpkg.com/mermaid@11/dist/mermaid.min.js",
    "https://registry.npmmirror.com/mermaid/latest/files/dist/mermaid.min.js"
  ];
  function hideLoading() {
    var el = document.getElementById("loading");
    if (el) { el.style.display = "none"; }
  }
  function showFallback(msg) {
    document.getElementById("fallback").style.display = "block";
    hideLoading();
    var err = document.getElementById("fallback-error");
    if (msg && err) { err.textContent = "错误信息: " + String(msg); }
  }
  function init() {
    try {
      mermaid.initialize({ startOnLoad: false, securityLevel: "loose", theme: "default" });
      var task;
      if (mermaid.run) {
        task = mermaid.run({ querySelector: ".mermaid" });
      } else {
        task = new Promise(function (res, rej) {
          try { mermaid.init(undefined, ".mermaid"); res(); } catch (e) { rej(e); }
        });
      }
      task.then(hideLoading).catch(function (e) { showFallback(e && e.message ? e.message : e); });
    } catch (e) {
      showFallback(e && e.message ? e.message : e);
    }
  }
  function load(i) {
    if (i >= SOURCES.length) { showFallback("所有 CDN 均加载失败，可能处于离线环境"); return; }
    var s = document.createElement("script");
    s.src = SOURCES[i];
    s.onload = function () { init(); };
    s.onerror = function () { load(i + 1); };
    document.head.appendChild(s);
  }
  load(0);
})();
</script>
</body>
</html>
"""
)


def render_pattern_html(pattern: Pattern) -> str:
    """Render a Pattern as self-contained HTML (open it directly in a browser to view)."""
    module_count, node_count = _module_summary(pattern)
    escaped_mermaid = html.escape(pattern_to_mermaid(pattern))
    return _HTML_TEMPLATE.substitute(
        name=html.escape(str(pattern.name or pattern.code)),
        description=html.escape(str(pattern.description or "")),
        code=html.escape(str(pattern.code or "")),
        entry=html.escape(str(pattern.entry_module_code or "-")),
        module_count=module_count,
        node_count=node_count,
        mermaid=escaped_mermaid,
        details=_modules_html(pattern),
    )


def _modules_html(pattern: Pattern) -> str:
    """Module / node detail cards (complete information supplementing the diagram below it)."""
    esc = html.escape
    parts: List[str] = []

    for module in _ordered_modules(pattern):
        mtype = _module_type(module)
        parts.append('<section class="card">')
        parts.append(
            "<h2><code>{code}</code><span class=\"badge {key}\">{type}</span>{name}</h2>".format(
                code=esc(module.module_code or ""),
                key=mtype.value,
                type=mtype.value.upper(),
                name=esc(module.module_name or ""),
            )
        )
        if module.module_description:
            parts.append(f'<p class="desc">{esc(module.module_description)}</p>')

        if module.module_nodes:
            parts.append(
                "<table><thead><tr><th>节点</th><th>名称</th><th>描述</th>"
                "<th>槽位</th><th>后继</th><th>跳转模块</th><th>终态</th></tr></thead><tbody>"
            )
            for node in module.module_nodes:
                parts.append(
                    "<tr><td><code>{code}</code></td><td>{name}</td><td>{desc}</td>"
                    "<td>{slots}</td><td>{sub}</td><td>{jump}</td><td>{end}</td></tr>".format(
                        code=esc(node.node_code or ""),
                        name=esc(node.node_name or ""),
                        desc=esc(node.node_description or ""),
                        slots=esc(", ".join(_slot_keys(node)) or "-"),
                        sub=esc(", ".join(node.sub_nodes or []) or "-"),
                        jump=esc(str(getattr(node, "jump_module", "") or "-")),
                        end="✓" if getattr(node, "is_end", False) else "",
                    )
                )
            parts.append("</tbody></table>")

            examples = [(n, getattr(n, "answer_examples", None) or []) for n in module.module_nodes]
            if any(exs for _, exs in examples):
                parts.append("<details><summary>回答示例</summary><dl class=\"examples\">")
                for node, exs in examples:
                    for ex in exs:
                        parts.append(
                            "<dt><code>{code}</code> · {name}</dt><dd>{ex}</dd>".format(
                                code=esc(node.node_code or ""),
                                name=esc(node.node_name or ""),
                                ex=esc(ex),
                            )
                        )
                parts.append("</dl></details>")
        else:
            use_tools = esc(", ".join(str(t) for t in (module.use_tools or [])) or "-")
            parts.append('<ul class="info">')
            parts.append("<li>类型: AGENT（无节点，走 Agent 对话循环）</li>")
            parts.append(f"<li>use_tools: {use_tools}</li>")
            parts.append("</ul>")
            if module.base_prompt:
                parts.append(
                    "<details><summary>base_prompt</summary><pre>{prompt}</pre></details>".format(
                        prompt=esc(module.base_prompt.strip())
                    )
                )

        parts.append("</section>")

    return "\n".join(parts)


# ============================================================================
# Rendering entry points and CLI
# ============================================================================

def render_pattern(pattern: Pattern, fmt: str = "html") -> str:
    """Render a Pattern in the given format.

    Args:
        pattern: a registered Pattern object
        fmt: output format, html / md / mermaid

    Returns:
        str: the rendered text
    """
    if fmt == "html":
        return render_pattern_html(pattern)
    if fmt == "md":
        return render_pattern_markdown(pattern)
    if fmt == "mermaid":
        return pattern_to_mermaid(pattern)
    raise ValueError(f"不支持的格式: {fmt}（可选 html / md / mermaid）")


def _default_out_path(pattern_code: str, fmt: str) -> Path:
    ext = {"html": "html", "md": "md", "mermaid": "mmd"}[fmt]
    return Path("diagrams") / f"{pattern_code}.{ext}"


def _write_one(pattern: Pattern, fmt: str, out: Optional[str]) -> None:
    """Render a single pattern, write it to a file, and print the output path."""
    content = render_pattern(pattern, fmt)
    out_path = Path(out) if out else _default_out_path(str(pattern.code), fmt)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"已生成: {out_path.resolve()}  (pattern={pattern.code}, format={fmt})")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry: python -m dialogue.visualize [pattern_code] [--format ...] [-o ...]"""
    parser = argparse.ArgumentParser(
        prog="python -m dialogue.visualize",
        description="将已注册的 Pattern 渲染为可视化图（HTML / Markdown / mermaid 源码）",
    )
    parser.add_argument("pattern_code", nargs="?", help="要可视化的 pattern code")
    parser.add_argument("--list", action="store_true", help="列出所有已注册 pattern code")
    parser.add_argument("--all", action="store_true", help="为所有已注册 pattern 各生成一份")
    parser.add_argument(
        "--format",
        dest="fmt",
        choices=("html", "md", "mermaid"),
        default="html",
        help="输出格式（默认 html）",
    )
    parser.add_argument("-o", "--out", default=None, help="输出文件路径（默认 diagrams/<code>.<ext>）")
    args = parser.parse_args(argv)

    discover_builtin_patterns()
    codes = registry.list_codes()

    if args.list:
        for code in codes:
            print(code)
        return 0

    if args.all:
        if args.out:
            parser.error("-o/--out 仅支持单个 pattern，使用 --all 时请省略")
        if not codes:
            print("没有已注册的 pattern")
            return 1
        for code in codes:
            _write_one(registry.get(code), args.fmt, None)
        return 0

    if not args.pattern_code:
        parser.print_usage()
        print("已注册 pattern: " + (", ".join(codes) or "（无）"))
        return 1

    pattern = registry.get(args.pattern_code)
    if pattern is None:
        print(f"pattern '{args.pattern_code}' 未注册，已注册: " + (", ".join(codes) or "（无）"))
        return 1

    _write_one(pattern, args.fmt, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
