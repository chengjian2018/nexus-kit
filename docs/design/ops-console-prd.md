# PRD：运营配置台（nexus-console）—— 知识库与 node / module / pattern 配置

> 状态：**设计稿（未实现，本文档不含任何实现承诺）**｜日期：2026-09-10
> 读者：运营（需求方）/ 研发（依赖方）/ 实现者
> 关联：[ARCHITECTURE.md](../../ARCHITECTURE.md) · [introspect-skill.md](introspect-skill.md)（面向 agent 的第 2 方受众）

---

## 1. 背景与定位

### 1.1 三方受众与配置台的位置

本项目（nexus-kit）服务三类受众，各自消费同一套"注册表即数据"的事实源：

| 受众 | 载体 | 读写性 | 关注点 |
|---|---|---|---|
| **运营人员** | **本 PRD 的 Web 配置台（nexus-console）** | 选择性读写 | 知识库内容、话术（prompt / 回答范式）、FAQ、节点流程与槽位、模型参数 |
| agent | `nexus-introspect` skill（另稿设计） | 只读 | 应用清单、pattern 装配、插件实现定位、改动影响面 |
| 研发 | 代码库本体 | 全量读写 | executor / stage 实现、provider、MCP 接入、内核 |

三者的公共底座是同一组注册表（`nexus/registry/`：patterns / plugins / tools / channels）与声明式模型（`nexus/model/`）。配置台与 introspect skill 共享"resolved 装配视图"等查询能力（见 §3.1、§8），不重复建设。

### 1.2 问题陈述：今天"改配置 = 改代码"

现状下运营的每一项日常修改都要动 Python 源码并重启/重载，证据（均为仓库现行代码）：

| 运营想改 | 实际要动 | 位置 |
|---|---|---|
| install / repair 的 FAQ 一条 | `FAQ_ENTRIES` Python 列表 | `apps/install_booking_agent/faq.py:25-78` |
| 闲鱼意图关键词 / 议价规则 / 违禁词 | `TECH_KEYWORDS` / `DEFAULT_BARGAIN_SETTINGS` / `BLOCKED_PHRASES` 常量 | `apps/xianyu_agent/route.py:78-111,316-322` |
| 任何话术 prompt | `apps/*/prompts.py` 字符串常量（customer_agent 甚至内联在 route.py） | `apps/*/prompts.py`、`apps/customer_agent/route.py:147-230` |
| 节点话术 / 槽位 / 跳转 | `route.py` 里节点构造参数 | `apps/install_booking_agent/route.py:104-395` |
| 知识库内容 | 仅 `cli knowledge-seed` 演示种子，无增删改查界面 | `host/cli.py:1076-1091` |
| 转人工时间 | `_BUSINESS_HOURS` 常量 | `apps/customer_agent/route.py:42` |

而框架侧的声明式模型已把 pattern 的结构面完全数据化（node/pattern 全字段 str/bool/list/dict，yml round-trip，收集式校验）——**配置台要做的就是把这条已有的数据通路接到 UI 上，而不是发明新的配置体系**。

### 1.3 目标

1. 运营人员**零代码**完成：知识库内容管理、FAQ 管理、话术（prompt / answer_examples）调整、节点流程与槽位调整、pattern 级模型参数调整。
2. 每次修改走**草稿 → 校验 → 发布 → 可回滚**闭环，错误拦截在发布前（校验层）而非线上（对话层）。
3. 研发的实现资产（executor / stage 代码、provider 凭据、MCP 传输）**对运营不可见或只读**，边界清晰。

### 1.4 非目标（本期明确不做）

- 不做对话调试 REPL（CLI `chat` 与 SSE 端点 `POST /api/v1/chat/stream` 已有；P2 仅做只读入口链接）。
- 不做 stage / executor / messages_builder 的**实现**编辑（那是研发代码域；运营只能在注册码目录中**选择**）。
- 不管理 `llm_providers` 连接凭据（api_key / api_base）、DB 路径、MCP 传输配置——红线清单见 §4.4。
- 不做 A/B 实验、多环境发布编排、灰度发布。
- 不做画布式拖拽图编辑（图一期只读 + 表单编辑；见 §6.3.1 决策 D-图）。

---

## 2. 用户与使用场景

### 2.1 角色

| 角色 | 说明 | 典型操作 |
|---|---|---|
| 运营编辑 | 日常配置修改者 | 改 FAQ、改话术、加知识、调槽位描述 |
| 运营管理员 | 有发布权 | 校验、发布、回滚、空间管理 |
| 研发 | 只读 + 依赖交付 | 查看生效装配、排查"线上为什么这么说" |

P0/P1 阶段不建账号体系，全体共用服务级鉴权（§10）；角色作为页面内**操作门控**的预留字段先行设计。

### 2.2 核心用户故事

| # | 故事 | 优先级 |
|---|---|---|
| U1 | 作为运营，我要新增/修改/下架一条客服知识，并立刻试搜验证能否命中，不用找研发 | **P0** |
| U2 | 作为运营，我要批量导入商品知识（含 markdown 详情），并在界面上核对 | **P0** |
| U3 | 作为运营，我要查看某个 pattern 的流程结构图和当前装配，理解"这个机器人是怎么搭的" | **P0** |
| U4 | 作为运营，我要改某节点的回答范式（answer_examples）和话术 prompt，校验通过后发布，新会话生效 | **P1** |
| U5 | 作为运营，我要增改 FAQ 条目（关键词 → 答案），改完能预览一句话命中哪条 | **P1** |
| U6 | 作为运营，我要给某个 pattern 换模型 / 调 temperature，不看也碰不到 API key | **P1** |
| U7 | 作为运营管理员，我要查看发布历史，出问题时一键回滚到上一版本 | **P1** |
| U8 | 作为研发，我要看某个 pattern 的**生效**装配（三层解析后的 stage/executor），定位问题 | P0（只读）/P1（resolved 视图） |

---

## 3. 现状盘点（代码库事实，设计输入）

### 3.1 可直接复用的资产

