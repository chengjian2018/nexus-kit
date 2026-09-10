"""deep_research pattern — 单模块 + 自定义 executor 的 app 配方。

组装方式(全部声明式,零 kernel 改动):
- 模块经 ``executor="deep_research"`` 绑定 apps/deep_research_agent/
  executor.py 注册的插件(模块级声明,优先级最高);
- ``use_tools=None`` = 模块层不设白名单,可用工具集完全由 pattern ACL
  决定(nexus.engine.loop._resolve_tools 的空集语义)——MCP 工具动态注册
  即动态可见,授权在 local_config.yaml 的 server 级
  ``allowed_patterns: ["deep_research"]`` 收窄;
- 研究过程住在 executor 私有工作区,对话历史只有「问题 → 报告」的 Q/A
  对,故不需要自定义 messages_builder(默认三段够用)。
"""

from apps.deep_research_agent.prompts import DEEP_RESEARCH_BASE_PROMPT
from nexus.model.module import AgentModule
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

deep_research = AgentModule(
    module_code="deep_research",
    module_name="深度研究",
    module_description=(
        "结构化深度研究:规划子问题 → MCP 工具迭代检索 → 反思补搜 → "
        "综合带引用的研究报告"
    ),
    module_todo_description="对复杂问题产出带引用来源的结构化研究报告",
    base_prompt=DEEP_RESEARCH_BASE_PROMPT,
    # 空 = pattern ACL 允许的全部工具;MCP server 配置
    # allowed_patterns: ["deep_research"] 决定哪些 server 的工具进来
    use_tools=None,
    # 模块级 executor 声明(最高优先),绑定下方注册的插件码
    executor="deep_research",
    # 独立 pattern,无邻接投影(不参与 defer_to_module 体系)
    enable_project=False,
)

deep_research_pattern = Pattern(
    code="deep_research",
    name="深度研究助手",
    description=(
        "Deep research 配方:DeepResearchExecutor 的 PLAN→SEARCH→SYNTHESIZE"
        "结构化研究循环 + MCP 动态工具(toolset=mcp-*)"
    ),
    entry_module_code="deep_research",
    modules=[deep_research],
)

registry.register(deep_research_pattern)

# executor 插件注册在 apps/deep_research_agent/executor.py 底部(与
# install_booking_agent 的 stages.py 同一 idiom:文件顶部已 import 本模块,
# 这里 import 只为把注册动作与本 pattern 的发现绑在同一文件里)
import apps.deep_research_agent.executor  # noqa: E402,F401
