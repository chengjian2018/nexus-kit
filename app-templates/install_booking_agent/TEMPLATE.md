# install_booking_agent — 安装预约外呼助手（应用模板）

> 元信息：业务形态=外呼预约型对话（开场自报 → 地址/到货核对 → 上门时间协商与守卫校验 → 最终确认，支持改约/回拨/拒绝旁路）｜图类型=fsm｜借用来源=零借用（本条目是知识库 FSM 范式之源：守卫化统一阶段、确定性改写搭统一阶段便车、两拍收尾、关键词卡控 clarify 均自此沉淀，ppt_generator_agent 条目已机制级借用）｜来源=逆向自 apps/install_booking_agent（手绘 FSM 流程草图转写）

## 节点清单

| code | 名称 | 用途 | sub_nodes | is_end |
|---|---|---|---|---|
| install_greet | 外呼开场 | 接通后自报家门说明来意，确认是否需要上门安装 | install_confirm_addr, install_end, install_decline | |
| install_confirm_addr | 地址核对 | 复述订单收货地址请客户核对，不一致则记录客户口径地址 | install_check_arrival, install_end, install_decline | |
| install_check_arrival | 到货确认 | 确认商品是否已送达（师傅须货到才能上门） | install_ask_time, install_ask_eta, install_decline | |
| install_ask_eta | 到货时间询问 | 未到货时问客户是否知道大概到货时间 | install_time_window, install_available, install_decline | |
| install_time_window | 时间段询问 | 客户知道到货时间后收集方便接收/安装的时间段 | install_ask_time, install_available, install_decline | |
| install_available | 上门方便确认 | 确认客户近期是否方便安排上门；不方便转问下次联系时间 | install_ask_time, install_ask_callback, install_decline | |
| install_ask_time | 上门时间协商 | 核心调度节点：按客户口径分发到具体日期/最近/档期推荐 | install_recommend, install_specific_date, install_nearest, install_decline | |
| install_recommend | 档期推荐 | 客户说不出时间或所给时间不可约时，按真实排班推荐 2-3 个档期 | install_specific_date, install_nearest, install_ask_time, install_decline | |
| install_specific_date | 具体日期约定 | 客户给具体日期，可约守卫校验后确认锁定；不可约被守卫改道推荐 | install_confirm_time, install_ask_time, install_decline | |
| install_nearest | 最近档期安排 | 客户要最近时间，按最近可约档期复述确认 | install_confirm_time, install_ask_time, install_decline | |
| install_confirm_time | 上门时间确认 | 最终确认节点：复述锁定时间与地址；改约则转改约节点 | install_end, install_reschedule, install_decline | |
| install_reschedule | 改约重协商 | 确认后反悔：致歉作废原时间，重新进入时间协商 | install_ask_time, install_decline | |
| install_ask_callback | 下次联系时间 | 现在没空/暂不想约时收集下次来电时间（联系时间≠上门时间，不进可约守卫） | install_end, install_callback_default, install_decline | |
| install_callback_default | 默认改约三天 | 客户给的联系时间太远/过去/未给时，改约默认 3 天后再联系 | install_end | |
| install_decline | 通用拒绝承接 | 不想约/已安装/质量问题/已退货/非本人等意图的共情承接 | install_end | |
| install_end | 通话结束语 | 所有终止路径的礼貌收尾（预约完成/约好回拨/拒绝/地址不符） | （无） | ✓ |

## 节点交互表

FSM 业务状态不走 graph_state 业务键：槽位逐轮累积在 `filled_slots`，排班与订单事实由 launch 层以 task_info 注入，相对时间先经 time_aug_query 改写并携带绝对时间标注（rewritten_queries），守卫观测写入 `metadata.unified`。

