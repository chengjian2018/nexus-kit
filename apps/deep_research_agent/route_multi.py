"""deep_research_multi pattern — 多模块版深度研究配方(相位即模块)。

与单模块版(route.py)共用同一套相位实现(executor_multi.py 的四个
executor 复用 DeepResearchExecutor 的无状态相位方法),组织方式不同:
每个研究相位一个 AGENT 模块,同轮经 ModuleJumpEvent 接力,hop 循环
消费跳转逐站推进:

    dr_preplan(预检索/初始化)→ dr_plan(规划)→ dr_search(检索)
      → dr_synthesize(综合报告 + 底座复位)

- ``max_hops=4``:3 跳接力 + 最终模块恰好 4 次模块执行(第 4 次无跳转
  事件即自然收束;线性流水线不会再多跳,force_close 分支实际不可达);
- 相位间状态经 ``cxt.metadata["deep_research_state"]``(轮内瞬态,
  dr_synthesize 收尾弹出;begin_turn 兜底出清),终态 trace 仍写
  ``deep_research`` 键——与单模块版同键同构;
- 全部模块 ``enable_project=False``(同轮跳转目标语义,不参与投影 /
  defer 体系);不声明 sub_modules——接力边由 executor 写事件表达
  (ARCHITECTURE.md「跳转多样化配方」),若声明则会往 system 注入与
  研究无关的团队协作规则(messages.build_system_prompt 的 sub_modules
  分支);
- ``use_tools=None`` 同单模块版:模块层不设白名单,可用工具集由
  pattern ACL 决定(MCP server 级 ``allowed_patterns`` 收窄)。
"""

from apps.deep_research_agent.prompts import DEEP_RESEARCH_BASE_PROMPT
from nexus.model.module import AgentModule
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

dr_preplan = AgentModule(
    module_code="dr_preplan",
    module_name="深度研究·预检索",
    module_description=(
        "研究流水线首站:构建研究工作区,模型自行决定是否先检索一轮"
        "补背景,完成后移交规划"
    ),
    module_todo_description="为复杂问题准备研究上下文(可选预检索)",
    base_prompt=DEEP_RESEARCH_BASE_PROMPT,
    # 空 = pattern ACL 允许的全部工具(同单模块版)
    use_tools=None,
    executor="dr_preplan",
    # 跳转目标语义:同轮被 executor 写事件接力,不参与投影/defer 体系
    enable_project=False,
)

dr_plan = AgentModule(
    module_code="dr_plan",
    module_name="深度研究·规划",
    module_description=(
        "把问题分解为可检索验证的子问题(JSON 计划;解析失败自纠重试,"
        "仍失败降级为原问题单计划)"
    ),
    module_todo_description="产出研究计划(子问题清单)",
    base_prompt=DEEP_RESEARCH_BASE_PROMPT,
    use_tools=None,
    executor="dr_plan",
    enable_project=False,
)

dr_search = AgentModule(
    module_code="dr_search",
    module_name="深度研究·检索",
    module_description=(
        "带工具 ReAct 研究循环:按状态板迭代检索,直至子问题覆盖、"
        "模型判定信息足够或轮次用尽"
    ),
    module_todo_description="迭代检索收集研究资料",
    base_prompt=DEEP_RESEARCH_BASE_PROMPT,
    use_tools=None,
    executor="dr_search",
    enable_project=False,
)

dr_synthesize = AgentModule(
    module_code="dr_synthesize",
    module_name="深度研究·综合",
    module_description=(
        "基于全部资料流式生成带引用的研究报告,收尾后把研究底座复位"
        "到流水线首站"
    ),
    module_todo_description="综合资料产出研究报告",
    base_prompt=DEEP_RESEARCH_BASE_PROMPT,
    use_tools=None,
    executor="dr_synthesize",
    enable_project=False,
)

deep_research_multi_pattern = Pattern(
    code="deep_research_multi",
    name="深度研究助手(多模块)",
    description=(
        "Deep research 多模块配方:PREPLAN/PLAN/SEARCH/SYNTHESIZE 各为"
        "一个模块,同轮 ModuleJumpEvent 接力;相位实现与单模块版共用"
    ),
    entry_module_code="dr_preplan",
    modules=[dr_preplan, dr_plan, dr_search, dr_synthesize],
    # 3 跳接力 + 最终模块 = 恰好 4 次模块执行(见文件头注释)
    max_hops=4,
)

registry.register(deep_research_multi_pattern)

# 相位 executor 插件注册在 apps/deep_research_agent/executor_multi.py 底部
# (与 route.py import executor 同一 idiom:把注册动作与本 pattern 的发现
# 绑在同一文件里)
import apps.deep_research_agent.executor_multi  # noqa: E402,F401
