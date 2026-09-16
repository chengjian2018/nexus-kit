"""topic_research_agent — a six-station structured deep-research recipe
(AGENT graph, runtime fan-out).

Graph topology (route.py): tr_preplan (pre-planning / pre-retrieval) →
tr_plan (per-topic planning, fanning out via ``TurnResult.sends``) →
tr_search ×N (one instance per topic, iterative retrieval via MCP tools)
→ tr_merge (structured merge, zero LLM) → tr_report (report draft) →
tr_polish (format polish, streaming terminal state); every user message
runs the whole graph from entry, relaying within the turn via
TurnResult.next. PREPLAN/SEARCH reuse deep_research_agent's stateless
phase methods (DeepResearchExecutor subclasses); the per-topic / merge /
report / polish stations are this app's own. MCP tools are registered
dynamically by atoms/tools/mcp_tool.py per local_config.yaml's
``mcp_servers:`` (toolset=mcp-<server>); this pattern grants the
mcp-websearch toolset via ``allow_toolset``, and tool-carrying nodes
narrow via ``use_tools``.
"""
