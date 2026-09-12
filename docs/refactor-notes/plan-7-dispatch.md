# 计划⑦：跳转事件一对多泛化 —— 模块分发机制（Dispatch）设计方案

- 日期：2026-09-10
- 修订：2026-09-11 ①分支数据从事件 payload 迁移至 ctx 共享状态板 GraphState；
  ②mode 硬约束（轮内至多一次 parallel / 单目标强制 serial）；③分支独立
  Task 推进 + 依赖门控 + join 聚合配置与等待语义 + 轮末状态契约；
  ④ROUTE 多意图（D5）deferred 出本期
- 状态：**作废归档（2026-09-12，计划⑧取代）**——module 层已被 plan-⑧
  删除，本方案的 ModuleDispatchEvent/BranchFrame 等对象失去载体；其目标
  场景（S1-S3）由计划⑧的 AGENT 图运行时（TurnResult.next 路由输出 +
  graph_state 状态板 + wait_human 挂起恢复）接管，动态扇出/并行分支仍为
  扩展位。本文保留作设计思路存档，不再实施。
- 前置：计划⑥（跳转/投影重构，transfer 删除 + enable_project + defer）

---

## 1. 现状梳理：为什么"一对多"走不通

### 1.1 当前跳转链路（单目标）

```
生产者（3 类，均 append 到 cxt.actions）
  ├─ ROUTE NLU jump_module 字段      → ModuleJumpChannel.detect_after_stage
  ├─ ROUTE 菜单节点 jump_module 配置  → 同上（next_node 命中后读节点配置）
  └─ 自定义 executor 写事件           → 如 executor_multi._jump / dr_pipeline 接力
                    │
载体  ModuleJumpEvent(target_module_code: str, reason, source)   ← 单目标
                    │
消费者 chat_turn_stream 的 hop 循环（线性）
  for hop in range(max_hops):
      执行当前模块 → _jumps.pop（取**第一个**事件）
      ├─ 无事件 → result.content 即本轮回复，break
      └─ 有事件 → emit module_jump trace → reroute（改写唯一游标
                   cxt.current_module_code、清 node、记 forced_projection）→ 下一跳
  超预算 → else 分支：pop 剩余事件 reroute 后 force_close 收尾
```

### 1.2 单目标约束清单（一对多的具体障碍）

| # | 约束 | 位置 |
|---|---|---|
| C1 | 事件只携带一个目标 | `ModuleJumpEvent.target_module_code: str`（context.py） |
| C2 | NLU 协议单值 | `nlu_result.jump_module` 单字符串；`format_jump_modules` 只为单选服务 |
| C3 | hop 循环线性、单游标 | `cxt.current_module_code` 是唯一执行位置；一次只 pop 一个事件 |
| C4 | 回复语义单值 | 只有"无事件的那次执行"的 content 成为回复，无多路合并 |
| C5 | cxt 单例槽位 | `nlu_result` / `nlg_result` / `current_node_code` 等都是单槽，两路并行互踩 |
| C6 | history 单流 | 并行分支的 tool 行交错会破坏 `tool_call_id` 配对回放（messages.py 的全量回放约定） |
| C7 | 流式单流 | delta 乐观转发假定线性回复，无分支标记 |
| C8 | trace 单边 | `module_jump` 只有 from→to 一条边 |

**现存边角（顺带修复）**：`pop` 只取第一个事件——若一个 turn 内被写了多个
ModuleJumpEvent（多个 stage 各写一个 / NLU 重复触发），滞留事件会被**后续跳**
误消费，产生计划外的额外跳转。泛化后的 drain 语义（见 D2）自然消除。

**佐证先例**：`apps/deep_research_agent/executor_multi.py` 已经在用"自定义
executor 手工写事件 + metadata 传状态"拼线性接力（preplan→plan→search→
synthesize），证明"写事件通道"表达力足够，但一对多（如 plan 把子问题分发给
多个检索 worker 并行）在当前机制下无法表达——这正是本方案要机制化的东西。

另注：`ModuleJumpEvent.reason` 的 docstring 声称"注入目标模块 prompt"，实际
无消费方（messages.py 无注入点；dr 配方用 metadata 自传状态佐证）。本方案
D4 把分支任务简报的注入做成真实机制。

---

## 2. 目标场景

| 场景 | 形态 | 例子 |
|---|---|---|
| S1 多意图串行 | route 一跳 fan-out N 目标，按序执行，末尾合并回复（**ROUTE NLU 多意图通道本期 deferred**，经自定义 dispatcher executor 写分发事件表达） | "查下明天天气，顺便帮我订最早的机票" → weather 模块 + booking 模块 |
| S2 多 agent 并行分发 | 规划模块把任务拆成 N 个子任务，并行派给 N 个 worker 模块，全部完成后由汇聚模块综合 | deep research：plan 把子问题分发给多个 search worker 并行检索，synthesize 汇总成报告 |
| S3 混合 | 串行链中某站再 fan-out（fan-out 即轮内**唯一**一次 parallel，见 D1 决策三） | dr_preplan → dr_plan →(fan-out 3×dr_search)→ dr_synthesize |

