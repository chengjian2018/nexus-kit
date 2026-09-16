# xianyu_agent — 闲鱼卖家客服助手（应用模板）

> 元信息：业务形态=意图菜单型客服（每条买家消息从路由根全图重跑：本地规则+LLM 兜底意图分类，条件边分发议价/技术/通用/固定拒绝四个菜单节点）｜图类型=agent｜借用来源=零借用（应用级无借用；复用内置 TimeAugQueryRewriter 类作路由执行器内的零 LLM 时间增强组件；图谱形态是「ROUTE 时代路由模式」的 agent 化表达，messages_builder 迁移范式另见 customer_agent 条目）｜来源=逆向自 apps/xianyu_agent（tmp_xianyu.XianyuReplyBot 迁移）

## 节点清单

| code | 名称 | 用途 | sub_nodes | is_end |
|---|---|---|---|---|
| xy_route_root | 闲鱼路由根节点 | 总入口：时间增强改写 → 本地规则+LLM 兜底意图分类 → 议价轮数控制 → 条件边分发 | xy_menu_price, xy_menu_price_refuse, xy_menu_tech, xy_menu_default | |
| xy_menu_price | 议价 | 砍价/问优惠（未达轮数上限）：按议价策略生成阶梯让利回复 | （无） | ✓ |
| xy_menu_price_refuse | 议价拒绝 | 议价轮数达上限：固定拒绝话术原样返回，零 LLM | （无） | ✓ |
| xy_menu_tech | 技术问答 | 商品功能/用法/参数/故障等技术咨询 | （无） | ✓ |
| xy_menu_default | 通用客服 | 商品介绍/物流/售后常规咨询；no_reply 意图短路为空回复 | （无） | ✓ |

注：四个菜单节点都是终站（执行器不返回 next、无后继即终止）。源码声明
未标 is_end（靠无后继自然终止，引擎行为等价）；模板声明补 is_end=true
把终站语义显式化（结构闸要求非终节点必须有出边），转写时两种写法等
价、任选其一并保持一致。

## 节点交互表

无 graph_state 业务键：本轮路由决策放在 `cxt.nlu_result`（next_node/intent/
slots），议价参数并入 `cxt.filled_slots`，意图回填到**当轮用户消息的
metadata.intent**（议价计数经历史回溯读取），消息文本先经零 LLM 时间增强
（rewritten_queries）。

| 节点 | 读 graph_state | 写 graph_state | 出边与路由 |
|---|---|---|---|
| xy_route_root | cxt.history（逐条读 metadata.intent 计议价轮数）、task_info、rewritten_queries | 当轮用户消息 metadata.intent；filled_slots（bargain_count/max_bargain_rounds/max_discount_percent/max_discount_amount）；nlu_result（next_node/intent/slots） | price 未达上限→xy_menu_price；price 达上限→xy_menu_price_refuse；tech→xy_menu_tech；default/no_reply→xy_menu_default（回复轮 content="" 静默） |
| xy_menu_price | nlu_result.intent、filled_slots 议价参数、task_info/history、rewritten_queries | —（动态温度只改 llm_config 副本） | 终止（无后继；让利回复=本轮用户可见回复，出站前过违禁词过滤） |
| xy_menu_price_refuse | 节点 answer_examples（固定话术本体） | — | 终止（零 LLM 原样返回拒绝话术） |
| xy_menu_tech | nlu_result.intent、task_info/history、rewritten_queries | — | 终止（技术答复=用户可见回复，过违禁词过滤） |
| xy_menu_default | nlu_result.intent、task_info/history、rewritten_queries | — | 终止；no_reply 意图短路：空回复（渠道契约=不发送） |

**循环继承（议价轮次控制，跨轮记忆）**：检查点是 `max_bargain_rounds`
确定性闸门——计数含当轮，达到上限即改道固定拒绝（第 max 次砍价听到的
就是拒绝话术，行为可预测可测试）；经验继承不走状态板，而是**每轮用户
消息上的 metadata.intent**：路由执行器当轮回填、计数时从 cxt.history 逐
条回溯（框架无系统侧议价消息，用户消息计数是同构实现）；议价参数
（bargain_count/max_*）每轮由路由器重算并入 filled_slots，非议价轮也写
bargain_count=0 保证模板键一致。图内无循环（每轮归根重跑）。

## Pattern 声明

