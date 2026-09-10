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
      1. begin_turn（context_lifecycle 重置每轮字段）
      2. 定位入口模块 → R1 刷新 llm_config（settings.get_llm_config 三级编排）
      3. maybe_compress（历史压缩）→ 记录 user 消息
      4. hop 循环（pattern.max_hops，默认 2）:
           _handle_module → 解析 executor 插件 → executor.execute(ExecutionContext)
           ModuleJumpChannel.pop → 有跳转事件则 reroute 同轮续答
      5. end_turn → build_chat_result（text + actions 快照）
```

## 插件中心（计划①引入）

`nexus/registry/plugins.py::PluginRegistry`——引擎扩展点的统一存放处，
字符串 kind（新扩展点无需改 API）：

| kind | 内容 | 注册者 |
|---|---|---|
| `executor` | 模块执行器：agent loop / FSM / ROUTE pipeline | `atoms/executors/` |
| `stage` | 具名 stage（stages 声明引用的字符串 code） | `atoms/stages/__init__` + app 自有 stage |
| `stage_factory` | 内置兜底 stage 工厂（`pipeline.register_default_*` 的内部存储） | `atoms/stages/__init__` |
| `messages_builder` | AGENT 消息构建器（内核注册 `default`） | apps（customer_agent 等） |
| `agent_hooks` | hooks 包（计划④前保留 str 解析能力） | 计划④定案 |

### API

```python
registry.register(kind, code, factory)   # 冲突：不同 factory 占同 (kind,code) → ValueError；同 factory 幂等
registry.resolve(kind, code)             # 实例缓存（factory 只调一次；executor/stage 约定无状态）
registry.has(kind, code)                 # 只查存在不实例化（校验用）
registry.deregister(kind, code)
registry.default_executor_code(type_value)  # "agent"→"default_loop" 等
```

### Executor 契约

- 接口：`ModuleExecutor.execute(ec: ExecutionContext) -> TurnResult`
  （`nexus/engine/execution.py`）
- `ExecutionContext`：`cxt / pattern / module / force_close / stream`（stream
  为计划⑤的流式发射器占位）。**包装而非扩展 DialogueContext**——cxt 是被
  持久化的数据载体，瞬态执行输入不进 cxt；executor 不持有 Session。
- `TurnResult`（`nexus/engine/turn_result.py`）：`content`（固有字段，回复
  文本）/ `actions`（事件通道）/ `extra`（开放扩展袋，消费端忽略未知 key）。
- 解析链（chat 层 `_resolve_executor_code`）：
  `module.executor > pattern.executor_{loop|fsm|route} > 类型默认码`。
  注意 pattern 字段后缀是 executor 家族名（loop/fsm/route），不是
  ModuleType 值（agent/fsm/route）。
- 默认实现：`atoms/executors/{loop,fsm,route}_executor.py`，codes
  `default_loop / default_fsm / default_route`。内核无兜底 executor——
  注册表未预热时 fail-fast，报错指引 import atoms.executors。
- AST 自动发现：`discover_builtin_plugins()` 扫 `atoms/executors/*.py` 的
  模块级 `registry.register(...)`（host/main.py 与 host/cli.py 装配时调用；
  tests/conftest.py 预热）。

## 声明式模型（计划②引入）

node/module/pattern 的所有字段均为常见值类型（str/bool/list/dict），
**无对象引用**——为 yml 序列化（计划③）与配置化铺路。

### stages 体系

```python
# pattern.stages：有序骨架，list[单键dict]（槽位名 → code 或 None）
Pattern(stages=[
    {"pre_recall": None},          # None = 运行时三层补值
    {"query": "time_aug_query"},   # pattern 级默认
    {"post_recall": None},
    {"nlu": "route_unified"},      # unified：nlu/nlg 可同填一个 code
    {"clarify": None},             # clarify：默认 None（声明即启用澄清）
    {"nlg": "route_unified"},
])
# module.stages / node.stages：Dict[str, str]（槽位名 → code）
RouteModule(stages={"nlu": "xianyu_intent_nlu", "nlg": "xianyu_fixed_nlg"})
```

- **默认骨架**（内核）：六槽 `[pre_recall, query, post_recall, nlu, clarify,
  nlg]`，值全 None；`normalize_skeleton` 构造期 fail-fast（非 list/非单键
  dict/非 str code → ValueError）
- **解析链**：`node.stages > module.stages > pattern 骨架值 > builtin 兜底`
  （nlu/nlg 兜底到 `register_default_generate` 注册的默认对；clarify **无
  兜底**——声明即启用）
- **通用跳过规则**：槽位三层解析后仍 None → 不执行（pre_recall/post_recall/
  clarify 均此语义）
- **unified 去重**：nlu/nlg 同 code → 只 execute 一次（unified stage 本就
  同时写 nlu_result/nlg_result）；其它重复 code 仅首个生效（计划③校验报错）
- **nlg 延迟解析**（继承旧 GenerateSlot 拆分的时序修复）：nlg 槽位在执行
  瞬间按当前节点解析——ROUTE 菜单命中切节点后，菜单级 nlg 同轮生效
  （pipeline `_DeferredNLG`）
- **删除的字段**：node/module 的 generate/pre_recall/query/post_recall
  显式槽位、module.enable_clarify（被 stages.clarify 吸收）、四个哨兵类
  （PreRecallSlot 等）与 GenerateSlot 展开逻辑
- **具名 stage codes**（atoms 注册）：`fsm_nlu/fsm_nlg/route_nlu/route_nlg/
  fsm_unified/route_unified/nlg_pass_through/time_aug_query/clarify_default`
  + app 自有 code（xianyu 的 `xianyu_intent_nlu/xianyu_fixed_nlg`）

### ModuleLink → dict

`sub_modules: List[Dict]`，形状 `{"target": str, "lend_knowledge": bool,
"lend_tools": [str]}`（str 简写自动包装）。Pattern 图校验（悬边/自环/越权）
与投影块构建均改读 dict。

### callable 字段 str 化

`messages_builder / agent_hooks`（module 与 pattern 层）均为 `Optional[str]`
插件 code；内核注册 `messages_builder: default`。transitional 兼容：可调用
对象仍可直接内联使用（messages.build_agent_messages 判断 callable）。

### 与四个领域 registry 的关系

patterns / tools / providers / channels 四个领域注册中心**保持独立**（载荷
异构：Pattern 对象 / schema+handler / ProviderEntry / ChannelSpec）。插件中
心只承接引擎扩展点。四 registry 原先各自复制的 AST 扫描代码已收敛到
`nexus/registry/discovery.py`（`module_registers(path)` + `import_modules()`），
注册习惯（模块级 `registry.register()`，无装饰器）不变。

### 旧约定废除说明

`nexus/engine/agents.py` 曾注明 "No registry here (CLAUDE.md: no new global
singletons)"——该约定已被插件中心**有意取代**：插件中心是引擎扩展点的唯一
中央存放处（与四个领域 registry 并列的第五个注册中心，而非无限全局单例）。
agents.py 现仅为过渡性 re-export。

## 引擎内核工具箱（executor 可复用）

留在 `nexus/engine/chat.py` / `loop.py`、被 atoms executor 反向 import 的
内核助手（合法方向：atoms→nexus）：

- `chat.py`：`_refresh_llm_config`（R1-R4）、`_resolve_entry_node`、
  `_run_stages`（含跳转检测）、`_fsm_node_transition`、`_default_skeleton`。
  **R1-R4 patch 锚**：这些函数在 chat 命名空间解析 `get_llm_config`
  （tests/test_llm_refresh.py patch `nexus.engine.chat.get_llm_config`），
  不得移走。
- `loop.py`：`_resolve_tools / _resolve_lent_tools / _dispatch_tool_calls /
  _parse_args / _execute_tool / build_transfer_tools`（工具解析与派发工具
  箱，test_customer_agent_route 的 import 锚）+ 框架强制项
  （force-close 后缀、prompt 长度告警）。

## 布局

| 包 | 职责 |
|---|---|
| `nexus/context.py` | DialogueContext / SessionMessage / ModuleJumpEvent / PipelineStage |
| `nexus/model/` | Pattern→Module→Node 三级模型 + 注册期 fail-fast 校验（executor 声明字段已加） |
| `nexus/pipeline.py` | 槽位骨架 + 三层延迟解析；兜底 stage 工厂经插件中心存储（公开 API register_default_* 不变） |
| `nexus/engine/` | chat（轮次编排+内核工具箱）/ execution（Executor 契约）/ turn_result / loop（工具箱+兼容门面）/ agents（过渡 re-export）/ hooks / messages / 压缩 / 持久化 |
| `nexus/registry/` | discovery（共享 AST 扫描）/ plugins（插件中心）/ patterns / tools / providers / channels |
| `nexus/llm/` | Provider 抽象 + 解析 |
| `nexus/settings.py` | 运行时设置（LLM 三级配置、压缩、DB 路径） |
| `nexus/channels/` | ChannelSpec 协议 + 通用 webhook 装配 |
| `atoms/executors/` | 三默认 executor（default_loop / default_fsm / default_route） |
| `atoms/stages/` | nlu / nlg / unified / query / recaller / clarify + 默认 prompt |
| `atoms/tools/` | knowledge / mcp 工具（MCP 动态注册 toolset `mcp-*`） |
| `atoms/providers/` | OpenAICompatible Provider |
| `atoms/knowledge/` | SQLite 知识库 |
| `atoms/mcp/` | MCP 连接管理器（专职线程 + event loop，工具动态注册 toolset `mcp-<server>`） |
| `apps/<name>/` | 业务 pattern（route.py）+ prompt 资产 + 渠道适配 |
| `host/` | main.py / cli.py / governor.py / config/ |

## Pattern 序列化与校验（计划③引入）

### yml round-trip（`nexus/model/serialization.py`）

- `pattern_to_dict / pattern_from_dict / pattern_to_yaml / pattern_from_yaml`
- dict/yml 形状镜像构造参数（type: agent/fsm/route → 对应子类；nodes 内嵌
  modules；jump_module 按可选属性带出）
- `from_*` 走完整构造路径（normalize_skeleton + 图校验 fail-fast 照跑）；
  **加载后校验是调用方责任**（CLI pattern-load 与 host 装配都会做）
- CLI：`python -m host.cli pattern-export <code> --out f.yml` /
  `pattern-load f.yml [--validate-only]`

### 校验体系（`nexus/model/validation.py`）

- `validate_base_info`：code/name/entry_module_code 非空且可解析、
  module_code 唯一（原来 module_map 静默覆盖）、FSM/ROUTE ≥1 节点、
  node_code 模块内唯一；name 缺失 = 软警告（仅日志）
- `validate_plugin_declarations`：executor/stages/messages_builder/
  agent_hooks 的字符串 code 经 `has()` 可解析（不实例化）；module/node
  stages 声明的槽位必须存在于 pattern 骨架；stage code 唯一性（nlu/nlg
  同 code 的 unified 形态是唯一合法重复）
- `validate_pattern`：**收集全部错误一次性 raise 编号 ValueError**
- 调用时机：host startup `_validate_registered_patterns`（发现预热后，
  失败 SystemExit）+ CLI pattern-load
- Pattern.__init__ 的图校验（悬边/自环/越权）不重复，保持原位

## Agent hooks（计划④后状态：机制保留，默认 no-op）

7 个点位（P1 on_agent_start / P2 on_llm_call / P3 on_llm_response /
P4 on_tool_call / P5 on_tool_result / P6 on_transfer / P7 on_agent_end）
在默认 loop executor 中照常调用；事件类、分发器签名、声明解析（str
code / callable / legacy dict 三形态）完整保留于
`nexus/engine/agent_hooks.py`。

**当前无任何 in-repo hooks 包**——无声明时所有点位零开销直通，这是唯一
受测契约（`tests/test_agent_hooks_contract.py`，13 用例）。恢复实现：注册
kind="agent_hooks" 插件包 + 从 git 历史恢复行为测试（计划④删除了 35 个
行为用例，4 个非 hooks 主Flow 的 loop 守卫用例迁移到
`tests/test_loop_tool_guards.py` 保留）。

| 点位 | 事件 | 语义（实现恢复后） |
|---|---|---|
| P1 | AgentStartEvent | 注入：返回 Optional[str] 片段 → builder 的 extra_blocks |
| P2 | LLMCallEvent | 观察：每次 LLM 调用前（messages 只读） |
| P3 | LLMResponseEvent | 观察：每次 LLM 响应后 |
| P4 | ToolCallEvent | 改写：Optional[RewriteToolCall]（name/args，allowed_names 守卫） |
| P5 | ToolResultEvent | 改写：Optional[str]（结果字符串） |
| P6 | TransferEvent | 观察：transfer 命中写跳转事件时 |
| P7 | AgentEndEvent | 观察：出口（reply / transfer / max_rounds） |

错误语义（保留契约）：hook 异常一律吞掉记日志保原值，对话永不阻塞。

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
chat_turn_stream ──yield ChatStreamEvent(delta|round|done)──► 消费者
        │
        └ 聚合 = chat_turn（外部行为字节级不变；HTTP/渠道零感知）
```

- **LLMChunk**（`nexus/llm/types.py`）：`text / tool_calls(delta 片段) /
  finish_reason / usage`。openai_provider 原生流解析 SSE 三特形：delta
  片段、usage-only 尾包（choices 为空数组）、finish_reason
- **聚合器**（`nexus/llm/aggregate.py::collect_stream`）：text 拼接、
  tool_calls 按 index 合并（id/name 取首个非空、**arguments 字符串增量
  拼接**非 JSON 合并）、finish/usage 取末值
- **双向桥**：基类 `chat_completion` 重写为聚合流式调用；默认流桥把只
  实现非流式的 provider 包装为单 chunk 流（FakeProvider 等零改动存活）
- **引擎层**（`nexus/engine/streaming.py`）：`ChatStreamEvent(kind:
  delta|round|done)` + `StreamEmitter`（executor 经 `ec.stream` 注入）
  + `aggregate_turn`。`chat_turn_stream` 为 generator 主体，`chat_turn`
  聚合包装
- **乐观转发 caveat**：finish_reason 轮末才到，无法预知最终轮——中间轮
  的 text delta 实时转发，轮末发 round 事件（outcome: tool/final/
  transfer/max_rounds），**done.result.text 是权威回复**；聚合消费者零
  感知
- **SSE 调试端点**：`POST /api/v1/chat/stream`（env `NEXUS_STREAM_DEBUG=1`
  门控挂载），非生产 API（同步 generator 占线程）

## 模块间移动模型（计划⑥引入）

transfer_to_XX 同轮移交工具族**已删除**。模块间移动现在是三条语义清晰的通道：

| 通道 | 触发 | 事件 | 消费时点 | 语义 |
|---|---|---|---|---|
| **投影代答 + 延迟切换** | AGENT 调 `defer_to_module` 工具（投影邻接自动生成） | `DeferredModuleSwitch`（cxt.actions） | 轮末（hop 循环后、end_turn 前） | 本轮照常用投影知识回答完；下一轮以目标模块为底座 |
| **同轮跳转** | ROUTE NLU `jump_module` / 菜单节点配置 / 自定义 executor 写事件 | `ModuleJumpEvent`（cxt.actions） | hop 循环（`ModuleJumpChannel`） | 同轮改道，目标模块立刻续答 |
| **（无移动）** | 直接回答 | — | — | 投影知识够用时原地作答 |

### enable_project 与互斥规则

- `BaseModule.enable_project: bool = True`：**子模块**声明自己如何被父模块消费——
  True = 投影服务（父 prompt 含投影块 + defer 工具）；False = 跳转目标
  （投影块不出现，仅供 ROUTE 跳转/自定义 executor 使用）
- 投影与同轮跳转互斥：一条邻接边要么投影要么可跳，不可同时
- **防乒乓（forced_projection）**：模块发生跳转/被 defer 后记入
  `cxt.metadata["forced_projection"]`（跨轮保留），此后任何模块枚举到它
  一律按投影服务。**绝不 mutate Pattern/Module 单例**（跨会话共享）——
  会话级覆盖走 cxt.metadata
- 校验：enable_project=True 的边必须 lend_knowledge 或 lend_tools
  （否则父模块无从代答，注册期报错）

### 跳转多样化配方（自定义 executor）

三种移动事件都可以在**自定义 executor 插件**中产生（需求 5.1 的
agent-as-tool / delegate 等，框架不内置、按配方实现）：

```python
from nexus.context import ModuleJumpEvent, DeferredModuleSwitch

class AgentAsToolExecutor(ModuleExecutor):
    """把子 agent 当工具调：子模块本轮产出被父模块采编（不真移动）。"""
    def execute(self, ec):
        child = ec.pattern.module_map["child"]
        # 在父的 loop 内把 run(child) 注册为普通工具；子回复作为工具结果
        # 回到父的上下文。需要真正移交时写事件：
        ec.cxt.actions.append(ModuleJumpEvent(
            target_module_code="child", reason="...", source="agent_as_tool"))

class DelegateExecutor(ModuleExecutor):
    """委派任务：子模块完成后带着结果回父模块（deferred 往返）。"""
    def execute(self, ec):
        ec.cxt.actions.append(DeferredModuleSwitch(
            target_module_code="worker", reason="task...", source="delegate"))
```

- 写 `ModuleJumpEvent` → 同轮续答（消费端是 hop 循环）
- 写 `DeferredModuleSwitch` → 轮末换底座（消费端是 chat 层
  `_apply_deferred_switch`）
- 声明方式：`module.executor="my_delegate"`（插件中心 kind="executor"）

## 热重载（2026-09-10）

不改代码结构的前提下，四类东西的运行时重载机制（`host/reload.py` 是
代码侧的装配点，`nexus/settings.py` 是数据侧的）：

### llm config — mtime 指纹缓存（`nexus/settings.py`）

`load_config` 按 resolved path 记 `(mtime_ns, size)` 指纹：每轮对话的
R1 刷新（`get_llm_config`）只 **stat** 不读文件；指纹变了才重新
读→校验→规范化。**无需任何显式 reload**——改 yaml 下一轮自动生效。
返回深拷贝（pattern_llm 校验会原地清非法键，不能污染缓存）；解析失败
缓存不落盘（上次的合法结果继续可用）。编程入口：
`reload_config()`（强制重读）/ `invalidate_config_cache()`。

### pattern / plugin / channel — mtime re-import（`host/reload.py`）

注册发生在模块 import 时，"重载"= 按依赖序重新执行：

- **追踪**：扫描域 = sys.modules 里 `apps.<pkg>.<mod>` 与
  `atoms.executors.<mod>` 名下的模块（含 prompts 等非注册支撑模块）；
  mtime 对比找变更。不做 AST 注册谓词过滤——编辑中途的语法错文件恰
  恰最需要重载反馈（重放失败 → 告警 + 保持旧注册）。
- **重放序**：AST import 边（含相对 import）的拓扑序。不能用
  sys.modules 插入序——import 机制在模块 body 执行**前**插入
  sys.modules，消费者反而排在依赖前面。
- **注册表收编**：plugin/channel 注册表有 `replace_on_conflict` 开关
  （重放窗口内临时打开：替换条目 + 清实例缓存；默认 False 保持"同名
  冲突拒绝"的严格模式）。pattern 注册本就覆盖同名。
- **channel router 不重建**：webhooks handler 每请求从 registry 活取
  spec（`_live_spec`），replace 后下一请求生效。

各注册表的重载语义：**pattern** 覆盖同名，运行中会话持旧引用跑完在途
轮次（`rebind_sessions` 显式重绑内存会话）；**plugin** 新类替换 +
实例缓存清空；**channel** spec 替换。**不在范围**：tools / MCP /
providers（重启进程）。

### 宿主挂点

- `POST /api/v1/reload`（host/main.py）：config 缓存失效 + 代码重载 +
  会话重绑；startup 时 `init_baseline()` 建基线，首个 reload 即可检测
  开机以来的变更
- CLI `/reload` slash 命令（同语义）
- `NEXUS_RELOAD_WATCH=1`：后台线程轮询 mtime 自动 reload（开发期便利，
  默认关）

## 变更记录

- 计划①（2026-09-09）：插件中心 + executor 插件化 + discovery 统一。
  详见 `docs/refactor-notes/plan-1.md`。
- 计划②（2026-09-09）：模型声明式重构（stages 体系 + ModuleLink→dict +
  callable 字段 str 化）。详见 `docs/refactor-notes/plan-2.md`。
- 计划③（2026-09-09）：pattern yml round-trip + 校验体系。详见
  `docs/refactor-notes/plan-3.md`。
- 计划④（2026-09-09）：hooks 清理 + 默认置空。详见
  `docs/refactor-notes/plan-4.md`。
- 计划⑤（2026-09-09）：LLM 默认流式 + 引擎流式协议。详见
  `docs/refactor-notes/plan-5.md`。
- 计划⑥（2026-09-09）：跳转/投影重构（transfer 删除 + enable_project +
  DeferredModuleSwitch）。详见 `docs/refactor-notes/plan-6.md`。
- 热重载（2026-09-10）：llm config mtime 指纹缓存 + pattern/plugin/
  channel 的 mtime re-import（`POST /api/v1/reload` / CLI `/reload` /
  `NEXUS_RELOAD_WATCH`）。详见上文"热重载"节。
- plugins 合并字段 + pattern→module 转换（2026-09-10）：executor 族 /
  messages_builder / agent_hooks 合并为 stages 同款 `plugins` dict
  （pattern 与 module 两层，旧字段经 property 兼容读写）；
  `pattern_to_module` 声明式转换（入口模块为基底 + pattern 身份 +
  plugins 折入）。详见上文"plugins 合并声明字段"与"pattern → module
  转换"节。
