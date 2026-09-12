# 计划⑨：动态扇出 —— AGENT 图运行时的 map-reduce 并行子 agent

- 日期：2026-09-12
- 状态：**已实施（全量测试绿，含实施期勘误 §8）**
- 前置：计划⑧（二层模型 + 图运行时）已实施；计划⑦（dispatch）作废归档，
  其扇出设计（BranchFrame / max_fanout / join 语义）**批判性移植**——设计复活，
  载体（module 层）不复活
- 定案方式：三轮设计拷问（形态 / 机制 / 契约），全部分支走到叶子

---

## 1. 目标与非目标

### 1.1 目标

1. **引擎级动态扇出（map-reduce）**：节点在运行时派发 N 个**同构**子 agent
   实例（langgraph `Send` 模型），线程池并行执行，barrier join 汇聚。
   N 编译期未知，运行时由节点 executor 决定。
2. **"子 agent"零新概念**：executor 为 default_loop（或任意自定义执行器）的
   普通节点，多次带参调用即是子 agent。不新增节点类型。
3. **并行真延迟收益**：扇出分支总耗时 = 最慢分支（非子任务之和）；快慢分支
   由 join 自然吸收。
4. **deep_research 迁移作为验收**：search 节点 12 轮私有循环退役，改为引擎
   扇出（search×N 并行 → synthesize 作 join）。

### 1.2 非目标（二期候选，本期明确不做）

- 异构动态图（运行时生成不同类型节点/整图）——击穿编译期 fail-fast 体系
- 嵌套扇出（worker 内再扇出）、子图 pattern 复用（node 引用另一个 pattern）
- quorum join（法定数量即收）、分支取消、分支超时
- 引擎 asyncio 化（线程池达成并行，executor 同步签名不动）
- wait_human / 挂起恢复进入扇出分支（挂起仍只属于图的普通节点）
- store 结构变更（分支状态全部落 `graph_state` JSON 列，零 ALTER）

### 1.3 覆盖矩阵（"接住任何场景"的可证伪边界）

| 场景类 | 覆盖状态 |
|---|---|
| 静态图（含环、条件边、挂起恢复） | ✅ 计划⑧已有 |
| 单节点 ReAct / FSM / 人工审批流 | ✅ 计划⑧已有 |
| map-reduce 扇出（同构子 agent、并行、barrier join） | ✅ 本计划 |
| 异构动态图（运行时生成异构节点） | ❌ 非目标，二期 |
| 嵌套扇出 / 子图 pattern 复用 | ❌ 非目标，二期 |
| quorum join / 分支取消 / 分支超时 | ❌ 非目标 |

表格之上的场景，pattern + 插件接得住；表格之下的，明确说接不住、何时接。

---

## 2. 契约

### 2.1 Send 与 TurnResult

```python
@dataclass
class Send:
    node_code: str        # 必须是本节点 sub_nodes 声明过的边目标
    input: Any = None     # 该实例的任务载荷（父节点负责把必要背景揉进来）

class TurnResult:
    ...
    sends: Optional[List[Send]] = None   # 新增；与 next 互斥
```

规则：

- `sends` 非空时 `next` 必须为 None；同时给出 → raise（executor 契约错误）。
- 每个 `Send.node_code` 必须 ∈ 派发节点的 `sub_nodes`（编译期声明过的边）；
  运行时违反 → `graph_done reason="undeclared_edge"` 终止（沿用现有守卫家族，
  chat.py:370-381 的语义平移）。
- **v1 全部 Send 指向同一节点**（同构）；`len(sends) > max_fanout` → fail-fast。
- 空列表视同 None（无扇出，走正常路由/终止语义）。
- `sends` 仅 AGENT 路径；FSM 路径收到 sends → raise。
- 扇出节点自身可 wait_human（先挂起取输入，恢复重执行后再 sends——
  现有 resume 语义自然组合）。
- `max_fanout` 默认 8，pattern `config` 可覆写。

