# topic_research — 主题研究助手（应用模板）

> 元信息：业务形态=端到端主题研究管线（单条问题触发：预规划 → 分主题规划 → 按主题扇出并行检索 → 零 LLM 结构合并 → 报告草稿 → 流式美化交付）｜图类型=agent｜借用来源=deep_research（机制级：PREPLAN/SEARCH 相位方法直接复用其无状态执行器基类；差异=规划粒度改为研究主题、汇聚环节拆为合并/报告/美化三显式站点，示范更长扇出流水线）｜来源=逆向自 apps/topic_research_agent

## 节点清单

| code | 名称 | 用途 | sub_nodes | is_end |
|---|---|---|---|---|
| tr_preplan | 主题研究·预规划 | 初始化研究状态；模型自决是否先检索一轮补背景；移交主题规划 | tr_plan | |
| tr_plan | 主题研究·分主题规划 | 把问题拆为 3-5 个独立研究主题（JSON，自纠重试，失败降级单主题）；按主题扇出检索实例 | tr_search, tr_merge | |
| tr_search | 主题研究·检索(worker) | 扇出 worker：一实例负责一个主题的带工具检索循环（私有工作区） | tr_merge | |
| tr_merge | 主题研究·合并(join) | 零 LLM 结构化合并全部分支资料（统一 [S#] 引用编号，失败分支降级不阻塞） | tr_report | |
| tr_report | 主题研究·报告生成 | 基于合并资料撰写报告草稿（执行摘要/分主题分析/结论与不确定性/参考来源） | tr_polish | |
| tr_polish | 主题研究·格式美化 | 终态站：流式输出美化后的最终报告（不改事实与引用），写终迹 | （无） | ✓ |

## 节点交互表

站点间状态经 `graph_state["topic_research_state"]` 传递（问题/计划/合并资
料/草稿）；扇出 worker 分支隔离看不到它，成果仅经 `TurnResult.extra` 沉
入引擎 `__fanout_results__` 结果板由 join 折叠；终态迹写
`metadata["topic_research"]`。

| 节点 | 读 graph_state | 写 graph_state | 出边与路由 |
|---|---|---|---|
| tr_preplan | cxt.history（本轮问题） | topic_research_state（初始化 question/phases/messages/findings/tool_stats） | →tr_plan（content="" 静默中继）；force_close→诚实收尾文本终止 |
| tr_plan | topic_research_state（工作区消息）；无在飞状态（孤儿进入）→构造降级状态 | topic_research_state（plan/themes/degraded/超宽备注） | 正常→sends 派发 tr_search×N（上限 max_fanout）；孤儿逃生边→tr_merge（降级合并）；force_close→诚实收尾 |
| tr_search | __fanout_results__ 不读；branch_input 携带 {theme, sub_question}（分支隔离） | TurnResult.extra（findings/tool_stats/rounds/reflection_note→沉入 __fanout_results__） | 分支完成→join 汇入 tr_merge（唯一后继）；无派发坐标的防御入口→tr_merge；force_close→诚实收尾 |
| tr_merge | topic_research_state + __fanout_results__（逐分支折叠，失败分支计数置 degraded） | topic_research_state（merged_findings 统一 [S1..Sn] 编号，FIFO 截断） | →tr_report（零 LLM 结构合并，静默中继）；force_close→诚实收尾 |
| tr_report | topic_research_state（merged_findings） | topic_research_state（draft 报告草稿） | →tr_polish（草稿不直接回复，静默中继）；force_close→诚实收尾 |
| tr_polish | topic_research_state（draft） | metadata.topic_research（终迹：phases/themes/per_theme/branches/sources/tool_stats/degraded）；回复=美化后报告 | 终止（is_end）；下一轮重新从入口进图 |

**循环继承（SEARCH 分支内检索循环，≤5 轮/实例）**：检查点是每实例状态
板——system 内【研究状态板】每轮确定性重写（剩余轮数/findings 数/工具
统计/主题勾选：主题关键词出现在任一 finding 的 query 即打 ✓）；经验继
承即 findings 数组（跨轮累积、FIFO 截断、错误 JSON 不入 findings）+ 工
作区字符预算中位截断；无 tool_calls 即资料充分，content 成为分支反思小
结。**管线无图内多轮循环**（单程六站），预算三层独立：主循环图步数（5
步，worker 不占）× 扇出宽度 × 分支轮数；每真实查询后限速停顿。
**跨站继承纪律**：合并站统一引用编号后，草稿与美化站只引用 [S#]，事实
与引用在美化站被明令禁止改动。跨轮继承：metadata.topic_research 终迹保
留上轮来源；研究过程不进 cxt.history。

## Pattern 声明

```yaml
# nexus-pattern: topic_research
code: topic_research
name: 主题研究助手
description: >-
  Topic research 图配方：PREPLAN/PLAN/SEARCH/MERGE/REPORT/POLISH 各为一个
  AGENT 节点；PLAN 按主题扇出 N 个 SEARCH 实例并行检索，MERGE 结构化合并，
  REPORT 生成草稿，POLISH 流式美化交付（引擎运行时扇出的长流水线配方）。
pattern_type: agent
entry_node_code: tr_preplan
nodes:
  - code: tr_preplan
    name: 主题研究·预规划
    description: 研究流水线首站:构建研究工作区,模型自行决定是否先检索一轮补背景,完成后移交主题规划
    task_description: 为复杂问题准备研究上下文(可选预检索)
    sub_nodes: [tr_plan]
    plugins:
      loop: tr_preplan
    use_tools: [web_search_prime]
  - code: tr_plan
    name: 主题研究·分主题规划
    description: 把问题拆分为 3-5 个互相独立的研究主题(JSON 计划;解析失败自纠重试,仍失败降级为原问题单主题),随后按主题扇出 N 个检索实例(引擎运行时扇出,宽度受 max_fanout 约束)
    task_description: 拆分研究主题并派发检索实例
    sub_nodes: [tr_search, tr_merge]
    plugins:
      loop: tr_plan
  - code: tr_search
    name: 主题研究·检索(worker)
    description: 扇出 worker:一个实例负责一个研究主题的带工具 ReAct 检索循环(私有工作区,每实例独立轮次守卫);结果经引擎结果板交给合并站
    task_description: 检索单个研究主题收集研究资料
    sub_nodes: [tr_merge]
    plugins:
      loop: tr_search
    use_tools: [web_search_prime]
  - code: tr_merge
    name: 主题研究·合并(join)
    description: 扇出 join:结构化合并全部检索分支的资料(失败分支降级不阻塞),统一引用编号,零 LLM 调用;合并产物移交报告站
    task_description: 合并各主题检索资料
    sub_nodes: [tr_report]
    plugins:
      loop: tr_merge
  - code: tr_report
    name: 主题研究·报告生成
    description: 基于合并资料撰写研究报告草稿(执行摘要/分主题分析/结论与不确定性/参考来源,引用 [S1] 标记);草稿不直接回复,移交美化站
    task_description: 撰写研究报告草稿
    sub_nodes: [tr_polish]
    plugins:
      loop: tr_report
  - code: tr_polish
    name: 主题研究·格式美化
    description: 终态站:流式输出美化后的最终报告(标题层级/重点加粗/来源列表对齐,不改事实与引用),本回合回复
    task_description: 美化报告格式并交付
    sub_nodes: []
    plugins:
      loop: tr_polish
    is_end: true
allow_toolset: [mcp-websearch]
```

## 插件步骤卡

#### 插件卡：tr_preplan（executor）
- 绑定位置：tr_preplan 节点 node.plugins 的 loop 槽
- 触发时机：每条研究消息进图的首站
- 读（graph_state）：cxt.history（取本轮用户问题）
- 处理步骤：**复用 deep_research 执行器基类的 PREPLAN 相位方法**（跨应
  用 import，无重实现）：force_close 诚实收尾防御 → 等待 MCP 就绪闸 →
  解析工具面 → 单次带工具 LLM 调用由模型自决是否预检索补背景（无
  tool_calls 跳过；有则执行一轮并留痕 round=0），过程不转发流式增量 →
  初始化研究状态板。
- 写（graph_state）：topic_research_state（question/phases/messages/findings/tool_stats）
- 出边影响：TurnResult(content="", next=tr_plan) 静默中继

#### 插件卡：tr_plan（executor）
- 绑定位置：tr_plan 节点 node.plugins 的 loop 槽
- 触发时机：预规划完成后的第二站
- 读（graph_state）：topic_research_state（messages 含预检索上下文）
- 处理步骤：孤儿防御（无在飞状态→构造降级状态走逃生边直达合并站）→
  分主题规划相位：单次无工具 LLM 调用把问题拆为 3-5 个互相独立的研究主
  题（JSON {"themes":[...]}；容错抽取首个平衡 {...} 块；失败把坏输出+错
  误反馈进 messages 自纠重试一次；仍失败降级为「原问题单主题」并标记
  degraded）→ 派发：每主题一个 Send(tr_search, {theme, sub_question})，
  超 max_fanout 截断并在 plan 备注如实说明。
- 写（graph_state）：topic_research_state（plan/themes/degraded）
- 出边影响：TurnResult.sends 扇出；tr_merge 是声明的孤儿逃生边（sends
  目标边的 join 兄弟边，必须声明否则引擎悬边告警）

#### 插件卡：tr_search（executor）
- 绑定位置：tr_search 节点 node.plugins 的 loop 槽
- 触发时机：扇出派发后，每个 worker 实例并发执行一次（实例数=主题数）
- 读（graph_state）：不可见图状态（分支隔离）——上下文全部来自
  ec.branch_input 载荷
- 处理步骤：**复用 deep_research 执行器基类的 SEARCH 相位方法**（私有版
  工具派发、状态板重写、限速全套随行）：无派发坐标的防御入口照孤儿直
  达合并站；私有工作区小步带工具检索循环 ≤5 轮（每轮重写状态板；tool_
  calls 走「归一化→hooks 改写→可用集校验→执行→P5 改写」私有派发链，
  只写私有 messages 永不写 cxt.history；同轮非首次查询前限速停顿）；无
  tool_calls 即资料充分收束；成果只经 TurnResult.extra 返回。
- 写（graph_state）：TurnResult.extra（sub_question/findings/tool_stats/rounds/reflection_note）
- 出边影响：分支无路由权——引擎按唯一公共后继 settle 结果板后执行 join

#### 插件卡：tr_merge（executor）
- 绑定位置：tr_merge 节点 node.plugins 的 loop 槽
- 触发时机：全部分支 settle 后的 join 屏障站
- 读（graph_state）：topic_research_state + __fanout_results__ 结果板
- 处理步骤：**零 LLM 纯结构合并**（本配方与 deep_research 的关键差异
  之一）：逐分支取 ok 条目的 findings 并入（错误条目只计数置 degraded，
  不阻塞不重试），全局 FIFO 截断，再统一重编引用号为 [S1..Sn]（合并顺
  序确定→编号确定，下游只引编号）；合并产物写回状态板，全程无模型调用。
- 写（graph_state）：topic_research_state（merged_findings 统一编号）
- 出边影响：TurnResult(content="", next=tr_report) 静默中继

#### 插件卡：tr_report（executor）
- 绑定位置：tr_report 节点 node.plugins 的 loop 槽
- 触发时机：合并完成后的草稿站
- 读（graph_state）：topic_research_state（merged_findings）
- 处理步骤：单次无工具 LLM 调用基于合并资料撰写报告草稿，结构固定为执
  行摘要 / 分主题分析 / 结论与不确定性 / 参考来源，事实陈述必须挂
  [S#] 引用标记；资料为空时如实写证据不足。草稿不转发流式、不直接回复，
  写回状态板移交美化站。
- 写（graph_state）：topic_research_state（draft）
- 出边影响：TurnResult(content="", next=tr_polish) 静默中继

#### 插件卡：tr_polish（executor）
- 绑定位置：tr_polish 节点 node.plugins 的 loop 槽
- 触发时机：草稿完成后的终态站（本轮最后一站）
- 读（graph_state）：topic_research_state（draft）
- 处理步骤：单次流式 LLM 调用美化格式（标题层级/重点加粗/来源列表对
  齐），prompt 明令不改事实与引用——美化只动呈现不动内容；流式增量转
  发（本配方唯一流式相位，与 deep_research 的 synthesize 对应）；写终态
  迹到 metadata（phases/themes/per_theme/branches/sources/tool_stats/
  degraded/完成时间）并触发收尾 hooks；force_close 时返回诚实收尾文本。
- 写（graph_state）：metadata.topic_research（终迹）；TurnResult.content=最终报告
- 出边影响：终止（is_end，next=None）；报告是本轮唯一用户可见回复

## 工具描述卡

#### 工具卡：web_search_prime（mcp-websearch）
- 用途：联网检索（zai 官方远程 web search），预检索补背景与各主题检索
  分支的查询执行
- 参数：search_query（检索词）等 JSON 参数（MCP schema 定义）
- 返回：检索结果文本（错误以 {"error":...} JSON 返回，永不入 findings）
- 为什么是工具而非 prompt：外部事实数据访问；研究结论必须来自真实检索
  结果且报告带 [S#] 引用可溯源，模型记忆编造不可接受

## 实现注意事项

- **该应用已存在**：`apps/topic_research_agent/`（route.py 声明图，
  executor.py 承载执行体），本条目为其结构沉淀，是「长扇出流水线」配
  方：把综合环节拆为合并/报告/美化三显式站点（deep_research 条目为一站
  式综合，两者互补）。
- **相位复用范式**：PREPLAN/SEARCH 直接 import deep_research 的执行器基
  类复用其无状态相位方法（apps 层间 import，分层契约不受影响）；fork 本
  模板时保持「机制借用走子类/相位复用、站点自有逻辑写本应用 executor」
  的分工。
- **零 LLM 合并站是分诊样板**：结构化折叠（并数组/重编号/计数）是确定
  性变换，交给代码——省一次模型调用且保证编号确定，报告与美化站才能稳
  定引用。
- Send 载荷纪律、孤儿逃生边声明、三层预算、MCP 就绪闸、findings 卫生：
  与 deep_research 条目同构，照其实现注意事项执行（状态键换为
  topic_research_state / metadata.topic_research）。
- 美化站的「不改事实与引用」是 prompt 硬约束而非代码校验；若需强保证，
  可在美化后比对 [S#] 集合（确定性校验），属实现增强项。
- 密钥：MCP server 密钥走环境变量引用（${Z_AI_API_KEY}），不进模板。
- 测试范式：tests/test_topic_research.py——图结构/插件绑定、全流水线
  （sends 扇出 + merge 零 LLM 断言）、主题自纠与降级、单分支失败降级、
  流式 delta 只来自 polish。
