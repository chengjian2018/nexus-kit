# nexus-kit 架构

> 本文档是架构的**权威描述**，随每个重构计划更新（变更记录见
> `docs/refactor-notes/plan-N.md`）。README 只保留概览与运行说明。

## 四层分层

```
host  (3)  组装根：FastAPI 入口 / CLI / 配置装载 / 会话治理
 └─> apps (2)  组合层：业务 pattern（xianyu_agent、customer_agent）+ 各自 prompt 资产
      └─> atoms (1)  原子积木：executors / stages / tools / providers / knowledge / augmentation
           └─> nexus (0)  内核：context / model / pipeline / engine / registry / llm / settings
```

依赖方向单向向下（host→apps→atoms→nexus），由 `tests/test_architecture.py`
在每次 pytest 时强制（importlinter 配置在 `pyproject.toml`，为本地人工审计
工具）。

关键内核纯度规则：**nexus 永不 import atoms**。默认实现（executor / 兜底
stage）由 atoms 在 import 时反向注册进内核（见"插件中心"节）——与
`pipeline.register_default_generate` 同一模式。

## 核心执行流（一轮对话）

```
host POST /api/v1/chat
  → chat_turn (nexus/engine/chat.py)
      1. begin_turn（context_lifecycle 重置每轮字段；graph_state 不清——挂起游标跨轮）
      2. R1 刷新 llm_config（plugins["llm"] > settings 分层 > llm_override）
      3. maybe_compress（历史压缩）→ 记录 user 消息
      4. 按 pattern.pattern_type 分流：
         "fsm"   → FSM executor（plugins["fsm"] > default_fsm）：
                    入口节点解析 → R3 → stages（两层解析）→ next_node 轮末跳转
         "agent" → 图运行时（chat._run_agent_graph）：
                    挂起游标存在则恢复（resume_input=本轮消息），否则从 entry 跑全图；
                    每步：R4 按节点刷新 → 解析 loop executor → execute → 消费
                    TurnResult.next（条件边，须在 sub_nodes 内）或
                    TurnResult.sends（运行时扇出，见下节）；
                    wait_human → 挂起（游标+步数入 graph_state，轮次结束）；
                    终止 = 无后继 / is_end / max_steps 耗尽（兜底话术）
      5. end_turn → build_chat_result（text + actions 快照）
```

## AGENT 图运行时（计划⑧；计划⑨扩展运行时扇出）

自研轻量图运行时（零第三方依赖，借鉴 langgraph 的概念：编译期静态图 /
条件边 / interrupt-checkpointer / Send 式扇出），取代计划⑥-⑦时代的事件接力机制
（ModuleJumpEvent / hop 循环 / DeferredModuleSwitch 全部删除）：

- **图 = 静态邻接**：`node.sub_nodes` 一个字段两种编译期语义——FSM =
  next_node 合法转移集，AGENT = 图邻接边。条件边不在边上挂函数，而在
  **节点执行契约的路由输出**（`TurnResult.next`，单值映射 sub_nodes；
  list 形态为遗留容忍——串行消费首个，扇出改用 `sends` 声明）。
- **运行时扇出（计划⑨，map-reduce）**：`TurnResult.sends=[Send(node,
  input), ...]` 派发 N 个 worker 实例（**异构扇出**：每个 send 指向自己的
  已声明 sub_node，同一节点多实例 = 同构特例，"子 agent"零新概念）——
  `asyncio.gather` 并发执行（同事件循环交错，
  无锁），每个实例跑在结构隔离的私有工作区（cxt 浅拷贝：空 history、
  message_sink 切断、Send.input 作显式查询）；实例落定即写入结果板
  `graph_state["__fanout_results__"]`（完成序），全部落定后执行 merge 节点
  （= 各目标 worker `sub_nodes` 交集的**唯一**共同后继；恰好一个 worker
  无共同 merge 时忽略该 worker 并告警执行剩余，merge 不可唯一解析则拒绝
  执行并提示模板正确性。join 为普通节点语义：可路由可挂起）。失败分支
  落 error 条目不阻塞图；分支内 wait_human/嵌套扇出 = 该分支失败。