非目标（本期不做）：跨 turn 的分支存活（分支是**轮内瞬态**）；分支间通信
（泳道间中间产物依赖，见 D4.5 边界）；动态注册模块；ROUTE NLU 多意图协议
（D5，deferred）。

---

## 3. 设计总览

核心抽象四个，全部贴着现有机制长出来：

1. **分发事件**（新载体，纯寻址）：`ModuleDispatchEvent` —— 一次声明 N 个
   目标 + 执行模式（serial/parallel）+ 汇聚说明。**事件不携带业务数据**，
   只带"去哪 + 为什么 + state_key 指针"。`ModuleJumpEvent` 原样保留，消费端
   归一化后单目标跳转就是分发计划的退化情形。
2. **GraphState**（ctx 数据面）：`cxt.graph_state` 共享状态板——分发者写
   子任务数据、分支读输入/写产出缓冲、join 前按 reducer 规则合并。数据一律
   走状态板，边不运货（langgraph 的 GraphState 对应物）。
3. **turn 内调度器**（hop 循环的泛化）：线性游标 → **分支 = 独立 Task**。
   分支内帧链串行推进、分支间互不等帧，唯一的 barrier 在 join 点。
   单帧 + 单目标 + serial 的退化路径与今日 hop 循环**逐位相同**。
4. **分支帧**（并行执行单元）：对 cxt 做影子派生——共享读、分支写覆盖、
   history 走分支缓冲，join 时整块合并（保 tool 配对）。

```
生产者（两步，先数据后控制）
  ① 写数据 ──▶ cxt.graph_state（GraphState，轮内共享状态板）
  ② 写事件 ──▶ cxt.actions（纯寻址：targets×N + 各自 state_key 指针）
                     │
                     ▼  drain 归一化
              DispatchPlan（targets + mode + join）
                     │  expand：子帧绑定 state_key 切片（快照只读 + 写缓冲）
                     │         + 依赖门控（wait_state_keys 缺键挂起）
                     ▼
         ┌──────────── turn 内调度器（chat_turn_stream 主体）─────────────┐
         │ 分支 = 独立 Task：帧链串行推进，分支间互不等帧                │
         │ 预算：max_hops = turn 内模块执行总次数（线性退化下语义不变）    │
         │ 不变量：轮内至多一次 parallel 展开（parallel_used 标志），      │
         │         其后一切 parallel 声明降级 serial + trace              │
         │ join 点（唯一 barrier）：分支写缓冲按 reducer 合并回主 state   │
         │         （派发序，确定性）+ 框架写 branch_results 供 join 消费  │
         └──────────────────────────────────────────────────────────────────┘
```

---

## 4. 详细设计

### D1 事件 schema（nexus/context.py 新增）

```python
@dataclass
class JumpTarget:
    """分发计划中的单个目标（纯寻址，不携带业务数据）。"""
    module_code: str
    reason: str = ""          # 分支任务简报（D4 注入目标模块 prompt）
    state_key: str = ""       # 分支收件箱指针：graph_state[state_key] 为本分支输入切片；
                              # 空 = 无切片，共享全量 state 只读

@dataclass
class ModuleDispatchEvent:
    """一对多分发事件（ModuleJumpEvent 的多目标泛化）。"""
    targets: List[JumpTarget]        # ≥1；==1 时消费端退化为单跳，且 mode 强制 serial
    mode: str = "serial"             # serial | parallel；声明受两条硬约束（决策三）：
                                      # ① 单目标一律 serial（parallel 声明被忽略）
                                      # ② 每个 chat_turn 至多一次 parallel 展开，
                                      #    已发生后所有分发（含嵌套）一律降级 serial
    join_module_code: str = ""       # 汇聚模块；空 = 走 pattern 级 dispatch.join 或默认合并
    join_policy: str = "all"         # 等待语义：本期仅 "all"（D4.4），预留扩展不实现
    merge: str = "concat"            # 无 join 时的默认合并：concat（后续可扩展）
    source: str = ""                 # nlu_dispatch / route_menu / executor 自定义

    def to_dict(self) -> Dict[str, Any]:     # 观测形态（ChatResult.actions 快照）
        return {"module_dispatch": {
            "targets": [t.module_code for t in self.targets],
            "mode": self.mode, "join": self.join_module_code,
            "join_policy": self.join_policy,
            "merge": self.merge, "source": self.source}}
```

