"""
Pattern visualization -- renders the structural relationships of a Pattern / Node as a diagram.

Two-layer model (Pattern → Node; no module layer). ``pattern_type`` selects
the reading of the same fields:

- FSM pattern   -> a state machine: ``sub_nodes`` are the next_node legal
  transition set (solid edges), ``is_end`` nodes are terminal states, slots /
  answer_examples render in the details.
- AGENT pattern -> the graph runtime's static graph: ``sub_nodes`` are the
  adjacency edges (conditional edges are the node executors' routing output
  ``next``, mapped back onto sub_nodes), the entry node is where each turn
  starts, use_tools / plugins render in the details.

Three output formats (all sharing the same Mermaid generator, zero third-party dependencies):
- html    : self-contained HTML with mermaid.js loaded via multi-CDN fallback, opens directly in a browser
- md      : Mermaid Markdown, renders directly on GitHub / in IDEs
- mermaid : plain mermaid source text

Diagram-to-structure mapping conventions:
- Node   -> node, labeled with code, name, and slots (FSM); terminal nodes colored separately
- node.sub_nodes -> solid arrow (FSM transitions / AGENT adjacency)
- entry_node_code -> a 「⏵ 开始」 ("Start") virtual node pointing at the entry node

Usage:
    python -m nexus.visualize --list
    python -m nexus.visualize xianyu_agent                   # diagrams/xianyu_agent.html
    python -m nexus.visualize xianyu_agent --format md      # Mermaid Markdown
    python -m nexus.visualize xianyu_agent --format mermaid -o graph.mmd
    python -m nexus.visualize --all
"""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional, Tuple

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
    """Escape special characters in mermaid labels: backslash / quotes /
    newlines for mermaid syntax, plus HTML-significant characters (& < >) —
    labels are node codes/names that may carry arbitrary text, and every
    renderer in play (studio's in-app mermaid render, the standalone HTML
    export which initializes mermaid with securityLevel=loose) inserts them
    into live DOM; the <br/> newline join must happen after escaping."""
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "#quot;")
        .replace("\n", "<br/>")
    )


def _slot_keys(node: Any) -> List[str]:
    """The node's slot key list (used for graph labels and the details table)."""
    slots = getattr(node, "slots", None) or {}
    return list(slots.keys()) if isinstance(slots, dict) else []


def _ordered_nodes(pattern: Pattern) -> List[Any]:
    """Node list with the entry node first, keeping the diagram's reading order aligned with dialogue entry order."""
    nodes = list(getattr(pattern, "nodes", None) or [])
    entry = pattern.node_map.get(pattern.entry_node_code) if getattr(pattern, "entry_node_code", None) else None
    if entry is not None and entry in nodes:
        nodes.remove(entry)
        nodes.insert(0, entry)
    return nodes


# ============================================================================
# Mermaid generation
# ============================================================================

def pattern_to_mermaid(pattern: Pattern) -> str:
    """Render a Pattern as mermaid flowchart source (two-layer: one node graph).

    Args:
        pattern: a registered Pattern object

    Returns:
        str: mermaid flowchart source
    """
    nodes = _ordered_nodes(pattern)

    # node_code -> mermaid node ID
    node_ids: Dict[str, str] = {}
    for node in nodes:
        if node.code and node.code not in node_ids:
            node_ids[node.code] = _sanitize_id(node.code, "n_")

    lines: List[str] = ["flowchart TB", '    START(("⏵ 开始"))']
    seen_edges = set()

    def add_edge(edge: str) -> None:
        if edge not in seen_edges:
            seen_edges.add(edge)
            lines.append(f"    {edge}")

    end_nodes: List[str] = []

    # ------------------------------------------------------------------
    # 1. Node declarations + sub_nodes edges (FSM transitions / AGENT
    #    adjacency — the same solid arrow, semantics selected by pattern_type)
    # ------------------------------------------------------------------
    for node in nodes:
        nid = node_ids[node.code]
        lines.append(f'    {nid}["{_node_label(node)}"]')
        if getattr(node, "is_end", False):
            end_nodes.append(nid)

    for node in nodes:
        if not node.sub_nodes:
            continue
        nid = node_ids[node.code]
        for sub_code in node.sub_nodes:
            if sub_code in node_ids:
                # FSM = next_node legal set; AGENT = static adjacency (the
                # conditional edge routes are a runtime subset of these)
                add_edge(f"{nid} --> {node_ids[sub_code]}")

    # ------------------------------------------------------------------
    # 2. Entry edge: START -> entry_node_code
    # ------------------------------------------------------------------
    entry_id = node_ids.get(pattern.entry_node_code)
    if entry_id:
        add_edge(f"START --> {entry_id}")

    # ------------------------------------------------------------------
    # 3. Styling: entry node bolded, terminal nodes, start node
    # ------------------------------------------------------------------
    lines.append("    classDef nodeEnd fill:#f5f3ff,stroke:#7c3aed,stroke-width:2px")
    lines.append("    classDef nodeEntry fill:#fffbeb,stroke:#d97706,stroke-width:2px")
    for nid in end_nodes:
        lines.append(f"    class {nid} nodeEnd")
    if entry_id:
        lines.append(f"    class {entry_id} nodeEntry")
    lines.append("    style START fill:#fffbeb,stroke:#d97706,stroke-width:2px")

    return "\n".join(lines) + "\n"


