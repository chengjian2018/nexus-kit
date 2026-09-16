"""load_skill / read_skill_file (the skill knowledge surface, auto-granted
with skill enablement).

A skill is a data asset: one directory under the scan root + a SKILL.md
manual + optional reference files (scanning and declaration resolution in
nexus/skills.py). This module provides only two **read-only knowledge
tools**, letting a node pull an enabled skill's manual into context on
demand:

- ``load_skill(name)``: returns the SKILL.md full text (the header carries
  the skill directory path — relative paths referenced by the manual
  resolve against it);
- ``read_skill_file(name, rel_path)``: reads a reference file inside the
  skill directory (after resolve it must land inside that skill directory —
  path-traversal guard).

Authorization (NOT the toolset three-layer gate but the skill's own
two-layer deny-by-default)::

    pattern:
      allow_skills: [archify]        # the skill pool (empty = none)
    node:
      use_skills: [archify]          # enabled on this node (empty = none)

Effective set = use_skills ∩ allow_skills. When the set is non-empty the
default_loop executor appends this module's two schemas to the round's tool
list automatically (``SKILL_TOOL_SCHEMAS``) and publishes skills_dir /
enabled_skills via ``tool_call_context`` — the handler side rejects
out-of-pool names on that basis (error backfill for model self-correction,
same semantics as hallucinated tool names). Declarers therefore need not
(and should not) write load_skill into use_tools.

Red line: **skills grant knowledge, not permissions** — running scripts /
reading-writing workspace files under manual guidance still goes through
the existing authorization of execution-surface tools (bash / read_text
etc.); skill content comes from the operator-configured scan root (trusted
at the same level as base_prompt, may enter the system role), and at
runtime the tools never accept path arguments (both take a skill name
only).

Direct dispatch outside the agent loop (ambient context None) falls back to
the globally configured scan root and allows any scanned skill — a
read-only knowledge surface, the same stance as subagent_tool's detached
fallback.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from nexus.engine.tool_context import current_tool_context
from nexus.registry.tools import registry, tool_error
from nexus.skills import resolve_skills_dir, scan_skills

logger = logging.getLogger(__name__)

# SKILL.md full-text backfill cap (the manual IS the discipline — truncation
# would make the process unreadable, so give it ample headroom)
_MAX_SKILL_DOC_CHARS = 80000
# Per-read backfill cap for reference files inside the skill directory
_MAX_SKILL_FILE_CHARS = 20000


LOAD_SKILL_SCHEMA = {
    "name": "load_skill",
    "description": (
        "装载一个已授权技能的完整手册（SKILL.md 全文）。返回头部附技能"
        "目录路径，手册引用的相对路径均以该目录为基准。需要按技能纪律"
        "干活时先调用本工具，再严格按手册执行。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名（本节点已启用的技能之一）"},
        },
        "required": ["name"],
    },
}

READ_SKILL_FILE_SCHEMA = {
    "name": "read_skill_file",
    "description": (
        "读取技能目录内的参考文件（手册指明要看某个 reference/schema/"
        "example 时用）。rel_path 相对技能目录解析，越出技能目录即拒绝。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名"},
            "rel_path": {"type": "string", "description": "技能目录内的相对路径，如 schemas/workflow.schema.json"},
        },
        "required": ["name", "rel_path"],
    },
}

# The shape default_loop appends to the tool list when the effective skill
# set is non-empty (same wrapper shape as registry.get_definitions)
SKILL_TOOL_SCHEMAS = [
    {"type": "function", "function": LOAD_SKILL_SCHEMA},
    {"type": "function", "function": READ_SKILL_FILE_SCHEMA},
]


def _resolve_skill(name: str) -> Tuple[Optional[Any], Optional[str]]:
    """Ambient context → (SkillEntry, None) or (None, error backfill string).

    Out-of-pool (not in enabled_skills) and not-found (absent from the scan
    root) get separate backfills, each listing the options for model
    self-correction."""
    if not str(name or "").strip():
        return None, tool_error("name 必填：要装载的技能名")
    name = str(name).strip()

    ambient = current_tool_context()
    root = ""
    enabled = None
    if ambient is not None:
        root = ambient.skills_dir or ""
        enabled = ambient.enabled_skills
    if not root:
        root = str(resolve_skills_dir(None))

    if enabled is not None and name not in enabled:
        return None, tool_error(
            f"技能 '{name}' 未授权给本节点。已授权：{sorted(enabled)}。"
            f"请从已授权技能中选择。")

    entries = scan_skills(Path(root))
    entry = entries.get(name)
    if entry is None:
        return None, tool_error(
            f"技能 '{name}' 不存在。可用技能：{sorted(entries) or '（无）'}。")
    return entry, None


def _handle_load_skill(args: Dict[str, Any]) -> str:
    entry, err = _resolve_skill(args.get("name"))
    if err:
        return err
    try:
        text = (entry.path / "SKILL.md").read_text(
            encoding="utf-8", errors="replace")
    except OSError as e:
        return tool_error(f"SKILL.md 读取失败: {e}")
    truncated = ""
    if len(text) > _MAX_SKILL_DOC_CHARS:
        text = text[:_MAX_SKILL_DOC_CHARS]
        truncated = "\n\n[手册超长已截断]"
    header = (f"[skill: {entry.name}] 技能目录: {entry.path}"
              + (f"（需要 toolset: {', '.join(entry.requires_toolsets)}）"
                 if entry.requires_toolsets else ""))
    return f"{header}\n\n{text}{truncated}"


def _handle_read_skill_file(args: Dict[str, Any]) -> str:
    entry, err = _resolve_skill(args.get("name"))
    if err:
        return err
    rel = str(args.get("rel_path") or "").strip()
    if not rel:
        return tool_error("rel_path 必填：技能目录内的相对路径")
    base = entry.path.resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        return tool_error(
            f"rel_path 越出技能目录（拒绝）: {rel}（技能目录: {base}）")
    if not target.is_file():
        return tool_error(f"文件不存在: {target}")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return tool_error(f"文件读取失败: {e}")
    if len(text) > _MAX_SKILL_FILE_CHARS:
        text = text[:_MAX_SKILL_FILE_CHARS] + "\n\n[超长已截断]"
    return text


# ---------------------------------------------------------------------------
# Self-registration (registered on module import; AST scan auto-discovery —
# note this must be a top-level registry.register() call expression: the
# scanner only matches module-body Exprs; calls inside for loops are
# invisible)
# ---------------------------------------------------------------------------

registry.register(
    name="load_skill",
    toolset="skills",
    schema=LOAD_SKILL_SCHEMA,
    handler=_handle_load_skill,
    description="装载已启用技能的完整手册（SKILL.md）",
    emoji="📘",
)

registry.register(
    name="read_skill_file",
    toolset="skills",
    schema=READ_SKILL_FILE_SCHEMA,
    handler=_handle_read_skill_file,
    description="读技能目录内的参考文件（防穿越）",
    emoji="📄",
)
