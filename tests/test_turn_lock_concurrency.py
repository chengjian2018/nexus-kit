"""同会话并发 turn 的串行化行为回归（审查 M-4）。

生产代码靠 ``_run_chat_turn_core`` 里的 ``async with session.turn_lock``
防止同 session 并发 turn 交错 begin_turn 重置与 history 追加（买家重试 /
渠道重放场景）；此前唯一相关测试只断言了 ``isinstance(turn_lock,
asyncio.Lock)``。这里用行为级验证：

- 同 session 两个并发 turn：history 是两个完整 (user, assistant) 对，
  绝不交错（相邻 user 行 = 串行化失效的直接症状）
- 跨 session：慢 turn 不阻塞别的 session（锁是 per-session 的）
"""

import asyncio
import time

from async_utils import arun
from nexus.engine.session import Session


def _mk_session(sid: str) -> Session:
    s = Session(session_id=sid, pattern_code="xianyu_agent")
    return s


def _install_chat(monkeypatch, *sessions: Session):
    """Replace host.main's chat with a stub that mirrors the real turn
    contract on history (user row → slow 'LLM' → assistant row); register
    the sessions into the governor (chat re-fetches by id) and restore the
    table afterwards."""
    import host.main as main

    with main.governor.lock:
        snap = dict(main.governor.sessions)
        main.governor.sessions.clear()
        for s in sessions:
            main.governor.sessions[s.session_id] = s

    def _restore():
        with main.governor.lock:
            main.governor.sessions.clear()
            main.governor.sessions.update(snap)

    monkeypatch.setattr(main, "chat", _make_stub_chat())
    monkeypatch.setattr(main, "store", None)
    return main, _restore


def _make_stub_chat():
    async def stub_chat(query, session_id, all_sessions, store=None, **kw):
        cxt = all_sessions[session_id].cxt
        await cxt.add_message("user", query, stage="chat")
        await asyncio.sleep(0.2)  # 慢 LLM：给并发交错留足窗口
        await cxt.add_message("assistant", f"reply::{query}", stage="chat")
        return f"reply::{query}"

    return stub_chat


def test_concurrent_turns_same_session_serialize(monkeypatch):
    s = _mk_session("s-serial")
    main, restore = _install_chat(monkeypatch, s)

    async def scenario():
        return await asyncio.gather(
            main._run_chat_turn_core(s, "q1"),
            main._run_chat_turn_core(s, "q2"),
        )

    (r1, e1), (r2, e2) = arun(scenario())
    restore()
    assert e1 is None and e2 is None
    assert r1 == "reply::q1" and r2 == "reply::q2"

    pairs = [(m.role, m.content) for m in s.cxt.history]
    # 两个完整 (user, assistant) 对，以某种顺序排列，但绝不交错：
    # user 后必须紧跟本 turn 的 assistant（相邻 user 行 = 锁失效）
    assert len(pairs) == 4
    for i, (role, content) in enumerate(pairs):
        if role == "user":
            nxt_role, nxt_content = pairs[i + 1]
            assert nxt_role == "assistant", f"turn 交错: {pairs}"
            assert nxt_content == f"reply::{content}"
    assert sorted(c for r, c in pairs if r == "user") == ["q1", "q2"]


def test_cross_session_turns_do_not_block_each_other(monkeypatch):
    """per-session 锁：慢 session 的 turn 进行中，快 session 的 turn 照常完成。"""
    s_slow, s_fast = _mk_session("s-slow"), _mk_session("s-fast")
    main, restore = _install_chat(monkeypatch, s_slow, s_fast)
    done: list[tuple[str, float]] = []

    async def timed_chat(query, session_id, all_sessions, store=None, **kw):
        await asyncio.sleep(0.4 if query == "slow" else 0.02)
        done.append((query, time.monotonic()))
        return query

    monkeypatch.setattr(main, "chat", timed_chat)

    async def scenario():
        return await asyncio.gather(
            main._run_chat_turn_core(s_slow, "slow"),
            main._run_chat_turn_core(s_fast, "fast"),
        )

    (_, e1), (_, e2) = arun(scenario())
    restore()
    assert e1 is None and e2 is None

    t_fast = next(t for q, t in done if q == "fast")
    t_slow = next(t for q, t in done if q == "slow")
    assert t_fast < t_slow, "快 turn 被慢 turn 的锁挡住（锁应为 per-session）"
