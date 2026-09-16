"""read_tasks / write_tasks — session task list (toolset: tasks).

Self process-tracking for multi-step agent work (the TodoWrite/TodoRead
equivalent): plan before starting, tick statuses while executing, so "where
are we, what is left" stays visible in long tasks, and users get a stable
observation surface.

Authorization (deny-by-default three-layer gate: registered toolset →
pattern.allow_toolset → node.use_tools)::

    pattern:
      allow_toolset: [tasks, filesystem]
    node:
      use_tools: [read_tasks, write_tasks]

Scope = the session: lists are isolated by ``session_id``, injected via
``nexus.engine.tool_context`` (published by the default_loop executor;
direct dispatch outside the agent loop lands in the global bucket
``_global``). delegate_task sub-agents and run_workflow leaves inherit the
parent session's session_id (subagent_scope / workflow_scope keep the
field via replace()) — the main agent and its sub-agents share one list
within a session, and a sub-agent may tick completed items on the main
agent's behalf.

write_tasks is whole-replacement semantics (full-table rewrite, the same
idempotent design as TodoWrite): the caller passes the complete list every
time, avoiding incremental-diff misalignment; entries carry stable ids
(assigned from 1 at write time) whose order survives across writes —
append-only writes keep old ids unchanged.

In-memory storage (not persisted): the task list is intra-session working
state, cleared on process restart — a different positioning from session
history (the SQLite audit). Guardrails (config ``tasks_tool`` section):
max_tasks entry cap (default 50), max_task_chars per-entry cap (default
500; over-limit is rejected — long descriptions should be split into items
or moved into a file).
"""

import logging
import threading
from typing import Any, Dict, List

from nexus.engine.tool_context import ambient_pattern_code, current_tool_context
from nexus.registry.tools import registry, tool_error, tool_result
from nexus.settings import get_tasks_tool_config

logger = logging.getLogger(__name__)

# Session-isolated task-list store: session_id -> {"next_id", "tasks": [...]}.
# Sync handlers run on to_thread and fire scenarios run on the event loop
# thread, hence a threading.Lock. LRU cap on the scope count: a long-running
# host accumulates one per session / cron job; cold scopes (the dict front)
# are evicted first when over the cap (lists are intra-session working
# state and are allowed to disappear)
_GLOBAL_SCOPE = "_global"
_STORE: Dict[str, Dict[str, Any]] = {}
_STORE_LOCK = threading.Lock()
_STORE_CAP = 256

_VALID_STATUS = ("pending", "in_progress", "completed")
_VALID_PRIORITY = ("high", "medium", "low")


def _current_scope() -> str:
    ambient = current_tool_context()
    session_id = ambient.session_id if ambient is not None else ""
    return session_id or _GLOBAL_SCOPE


def _validate_tasks(raw: Any, guard: Dict[str, Any]
                    ) -> List[Dict[str, Any]]:
    """Validate and shape the tasks array (raises ValueError on anything illegal; handlers convert to tool_error)."""
    if not isinstance(raw, list):
        raise ValueError("tasks 应为数组")
    if not raw:
        raise ValueError("tasks 不能为空（清空清单请直接说明，不需要传空数组）")
    cap = int(guard["max_tasks"])
    if len(raw) > cap:
        raise ValueError(f"任务条数 {len(raw)} 超过上限 {cap}，请合并或精简")
    chars_cap = int(guard["max_task_chars"])

    cleaned = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"tasks[{i}] 应为对象（content/status/priority）")
        content = str(item.get("content") or "").strip()
        if not content:
            raise ValueError(f"tasks[{i}].content 不能为空")
        if len(content) > chars_cap:
            raise ValueError(
                f"tasks[{i}].content {len(content)} 字符超过上限 {chars_cap}，"
                "请精简描述（细节落到文件）")
        status = str(item.get("status") or "pending")
        if status not in _VALID_STATUS:
            raise ValueError(
                f"tasks[{i}].status 非法: {status!r}，合法值: "
                f"{list(_VALID_STATUS)}")
        priority = str(item.get("priority") or "medium")
        if priority not in _VALID_PRIORITY:
            raise ValueError(
                f"tasks[{i}].priority 非法: {priority!r}，合法值: "
                f"{list(_VALID_PRIORITY)}")
        cleaned.append({"content": content, "status": status,
                        "priority": priority})
    in_progress = [t for t in cleaned if t["status"] == "in_progress"]
    if len(in_progress) > 1:
        raise ValueError(
            f"in_progress 任务最多 1 条（当前 {len(in_progress)}）——"
            "串行推进，一次只做一件事")
    return cleaned


