# archify — 图表工程助手（应用模板）

> 元信息：业务形态=图表工程交付管线（需求 → 五类选型 → 产物优先创作 → 验证闸门 ⇄ 聚焦修复收敛闭环 → 原子交付 → 浏览器证据 → 感知评审 → 三级分离汇报）｜图类型=agent｜借用来源=零借用（本条目是「agent + 修复回路」纪律之源：语义归模型、几何归工具、验收归回执；val_history/best_checkpoint/solver_tried 经验继承与 stale-N 诚实出口均自此沉淀）｜来源=逆向自 apps/archify_agent（archify skill 快速创作路径的图转写）

## 节点清单

| code | 名称 | 用途 | sub_nodes | is_end |
|---|---|---|---|---|
| af_route | 图表·类型路由 | 语义选型站：从需求判定 architecture/workflow/sequence/dataflow/lifecycle 五类之一 | af_author | |
| af_author | 图表·产物优先创作 | 读 schema+示例后下一动作必是写出候选 JSON；一条主路径、≤12 主节点、未经诊断不加几何控制 | af_update_probe, af_validate | |
| af_update_probe | 图表·更新探针 | 侧枝（仅首个候选后探一次）：跑更新检查器并以一条通知呈现；信息不是许可 | af_validate | |
| af_validate | 图表·验证闸门 | validate showcase 级验收；通过即冻结候选，未过携诊断进修复回路 | af_deliver, af_repair | |
| af_repair | 图表·聚焦修复回路 | 先零 LLM 标签清障求解器再 LLM 聚焦修复；stale-N 无改进走诚实出口 | af_validate, af_report | |
| af_deliver | 图表·最终交付 | 一次性最终验收：冻结快照、原子提交 HTML、报告 SHA-256；非零退出绝不说成功 | af_visual_check, af_report | |
| af_visual_check | 图表·浏览器证据 | 从已交付 HTML 收集有界自动化浏览器证据，不修改不重渲染 | af_percept | |
| af_percept | 图表·感知评审 | 图像能力评审模型按截图侧车逐项审查感知质量；无证据如实标 skipped | af_report | |
| af_report | 图表·三级分离汇报 | 分开陈述 deliver/visual-check/感知评审三种证明；回执组装，未做不声称 | （无） | ✓ |

## 节点交互表

站点间状态经 `graph_state["archify_state"]` 传递（request/workspace/artifact_path/candidate/val_history/design_notes/repair_log/solver_tried/best_checkpoint）；最终迹写 `metadata["archify"]`（引擎轮末读取）。确定性站（闸门/交付/浏览器检查）在 executor 内直调 bash，与 LLM 站走同一三层授权解析。

| 节点 | 读 graph_state | 写 graph_state | 出边与路由 |
|---|---|---|---|
| af_route | 用户需求 | archify_state 初始化（request 等） | →af_author（判型语义归模型，route_retries 自纠重试） |
| af_author | archify_state.request/workspace（会话工作区绝对路径） | 候选 JSON 落盘（adopt-or-fail：内容是模型的、落位是代码的）、design_notes（创作备忘 ≤600 字） | 首个候选→af_update_probe；已有候选的再访问→af_validate |
| af_update_probe | archify_state.probe_done | probe_done=true（一次性侧枝标记） | 呈现通知后→af_validate（回主线） |
| af_validate | archify_state.artifact_path | val_history += 本次错误数；刷新下限时 best_checkpoint ←候选字节+回执快照；通过则冻结候选 | showcase 通过→af_deliver；未过→af_repair（携诊断） |
| af_repair | archify_state{val_history, best_checkpoint, repair_log, solver_tried, design_notes, 诊断} | repair_log += 本轮动作摘要；solver_tried += 失败 nudge 键；worse 回滚至 best_checkpoint 字节 | 错误数刷新下限→af_validate（再试）；连续 stale_limit 轮无新下限→af_report（诚实出口，带未解决诊断） |
| af_deliver | archify_state.artifact_path（冻结字节） | 交付回执（SHA-256/字节数/私有快照路径） | 退出码 0→af_visual_check；非零→af_report（交付失败逃生边：不做浏览器检查，避免查到陈旧产物） |
| af_visual_check | 已交付 HTML 确切路径 | 浏览器证据（机器可读测量+截图侧车） | →af_percept |
| af_percept | af_visual_check 的截图侧车 | 感知判定（逐项 passed/failed/skipped；无证据/无图像能力=skipped） | →af_report |
| af_report | 全部回执（deliver/浏览器/感知）+ archify_state 迹 | metadata.archify（最终迹：val_history/repair_log/design_notes 等） | 终止（is_end）；三级分离汇报=本轮唯一用户可见回复 |

