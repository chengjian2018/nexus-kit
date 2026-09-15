"""技能资产机制离线测试（nexus/skills.py + atoms/tools/skill_tool.py）。

覆盖:
1. 目录扫描:frontmatter 解析（description / requires_toolsets / metadata）、
   缺 SKILL.md 的目录跳过、frontmatter name 与目录名不一致以目录名为准
2. mtime 指纹缓存:同指纹返回同一份结果;SKILL.md 内容/时间戳变更后重扫;
   根不存在 = 空结果静默
3. 双层 deny-by-default:use_skills ∩ allow_skills;越权与缺失告警降级
4. L0 元数据块:含名称与描述;无技能时 None
5. validate_skills:缺失名/越权 use_skills/requires_toolsets 未授权 →
   strict 报错;lenient 放行;扫描根不存在整体延后
6. YAML round-trip:allow_skills / use_skills / config.skills_dir 保真
7. skill 工具:enabled 集拦截越权名、手册全文+目录头、参考文件读取、
   路径穿越拒绝、detached 调用回退全局根、注册表形态
8. settings skills 节:缺省 = skills 约定目录,显式配置直达
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
    """两个技能 + 一个缺 SKILL.md 的目录。alpha 声明 requires_toolsets。"""
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
    (root / "hollow").mkdir(parents=True)  # 缺 SKILL.md → 跳过
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
# 1-2. 扫描 + 指纹缓存
# ============================================================================

def test_scan_parses_frontmatter_and_skips_hollow(skill_root):
    entries = scan_skills(skill_root)
    assert sorted(entries) == ["alpha", "beta"]
    alpha = entries["alpha"]
    assert isinstance(alpha, SkillEntry)
    assert alpha.name == "alpha"  # 目录名为准（frontmatter 写的是 alpha-alias）
    assert "alpha 技能" in alpha.description
    assert alpha.requires_toolsets == ("filesystem",)
    assert alpha.metadata.get("version") == "1.0"
    assert entries["beta"].requires_toolsets == ()
    assert alpha.path.is_absolute()


def test_scan_cached_by_fingerprint(skill_root):
    first = scan_skills(skill_root)
    second = scan_skills(skill_root)
    assert first is second  # 同指纹 → 同一快照（零重扫）

    # 内容变更（size + mtime 变化）→ 重扫
    doc = skill_root / "beta" / "SKILL.md"
    doc.write_text("---\ndescription: beta 更新了\n---\n新正文\n",
                   encoding="utf-8")
    os.utime(doc, (doc.stat().st_atime, doc.stat().st_mtime + 5))
    third = scan_skills(skill_root)
    assert third is not first
    assert third["beta"].description == "beta 更新了"
    assert third["alpha"] == first["alpha"]  # 未变更的技能快照值不变


def test_scan_missing_root_is_silent(tmp_path):
    invalidate_skills_cache()
    assert scan_skills(tmp_path / "nope") == {}


# ============================================================================
# 3-4. 双层解析 + L0 元数据块
# ============================================================================

def test_enabled_intersection_and_warnings(skill_root):
    pattern = make_pattern(skill_root)
    # 空声明 = 无技能
    assert resolve_enabled_skills(make_node(), pattern) == {}
    # allow_skills 空 = 整体无
    empty_pool = make_pattern(skill_root, allow_skills=())
    assert resolve_enabled_skills(make_node(["alpha"]), empty_pool) == {}
    # 交集生效
    enabled = resolve_enabled_skills(make_node(["alpha", "beta"]), pattern)
    assert sorted(enabled) == ["alpha", "beta"]
    # 越权项丢弃（只剩交集）+ 缺失项告警降级
    partial = make_pattern(skill_root, allow_skills=["alpha", "ghost"])
    enabled = resolve_enabled_skills(make_node(["alpha", "beta", "ghost"]),
                                     partial)
    assert list(enabled) == ["alpha"]


def test_prompt_block_contains_metadata(skill_root):
    pattern = make_pattern(skill_root)
    assert skill_prompt_block(make_node(), pattern) is None  # 无声明零成本
    block = skill_prompt_block(make_node(["alpha"]), pattern)
    assert "可用技能" in block
    assert "alpha" in block and "load_skill" in block
    assert "alpha 技能" in block
    assert "read_skill_file" in block


def test_resolve_skills_dir_pattern_overrides_global(skill_root):
    pattern = make_pattern(skill_root)
    assert resolve_skills_dir(pattern) == skill_root.resolve()
    # 无覆盖 → 走全局配置（读不到配置回退默认 skills 约定目录）
    fallback = resolve_skills_dir(None)
    assert fallback.is_absolute()


# ============================================================================
# 5. validate_skills
# ============================================================================

def test_validate_skills_strict_findings(skill_root):
    from nexus.model.validation import validate_skills

    # 幽灵名 → 报错
    pattern = make_pattern(skill_root, allow_skills=["alpha", "ghost"])
    assert any("ghost" in f for f in validate_skills(pattern))

    # 越权 use_skills → 报错
    pattern = make_pattern(skill_root, allow_skills=["alpha"],
                           node=make_node(["beta"]))
    assert any("越权" in f and "beta" in f for f in validate_skills(pattern))

    # requires_toolsets 未授权 → 报错（alpha 要 filesystem，授空）
    pattern = make_pattern(skill_root, allow_skills=["alpha"],
                           allow_toolset=(), node=make_node(["alpha"]))
    assert any("filesystem" in f for f in validate_skills(pattern))

    # 全部合规 → 零 findings
    pattern = make_pattern(skill_root, node=make_node(["alpha"]))
    assert validate_skills(pattern) == []


def test_validate_skills_lenient_and_missing_root(skill_root):
    from nexus.model.validation import validate_skills

    pattern = make_pattern(skill_root, allow_skills=["ghost"])
    assert validate_skills(pattern, strict=False) == []  # 宽松放行

    pattern = make_pattern(skill_root, allow_skills=["ghost"])
    pattern.config["skills_dir"] = str(skill_root.parent / "nope")
    assert validate_skills(pattern) == []  # 根不存在 → 延后运行期


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
# 7. skill 工具（handler 语义 + 注册形态）
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

    # handler 是同步函数（registry 侧经 to_thread 桥接），直调即可
    with tool_call_context(
            {}, [], session_id="s", skills_dir=str(skill_root),
            enabled_skills=frozenset({"alpha", "stale"})):
        ok = skill_tool._handle_load_skill({"name": "alpha"})
        denied = skill_tool._handle_load_skill({"name": "beta"})
        stale = skill_tool._handle_load_skill({"name": "stale"})
        blank = skill_tool._handle_load_skill({"name": ""})

    assert "[skill: alpha]" in ok
    assert str(skill_root / "alpha") in ok  # 目录头（手册相对路径的基准）
    assert "Alpha 手册" in ok
    # 未启用名一律"未授权"（不向未授权节点泄露扫描内容）,列出已授权项
    assert "未授权给本节点" in denied and "alpha" in denied
    # 已启用但磁盘缺失 → "不存在"（错误回填供模型自纠）
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
    """脱离 agent loop 直接 dispatch：回退 resolve_skills_dir(None) 的全局
    根（monkeypatch 钉到 tmp），允许任意已扫描技能（只读知识面）。"""
    from atoms.tools import skill_tool

    monkeypatch.setattr(skill_tool, "resolve_skills_dir",
                        lambda pattern=None: skill_root)
    result = skill_tool._handle_load_skill({"name": "beta"})
    assert "Beta 手册正文" in result


# ============================================================================
# 8. settings skills 节
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