| 资产 | 位置 | 对配置台的意义 |
|---|---|---|
| 声明式模型（node/module/pattern 全字段 str/bool/list/dict） | `nexus/model/{node,module,pattern}.py` | **编辑表单的字段字典就是构造参数表**（附录 A） |
| yml round-trip | `nexus/model/serialization.py:118,124`（`pattern_to_yaml` / `pattern_from_yaml`） | 草稿与版本快照的存储格式，零新造 |
| 收集式校验 | `nexus/model/validation.py:53,130,246`（`validate_pattern` 汇总所有错误一次性抛出） | 发布闸门的错误展示直接复用其编号清单格式 |
| 注册期图校验（悬空边/自环/lend_tools 越权） | `nexus/model/pattern.py:69-105` | 前端即时校验的规则来源（§9） |
| pattern 运行时注册/替换（generation 计数） | `nexus/registry/patterns.py:70-149`；CLI `pattern-load` 已走"构造+校验+注册" | 发布 = 同一条通路的 HTTP 化 |
| **结构图渲染** | `nexus/visualize.py:86`（`pattern_to_mermaid`；模块 subgraph 按类型着色、sub_nodes 实线 / jump_module 虚线 / 投影·defer 与跳转目标双样式、终态高亮） | pattern 详情页的图视图**整体复用**，含图例语义 |
| 热重载 | `host/reload.py:238-272`（`POST /api/v1/reload` 全量重放；运行中会话持旧引用跑完，`rebind_sessions` 重绑） | 发布后的生效机制；其语义决定发布确认文案（§6.3.6） |
| 知识库 SQLite（WAL，scope 隔离） | `atoms/knowledge/store.py`：`product_knowledge` / `customer_service_knowledge` 两表（:28-56），`upsert_product` / `add_cs` / `search_products` / `search_cs`（:106-273），进程级单例（:355-379） | 知识库页的数据层已存在，缺 update/delete/enabled 切换 API 与 HTTP 面 |
| 会话/消息只读 API | `host/main.py:628,651`（`GET /sessions`、`GET /sessions/{id}/messages`） | P2 调试视图直接挂接 |
| 三级模型参数合并 | `nexus/settings.py:48-58`（`pattern_llm`：pattern → modules → nodes 逐级覆盖 `llm_default`）；llm 配置 mtime 自动生效 | 模型参数页的数据面；改后无需 reload |
| pattern → module 组装复用 | `nexus/model/convert.py:40`（`pattern_to_modules`） | "把现有 pattern 收编为模块"的进阶操作（P2，不在本期编辑器范围） |

### 3.2 缺口清单（配置台的后端/数据依赖，研发交付项）

| # | 缺口 | 现状证据 | 需要什么 |
|---|---|---|---|
| G-1 | pattern 无 HTTP 管理面 | export/load 仅 CLI（`host/cli.py:1094,1115`）；无列表/详情/草稿/发布端点 | §8 的 `/console/patterns*` API 族 |
| G-2 | 知识库无 update/delete/enabled | `store.py` 仅 upsert/add/seed；`enabled` 列存在但无 set 方法 | store 层补 API + HTTP 化 |
| G-3 | 知识库无 HTTP 面 | 仅 CLI seed | §8 的 `/console/knowledge*` |
| G-4 | FAQ 硬编码在代码 | `apps/*/faq.py` 的 `FAQ_ENTRIES` | 数据化迁移（§7.3）后才有 FAQ 页 |
| G-5 | pattern 编辑产物无处安放 | pattern 全部由代码模块级注册（AST 发现） | 控制台托管 yml 目录 + 启动/reload 后重载钩子（§7.1） |
| G-6 | 无账号/角色 | 仅单 `NEXUS_API_KEY`（`host/main.py:74-96`） | P0 接受服务级鉴权；P1 账号体系 |
| G-7 | xianyu 业务常量不可配 | 关键词表/议价/违禁词为 Python 常量 | 研发数据化（P2，依赖项非本 PRD 范围） |
| G-8 | 校验器死代码 | `validation.py:106` `return errors` 之后的投影边一致性检查（:109-127）**不可达** | 研发修复；前端仍按该规则提示（§9 注） |

---

## 4. 总体设计原则

### 4.1 声明式边界 = 编辑边界

运营可编辑的字段集合 ≡ `serialization.py` 可序列化字段集合（`_PATTERN_FIELDS` / `_MODULE_FIELDS` / `_NODE_FIELDS`，`serialization.py:24-46`）+ 知识库/FAQ/模型参数三块数据面。凡是字符串码引用（stage code、executor code、tool 名），UI 一律**下拉选择注册码目录**，不做自由文本——从交互上消灭"未注册 code"这一类校验错误。

### 4.2 单一事实源：fork-to-edit（决策 D-1）

一个 pattern code 在任一时刻只有**一个**可写事实源：

- **code-managed**：由 `apps/*/route.py` 声明（研发资产）。控制台**只读**，可查看/导出/复制。
- **console-managed**：由控制台 fork 代码 pattern 产生的 yml 副本（存于托管目录，§7.1），注册名不变、来源切换。控制台全功能编辑。

规则：
1. fork 是单向的（代码 → 控制台）。fork 后原代码声明不再生效（同 code 注册以 console yml 为准，加载顺序在代码发现之后，§7.1）。
2. 研发后续改了同名代码 pattern，控制台**不自动跟进**；详情页给出"源代码已变化，可重新 fork（覆盖当前草稿与版本历史）"的强提示。
3. 新建 pattern 只能以 console-managed 形态创建（从空白或从模板复制起步）。

> 备选方案（否决记录）：① 控制台直接写回 Python 代码——运营改代码正是要消灭的现状；② 全部 pattern 迁 yml——一次性重构量大且剥夺研发在代码里声明 pattern 的既有习惯（deep_research 这类重度自定义 executor 的 pattern 也不适合表单化）。fork-to-edit 让两群人各用顺手的工具，同 code 不并存双写。

### 4.3 草稿 → 校验 → 发布 → 回滚（决策 D-2）

```
草稿（可反复保存，不生效）
  → 校验（dry-run：pattern_from_yaml 构造 + validate_pattern，收集式报错）
  → 发布（register + 触发 reload；生成不可变版本快照 + 审计记录）
  → 回滚（= 以历史快照内容重新走发布）
```

发布语义对齐 `host/cli.py pattern-load`：构造 → 校验 → 注册；生效语义对齐 `host/reload.py`：**运行中会话持旧引用跑完当前轮，新会话用新版本**（发布确认文案必须如实告知，§6.3.6）。

### 4.4 运营红线（可见不可改 / 不可见）