### 2.2 ExecutionContext 扩展

```python
branch_id: Optional[str] = None     # 形如 "dr_search#3"，仅扇出实例内非空
branch_input: Any = None            # = Send.input，仅扇出实例内非空
```

### 2.3 worker 实例的上下文切片

- **私有 messages 工作区**：不落 `cxt.history`（线程安全靠结构隔离，不靠锁；
  deep_research 私有工作区已验证此形态）。
- 只读：pattern / config / 工具集；可读 `ec.branch_input`。
- **禁写一切共享字段**；结果由引擎回收，worker 无保存 API。
- worker 的 `TurnResult.next` 被忽略；worker 在扇出上下文返回
  `wait_human` 或 `sends` → **该分支失败**（error 条目进结果板），
  不挂起、不静默降级。
- worker 节点可同时被图中其他路径普通调用（此时 `branch_input=None`，
  节点就是节点，无特殊态）。

---

## 3. 执行模型

### 3.1 主循环扩展（chat.py `_run_agent_graph`）

节点返回 `sends` 时：

1. **校验**（不满足 → fail-fast，图终止，错误可读）：同构（唯一目标节点）、
   宽度 ≤ max_fanout、目标在 sub_nodes 声明、join 可解析 = worker 节点的
   `sub_nodes` 有且仅有一个目标。
2. `fanout_start` trace；结果板重建（覆写式清空）。
3. `asyncio.gather` 并发执行
   N 个实例：`branch_start` → executor（独立 ExecutionContext + 私有工作区）
   → `branch_end`（ok / error）。executor 抛异常 = 分支失败，engine 捕获，
   单分支失败绝不杀死整轮。
4. 实例落定即向结果板追加条目（**完成顺序**）：
   `graph_state["__fanout_results__"]` = `[{branch_id, node_code, ok,
   content, extra, error?}, ...]`——新增保留键，与 `__paused_node__` /
   `__step__` 并列（context.py:189-199 的保留键表扩展）。
5. 全部落定 → `fanout_join` trace → 执行 join 节点（**普通节点语义**：
   可 `next` 路由、可 `wait_human`——单游标指向 join，天然合法）。
6. join 的 executor 从 `ec.cxt.graph_state["__fanout_results__"]` 读结果，
   自行决定如何呈现/消化失败分支。

### 3.2 并行机制

- **asyncio.gather**（实施期修正，见 §8-1：裁决时"引擎全同步"的前提有误，
  executor 本就是 `async def`——gather 是比线程池更小的侵入面，裁决
  "不做全引擎重写、选最小侵入机制"的本意由此更好地达成）：分支协程在
  轮次事件循环上交错，共享 emitter 队列与结果板追加**零锁**。
- **轮内并发、轮间串行**：turn_lock 语义原样（每会话每轮一个扇出窗口）。
- 流式事件经 `BranchStreamEmitter` 包装器打上 branch_id 后汇入现有队列；
  共享 LLM client / 工具注册表按单循环并发使用（无跨线程问题）。

### 3.3 预算三层守卫（各管各维度）

| 守卫 | 维度 | 默认 |
|---|---|---|
| `max_steps` | 图主循环步数（扇出节点 1 步 + join 1 步，**worker 不占步数**） | 10 |
| `max_fanout` | 扇出宽度（实例数） | 8 |
| executor 内部守卫 | 分支内轮次（`_MAX_TOOL_ROUNDS` / `_MAX_SEARCH_ROUNDS` 等，每实例独立计） | 各自现状 |

### 3.4 与挂起恢复的正交性

- 单游标 `__paused_node__` 语义原样保留；join 可挂起、扇出分支不可。
- 扇出在单轮内完成；进程崩溃 = 该轮丢失（与会话现有轮次语义一致，
  不新增跨轮扇出持久状态——结果板仅供 join 与事后检视）。

---

## 4. trace / streaming / ops-console

### 4.1 事件词汇（streaming.py 词汇表扩展）

