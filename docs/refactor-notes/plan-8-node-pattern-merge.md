# 计划⑧：node/module 合并 —— node + pattern 二层模型重构设计方案

- 日期：2026-09-12
- 状态：**已共识（三轮拷问定案），实施中**
- 前置：计划⑦（dispatch 分发机制）**作废归档**——本方案取代其目标场景
- 定案方式：三轮设计拷问（pattern_type 分流 / 图运行时 / tools 收口），全部分支走到叶子

---

## 1. 目标与非目标

### 1.1 目标

1. **合并 node 与 module**：三层结构（Pattern → Module → Node）塌缩为两层
   （Pattern → Node）。`ModuleType`（agent/fsm/route）消亡，类型概念上移为
   `pattern.pattern_type`（fsm/agent）。
2. **pattern_type 成为引擎分流依据**：FSM 走 FSM executor + stages 管线，
   节点按轮次推进；AGENT 走自研图运行时，整图运行（可静态可动态）。
3. **tools 权限收窄**：deny-by-default，节点不声明即无工具。
4. **AGENT 图支持挂起/恢复**（人工审批、等待人工输入），从会话重载直至终节点。

### 1.2 非目标

- 不引入 langgraph 依赖（只借鉴概念：编译期静态图、条件边、state、
  interrupt/checkpointer）。
- 不做引擎级运行时并行分支/动态扇出（执行契约留扩展位，deep_research 的
  多子问题检索由节点 executor 内部循环表达）。
- 不做子图节点（node 引用另一个 pattern）。
- 不保留兼容层（硬切：旧 modules API、旧 YAML 格式、convert.py 全部删除）。

---

## 2. 新模型契约

### 2.1 Pattern

```python
class Pattern:
    def __init__(self,
                 code,
                 name: str,
                 description: str,
                 pattern_type: Optional[str] = None,      # "fsm" | "agent"，默认 "agent"
                 entry_node_code: Optional[str] = None,   # 空 = nodes[0].code
                 nodes: Optional[List[BaseNode]] = None,  # 空 = 自动造默认节点
                 stages: Optional[List[Dict[str, str]]] = None,   # FSM 骨架
                 plugins: Optional[Dict[str, str]] = None,        # 见 2.3 槽位表
                 agent_hooks: Optional[str] = None,       # 语法糖 → plugins
                 allow_toolset: Optional[List[str]] = None,        # 见 §4
                 config: Optional[Dict[str, Any]] = None,          # 单一真源
                 **kwargs):
```

- **config 单一真源**：显式参数（stages/plugins/agent_hooks/allow_toolset/
  max_steps）是语法糖，构造时折叠进 config（显式参数胜出）；运行期统一从
  config 读取。`max_steps` 默认 10（AGENT 图步数预算）。
- **nodes**：Python 声明主路径收 BaseNode 对象；YAML/序列化收 inline dict。
  空时自动造 `code=pattern.code` 的默认节点（继承 pattern 层 plugins、无
  工具）——单节点 AGENT 图三行可声明。
- **`max_hops` 删除**：FSM 每轮恰好推进一个节点（无预算）；AGENT 用
  `max_steps`。

### 2.2 BaseNode

```python
class BaseNode:
    def __init__(self,
                 code: Optional[str] = None,
                 name: Optional[str] = None,
                 description: Optional[str] = None,
                 task_description: Optional[str] = None,
                 sub_nodes: Optional[List[str]] = None,   # FSM=转移合法集 / AGENT=图邻接
                 answer_examples: Optional[List[str]] = None,
                 stages: Optional[Dict[str, str]] = None, # 节点层 stages 覆写（FSM）
                 slots: Optional[Dict[str, str]] = None,  # 业务槽定义，仅 FSM
                 use_tools: Optional[List[str]] = None,   # 空 = 无工具
                 is_end: Optional[bool] = False,
                 **kwargs):                               # prompt 等自由字段 → config
```