| 项 | 处置 |
|---|---|
| stage / executor / messages_builder **实现代码** | 只读目录（注册码 + 描述 + 归属文件），不可编辑 |
| `llm_providers`（api_key / api_base / timeout / retries） | **不展示** |
| `mcp_servers` 传输配置（command/args/env/url/headers） | 不展示；仅 `allowed_patterns` 可在 P2 以开关面暴露 |
| DB 路径、压缩阈值、TTL、LRU 上限 | 不展示（研发域） |
| 框架默认 prompt（`atoms/stages/_prompts.py`、clarify 三模板） | 只读参考视图（帮助运营理解"我的覆盖 prompt 会替换掉什么"） |
| `atoms/knowledge/store.py` 检索实现 / scope 前缀 | 不可见；试搜台如实呈现关键词匹配行为（§6.4.5） |

---

## 5. 信息架构

```
nexus-console
├── 总览 Dashboard                        （P0）
├── 应用（Pattern）管理                    （P0 只读 / P1 编辑+发布）
│   ├── 列表页
│   └── 详情/编辑器
│       ├── ① 结构图（mermaid，只读）
│       ├── ② 模块（列表 + 表单）
│       │     └── 节点（表格式编辑 + 表单）
│       ├── ③ 流水线骨架（六槽）
│       ├── ④ 生效装配（resolved，只读）
│       └── ⑤ 版本与发布
├── 知识库                                 （P0）
│   ├── 空间（scope）管理
│   ├── 商品知识 / 客服知识（CRUD + 导入导出）
│   └── 试搜台
├── FAQ 管理                               （P1，依赖 G-4）
├── 模型与参数（pattern_llm）              （P1）
├── 发布中心（全局变更流 + 审计）           （P1）
└── 会话与调试（只读入口，链接既有 API）    （P2）
```

导航左侧固定五组（会话与调试 P2 再挂）；顶部全局搜索（pattern code / 模块 / 节点 / 知识标题直达）。

---

## 6. 页面详细设计

### 6.1 总览 Dashboard（P0）

- **应用卡片墙**：每个注册 pattern 一卡——名称 / code / 类型徽标（按入口模块类型）/ 模块数·节点数 / 事实源徽标（`code` / `console`）/ 最近发布时间。点击进详情。
- **知识库概要**：空间数、两表条目数、最近导入。
- **最近变更流**：最近 10 条发布/知识变更（谁、何时、改了什么、可跳转）。
- 数据源：`GET /console/overview`（聚合注册表 + 审计表）。

### 6.2 应用列表页（P0）

| 列 | 说明 |
|---|---|
| code / 名称 / 描述 | 来自 Pattern 对象 |
| 入口模块 / 类型 | `entry_module_code`；入口模块的 type 决定徽标 |
| 模块 / 节点数 | 遍历统计 |
| 事实源 | code-managed（只读徽标）/ console-managed（可编辑） |
| 草稿状态 | 无草稿 / 有未发布草稿（黄色标记）/ 草稿校验失败（红色） |
| generation | 注册代数（`registry` generation 计数），排障用 |
| 操作 | 查看；（console-managed）编辑；导出 yml；（code-managed）Fork 为可编辑副本 |

筛选：事实源、类型、草稿状态。当前注册 6 个 pattern（customer_agent / deep_research / deep_research_multi / install_booking_agent / repair_booking_agent / xianyu_agent）即为初始数据。

### 6.3 Pattern 详情 / 编辑器（核心页面）

布局：左侧结构树（pattern → modules → nodes，标注 entry / 终态），中间主区五标签页，右侧上下文帮助（当前选中元素的字段语义提示，内容取自附录 A 的"运营释义"列）。

```
┌────────────┬──────────────────────────────────────────────┬──────────┐
│ 结构树      │  [结构图] [模块] [流水线骨架] [生效装配] [版本]   │ 字段帮助  │
│ ▼ pattern  │ ┌──────────────────────────────────────────┐ │ 当前选中: │
│   ▸ mod A  │ │        mermaid 结构图（只读）              │ │ node     │
│     · n1   │ │  模块=色块子图 实线=跳转 虚线=jump/投影      │ │ todo 描述│
│     · n2   │ └──────────────────────────────────────────┘ │ 会注入   │
│   ▸ mod B* │  （console-managed 时顶部出现 [编辑草稿] 按钮）  │ NLU 上下文│
└────────────┴──────────────────────────────────────────────┴──────────┘
```

#### 6.3.1 结构图标签（只读，P0）

- 复用 `pattern_to_mermaid` 渲染，图例照搬 `visualize.py` 语义：ROUTE 蓝 / FSM 绿 / AGENT 橙子图；实线 = `sub_nodes`；虚线 jump_module / 重置回根；点划 `..>` 投影·defer vs `-.->` 跳转目标（按 `enable_project`）；终态紫框；⏵ 开始 = 入口。
- 图上节点可点击 → 跳到模块标签内对应节点表单（图是导航器，不是编辑器——**决策 D-图**：画布拖拽编辑成本高且易误连边，一期"图只读 + 表单编辑"已覆盖全部字段；画布编辑列 P2）。

#### 6.3.2 模块标签（P1 编辑 / P0 只读）

模块列表（卡片或表格：code / 名称 / 类型 / 节点数 / executor / 工具数 / 邻接出边），点开模块表单：

| 字段 | 控件 | 约束与提示 |
|---|---|---|
| module_code | 文本，创建后锁定 | pattern 内唯一（`validation.py:75-83`） |
| module_name / description / todo_description | 文本 / 多行 | name 缺失为软警告 |
| type | radio：agent / fsm / route | 创建后锁定（影响节点合法性与执行器链）；fsm/route 必须≥1 节点（`validation.py:87-90`） |
| executor | 下拉（`list_codes("executor")`，含"默认链尾"空选项） | 解析优先级提示：module.executor > pattern.plugins > 类型默认（`plugins.py:44-50`） |
| use_tools | 多选（工具目录按 toolset 分组） | 仅提示可借出；实际生效还受 tool 的 `allowed_patterns` ACL 约束（页内显示 ACL 提示） |
| stages | 槽位 → stage code 下拉 | 槽位选项 = 骨架已声明槽位（`validation.py:181-184`）；code 选项 = `list_codes("stage")` |
| base_prompt（agent 型） | 大文本编辑器 | 附框架变量插槽说明（§6.6）；编辑后显示与默认 prompt 的 diff |
| base_nlu_prompt / base_nlg_prompt | 大文本编辑器 | 三层覆盖提示：node > module > 骨架默认 |
| sub_modules | **邻接表编辑器**：每行 = target 下拉 + lend_knowledge 开关 + lend_tools 多选 | target 排除自身（自环禁止，`pattern.py:78-81`）；lend_tools 选项集 = 目标模块 use_tools（越权禁止，`pattern.py:82-89`）；enable_project 开关与 lend 语义联动提示（§6.3.2.1） |
| enable_project | 开关（默认开） | 开 = 投影·defer（父模块代答）；关 = 跳转目标（ModuleJumpEvent 同轮移交） |
| is_end / answer_examples | 开关 / 列表编辑器 | answer_examples 支持业务占位符 `{slot}`，占位符不在已知槽/任务字段时黄条警告 |
| plugins（messages_builder / agent_hooks） | 下拉 | 注册码目录 |

