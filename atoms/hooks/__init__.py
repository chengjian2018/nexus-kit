"""Hooks atoms: agent_hooks 插件包（kind="agent_hooks"）。

本包内的模块各自带顶层 ``registry.register("agent_hooks", ...)`` 调用，
由 discover_builtin_plugins 的 AST 扫描自动 import（与 atoms/tools /
atoms/executors 同一套发现机制）。目前成员：

- ``tool_guard``：P4 on_tool_call 工具执行前危险操作播报（规则类 +
  轻量 LLM 判读，v1 只播报不干预）。
"""
