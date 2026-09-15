"""load_skill / read_skill_file（技能知识面，随技能启用自动授予）.

技能（skill）是数据资产：扫描根下的一个目录 + SKILL.md 手册 + 可选参考
文件（扫描与声明解析见 nexus/skills.py）。本模块只提供两个**只读知识面
工具**，让节点把已启用技能的手册按需装进上下文：

- ``load_skill(name)``：返回 SKILL.md 全文（头部附技能目录路径，手册里
  的相对路径据此解析）；
- ``read_skill_file(name, rel_path)``：读技能目录内的参考文件（resolve
  后必须落在该技能目录内，防路径穿越）。

授权（不走 toolset 三层收口，而是技能自己的双层 deny-by-default）::

    pattern:
      allow_skills: [archify]        # 技能池（空 = 无）
    node:
      use_skills: [archify]          # 本节点启用（空 = 无）

生效集 = use_skills ∩ allow_skills。default_loop 执行器在该集非空时把本
模块的两个 schema 自动追加进本轮工具列表（``SKILL_TOOL_SCHEMAS``），并经
``tool_call_context`` 发布 skills_dir / enabled_skills——handler 侧据此
拦截越权名（错误回填供模型自纠，同幻觉工具名语义）。因此声明方不需要
（也不应该）把 load_skill 写进 use_tools。

红线：**技能给知识不给权限**——手册指引下跑脚本/读写工作区文件仍走
bash / read_text 等执行面工具的既有授权；技能内容来自 operator 配置的
扫描根（与 base_prompt 同级信任，可进 system role），运行期绝不接受
路径入参（两个工具都只收技能名）。

脱离 agent loop 直接 dispatch（ambient 上下文为 None）时回退全局配置的
扫描根、允许任意已扫描技能——只读知识面，与 subagent_tool 的 detached
回退同姿态。
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from nexus.engine.tool_context import current_tool_context
from nexus.registry.tools import registry, tool_error
from nexus.skills import resolve_skills_dir, scan_skills

logger = logging.getLogger(__name__)

# SKILL.md 全文回填上限（手册是纪律本体，截断会让流程不可读，给足余量）
_MAX_SKILL_DOC_CHARS = 80000
# 技能目录内参考文件的单次回填上限
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

# default_loop 在生效技能集非空时追加进工具列表的形态（与
# registry.get_definitions 的包裹形状一致）
SKILL_TOOL_SCHEMAS = [
    {"type": "function", "function": LOAD_SKILL_SCHEMA},
    {"type": "function", "function": READ_SKILL_FILE_SCHEMA},
]


def _resolve_skill(name: str) -> Tuple[Optional[Any], Optional[str]]:
    """Ambient 上下文 → (SkillEntry, None) 或 (None, 错误回填串)。

    越权（不在 enabled_skills）与不存在（扫描根里没有）分别回填，均列出
    可选项供模型自纠。"""
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
# Self-registration（registered on module import; AST scan auto-discovery —
# 注意必须是顶层 registry.register() 调用表达式：扫描器只匹配 module body
# 的 Expr，for 循环体内的调用不可见）
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
