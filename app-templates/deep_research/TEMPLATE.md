# deep_research — 深度研究助手（应用模板）

> 元信息：业务形态=端到端研究管线（单条问题触发：预检索 → 子问题规划 → 按子问题扇出并行检索 → 汇聚综合出带引用报告）｜图类型=agent｜借用来源=零借用（本条目是引擎运行时 fan-out 配方之源：sends 派发纪律、branch_input 载荷、__fanout_results__ join、孤儿逃生边均自此沉淀；topic_research 条目机制级借用其检索分支）｜来源=逆向自 apps/deep_research_agent（route_multi.py + executor_multi.py 图版）

## 节点清单

| code | 名称 | 用途 | sub_nodes | is_end |
|---|---|---|---|---|
| dr_preplan | 深度研究·预检索 | 初始化研究状态；模型自行决定是否先检索一轮补背景；移交规划 | dr_plan | |
| dr_plan | 深度研究·规划与派发 | 分解子问题（JSON 计划，自纠重试，失败降级原问题）；按子问题扇出 N 个检索实例 | dr_search, dr_synthesize | |
| dr_search | 深度研究·检索(worker) | 扇出 worker：一实例负责一个子问题的带工具检索循环（私有工作区） | dr_synthesize | |
| dr_synthesize | 深度研究·综合(join) | 汇聚预检索资料与全部分支成果（失败分支降级不阻塞），流式产出研究报告 | （无） | ✓ |

## 节点交互表

阶段间状态经 `graph_state["deep_research_state"]` 传递（问题/工作区消息/
findings/计划/阶段迹）；扇出 worker 看不到它（分支隔离，cxt 副本为空），
其成果只经 `TurnResult.extra` 沉到引擎 `__fanout_results__` 结果板，由
join 节点合并；最终研究迹写 `metadata["deep_research"]` 供观测与续研。

| 节点 | 读 graph_state | 写 graph_state | 出边与路由 |
|---|---|---|---|
| dr_preplan | —（入口，初始化 deep_research_state：question/phases/findings/tool_stats/plan） | deep_research_state（+预检索 messages/ findings/tool_stats） | →dr_plan（content="" 静默中继）；force_close→诚实收尾文本终止 |
| dr_plan | deep_research_state（工作区消息）；无在飞状态（孤儿进入）→构造降级状态 | deep_research_state（plan、degraded、超宽截断备注） | 正常→sends 派发 dr_search×N（上限 max_fanout）；孤儿逃生边→dr_synthesize（降级综合）；force_close→诚实收尾 |
| dr_search | __fanout_results__ 不读；branch_input 携带 {theme, sub_question}（分支隔离，图状态不可见） | TurnResult.extra（findings/tool_stats/rounds/reflection_note→引擎沉入 __fanout_results__） | 分支完成→join 汇入 dr_synthesize（引擎公共后继解析）；无派发坐标的防御入口→dr_synthesize；force_close→诚实收尾 |
| dr_synthesize | deep_research_state（findings/plan）+ __fanout_results__（逐分支合并，失败分支计入 degraded） | metadata.deep_research（最终研究迹，含 branches 汇总）；deep_research_state 清理由图终止承载 | 终止（is_end）；报告=本轮唯一用户可见回复 |

**循环继承（SEARCH 分支内检索循环，≤5 轮/实例）**：检查点是每实例状态
板——system 内【研究状态板】每轮确定性重写（剩余轮数/findings 数/工具
统计/子问题勾选：子问题关键词出现在任一 finding 的 query 即打 ✓，纯代
码启发式）；经验继承即 findings 数组（跨轮累积、30 条 FIFO 截断、错误
JSON 永不入 findings）+ 工作区 60k 字符预算超限时对最老 tool 行中位截
断；无 tool_calls 即模型判定资料充分，content 成为分支反思小结收束。
**强制收尾语义**：max_steps 耗尽的 force_close 节点返回固定诚实收尾文
本（不伪造报告）——三层预算独立生效：图步数 × 扇出宽度 × 分支轮数。
跨轮继承：metadata.deep_research 迹保留上轮来源（续研可引用）；研究过
程永不进 cxt.history（history 只有「问题→报告」问答对）。

## Pattern 声明

