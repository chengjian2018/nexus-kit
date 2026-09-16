# ppt_generator_agent — AI PPT 生成助手（应用模板）

> 元信息：业务形态=外部 API 交付型对话（收集参数 → 逐轮确认 → 调用外部服务 → 交付产物链接）｜图类型=fsm｜借用来源=install_booking_agent（机制级：FSMUnifiedNLU 子类 + 确定性守卫搭统一阶段便车、两拍拒绝收尾、离线路由测试范式）｜来源=逆向自 apps/ppt_generator_agent（源自 ai-ppt-generator skill）

## 节点清单

| code | 名称 | 用途 | sub_nodes | is_end |
|---|---|---|---|---|
| ppt_start | 开场收集主题 | 收集 PPT 主题（槽位 ppt_topic） | ppt_ask_template, ppt_decline | |
| ppt_ask_template | 模板选择询问 | 问是否自选模板风格：是→列表；否→自动匹配生成 | ppt_show_themes, ppt_generate, ppt_decline | |
| ppt_show_themes | 模板列表展示 | 展示真实模板列表（守卫确定性注入），收模板编号/风格名 | ppt_generate, ppt_ask_template, ppt_decline | |
| ppt_generate | PPT生成交付 | 转入即由守卫执行真实生成并交付链接；承载交付后分支（感谢/换模板/再生成/取消） | ppt_end, ppt_show_themes, ppt_generate, ppt_decline | |
| ppt_decline | 通用取消承接 | 任意阶段的取消意图，共情回应后收尾（两拍第一拍） | ppt_end | |
| ppt_end | 结束语 | 所有终止路径的礼貌收尾 | （无） | ✓ |

## 节点交互表

| 节点 | 读 graph_state | 写 graph_state | 出边与路由 |
|---|---|---|---|
| ppt_start | —（入口，首次访问建 ppt_gen_state 键） | — | 主题给出→ppt_ask_template；取消意图→ppt_decline |
| ppt_ask_template | ppt_gen_state（弱依赖，主题经 filled_slots 传递） | —（want_choose 进 filled_slots） | 自选→ppt_show_themes；自动→ppt_generate；取消→ppt_decline |
| ppt_show_themes | ppt_gen_state.themes（缓存命中即免拉取） | themes、themes_fetched（守卫在缓存未建时写入） | 选定→ppt_generate；返回/拿不定主意→ppt_ask_template；取消→ppt_decline |
| ppt_generate | topic、themes、selected、failed_tpl_ids | topic、selected、last_result、failed_tpl_ids（失败 +1） | 成功落位后：感谢→ppt_end；换模板→ppt_show_themes；再生成→自环 ppt_generate；取消→ppt_decline；生成失败→确定性停留（next_node 置空，留在原节点） |
| ppt_decline | — | —（decline_reason 进 filled_slots） | →ppt_end |
| ppt_end | — | —（graph_state 随会话终止清理） | 终止（is_end） |

**循环继承（生成 ⇄ 重试/换模板循环）**：`failed_tpl_ids` 是试败日志——
同模板连续失败 ≥2 次后确定性拒绝重试，回复里如实列出已尝试的模板与次数
（防重放记忆，archify `solver_tried` 的同构物）；`themes` 缓存跨循环继承
（换模板回到列表节点不再重复拉取）；`topic` 写入状态板后由重新生成轮
继承，不依赖对话历史。无部分产物，故无 best_checkpoint。

## Pattern 声明

```yaml
# nexus-pattern: ppt_generator_agent
code: ppt_generator_agent
name: AI PPT 生成助手（skill 转写）
description: >-
  FSM 统一阶段推进 AI PPT 生成：收集主题、询问模板选择意向、展示真实
  模板列表（确定性注入）、按选定或智能匹配模板执行真实生成并交付下载
  链接；通用取消通道；模板编号与链接全部来自 API 数据，模型零编造。
pattern_type: fsm
entry_node_code: ppt_start
stages:
  - nlu: ppt_unified
  - nlg: nlg_pass_through
nodes:
  - code: ppt_start
    name: 开场收集主题
    description: 开场并收集本次要生成的 PPT 主题（一句话主题或内容描述），拿到主题后进入模板选择询问
    task_description: 收集 PPT 主题，确认后进入模板选择询问
    slots:
      ppt_topic: 用户要生成的 PPT 主题/内容描述
    sub_nodes: [ppt_ask_template, ppt_decline]
  - code: ppt_ask_template
    name: 模板选择询问
    description: 拿着已收集的主题询问用户是否自己挑选模板风格；要则展示真实列表，不要则由系统按主题关键词智能匹配后直接生成
    task_description: 询问是否自己挑选模板风格，分发到列表选择或自动匹配
    slots:
      want_choose: 是否要自己挑选模板风格（是/否）
    sub_nodes: [ppt_show_themes, ppt_generate, ppt_decline]
  - code: ppt_show_themes
    name: 模板列表展示
    description: 展示系统拉取的真实模板列表（风格名+模板编号），等待用户按编号或风格名称选定；列表由守卫确定性注入，模型不参与编造
    task_description: 展示真实模板列表并收集用户选定的模板编号/名称
    slots:
      tpl_id: 用户选定的模板编号
      style_name: 或用户选定的模板风格名称
    sub_nodes: [ppt_generate, ppt_ask_template, ppt_decline]
  - code: ppt_generate
    name: PPT生成交付
    description: 转入本节点时由守卫执行真实生成（选定模板或按主题自动匹配，调用外部 AIPPT 服务，约 2-5 分钟）并把回复改写为生成结果（标题+下载链接）；落位后承载感谢/换模板/再生成/取消分支
    task_description: 执行 PPT 生成并交付下载链接，处理交付后的收尾分支
    slots:
      tpl_id: 本次生成使用的模板编号（自动匹配时由守卫回填）
      generation_status: 本次生成结果状态（success/failed）
    sub_nodes: [ppt_end, ppt_show_themes, ppt_generate, ppt_decline]
  - code: ppt_decline
    name: 通用取消承接
    description: 通用退出通道：用户在流程任何阶段表示不想做了/取消/以后再说，按场景共情回应，然后转入结束语
    task_description: 识别取消意图并共情回应，转结束语
    slots:
      decline_reason: 取消原因（不想做了/稍后再说/其他）
    sub_nodes: [ppt_end]
  - code: ppt_end
    name: 结束语
    description: 收尾：交付完成、用户取消等所有终止路径的礼貌收尾
    task_description: 礼貌收尾，结束会话
    slots: {}
    sub_nodes: []
    is_end: true
```

