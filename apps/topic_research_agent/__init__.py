"""topic_research_agent — 六站结构化深度研究配方(AGENT 图,plan-⑨ 扇出)。

图拓扑(route.py):tr_preplan(预规划/预检索) → tr_plan(分主题规划,经
``TurnResult.sends`` 扇出) → tr_search ×N(一实例一主题,经 MCP 工具迭代
检索) → tr_merge(结构化合并,零 LLM) → tr_report(报告草稿) →
tr_polish(格式美化,流式终态);每条用户消息从 entry 跑全图,同轮经
TurnResult.next 接力。PREPLAN/SEARCH 复用 deep_research_agent 的无状态
相位方法(DeepResearchExecutor 子类);分主题/合并/报告/美化为本应用
自有站点。MCP 工具由 atoms/tools/mcp_tool.py 按 local_config.yaml 的
``mcp_servers:`` 动态注册(toolset=mcp-<server>);本 pattern 经
``allow_toolset`` 授权 mcp-websearch 工具集,携带工具的节点经
``use_tools`` 收窄。
"""