```yaml
# nexus-pattern: deep_research
code: deep_research
name: 深度研究助手
description: >-
  Deep research 图配方：PREPLAN/PLAN/SEARCH/SYNTHESIZE 各为一个 AGENT
  节点；PLAN 按子问题扇出 N 个 SEARCH 实例并行检索，SYNTHESIZE 作 join
  汇聚综合（引擎运行时扇出的验收配方）。
pattern_type: agent
entry_node_code: dr_preplan
nodes:
  - code: dr_preplan
    name: 深度研究·预检索
    description: 研究流水线首站：构建研究工作区，模型自行决定是否先检索一轮补背景，完成后移交规划
    task_description: 为复杂问题准备研究上下文(可选预检索)
    sub_nodes: [dr_plan]
    plugins:
      loop: dr_preplan
    use_tools: [web_search_prime]
  - code: dr_plan
    name: 深度研究·规划与派发
    description: 把问题分解为可检索验证的子问题(JSON 计划;解析失败自纠重试,仍失败降级为原问题单计划),随后按子问题扇出 N 个检索实例(引擎运行时扇出,宽度受 max_fanout 约束)
    task_description: 产出研究计划并派发检索实例
    sub_nodes: [dr_search, dr_synthesize]
    plugins:
      loop: dr_plan
  - code: dr_search
    name: 深度研究·检索(worker)
    description: 扇出 worker:一个实例负责一个子问题的带工具 ReAct 检索循环(私有工作区,每实例独立轮次守卫);结果经引擎结果板交给综合站
    task_description: 检索单个子问题收集研究资料
    sub_nodes: [dr_synthesize]
    plugins:
      loop: dr_search
    use_tools: [web_search_prime]
  - code: dr_synthesize
    name: 深度研究·综合(join)
    description: 扇出 join:合并预检索资料与全部检索分支的成果(失败分支降级不阻塞),流式生成带引用的研究报告
    task_description: 综合资料产出研究报告
    sub_nodes: []
    plugins:
      loop: dr_synthesize
    is_end: true
allow_toolset: [mcp-websearch, mcp-zai]
```

## 插件步骤卡

#### 插件卡：dr_preplan（executor）
- 绑定位置：dr_preplan 节点 node.plugins 的 loop 槽
- 触发时机：每条研究消息进图的首站（agent 图从入口整图执行）
- 读（graph_state）：cxt.history（取本轮用户问题）、pattern/node 装配的 base_prompt
- 处理步骤：①force_close 防御：步数预算耗尽直接返回诚实收尾文本；②等
  待 MCP 就绪闸（连接未完成时解析工具会冻结出空可用集）；③解析工具面
  并收集 on_agent_start hooks 片段；④PREPLAN 相位——单次带工具 LLM 调
  用，模型自决是否先检索一轮补背景（无 tool_calls 即跳过；有则执行检索
  轮，结果留在 messages 供规划引用，findings 记 round=0 预检索标记），过
  程不转发流式增量；⑤初始化研究状态板并落盘。
- 写（graph_state）：deep_research_state（question/phases/messages/base_messages/findings/tool_stats）
- 出边影响：TurnResult(content="", next=dr_plan)——中间站静默，回复留
  给终站

#### 插件卡：dr_plan（executor）
- 绑定位置：dr_plan 节点 node.plugins 的 loop 槽
- 触发时机：预检索完成后的第二站
- 读（graph_state）：deep_research_state（messages 含预检索上下文）
- 处理步骤：①孤儿防御：无在飞状态（异常进入）时构造降级状态并走逃生
  边直达综合站；②PLAN 相位——单次无工具 LLM 调用产出
  {"sub_questions":[...]}；容错 JSON 抽取（首个平衡 {...} 块），失败时
  把坏输出与解析错误作为 assistant/user 两行反馈进 messages 自纠重试
  一次，仍失败降级为「原问题单计划」并标记 degraded——规划失败永不阻
  塞研究；③派发：每个子问题一个 Send(dr_search, {theme, sub_question})
  实例，超过 max_fanout 截断并在 plan.notes 如实备注（非致命）。
- 写（graph_state）：deep_research_state（plan/degraded/notes）
- 出边影响：TurnResult.sends 扇出（与 next 互斥）；dr_synthesize 是声明
  的孤儿逃生边（也是 sends 目标边的兄弟边，必须声明否则引擎悬边告警）

#### 插件卡：dr_search（executor）
- 绑定位置：dr_search 节点 node.plugins 的 loop 槽
- 触发时机：扇出派发后，每个 worker 实例并发执行一次（实例数=子问题数）
- 读（graph_state）：不可见图状态（分支隔离）——上下文全部来自
  ec.branch_input 载荷（theme + sub_question），这是派发方必须折叠齐全
  载荷的原因
- 处理步骤：①无派发坐标的防御入口照孤儿处理直达综合站；②SEARCH 相
  位——私有 messages 工作区（system=基础 prompt + 【研究子任务】框定 +
  【研究状态板】；user=子问题本身），小步带工具检索循环 ≤5 轮：每轮重
  写状态板（剩余轮数/findings 数/工具统计/子问题勾选），tool_calls 走私
  有版工具派发（通用工具名先归一化→hooks P4 改写→可用集校验（非法名
  反馈错误 JSON）→执行→P5 改写；只写私有 messages 永不写 cxt.history；
  同轮非首次检索前限速停顿）；无 tool_calls 即模型判定充分，content 作
  为分支反思小结收束；工作区超 60k 字符中位截断最老 tool 行；③全部成
  果只经 TurnResult.extra 返回（引擎沉入 __fanout_results__ 结果板）。
