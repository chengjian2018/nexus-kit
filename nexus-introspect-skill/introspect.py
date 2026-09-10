#!/usr/bin/env python3
"""nexus-introspect — 面向 agent 的应用/插件内省 CLI（独立脚本）。

设计约束：**不改动仓库任何现有文件**——内核能力全部经 import 复用：

- 注册表快照：``nexus.registry.patterns`` / ``nexus.registry.plugins``
  （app→pattern/plugin 归属 = 逐 app 导入 diff 注册表，先导入
  ``atoms.stages`` / ``atoms.executors`` 把内核注册物摘出去）
- 装配口径 = 运行时口径：``nexus.pipeline.resolve_stage_code`` /
  ``builtin_generate_default``（``cxt=None`` 即模块级解析，pipeline 显式
  支持）；executor 链镜像 ``chat._resolve_executor_code`` 的四层读法
- pattern YAML：``nexus.model.serialization.pattern_to_yaml``
- 源码提取：``inspect`` + 三种 factory 形态的 unwrap 规则（类/函数本体、
  lambda 包装、工厂函数+AST 产物类），``builtin:`` 标记经
  ``pipeline._resolve_builtin_stage`` 解析

用法（仓库根目录、项目 venv）::

    python nexus-introspect-skill/introspect.py apps
    python nexus-introspect-skill/introspect.py plugins --kind stage
    python nexus-introspect-skill/introspect.py pattern xianyu_agent --view resolved
    python nexus-introspect-skill/introspect.py plugin stage install_unified
    python nexus-introspect-skill/introspect.py who-uses stage install_unified

所有子命令支持 ``--json``。只读，无 LLM/DB 依赖（import 副作用为纯注册，
与 host CLI 的 ``_ensure_discovery`` 同源）。用法与语义说明见同目录 SKILL.md。
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 仓库根 bootstrap（脚本可从任意 CWD 运行；nexus/atoms 需在 sys.path）
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nexus.model.serialization import pattern_to_yaml  # noqa: E402
from nexus.pipeline import (  # noqa: E402
    _resolve_builtin_stage,
    builtin_generate_default,
    normalize_skeleton,
    resolve_stage_code,
)
from nexus.registry.discovery import import_modules, module_registers  # noqa: E402
from nexus.registry.patterns import registry as pattern_registry  # noqa: E402
from nexus.registry.plugins import DEFAULT_EXECUTOR_CODES  # noqa: E402
from nexus.registry.plugins import registry as plugin_registry  # noqa: E402

APPS_DIR = REPO_ROOT / "apps"

# module type value -> executor family（镜像 chat._resolve_executor_code）
FAMILY_OF_TYPE = {"agent": "loop", "fsm": "fsm", "route": "route"}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PluginInfo:
    kind: str
    code: str
    file: str                       # 仓库相对路径（解析失败为空）
    lineno: int                     # 定义起始行（0 = 未知）
    owner: str                      # apps | atoms | nexus | other
    owner_app: Optional[str]        # 仅 owner=apps
    factory_form: str               # class | function | lambda-wrapped | factory-function | builtin-marker | unknown
    source: str = ""                # 源码纯文本（--no-source / 失败为空）
    note: str = ""                  # 提取注记（unwrap 说明 / 失败原因）
    used_by: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class AppInfo:
    name: str
    files: List[str]
    patterns: List[str]
    plugins: List[List[str]]        # [[kind, code], ...]


# ---------------------------------------------------------------------------
# 数据层：discovery + 归属 + 源码定位
# ---------------------------------------------------------------------------

_WARM = False
_APPS: List[AppInfo] = []


def _registry_snapshot() -> Tuple[set, set]:
    """(pattern 码集合, (kind, code) 插件键集合)——读私有存储，只读内省可接受。"""
    return (set(pattern_registry.list_codes()),
            set(plugin_registry._factories.keys()))


def warm_up() -> List[AppInfo]:
    """触发注册（幂等）：先内核 atoms，再逐 app 导入并 diff 归属。"""
    global _WARM, _APPS
    if _WARM:
        return _APPS

    # 内核注册物先落位（apps 的 import 链可能带出它们；先导入保证归属
    # diff 不会把 default_loop 等算到某个 app 头上）
    import atoms.executors  # noqa: F401  三默认 executor
    import atoms.stages     # noqa: F401  内置具名 stage + 默认工厂

    apps: List[AppInfo] = []
    if APPS_DIR.is_dir():
        for app_dir in sorted(p for p in APPS_DIR.iterdir()
                              if p.is_dir() and (p / "__init__.py").exists()):
            registering = [
                p for p in sorted(app_dir.glob("*.py"))
                if p.name != "__init__.py" and module_registers(p)
            ]
            pats_before, plugs_before = _registry_snapshot()
            import_modules(
                [f"apps.{app_dir.name}.{p.stem}" for p in registering],
                what="introspect app module")
            pats_after, plugs_after = _registry_snapshot()
            apps.append(AppInfo(
                name=app_dir.name,
                files=[p.name for p in sorted(app_dir.glob("*.py"))],
                patterns=sorted(pats_after - pats_before),
                plugins=[list(k) for k in sorted(plugs_after - plugs_before)],
            ))
    _WARM, _APPS = True, apps
    return apps


def _relativize(path: str) -> str:
    p = Path(path).resolve()
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def owner_of(file: str) -> Tuple[str, Optional[str]]:
    """实现文件 → (owner, owner_app)（按路径前缀推断，零注册表改动）。"""
    norm = "/" + file.replace("\\", "/").lstrip("/")
    if "/apps/" in norm:
        return "apps", norm.split("/apps/")[1].split("/")[0]
    if "/atoms/" in norm:
        return "atoms", None
    if "/nexus/" in norm:
        return "nexus", None
    return "other", None


# ---------------------------------------------------------------------------
# 源码提取（三种 factory 形态 + builtin 标记）
# ---------------------------------------------------------------------------

def _closure_target(fn) -> Optional[Any]:
    """lambda 工厂 → 真实实现。闭包 cell 优先（单 cell 直指）；
    无捕获的 lambda（如 ``lambda: customer_agent_messages_builder``、
    ``lambda: (FSMNLU(), FSMNLG())``——引用的是模块级全局名，非自由变量）
    回退 ``__code__.co_names``（代码对象引用的全局名元组）→
    ``__globals__`` 找 类/函数。co_names 无解析、不受 getsource 只能
    拿到 lambda 所在行片段（常带尾括号、AST 必败）的影响。"""
    for cell in (getattr(fn, "__closure__", None) or ()):
        try:
            value = cell.cell_contents
        except ValueError:            # empty cell
            continue
        if inspect.isclass(value) or inspect.isfunction(value):
            return value
    for name in getattr(getattr(fn, "__code__", None), "co_names", ()):
        target = (fn.__globals__ or {}).get(name)
        if ((inspect.isclass(target) or inspect.isfunction(target))
                and target is not fn):
            return target
    return None


def _factory_products(fn) -> List[Any]:
    """AST 扫工厂函数体：return <Name>(...) / return (a(), b()) 中的产物类。"""
    try:
        tree = ast.parse(inspect.getsource(fn))
    except (OSError, TypeError, SyntaxError):
        return []
    calls: List[ast.Call] = []

    def _collect(node: ast.AST) -> None:
        if isinstance(node, ast.Return):
            value = node.value
            targets = value.elts if isinstance(value, ast.Tuple) else [value]
            calls.extend(t for t in targets if isinstance(t, ast.Call))
        for child in ast.iter_child_nodes(node):
            _collect(child)

    _collect(tree)
    out = []
    for call in calls:
        if isinstance(call.func, ast.Name):
            cls = (fn.__globals__ or {}).get(call.func.id)
            if inspect.isclass(cls):
                out.append(cls)
    return out


def _locate(obj) -> Tuple[str, int, str]:
    """(file, lineno, qualname)；取不到源码位置时逐级降级。"""
    try:
        file = _relativize(inspect.getfile(obj))
    except TypeError:
        return "", 0, getattr(obj, "__qualname__", repr(obj))
    lineno = 0
    try:
        lineno = int(inspect.getsourcelines(obj)[1])   # 类与函数通用
    except (OSError, TypeError):
        pass
    return file, lineno, getattr(obj, "__qualname__", getattr(obj, "__name__", ""))


def plugin_source(kind: str, code: str) -> PluginInfo:
    """(kind, code) → 元数据 + 源码纯文本。builtin: 标记单独解析。"""
    if code.startswith("builtin:"):
        rest = code.removeprefix("builtin:")
        element = 0
        if "#" in rest:
            rest, elem = rest.split("#", 1)
            element = int(elem)
        try:
            instance = _resolve_builtin_stage(f"builtin:{rest}", element)
            target = type(instance)
        except Exception as e:                      # noqa: BLE001 只读查询不炸
            return PluginInfo(kind, code, "", 0, "other", None,
                              "builtin-marker", note=f"解析失败: {e}")
        info = _info_from_target("stage_factory", rest, target,
                                 factory_form="builtin-marker",
                                 note=f"builtin 标记，产物类经 element=#{element} 选取")
        info.code = code
        return info

    factory = plugin_registry._factories.get((kind, code))
    if factory is None:
        raise SystemExit(f"插件未注册: kind={kind!r}, code={code!r}"
                         f"（可用 kinds: {sorted({k for k, _ in plugin_registry._factories})}）")

    # 形态 2：lambda 包装 → 闭包/co_names unwrap 到真实实现
    if inspect.isfunction(factory) and factory.__name__ == "<lambda>":
        target = _closure_target(factory)
        if target is not None:
            return _info_from_target(kind, code, target,
                                     factory_form="lambda-wrapped",
                                     note="经 lambda 工厂注册，以下为解出的真实实现")
        info = _info_from_target(kind, code, factory,
                                 factory_form="lambda-wrapped",
                                 note="闭包 unwrap 失败，仅有 lambda 本体")
        return info

    # 形态 3：具名工厂函数 → 工厂体 + AST 扫出的产物类（双段）
    if inspect.isfunction(factory):
        products = _factory_products(factory)
        if products:
            info = _info_from_target(kind, code, products[0],
                                     factory_form="factory-function")
            try:
                info.source = (inspect.getsource(factory)
                               + "\n\n# ---- 产物类（AST 扫工厂体所得）----\n\n"
                               + info.source)
            except (OSError, TypeError):
                pass
            return info

    # 形态 1：类/函数本体（或未识别形态尽力取一次）
    return _info_from_target(kind, code, factory)


def _info_from_target(kind: str, code: str, target: Any,
                      factory_form: str = "", note: str = "") -> PluginInfo:
    file, lineno, _qual = _locate(target)
    owner, owner_app = owner_of(file)
    form = factory_form or ("class" if inspect.isclass(target) else "function")
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError) as e:
        source = ""
        note = note or f"源码不可得（{e}），仅定位: {file}:{lineno}"
    return PluginInfo(kind, code, file, lineno, owner, owner_app,
                      form, source=source, note=note)


# ---------------------------------------------------------------------------
# 查询层：pattern 视图 / 生效装配 / 反向索引
# ---------------------------------------------------------------------------

def _type_value(module) -> str:
    return getattr(getattr(module, "type", None), "value", "") or ""


def resolved_executor(module, pattern) -> Tuple[str, str]:
    """(code, 来源层)——镜像 chat._resolve_executor_code 四层链。"""
    decl = getattr(module, "executor", None)
    if decl:
        return decl, "module.executor"
    family = FAMILY_OF_TYPE.get(_type_value(module), "")
    decl = (getattr(module, "plugins", None) or {}).get(family) if family else None
    if decl:
        return decl, f"module.plugins[{family!r}]"
    decl = getattr(pattern, f"executor_{family}", None) if family else None
    if decl:
        return decl, f"pattern.executor_{family}"
    type_key = _type_value(module)
    if type_key in DEFAULT_EXECUTOR_CODES:
        return DEFAULT_EXECUTOR_CODES[type_key], "默认链尾"
    return "", "未解析"


def resolved_stages(module, pattern) -> List[Dict[str, str]]:
    """逐槽生效码 + 来源层——复用 resolve_stage_code（cxt=None 模块级），
    builtin 兜底同 resolve_execution_sequence（仅 nlu/nlg、仅骨架携带的槽）。"""
    skeleton = normalize_skeleton(getattr(pattern, "stages", None))
    skel_vals = {slot: code for entry in skeleton for slot, code in entry.items()}
    codes: Dict[str, Optional[str]] = {}
    for entry in skeleton:
        (slot, _skel), = entry.items()
        codes[slot] = resolve_stage_code(
            slot, None, module, pattern, skeleton_value=skel_vals.get(slot))
    if codes.get("nlu") is None or codes.get("nlg") is None:
        pair = builtin_generate_default(getattr(module, "type", None))
        if pair:
            for slot in ("nlu", "nlg"):
                if codes.get(slot) is None:
                    codes[slot] = pair[slot]

    out = []
    module_stages = getattr(module, "stages", None) or {}
    for slot, code in codes.items():
        if code is None:
            out.append({"slot": slot, "code": None, "layer": "跳过（声明 None）"})
        elif slot in module_stages and module_stages[slot] == code:
            out.append({"slot": slot, "code": code, "layer": "module 层"})
        elif skel_vals.get(slot) == code:
            out.append({"slot": slot, "code": code, "layer": "pattern 层"})
        else:
            out.append({"slot": slot, "code": code, "layer": "builtin 默认"})
    return out


def _locate_code(code: str) -> str:
    """码 → 文件:行 定位串（stage/executor 注册物或 builtin 标记）。"""
    try:
        for kind in ("stage", "executor", "messages_builder", "stage_factory"):
            if plugin_registry.has(kind, code):
                info = plugin_source(kind, code)
                return f"{info.file}:{info.lineno}" if info.file else "(定位失败)"
    except SystemExit:
        pass
    if code.startswith("builtin:"):
        info = plugin_source("stage_factory", code)
        return f"{info.file}:{info.lineno}" if info.file else "(定位失败)"
    return "(未注册码)"


def _module_header(module) -> str:
    nodes = getattr(module, "module_nodes", None) or []
    bits = [f"[{_type_value(module) or '?'}"]
    if nodes:
        bits[0] = f"[{_type_value(module) or '?'}, {len(nodes)} 节点"
    bits[0] += "]"
    if getattr(module, "is_end", False):
        bits.append("is_end")
    return " ".join(bits)


def render_tree(pattern) -> str:
    lines = [f"{pattern.code} — {pattern.name} [entry: {pattern.entry_module_code}]"]
    if getattr(pattern, "description", ""):
        lines.append(f"  {pattern.description}")
    for module in (pattern.modules or []):
        lines.append(f"├─ {module.module_code}  {module.module_name}  "
                     f"{_module_header(module)}")
        for key, label in (("stages", "stages 声明"),
                           ("executor", "executor 声明"),
                           ("messages_builder", "messages_builder"),
                           ("use_tools", "use_tools")):
            val = getattr(module, key, None)
            if val:
                lines.append(f"│    {label}: {val}")
        subs = getattr(module, "sub_modules", None) or []
        if subs:
            lines.append(f"│    sub_modules: {subs}")
        for node in (getattr(module, "module_nodes", None) or []):
            marks = []
            if getattr(node, "is_end", False):
                marks.append("is_end")
            node_stages = getattr(node, "stages", None) or {}
            if node_stages:
                marks.append(f"stages={node_stages}")
            suffix = f"  ({', '.join(marks)})" if marks else ""
            lines.append(f"│    · {node.node_code}  {node.node_name}"
                         f"→ {list(node.sub_nodes or [])}{suffix}")
    return "\n".join(lines)


def render_resolved(pattern) -> str:
    lines = [f"{pattern.code} — {pattern.name} [entry: {pattern.entry_module_code}]"]
    lines.append("  （生效装配 = 运行时口径：module > pattern 骨架 > builtin 默认；"
                 "executor 链 module.executor > pattern.executor_<family> > 默认）")
    for module in (pattern.modules or []):
        lines.append(f"├─ {module.module_code}  {module.module_name}  "
                     f"{_module_header(module)}")
        ex_code, ex_layer = resolved_executor(module, pattern)
        lines.append(f"│    executor  {ex_code or '—'}  [{ex_layer}]  "
                     f"{_locate_code(ex_code) if ex_code else ''}")
        stages = resolved_stages(module, pattern)
        if all(s["code"] is None for s in stages):
            lines.append("│    stages    全槽跳过（无声明且无 builtin 默认——"
                         "agent 模块走 executor 自主管线，不经 stages 流水线）")
            continue
        nlu_code = next((s["code"] for s in stages if s["slot"] == "nlu"), None)
        for s in stages:
            code, slot, layer = s["code"], s["slot"], s["layer"]
            if slot == "nlg" and code and code == nlu_code:
                lines.append(f"│    {slot:<8} {code}  [{layer}]  "
                             "(与 nlu 同码，运行时统一阶段合并执行)")
            elif code:
                lines.append(f"│    {slot:<8} {code}  [{layer}]  {_locate_code(code)}")
            else:
                lines.append(f"│    {slot:<8} —  {layer}")
    return "\n".join(lines)


def _declared_refs(pattern) -> List[Tuple[str, str, str]]:
    """直接声明引用 (kind, code, where)——who-uses 数据源。"""
    refs: List[Tuple[str, str, str]] = []
    for module in (pattern.modules or []):
        for slot, code in (getattr(module, "stages", None) or {}).items():
            if code:
                refs.append(("stage", code, f"{module.module_code}.stages[{slot!r}]"))
        for key, kind in (("executor", "executor"), ("messages_builder", "messages_builder"),
                          ("agent_hooks", "agent_hooks")):
            code = getattr(module, key, None)
            if code:
                refs.append((kind, code, f"{module.module_code}.{key}"))
        for family, code in (getattr(module, "plugins", None) or {}).items():
            if code and family in FAMILY_OF_TYPE.values():
                refs.append(("executor", code, f"{module.module_code}.plugins[{family!r}]"))
    for slot, code in {s: c for e in normalize_skeleton(getattr(pattern, "stages", None))
                       for s, c in e.items()}.items():
        if code:
            refs.append(("stage", code, f"pattern.stages[{slot!r}]"))
    for family in FAMILY_OF_TYPE.values():
        code = getattr(pattern, f"executor_{family}", None)
        if code:
            refs.append(("executor", code, f"pattern.executor_{family}"))
    for key, kind in (("messages_builder", "messages_builder"), ("agent_hooks", "agent_hooks")):
        code = getattr(pattern, key, None)
        if code:
            refs.append((kind, code, f"pattern.{key}"))
    return refs


def who_uses(kind: str, code: str) -> List[Tuple[str, List[str]]]:
    """反向索引：[(pattern 码, [引用位置…])]——只含直接声明引用。"""
    warm_up()
    out = []
    for pattern in pattern_registry.list_patterns():
        wheres = [w for (k, c, w) in _declared_refs(pattern)
                  if (k, c) == (kind, code)]
        if wheres:
            out.append((pattern.code, wheres))
    return sorted(out)


# ---------------------------------------------------------------------------
# 暴露层：子命令渲染
# ---------------------------------------------------------------------------

def cmd_apps(args) -> None:
    apps = warm_up()
    if args.json:
        print(json.dumps([dataclasses.asdict(a) for a in apps],
                         ensure_ascii=False, indent=2))
        return
    for app in apps:
        print(f"▸ {app.name}  ({len(app.files)} files)")
        for code in app.patterns:
            p = pattern_registry.get(code)
            print(f"    pattern  {code} — {getattr(p, 'name', '')}")
        for kind, code in app.plugins:
            print(f"    plugin   ({kind}) {code}")
        if not app.patterns and not app.plugins:
            print("    （无注册物）")


def cmd_plugins(args) -> None:
    warm_up()
    infos = []
    for (kind, code) in sorted(plugin_registry._factories):
        if args.kind and kind != args.kind:
            continue
        info = plugin_source(kind, code)
        if args.owner and info.owner != args.owner:
            continue
        infos.append(info)
    if args.json:
        print(json.dumps([dataclasses.asdict(i) for i in infos],
                         ensure_ascii=False, indent=2))
        return
    by_owner: Dict[str, List[PluginInfo]] = {}
    for i in infos:
        by_owner.setdefault(i.owner_app or i.owner, []).append(i)
    for owner, group in sorted(by_owner.items()):
        print(f"▸ {owner} ({len(group)}):")
        for i in group:
            loc = f"{i.file}:{i.lineno}" if i.file else "(定位失败)"
            print(f"    ({i.kind}) {i.code}  —  {loc}  [{i.factory_form}]")


def _get_pattern(code: str):
    warm_up()
    pattern = pattern_registry.get(code)
    if pattern is None:
        raise SystemExit(f"pattern {code!r} 未注册（可用: "
                         f"{pattern_registry.list_codes()}）")
    return pattern


def cmd_pattern(args) -> None:
    pattern = _get_pattern(args.code)
    if args.view == "yaml":
        print(pattern_to_yaml(pattern))
        return
    if args.json:
        modules = []
        for module in (pattern.modules or []):
            modules.append({
                "module_code": module.module_code,
                "type": _type_value(module),
                "executor": resolved_executor(module, pattern),
                "stages": resolved_stages(module, pattern),
            })
        print(json.dumps({"code": pattern.code, "name": pattern.name,
                          "entry": pattern.entry_module_code,
                          "modules": modules}, ensure_ascii=False, indent=2))
        return
    print(render_tree(pattern) if args.view == "tree" else render_resolved(pattern))


def cmd_plugin(args) -> None:
    warm_up()
    info = plugin_source(args.kind, args.code)
    if not args.no_source:
        info.used_by = [p for p, _ in who_uses(args.kind, args.code)]
    if args.json:
        print(json.dumps(dataclasses.asdict(info), ensure_ascii=False, indent=2))
        return
    print(f"({info.kind}) {info.code}")
    print(f"  归属:   {info.owner}"
          + (f" / {info.owner_app}" if info.owner_app else ""))
    print(f"  定义:   {info.file or '?'}:{info.lineno}"
          f"  [{info.factory_form}]")
    if info.note:
        print(f"  注记:   {info.note}")
    if not args.no_source:
        if info.used_by:
            print(f"  被引用: {', '.join(info.used_by)}（直接声明）")
        print("─" * 60)
        print(info.source or "(源码不可得)")


def cmd_who_uses(args) -> None:
    hits = who_uses(args.kind, args.code)
    if args.json:
        print(json.dumps([{"pattern": p, "where": w} for p, w in hits],
                         ensure_ascii=False, indent=2))
        return
    if not hits:
        print(f"无 pattern 直接声明引用 ({args.kind}) {args.code}")
        return
    for p, wheres in hits:
        print(f"▸ {p}")
        for w in wheres:
            print(f"    {w}")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="nexus-introspect",
        description="nexus-kit 应用/pattern/插件内省（只读；见 "
                    "docs/design/introspect-skill.md）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    json_flag = {"action": "store_true", "help": "JSON 输出"}

    sub.add_parser("apps", help="应用总览：目录 × 注册物归属").add_argument(
        "--json", **json_flag)

    p_plugins = sub.add_parser("plugins", help="插件总览")
    p_plugins.add_argument("--kind", help="executor|stage|messages_builder|stage_factory")
    p_plugins.add_argument("--owner", help="apps|atoms|nexus")
    p_plugins.add_argument("--json", **json_flag)

    p_pattern = sub.add_parser("pattern", help="pattern 视图")
    p_pattern.add_argument("code")
    p_pattern.add_argument("--view", choices=["tree", "yaml", "resolved"],
                           default="tree")
    p_pattern.add_argument("--json", **json_flag)

    p_plugin = sub.add_parser("plugin", help="插件元数据 + 源码")
    p_plugin.add_argument("kind")
    p_plugin.add_argument("code")
    p_plugin.add_argument("--no-source", action="store_true")
    p_plugin.add_argument("--json", **json_flag)

    p_who = sub.add_parser("who-uses", help="反向索引（直接声明引用）")
    p_who.add_argument("kind")
    p_who.add_argument("code")
    p_who.add_argument("--json", **json_flag)

    args = parser.parse_args(argv)
    handlers = {"apps": cmd_apps, "plugins": cmd_plugins, "pattern": cmd_pattern,
                "plugin": cmd_plugin, "who-uses": cmd_who_uses}
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
