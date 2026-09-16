"""Offline tests for the skill asset mechanism (nexus/skills.py +
atoms/tools/skill_tool.py).

Covers:
1. Directory scan: frontmatter parsing (description / requires_toolsets /
   metadata), directories missing SKILL.md skipped, frontmatter name vs
   directory name mismatch resolved by the directory name
2. mtime fingerprint cache: same fingerprint returns the same snapshot;
   rescan after SKILL.md content/mtime changes; missing root = silent
   empty result
3. Two-layer deny-by-default: use_skills ∩ allow_skills; unauthorized and
   missing entries warn and degrade
4. L0 metadata block: contains name and description; None when no skills
5. validate_skills: missing name / unauthorized use_skills / unauthorized
   requires_toolsets → strict raises; lenient passes; missing scan root
   deferred entirely
6. YAML round-trip: allow_skills / use_skills / config.skills_dir preserved
7. Skill tools: enabled-set blocks unauthorized names, manual full text +
   directory header, reference file reads, path traversal rejected,
   detached calls fall back to the global root, registration shape
8. settings skills section: default = the conventional skills directory,
   explicit config wins
"""

import os

import pytest

from async_utils import arun
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.skills import (
    SkillEntry,
    invalidate_skills_cache,
    resolve_enabled_skills,
    resolve_skills_dir,
    scan_skills,
    skill_prompt_block,
)


@pytest.fixture()
def skill_root(tmp_path):
    """Two skills + one directory missing SKILL.md. alpha declares requires_toolsets."""
    root = tmp_path / "root"
    alpha = root / "alpha"
    (alpha / "references").mkdir(parents=True)
    (alpha / "SKILL.md").write_text(
        "---\n"
        "name: alpha-alias\n"
        "description: alpha 技能，用于测试手册装载\n"
        "requires_toolsets: [filesystem]\n"
        "metadata:\n"
        "  version: '1.0'\n"
        "---\n"
        "# Alpha 手册\n\n按步骤执行。\n",
        encoding="utf-8")
    (alpha / "references" / "guide.md").write_text(
        "# 参考指南\n正文内容", encoding="utf-8")
    beta = root / "beta"
    beta.mkdir(parents=True)
    (beta / "SKILL.md").write_text(
        "---\ndescription: beta 技能，无执行面依赖\n---\nBeta 手册正文\n",
        encoding="utf-8")
    (root / "hollow").mkdir(parents=True)  # missing SKILL.md -> skipped
    invalidate_skills_cache()
    return root


def make_pattern(skill_root, allow_skills=("alpha", "beta"),
                 allow_toolset=("filesystem",), node=None):
    return Pattern(
        code="p", name="p", description="test",
        allow_toolset=list(allow_toolset),
        allow_skills=list(allow_skills),
        config={"skills_dir": str(skill_root)},
        nodes=[node] if node is not None else None)


def make_node(use_skills=None, use_tools=None):
    return BaseNode(code="n", use_skills=use_skills, use_tools=use_tools)


# ============================================================================
# 1-2. Scan + fingerprint cache
# ============================================================================

def test_scan_parses_frontmatter_and_skips_hollow(skill_root):
    entries = scan_skills(skill_root)
    assert sorted(entries) == ["alpha", "beta"]
    alpha = entries["alpha"]
    assert isinstance(alpha, SkillEntry)
    assert alpha.name == "alpha"  # directory name wins (frontmatter says alpha-alias)
    assert "alpha 技能" in alpha.description
    assert alpha.requires_toolsets == ("filesystem",)
    assert alpha.metadata.get("version") == "1.0"
    assert entries["beta"].requires_toolsets == ()
    assert alpha.path.is_absolute()


def test_scan_cached_by_fingerprint(skill_root):
    first = scan_skills(skill_root)
    second = scan_skills(skill_root)
    assert first is second  # same fingerprint -> same snapshot (zero rescan)

    # content changed (size + mtime changed) -> rescan
    doc = skill_root / "beta" / "SKILL.md"
    doc.write_text("---\ndescription: beta 更新了\n---\n新正文\n",
                   encoding="utf-8")
    os.utime(doc, (doc.stat().st_atime, doc.stat().st_mtime + 5))
    third = scan_skills(skill_root)
    assert third is not first
    assert third["beta"].description == "beta 更新了"
    assert third["alpha"] == first["alpha"]  # the unchanged skill's snapshot value stays the same


