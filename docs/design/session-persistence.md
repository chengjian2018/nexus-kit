# 会话中间状态持久化设计方案（trace 落盘 + turn 后台化）

- 状态：已评审定稿（grill 会话 2026-09-15，10 问全落定）
- 范围：trace 事件落盘（审计）、turn 生命周期与 SSE 连接解耦（断开不丢任务）、archify 终态 trace 捡回
- 不动：graph_state 快照时机（仍只在 turn 落定时写，"半程状态不落库"决策保留）、SSE 断点续传、
  进程崩溃后续跑（方向已存档，见 §9）、库清理/保留策略

---

## 1. 问题诊断

### 1.1 持久化面现状（代码事实）

| 数据 | 现状 | 证据 |
|---|---|---|
| user / assistant 消息 | turn 进行中逐条 write-through 落 messages 表 | `nexus/context.py:238-240`（sink await）、`nexus/engine/store.py:223` |
| graph_state / current_node_code / filled_slots | **仅** launch 时与 turn 正常落定后写快照 | `nexus/engine/store.py:126,184`（create_session / save_snapshot） |
| 默认 loop 执行器的工具调用与结果 | 随消息 write-through 落库 | `nexus/engine/loop.py:114,167` |
| **turn 进行中的 graph_state** | 只在内存，turn 中断即丢 | save_snapshot 无中途调用点 |
| **trace 事件**（node_start/node_end/tool_call…） | 纯内存 queue → SSE，进程内即焚，无落盘路径 | `nexus/engine/streaming.py:110,150,183` |
| **cxt.metadata**（llm_override、archify 终态 trace 等） | sessions 表无此列，重启丢失 | `nexus/engine/store.py:196-208` |
| **在跑的 turn 本身** | 不持久（见 §1.2） | — |

即："消息存储只存进出数据"不完全准确——graph_state 等有 turn 末快照；真正缺的是
**turn 进行中的过程记录**与**在跑 turn 的存活**。

### 1.2 两种失败模式，现状行为不同

| 失败模式 | 现状行为 | 证据 |
|---|---|---|
| 浏览器/SSE 断开（服务还活着） | turn 任务被 **cancel**；`turn_settled=False` → **故意不写快照**（防半执行的 graph_state 落库）；库里留下"有问无答" | `nexus/engine/chat.py:807-818`（task.cancel）、`host/main.py:574-636` |
| 服务进程崩溃/重启 | turn 真死；下条消息发现无挂起游标 → `graph_state.clear()` 从 entry **全量重跑** | `nexus/engine/chat.py:521-539` |

"断开重启读不到中间状态"是**设计决定**而非遗漏；本方案不反转其核心（快照仍不写半程），
只把"过程事实"与"turn 存活"补上。

### 1.3 archify 的双重缺口

- 各站用私有 `_dispatch_tool_calls`（`apps/archify_agent/executor.py:817-874`），
  **全文件零次 `cxt.add_message`**——工具过程完全不落库（这就是 archify 会话只有
  2 条消息的原因）。
- executor 已生成完整终态 trace（`_new_state` 全键，`executor.py:234-267`；写入
  `cxt.metadata[_TRACE_KEY]`，`executor.py:1576`），但 chat 层只取 `result.content`
  把 `TurnResult.extra` **丢弃**（`nexus/engine/chat.py:793`），metadata 又不持久化
  ——最有价值的中间过程记录已经生成，只在出口被扔。
- af_report 是终节点 → 图终止时 `graph_state.clear()`（`chat.py:612`），archify 会话
  的轮末快照恒为 `{}`。

### 1.4 其他相关事实

- **库只增不减**：sessions/messages 无任何清理；同 session 重开仅 `launch_epoch+1`，
  旧代消息行永久保留（`store.py:126-163`）；governor 只清内存（`host/governor.py:20,23`，
  TTL 2h / LRU 10000）。
- **同 session 并发 = FIFO 排队**：每 Session 一把 `asyncio.Lock`（`nexus/engine/session.py:30`），
  不拒绝、不超时；断开时随任务取消释放（`host/main.py:458,576`）。
- 现存边角：跑动中 session 被 governor TTL/LRU 逐出后重开 → 新 Session 新锁 →
  同 session 两轮**真并发**。

## 2. 目标与非目标

**目标**

1. **审计可见**：turn 的节点/工具/图事件持久化，重启或结束后可回放（append-only）。
2. **断开不丢任务**：SSE 断开后 turn 转后台跑完、结果照常落库；重连或刷新后可事后读取。
3. **archify 过程可查**：终态 trace 不再在出口被丢弃。

**非目标（本期明确不做）**

