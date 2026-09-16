---
name: nexus-app-builder
description: Convert a business requirement into a nexus-kit application (a Pattern graph + capability plugins under apps/). Use when the user wants to turn a business process, workflow, or requirement into an app on this framework, create a new app under apps/, or design a pattern graph (fsm or agent) with nodes, executors, stages, or tools.
---

# nexus-app-builder（中文对照版）

> 本文件是 [SKILL.md](SKILL.md) 的中文版；`references/` 下的参考文档为英文，
> 术语与路径两边一致。两版如有出入，以英文版为准。

将业务需求转化为 nexus-kit 应用：一个 **Pattern**（数据模版——节点图）加
**能力插件**（executor / stage / tool），落在 `apps/<name>/` 下。

按下面的阶段依次推进，全程自主——任何一步都不停下来等待用户确认。
Phase 1 的选型陈述与 Phase 2 的方案仍是强制产物，但不是许可申请：图类型
或节点交互设计选错意味着整个应用重画。

## 框架 60 秒速览

| 层 | 目录 | 职责 |
|---|---|---|
| 内核 | `nexus/` | Pattern/BaseNode 模型、chat 引擎、注册中心——应用永不修改 |
| 原子 | `atoms/` | 默认 executor（`default_loop`/`default_fsm`）、内置 stage、内置 tool |
| 应用 | `apps/` | **你写代码的地方。** 一个应用一个目录：`route.py`（pattern + 注册），可选 `executor.py` / `stages.py` / `prompts.py` / `tools.py` / `config.yaml` |
| 宿主 | `host/` | FastAPI 组装 + 自动发现（`host/main.py`） |

- 应用在 import 时通过模块级 `registry.register(...)` 自注册；
  `host/main.py` 用 AST 扫描 `apps/*/*.py` 完成发现，不需要改任何中央文件。
- 图类型只有两种——`pattern_type="fsm"`（每轮用户消息前进一个节点）或
  `"agent"`（每条用户消息从入口跑完整张图）。细节见
  `references/architecture.md`。
- 节点之间通过 `cxt.graph_state`（状态板）共享状态；节点经
  `NodeExecutor` 执行，解析顺序为 `node.plugins > pattern.plugins > 类型默认`。
- tool 默认拒绝：一个 tool 只有同时命中 `pattern.allow_toolset`（toolset 层）
  **和** `node.use_tools`（节点层）才可调用。

## Phase 0 — 侦查（先事实后观点）

1. 按需阅读本 skill 的 references（索引见文末）。
2. 摸底模版库——`apps/` 下的 8 个应用就是模版库
   （`references/template-index.md` 给了业务形态 → 应用的映射）。
3. 精读最接近的 1–2 个模版源码。**源码是唯一事实**，本 skill 的文档是
   精编快照、可能漂移，写码前必须用 introspect CLI 交叉核对：

   ```bash
   PY=nexus-introspect-skill/introspect.py
   python $PY apps                              # 有哪些应用、各注册了什么
   python $PY pattern archify --view yaml       # 任意 pattern 的声明形态
   python $PY pattern install_booking_agent --view resolved
   python $PY plugin executor af_repair         # 某插件码的实现源码
   python $PY who-uses stage install_unified    # 反向索引：谁在引用它
   ```

## Phase 1 — 图类型选型

用以下判据决定 `fsm` 还是 `agent`（已对照 `nexus/engine/chat.py` 核实）：

- **fsm** —— 需求是引导式对话：助手问、用户答，**每条用户消息恰好前进
  一个节点**。信号：槽位/表单收集、多轮协商、逐轮确认、天然回环（重问、
  改约）。回复由 stages（NLU/NLG）产出，通常不需要自定义 executor。
- **agent** —— **一条用户消息要触发端到端交付物**（报告、产物、调研
  答案），由多节点流水线自主跑到终止。条件边就是 executor 的路由输出
  （`TurnResult.next`）。中途需要人工介入用 `wait_human`（图挂起，下一
  条用户消息回到同一节点续跑）。
- 拿不准 → 默认 **agent**（框架默认值，`nexus/model/pattern.py:36`），
  但必须说明 fsm 信号为什么不占上风。

**先陈述决策再前进（不等待）：** 选定的 `pattern_type`、引用的判据（哪些
信号成立）、一段式节点草图（5–10 个节点 code 加一句话用途）——该陈述
进入 Phase 2 的方案。此时不要展开完整设计。

## Phase 2 — 方案

产出包含以下**全部五件**的方案。缺节点交互表、或"零借鉴"没有显式声明，
方案无效。

1. **模版借鉴清单 + 差异分析。** 默认代码级 fork：复制 `apps/<模版>/`
   作为起步骨架（route 注册惯用法、prompts 组织、config 接线全部白得）。
   对每个模版写明：保留 / 删除 / 改造 / 新增了哪些节点，为什么。确实没有
   值得借鉴的，必须**显式**声明"零借鉴"并给理由——悄悄跳过扫描是不允许的。
   ⚠️ 只走代码路径。**绝不**用 studio 的 fork-to-edit 路径
   （`POST /api/v1/studio/patterns/fork` 会写 `host/config/patterns/`——
   那是 studio 的地盘，在你必须遵守的 apps/ 边界之外）。
2. **全节点清单。** 每个节点的 `code / name / 用途 / sub_nodes / is_end`，
   fsm 另加 slots 与 stages 骨架，agent 另加各节点绑定的 executor
   （`plugins={"loop": ...}`）。
