"""read_tasks / write_tasks — 会话任务清单（toolset: tasks）.

agent 执行多步工作时的自我进程跟踪（TodoWrite/TodoRead 等价物）：开工
前列计划、执行中勾状态，让"现在做到哪、还剩什么"在长任务里始终可查，
也给用户侧一个稳定的观察面。

授权（deny-by-default 三层收口：注册 toolset → pattern.allow_toolset →
node.use_tools）::

    pattern:
      allow_toolset: [tasks, filesystem]
    node:
      use_tools: [read_tasks, write_tasks]

作用域 = 会话：清单按 ``session_id`` 隔离，id 经
``nexus.engine.tool_context`` 注入（default_loop 执行器发布，脱离
agent loop 的直接 dispatch 落到全局桶 ``_global``）。delegate_task
子代理与 run_workflow 叶子继承父会话的 session_id（subagent_scope /
workflow_scope 用 replace() 保留该字段）——同一会话内主 agent 与子
代理共享同一份清单，子代理可以替主 agent 勾掉已完成的条目。

write_tasks 是全量替换语义（整表重写，同 TodoWrite 的幂等设计）：调用
方每次带上完整清单，避免增量 diff 的错位问题；条目带稳定 id（写入时
按 1 起分配），跨多次写 id 顺序保持——只追加时旧条目 id 不变。

内存态存储（不落库）：任务清单是会话内工作状态，进程重启即清空，
与会话历史（SQLite 审计）的定位不同。护栏（config ``tasks_tool`` 节）：
max_tasks 条数上限（默认 50），max_task_chars 单条内容上限（默认 500，
超限拒绝——长描述应拆条或落到文件）。
"""

import logging
import threading
from typing import Any, Dict, List

from nexus.engine.tool_context import current_tool_context
from nexus.registry.tools import registry, tool_error, tool_result
from nexus.settings import get_tasks_tool_config

logger = logging.getLogger(__name__)

# 会话隔离的任务清单仓：session_id -> {"next_id", "tasks": [...]}。
# 同步 handler 跑在 to_thread、fire 场景在事件循环线程，用 threading.Lock。
_GLOBAL_SCOPE = "_global"
_STORE: Dict[str, Dict[str, Any]] = {}
_STORE_LOCK = threading.Lock()

_VALID_STATUS = ("pending", "in_progress", "completed")
_VALID_PRIORITY = ("high", "medium", "low")


def _current_scope() -> str:
    ambient = current_tool_context()
    session_id = ambient.session_id if ambient is not None else ""
    return session_id or _GLOBAL_SCOPE


def _validate_tasks(raw: Any, guard: Dict[str, Any]
                    ) -> List[Dict[str, Any]]:
    """校验并整形 tasks 数组（非法即抛 ValueError，由 handler 转 tool_error）。"""
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
        board = _STORE.get(scope)
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
    guard = get_tasks_tool_config()
    try:
        cleaned = _validate_tasks(args.get("tasks"), guard)
    except ValueError as e:
        return tool_error(str(e))

    scope = _current_scope()
    with _STORE_LOCK:
        board = _STORE.setdefault(scope, {"next_id": 1, "tasks": []})
        # 稳定 id：按写入顺序 1 起分配，前缀相同的旧条目保持原 id
        # （write 是全量替换，"保持"指本次数组前部与上次一致的条目）
        old_ids = {t["content"]: t["id"] for t in board["tasks"]}
        tasks: List[Dict[str, Any]] = []
        for item in cleaned:
            task_id = old_ids.pop(item["content"], None) or board["next_id"]
            if task_id >= board["next_id"]:
                board["next_id"] = task_id + 1
            tasks.append({"id": task_id, **item})
        board["tasks"] = tasks

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
# 顶层 registry.register() 调用表达式，见 file_tool.py 的同类说明)
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
