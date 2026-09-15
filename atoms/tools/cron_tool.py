"""create_cron / list_crons / update_cron / delete_cron（toolset: cron）.

定时调度四件套：把"到点跑一个自包含 agent 任务"注册成持久化作业
（CronCreate/List/Update/Delete 等价物）。fire = 授权快照工具池上的
delegate 式子代理执行（内核在 atoms/tools/_cron_core.py，与
delegate_task 同一原语），适合每日简报、定期巡检、周期性数据整理等
"无需人守着"的周期工作。

授权（deny-by-default 三层收口：注册 toolset → pattern.allow_toolset →
node.use_tools）::

    pattern:
      allow_toolset: [cron, filesystem, knowledge]
    node:
      use_tools: [create_cron, list_crons, update_cron, delete_cron]

作业权限 = 创建时刻 pattern 授权的冻结快照（剔除 subagent/workflow/
cron 编排工具集，结构性防递归与自复制），args.tools 只能收窄。fire
在 host 进程内由 tick 循环驱动（默认 20s 一拍），同 job 不重入；停机
期间错过的触发不补跑（重启后从下一个匹配点继续）。

调度表达：``schedule`` 传标准 5 字段 cron（分 时 日 月 周，宿主本地
时间，支持 ``*``/``*/n``/``a``/``a-b``/``a-b/n`` 及逗号），或
``interval_minutes`` 固定间隔（1-43200，即最长 30 天）——二选一。

护栏（config ``cron_tool`` 节）：max_jobs（默认 20）、fire_timeout_
seconds 单次触发超时（默认 300s，args 只能调小）、max_rounds 子循环
轮次（默认 8）、history_cap 每作业保留的历史条数（默认 10）、
max_input_chars（默认 8000）、max_result_chars 单次结果截断（默认
4000）、jobs_path 持久化文件（默认 data/cron_jobs.json）、tick_seconds
调度拍长（默认 20）。
"""

import datetime
import logging
from typing import Any, Dict, List

from atoms.tools._cron_core import (
    _disabled,
    compute_next_fire,
    get_scheduler,
    new_job_id,
    parse_cron,
    snapshot_tool_pool,
)
from nexus.engine.tool_context import current_tool_context
from nexus.registry.tools import registry, tool_error, tool_result
from nexus.settings import get_cron_tool_config

logger = logging.getLogger(__name__)

_MAX_INTERVAL_MINUTES = 43200   # 30 天
_MAX_NAME_CHARS = 100

_SCHEDULE_DESC = (
    "标准 5 字段 cron 表达式（分 时 日 月 周，空格分隔，宿主本地"
    "时间）：每字段支持 * / */n / a / a-b / a-b/n 及逗号组合；"
    "0-6 或 7 表示周日。例：'0 9 * * 1-5' = 工作日每天 9 点；"
    "'*/30 * * * *' = 每 30 分钟。与 interval_minutes 二选一。"
)


def _resolve_schedule(args: Dict[str, Any]
                      ) -> tuple:  # (err, schedule, schedule_desc)
    """调度参数解析：schedule（cron 串）与 interval_minutes 二选一。"""
    cron = args.get("schedule")
    interval = args.get("interval_minutes")
    has_cron = cron is not None and str(cron).strip() != ""
    has_interval = interval is not None
    if has_cron and has_interval:
        return ("schedule 与 interval_minutes 只能二选一", None, None)
    if not has_cron and not has_interval:
        return ("缺少调度表达：schedule（cron 表达式）或 interval_minutes"
                " 至少其一", None, None)
    if has_cron:
        try:
            parse_cron(str(cron).strip())   # 提前校验，坏表达式即刻报错
        except ValueError as e:
            return (str(e), None, None)
        return None, {"cron": str(cron).strip()}, f"cron={cron!r}"
    try:
        minutes = int(interval)
    except (TypeError, ValueError):
        return ("interval_minutes 应为正整数（分钟）", None, None)
    if not 1 <= minutes <= _MAX_INTERVAL_MINUTES:
        return (f"interval_minutes 应在 1-{_MAX_INTERVAL_MINUTES} 之间",
                None, None)
    return None, {"interval_minutes": minutes}, f"interval={minutes}min"


