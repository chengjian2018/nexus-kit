"""Pattern validation — base-info completeness + plugin-declaration
resolvability + toolset authorization, collecting ALL errors before raising
one numbered ValueError (fail-fast timing: assembly/registration time, after
the tool/plugin registries are warmed).

Entry points:

- validate_base_info(pattern): code/name/entry non-empty, node codes unique
  (constructor already raised on the hard ones — this catches the soft
  remainder), unreachable-node warning, AGENT nodes must not declare stages
  (constructor raised on pattern-level stages; node-level stages are an
  AGENT declaration error caught here).
- validate_plugin_declarations(pattern): executor / stages / skeleton /
    messages_builder / agent_hooks codes resolve in the plugin registry
    (``llm`` resolves via settings at refresh time — not checked here).
- validate_tools(pattern): every node.use_tools name is registered AND its
  toolset ∈ pattern.allow_toolset (deny-by-default 三层收口, plan-⑧ §4).

Callers: host/cli assembly validates every registered pattern; yml loading
validates after construction. The structural graph checks (dangling
sub_nodes edges / duplicate node codes / entry resolvability / slots on
AGENT nodes) already run in Pattern.__init__ and are not duplicated here.
"""

import logging
from typing import List

from nexus.model.pattern import Pattern
from nexus.model.plugins_field import PLUGIN_KINDS, plugins_slot_label
from nexus.pipeline import normalize_skeleton
from nexus.registry.plugins import registry as plugin_registry

logger = logging.getLogger(__name__)


def _slot_codes_declared(stages) -> List[str]:
    """All non-None codes declared in a stages dict or skeleton."""
    codes = []
    if not stages:
        return codes
    if isinstance(stages, dict):
        return [v for v in stages.values() if v]
    for entry in stages:
        for code in entry.values():
            if code:
                codes.append(code)
    return codes


def validate_base_info(pattern: Pattern) -> List[str]:
    """Validate structural completeness; returns the collected error list
    (empty = valid). Missing display names log warnings but never raise."""
    errors: List[str] = []
    warnings: List[str] = []

    if not getattr(pattern, "code", None):
        errors.append("pattern.code 为空")
    if not getattr(pattern, "name", None):
        warnings.append(f"pattern {pattern.code!r} 缺少 name")

    if not getattr(pattern, "nodes", None):
        errors.append(f"pattern {pattern.code!r} 没有任何节点")
        return _finish(errors, warnings)

    if pattern.pattern_type == "agent":
        for node in pattern.nodes:
            if getattr(node, "stages", None):
                errors.append(
                    f"AGENT pattern 的节点 {node.code!r} 声明了 stages"
                    f"（stages 管线仅 FSM pattern 可用）")

    # Reachability from entry (warning only — a disconnected node is an
    # authoring smell, not a hard structural error)
    reachable = set()
    frontier = [pattern.entry_node_code]
    while frontier:
        cur = frontier.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        node = pattern.node_map.get(cur)
        if node is not None:
            frontier.extend(node.sub_nodes)
    for node in pattern.nodes:
        if node.code not in reachable:
            warnings.append(
                f"节点 {node.code!r} 从 entry 不可达（悬空声明？）")

    for node in pattern.nodes:
        if not getattr(node, "name", None):
            warnings.append(f"node {node.code!r} 缺少 name")

    return _finish(errors, warnings)


def _finish(errors: List[str], warnings: List[str]) -> List[str]:
    for w in warnings:
        logger.warning("[validation] %s（软警告）", w)
    return errors