- **每条用户消息跑全图**（从 entry），或**从挂起节点恢复**——两种行为同一
  引擎零配置：从不 wait_human 的图（闲聊客服）自然单轮跑完，会挂起的图
  （审批流）跨轮延续。
- **挂起/恢复**（langgraph interrupt 语义）：节点执行器返回
  `TurnResult.wait_human=True` → 图暂停，游标 + 步数记账写
  `cxt.graph_state`（sessions 表落盘，进程重启可续）；下一轮用户消息作为
  `ec.resume_input` 送达，**恢复时该节点重新执行**——副作用（工具调用）
  幂等责任在节点执行器（v1 文档化责任，不做事重放）。
- **预算三层守卫（各管各维度）**：`config.max_steps`（默认 10）限主循环
  节点执行次数（扇出节点 + join 各占 1 步，**worker 实例不占图步数**；
  挂起续跑跨轮延续记账）；`config.max_fanout`（默认 8）限一次 sends 的
  实例宽度；executor 内部守卫（`_MAX_TOOL_ROUNDS` 等）限分支内轮次。
  耗尽 → force-close 兜底话术。环因此是合法语义。FSM 无预算——每轮恰好
  一个节点，也不收 sends。
- **跨轮状态**：挂起游标走 graph_state；业务状态走 cxt 既有字段
  （metadata / filled_slots / history），无图级持久状态。

### In-repo 实例：`deep_research`（四节点图 + 引擎级扇出，计划⑨验收）

`apps/deep_research_agent/route_multi.py`——preplan→plan→search→synthesize
四个节点：plan 站产出子问题后 `sends=[Send(dr_search, …) × N]` 引擎级扇出
（宽度受 max_fanout 截断），search 站是 worker（一实例一子问题、私有工作区、
每实例独立轮次守卫，实例隔离结构化取代了旧的全局覆盖度启发式），synthesize
站是 join（合并预检索资料与全部分支成果，失败分支降级不阻塞）——检索延迟
从"子问题之和"降为"最慢分支"。相位间状态走 `cxt.graph_state`（图终止自动
清空）。单模块版 executor 已删除（图版即其声明式形态）。

## 插件中心（计划①引入）

`nexus/registry/plugins.py::PluginRegistry`——引擎扩展点的统一存放处，
字符串 kind（新扩展点无需改 API）：

| kind | 内容 | 注册者 |
|---|---|---|
| `executor` | 节点执行器：ReAct 工具循环 / FSM pipeline | `atoms/executors/` + app 自有执行器 |
| `stage` | 具名 stage（stages 声明引用的字符串 code；FSM 专属） | `atoms/stages/__init__` + app 自有 stage |
| `stage_factory` | 内置兜底 stage 工厂（`pipeline.register_default_*` 的内部存储） | `atoms/stages/__init__` |
| `messages_builder` | AGENT 消息构建器（内核注册 `default`） | apps（customer_agent 等） |
| `agent_hooks` | hooks 包（机制保留，默认 no-op） | 计划④定案 |

### API

```python
registry.register(kind, code, factory)   # 冲突：不同 factory 占同 (kind,code) → ValueError；同 factory 幂等
registry.resolve(kind, code)             # 实例缓存（factory 只调一次；executor/stage 约定无状态）
registry.has(kind, code)                 # 只查存在不实例化（校验用）
registry.deregister(kind, code)
registry.default_executor_code(pattern_type)  # "agent"→"default_loop"、"fsm"→"default_fsm"
```

### Executor 契约（NodeExecutor）

- 接口：`NodeExecutor.execute(ec: ExecutionContext) -> TurnResult`
  （`nexus/engine/execution.py`；`ModuleExecutor` 是迁移期别名）
