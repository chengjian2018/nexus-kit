"""deep_research_agent — 结构化深度研究配方(单模块 + 自定义 executor)。

数据流:用户问题 → DeepResearchExecutor 的 PLAN(分解子问题)→ SEARCH
(经 MCP 工具迭代检索,研究状态板反思)→ SYNTHESIZE(带引用的研究报告)。
MCP 工具由 atoms/tools/mcp_tool.py 按 local_config.yaml 的 ``mcp_servers:``
动态注册;本 pattern 通过 server 配置的 ``allowed_patterns: ["deep_research"]``
获得授权。
"""