- 进程崩溃后的断点续跑（方向已定，存档 §9）。
- SSE 断点续传（Last-Event-ID 续流）。
- 库清理/保留策略（独立 ops 决策，§9）。
- studio 前端 trace 回放改造（接口本期给，前端下期）。
- graph_state 高频快照 / 快照时机任何变更。

## 3. 总体设计：三件事

```
                ┌─ SSE queue（现状，消费者在才推）
emit_trace ─────┤
                └─ trace_sink（新）→ trace_events 表（append-only，随时写）

turn task：SSE 生成器退出只断消费 ──→ host 层 task registry 持有 ──→ 跑完落库
                （现状：断开即 cancel + 不快照）

archify 终态 trace：turn 结束点把 cxt.metadata[_TRACE_KEY] 批量补写进 trace_events
```

核心不变式：**事件 = 已发生事实，随时写；快照 = 图状态，仍只在 turn 落定时写。**
不反转"半程状态不落库"决策——append-only 事实流可以诚实记录半程，可变状态不行。

## 4. A：trace_events 落盘

### 4.1 表（参考 DDL，细节按实现定）

```sql
CREATE TABLE trace_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增 id 即全局时序
    session_id   TEXT NOT NULL,
    turn_id      TEXT NOT NULL DEFAULT '',           -- host 层 request_id，sink 挂接时闭包注入
    launch_epoch INTEGER NOT NULL DEFAULT 0,         -- 对齐 messages 表换代语义
    kind         TEXT NOT NULL,                      -- 复用 TraceEvent kind 词表
    payload      TEXT NOT NULL DEFAULT '{}',         -- JSON
    truncated    INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);
CREATE INDEX idx_trace_session ON trace_events(session_id, id);
CREATE INDEX idx_trace_turn    ON trace_events(session_id, turn_id, id);
```

### 4.2 写入路径：sink 注入，engine 零持久化依赖

与 `message_sink` 完全同构（`nexus/context.py:146`）：host 层在 session attach 时把
SQLite 写入挂成 trace_sink；engine 的 `emit_trace` 扇出到 SSE queue 与 sink 两路。
engine 不 import store；测试 monkeypatch sink 即可隔离（对齐 `tests/test_trace_events.py` 习语）。

- **全 kind 存储**（node_start/node_end/graph_*/tool_call/tool_result…）——审计价值在全量。
- **截断**：单事件 payload > 8KB 截断 + `truncated=1`；二进制 / base64 一律不进。
- **落盘不因 SSE 消费者缺席而阻塞**：trace 写库独立于 SSE queue——事件经
  per-turn 单 writer 串行落库（FIFO 即自增 id 序）；SSE 断开后消费侧置 live=false
  停止入队（queue 不再随长 turn 无界增长），sink 与轮末回调照常工作（见 §5）。
- 写失败语义对齐 message_sink：计数告警，不阻断对话（`nexus/context.py` 同款处理）。

### 4.3 读取接口

- 新增 `GET /api/v1/sessions/{session_id}/trace`：分页（id 游标），可按 `turn_id` 过滤。
  与现有只读审计接口同族（`host/main.py:930,965`）。
- sessions 列表/详情顺带新增 `turn_running` 字段（后端本期；前端消费下期）。

## 5. B：turn 与 SSE 解耦

### 5.1 task registry（host 层，per-session）

- turn task 的持有者从"SSE 消费循环"（现状 `chat.py:807-818`，断开即 cancel）改为
  host 层 per-session task registry；SSE 生成器退出只断开消费，**不 cancel 任务**。
- 断开后 turn 后台跑完：user 消息、assistant 回复、trace 事件照常落库；
  turn 落定时 `save_snapshot` 照常写。
- `/api/v1/chat` 非流式通道与 channels 的 `run_chat_turn` **同构处理**——turn 归 registry，
  与传输无关。

### 5.2 三个配套语义（一揽子）

| # | 语义 | 说明 |
|---|---|---|
| 1 | **锁不释放** | 断开后 turn 转后台，`turn_lock` 持有到跑完；同 session 新消息维持 FIFO 排队（现有语义不变） |
| 2 | **governor 逐出跳过在跑 session** | TTL/LRU 判定排除 registry 中有在跑 turn 的 session——顺带修复 §1.4 的"逐出后重开真并发"边角 |
| 3 | **优雅停机 cancel 后台 turn** | 已写入的事件/消息保留；进程重启后维持现状全量重跑（无新语义） |

### 5.3 重连语义

不做 SSE resume。断开后重连/刷新，经 `GET /sessions/{id}/messages` 与
`GET /sessions/{id}/trace` 事后读取；`turn_running` 字段供 UI 标注
"上轮任务后台执行中/已完成"——前端心智从"断开 = 任务死"改为"断开 = 任务转后台"。