**循环继承（validate ⇄ repair 收敛闭环）**：检查点四件套——`val_history`
（每次验证的客观错误数历史，收敛信号=错误数下限是否被刷新）、
`best_checkpoint`（每个新下限时刻的候选字节+回执快照，回滚源与回归守
卫）、`repair_log`（每次修复访问的动作摘要，防重放已失败的修复动作）、
`solver_tried`（求解器跨访问的失败 nudge 键，防重放失败几何）；陈旧规
则 = `_trailing_stale(val_history) ≥ stale_limit`（代码默认 5，config
bag 可收紧为 3）→**计算得出而非猜测**，走诚实出口边（已声明到
af_report）带未解决诊断如实汇报。预算三层独立：max_steps=20（修复环
消耗图步，一轮=2 步；stale 诚实出口全程约 16 步留余量）× stale-N 语义
停止 × 每站内轮帽（author ≤author_rounds / repair ≤repair_rounds）。
无部分产物丢失风险：候选字节级快照即最佳检查点。

## Pattern 声明

```yaml
# nexus-pattern: archify
code: archify
name: 图表工程助手
description: >-
  archify 图表配方：ROUTE/AUTHOR/VALIDATE/REPAIR/DELIVER/VISUAL_CHECK/
  PERCEPT/REPORT 各为一个 AGENT 节点；验证闸门与修复回路构成收敛闭环
  （连续五轮不改进走诚实出口），更新探针为首个候选落地后的一次性侧枝，
  感知评审按 visual-check 截图独立判定；语义归模型、几何归工具、验收归
  回执。
pattern_type: agent
entry_node_code: af_route
nodes:
  - code: af_route
    name: 图表·类型路由
    description: 语义选型站:从需求选 architecture/workflow/sequence/dataflow/lifecycle 五类之一;Mermaid 输入只读拓扑与含义(flowchart→workflow、sequenceDiagram→sequence、stateDiagram→lifecycle),不照搬样式;歧义场景可调 guide 取结构性参考
    task_description: 判定图表类型与输入形态,移交创作
    sub_nodes: [af_author]
    plugins:
      loop: af_route
  - code: af_author
    name: 图表·产物优先创作
    description: 读一个匹配 schema + 公共 schema + 一个示例(示例只取字段形态,不取事实),然后下一个动作必须是写出候选 JSON——不在文字里规划坐标;一条清晰主路径、短侧枝、稀疏标签、主节点至多 12 个,meta.quality_profile 固定 showcase,从自动路由与标签起步,未经诊断不添加任何几何控制
    task_description: 全新创作候选图表规范 JSON
    sub_nodes: [af_update_probe, af_validate]
    plugins:
      loop: af_author
    use_tools: [read_text, write_text, find_files]
  - code: af_update_probe
    name: 图表·更新探针
    description: 侧枝(首个候选落地后探一次,仅一次):跑打包的更新检查器;silent 不提及;update_available 以一条紧凑通知呈现(已装版本/最新版本/官方发布说明链接),security 级加克制警告标记;通知是信息不是许可——已装版本保持不变,是否更新由用户决定;呈现后按 eventKey 确认并回到主线
    task_description: 一次性更新感知探针,随后回验证主线
    sub_nodes: [af_validate]
    plugins:
      loop: af_update_probe
    use_tools: [bash]
  - code: af_validate
    name: 图表·验证闸门
    description: validate --quality showcase --json:showcase 通过必须 9 项产物检查全过且 0 组合错误 0 警告(仅 4 项检查只是基础验证);遗漏或拼错 meta.quality_profile 先修字段再修几何;通过即冻结候选,此后绝不修改;未过则携诊断进入修复回路
    task_description: 验证候选规范,通过即冻结
    sub_nodes: [af_deliver, af_repair]
    plugins:
      loop: af_validate
    use_tools: [bash]
  - code: af_repair
    name: 图表·聚焦修复回路
    description: 只修改被诊断的 subject,核实 evidence,从 supportedFixes 中选取方案;每轮至多应用一个几何控制;保留一切有意义的标签——删除语义标签不是几何修复;错误数刷新下限则回验证闸门再试,连续五轮无改进即停止打磨,带未解决诊断走诚实出口
    task_description: 按诊断聚焦修复,或如实上报终止
    sub_nodes: [af_validate, af_report]
    plugins:
      loop: af_repair
    use_tools: [read_text, edit_file, write_text, bash]
  - code: af_deliver
    name: 图表·最终交付
    description: deliver 一次性最终验收:冻结规范字节为同目录私有快照,渲染并检查该快照,原子提交 HTML,报告规范与产物的 SHA-256 与字节数;非零退出绝不能被描述为成功;失败交付保留旧输出——后续不得对该路径跑浏览器检查(会查到陈旧产物)
    task_description: 冻结规范并原子提交 HTML 产物
    sub_nodes: [af_visual_check, af_report]
    plugins:
      loop: af_deliver
    use_tools: [bash]
  - code: af_visual_check
    name: 图表·浏览器证据
    description: visual-check 从确切的已交付 HTML 收集自动化浏览器证据,不修改不重渲染;机器可读测量与截图不证明感知精致——浏览器证据与感知审查分开报告
    task_description: 收集有界浏览器行为证据
    sub_nodes: [af_percept]
    plugins:
      loop: af_visual_check
    use_tools: [bash]
  - code: af_percept
    name: 图表·感知评审
    description: 具备图像能力的评审模型按 visual-check 截图侧车(明/暗主题 × 桌面视口)逐项审查感知质量:构图收敛、双主题一致、连线质量、标签遮罩、卡片适配、READ 常态、导出整洁;只评所附截图、未附不评,无证据或评审模型无图像能力时如实标 skipped,绝不编造通过;判定独立于确定性检查与浏览器证据
    task_description: 按截图执行图像能力感知评审
    sub_nodes: [af_report]
    plugins:
      loop: af_percept
  - code: af_report
    name: 图表·三级分离汇报
    description: 把三种证明分开陈述:deliver=确定性产物检查;visual-check=真实浏览器中的有界行为;感知审查=图像能力评审站按截图的独立判定(passed/failed/skipped)——非零退出的命令不声称成功,未实施的检查不声称已做
    task_description: 如实汇报三级证明与回执
    sub_nodes: []
    plugins:
      loop: af_report
    is_end: true
allow_toolset: [shell, filesystem]
config:
  max_steps: 20
```