##### 6.3.2.1 sub_modules 语义助手

邻接表每行右侧内联解释当前配置的实际行为，文案直接取自内核语义（`module.py:150-160`）：

- `enable_project=true` + `lend_knowledge=true` → "父模块本轮以投影知识代答；agent 可调 defer 工具在轮末切底座（防乒乓强制投影）"。
- `enable_project=true` 且既不 lend_knowledge 也无 lend_tools → **红字**："父模块无从代答，请补 lend_knowledge 或改 enable_project=false 走跳转"（规则源 `validation.py:109-127`；注意该检查现行代码不可达，见 G-8——前端照常提示，服务端以构造器为准）。
- `enable_project=false` → "此模块作为跳转目标：菜单节点 jump_module 或 defer 可同轮移交"。

#### 6.3.3 节点编辑（模块标签内，P1）

模块详情内节点表（code / 名称 / 槽位 / 后继数 / 终态 / jump_module），点开节点表单：

| 字段 | 控件 | 运营释义（同时显示为字段帮助） |
|---|---|---|
| node_code | 文本，创建后锁定 | 模块内唯一（`validation.py:96-102`） |
| node_name | 文本 | 展示名 |
| node_description | 多行 | **会注入 NLG（生成回复时）的当前节点语境**（`node.py:119-131`） |
| node_todo_description | 多行 | **会注入 NLU（理解用户时）的当前节点任务说明**（`node.py:101-117`）——运营写这两个字段时需要知道这个区别 |
| node_slots | 键值对编辑器（槽名 → 中文描述） | 槽位定义会进入 NLU 抽取 prompt（`node.py:94-97`） |
| sub_nodes | 多选节点（全 pattern node_map 范围，可跨模块） | **FSM 转移表**：NLU 只能在这批后继里选 next_node（unified stage 硬校验，`unified.py:273-288`） |
| answer_examples | 排序列表编辑器 | 回答范式，注入 NLG；支持 `{slot}` 占位符 |
| base_nlu_prompt / base_nlg_prompt | 大文本 | 覆盖链：node > module > 默认 |
| stages | 槽位 → code 下拉 | 同模块层约束 |
| is_end | 开关 | 终态：触发会话结束语义 |
| jump_module | 模块下拉（排除本模块） | ROUTE 菜单节点的跨模块分发目标；悬空/自环在构造期即 fail-fast（`pattern.py:93-105`） |

交互：删除模块最后一个节点时阻止并提示（fsm/route ≥1 节点）；编辑 sub_nodes 即时刷新结构图标签中的实线边预览。

#### 6.3.4 流水线骨架标签（P1）

六槽有序展示（`pipeline.py:49-51`）：`pre_recall → query → post_recall → nlu → clarify → nlg`。每槽 = 空或 stage code 下拉。

- 槽位语义一句话说明（如 query = 查询改写、clarify = 澄清路由）。
- **unified 形态引导**：nlu 与 nlg 选同一 code 时提示"统一单调用形态（合法）"；其余跨槽重复 code 即时红字（`validation.py:237-241`）。
- 模块/节点层覆盖了某槽时，在此标签以角标显示覆盖数（点开列出覆盖者），帮助运营理解"骨架是默认，模块/节点可各自覆盖"。

#### 6.3.5 生效装配标签（resolved，只读，P1；依赖 introspect 的公共查询函数）

逐模块展示**运行时真实装配**（而非声明），形如（对齐 `introspect-skill.md` §4.3 的输出设计）：

```
install_booking [fsm, 16 nodes]
 ├─ executor     default_fsm        [默认链尾]
 ├─ stage query  time_aug_query     [pattern]
 ├─ stage nlu    install_unified    [module]
 ├─ stage clarify install_clarify   [module]
 └─ stage nlg    nlg_pass_through   [module]
```

方括号标注该装配由哪一层决定。实现上必须复用 `pipeline.resolve_stage_code` 与 executor 解析链（与 introspect skill 共建 `resolve_declared_stages`，避免双写漂移）。

#### 6.3.6 版本与发布标签（P1）

- **草稿区**：当前草稿 yml（可切换"表单视图 / YAML 源码视图"双向同步，YAML 视图供研发协同与快速 diff）；[保存草稿] [校验] [发布] 按钮。
- **校验结果面板**：dry-run `pattern_from_yaml` + `validate_pattern` 的收集式编号错误/警告清单（软警告如"缺少 name"单列灰色区）。
- **发布对话框**：
  1. 变更 diff（上一版本 yml vs 草稿 yml）；
  2. 生效语义确认文案：*"发布后将触发重载：进行中的会话以旧配置跑完当前对话轮，新对话轮起使用新版本。"*
  3. 变更说明（必填，进审计）；
  4. 确认 → 注册 + reload → 版本快照落库。
- **版本列表**：时间 / 发布人 / 说明 / 快照查看（yml 全文）/ 两版 diff / [回滚到此版]（回滚 = 以该快照内容重新走发布流程，同样生成新版本与审计，**不删历史**）。

### 6.4 知识库管理（P0）

#### 6.4.1 空间（scope）管理

- 空间 = `"{channel}:{account_id}"`（如 `xianyu:demo`），列表显示两表条目数与最近更新。
- 新建空间 = 输入 channel + account_id（格式校验）；删除空间 = 软确认 + 二次输入 code 确认（级联删除需后端支持，P0 可先只允许清空空间）。
- 页面常驻提示：知识工具当前 ACL 仅授权 `customer_agent`（`atoms/tools/knowledge_tool.py:246-255`）——"此处改动影响该应用的检索结果"。