- 写（graph_state）：TurnResult.extra（sub_question/findings/tool_stats/rounds/reflection_note）
- 出边影响：分支无路由权——引擎按公共后继把实例结果 settle 到结果板后
  执行 join 节点；分支内 wait_human 禁止

#### 插件卡：dr_synthesize（executor）
- 绑定位置：dr_synthesize 节点 node.plugins 的 loop 槽
- 触发时机：全部分支 settle 后的 join 屏障站（本轮最后一站）
- 读（graph_state）：deep_research_state（预检索 findings/plan）+ __fanout_results__ 结果板
- 处理步骤：①join 合并：逐分支取 ok 条目的 findings/tool_stats/rounds/
  反思小结并入状态（findings 全局 30 条 FIFO 截断；失败分支计数、置
  degraded——报告基于部分资料，不阻塞不重试）；②SYNTHESIZE 相位——
  findings 编号成 [S1][S2]… 引用块（空资料时明示「证据不足」），瘦身消
  息后流式生成带引用的研究报告（唯一转发流式增量的相位）；③终迹落盘：
  metadata 写入完整研究迹（阶段序列/降级标记/子问题/来源/工具统计/轮
  次/branches 汇总/完成时间），并触发 on_agent_end hooks。
- 写（graph_state）：metadata.deep_research（最终研究迹）；TurnResult.content=报告
- 出边影响：终止（is_end，next=None）；报告是本轮唯一用户可见回复（前
  面各站全部静默）

## 工具描述卡

#### 工具卡：web_search_prime（mcp-websearch）
- 用途：联网检索（zai 官方远程 web search），预检索补背景与各检索分支
  的查询执行
- 参数：search_query（检索词）等 JSON 参数（MCP schema 定义）
- 返回：检索结果文本（错误以 {"error":...} JSON 返回，永不入 findings）
- 为什么是工具而非 prompt：外部事实数据访问；研究结论必须来自真实检索
  结果且报告带 [S#] 引用可溯源，模型记忆编造不可接受

#### 工具卡：analyze_image（mcp-zai）
- 用途：视觉分析工具族代表（analyze_image / analyze_video /
  extract_text_from_screenshot / ui_diff_check / understand_technical_
  diagram / analyze_data_visualization / diagnose_error_screenshot）——
  截图/图表/界面图的视觉理解与比对
- 参数：图像（URL/base64）+ 分析指令等 JSON 参数
- 返回：视觉分析结论文本
- 为什么是工具而非 prompt：视觉感知是外部能力调用（多模态模型服务），
  不是提示词能替代的；本模板已授权该工具集作为视觉增强研究的预留位，
  当前无节点挂其工具（MCP 工具启动后异步注册，未注册名校验按延迟合法）

## 实现注意事项

- **该应用已存在**：`apps/deep_research_agent/`（route_multi.py 声明图，
  executor_multi.py 承载全部执行体），本条目为其结构沉淀，是引擎运行时
  fan-out 的验收配方。
- **Send 载荷纪律**：worker 实例在私有工作区运行、无会话历史、不可见图
  状态——派发节点必须把所需上下文（主题+子问题）完整折叠进 Send.input；
  worker 成果只能经 TurnResult.extra 回流（不直接成为用户回复）。
- **孤儿逃生边必须声明**：dr_plan/dr_search 的异常入口直达 dr_synthesize
  是合法控制流边，写进 sub_nodes 引擎才放行（未声明目标会终止图并告
  警）；join 节点 = 全部扇出目标的唯一公共后继（dr_search 只声明
  dr_synthesize 一个后继）。
- **三层预算公式**：max_steps ≥ 干线步数（3）+ 余量；扇出宽度
  max_fanout 截断子问题数（超宽如实备注不致命）；分支轮数 ≤5 独立守卫。
  force_close 节点必须返回诚实「研究被强制收尾」文本。
- MCP 时序：工具解析前必须 await ensure_mcp_ready()（连接未就绪时可用
  集冻结为空）；查询限速（每轮非首次查询前停顿数秒）在分支内独立计。
- findings 卫生：错误 JSON 永不入 findings；全局 FIFO 截断保留最新；工
  作区字符预算超限中位截断最老 tool 行——三条共同保证 join 消息不爆炸。
- 模型泛化：通用工具名归一化表（如 web_search→web_search_prime）在
  hooks/守卫前就地归一，省一轮自纠（assistant 载荷保留规范名，历史顺便
  教会模型正确名）。
- 密钥：MCP server 密钥走环境变量引用（host/config/local_config.yaml 的
  ${Z_AI_API_KEY}），不进模板与代码。
- 测试范式：脚本化 provider + 打桩 MCP 工具，断言扇出派发数、join 合并、
  失败分支降级、孤儿逃生边、force_close 诚实收尾。