- `ExecutionContext`：`cxt / pattern / node / force_close / stream /
  resume_input / step`。**包装而非扩展 DialogueContext**——cxt 是被持久化的
  数据载体，瞬态执行输入不进 cxt；executor 不持有 Session。
- `TurnResult`（`nexus/engine/turn_result.py`）：`content`（回复文本）/
  `next`（路由输出——AGENT 图条件边）/ `wait_human`（挂起信号）/
  `actions`（事件通道）/ `extra`（开放扩展袋）。
- 解析链（chat 层 `_resolve_node_executor_code`，AGENT 节点）：
  `node.plugins["loop"] > pattern.plugins["loop"] > default_loop`；
  FSM pattern：`pattern.plugins["fsm"] > default_fsm`。
- 默认实现：`atoms/executors/{loop,fsm}_executor.py`，codes
  `default_loop / default_fsm`。内核无兜底 executor——注册表未预热时
  fail-fast，报错指引 import atoms.executors。
- AST 自动发现：`discover_builtin_plugins()` 扫 `atoms/executors/*.py` 的
  模块级 `registry.register(...)`（host/main.py 与 host/cli.py 装配时调用；
  tests/conftest.py 预热）。app 自有执行器（xianyu 路由器、dr_* 相位）同法
  注册。

## 声明式模型（计划⑧：node + pattern 二层）

Pattern → Node 两层（module 层已删）。所有字段均为常见值类型
（str/bool/list/dict），**无对象引用**——yml 序列化与配置化直达。

```python
Pattern(code, name, description,
        pattern_type="fsm"|"agent",   # 默认 agent；引擎分流键
        entry_node_code=None,         # 默认 nodes[0]
        nodes=[BaseNode, ...],        # 空 = 自动造 code=pattern.code 的默认节点
        stages=[{槽位: code}],        # FSM 专属骨架（AGENT 声明 raise）
        plugins={槽位: str},          # 见下表
        allow_toolset=[str],          # 工具集授权（空 = 无）
        config=dict,                  # 单一真源：显式参数是语法糖（冲突显式胜）
        **kwargs)                     # 自由字段 → config

BaseNode(code, name, description, task_description,
         sub_nodes=[后继code],        # FSM=转移合法集 / AGENT=图邻接
         answer_examples=[], stages={},   # stages 仅 FSM
         slots={},                    # 业务槽定义，仅 FSM（AGENT 声明 raise）
         use_tools=[],                # 空 = 无工具（deny-by-default）
         is_end=False, plugins={}, config={},
         **kwargs)                    # base_prompt 等提示词资产 → config
```

旧字段改名对照：node_code→code、node_name→name、node_description→
description、node_todo_description→task_description、node_slots→slots；
模块级概念（sub_modules / lend_* / enable_project / executor 直配 /
jump_module / max_hops）全部删除。

### stages 体系（FSM 专属）

- **默认骨架**（内核）：六槽 `[pre_recall, query, post_recall, nlu,
  clarify, nlg]`，值全 None；`normalize_skeleton` 构造期 fail-fast
- **两层解析**：`node.stages > pattern 骨架值 > builtin 兜底`（模块层已删）
- **通用跳过规则**：解析后仍 None 的槽位不执行；clarify 声明即启用
- **unified 去重**：nlu/nlg 同 code 只 execute 一次；其它重复 code 仅首个
  生效（校验报错）
- **合法 next 值**（unified stage 硬校验）：当前节点 sub_nodes + ""（+
  "clarify"，当 node.stages 声明了 clarify 槽位）
- **具名 stage codes**：`fsm_nlu/fsm_nlg/fsm_unified/nlg_pass_through/
  time_aug_query/clarify_default` + app 自有 code

### plugins 声明字段（`nexus/model/plugins_field.py`）

