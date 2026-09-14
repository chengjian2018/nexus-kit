"""deep_research_agent — 结构化深度研究配方(四节点 AGENT 图)。

图拓扑(route_multi.py):dr_preplan(预检索/初始化) → dr_plan(子问题规划)
→ dr_search(经 MCP 工具迭代检索,研究状态板反思) → dr_synthesize(带引用
的研究报告);每条用户消息从 entry 跑全图,同轮经 TurnResult.next 接力。
MCP 工具由 atoms/tools/mcp_tool.py 按 local_config.yaml 的 ``mcp_servers:``
动态注册(toolset=mcp-<server>);本 pattern 经 ``allow_toolset`` 授权
mcp-websearch / mcp-zai 工具集,携带工具的节点经 ``use_tools`` 收窄。
"""
