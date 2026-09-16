---
name: nexus-app-template-builder
description: 把业务需求（正向）或现有 apps/ 应用（逆向）转成 nexus-kit 应用模板，写入模板知识库 app-templates/——不实现任何插件与工具，插件位置以「处理步骤卡 / 工具描述卡」的简短描述占位。当用户想为模板知识库新增条目、只要应用蓝图不要实现、或把现有应用逆向沉淀为可复用模板时使用。
---

# nexus-app-template-builder

把业务需求转成 nexus-kit 应用**模板**：一张 Pattern 图（节点/边/图类型）
加上一组**以描述占位的插件**（executor / stage / tool 的位置写"处理步骤
卡"或"工具描述卡"，不写实现代码），落成模板知识库 `app-templates/` 的
一个条目。模板是知识库资产：结构一致、单文件自包含、机器可校验，后续
任何实现者（人或 agent）照卡即可落地代码。

本 skill 完全自持：框架知识在自有 `references/` 里，结构校验脚本在
`references/verify_template.py`，不依赖仓库内其他 skill 的资产。

全程自主推进，不等确认；但 Phase 1 选型陈述与 Phase 2 五件套仍是强制
产物——图类型选错或节点交互表缺失，模板就整体作废。

## 框架参考（自有 references/，路径相对本目录）

| 文件 | 覆盖 |
|---|---|
| `references/architecture.md` | 分层、两种图类型（fsm/agent）的运行时语义 |
| `references/pattern-schema.md` | Pattern/BaseNode 字段表、YAML 形态 |
| `references/plugin-and-tool-guide.md` | 插件契约、能力分诊三路由 |
| `references/template-index.md` | app-templates/ 知识库条目 → 业务形态/可借用纪律映射 |
| `references/pitfalls.md` | 节点交互表模板、graph_state 规则、循环继承 |
| `references/verify_template.py` | 结构闸校验脚本（Phase 4 用） |

CLI 交叉核对：`python nexus-introspect-skill/introspect.py apps` /
`pattern <code> --view yaml` / `plugin executor <code>`——源码是唯一事实。

## 知识库布局

```
app-templates/
├── INDEX.md                       # 总目录：code/名称/业务形态/图类型/来源
└── <code>/                        # 每模板一目录（code = pattern code）
    └── TEMPLATE.md                # 单文件自包含条目
```

## TEMPLATE.md 章节骨架（强制，顺序固定）

```markdown
# <code> — <名称>（应用模板）

> 元信息：业务形态=<一句话> ｜ 图类型=<fsm/agent> ｜ 借用来源=<模板应用/零借用>
> 来源=<业务需求描述 或 逆向自 apps/<name>>

## 节点清单
（表格：code / 名称 / 用途 / sub_nodes / is_end）

## 节点交互表
（表格：节点 / 读 graph_state / 写 graph_state / 出边与路由；每个循环
必须写检查点与经验继承策略；表后可加循环继承说明段落）

## Pattern 声明
（一个 ```yaml 围栏块，首行必须是标记注释 `# nexus-pattern: <code>`，
字段见下；这是 verify_template.py 的抽取锚点）

## 插件步骤卡
（每卡一个 `#### 插件卡：<code>（<kind>）` 四级标题；kind ∈
stage / executor / messages_builder / agent_hooks；**内置件被引用也要
有卡**，绑定位置注明"内置"。卡内固定字段：

- 绑定位置：pattern.stages 的 <槽位> 槽 / node.plugins / pattern.plugins
- 触发时机：何时执行
- 读（graph_state）：读哪些键
- 处理步骤：几句话自由描述做什么、怎么做——这是占位实现的核心描述，
  后续实现者照此实现
- 写（graph_state）：写哪些键
- 出边影响：是否/如何改写 next_node 与回复）

## 工具描述卡
（每卡一个 `#### 工具卡：<name>（<toolset>）` 四级标题，固定字段：
用途 / 参数 / 返回 / 为什么是工具而非 prompt。无工具则写"（无）"）

## 实现注意事项
（给未来实现者的提示：时序陷阱、预算、密钥来源、测试范式等）
```

四级标题格式（`#### 插件卡：<code>（<kind>）`、`#### 工具卡：<name>（<toolset>）`）
是机器可 grep 的契约，verify_template.py 靠它做卡片覆盖校验，**不得变体**。

Pattern 声明 YAML 块写**精简声明**：code / name / description /
pattern_type / entry_node_code / stages（fsm）/ nodes（code、name、
description、task_description、sub_nodes、slots、is_end、plugins）。
不放大段 prompt 文本（prompts 属实现，写入实现注意事项即可引用）。

## Phase 0 — 侦察

1. 读上表参考文档中需要的篇目。
2. 盘点知识库库存：`app-templates/` 已有条目（权威目录 `INDEX.md`；
   避免业务形态重复，同形态应说明差异或合并）。
3. 精读 1-2 个业务形态最接近的既有模板条目（正向模式，选型参考
   `references/template-index.md` 的"borrow it for"列）或目标应用
   全量源码（逆向模式）。

## Phase 1 — 图类型决策