### 5.4 边界守卫

- 后台 turn 运行中收到同 session **重新 launch**：拒绝（epoch 翻代会孤儿化在跑 turn
  的写入），沿用 409 形态（对齐 `host/main.py:409` 重复 launch 行为）。

## 6. C：archify 终态 trace 捡回

- turn 结束点：若 `cxt.metadata[_TRACE_KEY]` 非空，捡成**一条** `app_trace` 事件
  （data = `{"app": key, "trace": <app 终态 dict>}`）进 trace_events，随后
  **从 metadata 摘除该键**（防后续轮次重复捡旧值）。数据现成（`executor.py:1576`），
  executor 仅更新 docstring；站内逐事件实时 emit（改全部站点）**不做**——等
  "实时观察 archify 跑动"的需求立项再说。
- 注意：这解决**审计**（结果可回放），不是**续跑**——状态板重建已证实不可能且不做（§9）。
- turn_id 的落点：`/api/v1/chat` 与 `/api/v1/chat/stream` 把请求的 `request_id`
  写入 `cxt.metadata["request_id"]`（每轮覆盖），引擎发射 trace 时读取——trail 的
  turn 身份是"发起该轮的请求"，而非 launch 时的 id。

## 7. 实现顺序建议

1. trace_events 表 + sink + 读取接口（独立可验，不动 turn 生命周期）。
2. archify trace 捡回（依赖 1，一个挂点）。
3. turn 后台化 + registry + 锁/governor/停机配套（最大件，动 `chat.py` 消费循环）。

测试锚：`tests/test_trace_events.py`（sink 注入）、`tests/test_session_store.py`
（表行为/换代）、`tests/test_chat_stream.py`（断开不 cancel + 锁排队）。

## 8. 明示的实现取舍

| 取舍 | 结论 |
|---|---|
| 双通道一致性 | 非流式 / channels 与 SSE 同构：turn 归 registry，与传输无关 |
| 断开后 SSE 端点行为 | 生成器即结束，无特殊协议 |
| turn_lock 归属 | 锁移入**引擎 turn task**（跨 body + settled 回调，断开后随后台 turn 存续）；host 层两处 `async with session.turn_lock` 移除 |
| 快照调用点 | 条件不变（仅落定轮写、cancel 不写）；调用点从 host 挪进引擎经 `on_settled` 回调——后台 turn 落定也能快照 |
| trace 定序 | 自增 id 即全局时序（per-turn 单 writer 串行写，FIFO 无乱序） |
| 保留策略 | 与现状一致不清理；审计保留期连同 sessions/messages 生命周期另立 ops 决策 |

## 9. 推迟项存档（方向已定，不预支设计）

| 事项 | 已定方向 |
|---|---|
| 进程崩溃后续跑 | AGENT 图按**工具调用边界**、FSM/loop 按**节点边界** checkpoint，不搞统一抽象；at-least-once，幂等归 executor（`chat.py` 既有契约） |
| archify 状态板重建 | 完整重建不可能（last_receipt/design_notes/repair_log/solver_tried/author_rounds/phases 纯内存）；未来由 archify 自写状态板到 `data/archify/<session>/` 工作区，app 层职责，引擎不感知 |
| 审计保留期 / 库清理 | 独立 ops 决策（现状只增不减，trace_events 会放大增长，已知） |
| studio 前端 trace 回放 / archify 实时观察 | 下期单独立项 |

## 10. 决策记录（grill 会话 2026-09-15）

| # | 问题 | 决策 |
|---|---|---|
| Q1 | 覆盖哪种失败模式 | 只做"断开但服务活着"；进程重启续跑推迟 |
| Q2 | 目的 | 审计可见先行；断点续跑另立 |
| Q3 | （若续跑）checkpoint 粒度 | 按图类型分治：AGENT→工具调用边界，FSM/loop→节点边界 |
| Q4 | （若续跑）副作用语义 | at-least-once，幂等归 executor |
| Q5 | 存储形态 | append-only trace_events 表（SQLite 单库） |
| Q6 | archify 免费重建路线 | 验证：不成立（工具调用不落库 + 状态板部分纯内存） |
| Q7 | trace 保留策略 | 本期不清理，保留期另立 ops 决策 |
| Q8 | 锁/governor/停机配套 | 锁不释放（FIFO 不变）、逐出跳过在跑、停机 cancel |
| Q9 | archify 站内审计覆盖 | turn 末批量捡回 metadata trace，不改 executor |
| Q10 | archify 重建收窄 | 本期不做；未来 app 层自写状态板到工作区 |