| 节点 | 读 graph_state | 写 graph_state | 出边与路由 |
|---|---|---|---|
| install_greet | task_info（product_name/user_name/order_id） | filled_slots.service_needed | 需要安装→install_confirm_addr；拒绝意图→install_decline；明确不需要（直终）→install_end |
| install_confirm_addr | task_info.address | filled_slots.address_confirmed、address（不一致时） | 一致→install_check_arrival；地址不符终止→install_end；拒绝→install_decline |
| install_check_arrival | task_info | filled_slots.arrived | 已到货→install_ask_time；未到货→install_ask_eta；拒绝→install_decline |
| install_ask_eta | — | filled_slots.eta_known、eta | 知道→install_time_window；不知道→install_available；拒绝→install_decline |
| install_time_window | — | filled_slots.time_window | 给出时间段→install_ask_time；近期不便→install_available；拒绝→install_decline |
| install_available | — | filled_slots.available | 方便→install_ask_time；不方便→install_ask_callback；拒绝→install_decline |
| install_ask_time | rewritten_queries（时间标注） | filled_slots.visit_time | 具体日期→install_specific_date；要最近→install_nearest；说不出→install_recommend；拒绝→install_decline |
| install_recommend | task_info.available_slots（守卫确定性改写回复） | filled_slots.recommended_slots、chosen_slot | 选定日期→install_specific_date；选最近→install_nearest；都不合适→install_ask_time（回环）；拒绝→install_decline |
| install_specific_date | rewritten_queries 标注 + available_slots（守卫校验） | filled_slots.visit_date、visit_hour、bookable、matched_slot | 可约→install_confirm_time；不可约→守卫强制改道 install_recommend；拒绝→install_decline |
| install_nearest | available_slots（守卫校验/按最近档期回填） | filled_slots.visit_time、bookable、matched_slot | 可约→install_confirm_time；不可约→守卫改道 install_recommend；拒绝→install_decline |
| install_confirm_time | filled_slots.visit_time、task_info.address | filled_slots.visit_time（最终确认） | 确认无误→install_end；改约→install_reschedule；拒绝→install_decline |
| install_reschedule | filled_slots.visit_time（作废前记录 prev_visit_time） | filled_slots.rescheduled、prev_visit_time | →install_ask_time（重新协商）；拒绝→install_decline |
| install_ask_callback | rewritten_queries 标注（守卫裁定：有标注=两周内有效时间） | filled_slots.callback_time、callback_source（customer/default） | 有效时间→install_end（守卫复述时间收尾）；太远/过去/未给→守卫改道 install_callback_default；拒绝→install_decline |
| install_callback_default | filled_slots.callback_time（承接改道轮写入的默认时间） | — | 应答→install_end（守卫复述默认时间收尾） |
| install_decline | — | filled_slots.decline_reason | →install_end |
| install_end | filled_slots（收尾复述用） | —（会话终止清理） | 终止（is_end） |

**循环继承（改约循环 confirm_time → reschedule → ask_time → … → confirm_time）**：
检查点是可约守卫本身——每次改约重协商出的新时间都必须重新通过
available_slots 确定性匹配，模型无法承诺排班外时间；状态继承靠
filled_slots（visit_date/visit_hour 逐轮覆盖，作废时间记入
prev_visit_time），不依赖对话历史。改约轮数无上限（FSM 无步数预算），
靠自然对话收敛。**推荐回环（ask_time ⇄ recommend ⇄ specific_date/nearest）**：
档期唯一事实源是 task_info.available_slots，每轮由守卫从 task_info 重读，
推荐回复由零 LLM 改写整体生成（不记忆、不漂移）；守卫改道在
metadata.unified.booking_guard 留观测痕迹。无最佳检查点需求（对话无部分
产物）；离线路由测试范式见实现注意事项。

## Pattern 声明