```yaml
# nexus-pattern: xianyu_agent
code: xianyu_agent
name: 闲鱼卖家客服助手
description: >-
  对话管理：AGENT 图每轮从路由根全图重跑——本地规则 + LLM 兜底意图分类、
  条件边分发议价/技术/通用菜单、议价轮数控制（达上限走零 LLM 固定拒绝话
  术）与意图级 prompt。
pattern_type: agent
entry_node_code: xy_route_root
nodes:
  - code: xy_route_root
    name: 闲鱼路由根节点
    description: 闲鱼卖家客服总入口，覆盖议价、技术问答与通用咨询三大场景
    task_description: 识别买家消息意图（本地规则 + LLM 兜底），分发到议价/技术/通用菜单节点
    sub_nodes: [xy_menu_price, xy_menu_price_refuse, xy_menu_tech, xy_menu_default]
    plugins:
      loop: xianyu_router
    answer_examples:
      - 您好，在的。关于商品的问题都可以问我哦。
  - code: xy_menu_price
    name: 议价
    description: 买家在砍价/询问优惠，需按议价策略让利但守住底线
    task_description: 命中议价意图（未达轮数上限），生成阶梯让利回复
    sub_nodes: []
    is_end: true
    plugins:
      loop: xianyu_reply
    answer_examples:
      - 亲，价格已经很实惠啦，可以包邮哦。
  - code: xy_menu_price_refuse
    name: 议价拒绝
    description: 议价轮数已达上限，礼貌坚持底价
    task_description: 命中议价意图且轮数达上限，输出固定拒绝话术
    sub_nodes: []
    is_end: true
    plugins:
      loop: xianyu_rule_reply
    answer_examples:
      - 抱歉，这个价格已经是最优惠的了，不能再便宜了哦！
  - code: xy_menu_tech
    name: 技术问答
    description: 买家咨询商品功能、用法、参数、故障等技术问题
    task_description: 命中技术意图，基于商品信息简短作答
    sub_nodes: []
    is_end: true
    plugins:
      loop: xianyu_reply
    answer_examples:
      - 支持蓝牙连接，说明书里有详细教程。
  - code: xy_menu_default
    name: 通用客服
    description: 商品介绍、物流、售后等常规咨询
    task_description: 未命中议价/技术关键词，按通用客服作答
    sub_nodes: []
    is_end: true
    plugins:
      loop: xianyu_reply
    answer_examples:
      - 亲，现货的，拍下后 48 小时内发货。
```

## 插件步骤卡

#### 插件卡：xianyu_router（executor）
- 绑定位置：xy_route_root 节点 node.plugins 的 loop 槽
- 触发时机：每条买家消息进图的首站（每轮从根全图重跑）
- 读（graph_state）：cxt.history（回溯 metadata.intent 计议价轮数）、task_info、metadata.bargain_settings（账户级议价配置注入，缺省用内置默认）
- 处理步骤：①时间增强改写（内置 TimeAugQueryRewriter 实例，零 LLM，直
  接在执行器内跑——AGENT 图无 stages 骨架，原 pattern 级 query 槽搬进
  此处）：买家消息里的相对时间（「明天下午」等）解析为绝对时间标注落
  rewritten_queries；②意图分类两级：本地规则层零 LLM（关键词表+正则，
  技术优先——金额与技术词共现判技术）→未命中走 XIANYU_NLU_PROMPT 单次
  LLM 兜底（四类 price/tech/no_reply/default，no_reply 是防消息轰炸的
  最具体类；调用失败或输出非法标签一律回落 default）；③意图回填当轮用
  户消息 metadata（议价计数依赖）；④议价轮数控制：计数含当轮，达到
  max_bargain_rounds 把 price 意图改道拒绝节点；⑤议价参数并入
  filled_slots、路由决策写 nlu_result（后站与迹消费者读）。
- 写（graph_state）：当轮用户消息 metadata.intent、filled_slots
  （bargain_count/max_bargain_rounds/max_discount_percent/max_discount_
  amount）、nlu_result
- 出边影响：条件边唯一来源——TurnResult(content="", next=<菜单码>)，必
  须落在 sub_nodes 四个菜单内；路由轮不产文本（分支节点才回复）

#### 插件卡：xianyu_reply（executor）
- 绑定位置：xy_menu_price / xy_menu_tech / xy_menu_default 三个菜单节点
  node.plugins 的 loop 槽（同一码复用于三个节点）
