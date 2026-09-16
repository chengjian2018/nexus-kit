"""Unit tests for cron scheduling (cron tool + _cron_core kernel):

- Cron-expression subset parsing and next_fire computation (dead dates,
  dow normalization, OR semantics)
- CRUD four tools: cron/interval dual scheduling, authorization snapshot
  (orchestration toolsets stripped), args.tools may only narrow, guardrail
  caps, update partial updates and pause/resume
- CronStore JSON persistence roundtrip (atomic writes, corrupt files
  degrade to empty)
- fire path: a stub executor is injected (no real LLM) to verify history
  records, next-fire rescheduling, timeout and exception paths, reentry
  skipping, and that _pool does not leak
"""

import asyncio
import json
from datetime import datetime
from unittest.mock import patch

import pytest

from atoms.tools import cron_tool  # noqa: F401 -- module import performs the registration
from atoms.tools import knowledge_tool  # noqa: F401 -- the snapshot-pool assertions depend on its registration
from atoms.tools import _cron_core
from atoms.tools._cron_core import (
    CronScheduler,
    CronStore,
    compute_next_fire,
    get_scheduler,
    new_job_id,
    next_fire,
    parse_cron,
    reset_scheduler,
    snapshot_tool_pool,
)
from nexus.engine.tool_context import current_tool_context, tool_call_context
from nexus.registry.tools import registry as tool_registry
from async_utils import arun

_GUARD = {"max_jobs": 20, "fire_timeout_seconds": 300, "max_rounds": 8,
          "history_cap": 10, "max_input_chars": 8000,
          "max_result_chars": 4000, "jobs_path": "/unused/cron.json",
          "tick_seconds": 20}


@pytest.fixture(autouse=True)
def _fresh_scheduler():
    """Isolated scheduler singleton + uniform guardrail patch (both
    reference namespaces covered)."""
    reset_scheduler()
    with patch("atoms.tools._cron_core.get_cron_tool_config",
               return_value=dict(_GUARD)), \
         patch("atoms.tools.cron_tool.get_cron_tool_config",
               return_value=dict(_GUARD)):
        yield


def _dispatch(name, args, allow_toolsets=None):
    ctx = tool_call_context({"code": "x", "model": "m"},
                            allow_toolsets or ["cron", "knowledge",
                                               "subagent"])
    with ctx:
        return json.loads(arun(tool_registry.dispatch(name, args)))


# =========================================================================
# cron parsing and next_fire
# =========================================================================

def test_parse_cron_shapes():
    assert parse_cron("* * * * *")["minute"] == set(range(60))
    assert parse_cron("*/15 * * * *")["minute"] == {0, 15, 30, 45}
    assert parse_cron("0 9 * * 1-5")["day_of_week"] == {1, 2, 3, 4, 5}
    # dow 7 normalizes to 0 (Sunday)
    assert parse_cron("0 0 * * 7")["day_of_week"] == {0}
    assert parse_cron("1-10/3 * * * *")["minute"] == {1, 4, 7, 10}


def test_parse_cron_errors():
    for bad in ("0 9 * *",                # 4 fields
                "61 * * * *",             # minute out of range
                "* 24 * * *",             # hour out of range
                "a b c d e",              # non-numeric
                "*/0 * * * *",            # step of 0
                "5-2 * * * *"):           # inverted range
        with pytest.raises(ValueError):
            parse_cron(bad)


def test_next_fire_weekday_morning():
    # 2026-09-11 is a Friday: after Fri 10:00 -> the next weekday 9am is the following Monday
    parsed = parse_cron("0 9 * * 1-5")
    after = datetime(2026, 9, 11, 10, 0)
    fire = next_fire(parsed, after)
    assert (fire.year, fire.month, fire.day, fire.hour, fire.minute) == \
        (2026, 9, 14, 9, 0)
    assert fire.weekday() == 0   # Monday


def test_next_fire_minute_step_and_month_day():
    parsed = parse_cron("*/15 * * * *")
    fire = next_fire(parsed, datetime(2026, 9, 11, 9, 7))
    assert (fire.hour, fire.minute) == (9, 15)

    # 1st of every month at 4:30
    parsed = parse_cron("30 4 1 * *")
    fire = next_fire(parsed, datetime(2026, 9, 2, 0, 0))
    assert (fire.month, fire.day, fire.hour, fire.minute) == (10, 1, 4, 30)


def test_next_fire_dead_date_returns_none():
    # Feb 30 does not exist -> gives up after scanning a year
    parsed = parse_cron("0 0 30 2 *")
    assert next_fire(parsed, datetime(2026, 9, 1)) is None


