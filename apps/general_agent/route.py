"""general_agent pattern — 单节点通用 AI Agent（dsh 风格系统提示词）。

一个最小 AGENT 图（pattern code "general_agent"），**零自定义 executor**：
节点跑 default_loop（ReAct 工具循环），两层声明把能力面接上——

- 工具授权（deny-by-default 三层）：``allow_toolset=["shell",
  "filesystem"]`` × ``use_tools``（bash / run_python + filesystem 六件套
  read_text/write_text/edit_file/list_dir/search_files/find_files）——
  全部复用内置工具，本 app 不注册任何新工具；
- 技能授权（deny-by-default 两层，nexus/skills.py 解析）：
  ``allow_skills`` × ``use_skills`` = nexus-app-builder-skill +
  nexus-app-template-skill（扫描根中的目录名即规范名；frontmatter 的
  name=nexus-app-builder / nexus-app-template-builder 只是展示名）。
  生效集非空时 default_loop 自动附带 load_skill / read_skill_file 两个
  只读知识工具，并把技能元数据块注入 system prompt（description 即
  触发器）；技能的执行面（写 apps/、跑 pytest）仍走上面的三层工具
  收敛——技能授予知识，不授予权限。

base_prompt 借镜 DeepSeek Harness（dsh）的 system prompt 文风：逐工具
一节祈使句规则 + 装载/汇报/安全横向纪律（见 prompts.py 头注）；两个
builder 技能的流程纪律归 SKILL.md 手册本体。
"""

from apps.general_agent.prompts import GENERAL_AGENT_BASE_PROMPT
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

assistant = BaseNode(
    code="assistant",
    name="通用助手",
    description=(
        "通用 ReAct 工具循环节点：读写文件、执行命令、按需装载技能手册"
        "（nexus-app-builder / nexus-app-template-builder），端到端完成"
        "任务并汇报产出"
    ),
    task_description="理解需求，用工具与技能完成端到端交付",
    sub_nodes=[],
    is_end=True,
    plugins={"loop": "default_loop"},
    use_skills=["nexus-app-builder-skill", "nexus-app-template-skill"],
    use_tools=[
        "bash", "run_python",
        "read_text", "write_text", "edit_file",
        "list_dir", "search_files", "find_files",
    ],
    base_prompt=GENERAL_AGENT_BASE_PROMPT,
)

general_agent_pattern = Pattern(
    code="general_agent",
    name="通用 AI Agent（单节点·技能挂载版）",
    description=(
        "单节点通用 Agent：default_loop + filesystem/shell 内置工具组 + "
        "nexus-app-builder / nexus-app-template-builder 两个技能（手册式"
        "消费）；base_prompt 借镜 dsh 的逐工具纪律文风"
    ),
    pattern_type="agent",
    entry_node_code="assistant",
    nodes=[assistant],
    allow_toolset=["shell", "filesystem"],
    allow_skills=["nexus-app-builder-skill", "nexus-app-template-skill"],
    # 两个技能随仓库发布（skills/ 目录），用 settings skills.dir 的默认
    # 扫描根；部署方要换技能目录时经 app config.yaml 的 skills.dir 覆盖
    # （或在下面 config 里钉 skills_dir），不在声明里写死绝对路径
)

registry.register(general_agent_pattern)