**决策一：新事件类型而非给 ModuleJumpEvent 加 targets 字段。** 理由：
(a) 跳转=改道（放弃源回复、控制流整体移动）、分发=分裂（源模块把 N 个子任务
委派出去），语义不同，混在一个类型里消费者要处处分支判断；
(b) `ModuleJumpEvent(target_module_code=...)` 是既有构造签名与测试锚点，保持
冻结；(c) 两个类型在 channel 层归一化，生产端可以继续只写单目标事件。

**决策二：事件不带 payload，数据走 GraphState（边/状态分工）。** 理由：
(a) 对齐 langgraph 的核心分工——边（事件）运控制流寻址，状态运数据；
(b) 事件保持不可变小对象，审计/快照/trace 不随业务数据膨胀；
(c) S3 嵌套分发时子分发者只需写**新** state 键，不碰外层键，无事件树的递归
收集问题；(d) join 模块读的是**合并后的单一 state**，不必逐事件翻找 payload；
(e) 分支间想共享中间产物时（S2 worker 互相可见的素材板），有天然的落点
（共享键），事件模型给不出这个位置。

**决策三：mode 的两条硬约束（轮内控制流不变量）。**

1. **单目标强制 serial**：`len(targets) == 1` 时 mode 归一化为 serial——
   声明了 parallel 也忽略 + warning。单目标"并行"没有语义，且这保证单跳
   退化路径（D9）天然满足。
2. **轮内至多一次 parallel 展开**：`parallel_used` 是 turn 级标志，首次
   parallel 展开时置位；此后 drain 到的任何 `mode="parallel"` 分发事件
   （无论来自哪个分支、嵌套多深）一律**降级 serial** + trace（不报错——
   沿用幻觉目标剔除的同款容忍语义，控制流不因声明非法而中断）。

   理由：(a) 并行的并行 = N×M 路并发 LLM 调用，资源/流式/合并复杂度全部
   爆炸；(b) 一次 parallel 之后各分支只能串行续链，frontier 形状被钉在
   **扁平森林**（≤ max_fanout 条并行泳道 × 各自的线性尾），不会长出深层树；
   (c) 与 D7 的预算/宽度旋钮正交——`max_hops` 限总量、`max_fanout` 限单次
   宽度、本约束限**瞬时并发形状**，三者合起来把轮内瞬时并发上界钉在
   `max_fanout`。

**互斥关系**：与 `DeferredModuleSwitch` 维持现状互斥（同轮分发 ≠ 轮末换底座），
交互规则见 D8。

### D2 ModuleJumpChannel 扩展（nexus/engine/chat.py）

在现有 `peek / pop / reroute / detect_after_stage` 之上新增：

```python
@staticmethod
def drain(cxt) -> "DispatchPlan | None":
    """取走本轮全部跳转/分发事件，归一化为一个 DispatchPlan。

    - 只有一个单目标事件 → DispatchPlan(targets=[...], mode="serial",
      join="", ...) —— 线性退化，消费路径与今日等价；
    - 多个事件 / 多目标事件 → 合并 targets（按写入序），mode/join 取
      分发事件中的最高优先级声明（parallel > serial；join 取首个非空）；
    - 归一化硬约束（D1 决策三）：合并后 len(targets)==1 → mode 强制
      "serial"（parallel 声明忽略 + warning）；
    - 修复现存滞留边角：不再"每跳 pop 第一个"，一次取清。
    """

@staticmethod
def expand(cxt, plan) -> List["BranchFrame"]:
    """DispatchPlan → N 个分支帧。逐目标存在性校验（沿用今日容忍语义：
    非法/自身目标剔除 + warning，全非法 = 无跳转），记 forced_projection
    （防乒乓规则照旧：分发者入册）。每个子帧绑定 target.state_key 指向的
    graph_state 切片（spawn 时刻的快照只读视图 + 分支写缓冲）；
    目标模块声明 wait_state_keys 且缺键的子帧进 pending 池（D4.5）。"""
```

`pop / reroute` 保留不删（测试锚点 + force_close else 分支的线性语义仍走它）；
`detect_after_stage` 的多值读随 D5 一并 deferred（本期生产端 = 自定义
executor 写事件，引擎级通道不变）。

### D3 turn 内调度器（chat_turn_stream 的 hop 循环泛化）

**执行模型：分支 = 独立 asyncio Task**（示意代码，非最终实现）：