def test_scan_missing_root_is_silent(tmp_path):
    invalidate_skills_cache()
    assert scan_skills(tmp_path / "nope") == {}


# ============================================================================
# 3-4. Two-layer resolution + L0 metadata block
# ============================================================================

def test_enabled_intersection_and_warnings(skill_root):
    pattern = make_pattern(skill_root)
    # empty declaration = no skills
    assert resolve_enabled_skills(make_node(), pattern) == {}
    # empty allow_skills = none at all
    empty_pool = make_pattern(skill_root, allow_skills=())
    assert resolve_enabled_skills(make_node(["alpha"]), empty_pool) == {}
    # intersection takes effect
    enabled = resolve_enabled_skills(make_node(["alpha", "beta"]), pattern)
    assert sorted(enabled) == ["alpha", "beta"]
    # unauthorized entries dropped (only the intersection remains) + missing ones warn and degrade
    partial = make_pattern(skill_root, allow_skills=["alpha", "ghost"])
    enabled = resolve_enabled_skills(make_node(["alpha", "beta", "ghost"]),
                                     partial)
    assert list(enabled) == ["alpha"]


def test_prompt_block_contains_metadata(skill_root):
    pattern = make_pattern(skill_root)
    assert skill_prompt_block(make_node(), pattern) is None  # zero cost without a declaration
    block = skill_prompt_block(make_node(["alpha"]), pattern)
    assert "可用技能" in block
    assert "alpha" in block and "load_skill" in block
    assert "alpha 技能" in block
    assert "read_skill_file" in block


def test_resolve_skills_dir_pattern_overrides_global(skill_root):
    pattern = make_pattern(skill_root)
    assert resolve_skills_dir(pattern) == skill_root.resolve()
    # no override -> global config (falls back to the default conventional skills dir when no config is found)
    fallback = resolve_skills_dir(None)
    assert fallback.is_absolute()


# ============================================================================
# 5. validate_skills
# ============================================================================

def test_validate_skills_strict_findings(skill_root):
    from nexus.model.validation import validate_skills

    # ghost name -> error
    pattern = make_pattern(skill_root, allow_skills=["alpha", "ghost"])
    assert any("ghost" in f for f in validate_skills(pattern))

    # unauthorized use_skills -> error
    pattern = make_pattern(skill_root, allow_skills=["alpha"],
                           node=make_node(["beta"]))
    assert any("越权" in f and "beta" in f for f in validate_skills(pattern))

    # unauthorized requires_toolsets -> error (alpha needs filesystem, toolsets granted empty)
    pattern = make_pattern(skill_root, allow_skills=["alpha"],
                           allow_toolset=(), node=make_node(["alpha"]))
    assert any("filesystem" in f for f in validate_skills(pattern))

    # all compliant -> zero findings
    pattern = make_pattern(skill_root, node=make_node(["alpha"]))
    assert validate_skills(pattern) == []


def test_validate_skills_lenient_and_missing_root(skill_root):
    from nexus.model.validation import validate_skills

    pattern = make_pattern(skill_root, allow_skills=["ghost"])
    assert validate_skills(pattern, strict=False) == []  # lenient passes

    pattern = make_pattern(skill_root, allow_skills=["ghost"])
    pattern.config["skills_dir"] = str(skill_root.parent / "nope")
    assert validate_skills(pattern) == []  # root missing -> deferred to runtime


def test_validate_pattern_integrates_skills(skill_root):
    from nexus.model.validation import validate_pattern

    pattern = make_pattern(skill_root, allow_skills=["ghost"],
                           node=make_node(["ghost"]))
    with pytest.raises(ValueError, match="ghost"):
        validate_pattern(pattern)


# ============================================================================
# 6. YAML round-trip
# ============================================================================

