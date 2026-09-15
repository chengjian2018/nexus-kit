"""Cron 调度共享内核 —— cron_tool 的执行原语（无顶层 registry.register，
AST 发现不 import 本模块；由 atoms/tools/cron_tool.py 引入）。

三件事：

1. **cron 表达式子集解析 + next_fire 计算**：标准 5 字段（分 时 日 月
   周，宿主本地时间），每字段支持 ``*`` / ``*/n`` / ``a`` / ``a-b`` /
   ``a-b/n`` 及逗号组合；dow 0-7（7≡0=周日）。日/周同时受限时按 Vixie
   cron 的 OR 语义。next_fire 从给定时刻起逐分钟推进（上限一年，防
   2/30 这类死日期死循环）。不支持秒级、时区名与 @alias——LLM 生成
   5 字段串即可覆盖常见调度；更复杂的语义交给宿主 crontab。
2. **JSON 持久化仓**：data/cron_jobs.json（config ``cron_tool.jobs_path``
   可改），原子写（tmp+rename）；启动时加载并跳过停机期间错过的触发
   （重算 next_fire，不补跑）。
3. **调度器单例**：host 启动时 ``ensure_scheduler()`` 拉起 tick 循环
   （默认 20s 一拍，只比较 next_fire_at，到点 spawn fire 任务）；fire
   = 授权快照工具池上的 delegate 式子代理执行（_subagent_core，与
   delegate_task 同一原语），整体受 fire_timeout 包裹，同一 job 不
   重入（前一跑未结束则本拍跳过）。

授权模型：job 创建时刻把当时 pattern 的 ``allow_toolset`` 解析成具体
工具名冻结进 job（剔除 subagent/workflow/cron 三个编排工具集，结构性
防"编排套编排/自复制"）；fire 时按快照池执行，权限永不越出创建者的
授权边界。llm_config 在 fire 时刻现解析（配置热更新生效）。

测试隔离：环境变量 ``NEXUS_CRON_DISABLED=1``（tests/conftest.py 与
NEXUS_MCP_DISABLED 同款模式）让 ensure_scheduler 直接 no-op、仓不读
不写真实文件——测试进程绝不会真触发 LLM 调用或碰宿主的 jobs 文件。
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from nexus.registry.tools import registry as tool_registry
from nexus.settings import get_cron_tool_config

logger = logging.getLogger(__name__)

# fire 子代理的工具池永不包含这三个编排工具集（防递归/自复制）
_EXCLUDED_TOOLSETS = frozenset({"subagent", "workflow", "cron"})

# 逐分钟扫描的硬上限（一年）：2/30、2/29（非闰年）这类死日期到头即放弃
_MAX_SCAN_MINUTES = 366 * 24 * 60

_CRON_FIELD_RANGES = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 7),   # 7 ≡ 0 ≡ 周日，存集合时归一到 0-6
)

_CRON_FIELD_RE = re.compile(r"^(?:\*|\d+|\d+-\d+)(?:/\d+)?$")


def _disabled() -> bool:
    return os.environ.get("NEXUS_CRON_DISABLED", "") == "1"


# =========================================================================
# 1. cron 表达式解析 + next_fire
# =========================================================================

def parse_cron_field(field: str, low: int, high: int,
                     name: str) -> Set[int]:
    """解析单个字段为合法值集合（``*`` / ``*/n`` / ``a`` / ``a-b`` /
    ``a-b/n`` 及逗号组合）。day_of_week 的 7 归一为 0。"""
    values: Set[int] = set()
    for part in str(field).split(","):
        part = part.strip()
        if not part or not _CRON_FIELD_RE.match(part):
            raise ValueError(
                f"cron {name} 字段非法: {field!r}（支持 * / */n / a / a-b / a-b/n 及逗号组合）")
        step = 1
        body = part
        if "/" in part:
            body, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                raise ValueError(f"cron {name} 字段步长必须 ≥ 1: {part!r}")
        if body == "*":
            start, end = low, high
        elif "-" in body:
            start_s, end_s = body.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(body)
            if "/" in part:   # "a/n" 语义上等于 a..high/n（Vixie 允许，罕见）
                end = high
        if start < low or end > high or start > end:
            raise ValueError(
                f"cron {name} 字段取值越界: {part!r}（合法范围 {low}-{high}）")
        values.update(range(start, end + 1, step))
    if name == "day_of_week" and 7 in values:
        values.discard(7)
        values.add(0)
    if not values:
        raise ValueError(f"cron {name} 字段解析结果为空: {field!r}")
    return values


def parse_cron(expr: str) -> Dict[str, Set[int]]:
    """解析 5 字段 cron 表达式 → {field: 合法值集合}。"""
    parts = str(expr).split()
    if len(parts) != 5:
        raise ValueError(
            f"cron 表达式应为 5 个空格分隔的字段（分 时 日 月 周）: {expr!r}")
    return {
        name: parse_cron_field(part, low, high, name)
        for part, (name, low, high) in zip(parts, _CRON_FIELD_RANGES)
    }


def next_fire(parsed: Dict[str, Set[int]],
              after: datetime) -> Optional[datetime]:
    """从 after 起逐分钟找下一个命中时刻（秒归零；不含 after 本身）。

    日/周同时受限时按 Vixie cron 的 OR 语义（其一为全集时另一个独立
    生效）。扫描上限一年，超限返回 None（死日期）。
    """
    dom_open = len(parsed["day_of_month"]) == 31
    dow_open = len(parsed["day_of_week"]) == 7
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(_MAX_SCAN_MINUTES):
        if (candidate.minute in parsed["minute"]
                and candidate.hour in parsed["hour"]
                and candidate.month in parsed["month"]):
            dom_hit = candidate.day in parsed["day_of_month"]
            dow_hit = (candidate.weekday() + 1) % 7 in parsed["day_of_week"]
            if (dom_hit and dow_hit) if (dom_open or dow_open) \
                    else (dom_hit or dow_hit):
                return candidate
        candidate += timedelta(minutes=1)
    return None


def next_fire_at(parsed: Dict[str, Set[int]], now: Optional[float] = None
                 ) -> Optional[float]:
    """next_fire 的 epoch 包装（None 传导）。"""
    fire = next_fire(parsed, datetime.fromtimestamp(
        now if now is not None else time.time()))
    return fire.timestamp() if fire is not None else None


def compute_next_fire(job: Dict[str, Any], now: Optional[float] = None
                      ) -> Optional[float]:
    """按 job 的调度方式（cron / interval）算下一次触发时刻。"""
    ts = now if now is not None else time.time()
    schedule = job.get("schedule") or {}
    if "cron" in schedule:
        return next_fire_at(parse_cron(schedule["cron"]), ts)
    interval = float(schedule.get("interval_minutes", 0))
    if interval <= 0:
        return None
    return ts + interval * 60.0


# =========================================================================
# 2. JSON 持久化仓
# =========================================================================

class CronStore:
    """jobs JSON 文件仓：load（不存在/损坏 → 空）+ save（原子写）。

    文件极小（≤ max_jobs 条），同步 IO 直写（handler 跑在 to_thread；
    fire 侧在事件循环上写毫秒级小文件，可接受）。
    """

    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning("[cron] jobs 文件不可读，按空处理（%s）: %s",
                           self.path, e)
            return {}
        if not isinstance(data, dict):
            logger.warning("[cron] jobs 文件结构非法，按空处理: %s", self.path)
            return {}
        return {jid: job for jid, job in data.items()
                if isinstance(job, dict) and isinstance(jid, str)}

    def save(self, jobs: Dict[str, Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)


# =========================================================================
# 3. 调度器
# =========================================================================

async def default_execute_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """fire 默认执行器：授权快照池上的 delegate 式子代理循环。

    llm_config 在 fire 时刻解析（热更新生效）；工具池是 job.tools 冻结
    快照（创建时已剔除编排工具集）。
    """
    from atoms.tools._subagent_core import (  # 延迟导入防环
        _DEFAULT_SYSTEM_PROMPT,
        _run_sub_agent,
    )
    from nexus.llm.resolve import build_provider
    from nexus.settings import get_llm_config

    llm_config = get_llm_config()
    guard = get_cron_tool_config()
    state: Dict[str, Any] = {}
    payload = await _run_sub_agent(
        provider=build_provider(llm_config),
        llm_config=llm_config,
        system_prompt=str(job.get("system_prompt") or "").strip()
        or _DEFAULT_SYSTEM_PROMPT,
        task=job["input"],
        granted=set(job.get("tools") or []),
        temperature=llm_config.get("temperature", 0.7),
        max_rounds=int(guard["max_rounds"]),
        state=state)
    return payload


class CronScheduler:
    """tick 循环 + fire 编排（jobs 存内存态 dict，每次变更即持久化）。

    无事件循环也能用（CRUD 直改 jobs + save）；tick 循环只在
    ensure_scheduler 后存在。同 job 不重入：fire 进行中本拍跳过。
    """

    def __init__(self, execute_job: Callable = default_execute_job):
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self._execute_job = execute_job
        self._tick_task: Optional[asyncio.Task] = None
        self._firing: Set[str] = set()
        self._fire_tasks: Set[asyncio.Task] = set()
        self._lock = threading.Lock()

    # -- 生命周期 --------------------------------------------------------

    def load(self) -> None:
        """启动加载：读仓 + 跳过错过的触发（重算 next，不补跑）。"""
        guard = get_cron_tool_config()
        self.jobs = CronStore(guard["jobs_path"]).load()
        now = time.time()
        for job in self.jobs.values():
            if not job.get("enabled", True):
                continue
            nxt = job.get("next_fire_at")
            if nxt is None or nxt < now:
                job["next_fire_at"] = compute_next_fire(job, now)

    async def start(self) -> None:
        if self._tick_task is not None and not self._tick_task.done():
            return
        self._tick_task = asyncio.get_running_loop().create_task(
            self._tick_loop(), name="nexus-cron-tick")

    async def stop(self) -> None:
        """停机回收：等 tick 退出，并取消在途 fire（LLM 调用）——
        fire 任务被追踪，不再在宿主拆除时无人认领地继续跑。"""
        tick, self._tick_task = self._tick_task, None
        if tick is not None:
            tick.cancel()
            try:
                await tick
            except (asyncio.CancelledError, Exception):
                pass
        pending = list(self._fire_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _tick_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(
                    int(get_cron_tool_config()["tick_seconds"]))
                self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[cron] tick 异常（继续循环）")

    def _tick_once(self) -> None:
        now = time.time()
        with self._lock:
            due = [
                jid for jid, job in self.jobs.items()
                if job.get("enabled", True)
                and job.get("next_fire_at") is not None
                and job["next_fire_at"] <= now
                and jid not in self._firing
            ]
            for jid in due:
                self._firing.add(jid)
        for jid in due:
            task = asyncio.get_running_loop().create_task(
                self._fire(jid), name=f"nexus-cron-fire-{jid}")
            self._fire_tasks.add(task)
            task.add_done_callback(self._fire_tasks.discard)

    # -- fire -------------------------------------------------------------

    async def _fire(self, job_id: str) -> None:
        try:
            with self._lock:
                job = self.jobs.get(job_id)
                if job is None or not job.get("enabled", True):
                    return
                guard = get_cron_tool_config()
                # 触发即排下一次（先排后跑：跑挂了也不丢后续调度）
                job["next_fire_at"] = compute_next_fire(job)
                job["runs"] = int(job.get("runs", 0)) + 1
                started = time.time()
                self._persist_locked(guard)

            timeout = float(job.get("timeout_seconds")
                            or guard["fire_timeout_seconds"])
            logger.info("[cron] fire: job=%s(%s) run#%d timeout=%.0fs",
                        job_id, job.get("name"), job["runs"], timeout)
            timed_out = False
            error: Optional[str] = None
            try:
                # 挂起会话作用域：fire 的子代理工具调用（如 task_list）
                # 以 cron:<job_id> 隔离——否则全部无上下文调用共享 _global
                # 桶，并发 fire 的 write_tasks 全量替换会互踩（llm/授权
                # 字段留空：fire 子代理池按冻结快照名字直发，不读这两项；
                # 同步 with 即可——contextvar 在同一 Task 内跨 await 生效）
                from nexus.engine.tool_context import tool_call_context
                with tool_call_context(
                        llm_config={}, allow_toolsets=set(),
                        session_id=f"cron:{job_id}"):
                    payload = await asyncio.wait_for(
                        self._execute_job(job), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True
                payload, error = None, f"触发超时（{timeout:.0f}s）被终止"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                payload, error = None, f"{type(e).__name__}: {e}"

            record = {
                "started_at": datetime.fromtimestamp(started).isoformat(
                    timespec="seconds"),
                "status": ("timeout" if timed_out
                           else "error" if error
                           else str((payload or {}).get("status", "ok"))),
                "content": (payload or {}).get("content", "")[:int(
                    guard["max_result_chars"])],
                "rounds": (payload or {}).get("rounds"),
                "usage": (payload or {}).get("usage"),
                "elapsed_seconds": round(time.time() - started, 1),
            }
            if error:
                record["error"] = error

            with self._lock:
                job = self.jobs.get(job_id)
                if job is not None:
                    job["last_run"] = record
                    history = job.setdefault("history", [])
                    history.append(record)
                    del history[:-int(guard["history_cap"])]
                    self._persist_locked(guard)
            logger.info("[cron] fire 完成: job=%s, status=%s, elapsed=%.1fs",
                        job_id, record["status"], record["elapsed_seconds"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[cron] fire 编排异常: job=%s", job_id)
        finally:
            with self._lock:
                self._firing.discard(job_id)

    # -- CRUD（工具 handler 走这里） ------------------------------------

    def _persist_locked(self, guard: Dict[str, Any]) -> None:
        if _disabled():
            return
        CronStore(guard["jobs_path"]).save(self.jobs)

    def add(self, job: Dict[str, Any]) -> None:
        """新增/覆盖一个作业（update_cron 复用覆盖语义）。

        max_jobs 上限在锁内检查：锁外的先查后加在并发 create 下会双双
        通过、超限写入。新增（id 不存在）超限时抛 ValueError。"""
        with self._lock:
            guard = get_cron_tool_config()
            if (job["id"] not in self.jobs
                    and len(self.jobs) >= int(guard["max_jobs"])):
                raise ValueError(
                    f"作业数已达上限 {guard['max_jobs']}，"
                    f"请先用 delete_cron 清理不再需要的作业")
            self.jobs[job["id"]] = job
            self._persist_locked(guard)

    def update(self, job_id: str,
               mutate: Callable[[Dict[str, Any]], None]
               ) -> Optional[Dict[str, Any]]:
        """锁内「读取-变更-持久化」：活字典不再在锁外被直改——半更新
        状态若被并发的 _fire 持久化，会把过期的 next_fire_at 固化到盘上
        （重启后 load 只在 nxt < now 时重算，作业会沉睡到旧的未来时刻）。
        mutate 抛 ValueError 视为校验失败，作业保持原状。"""
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            mutate(job)
            self._persist_locked(get_cron_tool_config())
            return job

    def list_jobs(self) -> List[Dict[str, Any]]:
        """锁内快照（history 单独拷贝）：CRUD 与 tick/fire 并发变更时，
        调用方在锁外排序/渲染不再迭代活容器。"""
        with self._lock:
            snapshot = []
            for job in self.jobs.values():
                view = dict(job)
                view["history"] = list(job.get("history") or [])
                snapshot.append(view)
            return snapshot

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.jobs.get(job_id)

    def remove(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self.jobs.pop(job_id, None)
            if job is not None:
                self._persist_locked(get_cron_tool_config())
            return job


# =========================================================================
# 单例管理
# =========================================================================

_SCHEDULER: Optional[CronScheduler] = None
_SINGLETON_LOCK = threading.Lock()


def get_scheduler() -> CronScheduler:
    """惰性单例：首次创建即 load（NEXUS_CRON_DISABLED 时空仓不读文件）。"""
    global _SCHEDULER
    with _SINGLETON_LOCK:
        if _SCHEDULER is None:
            sched = CronScheduler()
            if not _disabled():
                sched.load()
            _SCHEDULER = sched
        return _SCHEDULER


async def ensure_scheduler() -> CronScheduler:
    """host 启动接线：取单例并拉起 tick 循环（DISABLED 时 no-op）。"""
    sched = get_scheduler()
    if not _disabled():
        await sched.start()
    return sched


async def stop_scheduler() -> None:
    sched = _SCHEDULER
    if sched is not None:
        await sched.stop()


def reset_scheduler() -> None:
    """测试辅助：丢弃单例（下次 get_scheduler 重新 load）。"""
    global _SCHEDULER
    with _SINGLETON_LOCK:
        _SCHEDULER = None


def new_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:8]


def snapshot_tool_pool(allow_toolsets) -> List[str]:
    """把 pattern 授权工具集冻结成工具名快照（剔除编排工具集）。"""
    wanted = set(allow_toolsets or []) - _EXCLUDED_TOOLSETS
    return sorted(tool_registry.names_in_toolsets(wanted))
