"""MCP client atoms — the connection manager behind toolset ``mcp-*``.

The kernel's ToolRegistry was designed with MCP dynamic tools in mind
(``mcp-`` toolset prefix exemptions in register/deregister); this package
supplies the missing client side: an asyncio-native manager whose
connections live on the caller's event loop (loop affinity is a hard
constraint — a ClientSession cannot cross event loops), discovers tools via
``list_tools`` and registers them into the ToolRegistry with
``toolset="mcp-<server>"``.
"""