```
budget = pattern.max_hops                    # 语义升级：turn 内模块执行总次数
parallel_used = False                        # 轮内单次 parallel 不变量（D1 决策三）
results: Dict[branch_id, TurnResult] = {}    # 完成分支登记簿（异常 → error 占位）

async def run_branch(queue):                 # 一条分支 = 帧队列 + 独立 Task
    while queue and budget > 0:
        frame = queue.pop(0)
        budget -= 1
        result = await executor.execute(frame.ec)   # 异常 → error 占位，不传染
        plan = _jumps.drain(cxt)
        if plan is None:
            results[frame.id] = result       # 该帧自然完成；队列有后继则继续
            continue
        if plan.mode == "parallel" and parallel_used:
            plan.mode = "serial"             # 降级不报错（容忍语义）+ trace
        children = _jumps.expand(cxt, plan)  # 子帧绑定 state_key 切片 + 依赖门控
        if plan.mode == "parallel":          # 轮内首次（否则上方已降级）
            parallel_used = True
            for lane in children:
                spawn run_branch([lane])     # N 条独立泳道，本 Task 就此终止
            return
        queue.extend(children)               # serial：同分支按派发序续行

await gather(run_branch([根帧]))             # join 点 = 唯一 barrier（join_policy="all"）
```

- **分支间互不等帧**（消除伪同步）：串行分发 = 本分支队列就地续行；parallel
  分发 = 分发者链终止、展开 N 条独立泳道 Task。快分支走 1 帧完成即闲，慢分支
  自己走 3 帧——唯一的同步点是 join（全部分支 Task 终止），不是每帧对齐。
- **预算扣减**在帧执行前同步完成（asyncio 单线程，无竞态）；budget 归零后
  各分支循环自然退出，悬挂帧不执行。
- **分支异常**：捕获后登记 error 占位（D4.4），该分支终止，其余分支照常。

**线性退化校验**（兼容性的硬承诺）：分支队列恒长 1、事件恒为单目标 serial、
无 join —— 循环体与今日 hop 循环逐步等价：执行→drain（== 无事件时的
pop None）→回复；有事件→expand（== reroute：改写位置、清 node、记
forced_projection）→下一帧；预算耗尽→force_close else 分支今日语义保留
（pop 首个 pending、reroute、force_close 首目标）。trace 事件名与顺序不变。
退化路径不触碰 graph_state（无 state_key 的事件零开销）。

### D4 分支数据面（GraphState）与分支帧（BranchFrame）

> 新文件 `nexus/engine/branches.py`；GraphState 字段落在 `nexus/context.py`。

#### 4.1 GraphState：cxt 上的共享状态板

```python
# DialogueContext 新增一等字段（对齐 task_basic_info 的先例）
graph_state: Dict[str, Any] = field(default_factory=dict)
```

- **定位**：turn 内单一数据面。分发者写子任务数据、分支读输入、分支产出走
  写缓冲、join 读合并后的全量素材——所有跨模块数据流动只走这一个通道。
- **生命周期**：轮内瞬态。`begin_turn` 出清（对齐 deep_research_state 先例）；
  轮末快照携带当轮终态（观测友好，同 metadata 通道，无新表）；跨轮留痕见
  D8 轮末状态契约。
- **写入模型**（谁在何时写什么）：

| 写入者 | 时点 | 写哪里 |
|---|---|---|
| 分发者（executor） | 写分发事件**之前** | 主 state 任意业务键（`subtasks` / `plan` / 约定命名空间） |
| 分支 | 执行中 | **只写自己的写缓冲**（`frame.state_updates`），不直写主 state |
| 框架 | serial 分支链**完成时**（即时增量合并，供依赖门控消费）+ join 点（全量） | 合并缓冲进主 state（reducer 规则）+ 写保留键 |
| join 模块 | 执行中 | 直接写主 state（此时无并发，最终写语义） |

- **合并规则（reducer，按派发序确定性合并，与分支完成序无关）**：
  - 标量：覆盖（派发序 last-write-wins）
  - `list`：拼接（派发序 concat）
  - `dict`：浅合并（同键覆盖，不递归——规则保持可预测）
  - 类型冲突（前后写入类型不同）：派发序靠后者胜 + warning trace（无静默原则）
  - 框架保留键：`branch_results`（join 素材）、`dispatch`（分发元信息），
    业务键不得占用
- **读模型**：分支 spawn 时绑定主 state 的**快照只读视图**——并行分支互相
  看不见写入；嵌套分发（S3）读到的是外层 join 前的快照，子分支产出经自己的
  join 合并后才对外层可见，天然层次化、无环。

#### 4.2 分支任务简报注入（reason 机制化 + 切片投递）

- `state_key` 非空 → 分支帧绑定 `graph_state[state_key]` 切片，
  `{__branch_task__}` 插槽注入 `reason + json(切片)`；
  空 `state_key` = 无切片（共享全量只读，简报只有 reason）。
- 切片即"分支收件箱"：分发者写 `graph_state["subtask_3"] = {...}` 并在事件
  里声明 `state_key="subtask_3"`。寻址与数据分离——这同时补上 1.2 节末尾
  指出的"reason 声称注入 prompt 但无消费方"的欠账。

