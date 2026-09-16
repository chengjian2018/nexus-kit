"""deep_research_agent — a structured deep-research recipe (a four-node
AGENT graph).

Graph topology (route_multi.py): dr_preplan (pre-retrieval/init) →
dr_plan (sub-question planning) → dr_search (iterative retrieval via MCP
tools, research state-board reflection) → dr_synthesize (the cited
research report); every user message runs the whole graph from entry,
relaying within the turn via TurnResult.next. MCP tools are registered
dynamically by atoms/tools/mcp_tool.py per local_config.yaml's
``mcp_servers:`` (toolset=mcp-<server>); this pattern grants the
mcp-websearch / mcp-zai toolsets via ``allow_toolset``, and tool-carrying
nodes narrow via ``use_tools``.
"""
