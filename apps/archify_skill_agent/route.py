"""archify_skill pattern — a minimal declarative recipe for manual-style
skill operation.

A single-node AGENT graph (pattern code "archify_skill"), **zero custom
executors**: the node runs on default_loop, and two declaration layers
wire the archify skill in —

- Skill grants (deny-by-default two layers, resolved by nexus/skills.py):
  ``allow_skills=["archify"]`` × ``use_skills=["archify"]``. When the
  effective set is non-empty, default_loop automatically gains the two
  read-only knowledge tools load_skill / read_skill_file, and skill
  metadata is injected into the system prompt (the description is the
  trigger);
- The skill's execution surface (running the archify CLI, reading/writing
  candidates and artifacts) still goes through the three-layer tool
  narrowing: ``allow_toolset=["shell", "filesystem"]`` × ``use_tools``
  (bash + the five file tools).

Independent of apps/archify_agent (pattern "archify", the nine-node
workflow version): that one **compiles** SKILL.md's acceptance discipline
into graph gates and deterministic receipt stations, while this one leaves
the discipline to the manual itself and the node only loads and executes —
two consumption tiers of the same skill.
"""

from apps.archify_skill_agent.prompts import AS_BASE_PROMPT
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

as_work = BaseNode(
    code="as_work",
    name="图表工程·技能节点",
    description=(
        "装载 archify 技能手册并按其纪律完成一次图表交付：类型选型 → "
        "读 schema 与示例 → 产物优先写候选 → validate showcase → "
        "deliver → 诚实汇报；工作区 data/archify_skill/，CLI 经 bash 且 "
        "workdir=技能目录"
    ),
    task_description="按 archify 技能手册完成一次图表工程交付",
    sub_nodes=[],
    is_end=True,
    plugins={"loop": "default_loop"},
    use_skills=["archify"],
    use_tools=["bash", "read_text", "write_text", "edit_file", "find_files"],
    base_prompt=AS_BASE_PROMPT,
)

archify_skill_pattern = Pattern(
    code="archify_skill",
    name="图表工程助手·技能版",
    description=(
        "archify 技能的说明书式运行：单 AGENT 节点 + default_loop，"
        "use_skills/allow_skills 双层授权启用 archify 技能，流程纪律归 "
        "SKILL.md 手册本体（与九节点 workflow 版 archify 互相独立）"
    ),
    pattern_type="agent",
    entry_node_code="as_work",
    nodes=[as_work],
    allow_toolset=["shell", "filesystem"],
    allow_skills=["archify"],
    # The skill ships with the repo (skills/archify/) and uses the settings
    # skills.dir default root; a deployment wanting a different skill
    # directory overrides it here (e.g. ~/.claude/skills)
)

registry.register(archify_skill_pattern)