#### 4.3 BranchFrame：cxt 影子派生（解决 C5/C6）

```python
@dataclass
class BranchFrame:
    branch_id: str            # "b0"/"b1"...（trace/历史标记用）
    target: JumpTarget
    ec_factory: Callable      # 生成影子 ExecutionContext（懒构造）
```

**影子派生规则**（不继承、不改动单例，`dataclasses.replace` 派生）：

| 字段类 | 影子策略 |
|---|---|
| 只读共享（session_id、user_query、history 前缀、filled_slots、module_map、node_map） | 引用共享，分支只读 |
| 单槽可变（nlu_result / nlg_result / agent_result / current_module_code / current_node_code） | 影子帧各自持有，互不可见（join 时不回写——分支的中间态对本轮之后无意义） |
| graph_state | 快照只读视图 + 写缓冲（见 4.1；serial 链完成时/join 点合并回主 state） |
| metadata | copy-on-write：分支写键前浅拷贝；瞬态键由 join 阶段统一写主 cxt |
| history | `message_sink=None` + 分支缓冲 `List[SessionMessage]`（**不直写库**，规避 aiosqlite worker 上多分支并发写）；合并时按派发序**整块** append 回主 cxt，每行 `metadata.branch=branch_id` —— 整块 contiguous 保 `tool_call_id` 配对回放（C6） |
| llm_config | 分支执行前按目标模块 R4 刷新，影子帧各自持有（不互踩） |

#### 4.4 join：聚合配置与等待语义

**聚合在哪配置**（对齐仓库声明优先级惯例，事件 > pattern）：

| 层级 | 字段 | 语义 |
|---|---|---|
| 分发事件 | `join_module_code` | 本次分发的汇聚模块——dispatcher 最清楚要怎么综合，最高优先级 |
| pattern | `dispatch.join` | 该 pattern 的默认汇聚模块（省得每个 executor 重复声明） |
| （都未声明） | 事件 `merge`（默认 concat） | 无 join 模块的默认合并：按派发序拼接非空 content，全空 → 框架收尾话术 |

**等待语义（join_policy，本期仅 "all"）**：

- barrier 点 = **全部分支 Task 终止**（自然完成 / error 占位 / 预算耗尽），
  不是"每帧对齐"。"等 N 个研究分支都拿到结果再聚合"即此语义：每条泳道
  独立推进自己的串行尾，先完成的泳道空闲等待，不拖慢也不催促其它泳道。
- `join_policy` 显式成字段但本期只实现 `all`；`any` / quorum（任一完成即
  聚合、其余取消或后台化）引入竞态与取消语义，随 §5 备选 C 的 DAG 方向
  再议，不做预留实现。
- join 执行前框架合并写缓冲 + 写保留键 `branch_results`；join 以
  `force_close=(budget==0)` 执行一次（超预算时它有收尾义务）。

**部分失败**：分支异常 → 该分支登记 error 占位，不传染其它分支、不阻塞
join；join 模块看到占位自行降级（dr_synthesize 的"证据不足"报告即此先例）。
异常分支的 history 缓冲照常合并（审计完整优先于结果洁癖）。

**join 模块的数据面**（框架在 join 前写入主 state 的保留键）：

```python
graph_state["branch_results"] = [
    {"branch_id": "b0", "module_code": "dr_search",
     "content": "...", "extra": {...}},   # TurnResult.extra 原样；异常分支为 error 占位
    ...,                                    # 按派发序
]
graph_state["dispatch"] = {"targets": [...], "mode": "...", "join": "..."}
```

join 模块提示词用 `{__dispatch_results__}` 插槽消费（其 executor 也可直接读
`cxt.graph_state`）。`dr_synthesize` 就是这个协议的手工存在性证明——S3 场景
下 dr 配方可改写为：plan 先写 `graph_state["subtasks"]`，再写
`ModuleDispatchEvent(targets=[3×dr_search 各带 state_key], mode="parallel",
join="dr_synthesize")`，metadata 手工传状态的整段代码消失。

#### 4.5 依赖门控（wait_state_keys，module 级声明）

对应"分支 2 要等分支 1 的产出才能开始执行"的依赖表达：

```python
# AgentModule 新增可选声明：本模块的输入契约（graph_state 键清单）
wait_state_keys: List[str] = []
```

- **语义**：分发事件展开为子帧时，调度器检查各子帧目标模块的
  `wait_state_keys`——缺键的子帧进 **pending 池**（不执行）；每次状态合并
  事件（serial 链完成时的增量合并 / join 合并，均有 `state_merge` trace）
  后重查 pending，键齐即入列执行。
- **键就位的时点定义**：serial 分支链完成时其写缓冲**即时增量合并**入主
  state（这就是 4.1 写入模型里框架的第二个合并时点）；并行泳道的写只在
  join 点合并。