#### 6.4.2 商品知识（product_knowledge）

表格列：goods_id / goods_name / price / sold_quantity / specifications（JSON，展开查看）/ extracted_content（markdown，抽屉查看+编辑）/ updated_at / last_extracted_at。

- 行操作：编辑（upsert 语义：按 (scope, goods_id) 存在即部分更新，文案如实说明）/ 删除（新 API G-2）。
- 表单：单价/销量数字校验；specifications 提供 JSON 编辑器（格式校验）；extracted_content 提供 markdown 编辑器 + 预览。
- **导入导出**：CSV / JSON 模板下载、文件导入（逐行校验、错误行号报告、成功/失败计数）、整表导出。批量粘贴 markdown 场景给 JSON 直贴入口。

#### 6.4.3 客服知识（customer_service_knowledge）

表格列：title / content（截断+抽屉）/ tags / **enabled 开关**（列内直接切换，新 API）/ updated_at。行操作：编辑 / 删除 / 停用启用。
表单：title 必填；content 多行；tags 逗号分隔。停用即时生效于检索（`search_cs` 仅取 enabled=1，`store.py:243-273`）。

#### 6.4.4 一致性说明

`upsert_product` 为 COALESCE 部分更新（未填字段保留旧值，`store.py:106-135`）——编辑表单需区分"清空该字段"与"不修改"，UI 以显式"清空"动作表达前者，避免运营误以为留空=清空。

#### 6.4.5 试搜台（检索行为预演）

- 输入：query（或 goods_id 精确模式）+ limit（默认 10，上限 50，与 `store.py:210/250` 一致）。
- 输出：命中的商品与客服条目 + **命中解释**：展示 jieba 分词结果（`cut_for_search`，词长≥2，多词 AND、字段间 OR——`store.py:75-80`）、每条命中命中的字段与词。
- 目的：让运营在发布前验证"这句话能不能搜到我要的条目"，并对**关键词匹配**（非语义检索）建立正确预期；`<untrusted_knowledge>` 包裹等注入防护细节不暴露，仅在帮助文档说明。

### 6.4.6 RAG 检索配置（v1 已实现，2026-09-10）

> 状态：**已随 P0 一并实现**（原规划 P2「检索参数可调」提前）。实现：
> `atoms/stages/rag_config.py`（声明式装配 + 重注册 + 离线试跑）、
> `/api/v1/console/rag/*`、控制台「RAG 检索配置」页。

配置对象 = **clarify 召回管线**（`MultiPathRecaller` + `ClarifyRouteRule`），
持久化为 `host/config/rag.yaml`（`NEXUS_RAG_CONFIG` 可覆盖）：

| 配置段 | v1 取值 | 说明 |
|---|---|---|
| recall_paths | `kb_cs` / `kb_products`（name/scope/weight/top_k） | 召回通路经 `KeywordRecallPath.search_func` 接 SQLite 知识库；**候选池 + 命中打分**（标题×2+正文，除以查询词数×3）而非 store 的多词 AND 检索——召回要查全，精度交给门控。embedding/ES/LLM 通路需外部后端，v1 报错不开放 |
| filters | dedup(by) / score_threshold / max_results（有序） | 缺席补默认阈值 0.1；显式 `[]` = 无过滤 |
| fusion | weighted / rrf(k) / round_robin | — |
| reranker | score / diversity(λ) | 无「none」选项（构造器对 None 回落 score） |
| rule | t_high / t_low / keyword_bonus | kb/mixed/fallback 分界 + 业务关键词加分 |

**生效机制**：保存 = 收集式校验 → 落盘 → 经插件中心重注册
`rag_clarify` / `clarify_default` / builtin clarify 工厂（deregister 清实例缓存），
下一对话轮生效、无需重启；启动时 `load_and_apply_rag_config()` 自动加载
（文件缺席 = 内置默认零行为变化；坏文件记 ERROR 不拖垮服务）。

**试跑台**：按当前表单配置离线跑「召回 + 门控」（这两段零 LLM），
返回门控模式、加分后 top 分、分词、每路召回明细——参数调优闭环。

**生效范围（如实告知）**：声明 clarify 槽为 `rag_clarify` / `clarify_default`
/ `builtin:clarify` 的模块。install/repair 的 FAQ 澄清（自有装配）、
customer_agent 的工具检索不经此配置；pattern 级绑定随 P1 编辑器开放
（模块 stages 槽位选 `rag_clarify` 即可）。

### 6.5 FAQ 管理（P1，依赖 G-4 数据化迁移）

- pattern 选择器（初始仅 install_booking_agent / repair_booking_agent）。
- 条目列表：topic / keywords（chip 展示）/ answer（截断预览）/ 拖拽排序（**顺序即优先级**：specific-first 匹配语义，`faq.py:81-96`——页首明示"从上到下取第一条命中"）。
- 表单：topic、keywords（chip 输入）、answer（支持 `{product_name}` 等任务字段占位符，未知占位符黄条警告）。
- **命中预演**：输入一句话 → 高亮命中的第一条 FAQ 与命中关键词。
- 迁移说明见 §7.3。

### 6.6 模型与参数（pattern_llm，P1）

- pattern 列表 → 每 pattern 一页：
  - 基本档：llm code（下拉：`llm_default` + `llm_providers` 键，凭据字段绝不回显）/ model / temperature / max_tokens / timeout / enable_thinking。
  - 覆盖树：modules / nodes 两级逐级覆盖编辑（`settings.py:48-58` 的嵌套结构）。
  - **合并预览**：任一层改动后，展示逐级合并链 `llm_default → pattern → module → node` 的最终生效值（research/debug 排障同样依赖此视图）。
- 生效语义提示：llm 配置走 mtime 缓存自动生效，**无需发布/重载**（`settings.py:301-350`）——此页独立于 §6.3 的发布流，保存即生效（写回 local_config.yaml 由后端完成，G-1 同族 API）。

### 6.7 发布中心（P1）

全局变更流：时间 / 操作者 / 变更类型（pattern 发布 / 回滚 / 知识 CRUD / 导入 / FAQ / 模型参数）/ 对象 / 说明 / 结果（成功/校验失败）。可按类型与 pattern 筛选。数据即审计日志（§7.4）。

### 6.8 会话与调试（P2，仅设计预留）