```yaml
# nexus-pattern: install_booking_agent
code: install_booking_agent
name: 安装预约外呼助手（手绘FSM转写）
description: >-
  FSM 统一阶段推进安装预约外呼：核对地址、确认到货、协商师傅上门时间
  （可约守卫 + 档期推荐/具体日期/最近三路）、最终确认与改约；通用拒绝
  与下次联系时间补充通道；相对时间先经时间增强改写为绝对时间。
pattern_type: fsm
entry_node_code: install_greet
stages:
  - query: time_aug_query
  - nlu: install_unified
  - clarify: install_clarify
  - nlg: nlg_pass_through
nodes:
  - code: install_greet
    name: 外呼开场
    description: 电话接通后的开场：自报家门（品牌售后客服）、说明来意（客户购买的商品需要上门安装）、确认客户方便接听
    task_description: 播报外呼开场白，确认客户是否需要上门安装服务
    slots:
      service_needed: 客户是否需要上门安装服务（是/否）
    sub_nodes: [install_confirm_addr, install_end, install_decline]
  - code: install_confirm_addr
    name: 地址核对
    description: 复述订单收货地址，请客户核对是否一致（师傅按此地址上门）
    task_description: 核对上门安装地址是否一致，一致则进入到货确认
    slots:
      address_confirmed: 地址是否一致（是/否）
      address: 客户口径的安装地址（不一致时记录）
    sub_nodes: [install_check_arrival, install_end, install_decline]
  - code: install_check_arrival
    name: 到货确认
    description: 确认商品是否已经送达客户地址（师傅需货到后才能上门安装）
    task_description: 询问商品是否已到货，已到货直接约时间，未到货先问物流
    slots:
      arrived: 商品是否已到货（是/否）
    sub_nodes: [install_ask_time, install_ask_eta, install_decline]
  - code: install_ask_eta
    name: 到货时间询问
    description: 未到货时询问客户是否知道大概的到货时间
    task_description: 询问是否知道到货时间，知道则请客户给个方便的时间段
    slots:
      eta_known: 客户是否知道到货时间（是/否）
      eta: 客户知道的到货时间
    sub_nodes: [install_time_window, install_available, install_decline]
  - code: install_time_window
    name: 时间段询问
    description: 客户知道到货时间后，请客户讲一个方便接收/安装的时间段
    task_description: 收集客户方便上门安装的时间段
    slots:
      time_window: 客户提供的方便时间段
    sub_nodes: [install_ask_time, install_available, install_decline]
  - code: install_available
    name: 上门方便确认
    description: 确认客户近期是否方便安排师傅上门安装
    task_description: 询问客户是否方便上门，方便则进入时间协商
    slots:
      available: 客户是否方便上门（是/否）
    sub_nodes: [install_ask_time, install_ask_callback, install_decline]
  - code: install_ask_time
    name: 上门时间协商
    description: 核心调度节点：询问客户希望师傅什么时间上门；说不出具体时间则主动推荐档期，给出具体日期则记录，要最近的则走最近档期
    task_description: 收集客户期望的上门时间，按客户口径分发到推荐/具体日期/最近
    slots:
      visit_time: 客户期望的上门时间
    sub_nodes: [install_recommend, install_specific_date, install_nearest, install_decline]
  - code: install_recommend
    name: 档期推荐
    description: 客户说不出时间或所给时间不可约时，按师傅排班（任务信息 available_slots）主动推荐可约档期，客户选定后进入对应节点
    task_description: 给出2-3个可约档期供客户选择，等待客户挑选
    slots:
      recommended_slots: 已推荐的档期列表
      chosen_slot: 客户选定的推荐档期
    sub_nodes: [install_specific_date, install_nearest, install_ask_time, install_decline]
  - code: install_specific_date
    name: 具体日期约定
    description: 客户给出具体日期/时间，复述确认并锁定师傅上门时间；所给时间是否可约由统一阶段后的可约守卫校验（不可约会被改道到档期推荐）
    task_description: 记录具体日期与时间，可约则确认锁定，不可约守卫改道推荐
    slots:
      visit_date: 上门日期
      visit_hour: 上门时间（几点/时段）
    sub_nodes: [install_confirm_time, install_ask_time, install_decline]
  - code: install_nearest
    name: 最近档期安排
    description: 客户要最近的上门时间，按最近可约档期复述确认
    task_description: 给出最近可约时间并确认，客户不同意则回环重新协商
    slots:
      visit_time: 最近可约的上门时间
    sub_nodes: [install_confirm_time, install_ask_time, install_decline]
  - code: install_confirm_time
    name: 上门时间确认
    description: 最终确认节点：复述锁定的上门时间与地址，确认无误后收尾；客户此时改约则转入改约节点重新协商
    task_description: 复述上门时间等待客户最终确认，改约则重新协商
    slots:
      visit_time: 最终确认的上门时间
      address: 上门安装地址
    sub_nodes: [install_end, install_reschedule, install_decline]
  - code: install_reschedule
    name: 改约重协商
    description: 客户在时间确认后要求改期：致歉并作废原时间，重新进入上门时间协商（新时间会重新过可约守卫）
    task_description: 确认改约意向后重新协商上门时间
    slots:
      rescheduled: 是否发生改约（是）
      prev_visit_time: 改约前的原上门时间
    sub_nodes: [install_ask_time, install_decline]
  - code: install_ask_callback
    name: 下次联系时间
    description: 客户现在没空或暂时不想预约时，询问并记录下次来电时间（这是联系时间，不是上门时间，不进可约守卫）。答复分支由统一阶段的确定性守卫裁定：两周内有效时间→直接收尾；太远/过去/未给出→改走默认改约三天
    task_description: 收集下次来电联系时间，按答复分支收尾
    slots:
      callback_time: 下次来电联系时间
      callback_source: 时间来源（customer=客户给定 / default=默认3天）
    sub_nodes: [install_end, install_callback_default, install_decline]
  - code: install_callback_default
    name: 默认改约三天
    description: 客户给的下次联系时间太远（超过两周）、已是过去时间、或未给出时，改约默认 3 天后再联系：播报默认联系时间并征询客户意见，客户应答后进入通话结束
    task_description: 播报默认3天后再联系，等待客户应答后收尾
    slots:
      callback_time: 默认下次来电联系时间（今天+3天）
      callback_source: 时间来源（default=默认3天）
    sub_nodes: [install_end]
  - code: install_decline
    name: 通用拒绝承接
    description: 通用退出通道：客户不想预约/已安装过/商品有质量问题/已退货/非本人等意图，按场景共情回应，然后转入通话结束
    task_description: 识别拒绝意图并共情回应，转通话结束
    slots:
      decline_reason: 拒绝原因（不想预约/已安装/质量问题/退货/非本人）
    sub_nodes: [install_end]
  - code: install_end
    name: 通话结束语
    description: 通话收尾：预约完成、约好下次联系、客户拒绝、地址不符等所有终止路径的礼貌收尾
    task_description: 礼貌收尾，感谢客户接听，结束通话
    slots: {}
    sub_nodes: []
    is_end: true
```