## 插件步骤卡

#### 插件卡：af_route（executor）
- 绑定位置：af_route 节点 node.plugins 的 loop 槽
- 触发时机：每条需求消息进图的首站
- 读（graph_state）：用户需求、Mermaid 输入（如有）
- 处理步骤：纯语义站（零工具）：单次 LLM 调用判定图表类型
  （architecture/workflow/sequence/dataflow/lifecycle 五选一）与输入形态；
  Mermaid 输入只取拓扑语义（flowchart→workflow、sequenceDiagram→
  sequence、stateDiagram→lifecycle），不照搬样式；JSON 解析失败按
  route_retries 自纠重试。会话工作区初始化（per-session 绝对路径，
  session_id 消毒后落 data/<app>/<session>/，全部绝对路径上板）。
- 写（graph_state）：archify_state 初始化（request/workspace/type）
- 出边影响：→af_author（content="" 静默中继）

#### 插件卡：af_author（executor）
- 绑定位置：af_author 节点 node.plugins 的 loop 槽
- 触发时机：类型判定后的创作站
- 读（graph_state）：archify_state.request/workspace
- 处理步骤：带工具小循环（≤author_rounds 轮，config bag 可调）：先读一
  个匹配 schema + 公共 schema + 一个示例（示例只取字段形态不取事实），
  然后**产物优先**——下一个动作必须是写出候选 JSON（不在文字里规划坐
  标）；构图纪律进 prompt（一条清晰主路径/短侧枝/稀疏标签/主节点 ≤12/
  quality_profile=showcase/未经诊断不加几何控制）。落位收编 adopt-or-
  fail：模型把正确内容写到错误路径时由 executor 钉回约定路径（内容是模
  型的、落位是代码的）。收尾写创作备忘 design_notes（修复站的唯一创作
  语境）。