- **边界（重要）**：在"轮内单次 parallel + join 点合并"的模型下，**跨泳道
  依赖不可表达**——泳道 B 等泳道 A 的中间产出，等价于声明两者应当串行。
  需要这种依赖时的正确表达是**两次分发**：先 serial 分发 A，A 的链尾再
  fan-out 泳道（此时 A 的产出已在主 state，`wait_state_keys` 门控自然放行）。
  真正的数据流 DAG（map-shuffle-reduce 型跨泳道中间依赖）留待 §5 备选 C
  方向，本期不做。
- **典型用法**：parallel 展开的某泳道等一条**前置 serial 链**的业务键
  （时序上可行：serial 链先完成合并、泳道后激活——即"先规划后并行"的
  plan→searches 结构）。

### D5 ROUTE 多意图协议（**本期 deferred，不做**）

引擎级通道（ModuleDispatchEvent + 调度器）本期落地；以下 NLU/配置/提示词
层待引擎层稳定后单独立项：

- NLU 双字段（`jump_module` 单值保持 / `jump_modules` 列表新增）、
  `detect_after_stage` 多值读；
- 菜单节点 `jump_modules` 配置、pattern `dispatch: {default_mode,
  max_fanout, join}`；
- 提示词闸门（`allow_multi_intent`，防单意图应用被诱导乱跳）。

deferred 期间 S1 场景的用法：自定义 dispatcher executor 写
`ModuleDispatchEvent(mode="serial")`——引擎能力完整，只是没有 NLU 自动通道。

### D6 流式与 trace

**branch 标记规则**（谁的事件带 `branch`、聚合器怎么对待）：

| 事件来源 | branch 字段 | aggregate_turn |
|---|---|---|
| 主流执行（根帧 / join 模块） | None | delta 拼入最终 text |
| serial 分支帧 | delta **不带**（各分支回复按序 concat 即本轮回复流，S1 流式照常）；round/trace 带 branch_id | delta 拼入；trace 可观测 |
| parallel 泳道 | delta/round/trace 全带 branch_id | **delta 不拼入 text**（并行草稿不是最终回复，join 才是）；round/trace 可观测 |

- 设计原则：**协议层全发 + 打标，展示策略在消费端**。并行泳道的中间文本
  会被 join 综合，拼进主流只会得到乱文——但事件照发，实时消费端（SSE 调试
  端点 / ops-console）可按 `branch_id` 分泳道渲染。
- 工具调用展示：executor 的 `tool_call` / `tool_result` trace 本就逐事件发出
  （`ec.stream`），并行时多泳道事件在队列上按到达序交错——**tag 在即可重组**：
  trace 同时携带 `module_code + branch_id` 双键，同模块多实例（3×dr_search）
  靠 branch_id 区分；console 的 trace 树按泳道分组渲染，每条泳道一个可
  折叠 lane（工具调用/结果/轮次归各自的 lane）。
- `ChatStreamEvent` 增加可选 `branch: str|None` 字段（默认 None = 主流/单支，
  旧消费者零感知）；**done 仍是唯一权威回复**（聚合消费者天然兼容；
  `aggregate_turn` 的 text 拼接跳过 branch 非 None 的 delta——这是 P3 阶段
  唯一的聚合器改动）。
- trace 新增三个事件（`streaming.py` 的合法事件表同步）：
  - `dispatch_fanout`：`{parent, targets: [...], mode, branch_ids}`（展开时；
    mode 为**生效值**，发生 parallel→serial 降级时附
    `downgraded_from: "parallel"`——无静默）
  - `branch_join`：`{join_module, branch_count}`（join 模块执行前）
  - `state_merge`：`{keys, conflicts}`（缓冲合并；conflicts 非空才发，
    落实"类型冲突不静默"）
- `module_jump` / `route_hit` / `route_root` 语义与顺序不变（单跳路径零变化）。

### D7 预算与安全

| 旋钮 | 缺省 | 语义 |
|---|---|---|
| `max_hops` | 2（不变） | 升级为 **turn 内模块执行总次数预算**（含 join）；线性退化下与今日数值语义一致 |
| `max_fanout` | 3 | 单次分发的宽度上限；超宽裁剪 + warning + `dispatch_fanout` trace 记 dropped 目标（**无静默截断**） |
| （不变量，非旋钮） | — | 轮内至多一次 parallel 展开 + 单目标强制 serial（D1 决策三）——`max_hops` 限总量、`max_fanout` 限单次宽度、此不变量限**瞬时并发形状**，三者共同把轮内瞬时并发上界钉在 `max_fanout` |
| `max_depth` | —（不做） | 嵌套深度被总预算自然约束（每次执行都扣 budget），不单独设旋钮 |

