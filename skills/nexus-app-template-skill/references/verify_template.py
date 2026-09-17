#!/usr/bin/env python3
"""Structural gate for the template knowledge base -- the machine check for
app-templates/<code>/TEMPLATE.md.

Structural gate (SKILL.md Phase 4): validates a template's structural
soundness with zero implementation --

1. Extract the ```yaml fenced block carrying the ``# nexus-pattern: <code>``
   marker comment from TEMPLATE.md and load it as a Pattern declaration
   (nodes may appear in dict form);
2. Register a **placeholder factory** for every plugin code referenced by
   the declaration (pattern.plugins / node.plugins / the stages skeleton,
   including builtin parts) -- validate_pattern only requires the code to be
   present in the registry, so placeholder registration passes without any
   real implementation;
3. Construct the Pattern -> validate_pattern (strict_tools=False: templates
   may reference not-yet-registered toolsets) + structural assertions;
4. Card coverage: every declared plugin code has a ``#### 插件卡：<code>
   （<kind>）`` card whose kind matches, and every card maps back to a
   declaration (no orphan cards);
5. Interaction-table row count == node count;
6. INDEX.md lists the template code.

Usage:
    python nexus-app-template-skill/references/verify_template.py \
        app-templates/<code>/TEMPLATE.md

Exit codes: 0 = all checks pass; 1 = one or more failures (printed one by
one).
"""

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # repo root sits two levels above references/
sys.path.insert(0, str(REPO_ROOT))

# First-line marker of a YAML fenced block (the extraction anchor defined by SKILL.md)
PATTERN_FENCE_RE = re.compile(
    r"```yaml[^\n]*\n(\s*#\s*nexus-pattern:\s*(?P<mark>\S+)[^\n]*\n.*?)```",
    re.DOTALL,
)
PLUGIN_CARD_RE = re.compile(
    r"^####\s*插件卡：(?P<code>[A-Za-z0-9_]+)（(?P<kind>[a-z_]+)）",
    re.MULTILINE,
)
TOOL_CARD_RE = re.compile(
    r"^####\s*工具卡：(?P<name>[A-Za-z0-9_]+)（(?P<toolset>[A-Za-z0-9_-]+)）",
    re.MULTILINE,
)
INTERACTION_HEADER = "## 节点交互表"


class _PlaceholderPlugin:
    """Placeholder factory: exists only to satisfy plugin_registry.register's
    callable requirement; it is never instantiated or executed
    (validate_pattern only checks the registry)."""

    def __call__(self, *args, **kwargs):
        raise NotImplementedError("模板占位插件，无实现")


def load_pattern_from_template(text: str, path: Path):
    """Extract the YAML block and construct the Pattern; returns
    (pattern, declared_codes, errors)."""
    import yaml

    from nexus.model.pattern import Pattern
    from nexus.model.plugins_field import PLUGIN_KINDS
    from nexus.pipeline import normalize_skeleton

    errors = []
    match = PATTERN_FENCE_RE.search(text)
    if match is None:
        return None, {}, ["未找到带 `# nexus-pattern:` 标记的 ```yaml 围栏块"]

    spec = yaml.safe_load(match.group(1))
    if not isinstance(spec, dict):
        return None, {}, ["YAML 块不是 dict（Pattern 声明）"]

    code = str(spec.get("code") or "")
    if code and code != match.group("mark"):
        errors.append(
            f"标记注释 code（{match.group('mark')!r}）与声明 code（{code!r}）不一致")

    # Placeholder-register every declared plugin code (one-shot per process;
    # codes already registered are skipped)
    from nexus.registry.plugins import registry as plugin_registry

    declared: dict = {}  # code -> kind
    for slot, pc in (spec.get("plugins") or {}).items():
        kind = PLUGIN_KINDS.get(slot)
        if kind and pc:
            declared[str(pc)] = kind
    for node in spec.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        for slot, pc in (node.get("plugins") or {}).items():
            kind = PLUGIN_KINDS.get(slot)
            if kind and pc:
                declared.setdefault(str(pc), kind)
    if spec.get("pattern_type") == "fsm":
        try:
            for entry in normalize_skeleton(spec.get("stages")):
                for _slot, pc in entry.items():
                    if pc:
                        declared.setdefault(str(pc), "stage")
        except ValueError as e:
            errors.append(f"stages 骨架声明非法: {e}")

    for pc, kind in declared.items():
        if not plugin_registry.has(kind, pc):
            plugin_registry.register(kind, pc, _PlaceholderPlugin())

    try:
        pattern = Pattern(**spec)
    except Exception as e:  # noqa: BLE001 -- validation-script boundary, report as-is
        errors.append(f"Pattern 构造失败: {e}")
        return None, declared, errors
    return pattern, declared, errors