def test_yaml_roundtrip_preserves_skill_fields(skill_root):
    from nexus.model.serialization import pattern_from_dict, pattern_to_dict

    pattern = make_pattern(skill_root, node=make_node(["alpha"]))

    data = pattern_to_dict(pattern)
    assert data["allow_skills"] == ["alpha", "beta"]
    assert data["nodes"][0]["use_skills"] == ["alpha"]
    assert data["config"]["skills_dir"] == str(skill_root)

    rebuilt = pattern_from_dict(data)
    assert rebuilt.allow_skills == ["alpha", "beta"]
    assert rebuilt.nodes[0].use_skills == ["alpha"]
    assert rebuilt.config["skills_dir"] == str(skill_root)


# ============================================================================
# 7. Skill tools (handler semantics + registration shape)
# ============================================================================

def test_skill_tools_registered():
    from nexus.registry.tools import registry as tool_registry

    for name in ("load_skill", "read_skill_file"):
        entry = tool_registry.get_entry(name)
        assert entry is not None, f"{name} 未注册"
        assert entry.toolset == "skills"

    from atoms.tools.skill_tool import SKILL_TOOL_SCHEMAS
    names = {s["function"]["name"] for s in SKILL_TOOL_SCHEMAS}
    assert names == {"load_skill", "read_skill_file"}


def test_load_skill_enabled_set_and_manual(skill_root):
    from atoms.tools import skill_tool
    from nexus.engine.tool_context import tool_call_context

    # the handler is a sync function (bridged via to_thread on the registry side); call it directly
    with tool_call_context(
            {}, [], session_id="s", skills_dir=str(skill_root),
            enabled_skills=frozenset({"alpha", "stale"})):
        ok = skill_tool._handle_load_skill({"name": "alpha"})
        denied = skill_tool._handle_load_skill({"name": "beta"})
        stale = skill_tool._handle_load_skill({"name": "stale"})
        blank = skill_tool._handle_load_skill({"name": ""})

    assert "[skill: alpha]" in ok
    assert str(skill_root / "alpha") in ok  # directory header (base for manual relative paths)
    assert "Alpha 手册" in ok
    # disabled names always get "unauthorized" (scan content is not leaked to
    # unauthorized nodes), listing the authorized ones
    assert "未授权给本节点" in denied and "alpha" in denied
    # enabled but missing on disk -> "not found" (error backfill lets the model self-correct)
    assert "不存在" in stale and "alpha" in stale
    assert "name 必填" in blank


def test_read_skill_file_and_traversal_guard(skill_root):
    from atoms.tools import skill_tool
    from nexus.engine.tool_context import tool_call_context

    with tool_call_context(
            {}, [], session_id="s", skills_dir=str(skill_root),
            enabled_skills=frozenset({"alpha"})):
        ok = skill_tool._handle_read_skill_file(
            {"name": "alpha", "rel_path": "references/guide.md"})
        escape = skill_tool._handle_read_skill_file(
            {"name": "alpha", "rel_path": "../../escape.txt"})
        missing = skill_tool._handle_read_skill_file(
            {"name": "alpha", "rel_path": "references/nope.md"})

    assert "参考指南" in ok
    assert "越出技能目录" in escape
    assert "不存在" in missing


def test_detached_call_falls_back_to_global_root(skill_root, monkeypatch):
    """Dispatching directly outside the agent loop: falls back to the global
    root of resolve_skills_dir(None) (monkeypatched to tmp), allowing any
    scanned skill (read-only knowledge surface)."""
    from atoms.tools import skill_tool

    monkeypatch.setattr(skill_tool, "resolve_skills_dir",
                        lambda pattern=None: skill_root)
    result = skill_tool._handle_load_skill({"name": "beta"})
    assert "Beta 手册正文" in result


# ============================================================================
# 8. settings skills section
# ============================================================================

def test_settings_skills_node(tmp_path):
    from nexus.settings import load_config

    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(
        "llm_default:\n  code: x\n  model: m\n"
        "skills:\n  dir: my-skills\n", encoding="utf-8")
    assert load_config(str(cfg_file))["skills"] == {"dir": "my-skills"}

    default_file = tmp_path / "default.yaml"
    default_file.write_text(
        "llm_default:\n  code: x\n  model: m\n", encoding="utf-8")
    assert load_config(str(default_file))["skills"] == {"dir": "skills"}