- `sub_nodes` 一个字段两种语义，编译期按 pattern_type 定：FSM = next_node
  合法值集；AGENT = 静态图邻接边。
- `slots`（原 node_slots）仅 FSM：AGENT pattern 的节点声明 slots → 构造期
  raise。AGENT 节点要业务状态走 config 自由字段（不入 filled_slots 生命周期）。
- `answer_examples` 字段保留不限 FSM（xianyu 的 AGENT 规则 executor 消费），
  文档标注主要用途为 FSM。
- 旧字段改名对照：`node_code→code`、`node_name→name`、
  `node_description→description`、`node_todo_description→task_description`、
  `node_slots→slots`；`base_nlu_prompt/base_nlg_prompt` → config。

### 2.3 plugins 槽位表（新）

| 槽位 | 解析目标 | 说明 |
|---|---|---|
| `loop` | registry kind=executor | AGENT 节点执行器（default_loop / 自定义 / 规则 executor） |
| `fsm` | registry kind=executor | FSM 执行器 |
| `messages_builder` | kind=messages_builder | AGENT 消息构建器 |
| `agent_hooks` | kind=agent_hooks | agent 循环 hooks 包 |
| `llm` | settings LLM profile code | **新增**；声明式只存 code，不内联密钥 |

- `route` 槽位删除；值类型收窄为 **str/None**（callable 过渡期结束）。
- 层级规则：pattern.plugins 填空槽、node.plugins 胜出（同 slot 以 node 为准）。
- executor 解析链：`node.plugins["loop"]` > `pattern.plugins["loop"]` >
  default_loop（FSM 同理走 "fsm" 槽）。

### 2.4 编译期校验（构造 fail-fast）

- 节点 code 唯一；`sub_nodes` 边不悬空；`entry_node_code` 可解析。
- `pattern_type` 非法值 raise；agent pattern 声明非空 stages → raise。
- agent pattern 节点声明 `slots` → raise。
- 工具校验（`use_tools` 悬空 / 越出 `allow_toolset` 工具集）在
  `validate_pattern`（注册期，工具注册表已热身后）进行——构造期拿不到工具
  注册表。

---

## 3. 执行模型（engine/chat 按 pattern_type 分流）

### 3.1 FSM 路径

- FSM executor（`plugins["fsm"]` 解析）拉起 stages 管线，**两层解析**：
  `node.stages > pattern.stages 骨架值 > builtin 默认`（module 层删除）。
- next_node 按轮推进（NLU 输出 + sub_nodes 硬校验）、clarify 轮跳过、
  终节点 `is_end` → conversation_end。无预算（环是自然语义）。
- 原 ROUTE 应用（xianyu）迁为 AGENT 图（见 §6）。

### 3.2 AGENT 图运行时（自研，借鉴 langgraph 概念）

**编译期**（注册时）：Pattern → 运行时图。校验边悬空、entry 可达；允许环。

**运行期**（每条用户消息）：

1. `cxt.graph_state` 有挂起游标 → **恢复模式**：用户消息 = resume 值，
   从暂停节点续跑（不重头跑）。
2. 否则从 `entry_node_code` 起步。
3. 循环：解析节点执行器（`node.plugins["loop"] > pattern.plugins["loop"] >
   default_loop`，default_loop = 现有 ReAct 工具循环整体降格为节点执行器，
   `_MAX_TOOL_ROUNDS` 等守卫原样）→ 执行 → 消费路由输出。
4. **条件边 = 节点执行契约的路由输出**：TurnResult 返回 `next`
   （单值映射 sub_nodes；list 类型留扩展位——运行时扇出/并行分支的
   扩展点，本期串行消费或不实现）。
5. **终止**：节点无后继且 next 为空 / 命中 `is_end` 节点 / `max_steps`
   耗尽（force_close 收尾话术，沿用现有先例）。

**挂起/恢复（借鉴 langgraph interrupt/checkpointer）**：