def test_compute_next_fire_interval():
    job = {"schedule": {"interval_minutes": 30}}
    assert compute_next_fire(job, now=1000.0) == 1000.0 + 1800.0


# =========================================================================
# CRUD
# =========================================================================

def test_registered_in_cron_toolset():
    for name in ("create_cron", "list_crons", "update_cron", "delete_cron"):
        assert tool_registry.get_toolset_for_tool(name) == "cron"
    assert {"create_cron", "list_crons", "update_cron",
            "delete_cron"} <= tool_registry.names_in_toolsets({"cron"})


def test_create_and_list_cron():
    r = _dispatch("create_cron", {
        "name": "每日简报", "schedule": "0 9 * * *",
        "input": "汇总昨日数据并生成简报"})
    assert r["job_id"].startswith("job_")
    assert r["schedule"] == {"cron": "0 9 * * *"}
    assert r["next_fire_at"] is not None

    r = _dispatch("list_crons", {})
    assert r["count"] == 1
    job = r["jobs"][0]
    assert job["name"] == "每日简报"
    assert "_pool" not in job          # internal fields do not leak


def test_create_cron_interval_and_validation():
    r = _dispatch("create_cron", {"name": "巡检",
                                  "interval_minutes": 30,
                                  "input": "巡检磁盘水位"})
    assert r["schedule"] == {"interval_minutes": 30}

    # schedule and interval are mutually exclusive
    r = _dispatch("create_cron", {"name": "x", "schedule": "0 9 * * *",
                                  "interval_minutes": 5, "input": "t"})
    assert "二选一" in r["error"]
    # missing schedule expression
    r = _dispatch("create_cron", {"name": "x", "input": "t"})
    assert "调度" in r["error"]
    # a bad cron expression errors immediately
    r = _dispatch("create_cron", {"name": "x", "schedule": "not cron",
                                  "input": "t"})
    assert "cron" in r["error"]
    # interval out of range
    r = _dispatch("create_cron", {"name": "x", "interval_minutes": 0,
                                  "input": "t"})
    assert "interval_minutes" in r["error"]
    # missing input
    r = _dispatch("create_cron", {"name": "x", "schedule": "0 9 * * *"})
    assert "input" in r["error"]


def test_create_cron_tool_snapshot_excludes_orchestration():
    """Authorization snapshot: ambient grants minus subagent/workflow/cron;
    args.tools may only narrow."""
    r = _dispatch("create_cron", {"name": "带工具",
                                  "interval_minutes": 60, "input": "查资料"},
                  allow_toolsets=["cron", "knowledge", "subagent"])
    job = get_scheduler().get_job(r["job_id"])
    # cron/subagent stripped, knowledge tools retained
    pool = set(job["_pool"])
    assert "delegate_task" not in pool
    assert "create_cron" not in pool
    assert pool >= {"search_product_knowledge", "search_customer_service_knowledge",
                    "list_products", "send_goods_link"}

    # narrowing to a non-existent tool -> error listing the available pool
    r = _dispatch("create_cron", {
        "name": "x", "interval_minutes": 60, "input": "t",
        "tools": ["bash", "not_in_pool"]})
    assert "不在本作业可用池中" in r["error"]
    # valid narrowing
    r = _dispatch("create_cron", {
        "name": "y", "interval_minutes": 60, "input": "t",
        "tools": ["search_product_knowledge"]})
    assert r["tools"] == ["search_product_knowledge"]


def test_create_cron_max_jobs_cap():
    for i in range(3):
        _dispatch("create_cron", {"name": f"j{i}",
                                  "interval_minutes": 60, "input": "t"})
    with patch("atoms.tools._cron_core.get_cron_tool_config",
               return_value={**_GUARD, "max_jobs": 3}), \
         patch("atoms.tools.cron_tool.get_cron_tool_config",
               return_value={**_GUARD, "max_jobs": 3}):
        r = _dispatch("create_cron", {"name": "over",
                                      "interval_minutes": 60, "input": "t"})
        assert "上限" in r["error"]