def _resolve_tools(args: Dict[str, Any], pool: List[str]
                   ) -> tuple:  # (err, tools)
    """args.tools 可选收窄：必须 ⊆ 快照池。"""
    if args.get("tools") is None:
        return None, list(pool)
    requested = {str(t) for t in args["tools"]}
    invalid = sorted(requested - set(pool))
    if invalid:
        return (f"以下工具不在本作业可用池中: {invalid}。"
                f"可用工具：{pool}。请从中选择，或省略 tools 使用全部可用工具。",
                None)
    return None, sorted(requested)


def _resolve_timeout(args: Dict[str, Any], guard: Dict[str, Any]
                     ) -> tuple:  # (err, timeout)
    timeout = float(guard["fire_timeout_seconds"])
    if args.get("timeout_seconds") is not None:
        try:
            requested = float(args["timeout_seconds"])
        except (TypeError, ValueError):
            return ("timeout_seconds 应为正数（秒）", None)
        if requested <= 0:
            return ("timeout_seconds 必须大于 0", None)
        timeout = min(requested, timeout)
    return None, timeout


# ---------------------------------------------------------------------------
# create_cron
# ---------------------------------------------------------------------------

CREATE_CRON_SCHEMA = {
    "name": "create_cron",
    "description": (
        "创建一个定时作业：到点自动用一个子代理执行 input 描述的自包含"
        "任务（可选携带工具），结果记入作业历史（list_crons 可查）。"
        "子代理看不到当前对话——input 必须自包含（目标、背景、期望产出、"
        "结果去向的说明）。作业持久化，服务重启后继续生效；停机期间错过"
        "的触发不补跑。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "作业名（人类可读，用于 list/update/delete 定位）"},
            "schedule": {"type": "string", "description": _SCHEDULE_DESC},
            "interval_minutes": {"type": "integer", "description": "固定间隔分钟数（1-43200），与 schedule 二选一"},
            "input": {
                "type": "string",
                "description": (
                    "自包含的任务描述：到点后子代理拿到的全部信息。"
                    "务必写明做什么、背景、期望的产出格式。"
                ),
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "触发时子代理可用的工具名（可选；必须在系统授权范围内，缺省=授权范围内全部可用工具）",
            },
            "system_prompt": {"type": "string", "description": "触发时子代理的角色设定（可选）"},
            "timeout_seconds": {"type": "integer", "description": "单次触发超时秒数（可选；只能调小，不能超过系统上限）"},
        },
        "required": ["name", "input"],
    },
}


def _handle_create_cron(args: Dict[str, Any]) -> str:
    guard = get_cron_tool_config()
    name = str(args.get("name") or "").strip()
    if not name:
        return tool_error("name 必填：作业名（list/update/delete 用它定位）")
    if len(name) > _MAX_NAME_CHARS:
        return tool_error(f"name 超过 {_MAX_NAME_CHARS} 字符")
    task_input = str(args.get("input") or "").strip()
    if not task_input:
        return tool_error("input 必填：到点执行的自包含任务描述")
    if len(task_input) > int(guard["max_input_chars"]):
        return tool_error(
            f"input {len(task_input)} 字符超过上限 {guard['max_input_chars']}")

    err, schedule, sched_desc = _resolve_schedule(args)
    if err:
        return tool_error(err)
    err, timeout = _resolve_timeout(args, guard)
    if err:
        return tool_error(err)

    # 授权快照：创建时刻 pattern 授权 − 编排工具集；args.tools 只能收窄
    ambient = current_tool_context()
    pool = snapshot_tool_pool(
        ambient.allow_toolsets if ambient is not None else [])
    err, tools = _resolve_tools(args, pool)
    if err:
        return tool_error(err)

    sched = get_scheduler()
    next_fire_at = compute_next_fire({"schedule": schedule})
    if next_fire_at is None:
        # 死日期（如 2 月 30 日）：静默接受 = 永不触发的哑作业，且每次
        # 排程都要白扫一整年（52 万分钟步）——创建即拒绝
        return tool_error(
            "调度表达式永不命中（如 2 月 30 日 / 非闰年 2 月 29 日），"
            "请修正 schedule")

    job = {
        "id": new_job_id(),
        "name": name,
        "enabled": True,
        "schedule": schedule,
        "input": task_input,
        "system_prompt": str(args.get("system_prompt") or "").strip(),
        "tools": tools,
        "_pool": pool,          # 授权快照池（内部字段，update 校验用）
        "timeout_seconds": timeout,
        "created_at": datetime.datetime.now().isoformat(
            timespec="seconds"),
        "runs": 0,
        "next_fire_at": next_fire_at,
        "last_run": None,
        "history": [],
    }
    try:
        # max_jobs 在 add() 锁内检查（锁外先查后加在并发 create 下会超限）
        sched.add(job)
    except ValueError as e:
        return tool_error(str(e))
    logger.info("[create_cron] %s (%s): tools=%d, %s", job["id"], name,
                len(tools), sched_desc)
    return tool_result({
        "job_id": job["id"],
        "name": name,
        "schedule": schedule,
        "next_fire_at": job["next_fire_at"],
        "tools": tools,
        "note": "" if not _disabled() else
        "（NEXUS_CRON_DISABLED=1：调度器未运行，作业仅存在于本进程内存）",
    })


