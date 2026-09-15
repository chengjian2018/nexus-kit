"""Skill assets — directory scan + mtime fingerprint cache.

A skill is a **data asset**, not code: one directory under a scan root
carrying a ``SKILL.md`` (YAML frontmatter: name / description /
requires_toolsets + free ``metadata``), plus optional reference files the
manual points at. There is deliberately **no registry and no lifecycle** —
no register/deregister/reload: a directory dropped into the scan root is
usable on the next turn, same story as the llm-config mtime cache
(``nexus/settings.py``). Consumers:

- ``resolve_enabled_skills(node, pattern)`` — the declaration intersection
  (``node.use_skills ∩ pattern.allow_skills``) resolved against the scan
  (both deny-by-default; the model-layer analogue of the tool 三层收口).
- ``skill_prompt_block(node, pattern)`` — the L0 metadata block (one line
  per enabled skill) appended to the system prompt by the loop executor
  via ``extra_blocks``; the description is the trigger — the model cannot
  load what it cannot see.

Trust posture: scan roots come from operator configuration only (settings
``skills.dir``, an app ``skills.dir`` overlay, or pattern
``config.skills_dir`` — patterns are operator code, same trust tier as
base_prompt), so skill content may enter the
system role. Runtime never accepts user/channel-provided paths: the
load_skill tool takes a name, never a path.

Scan root resolution: app ``skills.dir`` > pattern ``config.skills_dir``
> settings ``skills.dir`` (default ``skills`` relative to the service
startup dir, ``~`` expanded) — the app yaml is the deployer's override,
the pattern declaration the code-level default (same precedence rule as
the loop budgets, design §5.4). A missing root is not an error — the whole
skill chain stays a no-op (mirror of ``mcp_servers: {}``).
"""

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# Default scan root when neither the pattern nor settings declares one
# (relative to the service startup directory, same semantics as the file
# tools' relative paths)
DEFAULT_SKILLS_DIR = "skills"