- 超预算 force_close 对齐今日：预算耗尽时若声明了 join → join 模块以
  `force_close=True` 执行一次（它自己有收尾义务）；无 join → 今日 else 分支
  语义保留（pop 首个 pending、reroute、force_close 首目标）。
- 幻觉目标：逐目标剔除（今日容忍语义），全部非法 = 无分发，源模块继续。
- 防乒乓：分发发生时分发者记入 forced_projection（照旧）。

### D8 轮末状态契约 + defer / 持久化 / 并发交互

**轮末状态契约（end-of-turn state contract）**——fan-out turn 结束时各状态面
的终值定义：

| 状态面 | 轮末终值 |
|---|---|
| 结束条件 | 全部分支 Task 终止（含 error 占位）+ join（或默认合并）产出回复 + 既有 end_turn 生命周期跑完——三者齐才算回合结束 |
| graph_state | join 合并后的终态随轮末快照落盘（观测/审计，走既有 cxt 快照通道，无新表）；下轮 `begin_turn` 出清。跨轮业务留痕两条路：join 模块显式写 `cxt.metadata`（dr_synthesize 写 trace 先例）；pattern 配置 `dispatch.persist_keys`（默认空）把选中键自动晋升进 metadata |
| 底座 current_module_code | 有 join → **join 模块**（turn 的最终执行者，下一轮延续它的域最自然）；无 join 的 fan-out → **派发序最后目标**（确定性优先于完成序——并行完成序不定）；模块可自行改写（dr_synthesize 复位 entry 先例）；join 写 `DeferredModuleSwitch` 轮末切底座合法 |
| current_node_code | 置 None——底座模块下一轮自解析入口节点（今日 reroute 清 node 语义的延伸） |
| history | 分支缓冲已按派发序整块合并（含异常分支，审计完整）；最终回复经 end_turn 正常 append；tool 配对完好（D4.3 整块合并保证） |
| actions | 分发事件 drain 消费后 **re-append**（对齐 DeferredModuleSwitch 的可观测惯例）→ ChatResult.actions 快照可见 fan-out 全貌 |
| 分支影子中间态 | nlu_result / nlg_result 等不回写主 cxt、随帧丢弃；主 cxt 的 nlu/nlg = join 执行时的写入值（无 join 则保留分发前值，下轮 begin_turn 出清） |
| 预算 / 并发 | budget 每轮重置（max_hops）；全部 Task 终止后才进 end_turn，无悬挂协程；turn 级异常走今日统一话术路径，graph_state 照常下轮出清 |

**与 defer 的交互**：轮末换底座是**单底座**概念。规则：发生过 fan-out 的
turn，分支内写的 defer 事件降级为观测（warning + ChatResult.actions 快照，
不生效）；join 模块作为 turn 最终执行者写 defer 合法；纯线性 turn 行为完全
不变。

**持久化**：分支缓冲不直写库（D4.3），合并时由主 cxt 统一 `add_message`
重放缓冲（带 `metadata.branch`），store/压缩/快照通道零改动；`graph_state`
为 cxt 一等字段，随轮末快照落盘（同 metadata 通道，无新表、无 schema
变更）；分支帧本身轮内瞬态，不进 SessionStore。

**并发安全**：parallel 泳道间不共享可变单例槽（D4.3 影子）；graph_state 的
并行写全部走分支缓冲、合并点串行合并（无锁）；预算扣减在帧执行前同步完成
（asyncio 单线程无竞态）；`ensure_mcp_ready` / settings 缓存读取均为幂等读，
无写竞争。

### D9 兼容性承诺（逐位不变清单）

| 面 | 承诺 |
|---|---|
| `ModuleJumpEvent` 构造签名 / to_dict | 不变（测试锚点冻结） |
| 单目标跳转的 trace 序列（route_hit → module_jump → …） | 不变 |
| `pop / peek / reroute / detect_after_stage` 单值路径 | 不变（drain 是新增旁路） |
| `max_hops=2` 的线性 pattern 行为 | 逐位等价（含 force_close else 分支；退化路径不触碰 graph_state） |
| `ChatResult.actions` 对单跳的快照形态 | 不变 |
| 流式协议（delta/round/done）与 aggregate_turn | 不变（branch 是可选新字段；聚合器过滤是新增行为，仅影响带标记事件） |
| store schema / 压缩 / 快照 | 零改动（graph_state 走既有 cxt 快照通道） |
| 现有 pattern（customer_agent / deep_research / deep_research_multi） | 不改一行即可继续跑 |

---

## 5. 替代方案对比（为什么不是它们）