- 写（graph_state）：候选产物落盘 + archify_state.design_notes
- 出边影响：首个候选→af_update_probe（探针侧枝只走一次）；否则→af_validate

#### 插件卡：af_update_probe（executor）
- 绑定位置：af_update_probe 节点 node.plugins 的 loop 槽
- 触发时机：首个候选落地后的一次性侧枝（仅一次）
- 读（graph_state）：archify_state.probe_done
- 处理步骤：executor 内确定性跑打包的更新检查器（直调 bash，走同一三
  层授权）；结果三态——silent 不提及；update_available 以一条紧凑通知
  呈现（已装版本/最新版本/官方发布说明链接），security 级加克制警告标
  记；通知是信息不是许可，已装版本保持不变。呈现后按 eventKey 确认回主
  线，置 probe_done 保证不再探。
- 写（graph_state）：archify_state.probe_done=true
- 出边影响：→af_validate（回验证主线）

#### 插件卡：af_validate（executor）
- 绑定位置：af_validate 节点 node.plugins 的 loop 槽
- 触发时机：创作/探针之后、每轮修复之后
- 读（graph_state）：archify_state.artifact_path、val_history
- 处理步骤：executor 内确定性跑 validate CLI（--quality showcase
  --json，直调 bash）：showcase 通过 = 9 项产物检查全过且 0 组合错误 0
  警告（4 项检查只是基础验证）。通过→冻结候选（此后任何站不再修改）；
  未过→把本次错误数追加进 val_history，刷新历史下限时同步刷新
  best_checkpoint（候选字节+回执快照），携诊断进修复回路。验收归回执：
  通过与否由 CLI 退出码与 JSON 裁定，模型不参与判定。
- 写（graph_state）：archify_state.val_history、best_checkpoint
- 出边影响：按回执选边——通过→af_deliver；未过→af_repair

#### 插件卡：af_repair（executor）
- 绑定位置：af_repair 节点 node.plugins 的 loop 槽
- 触发时机：验证未过后携诊断进入
- 读（graph_state）：archify_state{val_history, best_checkpoint,
  repair_log, solver_tried, design_notes} + 本轮诊断
- 处理步骤：收敛闸门先行——读 val_history 尾部，连续 stale_limit 轮无
  新错误数下限即停止打磨，走诚实出口（带未解决诊断与历次尝试摘要如实
  汇报）。未触闸则两段修复：①零 LLM 标签清障求解器（组件重叠+建议坐标、
  标签-连线清障四向最近腾挪，附几何证据；真验证器裁定，只保留严格改进，
  更劣字节级回滚 best_checkpoint，错误清零直达闸门）——像素几何归工具；
  ②LLM 聚焦修复（≤repair_rounds 轮/访问）：只改被诊断 subject、核实
  evidence、从 supportedFixes 选方案、每轮至多一个几何控制，prompt 声明
  上轮 repair_log 与 solver_tried（防重放失败动作/失败几何）与
  design_notes 创作语境。每访问的动作摘要写回 repair_log，失败 nudge 键
  写回 solver_tried。
