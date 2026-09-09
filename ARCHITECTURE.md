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
| `stage_factory` | 内置兜底 stage 工厂（`pipeline.register_default_*` 的内部存储） | `atoms/stages/__init__` |
| （后续计划）`stage` / `messages_builder` / `agent_hooks` | 具名 stage、消息构建器、hooks 包 | 计划②/④ |

### API

```python
registry.register(kind, code, factory)   # 冲突：不同 factory 占同 (kind,code) → ValueError；同 factory 幂等
registry.resolve(kind, code)             # 实例缓存（factory 只调一次；executor 约定无状态）
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
| `atoms/tools/` | calculator / weather / knowledge 工具 |
| `atoms/providers/` | OpenAICompatible Provider |
| `atoms/knowledge/` | SQLite 知识库 |
| `apps/<name>/` | 业务 pattern（route.py）+ prompt 资产 + 渠道适配 |
| `host/` | main.py / cli.py / governor.py / config/ |

## 变更记录

- 计划①（2026-09-09）：插件中心 + executor 插件化 + discovery 统一。
  详见 `docs/refactor-notes/plan-1.md`。