- 会话列表 / 消息查看：直接包装既有 `GET /api/v1/sessions`、`GET /sessions/{id}/messages`。
- 对话调试：挂接 SSE 端点（需将 `NEXUS_STREAM_DEBUG` 的 debug 定位升级为 console 鉴权下的正式端点——研发依赖项，P2）。
- 与配置台的价值闭环：从某条"答非所问"的消息一键跳到对应 pattern 的节点/prompt 编辑入口。

---

## 7. 数据与存储设计

### 7.1 console-managed pattern 存储（G-5）

- 目录：`host/config/patterns/*.yml`（一个文件一个 pattern，文件名 = pattern code；该目录是否入 git 为开放问题 Q-1）。
- 加载时机：① 服务启动，在代码 pattern AST 发现**之后**统一加载（同 code 后注册者生效，使 fork 语义成立）；② 每次 `POST /api/v1/reload` 重放完成后**重放一遍 console 目录**（否则全量 reload 会把 console pattern 冲掉——集成风险 R-2 的缓解）。
- 加载路径复用 CLI `pattern-load` 的三段式：`pattern_from_yaml` → `validate_pattern` → `registry.register`。
- 草稿与版本快照：`data/console.db`（SQLite）：
  - `pattern_drafts(pattern_code PK, content_yaml, version, updated_by, updated_at)` —— 乐观锁用 version；
  - `pattern_versions(id, pattern_code, content_yaml, comment, published_by, published_at)` —— 不可变；
  - `audit_log(id, ts, actor, action, object_type, object_key, detail_json, ok)`。

### 7.2 知识库 schema 增量（G-2）

- 不改表结构（`customer_service_knowledge.enabled` 列已存在）；补 store API：`update_product` / `delete_product` / `update_cs` / `delete_cs` / `set_cs_enabled` / `delete_scope`（或 `clear_scope`）。
- HTTP 面按 §8；导入导出为服务端文件解析，避免前端大文件处理。

### 7.3 FAQ 数据化迁移（G-4，研发前置项）

- 目标形态：`data/console.db` 内 `faq_entries(pattern_code, scope_nullable, ord, topic, keywords_json, answer, enabled, updated_*)`，启动时载入替换 `apps/*/faq.py` 的 `FAQ_ENTRIES`（保留代码常量作首次迁移种子与回退）。
- 迁移期语义冻结：匹配算法保持"按序首条命中"不变，UI 排序即优先级（§6.5）。
- install 与 repair 的 FAQ 结构同构（`faq.py` 双 app 已互为印证），迁移一次覆盖两个 app。

### 7.4 版本与审计

- 所有写操作（发布/回滚/知识 CRUD/导入/FAQ 修改/模型参数保存）统一写 `audit_log`；发布中心（§6.7）即其视图。
- pattern 的版本快照只增不删；知识条目暂不做行级版本（P2 视需要加 `knowledge_audit`）。

---

## 8. 后端 API 设计（配置台依赖的端点全集）

统一挂 `/api/v1/console/*`（天然被现有 API-key middleware 覆盖，`host/main.py:74-96`）；响应沿用 `{code, message, status, data}` 包裹。

| 方法 路径 | 用途 | 复用/新增 |
|---|---|---|
| GET `/console/overview` | 总览聚合 | 新增（registry + audit 聚合） |
| GET `/console/patterns` | 列表 + 事实源/草稿状态/generation | 新增（读 `registry.list` + 草稿表） |
| GET `/console/patterns/{code}` | 详情：yml + mermaid 源码 + 模块/节点树 + 元数据 | 复用 `pattern_to_yaml` / `pattern_to_mermaid` |
| POST `/console/patterns/{code}/fork` | code-managed → console-managed 副本 | 复用 export→落盘→load 链 |
| POST `/console/patterns` | 新建（空白/从模板） | 复用 load 链 |
| GET/PUT `/console/patterns/{code}/draft` | 草稿读写（If-Match version 乐观锁） | 新增 |
| POST `/console/patterns/{code}/validate` | dry-run 构造+校验，返回收集式错误清单 | 复用 `pattern_from_yaml` + `validate_pattern` |
| POST `/console/patterns/{code}/publish` | 注册 + reload + 快照 + 审计 | 复用 CLI load 链 + `host/reload.py` |
| GET `/console/patterns/{code}/versions`；GET `.../versions/{id}`；POST `.../rollback` | 版本族 | 新增 |
| GET `/console/catalog/stages` / `executors` / `messages-builders` / `tools` | 注册码目录（下拉数据源） | 复用 `plugins.list_codes` / `get_available_toolsets` |
| GET `/console/patterns/{code}/resolved` | 生效装配 | 与 introspect 共建 `resolve_declared_stages` |
| GET/POST `/console/knowledge/scopes`；DELETE `.../scopes/{scope}` | 空间管理 | 新增 |
| GET/POST/PUT/DELETE `/console/knowledge/products?scope=` | 商品 CRUD | 复用 + 新增（update/delete） |
| GET/POST/PUT/DELETE `/console/knowledge/cs-entries?scope=` | 客服 CRUD + enabled | 复用 + 新增 |
| POST `/console/knowledge/search-test` | 试搜（含分词与命中解释） | 复用 `search_*` + 分词中间结果 |
| POST `/console/knowledge/import`；GET `.../export` | 导入导出 | 新增 |
| GET/PUT `/console/faq?pattern=` | FAQ CRUD（P1） | 依赖 §7.3 |
| GET/PUT `/console/llm-overrides/{pattern}` | pattern_llm 编排字段（P1） | 新增（写 local_config.yaml 对应节） |
| GET `/console/audit` | 审计/变更流 | 新增 |

---

## 9. 校验规则镜像（双闸）

原则：**前端即时校验尽力拦截（体验），服务端发布校验为权威（安全）**。前端规则 = 内核校验的逐条镜像：