- 触发时机：路由条件边落地后的菜单节点执行时
- 读（graph_state）：nlu_result.intent、filled_slots 议价参数、
  task_info/history（经 prompt 模板槽）、rewritten_queries、节点
  base_nlg_prompt（意图级 NLG 模板）
- 处理步骤：三条路径。①no_reply 意图→零 LLM 短路空回复（渠道契约：空
  回复=不发送，防轰炸不防回复）；②其余→组装意图级 prompt（节点模板填
  task_info/history 槽，议价意图追加【议价设置】块含▲当前议价轮次，买
  家消息独立成节）→单次 LLM 调用，llm_config 按**意图级温度策略**改副
  本（price 动态 min(0.3+0.15×轮数, 0.9)、tech 0.4、default 0.7，
  max_tokens 一律 500——永不改写 cxt.llm_config 本体）；③出站安全过滤：
  命中违禁词（微信/QQ/支付宝/银行卡/线下）整条替换为平台安全提醒。
- 写（graph_state）：—（TurnResult.content=过滤后回复）
- 出边影响：终止（菜单节点无后继、不返回 next）；回复即本轮唯一用户可
  见内容；原流式 provider+流式轮路径下按 delta 转发

#### 插件卡：xianyu_rule_reply（executor）
- 绑定位置：xy_menu_price_refuse 节点 node.plugins 的 loop 槽
- 触发时机：议价轮数达上限，路由改道至拒绝节点时
- 读（graph_state）：节点 answer_examples（固定话术本体——本模板中
  answer_examples 双载「回复范式示例」与「确定性脚本体」两种角色）
- 处理步骤：零 LLM——把节点 answer_examples 首个非空条目**原样**作为
  TurnResult.content 返回（ROUTE 时代 FixedNLG 命中标记的 agent 形态消
  费）；无可用条目时告警并返回空回复。议价上限的拒绝必须可预测、可测
  试，故不走模型生成。
- 写（graph_state）：—（TurnResult.content=固定拒绝话术）
- 出边影响：终止（无后继）

## 工具描述卡

（无——本应用零工具授权（pattern 未声明 allow_toolset，各节点无
use_tools）：意图分类、议价控制、话术过滤全部是执行器内确定性代码或单
次 LLM 调用，无外部 API、无文件系统访问。时间增强复用内置
TimeAugQueryRewriter 类实例（执行器内直跑，非注册插件）。）

## 实现注意事项

- **该应用已存在**：`apps/xianyu_agent/`（route.py 全部图谱与执行器 +
  channel.py 渠道接入 + prompts.py 模板），本条目为其结构沉淀，是「路
  由式 agent 图」（旧 ROUTE 模式 agent 化）的参考实现。
- **每轮归根重跑语义**：AGENT 图每条消息从 entry 全图执行，旧 ROUTE「轮
  末归根」无需任何代码即成立；菜单节点必须无后继且执行器不返回 next
  （图在该处终止），路由轮必须静默（content=""）——回复语义=沿路最后
  一个非空 content。
- **跨轮记忆的三种载具分工**（本模板的教学点）：意图计数走用户消息
  metadata（随 history 持久、随会话存活）、本轮参数走 filled_slots（轮
  内传递、模板可引）、路由决策走 nlu_result（后站与迹消费）；不引入
  graph_state 业务键。
- **零 LLM 分支的分诊样板**：固定拒绝话术（行为可预测可测试）走
  answer_examples 直返；no_reply 空回复走渠道不发送契约；时间增强走纯
  代码类——「什么时候不花钱调用模型」与「什么时候花钱」同样需要显式设
  计。
- 意图级 LLM 调参：温度/长度策略按意图改 llm_config **副本**（引擎每节
  点刷新 llm_config，改本体会被下一节点继承造成串味）；旧实现的
  enable_search/top_p 未透传是已知取舍。
- 渠道接入：channel.py 是消息入口（默认回复 API 的插件判定位），环境变
  量 XIANYU_CHANNEL_PATTERN=xianyu_agent 挂接；渠道层契约「空回复=不发
  送」与 no_reply 短路配套。
- 密钥：无专用密钥（LLM 配置走全局 llm_providers）。
- 测试范式：离线路由测试——脚本化 provider，断言规则层/LLM 兜底分级、
  议价轮数改道（含边界第 max 轮）、拒绝话术零 LLM 原样返回、no_reply
  空回复、违禁词替换、意图级温度数值。