# ---------------------------------------------------------------------------
# list_crons
# ---------------------------------------------------------------------------

LIST_CRONS_SCHEMA = {
    "name": "list_crons",
    "description": (
        "列出全部定时作业：调度表达、启停状态、下次触发时间、执行次数、"
        "最近一次结果与历史尾部。检查作业状态/查历史用它。"
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


def _job_public(job: Dict[str, Any]) -> Dict[str, Any]:
    """对外视图：剔除内部 _pool，history 只带尾部 3 条。"""
    view = {k: v for k, v in job.items() if not k.startswith("_")}
    view["history"] = (job.get("history") or [])[-3:]
    return view


def _handle_list_crons(args: Dict[str, Any]) -> str:
    sched = get_scheduler()
    # 锁内快照后锁外排序渲染——不迭代活容器（add/remove/_fire 并发变更）
    jobs = [_job_public(job)
            for job in sorted(sched.list_jobs(),
                              key=lambda j: j.get("created_at", ""))]
    return tool_result({"jobs": jobs, "count": len(jobs)})


# ---------------------------------------------------------------------------
# update_cron
# ---------------------------------------------------------------------------

UPDATE_CRON_SCHEMA = {
    "name": "update_cron",
    "description": (
        "更新定时作业：只改传入的字段，其余保持不变。可改 name / 启停"
        "（enabled）/ 调度（schedule 或 interval_minutes，改后重排下次"
        "触发）/ input / system_prompt / tools（仍需在作业授权池内）/"
        " timeout_seconds（只能调小）。作业 ID 从 list_crons 取。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "作业 ID（list_crons 获取）"},
            "name": {"type": "string", "description": "新作业名（可选）"},
            "enabled": {"type": "boolean", "description": "启停（可选；暂停=false，保留作业不触发）"},
            "schedule": {"type": "string", "description": "新 cron 表达式（可选，与 interval_minutes 互斥）"},
            "interval_minutes": {"type": "integer", "description": "新固定间隔分钟（可选，与 schedule 互斥）"},
            "input": {"type": "string", "description": "新任务描述（可选）"},
            "system_prompt": {"type": "string", "description": "新角色设定（可选）"},
            "tools": {"type": "array", "items": {"type": "string"}, "description": "新的工具收窄列表（可选，须 ⊆ 作业授权池）"},
            "timeout_seconds": {"type": "integer", "description": "新超时秒数（可选；只能调小）"},
        },
        "required": ["job_id"],
    },
}


def _handle_update_cron(args: Dict[str, Any]) -> str:
    sched = get_scheduler()
    job_id = str(args.get("job_id") or "")
    # 校验阶段基于 get_job 快照读取（_pool 等只读字段）；变更阶段整体
    # 收进 scheduler.update 的锁内 mutate——活字典不再锁外直改
    job = sched.get_job(job_id)
    if job is None:
        return tool_error("job_id 不存在，请用 list_crons 查询有效作业")

    guard = get_cron_tool_config()
    updates: Dict[str, Any] = {}

    if args.get("name") is not None:
        name = str(args["name"]).strip()
        if not name or len(name) > _MAX_NAME_CHARS:
            return tool_error(f"name 应为 1-{_MAX_NAME_CHARS} 字符")
        updates["name"] = name
    if args.get("enabled") is not None:
        updates["enabled"] = bool(args["enabled"])
    if args.get("input") is not None:
        task_input = str(args["input"]).strip()
        if not task_input:
            return tool_error("input 不能为空")
        if len(task_input) > int(guard["max_input_chars"]):
            return tool_error(
                f"input {len(task_input)} 字符超过上限 "
                f"{guard['max_input_chars']}")
        updates["input"] = task_input
    if args.get("system_prompt") is not None:
        updates["system_prompt"] = str(args["system_prompt"]).strip()
    if args.get("tools") is not None:
        err, tools = _resolve_tools(args, list(job.get("_pool") or []))
        if err:
            return tool_error(err)
        updates["tools"] = tools
    if args.get("timeout_seconds") is not None:
        err, timeout = _resolve_timeout(args, guard)
        if err:
            return tool_error(err)
        # "只能调小"的基准是系统上限（_resolve_timeout 已收口），作业可
        # 在上限内自由改（含从更小值调回）
        updates["timeout_seconds"] = timeout

    reschedule = (args.get("schedule") is not None
                  or args.get("interval_minutes") is not None)
    if reschedule:
        err, schedule, _ = _resolve_schedule(args)
        if err:
            return tool_error(err)
        updates["schedule"] = schedule

    if not updates:
        return tool_error("没有任何要更新的字段（name/enabled/schedule/"
                          "input/system_prompt/tools/timeout_seconds）")

    def _mutate(live: Dict[str, Any]) -> None:
        if reschedule or updates.get("enabled") is True:
            # 先在「合并视图」上排程、写穿前拒绝死日期——mutate 抛错时
            # 作业保持原状（scheduler.update 的契约）
            nxt = compute_next_fire({**live, **updates})
            if nxt is None and reschedule:
                raise ValueError(
                    "调度表达式永不命中（如 2 月 30 日 / 非闰年 2 月 29 日），"
                    "请修正 schedule")
            live.update(updates)
            live["next_fire_at"] = nxt
        else:
            live.update(updates)
            if updates.get("enabled") is False:
                live["next_fire_at"] = None   # 暂停：不排下次；恢复时重排

    try:
        updated = sched.update(job_id, _mutate)
    except ValueError as e:
        return tool_error(str(e))
    if updated is None:
        return tool_error("job_id 不存在，请用 list_crons 查询有效作业")
    logger.info("[update_cron] %s: 更新字段 %s", job_id, sorted(updates))
    return tool_result({"job_id": job_id, "updated": sorted(updates),
                        "enabled": updated.get("enabled", True),
                        "next_fire_at": updated.get("next_fire_at")})


# ---------------------------------------------------------------------------
# delete_cron
# ---------------------------------------------------------------------------

DELETE_CRON_SCHEMA = {
    "name": "delete_cron",
    "description": (
        "删除定时作业（不可恢复）。作业 ID 从 list_crons 取。正在执行的"
        "一次触发不会被中断，但之后不再调度。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "作业 ID（list_crons 获取）"},
        },
        "required": ["job_id"],
    },
}