## 插件步骤卡

#### 插件卡：time_aug_query（stage）
- 绑定位置：pattern.stages 的 query 槽（内置件，atoms/stages/）
- 触发时机：每轮 FSM 管线最前，统一阶段与可约守卫解析时间之前
- 读（graph_state）：用户原始消息
- 处理步骤：内置相对时间增强——把「明天下午3点」等相对时间改写为携带
  绝对时间标注的形式（如「明天下午3点(2026-09-17 15:00)）」，只对未来
  两周内的时间加标注），写回 rewritten_queries。守卫据此判断「有标注=可
  解析的有效时间」，无需第二套时间解析。
- 写（graph_state）：rewritten_queries（改写文本+标注）
- 出边影响：无（不改路由，只为下游守卫提供确定性时间事实）

#### 插件卡：install_unified（stage）
- 绑定位置：pattern.stages 的 nlu 槽
- 触发时机：每轮 FSM 管线 NLU 位；时间增强之后
- 读（graph_state）：task_info（available_slots 排班与订单事实）、rewritten_queries（时间标注）、filled_slots、metadata.time_base（测试注入的时间基准）
- 处理步骤：内建统一阶段（FSMUnifiedNLU 子类）单次 LLM 调用产出
  reply/next_node/slots 之后，叠加三道确定性零 LLM 守卫。①可约守卫：转
  入具体日期/最近两个节点时，从改写标注（退化时从模型回填的
  visit_date/visit_hour/visit_time）提取客户所约时间窗口，与排班做包含
  匹配；可约则在 slots 注入 bookable/matched_slot 放行，不可约则强制
  next_node 改道档期推荐并留 booking_guard 观测——非法承诺永不进图。排
  班未注入时守卫 opted-out 放行（标注 bookable=no_schedule）。②推荐改
  写：任何转入档期推荐的转移（守卫改道或模型自选），回复由零 LLM 的推荐
  NLG（InstallRecommendNLG，独立注册为 install_recommend_nlg 码、由本卡
  内部调用，不单独绑定）从真实排班整体改写，不可约改道时加前缀播报客户
  所给时间；排班缺失时不动回复。③联系时间裁定：在下次联系时间节点上，
  有时间标注（两周内未来）→放行进结束并复述客户时间收尾；无标注（太远/
  过去/含糊/未给）→强制改道默认改约三天、零 LLM 播报「3 天后再联系」
  提案；在默认改约三天节点上客户应答后复述承接轮写入的默认时间收尾。
- 写（graph_state）：filled_slots（bookable/matched_slot/requested_time/
  callback_time/callback_source）、nlu_result.next_node（改道）、
  nlg_result（确定性改写回复）、metadata.unified（booking_guard/
  callback_guard 观测）
- 出边影响：可强制 next_node 改道（不可约→推荐、联系时间不可用→默认改
  约），或维持转移并改写回复；客户听到的档期、锁定与收尾话术全部来自守
  卫确定性产出

