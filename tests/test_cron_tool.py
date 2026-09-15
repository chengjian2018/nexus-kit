"""cron 定时调度（cron tool + _cron_core 内核）单测：

- cron 表达式子集解析与 next_fire 计算（含死日期、dow 归一、OR 语义）
- CRUD 四工具：cron/interval 双调度、授权快照（剔除编排工具集）、
  args.tools 只能收窄、护栏上限、update 局部更新与暂停/恢复
- CronStore JSON 持久化 roundtrip（原子写、损坏文件降级为空）
- fire 路径：注入 stub 执行器（不打真 LLM）验证 history 记录、
  next 重排、超时与异常路径、重入跳过、_pool 不外泄
"""

import asyncio
import json
from datetime import datetime
from unittest.mock import patch

import pytest

from atoms.tools import cron_tool  # noqa: F401 -- module import 即注册
from atoms.tools import knowledge_tool  # noqa: F401 -- 快照池断言依赖其注册
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
    """独立调度器单例 + 统一护栏 patch（两个引用命名空间都盖住）。"""
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
# cron 解析与 next_fire
# =========================================================================

def test_parse_cron_shapes():
    assert parse_cron("* * * * *")["minute"] == set(range(60))
    assert parse_cron("*/15 * * * *")["minute"] == {0, 15, 30, 45}
    assert parse_cron("0 9 * * 1-5")["day_of_week"] == {1, 2, 3, 4, 5}
    # dow 7 归一为 0（周日）
    assert parse_cron("0 0 * * 7")["day_of_week"] == {0}
    assert parse_cron("1-10/3 * * * *")["minute"] == {1, 4, 7, 10}


def test_parse_cron_errors():
    for bad in ("0 9 * *",                # 4 字段
                "61 * * * *",             # 分钟越界
                "* 24 * * *",             # 小时越界
                "a b c d e",              # 非数字
                "*/0 * * * *",            # 步长 0
                "5-2 * * * *"):           # 区间倒置
        with pytest.raises(ValueError):
            parse_cron(bad)


def test_next_fire_weekday_morning():
    # 2026-09-11 是周五：周五 10:00 之后 → 下一个工作日 9 点是下周一
    parsed = parse_cron("0 9 * * 1-5")
    after = datetime(2026, 9, 11, 10, 0)
    fire = next_fire(parsed, after)
    assert (fire.year, fire.month, fire.day, fire.hour, fire.minute) == \
        (2026, 9, 14, 9, 0)
    assert fire.weekday() == 0   # 周一


def test_next_fire_minute_step_and_month_day():
    parsed = parse_cron("*/15 * * * *")
    fire = next_fire(parsed, datetime(2026, 9, 11, 9, 7))
    assert (fire.hour, fire.minute) == (9, 15)

    # 每月 1 号 4:30
    parsed = parse_cron("30 4 1 * *")
    fire = next_fire(parsed, datetime(2026, 9, 2, 0, 0))
    assert (fire.month, fire.day, fire.hour, fire.minute) == (10, 1, 4, 30)


def test_next_fire_dead_date_returns_none():
    # 2 月 30 日不存在 → 扫描一年后放弃
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
    assert "_pool" not in job          # 内部字段不外泄


def test_create_cron_interval_and_validation():
    r = _dispatch("create_cron", {"name": "巡检",
                                  "interval_minutes": 30,
                                  "input": "巡检磁盘水位"})
    assert r["schedule"] == {"interval_minutes": 30}

    # schedule 与 interval 二选一
    r = _dispatch("create_cron", {"name": "x", "schedule": "0 9 * * *",
                                  "interval_minutes": 5, "input": "t"})
    assert "二选一" in r["error"]
    # 缺调度表达
    r = _dispatch("create_cron", {"name": "x", "input": "t"})
    assert "调度" in r["error"]
    # 坏 cron 表达式即刻报错
    r = _dispatch("create_cron", {"name": "x", "schedule": "not cron",
                                  "input": "t"})
    assert "cron" in r["error"]
    # interval 越界
    r = _dispatch("create_cron", {"name": "x", "interval_minutes": 0,
                                  "input": "t"})
    assert "interval_minutes" in r["error"]
    # input 缺失
    r = _dispatch("create_cron", {"name": "x", "schedule": "0 9 * * *"})
    assert "input" in r["error"]


