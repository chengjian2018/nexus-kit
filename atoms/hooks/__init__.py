"""Hooks atoms: agent_hooks plugin packages (kind="agent_hooks").

Each module in this package carries a top-level
``registry.register("agent_hooks", ...)`` call, auto-imported by the
discover_builtin_plugins AST scan (the same discovery mechanism as
atoms/tools and atoms/executors). Current members:

- ``tool_guard``: P4 on_tool_call pre-execution announce of dangerous
  operations (rule classes + a lightweight LLM review; v1 is
  observe-only — announce, never intervene).
"""