def test_update_and_delete_cron():
    job_id = _dispatch("create_cron", {
        "name": "原名", "schedule": "0 9 * * *", "input": "原任务"})["job_id"]

    # partial update: rename + pause
    r = _dispatch("update_cron", {"job_id": job_id, "name": "新名",
                                  "enabled": False})
    assert r["updated"] == ["enabled", "name"]
    assert r["enabled"] is False and r["next_fire_at"] is None
    # resume + switch to interval scheduling -> next_fire rescheduled
    r = _dispatch("update_cron", {"job_id": job_id, "enabled": True,
                                  "interval_minutes": 15})
    assert r["next_fire_at"] is not None
    job = get_scheduler().get_job(job_id)
    assert job["schedule"] == {"interval_minutes": 15}
    # tools narrowing is still constrained by the snapshot pool
    r = _dispatch("update_cron", {"job_id": job_id,
                                  "tools": ["not_in_pool"]})
    assert "可用池" in r["error"]
    # no fields to update
    r = _dispatch("update_cron", {"job_id": job_id})
    assert "没有" in r["error"]
    # ghost job_id
    r = _dispatch("update_cron", {"job_id": "job_ghost", "name": "x"})
    assert "不存在" in r["error"]

    r = _dispatch("delete_cron", {"job_id": job_id})
    assert r["deleted"] is True
    r = _dispatch("delete_cron", {"job_id": job_id})
    assert "不存在" in r["error"]
    assert _dispatch("list_crons", {})["count"] == 0


# =========================================================================
# Persistence store
# =========================================================================

def test_cron_store_roundtrip(tmp_path):
    store = CronStore(str(tmp_path / "jobs.json"))
    jobs = {"job_a": {"id": "job_a", "name": "a", "runs": 1}}
    store.save(jobs)
    assert store.load() == jobs
    # writable even when the parent directory does not exist (auto-created)
    store2 = CronStore(str(tmp_path / "deep" / "sub" / "jobs.json"))
    store2.save(jobs)
    assert store2.load() == jobs
    # corrupt file -> empty
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert CronStore(str(tmp_path / "broken.json")).load() == {}
    # missing file -> empty
    assert CronStore(str(tmp_path / "missing.json")).load() == {}


# =========================================================================
# fire path (stub executor injected, no real LLM)
# =========================================================================

def _make_job(**overrides):
    job = {"id": new_job_id(), "name": "t", "enabled": True,
           "schedule": {"interval_minutes": 60}, "input": "task",
           "system_prompt": "", "tools": [], "_pool": [],
           "timeout_seconds": 5, "created_at": "2026-09-14T00:00:00",
           "runs": 0, "next_fire_at": 1.0,   # already overdue
           "last_run": None, "history": []}
    job.update(overrides)
    return job


