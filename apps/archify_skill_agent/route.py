"""archify_skill pattern — skill 说明书式运行的最小声明式配方。

一个 AGENT 单节点图（pattern code "archify_skill"），**零定制执行器**：
节点挂 default_loop，靠两层声明把 archify 技能接进来——

- 技能授权（deny-by-default 双层，nexus/skills.py 解析）：
  ``allow_skills=["archify"]`` × ``use_skills=["archify"]``。生效集非空时
  default_loop 自动获得 load_skill / read_skill_file 两个只读知识工具，
  并在 system prompt 注入技能元数据（描述即触发器）；
- 技能的执行面（跑 archify CLI、读写候选与产物）仍走工具三层收口：
  ``allow_toolset=["shell", "filesystem"]`` × ``use_tools=[bash, 文件五件套]``。

与 apps/archify_agent（pattern "archify"，九节点 workflow 版）互相独立：
那边把 SKILL.md 的验收纪律**编译**成图闸门与确定性回执站，这边把纪律
留给手册本体，节点只负责装载与执行——同一条 skill 的两种消费档位。
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
    # 技能已随仓库分发（skills/archify/），走 settings skills.dir 默认根；
    # 部署想改用其它技能目录时在此覆盖（如 ~/.claude/skills）
)

registry.register(archify_skill_pattern)
