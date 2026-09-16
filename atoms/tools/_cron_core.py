"""Cron scheduling shared kernel — the execution primitives behind cron_tool
(no top-level registry.register, so AST discovery never imports this module;
it is imported by atoms/tools/cron_tool.py).

Three things:

1. **Cron-expression subset parsing + next_fire computation**: the standard
   5 fields (minute hour day month dow, host local time), each field
   supporting ``*`` / ``*/n`` / ``a`` / ``a-b`` / ``a-b/n`` and comma
   combinations; dow 0-7 (7≡0=Sunday). When day and weekday are both
   restricted, Vixie cron's OR semantics apply. next_fire advances
   minute-by-minute from the given moment (capped at one year, preventing
   infinite loops on dead dates like 2/30). No seconds, timezone names, or
   @alias — an LLM-generated 5-field string covers common scheduling;
   more complex semantics belong to the host crontab.
2. **JSON persistence store**: data/cron_jobs.json (config
   ``cron_tool.jobs_path``), atomic write (tmp+rename); loaded at startup,
   skipping fires missed while down (next_fire recomputed, no replay).
3. **Scheduler singleton**: the host startup calls ``ensure_scheduler()``
   to start the tick loop (20s cadence by default, only comparing
   next_fire_at, spawning a fire task when due); fire = a delegate-style
   sub-agent execution over the authorization-snapshot tool pool
   (_subagent_core, the same primitive as delegate_task), wrapped by
   fire_timeout, with no re-entry per job (a still-running job skips this
   tick).

Authorization model: at job creation the pattern's ``allow_toolset`` is
resolved into concrete tool names and frozen into the job (minus the
subagent/workflow/cron orchestration toolsets, structurally preventing
"orchestration inside orchestration" / self-copying), and the creating
pattern's ``pattern_code`` is frozen too (the app-overlay locator key for
fire-time guardrails and LLM config; old job files missing the field fall
back to "" = global); fire executes against the snapshot pool — permissions
never exceed the creator's granted boundary.
llm_config is resolved fresh at fire time (config hot-reload applies).

Test isolation: the env var ``NEXUS_CRON_DISABLED=1`` (same pattern as
NEXUS_MCP_DISABLED in tests/conftest.py) makes ensure_scheduler a no-op and
keeps the store from reading/writing real files — the test process never
actually triggers LLM calls or touches the host's jobs file.
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

from nexus.engine.tool_context import ambient_pattern_code
from nexus.registry.tools import registry as tool_registry
from nexus.settings import get_cron_tool_config

logger = logging.getLogger(__name__)

# The fire sub-agent's tool pool never includes these three orchestration toolsets (anti-recursion / self-copying)
_EXCLUDED_TOOLSETS = frozenset({"subagent", "workflow", "cron"})

# Hard cap for the minute-by-minute scan (one year): dead dates like 2/30 or
# 2/29 (non-leap) give up at the end
_MAX_SCAN_MINUTES = 366 * 24 * 60

_CRON_FIELD_RANGES = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 7),   # 7 ≡ 0 ≡ Sunday, normalized to 0-6 when stored
)

_CRON_FIELD_RE = re.compile(r"^(?:\*|\d+|\d+-\d+)(?:/\d+)?$")


def _disabled() -> bool:
    return os.environ.get("NEXUS_CRON_DISABLED", "") == "1"


# =========================================================================
# 1. cron expression parsing + next_fire
# =========================================================================

def parse_cron_field(field: str, low: int, high: int,
                     name: str) -> Set[int]:
    """Parse one field into its legal value set (``*`` / ``*/n`` / ``a`` /
    ``a-b`` / ``a-b/n`` and comma combinations). day_of_week's 7 normalizes to 0."""
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
            if "/" in part:   # "a/n" semantically equals a..high/n (Vixie allows it; rare)
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
    """Parse a 5-field cron expression → {field: legal value set}."""
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
    """Find the next hit minute-by-minute from after (seconds zeroed; after itself excluded).

    When day and weekday are both restricted, Vixie cron's OR semantics
    apply (either being the full set makes the other independently
    effective). The scan caps at one year; past it returns None (a dead
    date).
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
    """Epoch wrapper of next_fire (None propagates)."""
    fire = next_fire(parsed, datetime.fromtimestamp(
        now if now is not None else time.time()))
    return fire.timestamp() if fire is not None else None


def compute_next_fire(job: Dict[str, Any], now: Optional[float] = None
                      ) -> Optional[float]:
    """Compute the next fire time per the job's schedule kind (cron / interval)."""
    ts = now if now is not None else time.time()
    schedule = job.get("schedule") or {}
    if "cron" in schedule:
        return next_fire_at(parse_cron(schedule["cron"]), ts)
    interval = float(schedule.get("interval_minutes", 0))
    if interval <= 0:
        return None
    return ts + interval * 60.0


# =========================================================================
# 2. JSON persistence store
# =========================================================================