- 写（graph_state）：archify_state.repair_log、solver_tried、
  best_checkpoint（回滚）
- 出边影响：刷新下限→af_validate（再试）；stale 触闸→af_report（诚实
  出口边，已声明）

#### 插件卡：af_deliver（executor）
- 绑定位置：af_deliver 节点 node.plugins 的 loop 槽
- 触发时机：候选冻结后的一次性最终验收站
- 读（graph_state）：archify_state.artifact_path（冻结字节）
- 处理步骤：executor 内确定性跑 deliver CLI（直调 bash）：把冻结规范字
  节快照为同目录私有副本，渲染并检查**该快照**，原子提交 HTML，输出规范
  与产物的 SHA-256 与字节数回执。非零退出绝不能被描述为成功；失败交付
  保留旧输出。
- 写（graph_state）：交付回执（哈希/字节数/快照路径）
- 出边影响：退出码 0→af_visual_check；非零→af_report（交付失败逃生
  边：失败路径不跑浏览器检查，否则会查到陈旧的上一份好产物）

#### 插件卡：af_visual_check（executor）
- 绑定位置：af_visual_check 节点 node.plugins 的 loop 槽
- 触发时机：交付成功后
- 读（graph_state）：已交付 HTML 的确切路径
- 处理步骤：executor 内确定性跑 visual-check CLI（直调 bash）：从确切
  的已交付 HTML 收集有界自动化浏览器证据（行为测量+明/暗主题截图侧
  车），不修改不重渲染交付物；机器可读测量与截图不证明感知精致——证据
  留给感知评审站独立判读。
- 写（graph_state）：浏览器证据（测量 JSON+截图侧车路径）
- 出边影响：→af_percept

#### 插件卡：af_percept（executor）
- 绑定位置：af_percept 节点 node.plugins 的 loop 槽
- 触发时机：浏览器证据收集后
- 读（graph_state）：af_visual_check 的截图侧车
- 处理步骤：图像能力评审模型（config bag 指定视觉 provider/model）按截
  图侧车逐项审查感知质量：构图收敛/双主题一致/连线质量/标签遮罩/卡片适
  配/READ 常态/导出整洁；判定 JSON 解析失败按 percept_retries 自纠重试。
  只评所附截图、未附不评；无证据或评审模型无图像能力时如实标 skipped，
  绝不编造通过；判定独立于确定性检查与浏览器证据（不互相覆盖）。
- 写（graph_state）：感知判定（逐项 passed/failed/skipped）
- 出边影响：→af_report

#### 插件卡：af_report（executor）
- 绑定位置：af_report 节点 node.plugins 的 loop 槽
- 触发时机：终站（正常交付、交付失败逃生、修复诚实出口三条路汇入）
- 读（graph_state）：三级证明回执（deliver/浏览器/感知）+ archify_state 迹
- 处理步骤：零工具语义站：把三种证明分开陈述——deliver=确定性产物检
  查；visual-check=真实浏览器中的有界行为；感知审查=按截图的独立判定
  （passed/failed/skipped）；汇报从回执组装，非零退出的命令不声称成功、
  未实施的检查不声称已做；修复诚实出口路径则如实列出未解决诊断与历次
  尝试。最终迹写 metadata（val_history/repair_log/design_notes 等摘要），
  引擎轮末读取。
- 写（graph_state）：metadata.archify（最终迹）；TurnResult.content=三级汇报
- 出边影响：终止（is_end）；汇报是本轮唯一用户可见回复

## 工具描述卡