3. **节点交互表（强制）。** 每个节点一行：**读**、**写**哪些
   `graph_state` 键；对每一个循环（设计→验证→修复→验证…）写明
   **检查点与经验继承策略**：第 N 轮如何继承第 1..N-1 轮学到的东西
   （历史数组、最优检查点、失败动作台账）。模板与实例见
   `references/pitfalls.md`。这是最容易踩坑的设计决策；框架的参考答案在
   `apps/archify_agent/executor.py`（`val_history` / `best_checkpoint` /
   `repair_log` / `solver_tried`）。
4. **能力分流表。** 需求中每一项能力一行：

   | 能力性质 | 去向 |
   |---|---|
   | 开放语义判断、起草、文风、路由、摘要 | **prompt 原生节点**（base prompt；default_loop 或单次 LLM 调用） |
   | 精确数值计算、几何、一切确定性变换、外部/领域 API | **tool**（`apps/<name>/tools.py`，应用级注册）或自定义 executor 内的确定性代码——绝不能只靠 prompt |
   | 编排、状态桥接、收敛闸门、JSON 协议、验收裁定 | **自定义 `NodeExecutor`**（`apps/<name>/executor.py`） |

   规则：凡是可能静默出错的地方（算术、坐标、规范符合性、"这个文件合不
   合法"），就不属于 prompt。完整契约见
   `references/plugin-and-tool-guide.md`。
5. **产物清单。** 将要创建的每一个文件：`apps/<name>/...`（新目录）、
   `tests/test_<app>_route.py`（新文件），仅此而已。定好 pattern code 并
   确认不与现存冲突（`python $PY apps`）。

**方案落笔后直接进入 Phase 3，不做任何确认停留。** 五件套方案写入最终
汇报，供用户事后审计决策。

## Phase 3 — 实现

- fork/复制模版目录后，code 出现的**每一处**都要改名：`route.py`
  （`Pattern(code=...)`、节点 code、插件 code）、`config.yaml`（`pattern:`
  键，如有）、测试。残留模版的插件 code 会在 import 时抛 `插件冲突`
  （注册中心拒绝同 code 不同 factory）。
- 文件：`route.py`（pattern 声明 + `registry.register(pattern)` + 底部
  `import apps.<name>.executor`）、按方案的 `executor.py` / `stages.py` /
  `prompts.py`、`__init__.py`、`README.zh.md`（既有应用的惯例）。
  `config.yaml` 可选——目前全仓只有 `apps/archify_agent/config.yaml` 一份；
  需要应用级 `llm:` 覆盖、`loop:` 预算、`guardrails:` 或 `config:` 自由 bag
  （放 `workspace_root: data/<app>`）时才加。
- 运行时产物只写 `data/<app>/...`，按会话组织
  （`data/<app>/<净化后的-session-id>/`，见 archify 惯用法）。状态板上
  一律绝对路径（相对路径在 file 工具与 bash 工作目录下解析结果不同——
  真实事故，见 pitfalls）。
- 新 tool 放 `apps/<name>/tools.py`，用同样的模块级惯用法注册。**不要**
  写进 `atoms/tools/`——确有跨应用复用价值时，在最终报告里建议晋升，
  而不是直接动手。

## Phase 4 — 验证（全部强制；红灯 = 未完成）

1. `pytest tests/test_architecture.py` —— 分层守门必须保持绿。
2. 新增 `tests/test_<app>_route.py`，离线、不联网：结构断言
   （节点/边/类型）、`validate_pattern(pattern)`、脚本化 fake-provider
   对话走查。惯用法照抄 `tests/test_install_booking_agent_route.py`
   （fixtures：`register_fake_provider`、`fake_llm_config`、
   `discover_builtin_patterns`；辅助在 `tests/async_utils.py`）。
   这个测试**就是**冒烟测试——fake-provider 走查全绿即证明注册、分发、
   executor 接线端到端正确。
3. 如实汇报：过了什么、什么是桩、还剩什么。

任何一步红灯或被跳过，都不得宣告成功。

## 安全红线（硬约束——无例外，无需用户豁免）

1. **只写**：`apps/<name>/`（新目录）、`data/<name>/`（运行时产物，
   gitignored）、`tests/test_<app>_*.py`（仅新增文件）。
2. **绝不修改**：`nexus/`、`atoms/`、`host/`、`ui/`、任何既有应用、
   既有测试、`docs/`、`pyproject.toml`、`skills/`。
3. **绝不写** `host/config/patterns/` 或 `host/config/plugins/`
   （studio 专属），以及仓库根之外的任何路径。
4. 密钥绝不进应用代码或 config.yaml——只走环境变量 /
   `host/config/local_config.yaml`。
5. tool 授权保持双层默认拒绝（`allow_toolset` ∩ `use_tools`）；
   绝不"为了跑通"而放宽授权。

## References（英文）

| 文件 | 内容 |
|---|---|
| `references/architecture.md` | 分层、两种图类型的精确运行语义、分发、注册中心、配置、持久化 |
| `references/pattern-schema.md` | Pattern / BaseNode 字段表、YAML 往返、构造期校验规则 |
| `references/plugin-and-tool-guide.md` | NodeExecutor 契约、TurnResult/Send、注册惯用法、tool 注册中心、LLM 调用惯用法、分流规则 |
| `references/template-index.md` | 8 个模版应用 → 业务形态映射；fork 清单 |
| `references/example-archify-agent.md` | agent 范本解剖：9 站图、修复循环、状态板、预算 |
| `references/example-install-booking.md` | fsm 范本解剖：stages 管线、slots、守卫、路由测试惯用法 |
| `references/pitfalls.md` | 节点交互表模板、graph_state 规则、经验继承模式、已知陷阱 |
| `references/toy-app/` | 完整最小 agent 应用（起草→评审→修订→交付），可整段复制作为起步骨架 |