class CronStore:
    """jobs JSON file store: load (missing/corrupt → empty) + save (atomic write).

    The file is tiny (≤ max_jobs entries), so synchronous IO is written
    directly (handlers run on to_thread; the fire side writes a
    millisecond-scale file on the event loop — acceptable).
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
# 3. Scheduler
# =========================================================================

async def default_execute_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """The default fire executor: a delegate-style sub-agent loop over the authorization-snapshot pool.

    llm_config and guardrails resolve at fire time via the job's frozen
    ``pattern_code`` (app-overlay hot-reload applies; the locator key is the
    creation-time frozen value); the tool pool is the job.tools frozen
    snapshot (orchestration toolsets already removed at creation).
    """
    from atoms.tools._subagent_core import (  # lazy import against a cycle
        _DEFAULT_SYSTEM_PROMPT,
        _run_sub_agent,
    )
    from nexus.llm.resolve import build_provider
    from nexus.settings import get_llm_config

    pattern_code = str(job.get("pattern_code") or "")
    llm_config = get_llm_config(pattern_code=pattern_code)
    guard = get_cron_tool_config(pattern_code)
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
    """Tick loop + fire orchestration (jobs live in an in-memory dict, persisted on every change).

    Usable without an event loop (CRUD mutates jobs + saves directly); the
    tick loop exists only after ensure_scheduler. No re-entry per job: a
    fire in progress skips this tick.
    """

    def __init__(self, execute_job: Callable = default_execute_job):
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self._execute_job = execute_job
        self._tick_task: Optional[asyncio.Task] = None
        self._firing: Set[str] = set()
        self._fire_tasks: Set[asyncio.Task] = set()
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------

    def load(self) -> None:
        """Startup load: read the store + skip fires missed while down (recompute next, no replay)."""
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
        """Shutdown reclamation: wait for the tick to exit and cancel in-flight fires (LLM calls) —
        fire tasks are tracked, no longer running unclaimed through host teardown."""
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
                # Fire-time guardrails resolve via the job's frozen
                # pattern_code (no frozen value = an old job file, falling
                # back to the global section); the scheduler loop body
                # (tick/load) runs outside any session, so global resolution
                # is the correct behavior
                guard = get_cron_tool_config(str(job.get("pattern_code") or ""))
                # Schedule the next fire immediately (schedule-then-run: a
                # crashed run never loses subsequent scheduling)
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
                # Suspend a session scope: the fire sub-agent's tool calls
                # (e.g. task_list) are isolated under cron:<job_id> —
                # otherwise all context-less calls would share the _global
                # bucket and concurrent fires' write_tasks whole-replacements
                # would stomp each other (llm/authorization fields left
                # empty: the fire sub-agent pool dispatches by the frozen
                # snapshot names and never reads those two; a plain with is
                # enough — the contextvar lives across awaits within one Task)
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

    # -- CRUD (the tool handlers go through here) ------------------------------------

    def _persist_locked(self, guard: Dict[str, Any]) -> None:
        if _disabled():
            return
        CronStore(guard["jobs_path"]).save(self.jobs)

    def add(self, job: Dict[str, Any]) -> None:
        """Add or overwrite a job (update_cron reuses the overwrite semantics).

        The max_jobs cap is checked inside the lock: a check-then-add
        outside the lock would double-pass under concurrent creates and
        write over the cap. A new (absent id) job over the cap raises
        ValueError. Guardrails resolve via the calling handler's pattern
        app overlay."""
        with self._lock:
            guard = get_cron_tool_config(ambient_pattern_code())
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
        """Lock-held "read-mutate-persist": the live dict is never modified outside the lock —
        a half-updated state persisted by a concurrent _fire would bake a
        stale next_fire_at to disk (after restart, load only recomputes when
        nxt < now, so the job would sleep until that old future moment).
        A ValueError from mutate counts as failed validation; the job stays
        as it was."""
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            mutate(job)
            self._persist_locked(get_cron_tool_config())
            return job

    def list_jobs(self) -> List[Dict[str, Any]]:
        """Lock-held snapshot (history copied separately): callers sort/render outside the lock
        without iterating a live container while CRUD and tick/fire mutate
        concurrently."""
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
# Singleton management
# =========================================================================

_SCHEDULER: Optional[CronScheduler] = None
_SINGLETON_LOCK = threading.Lock()


def get_scheduler() -> CronScheduler:
    """Lazy singleton: created with load (an empty store, no file read, when NEXUS_CRON_DISABLED)."""
    global _SCHEDULER
    with _SINGLETON_LOCK:
        if _SCHEDULER is None:
            sched = CronScheduler()
            if not _disabled():
                sched.load()
            _SCHEDULER = sched
        return _SCHEDULER


async def ensure_scheduler() -> CronScheduler:
    """Host startup wiring: take the singleton and start the tick loop (a no-op when DISABLED)."""
    sched = get_scheduler()
    if not _disabled():
        await sched.start()
    return sched


async def stop_scheduler() -> None:
    sched = _SCHEDULER
    if sched is not None:
        await sched.stop()


def reset_scheduler() -> None:
    """Test helper: drop the singleton (the next get_scheduler reloads)."""
    global _SCHEDULER
    with _SINGLETON_LOCK:
        _SCHEDULER = None


def new_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:8]


def snapshot_tool_pool(allow_toolsets) -> List[str]:
    """Freeze the pattern's granted toolsets into a tool-name snapshot (orchestration toolsets removed)."""
    wanted = set(allow_toolsets or []) - _EXCLUDED_TOOLSETS
    return sorted(tool_registry.names_in_toolsets(wanted))