| 规则 | 内核来源 | UI 行为 |
|---|---|---|
| pattern.code / entry_module_code 非空且可解析 | `validation.py:59-69` | code 必填；entry 下拉仅列本 pattern 模块 |
| module_code 唯一非空 | `:75-83` | 重名即时标红 |
| FSM / ROUTE 模块 ≥1 节点 | `:87-90` | 删除最后节点时阻止 |
| node_code 模块内唯一 | `:96-102` | 即时标红 |
| 邻接边悬空 / 自环 | `pattern.py:73-81` | target 下拉仅列合法模块、排除自身 |
| lend_tools 越权（⊄ 目标 use_tools） | `:82-89` | lend_tools 选项集 = 目标 use_tools |
| jump_module 悬空 / 模块自环 | `:93-105` | 下拉仅跨模块目标 |
| stage code 未注册 | `validation.py:161-241` | 全部 code 字段为下拉（数据源 = 注册码目录），杜绝手输 |
| stages 槽位 ⊄ 骨架 | `:181-184` | 槽位下拉 = 骨架槽位 |
| unified 例外：仅 nlu/nlg 可同 code | `:237-241` | 同 code 选 nlu+nlg 给"统一形态"提示；其余重复红字 |
| 投影边一致性（enable_project=true 且无任何 lend） | `:109-127` | 红字提示（注：该检查在内核中位于 return 之后、当前不可达——G-8 记研发修复；前端照常执行） |
| 骨架声明格式（单键 dict 列表） | `pipeline.py:67-90` | 结构化编辑器天然不产生非法形态 |

服务端发布闸门永远再跑全量 `pattern_from_yaml`（构造期图校验）+ `validate_pattern`（收集式），返回编号错误清单原样展示于 §6.3.6 面板。

---

## 10. 权限与安全

| 阶段 | 方案 |
|---|---|
| P0 | 复用 `NEXUS_API_KEY`（middleware 已覆盖 `/api/v1/*`，console 路径天然受护）；未设 key 时的"无认证+每分钟告警"现状对内网演示可接受，**对外网部署必须设 key**（部署文档明示） |
| P1 | console 账号表（用户名/口令散列/角色：编辑/管理员），HTTP-only session；角色门控：发布/回滚/删除/导入 = 管理员，草稿/知识 CRUD = 编辑，查看 = 全员 |
| 持续 | 全部写操作进审计（actor 必填）；YAML 源码视图对注入做转义展示；知识 markdown 经既有 `_clean_untrusted` 防护链（`store.py:61-72`），编辑器预览同源处理 |

---

## 11. 非功能需求

- **并发**：草稿乐观锁（version 不匹配返回 409 + 双方 diff 供合并）；同一 pattern 发布互斥（进程内锁；多实例部署见 Q-3）；知识写操作走 SQLite WAL 既有并发语义。
- **性能**：知识列表分页 + 服务端筛选；试搜台直接复用生产检索路径（保证预演=生产）；pattern 详情接口一次返回 yml+mermaid+树（mermaid 渲染放前端，图节点 >200 时提示改用列表导航）。
- **兼容**：CLI（`pattern-export/load`）与 console 读写同一格式，互为逃生通道；导出文件可被 `pattern-load` 直接消费。
- **可观测**：发布/回滚/校验失败均结构化落审计；console 自身错误不吞（沿用 `{code,message,status}` 包裹）。

---

## 12. 分期规划

### P0 —— 只读台 + 知识库读写（最快兑现运营价值）

| 交付 | 内容 | 依赖 |
|---|---|---|
| 控制台骨架 + 鉴权 | 导航、API-key 接入 | — |
| Pattern 只读视图 | 列表 / 详情（结构图 + 声明树 + yml 导出） | G-1（只读部分）、`visualize.py` |
| 知识库管理 | 空间 / 两表 CRUD / enabled / 导入导出 / 试搜台 | G-2、G-3 |
| Dashboard + 变更流（只读知识部分） | — | 审计表 |

**验收**：运营完成 U1/U2/U3——改一条客服知识并试搜命中；导入一批商品；看懂 install_booking_agent 的 16 节点流程图。

### P1 —— pattern 编辑 + 发布闭环 + FAQ + 模型参数

| 交付 | 内容 | 依赖 |
|---|---|---|
| fork-to-edit + 五标签编辑器 | 模块/节点/骨架表单、草稿、校验、发布、回滚 | G-1（写部分）、G-5 |
| 生效装配视图 | resolved 标签 | 与 introspect 共建解析函数 |
| FAQ 管理 | CRUD + 排序 + 命中预演 | G-4 |
| 模型与参数 | pattern_llm 编辑 + 合并预览 | settings 写 API |
| 发布中心 + 账号角色 | 全局变更流；编辑/管理员门控 | G-6 |

**验收**：U4–U7——fork install_booking_agent，改一个节点 answer_examples 与模块 prompt，校验发布，CLI `ask` 验证新话术生效，回滚复原；FAQ 自助增改；调 temperature 后下一轮生效。

### P2 —— 体验与治理增强（择优排期）

画布式图编辑（拖拽建边，服务端仍走同一校验闸）；会话调试页（SSE 正式化）；per-pattern 细粒度 reload（研发改造 reload 后接入）；xianyu 硬编码常量数据化（G-7）；知识检索升级（FTS / 向量，可接 `atoms/stages/recaller` 已有的 `EmbeddingRecallPath` 能力）；知识行级版本。

---

## 13. 风险与开放问题

### 13.1 风险

| # | 风险 | 缓解 |
|---|---|---|
| R-1 | 双源漂移：fork 后研发又改了代码 pattern | 详情页"源代码已变化"提示 + 重新 fork（显式覆盖）；发布记录留痕 |
| R-2 | reload 全量重放冲掉 console pattern | reload 完成后重放 console 目录（§7.1）；集成测试覆盖该场景 |
| R-3 | 运营改坏系统 prompt 导致线上质量回退 | 草稿不生效 + 发布前收集式校验 + 版本秒级回滚 +（P2 可选）双人复核开关 |
| R-4 | 知识检索为关键词 LIKE，运营预期"语义搜索"产生落差 | 试搜台命中解释建立预期；帮助文档明示机制；P2 检索升级路线已列 |
| R-5 | 校验器死代码（投影边检查不可达，G-8）导致前后端口径不一 | 服务端以构造器行为为准；推动研发修复后自动对齐 |
| R-6 | 发布触发全量 reload，影响面大于单一 pattern | 发布确认文案如实说明；推动 per-pattern reload（P2） |
| R-7 | 大知识库下 LIKE 检索/列表性能 | 分页 + 筛选下推；FTS 演进已列 |

### 13.2 开放问题（需产品/研发共同决议）