- 触发：节点执行器返回 `wait_human=True` 信号（TurnResult 契约字段）。
- 持久化：挂起游标 + 图状态板存 `cxt.graph_state`（一等字段），随
  sessions 表落盘——进程重启可续（thread = 会话）。
- 恢复：下一轮用户消息作为等待节点的输入，**恢复时该节点重新执行、
  interrupt 点返回用户输入**，续跑到终节点。
- 语义细节：每会话同时至多一个挂起图（turn_lock 已保证轮次串行）；
  无 TTL（图挂到会话结束或节点自行清除）；离题 v1 一律当作对等待节点
  的回答续跑（同 FSM clarify 语义）；**节点重执行意味着副作用（工具调用）
  幂等责任在节点 executor**，v1 文档化此责任，不做事重放。
- "每轮全图重跑"（customer_agent 闲聊图）与"跨轮可挂起工作流"（审批流）
  是同一引擎的涌现行为，零配置开关：从不 wait_human 的图自然单轮跑完。

---

## 4. tools 权限模型（三层收口，deny-by-default）

1. **toolset 标签**：工具注册自带 toolset（builtin 已有：knowledge / mcp）；
   MCP server 注册时标明 toolset（配置字段，默认 = server 名）。
2. **`pattern.allow_toolset`**：工具集级授权，空 = 无任何工具集。
3. **`node.use_tools`**：具体工具名，**空 = 不能用任何工具**。

生效集 = `node.use_tools ∩ (toolset ∈ pattern.allow_toolset 的工具)`；
注册期（validate_pattern）对悬空/越集 fail-fast；运行期幻觉名拦截 +
回填可用列表保留（现机制不动）。

**删除**：工具注册时 `allowed_patterns` ACL 及其全部转换逻辑（含 MCP
manager 里 server allowed_patterns → 注册 ACL 的转换）；`_resolve_tools`
的 pattern ACL 分支；`_resolve_lent_tools`（借出机制整体删除）。

---

## 5. 删除清单

| 删除项 | 替代 |
|---|---|
| `nexus/model/module.py` 全家（BaseModule/ModuleType/sub_modules/lend_tools/lend_knowledge/enable_project/agent_stage/`_init_node` 别名） | BaseNode 字段 + plugins 槽位 |
| `nexus/model/convert.py`（pattern_to_module） | 无（模块复用场景不存在了） |
| `ModuleJumpEvent` / `ModuleJumpChannel` / hop 循环 | AGENT 图运行时的路由输出 |
| `DeferredModuleSwitch` / `defer_to_module` 工具 | 条件边读 cxt 状态（metadata flag） |
| 节点级 `jump_module` | FSM next_node / AGENT 条件边 |
| route 模块类型 + route executor + `plugins["route"]` 槽 | xianyu 迁 AGENT 图 |
| `pattern.max_hops` | FSM 无预算；AGENT `config.max_steps` |
| `_DeferredNLG`（pipeline.py，为 ROUTE 换节点服务） | nlg 即时解析（无同轮换节点场景） |
| NLU 协议 `jump_module` 字段 | 无 |
| 单模块版 deep_research executor（executor.py） | 四节点 AGENT 图版（route_multi 迁移） |
| 计划⑦ dispatch 设计 | 本方案（归档不删文档） |

---

## 6. 迁移影响面

### 6.1 五个 app

| app | 新形态 |
|---|---|
| customer_agent | AGENT 两节点图：customer_service —条件边→ human_handoff；转人工 flag 写 `cxt.metadata` 驱动路由（原 defer 语义），handoff 节点执行后清 flag |
| deep_research | **只留图版**：四节点静态 AGENT 图 preplan→plan→search→synthesize（sub_nodes 邻接），各节点挂原 dr_* executor（写事件 → 改返回 next）；search 节点内部循环多子问题 |
| install_booking | FSM：模块级 stages 上提 pattern 骨架（`[{"query": "time_aug_query"}, {"nlu": "install_unified"}, {"clarify": "install_clarify"}, {"nlg": "nlg_pass_through"}]`），node_slots→slots，sub_nodes/answer_examples/is_end 原样 |
| repair_booking | 同 install_booking |
| xianyu | AGENT 图：root 节点挂自定义 executor（内含意图分类，原 xianyu_intent_nlu 逻辑）+ 条件边分发；议价拒绝节点挂规则 executor（answer_examples 直出，零 LLM）；原"轮末回根"由"每轮从 entry 跑全图"天然满足 |

