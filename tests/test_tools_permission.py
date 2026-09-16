"""Toolset authorization tests (the deny-by-default three-layer gate).

- _resolve_tools: an empty node.use_tools = no tools; an empty
  pattern.allow_toolset = no toolsets; the effective set =
  use_tools ∩ tools-of-allowed-toolsets
- validate_tools: dangling / cross-toolset use_tools fail fast at
  registration time
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


# Three toolsets: knowledge (2 tools) / mcp-zai (1) / bare mcp (1)
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
    # perm_mcp_list's toolset (mcp) is not in allow_toolset → filtered out
    assert _names(p) == {"perm_kb_search", "perm_zai_vision"}


def test_names_in_toolsets():
    # Under a full-suite run each toolset also carries other test-registered
    # tools (knowledge's real tools, mcp-zai's offline-snapshot tools) —
    # assertions use subsets, never equality
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