判据见 `references/architecture.md`：引导式对话、每条用户消息推进一拍、
槽位/表单收集、逐轮确认、自然循环 → **fsm**；单条用户消息应触发端到端
交付物（报告/产物/研究结论）的多节点自主管线 → **agent**；模糊时默认
agent 并说明理由。**先陈述决策再动笔**：pattern_type、命中的信号、
5-10 个节点的一句话节点草图。决策直接决定 TEMPLATE.md 的节点清单与
交互表形态。

## Phase 2 — 五件套方案

1. **借用清单 + 差异分析**：从 `app-templates/` 已有条目中声明借用什么
   （图形态/守卫纪律/循环策略/卡片范式，机制级借用优于形态照搬）；
   零借用须明示理由。
2. **节点全清单**：code / name / purpose / sub_nodes / is_end。
3. **节点交互表**（强制）：每节点读/写哪些 graph_state 键；每个循环
   （设计→验证→修复…）写检查点与经验继承策略（历史数组 / 最佳检查点 /
   试败日志）。模板见 `references/pitfalls.md`。
4. **能力分诊表**：每个能力一行，路由到——**提示词原生节点**（语义
   判断/话术）/ **工具描述卡**（精确计算、外部 API、确定性变换）/
   **executor 或 stage 步骤卡**（编排、状态桥接、收敛闸门）。分诊规则
   同 `references/plugin-and-tool-guide.md`；模板里它们落到"卡"而不是
   代码。
5. **产物清单**：`app-templates/<code>/TEMPLATE.md`（新目录）+
   `INDEX.md` 追加一行；确认 code 无冲突（`introspect.py apps` +
   现有条目）。

## Phase 3 — 写模板

- 按章节骨架逐节填写；交互表行数必须等于节点数；Pattern YAML 块首行
  带标记注释。
- 每个被声明的插件码（pattern.plugins / node.plugins / stages 骨架，
  含内置件）都有一张插件步骤卡；每个工具一张工具描述卡。
- 处理步骤字段写"几句话"：讲清输入→变换→输出与失败路径即可，不写
  伪代码细节；确定性要求（零 LLM、防编造、防重放）写进出边影响或
  实现注意事项。
- `INDEX.md` 追加一行（code / 名称 / 业务形态 / 图类型 / 来源 / 条目
  相对链接）。

**逆向模式**（现有应用 → 模板）：
1. 读 `apps/<name>/` 全量源码（route/prompts/stages/executor/tools），
   `introspect.py pattern <code> --view yaml` 交叉核对声明。
2. 反推五件套：节点与边从 route.py 转写；交互表从 executor/stage 的
   graph_state 读写反推；分诊表从"哪些行为在代码里确定性完成"反推。
3. 插件卡的处理步骤 = 对该插件真实实现的**概括转述**（讲清读什么、
   分几步、写什么、失败怎么办），不是源码粘贴。
4. 来源标注"逆向自 apps/<name>"，实现注意事项写明该应用已存在、
   模板是它的结构沉淀。

## Phase 4 — 双闸验证（全部必过，红=未完成）

**内容闸（checklist）**：
- [ ] 章节骨架完整且顺序正确；交互表行数 = 节点数
- [ ] 每个循环都有检查点与经验继承策略
- [ ] 每个被声明的插件码（含内置件）有卡；每个工具有卡；无孤立卡
- [ ] YAML 块与卡声明一致（码、kind、绑定槽位）
- [ ] 借用清单明示；code 无冲突；INDEX.md 已追加

**结构闸（脚本）**：

```bash
python nexus-app-template-skill/references/verify_template.py \
    app-templates/<code>/TEMPLATE.md
```

脚本抽取 YAML 块 → 为声明的插件码注册占位工厂 → 构造 Pattern →
`validate_pattern` + 结构断言（入口/悬空边/终节点/卡片覆盖/交互表
行数/INDEX 收录）。零实现即可跑通结构合法性。

如实报告：过了什么、缺什么。任一闸红不得宣布完成。

## 模板 → 实现的接力

模板条目就是实现蓝本：节点清单、交互表、能力分诊表就位后，实现者
（人或 agent）照插件步骤卡与工具描述卡落地代码——每张卡的字段（绑定
位置/读/处理步骤/写/出边影响）即实现契约；实现注意事项给出时序与
测试要求。反向接力即本 skill 的逆向模式：已实现应用回填知识库。

## 安全红线（硬性，无例外）

1. **只写**：`app-templates/<code>/`（新目录）、`app-templates/INDEX.md`
   （追加行）、`nexus-app-template-skill/`（本 skill 自身维护）。
2. **不改**：`nexus/`、`atoms/`、`host/`、`ui/`、`apps/`、`tests/`、
   `docs/`、其他既有条目。
3. 模板不产生任何可执行代码（YAML 围栏块是声明数据，不是被 import
   的模块）；不写 `host/config/`。
4. 密钥等敏感信息不进模板（密钥来源以环境变量名提及即可）。
5. 工具授权描述遵循拒绝式默认语义（allow_toolset ∩ use_tools），模板
   不得描述"为跑通而放宽授权"的方案。