`plugins: Dict[str, str]`（槽位名 → code），pattern 与 node 两层
（node 压 pattern），值**只收 str/None**（callable 过渡期已关闭）：

| 槽位 | 解析目标 | 含义 |
|---|---|---|
| `loop` | executor | AGENT 节点执行器 |
| `fsm` | executor | FSM 执行器 |
| `messages_builder` | messages_builder | AGENT 消息构建器 |
| `agent_hooks` | agent_hooks | 循环 hooks 包 |
| `llm` | llm_providers code | LLM provider 声明（settings 解析，不内联密钥） |

### 工具授权（deny-by-default 三层收口）

1. **toolset 标签**：工具注册自带（builtin：`knowledge` / `mcp`；MCP
   server 工具：`mcp-<server名>`）。注册期 `allowed_patterns` ACL 已删。
2. **pattern.allow_toolset**：工具集级授权（空 = 无）。
3. **node.use_tools**：具体工具名（**空 = 无工具**）。

生效集 = `use_tools ∩ allow_toolset 工具集`（`loop._resolve_tools`）；
注册期 `validate_tools` 对悬空/越集 fail-fast；运行期幻觉名拦截 + 错误
回填供模型自纠（不变）。

### 编译期与注册期校验

- **构造期**（`Pattern.__init__`）：节点 code 唯一、sub_nodes 边不悬空、
  entry 可解析、pattern_type 合法、AGENT 节点声明 slots raise、AGENT
  pattern 声明 stages raise
- **注册期**（`nexus/model/validation.py`，收集全部错误一次性编号 raise）：
  `validate_base_info`（含 AGENT 节点 stages 报错、不可达节点软警告）/
  `validate_plugin_declarations`（str code 可解析、stages 槽位属骨架、
  unified 唯一合法重复）/ `validate_tools`（悬空/越集）。调用时机：host
  startup `_validate_registered_patterns`（失败 SystemExit）+ CLI
  pattern-load

### yml round-trip（`nexus/model/serialization.py`）

`pattern_to_dict/from_dict/to_yaml/from_yaml`——节点 inline（完整字段
dict 列表），无 node 注册表；`from_*` 走完整构造路径（编译期校验照跑），
加载后校验是调用方责任。CLI：`pattern-export <code>` / `pattern-load`。

### 与四个领域 registry 的关系

patterns / tools / providers / channels 四个领域注册中心**保持独立**。插件
中心只承接引擎扩展点。共享 AST 扫描在 `nexus/registry/discovery.py`，
注册习惯（模块级 `registry.register()`，无装饰器）不变。

## 引擎内核工具箱（executor 可复用）

留在 `nexus/engine/chat.py` / `loop.py`、被 atoms executor 反向 import 的
内核助手（合法方向：atoms→nexus）：

- `chat.py`：`_refresh_llm_config`（R1 轮级 / R3 FSM 节点 / R4 AGENT 节点，
  统一读 plugins["llm"] 声明）、`_resolve_entry_node`、`_run_stages`
  （两层解析）、`_fsm_node_transition`、`_handle_node`、图运行时
  `_run_agent_graph`。**R1-R4 patch 锚**：这些函数在 chat 命名空间解析
  `get_llm_config`（tests patch `nexus.engine.chat.get_llm_config`），
  不得移走。
- `loop.py`：`_resolve_tools`（三层收口）/ `_dispatch_tool_calls` /
  `_parse_args` / `_execute_tool` + 框架强制项（force-close 后缀、prompt
  长度告警）。

## 布局