#### 插件卡：install_clarify（stage）
- 绑定位置：pattern.stages 的 clarify 槽；且每个节点 node.stages 的
  clarify 槽重复声明同一码（该声明是统一阶段「clarify 可入 next_node 合
  法值」的逐节点准入开关）
- 触发时机：统一阶段输出 next_node=clarify（客户问了与预约待办无关的问
  题）时，本轮管线 clarify 位
- 读（graph_state）：用户问题、统一阶段给出的 topic/keywords 槽、FAQ 关
  键词表、task_info、cur_node 与 history
- 处理步骤：关键词卡控双轨澄清——把用户问题+主题+关键词拼成检索文本，
  纯关键词包含匹配 FAQ 表（条目按具体优先排序，首个命中生效）：命中→
  kb 轨，把 FAQ 答案（task_info 字段预填充）作为唯一事实源注入电话话术
  模板，单次 LLM 只做「口语化转述 + 拉回主线」；未命中→fallback 轨，
  诚实承接并告知稍后核实，拉回主线。无 mixed 模糊区（关键词匹配二值）。
  LLM 生成失败时用兜底话术。触发轮元数据记 clarify 状态，下游跳过节点
  跳转与槽位合并。
- 写（graph_state）：metadata.clarify（triggered/mode/recall_results）、
  nlg_result（澄清回复，本轮唯一 NLG 产出）
- 出边影响：不改路由（触发轮停留在当前节点），回复被整体替换为澄清话术

#### 插件卡：nlg_pass_through（stage）
- 绑定位置：pattern.stages 的 nlg 槽（内置件，atoms/stages/unified.py）
- 触发时机：每轮管线 NLG 位
- 读（graph_state）：—
- 处理步骤：内置占位 NLG——沿用统一阶段或守卫已写入的 nlg_result，跳过
  第二次 LLM 生成（守卫改写优先）。
- 写（graph_state）：—
- 出边影响：无（不改路由）

## 工具描述卡

（无——排班/订单事实由 launch 层以 task_info 注入，时间解析、可约匹
配、档期推荐全部是应用内确定性纯函数，由统一阶段守卫直调；本应用无外
部 API、无 LLM 之外的注册工具。）

## 实现注意事项

- **该应用已存在**：`apps/install_booking_agent/`，本条目为其结构沉淀，
  是知识库「外呼预约型 FSM」的参考实现。
- **FSM 轮末转移时序陷阱**：所有确定性改写（推荐改写、联系时间收尾）必
  须搭统一阶段（install_unified）便车；节点级 NLG 会晚一拍生效并覆盖当
  轮回复（详见 nexus-app-template-skill/references/pitfalls.md 第 5 条）。
- 守卫机制与节点码解耦：可约/推荐/联系时间守卫以类属性持有节点码与话
  术片段，同类场景（如维修预约）子类化统一阶段并只改绑定码与措辞，不改
  守卫机器（repair_booking_agent 条目即此模式）。
- clarify 准入开关：统一阶段仅当当前节点声明了 clarify 槽位才允许
  clarify 进 next_node 合法值——本应用在**每个节点**的 node.stages 重复
  声明 install_clarify 表达「全程可澄清」；漏声明某节点即关闭该节点的澄
  清通道。
- 时间语义：相对时间解析依赖 time_aug_query 的两周标注窗口；「有标注」
  即「可解析的有效时间」是联系时间裁定的唯一判据，不引入第二套解析层。
  可约匹配是包含匹配（所约窗口须完整落入一个排班窗口），点时间落在窗口
  内即可。
- 排班形态容错：available_slots 允许字符串列表 / JSON 数组字符串 / 分隔
  符串三种形态（渠道模型差异），坏条目跳过不阻塞对话；排班整体缺失时守
  卫 opted-out 并如实标注，不伪造档期。
- 密钥：无（无外部 API）。测试时间基准经 metadata.time_base 注入，测试
  不依赖真实时钟。
- 测试范式：离线路由测试——脚本化 LLM provider + 注入 task_info 排班，
  断言可约放行/不可约改道/推荐改写为真实档期/联系时间三分支/FAQ 命中与
  兜底，照 tests/test_install_booking_agent_route.py。
- fork 改名清单：插件码全局唯一，子类化守卫时须换统一阶段/clarify 的注
  册码与 stages 骨架引用（references/template-index.md）。
