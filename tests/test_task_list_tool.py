"""read_tasks / write_tasks (tasks tool) unit tests: write-read round trips, stable ids,
validation rules (illegal status / two in_progress / over-length / entry
cap / empty array), session isolation (the contextvar's session_id; outside
the loop lands in the _global bucket), sub-agents inheriting the parent
session scope, and registry-level registration ownership (toolset: tasks).
"""

import json
from unittest.mock import patch

import pytest

from atoms.tools import task_list_tool  # noqa: F401 -- the module import registers
from atoms.tools.task_list_tool import _STORE
from nexus.engine.tool_context import (
    ToolCallContext,
    subagent_scope,
    tool_call_context,
)
from nexus.registry.tools import registry as tool_registry
from async_utils import arun

_GUARD = {"max_tasks": 50, "max_task_chars": 500}


@pytest.fixture(autouse=True)
def _fresh_store():
    """A per-test task-list store (module-level global state; must be cleaned explicitly).

    Never wrap the yield in ``with _STORE_LOCK`` — that would hold the lock
    across the whole test and the handler-side ``with _STORE_LOCK`` would
    deadlock outright (tests run serially, so a bare clear is safe enough).
    """
    _STORE.clear()
    yield
    _STORE.clear()


def _dispatch(name, args, session_id=None):
    """Execute via registry.dispatch; publishes the contextvar when session_id is non-empty."""
    guard = patch("atoms.tools.task_list_tool.get_tasks_tool_config",
                  return_value=dict(_GUARD))
    if session_id is None:
        with guard:
            return json.loads(arun(tool_registry.dispatch(name, args)))
    ctx = tool_call_context({"code": "x", "model": "m"},
                            ["tasks"], session_id=session_id)
    with guard, ctx:
        return json.loads(arun(tool_registry.dispatch(name, args)))


def test_registered_in_tasks_toolset():
    for name in ("read_tasks", "write_tasks"):
        assert tool_registry.get_toolset_for_tool(name) == "tasks"
    assert {"read_tasks", "write_tasks"} <= \
        tool_registry.names_in_toolsets({"tasks"})


def test_read_empty():
    r = _dispatch("read_tasks", {})
    assert r["tasks"] == [] and r["count"] == 0
    assert "还没有" in r["note"]


def test_write_then_read_roundtrip():
    r = _dispatch("write_tasks", {"tasks": [
        {"content": "调研引擎接线", "status": "completed"},
        {"content": "实现 cron 内核", "status": "in_progress",
         "priority": "high"},
        {"content": "补测试"},
    ]})
    assert r["count"] == 3
    assert [t["id"] for t in r["tasks"]] == [1, 2, 3]
    # Missing fields are backfilled with defaults
    assert r["tasks"][0]["priority"] == "medium"
    assert r["tasks"][1]["priority"] == "high"
    assert r["tasks"][2]["status"] == "pending"

    r = _dispatch("read_tasks", {})
    by_content = {t["content"]: t for t in r["tasks"]}
    assert by_content["实现 cron 内核"]["status"] == "in_progress"


def test_write_stable_ids_on_prefix_rewrite():
    _dispatch("write_tasks", {"tasks": [{"content": "任务A"},
                                        {"content": "任务B"}]})
    # Whole rewrite: prefix entries keep their ids, appended entries get new ids
    r = _dispatch("write_tasks", {"tasks": [
        {"content": "任务A", "status": "completed"},
        {"content": "任务B", "status": "completed"},
        {"content": "任务C"},
    ]})
    ids = {t["content"]: t["id"] for t in r["tasks"]}
    assert ids["任务A"] == 1 and ids["任务B"] == 2 and ids["任务C"] == 3


def test_write_validation_errors():
    r = _dispatch("write_tasks", {"tasks": []})
    assert "不能为空" in r["error"]
    r = _dispatch("write_tasks", {"tasks": "not-a-list"})
    assert "数组" in r["error"]
    r = _dispatch("write_tasks", {"tasks": [{"content": "x",
                                             "status": "doing"}]})
    assert "status" in r["error"]
    r = _dispatch("write_tasks", {"tasks": [
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "in_progress"}]})
    assert "in_progress" in r["error"]
    r = _dispatch("write_tasks", {"tasks": [{"content": "  "}]})
    assert "content" in r["error"]
    r = _dispatch("write_tasks", {"tasks": [{"content": "x",
                                             "priority": "urgent"}]})
    assert "priority" in r["error"]


def test_write_guard_caps():
    # Entry cap / per-entry length cap (verified through the same dispatch path with a narrowed guard)
    with patch("atoms.tools.task_list_tool.get_tasks_tool_config",
               return_value={"max_tasks": 2, "max_task_chars": 10}):
        r = json.loads(arun(tool_registry.dispatch(
            "write_tasks", {"tasks": [{"content": "a"}, {"content": "b"},
                                      {"content": "c"}]})))
        assert "上限" in r["error"]
        r = json.loads(arun(tool_registry.dispatch(
            "write_tasks", {"tasks": [{"content": "x" * 11}]})))
        assert "超过上限" in r["error"]


def test_session_scoping():
    _dispatch("write_tasks", {"tasks": [{"content": "会话1的任务"}]},
              session_id="sess-A")
    _dispatch("write_tasks", {"tasks": [{"content": "会话2的任务"}]},
              session_id="sess-B")
    a = _dispatch("read_tasks", {}, session_id="sess-A")
    b = _dispatch("read_tasks", {}, session_id="sess-B")
    assert a["count"] == 1 and "会话1" in a["tasks"][0]["content"]
    assert b["count"] == 1 and "会话2" in b["tasks"][0]["content"]
    assert a["session"] == "sess-A"
    # Outside the agent loop (no contextvar) → the global bucket
    g = _dispatch("read_tasks", {})
    assert g["session"] == "_global" and g["count"] == 0


def test_subagent_inherits_session_scope():
    """subagent_scope's replace() keeps session_id — sub-agents share the parent session's list."""
    _dispatch("write_tasks", {"tasks": [{"content": "主会话任务"}]},
              session_id="sess-S")
    base = ToolCallContext(llm_config={}, allow_toolsets=frozenset(["tasks"]),
                           session_id="sess-S")
    with patch("atoms.tools.task_list_tool.get_tasks_tool_config",
               return_value=dict(_GUARD)), subagent_scope(base):
        r = json.loads(arun(tool_registry.dispatch("read_tasks", {})))
    assert r["session"] == "sess-S"
    assert "主会话任务" in r["tasks"][0]["content"]


def test_store_scopes_lru_capped(monkeypatch):
    """The scope store's LRU cap: scopes beyond _STORE_CAP evict oldest-first; active
    scopes are untouched (a long-running host no longer grows without
    bound)."""
    import atoms.tools.task_list_tool as tlt

    monkeypatch.setattr(tlt, "_STORE_CAP", 2)
    tlt._STORE.clear()

    from nexus.engine.tool_context import tool_call_context
    for sid in ("s1", "s2"):
        with tool_call_context({"code": "x"}, set(), session_id=sid):
            _dispatch("write_tasks", {"tasks": [
                {"content": f"t-{sid}", "status": "pending"}]})
    with tool_call_context({"code": "x"}, set(), session_id="s3"):
        _dispatch("write_tasks", {"tasks": [
            {"content": "t-s3", "status": "pending"}]})

    assert set(tlt._STORE) == {"s2", "s3"}   # s1, the oldest, was evicted
    with tool_call_context({"code": "x"}, set(), session_id="s2"):
        r = _dispatch("read_tasks", {})
    assert r["count"] == 1                   # the active scope is intact