**A. 留在 executor 配方层（agent-as-tool 手工拼）**——ARCHITECTURE.md 已有
配方先例，"多 agent 分发"确实能在父 loop 内把子模块注册成工具自拼。
否决理由：预算/force_close/trace/流式/历史分段全部要每个配方重造且必然漂移；
executor_multi 已示范了手工拼的复杂度上限（metadata 传状态 + 手写接力 +
手动复位底座）。配方层保留为逃生舱，引擎层把一对多变一等公民。

**B. 扩展 ModuleJumpEvent 加 targets 字段**——见 D1 决策一（语义混淆 +
冻结既有签名）。消费端归一化让两个类型共存无成本。

**C. 完整 DAG 执行引擎（节点级依赖、显式 join 点、join_policy=any/quorum、
跨泳道数据流）**——表达力最强但过度设计：目标场景 S1-S3 都是"一次分发 +
一次汇聚"的两层结构，跨泳道依赖用两次串行分发表达（D4.5）就够了。调度器
保持分支 Task + join barrier 而非 DAG 图，复杂度差一个量级。真 DAG 需求
出现时（map-shuffle-reduce 型）再立独立方案。

**D. 事件携带 payload（边运货）**——本方案初稿的形态。否决理由见 D1
决策二：审计面膨胀、嵌套分发要递归收集事件树、join 模块拿不到"合并后的
单一素材"。langgraph 的边/状态分工是更成熟的切法。

**E. per-round gather（每帧对齐的并行）**——本方案第二稿的形态。否决
理由：快分支被迫陪跑慢分支（分支 1 走 1 帧、分支 2 要 3 帧时，分支 1 的
完成被 barrier 拖到分支 2 之后）；join 点才是唯一必要的同步点。已改为
分支独立 Task（D3）。

---

## 6. 分阶段落地（建议的实施切分）

| 阶段 | 内容 | 交付后的能力 |
|---|---|---|
| P1 | D1 事件 + D2 drain/expand + D3 调度器（独立分支 Task + 线性退化）+ D7 预算/不变量 | 串行多目标 + 默认 concat 合并可用（dispatcher executor 声明式分发，S1 的引擎级形态） |
| P2 | D4 全量（GraphState + 分支帧 + join 协议/聚合配置 + D4.5 依赖门控）+ D8 轮末状态契约 + defer 降级 | **S2 并行分发 + S3 混合**可用（dr_multi 配方改写为声明式作为验收样例） |
| P3 | D6 流式 branch 标记 + aggregate_turn 过滤 + trace 补全 + 预算/状态审计日志 | 观测面完整；旧消费者零改动验证 |
| deferred | D5（ROUTE 多意图 NLU 协议 / 节点与 pattern 配置 / 提示词闸门） | S1 的 NLU 端到端——引擎层稳定后单独立项 |

每阶段验收：`pytest` 全绿 + 现有 pattern 回归（deep_research_multi /
customer_agent_route 不改代码原样跑）+ 新增对应测试文件
（test_dispatch_events / test_graph_state_merge / test_branch_parallel /
test_dispatch_budget / test_turn_end_state）。

## 7. 风险与开放问题

1. **分支历史重放成本**：合并时逐行 `add_message` 重放缓冲——行数有限
   （单分支 ≤ loop 轮次 × 工具数），可接受；若实测有压力，备选方案为
   "轮末快照兜底、跳过逐行 sink"（代价：崩溃时丢分支细节）。**P2 定案**。
2. **reducer 类型冲突**：两分支写同键不同类型按"派发序靠后者胜 + warning
   trace"处理（`state_merge` 事件可观测）；配方侧约定命名空间（每分支独占
   键）是最简单的规避，文档化到配方指南。
3. **graph_state 膨胀**：findings 类 list 键无上限——预算天然限轮次，终态
   落快照前可配置裁剪键列表（`dispatch.snapshot_keys`，与 persist_keys
   一并设计）。**P3 审计项**。
4. **parallel 下 max_tokens/速率**：N 路并行 LLM 调用的限流与退避沿 provider
   层现有机制，未新增；P2 验收时压测 3 泳道并发。
5. **join 模块的 executor 协议**：join 模块是普通 AGENT 模块（自选 executor），
   读 state 的约定是配方级（同 dr_synthesize 先例）还是框架注入
   `{__dispatch_results__}` 插槽——**倾向后者**（D4.4 已按此写），
   P2 实现时以提示词工程实际手感定案。
6. **依赖门控的键拼写错误**：`wait_state_keys` 声明了永远不会被写的键 →
   分支永久 pending，预算耗尽才收尾。缓解：join/预算收尾时对仍 pending 的
   帧发 warning trace（`state_merge` 之外补 `gate_timeout` 观测）；P2 实现
   时评估是否需要注册期静态校验（pattern 内可推断的键拼写）。
7. **`jump_modules` 的 NLU 输出稳定性**：随 D5 一并 deferred；届时验收含
   "单意图 pattern 不产出 jump_modules"的反向测试。