| 包 | 职责 |
|---|---|
| `nexus/context.py` | DialogueContext（current_node_code + graph_state）/ SessionMessage / PipelineStage |
| `nexus/model/` | Pattern + BaseNode 二层模型 + plugins 字段 + serialization + validation（构造期编译 + 注册期收集式校验） |
| `nexus/pipeline.py` | 槽位骨架 + 两层延迟解析（FSM 专属）；兜底 stage 工厂经插件中心存储 |
| `nexus/engine/` | chat（分流 + 图运行时）/ execution（NodeExecutor 契约）/ turn_result / loop（工具箱）/ agent_hooks / messages / streaming / 压缩 / 持久化 |
| `nexus/registry/` | discovery（共享 AST 扫描）/ plugins（插件中心）/ patterns / tools / providers / channels |
| `nexus/llm/` | Provider 抽象 + 解析 |
| `nexus/settings.py` | 运行时设置（LLM 分层配置 llm_default ⊕ pattern_llm ⊕ pattern_llm.nodes、压缩、DB 路径） |
| `nexus/channels/` | ChannelSpec 协议 + 通用 webhook 装配 |
| `atoms/executors/` | 两默认 executor（default_loop / default_fsm） |
| `atoms/stages/` | nlu / nlg / unified / query / recaller / clarify + 默认 prompt |
| `atoms/tools/` | knowledge / mcp 工具（toolset 标签授权单元） |
| `atoms/providers/` | OpenAICompatible Provider（dashscope / zai） |
| `atoms/knowledge/` | SQLite 知识库 |
| `atoms/mcp/` | MCP 连接管理器（工具动态注册 toolset `mcp-<server>`） |
| `apps/<name>/` | 业务 pattern（route.py：节点图 + 执行器）+ prompt 资产 + 渠道适配 |
| `host/` | main.py / cli.py / governor.py / config/ |

## Agent hooks（计划④后状态：机制保留，默认 no-op）

6 个点位（P1 on_agent_start / P2 on_llm_call / P3 on_llm_response /
P4 on_tool_call / P5 on_tool_result / P6 on_agent_end；on_transfer 随
defer 机制删除）在默认 loop executor 中照常调用；事件类字段携带
`node_code`；声明解析读 `plugins["agent_hooks"]`（node 层压 pattern 层）。
无声明时所有点位零开销直通（`tests/test_agent_hooks_contract.py`）。
错误语义：hook 异常一律吞掉记日志保原值，对话永不阻塞。

## 流式协议（计划⑤引入）

**底层默认流式，非流式 = 聚合流式。** 全链路：

```
provider.chat_completion_stream ──yield LLMChunk──► collect_stream（聚合）
        │                                          │
        │ text deltas（乐观转发）                    ▼ 传统 dict 形状
        ▼                                    provider.chat_completion
   StreamEmitter                              （引擎中间产物照旧消费）
        ▲
        │ drain & re-yield
chat_turn_stream ──yield ChatStreamEvent(delta|round|trace|done)──► 消费者
        │
        └ 聚合 = chat_turn（外部行为字节级不变；HTTP/渠道零感知）
```

- **LLMChunk**（`nexus/llm/types.py`）：`text / tool_calls(delta 片段) /
  finish_reason / usage`；聚合器按 index 合并、arguments 增量拼接
- **双向桥**：非流式 provider 包装为单 chunk 流（FakeProvider 零改动存活）
- **引擎层**（`nexus/engine/streaming.py`）：`ChatStreamEvent(kind:
  delta|round|trace|done)` + `StreamEmitter`（executor 经 `ec.stream`）+
  `aggregate_turn`
- **trace 事件**（计划⑧新表）：`node_start / node_end`（图节点步进，带
  step）/ `node_jump`（FSM 轮末跳转）/ `graph_wait / graph_resume`（挂起
  与恢复）/ `graph_done`（终止，reason: terminal/is_end/max_steps/
  undeclared_edge）/ `tool_call + tool_result` / `conversation_end`。
  仅在状态真实变化时发射；`aggregate_turn` 忽略 trace。消费者：CLI
  events 模式与 SSE 调试端点、ops-console
- **实时桥**：整轮编排放后台 task，emit 即时入队转发；task 异常有兜底
  done，消费端提前关闭会 cancel task