def validate_plugin_declarations(pattern: Pattern) -> List[str]:
    """Validate that all declared plugin codes resolve; returns the error
    list (empty = valid)."""
    errors: List[str] = []
    pcode = getattr(pattern, "code", "?")

    # Pattern-level plugins dict (executor family slots report with their
    # executor_<family> label; "llm" resolves via settings — skipped here)
    for slot, declared in (getattr(pattern, "plugins", None) or {}).items():
        if not (isinstance(declared, str) and declared):
            continue
        kind = PLUGIN_KINDS.get(slot)
        if kind and not plugin_registry.has(kind, declared):
            errors.append(
                f"pattern {pcode!r} 的 "
                f"plugins[{plugins_slot_label(slot)}]={declared!r} 未注册"
                f"（kind={kind}）")

    # Node-level plugins dict (node layer over pattern layer)
    for node in pattern.nodes:
        for slot, declared in (getattr(node, "plugins", None) or {}).items():
            if not (isinstance(declared, str) and declared):
                continue
            kind = PLUGIN_KINDS.get(slot)
            if kind and not plugin_registry.has(kind, declared):
                errors.append(
                    f"node {node.code!r} 的 "
                    f"plugins[{plugins_slot_label(slot)}]={declared!r} 未注册"
                    f"（kind={kind}）")

    # FSM skeleton + per-node stages codes resolve & slots belong to the
    # skeleton; the unified pair (nlu/nlg sharing a code) is the only legal
    # duplicate
    if pattern.pattern_type == "fsm":
        skeleton_slot_names: List[str] = []
        try:
            skeleton = normalize_skeleton(getattr(pattern, "stages", None))
            skeleton_slot_names = [slot for entry in skeleton
                                   for slot in entry.keys()]
        except ValueError as e:
            errors.append(f"pattern {pcode!r} 骨架声明非法: {e}")
            skeleton = []

        for entry in skeleton:
            for slot, code in entry.items():
                if code and not plugin_registry.has("stage", code):
                    errors.append(
                        f"pattern {pcode!r} 骨架槽位 {slot} 声明的 {code!r} "
                        f"未注册（kind=stage）")

        for node in pattern.nodes:
            stages = getattr(node, "stages", None) or {}
            if not isinstance(stages, dict):
                errors.append(
                    f"node {node.code!r} 的 stages 必须是 dict: {stages!r}")
                continue
            for slot, code in stages.items():
                if skeleton_slot_names and slot not in skeleton_slot_names:
                    errors.append(
                        f"node {node.code!r} 的 stages 声明了骨架不存在的"
                        f"槽位 {slot!r}")
                if code and not plugin_registry.has("stage", code):
                    errors.append(
                        f"node {node.code!r} 的 stages[{slot}]={code!r} 未注册"
                        f"（kind=stage）")

        # Plugin-code uniqueness across the resolved slots (the nlu/nlg pair
        # sharing one code — the unified form — is the only legal duplicate)
        slots_by_code: dict = {}
        for entry in skeleton:
            for slot, code in entry.items():
                if code:
                    slots_by_code.setdefault(code, []).append(slot)
        for node in pattern.nodes:
            for slot, code in (getattr(node, "stages", None) or {}).items():
                if code:
                    slots_by_code.setdefault(code, []).append(slot)
        for code, slots in slots_by_code.items():
            # Repetition of one code within a SINGLE slot (the skeleton
            # value + node overrides of the same slot, e.g. the same
            # clarify stage declared on every node) is layering, not a
            # duplicate — runtime resolution handles it without warnings;
            # the error is one code serving several DISTINCT slots
            # (nlu/nlg sharing a code — the unified form — is exempt).
            if len(set(slots)) > 1 and set(slots) - {"nlu", "nlg"}:
                errors.append(
                    f"stage code {code!r} 在多个槽位声明（{sorted(set(slots))}；"
                    f"仅 nlu/nlg 同 code 的 unified 形态允许，其余为声明错误）")

    return errors


