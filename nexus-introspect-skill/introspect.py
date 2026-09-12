#!/usr/bin/env python3
"""nexus-introspect — application/plugin introspection CLI for agents
(standalone script).

Design constraint: **modify no existing repo files** — kernel capabilities
are reused entirely via imports:

- Registry snapshots: ``nexus.registry.patterns`` / ``nexus.registry.plugins``
  (app→pattern/plugin ownership attribution = import each app and diff the
  registries, importing ``atoms.stages`` / ``atoms.executors`` first so the
  kernel registrations are set aside)
- Assembly view = runtime view: ``nexus.pipeline.resolve_stage_code`` /
  ``builtin_generate_default`` (``cxt=None`` means module-level resolution,
  explicitly supported by the pipeline); the executor chain mirrors
  ``chat._resolve_executor_code``'s four-layer read order
- pattern YAML: ``nexus.model.serialization.pattern_to_yaml``
- Source extraction: ``inspect`` + unwrap rules for three factory forms
  (class/function body, lambda wrapper, factory function + AST-scanned
  product classes); ``builtin:`` markers resolve via
  ``pipeline._resolve_builtin_stage``

Usage (repo root, project venv)::

    python nexus-introspect-skill/introspect.py apps
    python nexus-introspect-skill/introspect.py plugins --kind stage
    python nexus-introspect-skill/introspect.py pattern xianyu_agent --view resolved
    python nexus-introspect-skill/introspect.py plugin stage install_unified
    python nexus-introspect-skill/introspect.py who-uses stage install_unified

All subcommands support ``--json``. Read-only, no LLM/DB dependency (import
side effects are pure registration, same source as the host CLI's
``_ensure_discovery``). For usage and semantics see SKILL.md in the same
directory.
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
# Repo-root bootstrap (script is runnable from any CWD; nexus/atoms must be on sys.path)
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

# pattern_type -> executor slot (mirrors chat._resolve_node_executor_code;
# plan-⑧: FSM resolves at the pattern level, AGENT per node)
TYPE_SLOT = {"fsm": "fsm", "agent": "loop"}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PluginInfo:
    kind: str
    code: str
    file: str                       # repo-relative path (empty if resolution fails)
    lineno: int                     # definition start line (0 = unknown)
    owner: str                      # apps | atoms | nexus | other
    owner_app: Optional[str]        # only when owner=apps
    factory_form: str               # class | function | lambda-wrapped | factory-function | builtin-marker | unknown
    source: str = ""                # plain-text source (empty with --no-source / on failure)
    note: str = ""                  # extraction note (unwrap explanation / failure reason)
    used_by: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class AppInfo:
    name: str
    files: List[str]
    patterns: List[str]
    plugins: List[List[str]]        # [[kind, code], ...]


# ---------------------------------------------------------------------------
# Data layer: discovery + ownership attribution + source location
# ---------------------------------------------------------------------------

_WARM = False
_APPS: List[AppInfo] = []


def _registry_snapshot() -> Tuple[set, set]:
    """(set of pattern codes, set of (kind, code) plugin keys) — reads private storage; acceptable for read-only introspection."""
    return (set(pattern_registry.list_codes()),
            set(plugin_registry._factories.keys()))


def warm_up() -> List[AppInfo]:
    """Trigger registration (idempotent): kernel atoms first, then import each app and diff ownership."""
    global _WARM, _APPS
    if _WARM:
        return _APPS

    # Kernel registrations land first (apps' import chains may pull them in;
    # importing first guarantees the ownership diff never attributes
    # default_loop etc. to some app)
    import atoms.executors  # noqa: F401  three default executors
    import atoms.stages     # noqa: F401  builtin named stages + default factories

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
    """Implementation file → (owner, owner_app) (inferred from path prefix, zero registry changes)."""
    norm = "/" + file.replace("\\", "/").lstrip("/")
    if "/apps/" in norm:
        return "apps", norm.split("/apps/")[1].split("/")[0]
    if "/atoms/" in norm:
        return "atoms", None
    if "/nexus/" in norm:
        return "nexus", None
    return "other", None


# ---------------------------------------------------------------------------
# Source extraction (three factory forms + builtin markers)
# ---------------------------------------------------------------------------

def _closure_target(fn) -> Optional[Any]:
    """Lambda factory → real implementation. Closure cells take priority (a
    single cell points straight at it); capture-free lambdas (e.g.
    ``lambda: customer_agent_messages_builder``,
    ``lambda: (FSMNLU(), FSMNLG())`` — these reference module-level global
    names, not free variables) fall back to ``__code__.co_names`` (the tuple
    of global names referenced by the code object) → ``__globals__`` to find
    the class/function. co_names needs no resolution and is unaffected by
    getsource only returning the lambda's own line fragment (often with a
    trailing parenthesis, where AST parsing always fails)."""
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
    """AST-scan the factory function body: product classes inside return <Name>(...) / return (a(), b())."""
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
    """(file, lineno, qualname); degrades level by level when the source location is unavailable."""
    try:
        file = _relativize(inspect.getfile(obj))
    except TypeError:
        return "", 0, getattr(obj, "__qualname__", repr(obj))
    lineno = 0
    try:
        lineno = int(inspect.getsourcelines(obj)[1])   # works for both classes and functions
    except (OSError, TypeError):
        pass
    return file, lineno, getattr(obj, "__qualname__", getattr(obj, "__name__", ""))


def plugin_source(kind: str, code: str) -> PluginInfo:
    """(kind, code) → metadata + plain-text source. builtin: markers resolve separately."""
    if code.startswith("builtin:"):
        rest = code.removeprefix("builtin:")
        element = 0
        if "#" in rest:
            rest, elem = rest.split("#", 1)
            element = int(elem)
        try:
            instance = _resolve_builtin_stage(f"builtin:{rest}", element)
            target = type(instance)
        except Exception as e:                      # noqa: BLE001 read-only query must not crash
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

    # Form 2: lambda wrapper → closure/co_names unwrap to the real implementation
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

    # Form 3: named factory function → factory body + AST-scanned product classes (two segments)
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

    # Form 1: class/function body (or one best-effort attempt for unrecognized forms)
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
# Query layer: pattern views / effective assembly / reverse index
# ---------------------------------------------------------------------------

def _pattern_type(pattern) -> str:
    return getattr(pattern, "pattern_type", "") or "agent"


def resolved_executor(node, pattern) -> Tuple[str, str]:
    """(code, source layer) — mirrors chat._resolve_node_executor_code's
    two-layer chain: node.plugins['loop'] > pattern.plugins['loop'] >
    default_loop (AGENT nodes)."""
    decl = (getattr(node, "plugins", None) or {}).get("loop")
    if decl:
        return decl, "node.plugins['loop']"
    decl = (getattr(pattern, "plugins", None) or {}).get("loop")
    if decl:
        return decl, "pattern.plugins['loop']"
    type_key = _pattern_type(pattern)
    if type_key in DEFAULT_EXECUTOR_CODES:
        return DEFAULT_EXECUTOR_CODES[type_key], "默认链尾"
    return "", "未解析"


def resolved_fsm_executor(pattern) -> Tuple[str, str]:
    """FSM pattern-level executor: pattern.plugins['fsm'] > default_fsm."""
    decl = (getattr(pattern, "plugins", None) or {}).get("fsm")
    if decl:
        return decl, "pattern.plugins['fsm']"
    return DEFAULT_EXECUTOR_CODES["fsm"], "默认链尾"


def resolved_stages(pattern) -> List[Dict[str, str]]:
    """Effective code + source layer per skeleton slot (FSM only) — reuses
    resolve_stage_code at the pattern layer (cxt/node None; per-node
    overrides are visible in the tree view), with the builtin fallback
    identical to resolve_execution_sequence."""
    if _pattern_type(pattern) != "fsm":
        return []
    skeleton = normalize_skeleton(getattr(pattern, "stages", None))
    skel_vals = {slot: code for entry in skeleton for slot, code in entry.items()}
    codes: Dict[str, Optional[str]] = {}
    for entry in skeleton:
        (slot, _skel), = entry.items()
        codes[slot] = resolve_stage_code(
            slot, None, None, pattern, skeleton_value=skel_vals.get(slot))
    if codes.get("nlu") is None or codes.get("nlg") is None:
        pair = builtin_generate_default("fsm")
        if pair:
            for slot in ("nlu", "nlg"):
                if codes.get(slot) is None:
                    codes[slot] = pair[slot]

    out = []
    for slot, code in codes.items():
        if code is None:
            out.append({"slot": slot, "code": None, "layer": "跳过（声明 None）"})
        elif skel_vals.get(slot) == code:
            out.append({"slot": slot, "code": code, "layer": "pattern 层"})
        else:
            out.append({"slot": slot, "code": code, "layer": "builtin 默认"})
    return out


def _locate_code(code: str) -> str:
    """Code → file:line location string (stage/executor registrations or builtin markers)."""
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


def _node_marks(node) -> str:
    marks = []
    if getattr(node, "is_end", False):
        marks.append("is_end")
    node_stages = getattr(node, "stages", None) or {}
    if node_stages:
        marks.append(f"stages={node_stages}")
    if getattr(node, "slots", None):
        marks.append(f"slots={list(node.slots)}")
    if getattr(node, "use_tools", None):
        marks.append(f"use_tools={node.use_tools}")
    return f"  ({', '.join(marks)})" if marks else ""


def render_tree(pattern) -> str:
    lines = [f"{pattern.code} — {pattern.name} "
             f"[{_pattern_type(pattern)}, entry: {pattern.entry_node_code}]"]
    if getattr(pattern, "description", ""):
        lines.append(f"  {pattern.description}")
    if getattr(pattern, "allow_toolset", None):
        lines.append(f"  allow_toolset: {pattern.allow_toolset}")
    ptype = _pattern_type(pattern)
    if ptype == "fsm":
        skel = {slot: code for e in normalize_skeleton(getattr(pattern, "stages", None))
                for slot, code in e.items()}
        lines.append(f"  stages 骨架: {skel}")
    for node in (pattern.nodes or []):
        lines.append(f"├─ {node.code}  {node.name or ''}"
                     f"→ {list(node.sub_nodes or [])}{_node_marks(node)}")
        node_plugins = getattr(node, "plugins", None) or {}
        if node_plugins:
            lines.append(f"│    plugins: {node_plugins}")
    return "\n".join(lines)


def render_resolved(pattern) -> str:
    lines = [f"{pattern.code} — {pattern.name} "
             f"[{_pattern_type(pattern)}, entry: {pattern.entry_node_code}]"]
    ptype = _pattern_type(pattern)
    if ptype == "fsm":
        lines.append("  （生效装配 = 运行时口径：node.stages > pattern 骨架 > builtin 默认；"
                     "executor 链 pattern.plugins['fsm'] > 默认）")
        ex_code, ex_layer = resolved_fsm_executor(pattern)
        lines.append(f"├─ [pattern] executor  {ex_code or '—'}  [{ex_layer}]  "
                     f"{_locate_code(ex_code) if ex_code else ''}")
        stages = resolved_stages(pattern)
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
    else:
        lines.append("  （生效装配 = 运行时口径：节点执行器链 node.plugins['loop'] > "
                     "pattern.plugins['loop'] > default_loop；AGENT 不跑 stages）")
        for node in (pattern.nodes or []):
            lines.append(f"├─ {node.code}  {node.name or ''}")
            ex_code, ex_layer = resolved_executor(node, pattern)
            lines.append(f"│    executor  {ex_code or '—'}  [{ex_layer}]  "
                         f"{_locate_code(ex_code) if ex_code else ''}")
    return "\n".join(lines)


def _declared_refs(pattern) -> List[Tuple[str, str, str]]:
    """Directly declared references (kind, code, where) — the who-uses data source."""
    refs: List[Tuple[str, str, str]] = []
    for node in (pattern.nodes or []):
        for slot, code in (getattr(node, "stages", None) or {}).items():
            if code:
                refs.append(("stage", code, f"{node.code}.stages[{slot!r}]"))
        for family, code in (getattr(node, "plugins", None) or {}).items():
            if code and family in TYPE_SLOT.values():
                refs.append(("executor", code, f"{node.code}.plugins[{family!r}]"))
    for slot, code in {s: c for e in normalize_skeleton(getattr(pattern, "stages", None))
                       for s, c in e.items()}.items():
        if code:
            refs.append(("stage", code, f"pattern.stages[{slot!r}]"))
    for family, code in (getattr(pattern, "plugins", None) or {}).items():
        if code and family in TYPE_SLOT.values():
            refs.append(("executor", code, f"pattern.plugins[{family!r}]"))
    return refs


def who_uses(kind: str, code: str) -> List[Tuple[str, List[str]]]:
    """Reverse index: [(pattern code, [reference sites…])] — directly declared references only."""
    warm_up()
    out = []
    for pattern in pattern_registry.list_patterns():
        wheres = [w for (k, c, w) in _declared_refs(pattern)
                  if (k, c) == (kind, code)]
        if wheres:
            out.append((pattern.code, wheres))
    return sorted(out)


# ---------------------------------------------------------------------------
# Exposure layer: subcommand rendering
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
        ptype = _pattern_type(pattern)
        nodes = []
        for node in (pattern.nodes or []):
            entry = {
                "code": node.code,
                "name": node.name,
                "sub_nodes": list(node.sub_nodes or []),
                "is_end": bool(getattr(node, "is_end", False)),
                "plugins": dict(getattr(node, "plugins", None) or {}),
            }
            if getattr(node, "use_tools", None):
                entry["use_tools"] = list(node.use_tools)
            if ptype == "agent":
                entry["executor"] = resolved_executor(node, pattern)
            nodes.append(entry)
        doc = {"code": pattern.code, "name": pattern.name,
               "pattern_type": ptype,
               "entry": pattern.entry_node_code,
               "allow_toolset": list(getattr(pattern, "allow_toolset", None) or []),
               "nodes": nodes}
        if ptype == "fsm":
            doc["fsm_executor"] = resolved_fsm_executor(pattern)
            doc["stages"] = resolved_stages(pattern)
        print(json.dumps(doc, ensure_ascii=False, indent=2))
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