| # | 问题 | 初步倾向 |
|---|---|---|
| Q-1 | console yml 目录是否入 git | 入 git：发布产生可追溯提交，审计友好；代价是运行时用户需仓库写权限。内网工具倾向入 git |
| Q-2 | FAQ 迁移后 `apps/*/faq.py` 去留 | 保留一个版本周期作回退种子，之后删除 |
| Q-3 | 是否多实例部署（发布互斥、reload 广播） | 当前单进程 uvicorn 假设成立；若上多实例需引入发布锁与 reload 编排，列为架构决策前置 |
| Q-4 | 知识 scope 前缀硬编码 `xianyu`（`knowledge_tool.py:33`）的泛化 | P2 随多渠道接入一并命名化，UI 空间管理先按现有格式收口 |
| Q-5 | 是否需要测试/生产环境隔离 | P1 先单环境 + 版本回滚兜底；环境隔离视运营规模再议 |

---

## 附录 A：字段字典（编辑表单 ←→ 内核字段一一对应）

### A.1 Pattern（`nexus/model/pattern.py:8`；序列化白名单 `serialization.py:43-46`）

| 字段 | 类型 / 默认 | 约束 | 运营释义 / 编辑层级 |
|---|---|---|---|
| code | str | 非空，全局唯一，创建后锁定 | 应用标识 ◆ |
| name | str | 缺失为软警告 | 展示名 ◆ |
| description | str | — | 应用简介 ◆ |
| entry_module_code | str | 必须在 modules 中（`validation.py:63-69`） | 会话入口模块 ◆（下拉） |
| stages | list[单键 dict] | 六槽有序：pre_recall/query/post_recall/nlu/clarify/nlg（`pipeline.py:49-51`） | 流水线骨架（§6.3.4）◆ |
| plugins | dict | 键：loop/fsm/route/messages_builder/agent_hooks；值须为注册码 | 执行器族与装配插件 ◇（管理员） |
| max_hops | int，默认 2 | ≥1 | 模块间跳转预算 ◇ |

### A.2 Module（`nexus/model/module.py:65`；白名单 `serialization.py:24-30`）

| 字段 | 类型 / 默认 | 约束 | 运营释义 / 编辑层级 |
|---|---|---|---|
| type | agent / fsm / route | 创建后锁定 | 对话形态：直答 agent / 状态机 / 路由菜单 ◇ |
| module_code | str | pattern 内唯一 | 引用键，锁定 ◆ |
| module_name / module_description / module_todo_description | str | name 缺失软警告 | 名称 / 场景描述 / 职责描述 ◆ |
| module_nodes | list[Node] | fsm/route 必须≥1（`validation.py:87-90`） | 节点集（§6.3.3）◆ |
| use_tools | list[str] | 建议来自工具目录 | 本模块可用工具 ◇ |
| base_prompt | str | agent 型主入口 | 模块主话术 ◆（大文本） |
| base_nlu_prompt / base_nlg_prompt | str | 三层覆盖中间层 | 理解 / 生成默认话术 ◆ |
| stages | dict{槽: code} | 槽 ⊆ 骨架；code 已注册 | 槽位覆盖 ◇ |
| sub_modules | list[{target, lend_knowledge=true, lend_tools=[]}] | 目标存在、非自环、lend_tools ⊆ 目标 use_tools（`pattern.py:73-89`） | 邻接边 + 借出配置（§6.3.2.1）◇ |
| executor | str | 注册码（kind=executor） | 执行器直配（最高优先）▍只读目录选择 ◇ |
| enable_project | bool，默认 true | — | 投影（代答+defer）/ 跳转目标 ◇ |
| agent_stage | str | 注册码 | 自定义 agent 段 ▍ |
| plugins | dict | messages_builder / agent_hooks 注册码 | 装配插件 ◇ |
| is_end / answer_examples | bool / list[str] | — | 终态 / 回答范式 ◆ |

### A.3 Node（`nexus/model/node.py:18`；白名单 `serialization.py:33-37` + jump_module 特例 ：70-72）

| 字段 | 类型 / 默认 | 约束 | 运营释义 / 编辑层级 |
|---|---|---|---|
| node_code | str | 模块内唯一 | 引用键，锁定 ◆ |
| node_name | str | — | 展示名 ◆ |
| node_description | str | — | 注入 NLG 语境（`node.py:119-131`）◆ |
| node_todo_description | str | — | 注入 NLU 任务说明（`node.py:101-117`）◆ |
| sub_nodes | list[str] | 指向 pattern 级 node_map（可跨模块） | **转移表**（决定 NLU 可选后继）◆ |
| node_slots | dict{槽名: 中文描述} | — | 槽位定义（入 NLU 抽取）◆ |
| answer_examples | list[str] | 支持 `{slot}` 占位 | 回答范式（入 NLG）◆ |
| stages | dict{槽: code} | 槽 ⊆ 骨架 | 槽位覆盖（最高优先层）◇ |
| base_nlu_prompt / base_nlg_prompt | str | — | 节点级话术覆盖 ◆ |
| is_end | bool，默认 false | — | 终态标记 ◆ |
| jump_module | str（kwargs 附加属性） | 目标存在、非本模块（`pattern.py:93-105`） | 菜单跨模块分发 ◇ |

编辑层级图例：◆ 运营编辑 ｜ ◇ 管理员/高级编辑（默认收进"高级"折叠区） ｜ ▍ 只读展示。

### A.4 知识库（`atoms/knowledge/store.py:28-56`）

- `product_knowledge`：scope / goods_id（联合唯一）/ goods_name / price / sold_quantity / specifications(JSON) / extracted_content(markdown) / created_at / updated_at / last_extracted_at —— 全字段运营可编辑。
- `customer_service_knowledge`：scope / title / content / tags / **enabled** / created_at / updated_at —— 全字段运营可编辑。
- 检索参数：limit（默认 10，硬上限 50）；其余（分词、AND/OR 逻辑）为系统行为，试搜台透明化但不可配。

---

*本 PRD 基于对仓库以下核心文件的通读：`nexus/model/*`（node/module/pattern/serialization/validation/convert）、`nexus/registry/plugins.py`、`nexus/pipeline.py`（骨架）、`nexus/visualize.py`、`nexus/settings.py`、`host/{main,cli,reload,governor}.py`、`nexus/engine/store.py`、`atoms/knowledge/store.py`、`atoms/tools/{knowledge_tool,mcp_tool}.py`、`atoms/stages/*`（含 _prompts）、`atoms/executors/*`、`apps/` 全部 5 应用（含 deep_research 双版本）。行号以 2026-09-10 main 分支为准。*