def _tool_check_findings(pattern: Pattern) -> List[str]:
    """Collect the tool-authorization findings (unregistered use_tools
    names / cross-toolset references) as message strings — shared by the
    strict path (errors) and the lenient path (notices/warnings)."""
    from nexus.registry.tools import registry as tool_registry

    findings: List[str] = []
    pcode = getattr(pattern, "code", "?")
    allowed_toolsets = set(getattr(pattern, "allow_toolset", None) or [])
    allows_mcp = any(ts.startswith("mcp-") for ts in allowed_toolsets)

    for node in pattern.nodes:
        for name in (getattr(node, "use_tools", None) or []):
            entry = tool_registry.get_entry(name) if name else None
            if entry is None:
                if allows_mcp:
                    # MCP timing exception: mcp tools register asynchronously
                    # — defer to runtime resolution, not a finding (details
                    # in validate_tools's docstring)
                    logger.warning(
                        "[validation] pattern %r 节点 %r 的 use_tools 声明了"
                        "未注册的工具 %r（pattern 允许 mcp-* 工具集，MCP 工具"
                        "启动后异步注册，留待运行期解析）",
                        pcode, node.code, name)
                    continue
                findings.append(
                    f"pattern {pcode!r} 节点 {node.code!r} 的 use_tools 声明了"
                    f"未注册的工具 {name!r}")
                continue
            if entry.toolset not in allowed_toolsets:
                findings.append(
                    f"pattern {pcode!r} 节点 {node.code!r} 的 use_tools 越集: "
                    f"{name!r}（toolset={entry.toolset!r}，"
                    f"allow_toolset={sorted(allowed_toolsets) or '空'}）")

    return findings


def tool_check_notices(pattern: Pattern) -> List[str]:
    """The lenient-path soft notices (same findings, phrased as warnings) —
    the display payload of generation-workbench previews: a use_tools
    reference to a not-yet-registered tool (e.g. freshly generated) or a
    missing toolset grant does not block apply; the tool stays unavailable
    at runtime until it is registered (deny-by-default resolution)."""
    return [msg + "（宽松放行：工具运行期不可用，注册后自动生效）"
            for msg in _tool_check_findings(pattern)]


def validate_tools(pattern: Pattern, strict: bool = True) -> List[str]:
    """Validate the toolset authorization (plan-⑧ §4): every node's
    use_tools names must be registered and belong to an allowed toolset.

    Runs at registration time (tools discovered before patterns); patterns
    validated in unit tests without registered tools get their use_tools
    flagged — the intended deny-by-default fail-fast.

    MCP timing exception: MCP-server tools register **asynchronously** (the
    connections complete after startup validation — host/main.py validates
    before ensure_started), so a declared name matching no registered tool
    cannot be statically verified while the pattern allows ``mcp-*``
    toolsets. Such names downgrade to a warning and defer to the runtime
    checks (``_resolve_tools`` intersection + the "not in this round's
    available set" hallucination guard), keeping the designed
    ``allow_toolset=["mcp-<server>"]`` + ``use_tools=[MCP tool name]``
    usage bootable.

    ``strict=False``（宽松模式，生成工作台装载路径）: 未注册/越集整体
    降级为警告日志并放行——新生成的 pattern 可能引用尚未注册的新工具，
    阻塞校验会让「生成 → 应用」必然失败；运行期仍由 deny-by-default
    解析兜底。放行清单经 tool_check_notices 取（预览展示用）。
    """
    findings = _tool_check_findings(pattern)
    if strict:
        return findings
    for msg in findings:
        logger.warning("[validation]（宽松模式放行）%s", msg)
    return []


def validate_pattern(pattern: Pattern, strict_tools: bool = True) -> None:
    """Full validation: base info + plugin declarations + toolset
    authorization; collects ALL errors then raises one numbered ValueError
    (empty list = valid, silent return).

    ``strict_tools=False``：工具面走宽松校验（见 validate_tools）——
    生成工作台（studio 生成/发布/应用/托管目录重放）允许引用后补注册
    的新工具；内置 pattern 装配与 CLI pattern-load 保持严格默认。
    """
    errors = (validate_base_info(pattern)
              + validate_plugin_declarations(pattern)
              + validate_tools(pattern, strict=strict_tools))
    if errors:
        numbered = "\n".join(f"  [{i + 1}] {e}" for i, e in enumerate(errors))
        raise ValueError(
            f"pattern {getattr(pattern, 'code', '?')!r} 校验失败"
            f"（共 {len(errors)} 项）:\n{numbered}"
        )
