"""read_tasks / write_tasks（tasks tool）单测：写入读取往返、稳定 id、
校验规则（非法 status / 两条 in_progress / 超长 / 条数上限 / 空数组）、
会话隔离（contextvar 的 session_id；脱离 loop 落 _global 桶）、子代理
继承父会话作用域，以及 registry 层面的注册归属（toolset: tasks）。
"""

import json
from unittest.mock import patch

import pytest

from atoms.tools import task_list_tool  # noqa: F401 -- module import 即注册
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
    """每个测试独立的任务清单仓（模块级全局态，必须显式清理）。

    注意不能用 ``with _STORE_LOCK`` 包住 yield——那会把锁占满整个测试，
    handler 侧的 ``with _STORE_LOCK`` 直接死锁（测试串行执行，裸 clear
    已足够安全）。
    """
    _STORE.clear()
    yield
    _STORE.clear()


def _dispatch(name, args, session_id=None):
    """经 registry.dispatch 执行；session_id 非空时发布 contextvar。"""
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
    # 未提供的字段按默认补全
    assert r["tasks"][0]["priority"] == "medium"
    assert r["tasks"][1]["priority"] == "high"
    assert r["tasks"][2]["status"] == "pending"

    r = _dispatch("read_tasks", {})
    by_content = {t["content"]: t for t in r["tasks"]}
    assert by_content["实现 cron 内核"]["status"] == "in_progress"


def test_write_stable_ids_on_prefix_rewrite():
    _dispatch("write_tasks", {"tasks": [{"content": "任务A"},
                                        {"content": "任务B"}]})
    # 全量重写：前缀条目保持原 id，追加的拿新 id
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
    # 条数上限 / 单条长度上限（收窄 guard 后经同一 dispatch 路径验证）
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
    # 脱离 agent loop（无 contextvar）→ 全局桶
    g = _dispatch("read_tasks", {})
    assert g["session"] == "_global" and g["count"] == 0


def test_subagent_inherits_session_scope():
    """subagent_scope 的 replace() 保留 session_id —— 子代理共享父会话清单。"""
    _dispatch("write_tasks", {"tasks": [{"content": "主会话任务"}]},
              session_id="sess-S")
    base = ToolCallContext(llm_config={}, allow_toolsets=frozenset(["tasks"]),
                           session_id="sess-S")
    with patch("atoms.tools.task_list_tool.get_tasks_tool_config",
               return_value=dict(_GUARD)), subagent_scope(base):
        r = json.loads(arun(tool_registry.dispatch("read_tasks", {})))
    assert r["session"] == "sess-S"
    assert "主会话任务" in r["tasks"][0]["content"]