def test_create_cron_tool_snapshot_excludes_orchestration():
    """授权快照：ambient 授权 - subagent/workflow/cron；args.tools 只能收窄。"""
    r = _dispatch("create_cron", {"name": "带工具",
                                  "interval_minutes": 60, "input": "查资料"},
                  allow_toolsets=["cron", "knowledge", "subagent"])
    job = get_scheduler().get_job(r["job_id"])
    # cron/subagent 被剔除，knowledge 的工具保留
    pool = set(job["_pool"])
    assert "delegate_task" not in pool
    assert "create_cron" not in pool
    assert pool >= {"search_product_knowledge", "search_customer_service_knowledge",
                    "list_products", "send_goods_link"}

    # 收窄到不存在的工具 → 报错并列出可用池
    r = _dispatch("create_cron", {
        "name": "x", "interval_minutes": 60, "input": "t",
        "tools": ["bash", "not_in_pool"]})
    assert "不在本作业可用池中" in r["error"]
    # 合法收窄
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

    # 局部更新：改名 + 暂停
    r = _dispatch("update_cron", {"job_id": job_id, "name": "新名",
                                  "enabled": False})
    assert r["updated"] == ["enabled", "name"]
    assert r["enabled"] is False and r["next_fire_at"] is None
    # 恢复 + 换 interval 调度 → 重排 next
    r = _dispatch("update_cron", {"job_id": job_id, "enabled": True,
                                  "interval_minutes": 15})
    assert r["next_fire_at"] is not None
    job = get_scheduler().get_job(job_id)
    assert job["schedule"] == {"interval_minutes": 15}
    # tools 收窄仍受快照池约束
    r = _dispatch("update_cron", {"job_id": job_id,
                                  "tools": ["not_in_pool"]})
    assert "可用池" in r["error"]
    # 无字段可更新
    r = _dispatch("update_cron", {"job_id": job_id})
    assert "没有" in r["error"]
    # 幽灵 job_id
    r = _dispatch("update_cron", {"job_id": "job_ghost", "name": "x"})
    assert "不存在" in r["error"]

    r = _dispatch("delete_cron", {"job_id": job_id})
    assert r["deleted"] is True
    r = _dispatch("delete_cron", {"job_id": job_id})
    assert "不存在" in r["error"]
    assert _dispatch("list_crons", {})["count"] == 0


# =========================================================================
# 持久化仓
# =========================================================================

def test_cron_store_roundtrip(tmp_path):
    store = CronStore(str(tmp_path / "jobs.json"))
    jobs = {"job_a": {"id": "job_a", "name": "a", "runs": 1}}
    store.save(jobs)
    assert store.load() == jobs
    # 父目录不存在也能写（自动创建）
    store2 = CronStore(str(tmp_path / "deep" / "sub" / "jobs.json"))
    store2.save(jobs)
    assert store2.load() == jobs
    # 损坏文件 → 空
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert CronStore(str(tmp_path / "broken.json")).load() == {}
    # 不存在 → 空
    assert CronStore(str(tmp_path / "missing.json")).load() == {}


# =========================================================================
# fire 路径（注入 stub 执行器，不打真 LLM）
# =========================================================================

def _make_job(**overrides):
    job = {"id": new_job_id(), "name": "t", "enabled": True,
           "schedule": {"interval_minutes": 60}, "input": "task",
           "system_prompt": "", "tools": [], "_pool": [],
           "timeout_seconds": 5, "created_at": "2026-09-14T00:00:00",
           "runs": 0, "next_fire_at": 1.0,   # 已过期
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
    # interval 调度：next = fire 时刻 + 60min（不再是过期值）
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
    """重入保护 + enabled=false 不触发 + next 未到不触发。"""
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
        sched._firing.add(firing["id"])   # 模拟正在执行
        sched._tick_once()
        await asyncio.sleep(0.2)          # 等 spawn 出的 fire task 完成
        return sched, due["id"]

    sched, due_id = arun(scenario())
    assert calls == ["due"]               # 只有 due 被触发
    assert sched.jobs[due_id]["runs"] == 1
    assert "firing" not in str(calls)


def test_disabled_env_keeps_scheduler_inert(monkeypatch, tmp_path):
    """NEXUS_CRON_DISABLED=1：get_scheduler 空仓 + ensure no-op。"""
    monkeypatch.setenv("NEXUS_CRON_DISABLED", "1")
    reset_scheduler()
    sched = arun(_cron_core.ensure_scheduler())
    assert sched.jobs == {}
    assert sched._tick_task is None
    # CRUD 仍可用但不落盘
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
# 锁纪律 / 死日期 / fire 作用域 / stop 回收
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
    # mutate 抛错时作业保持原状（写穿前拒绝）
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
            sched.add({"id": "c"})       # 锁内检查：并发 create 不再双双通过
        sched.add({"id": "b"})           # 覆盖已有（update 复用）不受限


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
    # 每作业独立会话作用域——不再全体共享 _global 任务清单桶
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
        sched._tick_once()               # 到点 → fire 任务被追踪
        await asyncio.sleep(0.05)
        assert sched._fire_tasks
        await sched.stop()               # 停机取消在途 fire 并等收尾
        assert not sched._fire_tasks

    arun(scenario())
