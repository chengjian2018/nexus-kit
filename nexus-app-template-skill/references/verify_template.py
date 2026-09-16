#!/usr/bin/env python3
"""模板知识库结构校验 — app-templates/<code>/TEMPLATE.md 的机器闸门。

结构闸（SKILL.md Phase 4）：零实现即可校验一份模板的结构合法性——

1. 抽取 TEMPLATE.md 中带 ``# nexus-pattern: <code>`` 标记注释的
   ```yaml 围栏块，加载为 Pattern 声明（节点允许 dict 形态）；
2. 为声明中引用的每个插件码（pattern.plugins / node.plugins /
   stages 骨架，含内置件）注册**占位工厂**——validate_pattern 只要求
   码在注册表里，占位注册即可通过，无需任何真实实现；
3. 构造 Pattern → validate_pattern（strict_tools=False，模板允许引用
   尚未注册的工具集）+ 结构断言；
4. 卡片覆盖校验：每个被声明的插件码有一张 ``#### 插件卡：<code>
   （<kind>）`` 卡且 kind 一致；每张卡都有对应声明（无孤立卡）；
5. 节点交互表行数 = 节点数；
6. INDEX.md 收录该模板 code。

用法：
    python nexus-app-template-skill/references/verify_template.py \
        app-templates/<code>/TEMPLATE.md

退出码：0 = 全部通过；1 = 有失败项（逐条打印）。
"""

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # references/ 两层之上才是仓库根
sys.path.insert(0, str(REPO_ROOT))

# YAML 围栏块首行标记（SKILL.md 规定的抽取锚点）
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
    """占位工厂：仅满足 plugin_registry.register 的 callable 要求，
    永不实例化执行（validate_pattern 只查注册表）。"""

    def __call__(self, *args, **kwargs):
        raise NotImplementedError("模板占位插件，无实现")


def load_pattern_from_template(text: str, path: Path):
    """抽取 YAML 块并构造 Pattern；返回 (pattern, declared_codes, errors)。"""
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

    # 占位注册所有被声明的插件码（进程内一次性；已注册的同码跳过）
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
    except Exception as e:  # noqa: BLE001 — 校验脚本边界，如实上报
        errors.append(f"Pattern 构造失败: {e}")
        return None, declared, errors
    return pattern, declared, errors


def interaction_table_rows(text: str):
    """节点交互表一节的表格数据行数（剔除表头与分隔行）。"""
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

    # --- validate_pattern（strict_tools=False：模板允许引用未注册工具集）---
    from nexus.model.validation import validate_pattern

    try:
        validate_pattern(pattern, strict_tools=False)
    except ValueError as e:
        failures.append(f"validate_pattern: {e}")

    # --- 结构断言 ---
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

    # --- 卡片覆盖（双向）---
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

    # --- 工具卡与工具集授权（声明了 allow_toolset 的，每个工具集至少一卡）---
    granted = pattern.allow_toolset or []
    card_toolsets = {m.group("toolset") for m in TOOL_CARD_RE.finditer(text)}
    for ts in granted:
        if ts not in card_toolsets:
            failures.append(f"allow_toolset 含 {ts!r} 但没有对应工具描述卡")

    # --- 节点交互表行数 = 节点数 ---
    rows = interaction_table_rows(text)
    if rows is None:
        failures.append("缺少「## 节点交互表」章节")
    elif rows != len(pattern.nodes):
        failures.append(
            f"节点交互表行数（{rows}）≠ 节点数（{len(pattern.nodes)}）")

    # --- INDEX.md 收录 ---
    index = path.parent.parent / "INDEX.md"
    if not index.is_file():
        failures.append(f"知识库总目录缺失: {index}")
    elif pattern.code not in index.read_text(encoding="utf-8"):
        failures.append(f"INDEX.md 未收录模板 {pattern.code!r}")

    # --- 报告 ---
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
