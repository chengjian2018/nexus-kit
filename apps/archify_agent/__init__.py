"""archify_agent — 把 archify 图表技能转译为 nexus-kit 的 AGENT 图配方。

route.py 声明九节点拓扑 / 工具授权 / 语义契约并绑定执行器
(plugins={"loop": 节点码});executor.py 携带九站 NodeExecutor 实现
(创作/修复/感知评审三站驱动 LLM,探针/闸门/交付/浏览器检查/汇报五站
确定性执行);prompts.py 是提示词资产。图拓扑与各站纪律见 route.py 模块
docstring。
"""