def _node_label(node: Any) -> str:
    """Node label: code + name (terminal marker appended) + slots."""
    parts = [str(node.code or "?"), str(node.name or "")]
    if getattr(node, "is_end", False):
        parts[1] = f"{parts[1]} · 终态"
    slots = _slot_keys(node)
    if slots:
        parts.append("slots: " + ", ".join(slots))
    return "<br/>".join(_escape_label(p) for p in parts if p)


# ============================================================================
# Pattern / node details (data preparation shared by HTML and Markdown)
# ============================================================================

def _pattern_summary(pattern: Pattern) -> Tuple[int, str]:
    """Count nodes and read the pattern type label."""
    return len(getattr(pattern, "nodes", None) or []), str(
        getattr(pattern, "pattern_type", "") or "agent")


def _escape_cell(text: Any) -> str:
    """Escape Markdown table cell text (pipes and newlines)."""
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def _fmt_plugins(plugins: Dict[str, Any]) -> str:
    """Format a plugins dict as ``slot=code`` pairs (empty slots skipped)."""
    return ", ".join(f"{k}={v}" for k, v in sorted((plugins or {}).items()) if v)


# ============================================================================
# Markdown rendering
# ============================================================================

def render_pattern_markdown(pattern: Pattern) -> str:
    """Render a Pattern as a Markdown document with the mermaid diagram and details tables."""
    node_count, ptype = _pattern_summary(pattern)
    type_label = f"{ptype.upper()}（{'状态机：sub_nodes=转移合法集' if ptype == 'fsm' else '图运行时：sub_nodes=邻接边'}）"
    lines: List[str] = [
        f"# Pattern: {pattern.name} (`{pattern.code}`)",
        "",
    ]
    if pattern.description:
        lines += [f"> {pattern.description}", ""]
    lines += [
        f"- 类型: {type_label}",
        f"- 入口节点: `{pattern.entry_node_code or '-'}`",
        f"- 节点数: {node_count}",
        f"- plugins: `{_fmt_plugins(pattern.plugins) or '-'}`",
        f"- allow_toolset: {', '.join(pattern.allow_toolset) if pattern.allow_toolset else '（无工具集）'}",
        "",
        "## 结构图",
        "",
        "```mermaid",
        pattern_to_mermaid(pattern).rstrip("\n"),
        "```",
        "",
        "## 图例",
        "",
        "- **实线箭头** = 节点边（`sub_nodes`：FSM 转移合法集 / AGENT 图邻接）",
        "- **⏵ 开始** = 会话入口（`entry_node_code` 指向的入口节点，加粗高亮）",
        "- **终态** = `is_end` 节点",
        "",
    ]

    if pattern.stages:
        skeleton = " → ".join(
            "/".join(slot for slot in entry_dict.keys())
            for entry_dict in pattern.stages)
        lines += [
            "## stages 骨架（FSM）",
            "",
            f"`{skeleton}`",
            "",
        ]

    lines += ["## 节点详情", ""]
    lines += [
        "| 节点 | 名称 | 描述 | 槽位 | 后继 | 工具 | 终态 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for node in _ordered_nodes(pattern):
        lines.append(
            "| {code} | {name} | {desc} | {slots} | {sub} | {tools} | {end} |".format(
                code=_escape_cell(node.code),
                name=_escape_cell(node.name),
                desc=_escape_cell(node.description),
                slots=_escape_cell(", ".join(_slot_keys(node)) or "-"),
                sub=_escape_cell(", ".join(node.sub_nodes or []) or "-"),
                tools=_escape_cell(", ".join(node.use_tools or []) or "-"),
                end="✓" if getattr(node, "is_end", False) else "",
            )
        )
    lines.append("")

    examples = [(n, getattr(n, "answer_examples", None) or [])
                for n in _ordered_nodes(pattern)]
    if any(exs for _, exs in examples):
        lines += ["**回答示例**", ""]
        for node, exs in examples:
            for ex in exs:
                lines.append(f"- `{node.code}`: {ex}")
        lines.append("")

    node_plugins = [(n, _fmt_plugins(n.plugins))
                    for n in _ordered_nodes(pattern)]
    if any(p for _, p in node_plugins):
        lines += ["**节点级 plugins 覆写**", ""]
        for node, plugins in node_plugins:
            if plugins:
                lines.append(f"- `{node.code}`: {plugins}")
        lines.append("")

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
</style>
</head>
<body>
<div class="wrap">
<header>
<h1>$name</h1>
<div class="sub">$description</div>
<div class="meta">
<span><strong>code</strong>: <code>$code</code></span>
<span><strong>类型</strong>: $ptype_label</span>
<span><strong>入口节点</strong>: <code>$entry</code></span>
<span><strong>节点</strong>: $node_count</span>
</div>
</header>

<div class="legend">
<span class="chip $ptype_key">⏵ 开始（入口节点）</span>
<span class="chip c-end">终态节点（is_end）</span>
<span class="chip">实线 → 节点边 sub_nodes$edge_note</span>
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
    node_count, ptype = _pattern_summary(pattern)
    escaped_mermaid = html.escape(pattern_to_mermaid(pattern))
    return _HTML_TEMPLATE.substitute(
        name=html.escape(str(pattern.name or pattern.code)),
        description=html.escape(str(pattern.description or "")),
        code=html.escape(str(pattern.code or "")),
        ptype_key=("fsm" if ptype == "fsm" else "agent"),
        ptype_label=html.escape(
            ptype.upper() + (" 状态机" if ptype == "fsm" else " 图运行时")),
        edge_note=html.escape(
            "（转移合法集）" if ptype == "fsm" else "（图邻接边）"),
        entry=html.escape(str(pattern.entry_node_code or "-")),
        node_count=node_count,
        mermaid=escaped_mermaid,
        details=_nodes_html(pattern),
    )


def _nodes_html(pattern: Pattern) -> str:
    """Node detail cards (complete information supplementing the diagram below it)."""
    esc = html.escape
    _, ptype = _pattern_summary(pattern)
    parts: List[str] = []

    parts.append('<section class="card">')
    parts.append(
        "<h2>pattern 声明<span class=\"badge {key}\">{type}</span></h2>".format(
            key=("fsm" if ptype == "fsm" else "agent"),
            type=esc(ptype.upper()),
        )
    )
    info: List[str] = [
        f"入口节点: <code>{esc(pattern.entry_node_code or '-')}</code>",
        f"plugins: <code>{esc(_fmt_plugins(pattern.plugins) or '-')}</code>",
        "allow_toolset: " + (
            ", ".join(f"<code>{esc(t)}</code>" for t in pattern.allow_toolset)
            if pattern.allow_toolset else "（无工具集）"),
    ]
    if pattern.stages:
        skeleton = " → ".join(
            "/".join(slot for slot in entry_dict.keys())
            for entry_dict in pattern.stages)
        info.append(f"stages 骨架: <code>{esc(skeleton)}</code>")
    if ptype == "agent":
        info.append(f"max_steps: <code>{pattern.max_steps}</code>")
    parts.append('<ul class="info"><li>' + "</li><li>".join(info) + "</li></ul>")
    parts.append("</section>")

    parts.append('<section class="card">')
    parts.append("<h2>节点详情</h2>")
    parts.append(
        "<table><thead><tr><th>节点</th><th>名称</th><th>描述</th>"
        "<th>槽位</th><th>后继</th><th>工具</th><th>终态</th></tr></thead><tbody>"
    )
    for node in _ordered_nodes(pattern):
        parts.append(
            "<tr><td><code>{code}</code></td><td>{name}</td><td>{desc}</td>"
            "<td>{slots}</td><td>{sub}</td><td>{tools}</td><td>{end}</td></tr>".format(
                code=esc(node.code or ""),
                name=esc(node.name or ""),
                desc=esc(node.description or ""),
                slots=esc(", ".join(_slot_keys(node)) or "-"),
                sub=esc(", ".join(node.sub_nodes or []) or "-"),
                tools=esc(", ".join(node.use_tools or []) or "-"),
                end="✓" if getattr(node, "is_end", False) else "",
            )
        )
    parts.append("</tbody></table>")

    examples = [(n, getattr(n, "answer_examples", None) or [])
                for n in _ordered_nodes(pattern)]
    if any(exs for _, exs in examples):
        parts.append("<details><summary>回答示例</summary><dl class=\"examples\">")
        for node, exs in examples:
            for ex in exs:
                parts.append(
                    "<dt><code>{code}</code> · {name}</dt><dd>{ex}</dd>".format(
                        code=esc(node.code or ""),
                        name=esc(node.name or ""),
                        ex=esc(ex),
                    )
                )
        parts.append("</dl></details>")

    node_plugins = [(n, _fmt_plugins(n.plugins))
                    for n in _ordered_nodes(pattern)]
    if any(p for _, p in node_plugins):
        parts.append("<details><summary>节点级 plugins 覆写</summary><dl class=\"examples\">")
        for node, plugins in node_plugins:
            if plugins:
                parts.append(
                    "<dt><code>{code}</code> · {name}</dt><dd>{plugins}</dd>".format(
                        code=esc(node.code or ""),
                        name=esc(node.name or ""),
                        plugins=esc(plugins),
                    )
                )
        parts.append("</dl></details>")

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
    """CLI entry: python -m nexus.visualize [pattern_code] [--format ...] [-o ...]"""
    parser = argparse.ArgumentParser(
        prog="python -m nexus.visualize",
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