@dataclass(frozen=True)
class SkillEntry:
    """One scanned skill (immutable snapshot; cached per fingerprint)."""

    name: str                      # canonical = the directory name
    path: Path                     # the skill directory (absolute)
    description: str = ""
    requires_toolsets: Tuple[str, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fingerprint cache — resolved root -> ((fingerprint), {name: SkillEntry})
# ---------------------------------------------------------------------------

_CACHE: Dict[str, Tuple[Tuple[int, ...], Dict[str, SkillEntry]]] = {}
_CACHE_LOCK = threading.Lock()


def _absolutize(raw: str) -> Path:
    """Pin a declared root to an absolute path (相对根按服务启动目录解析，
    与 file 工具的相对路径语义一致；~ 展开）。"""
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (Path.cwd() / p).resolve()


def resolve_skills_dir(pattern: Any = None) -> Path:
    """The effective scan root: app ``skills.dir`` > pattern
    ``config.skills_dir`` > settings ``skills.dir`` > the ``skills``
    convention dir.

    A settings / app load failure (offline tests / bare CLI before config
    exists / a broken app file) degrades to the shallower layer instead of
    raising — skills are optional machinery and must never break a
    dialogue turn.
    """
    raw = ""
    pattern_code = (str(getattr(pattern, "code", "") or "")
                    if pattern is not None else "")
    if pattern_code:
        try:
            from nexus.settings import _get_app_config
            app = _get_app_config(pattern_code)
            if app:
                raw = str((app.get("skills") or {}).get("dir") or "").strip()
        except Exception as e:  # noqa: BLE001 — degrade, never break the turn
            logger.debug("app skills.dir 读取失败，回退更浅层: %s", e)
    if not raw and pattern is not None:
        raw = str((getattr(pattern, "config", None) or {})
                  .get("skills_dir") or "").strip()
    if not raw:
        try:
            from nexus.settings import get_skills_config
            raw = str(get_skills_config().get("dir") or "").strip()
        except Exception as e:  # noqa: BLE001 — config absent is a normal state
            logger.debug("skills.dir 读取配置失败，回退默认根: %s", e)
    if not raw:
        raw = DEFAULT_SKILLS_DIR
    return _absolutize(raw)


def _parse_frontmatter(text: str) -> Dict[str, Any]:
    """Leading ``---`` YAML frontmatter of a SKILL.md → dict ({} when absent
    or unparseable — a broken frontmatter never kills the scan)."""
    if not text.startswith("---"):
        return {}
    try:
        end = text.index("\n---", 3)
    except ValueError:
        return {}
    block = text[3:end]
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as e:
        logger.warning("[skills] SKILL.md frontmatter 解析失败（忽略）: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _scan_uncached(root: Path) -> Dict[str, SkillEntry]:
    """One directory scan: each direct subdirectory with a SKILL.md is a
    skill; the directory name is the canonical name (frontmatter ``name``
    that disagrees is reported once). Directories without SKILL.md are
    skipped with a warning."""
    entries: Dict[str, SkillEntry] = {}
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        doc = path / "SKILL.md"
        if not doc.is_file():
            logger.warning(
                "[skills] 目录 %s 缺少 SKILL.md，跳过（不构成技能）", path)
            continue
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("[skills] SKILL.md 读取失败，跳过 %s: %s", doc, e)
            continue
        fm = _parse_frontmatter(text)
        fm_name = str(fm.get("name") or "").strip()
        if fm_name and fm_name != path.name:
            logger.warning(
                "[skills] %s frontmatter name=%r 与目录名不一致，"
                "以目录名 %r 为准", doc, fm_name, path.name)
        requires = fm.get("requires_toolsets")
        if not isinstance(requires, (list, tuple)):
            requires = []
        entries[path.name] = SkillEntry(
            name=path.name,
            path=path.resolve(),
            description=str(fm.get("description") or "").strip(),
            requires_toolsets=tuple(str(t) for t in requires if str(t)),
            metadata=dict(fm.get("metadata") or {})
            if isinstance(fm.get("metadata"), dict) else {},
        )
    return entries


def _fingerprint(root: Path) -> Tuple[Tuple[int, ...], ...]:
    """Root identity = the root dir stat + every ``*/SKILL.md`` stat. A
    content edit inside a skill dir does not move the root's mtime, so the
    SKILL.md entries are fingerprinted individually; reference files are
    NOT tracked (only SKILL.md feeds the prompt, and read_skill_file reads
    references live anyway)."""
    marks: List[Tuple[int, ...]] = []
    try:
        st = root.stat()
        marks.append((st.st_mtime_ns, st.st_size))
        for doc in sorted(root.glob("*/SKILL.md")):
            st = doc.stat()
            marks.append((str(doc.parent.name), st.st_mtime_ns, st.st_size))
    except OSError:
        return ()
    return tuple(marks)


def scan_skills(root: Any) -> Dict[str, SkillEntry]:
    """Scan one root (absolutized) into ``{name: SkillEntry}``, cached by
    the directory fingerprint — repeated calls per turn cost one stat walk
    (a handful of stats), and a dropped-in skill folder is live on the next
    call with no invalidation API."""
    root_path = _absolutize(str(root))
    fp = _fingerprint(root_path)
    resolved = str(root_path)
    with _CACHE_LOCK:
        cached = _CACHE.get(resolved)
        if cached is not None and cached[0] == fp:
            return cached[1]
    entries: Dict[str, SkillEntry] = {}
    if root_path.is_dir():
        entries = _scan_uncached(root_path)
    else:
        logger.debug("[skills] 扫描根不存在（技能链整体静默）: %s", root_path)
    with _CACHE_LOCK:
        _CACHE[resolved] = (fp, entries)
    return entries


def invalidate_skills_cache() -> None:
    """Drop the scan cache (tests between same-fingerprint writes; the
    fingerprint handles real edits on its own)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Declaration intersection (the skill 双层 deny-by-default)
# ---------------------------------------------------------------------------

def resolve_enabled_skills(node: Any, pattern: Any = None
                           ) -> Dict[str, SkillEntry]:
    """``node.use_skills ∩ pattern.allow_skills`` resolved against the scan.

    - node.use_skills empty = no skills on this node (deny-by-default)
    - pattern.allow_skills empty = no skills in the whole pattern
    - a declared name that is not in the intersection logs like the tool
      resolution's "declared-but-unavailable" warning; a name in the
      intersection but missing on disk warns and drops (the load_skill
      tool backfills the error for model self-correction at runtime)
    """
    node_code = getattr(node, "code", "") if node is not None else ""
    use = set(getattr(node, "use_skills", None) or [])
    if not use:
        return {}
    allowed = set(getattr(pattern, "allow_skills", None) or []) \
        if pattern is not None else set()
    if not allowed:
        logger.info(
            "节点 '%s' 声明了 use_skills 但 pattern.allow_skills 为空"
            "（无可用技能）", node_code)
        return {}
    names = use & allowed
    dropped = use - names
    if dropped:
        logger.warning(
            "节点 '%s' 声明的技能不在 pattern.allow_skills 内（越权声明）: %s",
            node_code, sorted(dropped))

    entries = scan_skills(resolve_skills_dir(pattern))
    enabled: Dict[str, SkillEntry] = {}
    for name in sorted(names):
        entry = entries.get(name)
        if entry is None:
            logger.warning(
                "节点 '%s' 启用的技能 '%s' 在扫描根中不存在"
                "（运行期 load_skill 会报错回填）", node_code, name)
            continue
        enabled[name] = entry
    return enabled


# ---------------------------------------------------------------------------
# L0 metadata block (the trigger: the model cannot load what it cannot see)
# ---------------------------------------------------------------------------

def skill_prompt_block(node: Any, pattern: Any = None) -> Optional[str]:
    """The per-node skill metadata block, or None when no skills enabled
    (zero cost for nodes without declarations).

    This block rides the loop executor's ``extra_blocks`` into the system
    prompt — the MessagesBuilder contract requires including extra_blocks,
    so custom builders carry skills without knowing about them.
    """
    entries = resolve_enabled_skills(node, pattern)
    if not entries:
        return None
    lines = [
        "## 可用技能",
        "以下技能已授权给本节点。需要时先用 load_skill 工具装载技能手册，"
        "再严格按手册的流程与纪律执行；手册引用的技能目录内参考文件可用 "
        "read_skill_file 读取（手册会给出技能目录路径）。",
    ]
    for name, entry in entries.items():
        desc = entry.description or "（无描述）"
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)