def interaction_table_rows(text: str):
    """Number of table data rows in the interaction-table section
    (header and separator rows excluded)."""
    idx = text.find(INTERACTION_HEADER)
    if idx == -1:
        return None
    rest = text[idx + len(INTERACTION_HEADER):]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    section = rest[:nxt.start()] if nxt else rest
    rows = [ln for ln in section.splitlines() if ln.strip().startswith("|")]
    data = [ln for ln in rows[2:]] if len(rows) >= 2 else []
    return len(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="模板知识库结构校验")
    parser.add_argument("template", help="app-templates/<code>/TEMPLATE.md 路径")
    args = parser.parse_args()

    path = Path(args.template)
    if not path.is_file():
        print(f"[FAIL] 模板文件不存在: {path}")
        return 1
    text = path.read_text(encoding="utf-8")

    failures: list = []
    pattern, declared, errors = load_pattern_from_template(text, path)
    failures.extend(f"YAML/Pattern: {e}" for e in errors)
    if pattern is None:
        print("\n".join(f"[FAIL] {f}" for f in failures))
        return 1

    # --- validate_pattern (strict_tools=False: templates may reference unregistered toolsets) ---
    from nexus.model.validation import validate_pattern

    try:
        validate_pattern(pattern, strict_tools=False)
    except ValueError as e:
        failures.append(f"validate_pattern: {e}")

    # --- structural assertions ---
    node_codes = [n.code for n in pattern.nodes]
    if pattern.entry_node_code not in pattern.node_map:
        failures.append(f"入口节点 {pattern.entry_node_code!r} 不在节点表中")
    if len(node_codes) != len(set(node_codes)):
        failures.append("存在重复节点 code")
    end_nodes = [n.code for n in pattern.nodes if n.is_end]
    if pattern.pattern_type == "fsm" and not end_nodes:
        failures.append("fsm 模式缺少 is_end 终节点")
    for node in pattern.nodes:
        if not node.is_end and not node.sub_nodes:
            failures.append(f"非终节点 {node.code!r} 没有出边")
        if node.is_end and node.sub_nodes:
            failures.append(f"终节点 {node.code!r} 不应还有出边")

    # --- card coverage (both directions) ---
    plugin_cards = {m.group("code"): m.group("kind")
                    for m in PLUGIN_CARD_RE.finditer(text)}
    tool_cards = {m.group("name") for m in TOOL_CARD_RE.finditer(text)}

    for pc, kind in declared.items():
        if pc not in plugin_cards:
            failures.append(f"声明插件 {pc!r}（kind={kind}）缺少插件步骤卡")
        elif plugin_cards[pc] != kind:
            failures.append(
                f"插件卡 {pc!r} 的 kind（{plugin_cards[pc]!r}）与声明"
                f"（{kind!r}）不一致")
    for pc in plugin_cards:
        if pc not in declared:
            failures.append(f"插件卡 {pc!r} 未被 Pattern 声明引用（孤立卡）")

    # --- tool cards vs toolset grants (a declared allow_toolset needs at least one card each) ---
    granted = pattern.allow_toolset or []
    card_toolsets = {m.group("toolset") for m in TOOL_CARD_RE.finditer(text)}
    for ts in granted:
        if ts not in card_toolsets:
            failures.append(f"allow_toolset 含 {ts!r} 但没有对应工具描述卡")

    # --- interaction-table rows == node count ---
    rows = interaction_table_rows(text)
    if rows is None:
        failures.append("缺少「## 节点交互表」章节")
    elif rows != len(pattern.nodes):
        failures.append(
            f"节点交互表行数（{rows}）≠ 节点数（{len(pattern.nodes)}）")

    # --- INDEX.md listing ---
    index = path.parent.parent / "INDEX.md"
    if not index.is_file():
        failures.append(f"知识库总目录缺失: {index}")
    elif pattern.code not in index.read_text(encoding="utf-8"):
        failures.append(f"INDEX.md 未收录模板 {pattern.code!r}")

    # --- report ---
    print(f"模板: {path}")
    print(f"  pattern: {pattern.code}（{pattern.pattern_type}，"
          f"{len(pattern.nodes)} 节点，声明插件 {len(declared)} 个，"
          f"插件卡 {len(plugin_cards)} 张，工具卡 {len(tool_cards)} 张）")
    if failures:
        print()
        print("\n".join(f"[FAIL] {f}" for f in failures))
        return 1
    print("  [PASS] 结构闸全部通过（validate_pattern + 结构断言 + 卡片覆盖"
          "+ 交互表 + INDEX）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