```text
fanout_start  {node_code, branch_ids, join_node}
branch_start  {branch_id, node_code}
branch_end    {branch_id, node_code, ok, error?}
fanout_join   {node_code, total, failed}
```

- `branch_id` = `{node_code}#{seq}`（如 `dr_search#3`），人可读。
- delta / round / done **全量加 `branch_id` 字段**（主线路径为 null）——
  多分支同时吐 token 时终端与 console 才能分道渲染。
- **补计划⑧欠账**：`graph_compile` 一并实现——落点为**每次新图运行的
  首个 trace 事件**（实施期修正，见 §8-2：注册期没有流可承载事件，
  每轮首事件同样给出编译形状且可被全部消费者渲染）。
- `ChatResult.actions` 快照补 fanout_start / fanout_join 摘要。

### 4.2 渲染

- CLI：`render_trace_event` 增 graph_compile + fanout_* 五个事件；
  `StreamEventPrinter` 按分支开行——分支增量渲染为
  `[dr_search#3] …` 独立行（分支切换即收行），分支文本永不混入主回复流；
  分支 round 事件带分支前缀。
- visualize：静态图不变（plan→search 边本就声明；扇出是运行时行为）。
- ops-console：**勘误（§8-3）**——console 是静态管理面（pattern
  mermaid/YAML/树、知识库、RAG 配置），不存在运行时 trace 画布可挂泳道；
  扇出的动态可观测性由 trace 事件 + CLI 分支渲染 + actions 快照 +
  `graph_state` 结果板交付。泳道渲染留待未来出现会话监控视图时再做
  （独立特性，非本计划范围）。
- nexus-introspect：pattern 形态未变（扇出是运行时行为），装配面
  （nodes / executor 解析链 / config 含 max_fanout）原样自省，无需改动。

---

## 5. deep_research 迁移（验收用例）

**性质说破：为验证框架而迁，不是为业务投诉而迁——这笔账要认。**

| 项 | 迁移前 | 迁移后 |
|---|---|---|
| 图 | preplan → plan → search → synthesize（search 内部 12 轮私有循环） | 同拓扑；plan 节点扇出 `sends=[Send(dr_search, q) for q in 子问题]` |
| 孤子降级 | plan 直跳 synthesize（声明边已有） | 原样：`next="dr_synthesize"`（sub_nodes = [dr_search, dr_synthesize] 双边，sends/next 二选一，契约自然组合） |
| search | `_search_phase` 私有 ReAct 循环 + 私有 messages 工作区 + 状态板重写 hack（executor_multi.py:311-429） | 降格为 worker executor（每实例一个子问题，`_MAX_SEARCH_ROUNDS` 每实例独立计：全局 12 → **每实例 6**，宽度×深度各自有界）；覆盖度启发式的子问题清单部分退役（实例隔离结构化达成；单实例内的勾选板保留） |
| synthesize | 读 `graph_state["deep_research_state"]` | 作 join 节点，读 `__fanout_results__` |
| `deep_research_state` 状态板 | 节点间手递手 | 大幅退役（仅保留 preplan/plan 需要的少量字段） |
| 检索延迟 | 子问题之和 | **最慢分支** |

---

## 6. 实施切分

P1 契约层（Send / TurnResult / ExecutionContext + 校验规则）→
P2 引擎层（线程池扇出 + join + 结果板 + 守卫 + 契约测试）→
P3 trace / streaming / CLI → P4 ops-console 泳道 →
P5 deep_research 迁移 → P6 文档（ARCHITECTURE.md / 覆盖矩阵同步）。

**检查点**：P2 完成点 = 引擎契约测试绿（互斥 raise / 同构校验 / 宽度 / 
undeclared_edge / join 触发 / 失败不阻塞 / 结果板完成序 / wait_human 分支失败）；
**全量绿检查点 = P5 完成后、P6 完成后**。P3/P4 期间 console 测试预期红，
todo 跟踪不静默跳过。

---

## 7. 决策记录（三轮拷问裁决）