- **stage 层回复流式**：unified 单次调用用 `ReplyFieldTap` 增量提取
  reply 字段；已转发文本记录在 `current_streamed_reply` 供去重
- **乐观转发 caveat**：done.result.text 是权威回复；聚合消费者零感知
- **SSE 调试端点**：`POST /api/v1/chat/stream`（env `NEXUS_STREAM_DEBUG=1`）

## 会话持久化（计划⑧列变更）

sessions 表：`current_node_code`（FSM 游标 / AGENT 图位置镜像）+
`graph_state`（JSON 状态板，含挂起游标——进程重启后恢复续跑）。打开旧库
时一次性迁移（drop `current_module_code`、add `graph_state`）。messages
表零 schema（工具轨迹走 content/metadata payload）。

## 热重载（2026-09-10）

不改代码结构的前提下，四类东西的运行时重载机制（`host/reload.py` 是
代码侧的装配点，`nexus/settings.py` 是数据侧的）：

### llm config — mtime 指纹缓存（`nexus/settings.py`）

`load_config` 按 resolved path 记 `(mtime_ns, size)` 指纹：每轮对话的
R1 刷新（`get_llm_config`）只 **stat** 不读文件；指纹变了才重新
读→校验→规范化。**无需任何显式 reload**——改 yaml 下一轮自动生效。

### pattern / plugin / channel — mtime re-import（`host/reload.py`）

注册发生在模块 import 时，"重载"= 按依赖序重新执行：

- **追踪**：sys.modules 里 `apps.<pkg>.<mod>` 与 `atoms.executors.<mod>`
  名下的模块；mtime 对比找变更
- **重放序**：AST import 边（含相对 import）的拓扑序
- **注册表收编**：plugin/channel 注册表有 `replace_on_conflict` 开关；
  pattern 注册本就覆盖同名（`rebind_sessions` 显式重绑内存会话，回填
  node_map）
- **channel router 不重建**：webhooks handler 每请求从 registry 活取 spec

### 宿主挂点

- `POST /api/v1/reload`：config 缓存失效 + 代码重载 + 会话重绑
- CLI `/reload` slash 命令；`NEXUS_RELOAD_WATCH=1` 后台轮询（默认关）

## 变更记录

- 计划①（2026-09-09）：插件中心 + executor 插件化 + discovery 统一。
- 计划②（2026-09-09）：模型声明式重构（stages 体系 + callable 字段 str 化）。
- 计划③（2026-09-09）：pattern yml round-trip + 校验体系。
- 计划④（2026-09-09）：hooks 清理 + 默认置空。
- 计划⑤（2026-09-09）：LLM 默认流式 + 引擎流式协议。
- 计划⑥（2026-09-09）：跳转/投影重构（transfer 删除 + enable_project +
  DeferredModuleSwitch）。
- 热重载（2026-09-10）：llm config mtime 指纹缓存 + pattern/plugin/
  channel 的 mtime re-import。
- plugins 合并字段 + pattern→module 转换（2026-09-10）：plugins dict 统一
  声明；`pattern_to_module` 转换。
- 计划⑦（2026-09-10 设计稿，**作废归档**）：一对多分发机制
  （ModuleDispatchEvent / GraphState / BranchFrame）——目标场景由计划⑧
  的图运行时接管。
- **计划⑧（2026-09-12）：node/module 合并**——三层塌缩为 Pattern → Node
  二层；`pattern_type`（fsm/agent）成为分流键；AGENT 图运行时（条件边 =
  TurnResult.next、max_steps 预算、wait_human 挂起/恢复借鉴 langgraph
  interrupt/checkpointer）；tools 三层收口（toolset → allow_toolset →
  use_tools，deny-by-default）；删除 module 全家 / ModuleJumpEvent /
  DeferredModuleSwitch / defer / ROUTE 类型 / max_hops / convert.py。
  详见 `docs/refactor-notes/plan-8-node-pattern-merge.md`。