def test_fire_records_history_and_reschedules():
    calls = []

    async def stub(job):
        calls.append(job["input"])
        return {"status": "ok", "content": "结果内容", "rounds": 2,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    sched = CronScheduler(execute_job=stub)
    job = _make_job()
    sched.jobs[job["id"]] = job
    arun(sched._fire(job["id"]))

    assert calls == ["task"]
    assert job["runs"] == 1
    assert job["last_run"]["status"] == "ok"
    assert job["last_run"]["content"] == "结果内容"
    assert len(job["history"]) == 1
    # interval scheduling: next = fire time + 60min (no longer the stale value)
    assert job["next_fire_at"] > 1_000_000


def test_fire_timeout_and_exception_paths():
    async def sleepy(job):
        await asyncio.sleep(30)

    sched = CronScheduler(execute_job=sleepy)
    job = _make_job(timeout_seconds=0.2)
    sched.jobs[job["id"]] = job
    arun(sched._fire(job["id"]))
    assert job["last_run"]["status"] == "timeout"
    assert "超时" in job["last_run"]["error"]

    async def boom(job):
        raise RuntimeError("炸了")

    sched2 = CronScheduler(execute_job=boom)
    job2 = _make_job()
    sched2.jobs[job2["id"]] = job2
    arun(sched2._fire(job2["id"]))
    assert job2["last_run"]["status"] == "error"
    assert "RuntimeError" in job2["last_run"]["error"]


def test_fire_history_cap():
    async def stub(job):
        return {"status": "ok", "content": "x", "rounds": 1, "usage": {}}

    with patch("atoms.tools._cron_core.get_cron_tool_config",
               return_value={**_GUARD, "history_cap": 2}):
        sched = CronScheduler(execute_job=stub)
        job = _make_job()
        sched.jobs[job["id"]] = job
        for _ in range(4):
            arun(sched._fire(job["id"]))
        assert len(job["history"]) == 2


def test_tick_once_skips_firing_and_disabled_jobs():
    """Reentry protection + enabled=false not fired + next not due not fired."""
    calls = []

    async def stub(job):
        calls.append(job["name"])
        return {"status": "ok", "content": "x", "rounds": 1, "usage": {}}

    async def scenario():
        sched = CronScheduler(execute_job=stub)
        due = _make_job(name="due")
        firing = _make_job(name="firing")
        disabled = _make_job(name="disabled", enabled=False)
        future = _make_job(name="future", next_fire_at=9999999999.0)
        for j in (due, firing, disabled, future):
            sched.jobs[j["id"]] = j
        sched._firing.add(firing["id"])   # simulate already executing
        sched._tick_once()
        await asyncio.sleep(0.2)          # wait for the spawned fire task to finish
        return sched, due["id"]

    sched, due_id = arun(scenario())
    assert calls == ["due"]               # only "due" gets fired
    assert sched.jobs[due_id]["runs"] == 1
    assert "firing" not in str(calls)


def test_disabled_env_keeps_scheduler_inert(monkeypatch, tmp_path):
    """NEXUS_CRON_DISABLED=1: get_scheduler returns an empty store and
    ensure is a no-op."""
    monkeypatch.setenv("NEXUS_CRON_DISABLED", "1")
    reset_scheduler()
    sched = arun(_cron_core.ensure_scheduler())
    assert sched.jobs == {}
    assert sched._tick_task is None
    # CRUD still works but nothing is persisted
    with patch("atoms.tools._cron_core.get_cron_tool_config",
               return_value={**_GUARD,
                             "jobs_path": str(tmp_path / "j.json")}), \
         patch("atoms.tools.cron_tool.get_cron_tool_config",
               return_value={**_GUARD,
                             "jobs_path": str(tmp_path / "j.json")}):
        r = json.loads(arun(tool_registry.dispatch(
            "create_cron", {"name": "x", "interval_minutes": 5,
                            "input": "t"})))
        assert "NEXUS_CRON_DISABLED" in r["note"]
        assert not (tmp_path / "j.json").exists()


# =========================================================================
# Lock discipline / dead dates / fire scoping / stop reclamation
# =========================================================================

def test_create_rejects_dead_date_schedule():
    r = _dispatch("create_cron", {"name": "死日期", "schedule": "0 0 30 2 *",
                                  "input": "task"})
    assert "永不命中" in r["error"]


def test_update_dead_date_keeps_original_schedule():
    r = _dispatch("create_cron", {"name": "好作业", "interval_minutes": 60,
                                  "input": "task"})
    jid = r["job_id"]
    r2 = _dispatch("update_cron", {"job_id": jid, "schedule": "0 0 29 2 *"})
    assert "永不命中" in r2["error"]
    # when mutate raises, the job keeps its original state (rejected before write-through)
    assert get_scheduler().get_job(jid)["schedule"] == {
        "interval_minutes": 60}


def test_add_enforces_max_jobs_under_lock():
    from unittest.mock import patch as _patch
    with _patch("atoms.tools._cron_core.get_cron_tool_config",
                return_value={**_GUARD, "max_jobs": 2}):
        sched = CronScheduler()
        sched.add({"id": "a"})
        sched.add({"id": "b"})
        with pytest.raises(ValueError):
            sched.add({"id": "c"})       # checked under the lock: concurrent creates no longer both pass
        sched.add({"id": "b"})           # overwriting an existing job (update reuse) is not capped


def test_scheduler_update_missing_job_returns_none():
    sched = CronScheduler()
    assert sched.update("nope", lambda j: None) is None


def test_fire_scopes_tool_context_per_job():
    seen = {}

    async def spy(job):
        ctx = current_tool_context()
        seen[job["id"]] = ctx.session_id if ctx is not None else None
        return {"status": "ok"}

    sched = CronScheduler(execute_job=spy)
    j1, j2 = _make_job(), _make_job()
    sched.jobs[j1["id"]] = j1
    sched.jobs[j2["id"]] = j2
    arun(sched._fire(j1["id"]))
    arun(sched._fire(j2["id"]))
    # per-job session scope — no longer everyone sharing the _global task-list bucket
    assert seen == {j1["id"]: f"cron:{j1['id']}",
                    j2["id"]: f"cron:{j2['id']}"}


def test_stop_cancels_inflight_fire_tasks():
    async def scenario():
        async def sleepy(job):
            await asyncio.sleep(30)
            return {"status": "ok"}

        sched = CronScheduler(execute_job=sleepy)
        job = _make_job()
        sched.jobs[job["id"]] = job
        sched._tick_once()               # due -> the fire task gets tracked
        await asyncio.sleep(0.05)
        assert sched._fire_tasks
        await sched.stop()               # shutdown cancels in-flight fires and waits for them
        assert not sched._fire_tasks

    arun(scenario())