def _handle_delete_cron(args: Dict[str, Any]) -> str:
    job_id = str(args.get("job_id") or "")
    sched = get_scheduler()
    job = sched.remove(job_id)
    if job is None:
        return tool_error(f"job_id 不存在: {job_id!r}")
    logger.info("[delete_cron] %s (%s) 已删除，历史执行 %d 次",
                job_id, job.get("name"), job.get("runs", 0))
    return tool_result({"job_id": job_id, "deleted": True,
                        "name": job.get("name", "")})


# ---------------------------------------------------------------------------
# Self-registration (registered on module import; AST scan auto-discovery —
# 顶层 registry.register() 调用表达式，见 file_tool.py 的同类说明)
# ---------------------------------------------------------------------------

registry.register(
    name="create_cron",
    toolset="cron",
    schema=CREATE_CRON_SCHEMA,
    handler=_handle_create_cron,
    description="创建定时作业（cron/间隔，授权快照，持久化）",
    emoji="⏰",
)

registry.register(
    name="list_crons",
    toolset="cron",
    schema=LIST_CRONS_SCHEMA,
    handler=_handle_list_crons,
    description="列出定时作业与执行历史",
    emoji="📅",
)

registry.register(
    name="update_cron",
    toolset="cron",
    schema=UPDATE_CRON_SCHEMA,
    handler=_handle_update_cron,
    description="更新定时作业（改调度/启停/任务/工具）",
    emoji="🔁",
)

registry.register(
    name="delete_cron",
    toolset="cron",
    schema=DELETE_CRON_SCHEMA,
    handler=_handle_delete_cron,
    description="删除定时作业",
    emoji="🗑️",
)