# ---------------------------------------------------------------------------
# read_tasks
# ---------------------------------------------------------------------------

READ_TASKS_SCHEMA = {
    "name": "read_tasks",
    "description": (
        "读取当前会话的任务清单（多步工作的自我进程跟踪）：各条目的"
        "id、内容、状态（pending/in_progress/completed）与优先级。"
        "清单按会话隔离，本会话的子代理共享同一份。空清单返回空列表。"
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


def _handle_read_tasks(args: Dict[str, Any]) -> str:
    scope = _current_scope()
    with _STORE_LOCK:
        board = _STORE.pop(scope, None)     # LRU touch: reads count as activity
        if board is not None:
            _STORE[scope] = board
        tasks = list(board["tasks"]) if board else []
    return tool_result({
        "session": scope,
        "tasks": tasks,
        "count": len(tasks),
        "note": "" if tasks else "当前会话还没有任务清单",
    })


# ---------------------------------------------------------------------------
# write_tasks
# ---------------------------------------------------------------------------

WRITE_TASKS_SCHEMA = {
    "name": "write_tasks",
    "description": (
        "全量写入当前会话的任务清单（整体替换）。每次调用都带上完整"
        "列表——新计划一次写全，进度更新时把上一版读出来改状态后整表"
        "重写。规则：content 非空；status ∈ pending/in_progress/"
        "completed；in_progress 全表最多 1 条；priority ∈ high/medium/"
        "low（可选，默认 medium）。写入时按顺序分配稳定 id（1 起）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "任务描述（一句可执行的话）"},
                        "status": {
                            "type": "string",
                            "enum": list(_VALID_STATUS),
                            "description": "状态（可选，默认 pending）",
                        },
                        "priority": {
                            "type": "string",
                            "enum": list(_VALID_PRIORITY),
                            "description": "优先级（可选，默认 medium）",
                        },
                    },
                    "required": ["content"],
                },
                "description": "完整任务列表（全量替换语义）",
            },
        },
        "required": ["tasks"],
    },
}


def _handle_write_tasks(args: Dict[str, Any]) -> str:
    guard = get_tasks_tool_config(ambient_pattern_code())
    try:
        cleaned = _validate_tasks(args.get("tasks"), guard)
    except ValueError as e:
        return tool_error(str(e))

    scope = _current_scope()
    with _STORE_LOCK:
        board = _STORE.get(scope) or {"next_id": 1, "tasks": []}
        # Stable ids: assigned in write order from 1; old entries whose
        # prefix matches keep their ids (write is whole-replacement, so
        # "keep" means entries where this array's front matches last time's)
        old_ids = {t["content"]: t["id"] for t in board["tasks"]}
        tasks: List[Dict[str, Any]] = []
        for item in cleaned:
            task_id = old_ids.pop(item["content"], None) or board["next_id"]
            if task_id >= board["next_id"]:
                board["next_id"] = task_id + 1
            tasks.append({"id": task_id, **item})
        board["tasks"] = tasks
        _STORE.pop(scope, None)             # LRU touch: move to the most-recently-used end
        _STORE[scope] = board
        while len(_STORE) > _STORE_CAP:     # over cap: evict from the oldest scope
            del _STORE[next(iter(_STORE))]

    logger.info("[write_tasks] session=%s: %d 条（in_progress=%d）", scope,
                len(tasks),
                sum(1 for t in tasks if t["status"] == "in_progress"))
    return tool_result({
        "session": scope,
        "tasks": tasks,
        "count": len(tasks),
    })


# ---------------------------------------------------------------------------
# Self-registration (registered on module import; AST scan auto-discovery —
# a top-level registry.register() call expression; see file_tool.py's note)
# ---------------------------------------------------------------------------

registry.register(
    name="read_tasks",
    toolset="tasks",
    schema=READ_TASKS_SCHEMA,
    handler=_handle_read_tasks,
    description="读当前会话的任务清单",
    emoji="📋",
)

registry.register(
    name="write_tasks",
    toolset="tasks",
    schema=WRITE_TASKS_SCHEMA,
    handler=_handle_write_tasks,
    description="全量写当前会话的任务清单（稳定 id，≤1 条 in_progress）",
    emoji="📝",
)