## 插件步骤卡

#### 插件卡：ppt_unified（stage）
- 绑定位置：pattern.stages 的 nlu 槽
- 触发时机：每轮 FSM 管线执行到 NLU 位；内建统一阶段（FSMUnifiedNLU 子类）单次 LLM 调用产出 reply/next_node/slots 之后
- 读（graph_state）：ppt_gen_state.themes（模板缓存）、ppt_gen_state.failed_tpl_ids（试败记忆）、ppt_gen_state.topic
- 处理步骤：在单次 LLM 理解与话术之上叠加三道确定性零 LLM 守卫。①模板选定解析：转入生成节点且携带 tpl_id/风格名时，与真实缓存列表做精确/包含匹配，解析失败确定性停留并如实重列可选项（从列表节点出发必须选定，不静默转自动匹配）。②生成守卫：任何转入生成节点的转移——先继承主题（filled_slots → 状态板），解析模板（用户选定优先，否则关键词分类器自动匹配，列表不可用时退化为 API 随机模板），查试败上限（同模板失败 ≥2 次确定性拒绝并播报已试列表），通过后以后台线程执行真实外部生成（阻塞 2-5 分钟），成功则把回复改写为真实标题+下载链接，失败则确定性停留、如实播报原因并计入试败日志。③模板列表改写：转入列表节点时拉取（或读缓存）真实模板列表并整体改写回复。
- 写（graph_state）：themes、themes_fetched、topic、selected、last_result、failed_tpl_ids
- 出边影响：守卫可强制 next_node 置空（确定性停留）、维持原转移，或维持转移并改写 nlg_result；成功交付回复（含真实 URL）由本卡产出，模型只写过渡语

#### 插件卡：nlg_pass_through（stage）
- 绑定位置：pattern.stages 的 nlg 槽（内置件，atoms/stages/unified.py）
- 触发时机：每轮管线 NLG 位
- 读（graph_state）：—
- 处理步骤：内置占位 NLG——沿用统一阶段或守卫已写入的 nlg_result，跳过第二次 LLM 生成（守卫改写优先）。
- 写（graph_state）：—
- 出边影响：无（不改路由）

## 工具描述卡

#### 工具卡：ppt_gen_list_themes（ppt_gen）
- 用途：拉取百度文库 AI 可用 PPT 模板列表（风格名 / style_id / tpl_id）
- 参数：无（密钥读环境变量 BAIDU_API_KEY）
- 返回：模板对象数组（style_name_list / style_id / tpl_id），上限 100 条
- 为什么是工具而非 prompt：外部 API 调用；模板编号必须来自服务端真实数据，LLM 编造 tpl_id 不可接受

#### 工具卡：ppt_gen_generate（ppt_gen）
- 用途：两段式生成 PPT（先出大纲、再按大纲生成），流式等待至 is_end，返回含 ppt_url 的最终事件
- 参数：query（PPT 主题，必填）、style_id（默认 0）、tpl_id（可选）、web_content（可选参考内容）
- 返回：最终事件 dict（is_end=true 时 data.ppt_url 为下载链接）；耗时约 2-5 分钟
- 为什么是工具而非 prompt：外部 API 调用 + 长阻塞任务；交付链接必须来自服务端真实返回，编造不可接受

## 实现注意事项

- **FSM 轮末转移时序陷阱**：所有确定性改写必须搭统一阶段（ppt_unified）
  便车；节点级 NLG 会晚一拍生效并覆盖当轮回复（详见
  nexus-app-template-skill/references/pitfalls.md 第 5 条）。
- 统一阶段模板（prompts.py）含「防编造铁律」：跳转列表/生成节点时
  reply 只写过渡语，列表与结果由守卫注入；模板挂节点
  base_nlu_prompt（node.config）。
- 生成调用为 2-5 分钟阻塞任务，须以 `asyncio.to_thread` 承载；密钥
  仅从环境变量 `BAIDU_API_KEY` 读取，未配置时确定性如实告知并停留
  （配置错误不计入模板试败）。
- 工具注册于 toolset `ppt_gen`，但 pattern **不授予** allow_toolset
  （守卫直调函数本体；拒绝式默认语义未放宽）。
- 自动匹配的关键词分类器是纯代码查表（表序=优先级），不进 prompt；
  分类语义忠实于源 skill（如「报告」命中企业商务优先于科技）。
- 测试范式：离线路由测试（脚本化 provider + 打桩 API 客户端，含试败
  继承与防重放断言），照 tests/test_install_booking_agent_route.py。
- 该模板已有完整实现：`apps/ppt_generator_agent/`（本条目为其结构
  沉淀，供同类「外部 API 交付型对话」场景复用）。