| # | 裁决 | 选择 |
|---|---|---|
| 1 | 动态性形态 | 同构 Send 式扇出 map-reduce；异构动态图 / 子图 / 嵌套 = 二期非目标 |
| 2 | 并行机制 | 线程池（executor 同步签名不动，轮内并发轮间串行）；不 asyncio 化 |
| 3 | join 语义 | 声明边指定（worker.sub_nodes 唯一目标）、wait-for-all、失败 = error 条目不阻塞、结果板完成序 + branch_id、max_fanout=8、无取消/超时/quorum |
| 4 | 分支守卫 | 扇出分支内 wait_human / 嵌入 sends = 分支失败 fail-fast；单游标挂起语义不动 |
| 5 | 契约 | 新增 `sends` 字段与 `next` 互斥；`Send(node_code, input)`；ExecutionContext 增 `branch_id` / `branch_input` |
| 6 | worker 切片 | 私有工作区、不见 cxt.history、禁写共享、引擎回收结果（背景由父节点揉进 Send.input） |
| 7 | 预算 | max_steps 只数主循环节点；三层守卫（图步数/宽度/分支内轮次）各管各维度 |
| 8 | 验收 | deep_research 迁移（search×N 并行 → synthesize join，私有循环退役）；无业务载体，为验证框架而迁 |
| 9 | 覆盖矩阵 | §1.3 钉死"接住任何场景"的可证伪边界 |
| 10 | 计划⑦遗骸 | BranchFrame / max_fanout / join 语义批判性移植；module 载体不复活 |
| 11 | 事件 | fanout_start / branch_start / branch_end / fanout_join + delta 系全量 branch_id；补 graph_compile 欠账 |
| 12 | store | 零 ALTER，分支状态全落 graph_state JSON 列 |

---

## 8. 实施期勘误与定案（2026-09-12 实施时）

实施推翻了拷问期的两个事实前提、缩减了一项渲染范围——裁决本意全部保留：

1. **并行机制：线程池 → `asyncio.gather`**。拷问期裁决 #2 的前提"引擎全
   同步、executor 同步签名"有误——asyncio 化早已完成（`NodeExecutor.
   execute` 是 `async def`，`chat_turn_stream` 本就跑在事件循环上）。裁决
   本意"不做全引擎并发重写、选最小侵入机制"由 gather 更好地达成：零线程、
   零锁、executor 签名不动。计划⑦遗骸中为线程安全设计的分支作用域写入
   简化为"分支 cxt 浅拷贝隔离"（空 history / message_sink 切断 / 写丢弃）。
2. **`graph_compile` 落点：注册期 → 每次新图运行的首个 trace 事件**。
   注册期没有流可承载事件（StreamEmitter 轮次作用域）；每轮首事件同样
   携带编译形状（entry / nodes / pattern），且 CLI 与 console 均可渲染。
   恢复轮以 `graph_resume` 开头、不重发。
3. **ops-console 泳道渲染：无画布，降级为不做**。console 是静态管理面
   （pattern / catalog / 知识库 / RAG 配置），plan-⑧ §6.2 的"trace 树
   渲染"从未落地成会话视图——本计划 §4.2 的泳道行基于错误前提。动态
   可观测性由 trace 事件 + CLI 分支行 + actions 快照交付；泳道留待
   未来的会话监控视图（独立特性）。
4. **deep_research 轮次守卫：全局 12 → 每实例 6**（宽度 × 深度各自有界，
   单子问题无需旧的全局预算）；分支 findings 合并在 join 侧做全局 FIFO
   （_MAX_FINDINGS=30 不变），派发宽度在 plan 站按 `max_fanout` 截断并
   记入 plan notes。
5. **事件面微调**：`ChatStreamEvent` 与 `TraceEvent` 均增 first-class
   `branch_id` 字段（不只是 delta 系——trace 事件同样带）；round 事件
   的 `round_info` 内含 branch_id 便于扁平消费。