#### 工具卡：bash（shell）
- 用途：确定性站在 executor 内直调（经 _execute_tool，走三层授权解
  析）——validate/deliver/visual-check 三个 archify CLI 与更新检查器全
  部由此执行；af_update_probe/af_validate/af_repair/af_deliver/
  af_visual_check 的 use_tools 都含它
- 参数：命令行（workdir=会话工作区）、timeout 等
- 返回：stdout/stderr+退出码（CLI 输出机器可读 JSON 回执）
- 为什么是工具而非 prompt：验收与几何是确定性变换，错误判定必须可重放；
  CLI 回执是「验收归回执」纪律的物理载体

#### 工具卡：read_text（filesystem）
- 用途：创作/修复站读 schema、示例、候选与诊断文件
- 参数：绝对路径（会话工作区内）
- 返回：文件文本
- 为什么是工具而非 prompt：schema 与候选是外部文件事实，必须实读不可
  凭记忆重构

#### 工具卡：write_text（filesystem）
- 用途：创作站写候选 JSON、修复站整体重写候选
- 参数：绝对路径 + 内容
- 返回：写入确认
- 为什么是工具而非 prompt：产物优先纪律的载体——候选必须落盘为文件
  （验证/交付 CLI 只认文件），不能停留在对话文本里

#### 工具卡：edit_file（filesystem）
- 用途：修复站对候选 JSON 做局部修改（配合「每轮至多一个几何控制」）
- 参数：绝对路径 + 旧串/新串
- 返回：编辑确认
- 为什么是工具而非 prompt：聚焦修复要求最小 diff，整文件重写会引入未
  诊断的改动

#### 工具卡：find_files（filesystem）
- 用途：创作站在工作区定位 schema/示例/已有候选文件
- 参数：目录+模式
- 返回：匹配文件路径列表
- 为什么是工具而非 prompt：文件存在性是可验证事实，路径猜测会导致后续
  全链路落空

## 实现注意事项

- **该应用已存在**：`apps/archify_agent/`，本条目为其结构沉淀，是「agent
  + 修复回路」的参考实现，也是唯一带全量 config.yaml 的应用（llm per-
  node 细化/loop 预算/compression/guardrails/config bag 的完整词表照它
  抄）。
- **三重独立的停止守卫**：max_steps=20（图步预算，修复环一轮 2 步，
  stale 出口全程约 16 步）× stale-N 语义停止（stale_limit 代码默认 5、
  config bag 收紧为 3，调整须同步 route 文案与测试断言）× 每站内轮帽
  （author_rounds/repair_rounds）。三者任一触发都必须诚实终止，禁止伪
  造成功。
- **非主干逃生边必须声明**：af_repair→af_report（诚实出口）与
  af_deliver→af_report（交付失败）是合法控制流边，写进 sub_nodes 引擎
  才放行；交付失败路径不跑浏览器检查（陈旧产物陷阱）。
- 状态板纪律：一个命名空间键 archify_state；全部绝对路径（相对路径对
  文件工具与 bash workdir 解析出两个不同文件——真实事故）；会话工作区
  data/<app>/<session>/；跨会话事实才进 metadata。
- **经验继承四件套是修复回路的地基**：val_history（客观收敛信号）/
  best_checkpoint（回滚源，字节级）/ repair_log + solver_tried（防重
  放）——缺一件，修复环就会打转或打磨不止。
- 感知评审与浏览器证据**分离汇报**：机器测量不证明精致；评审模型无图
  像能力或无截图时如实 skipped。感知站 provider 换源时 llm.code 与
  model 必须一起写（config.yaml 词表）。
- 配置收口：workspace_root/repo_root/author_rounds/repair_rounds/
  route_retries/stale_limit/percept_retries 全部走 config bag，executor
  经 get_pattern_custom_config("archify") 读取，不硬编码。
- 测试范式：图结构/插件绑定断言 + 脚本化 provider 与打桩 CLI 回执的全
  流水线路由测试（通过/失败/stale 诚实出口/交付失败逃生四路径）。