### 6.2 基础设施

- **store**：`current_module_code` 列 → `current_node_code`；新增
  `graph_state` 列（TEXT/JSON）。一次性 ALTER 迁移；活跃会话恢复改读新列。
- **serialization**：YAML 只认新格式（nodes inline dict 列表）；
  `pattern_from_dict` 走完整构造路径（归一化 + 编译校验）。
- **trace**：删 `module_jump/route_hit/route_root`；增
  `graph_compile`（注册期）、`node_start/node_end`（带 node_code、step）、
  `graph_wait/graph_resume`。delta/round/done 流式协议不变；
  `ChatResult.actions` 快照补 wait 信号。**事件表先冻结再动 ops-console**。
- **visualize**：三层图改两层（pattern → node 图；AGENT 渲染邻接 + 条件边，
  FSM 渲染状态机）。
- **ops-console（ui/）**：trace 树从"模块跳转边"改渲染"节点步进 + 挂起状态"。

### 6.3 跨轮状态（AGENT）

挂起游标之外，AGENT 图跨轮状态 = cxt 现有字段（metadata + filled_slots +
history），不新增图级持久状态。

---

## 7. 实施切分

P1 模型层 → P2 引擎层（关键路径：图运行时 + 挂起恢复）→ P3 atoms 层 →
P4 五 app 迁移 → P5 store/CLI/visualize（可与 P4 并行）→ P6 console/docs。

**检查点策略（对"每阶段全绿"的诚实修正）**：硬切无兼容层意味着中间态
无法全量绿（module 被 ~40 文件引用，P1 改签名即全局断裂）。约定：

- P1/P3 完成点：对应层的新测试子集绿（`pytest tests/test_pattern_yaml.py
  tests/test_plugins_field.py …`）。
- **全量绿检查点 = P4 完成后、P5 完成后、P6 完成后**。
- P2 期间引擎测试预期红，P4 期间 app 测试预期红——以 todo 清单跟踪，
  不静默跳过。

---

## 8. 决策记录（三轮拷问裁决）

| # | 裁决 | 选择 |
|---|---|---|
| 1 | ROUTE 归宿 | 归 AGENT（入口路由节点 + 条件边） |
| 2 | langgraph | 自研轻量图运行时，只借鉴概念，零新依赖 |
| 3 | 计划⑦ | 作废（本方案取代） |
| 4 | stages 归属 | FSM 专属；AGENT 属性走 plugins（loop/messages_builder/llm） |
| 5 | tools | node.use_tools 为准（空=拒绝）；注册 allowed_patterns 删除；pattern 增加 allow_toolset；MCP server 标 toolset |
| 6 | config 单一真源 | 显式参数是语法糖折叠进 config，冲突显式胜 |
| 7 | 迁移策略 | 硬切，无兼容层，store 一次性 ALTER |
| 8 | slots | 独立字段保留，仅 FSM（AGENT 声明即 raise） |
| 9 | nodes 形态 | Python 收对象；YAML inline dict；空造默认节点 |
| 10 | 边表达 | 复用 sub_nodes；条件边 = 路由输出 next |
| 11 | 动态性 | 本期不做运行时扇出/并行/子图（留扩展位） |
| 12 | 挂起恢复 | 借鉴 langgraph interrupt/checkpointer：wait_human 信号 + graph_state 落盘 + 恢复重执行该节点 |
| 13 | deep_research | 单模块版删除，只留图版 |
| 14 | 幂等责任 | 恢复时节点重执行，副作用幂等责任在节点 executor（v1 文档化） |
