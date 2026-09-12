"""Toolset authorization tests (plan-⑧ §4 deny-by-default 三层收口).

- _resolve_tools: node.use_tools 空 = 无工具；pattern.allow_toolset 空 =
  无工具集；生效集 = use_tools ∩ allowed-toolsets 的工具
- validate_tools: use_tools 悬空 / 越集在注册期 fail-fast
"""

import pytest

from nexus.engine.loop import _resolve_tools
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.model.validation import validate_tools
from nexus.registry.tools import registry as tool_registry


def _register_tool(name, toolset):
    tool_registry.register(
        name=name, toolset=toolset,
        schema={"name": name, "description": f"test tool {name}",
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, _n=name: f"{_n} ok",
    )


# 三个工具集：knowledge（2 个）/ mcp-zai（1 个）/ 裸 mcp（1 个）
for _name, _ts in [("perm_kb_search", "knowledge"),
                   ("perm_kb_list", "knowledge"),
                   ("perm_zai_vision", "mcp-zai"),
                   ("perm_mcp_list", "mcp")]:
    _register_tool(_name, _ts)


def _pattern(allow_toolset, use_tools):
    return Pattern(
        code="perm_p", name="n", description="d",
        allow_toolset=allow_toolset,
        nodes=[BaseNode(code="only", use_tools=use_tools)],
    )


def _names(pattern):
    return {t["function"]["name"] for t in _resolve_tools(
        pattern.node_map["only"], pattern)}


def test_empty_use_tools_denies_everything():
    p = _pattern(allow_toolset=["knowledge"], use_tools=[])
    assert _names(p) == set()


def test_empty_allow_toolset_denies_everything():
    p = _pattern(allow_toolset=[], use_tools=["perm_kb_search"])
    assert _names(p) == set()


def test_intersection_of_layers():
    p = _pattern(allow_toolset=["knowledge", "mcp-zai"],
                 use_tools=["perm_kb_search", "perm_zai_vision",
                            "perm_mcp_list"])
    # perm_mcp_list 的 toolset（mcp）不在 allow_toolset → 被滤掉
    assert _names(p) == {"perm_kb_search", "perm_zai_vision"}


def test_names_in_toolsets():
    # 全量跑套时各 toolset 还含其它测试注册的工具（knowledge 的真实工具、
    # mcp-zai 的离线快照工具）——断言一律用子集而非全等
    assert {"perm_kb_search", "perm_kb_list"} <= \
        tool_registry.names_in_toolsets({"knowledge"})
    assert "perm_zai_vision" in tool_registry.names_in_toolsets({"mcp-zai"})
    assert tool_registry.names_in_toolsets({"nope"}) == set()
    assert tool_registry.names_in_toolsets([]) == set()


def test_validate_tools_flags_dangling_name():
    p = _pattern(allow_toolset=["knowledge"], use_tools=["ghost_tool"])
    errors = validate_tools(p)
    assert any("未注册" in e for e in errors)


def test_validate_tools_flags_cross_toolset():
    p = _pattern(allow_toolset=["knowledge"], use_tools=["perm_zai_vision"])
    errors = validate_tools(p)
    assert any("越集" in e for e in errors)


def test_validate_tools_passes_legal_declaration():
    p = _pattern(allow_toolset=["knowledge", "mcp-zai"],
                 use_tools=["perm_kb_search", "perm_zai_vision"])
    assert validate_tools(p) == []
