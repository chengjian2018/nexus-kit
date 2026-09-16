# repair_booking_agent — 维修预约外呼助手（应用模板）

> 元信息：业务形态=外呼预约型对话（安装预约的维修变体：开场自报 → 地址核对 → 上门时间协商与守卫校验 → 确认后继续采集故障信息再挂机，回拨/拒绝旁路）｜图类型=fsm｜借用来源=install_booking_agent（机制级子类复用：可约守卫/推荐改写/联系时间裁定/关键词卡控 clarify 全部跨应用继承，只重绑节点码类属性与话术片段，零机制重实现）｜来源=逆向自 apps/repair_booking_agent

## 节点清单

| code | 名称 | 用途 | sub_nodes | is_end |
|---|---|---|---|---|
| repair_greet | 外呼开场 | 接通后自报家门说明来意（客户报修商品需上门维修），确认是否方便接听 | repair_confirm_addr, repair_end, repair_decline | |
| repair_confirm_addr | 地址核对 | 复述订单地址请客户核对；维修场景无到货环节，核对后直达时间协商 | repair_ask_time, repair_end, repair_decline | |
| repair_ask_time | 上门时间协商 | 核心调度节点：按客户口径分发到具体日期/最近/档期推荐/下次联系 | repair_recommend, repair_specific_date, repair_nearest, repair_ask_callback, repair_decline | |
| repair_recommend | 档期推荐 | 客户说不出时间或所给时间不可约时，按真实排班推荐 2-3 个档期 | repair_specific_date, repair_nearest, repair_ask_time, repair_decline | |
| repair_specific_date | 具体日期约定 | 客户给具体日期，可约守卫校验后确认锁定；不可约被守卫改道推荐 | repair_confirm_time, repair_ask_time, repair_decline | |
| repair_nearest | 最近档期安排 | 客户要最近时间，按最近可约档期复述确认 | repair_confirm_time, repair_ask_time, repair_decline | |
| repair_confirm_time | 上门时间确认 | 最终确认节点；确认后不挂机——维修场景转故障信息采集（师傅要带对配件工具） | repair_ask_fault, repair_reschedule, repair_decline | |
| repair_reschedule | 改约重协商 | 确认后反悔：致歉作废原时间，重新进入时间协商 | repair_ask_time, repair_decline | |
| repair_ask_fault | 故障信息询问 | 询问商品具体故障表现（师傅按此带配件工具）；说不清则留本节点继续引导 | repair_confirm_fault, repair_decline | |
| repair_confirm_fault | 故障信息确认 | 复述故障要点请客户确认（报修口径准确），确认后才能挂机 | repair_end | |
| repair_ask_callback | 下次联系时间 | 现在没空/暂不想约时收集下次来电时间（联系时间≠上门时间，不进可约守卫） | repair_end, repair_callback_default, repair_decline | |
| repair_callback_default | 默认改约三天 | 客户给的联系时间太远/过去/未给时，改约默认 3 天后再联系 | repair_end | |
| repair_decline | 通用拒绝承接 | 不想修/已自修/已找别人修/非本人等意图的共情承接（维修场景无质量问题/退货出口） | repair_end | |
| repair_end | 通话结束语 | 所有终止路径的礼貌收尾（故障信息已采集/约好回拨/拒绝/地址不符） | （无） | ✓ |

## 节点交互表

与 install_booking_agent 同构：槽位逐轮累积在 `filled_slots`，排班与订单事实由 launch 层以 task_info 注入，相对时间先经 time_aug_query 改写并携带绝对时间标注，守卫观测写入 `metadata.unified`。维修差异：时间确认后流程继续（故障采集），挂机推迟到故障信息确认之后。

| 节点 | 读 graph_state | 写 graph_state | 出边与路由 |
|---|---|---|---|
| repair_greet | task_info（product_name/user_name/order_id） | filled_slots.service_needed | 需要维修→repair_confirm_addr；拒绝意图→repair_decline；明确不需要（直终）→repair_end |
| repair_confirm_addr | task_info.address | filled_slots.address_confirmed、address（不一致时） | 一致→repair_ask_time（无到货环节）；地址不符终止→repair_end；拒绝→repair_decline |
| repair_ask_time | rewritten_queries（时间标注） | filled_slots.visit_time | 具体日期→repair_specific_date；要最近→repair_nearest；说不出→repair_recommend；现在没空→repair_ask_callback；拒绝→repair_decline |
| repair_recommend | task_info.available_slots（守卫确定性改写回复） | filled_slots.recommended_slots、chosen_slot | 选定日期→repair_specific_date；选最近→repair_nearest；都不合适→repair_ask_time（回环）；拒绝→repair_decline |
| repair_specific_date | rewritten_queries 标注 + available_slots（守卫校验） | filled_slots.visit_date、visit_hour、bookable、matched_slot | 可约→repair_confirm_time；不可约→守卫强制改道 repair_recommend；拒绝→repair_decline |
| repair_nearest | available_slots（守卫校验/按最近档期回填） | filled_slots.visit_time、bookable、matched_slot | 可约→repair_confirm_time；不可约→守卫改道 repair_recommend；拒绝→repair_decline |
| repair_confirm_time | filled_slots.visit_time、task_info.address | filled_slots.visit_time（最终确认） | 确认无误→repair_ask_fault（转故障采集，不收尾）；改约→repair_reschedule；拒绝→repair_decline |
| repair_reschedule | filled_slots.visit_time（作废前记录 prev_visit_time） | filled_slots.rescheduled、prev_visit_time | →repair_ask_time（重新协商）；拒绝→repair_decline |
| repair_ask_fault | task_info.product_name | filled_slots.fault_description、fault_since（可选） | 描述清楚→repair_confirm_fault；说不清→确定性停留（next_node 置空，留在本节点继续引导）；拒绝→repair_decline |
| repair_confirm_fault | filled_slots.fault_description | filled_slots.fault_description（最终确认） | →repair_end（故障信息落定后才收尾） |
| repair_ask_callback | rewritten_queries 标注（守卫裁定：有标注=两周内有效时间） | filled_slots.callback_time、callback_source（customer/default） | 有效时间→repair_end（守卫复述时间收尾）；太远/过去/未给→守卫改道 repair_callback_default；拒绝→repair_decline |
| repair_callback_default | filled_slots.callback_time（承接改道轮写入的默认时间） | — | 应答→repair_end（守卫复述默认时间收尾） |
| repair_decline | — | filled_slots.decline_reason | →repair_end |
| repair_end | filled_slots（收尾复述用） | —（会话终止清理） | 终止（is_end） |

**循环继承（改约循环 confirm_time → reschedule → ask_time → … → confirm_time）**：检查点是可约守卫——每次改约的新时间都重新过 available_slots 确定性匹配；状态继承靠 filled_slots 逐轮覆盖（作废时间记 prev_visit_time），改约轮数无上限，靠自然对话收敛。**推荐回环（ask_time ⇄ recommend ⇄ specific_date/nearest）**：档期唯一事实源是 task_info.available_slots，每轮由守卫重读，推荐回复零 LLM 整体改写，不漂移；改道在 metadata.unified.booking_guard 留观测。**故障采集停留**：客户说不清故障时守卫不涉及、由统一阶段输出空 next_node 停留当前节点（引导继续描述），fault_description 以 filled_slots 继承到确认节点。

## Pattern 声明

```yaml
# nexus-pattern: repair_booking_agent
code: repair_booking_agent
name: 维修预约外呼助手（安装场景变体）
description: >-
  FSM 统一阶段推进维修预约外呼：核对地址、协商师傅上门时间（可约守卫 +
  档期推荐/具体日期/最近三路）、最终确认与改约、确认后继续采集商品故障
  信息再挂机；通用拒绝与下次联系时间补充通道；相对时间先经时间增强改写
  为绝对时间。
pattern_type: fsm
entry_node_code: repair_greet
stages:
  - query: time_aug_query
  - nlu: repair_unified
  - clarify: repair_clarify
  - nlg: nlg_pass_through
nodes:
  - code: repair_greet
    name: 外呼开场
    description: 电话接通后的开场：自报家门（品牌售后客服）、说明来意（客户报修的商品需要安排师傅上门维修）、确认客户方便接听
    task_description: 播报外呼开场白，确认客户是否需要上门维修服务
    slots:
      service_needed: 客户是否需要上门维修服务（是/否）
    sub_nodes: [repair_confirm_addr, repair_end, repair_decline]
  - code: repair_confirm_addr
    name: 地址核对
    description: 复述订单地址，请客户核对是否一致（师傅按此地址上门）；维修场景无到货环节，核对后直达时间协商
    task_description: 核对上门维修地址是否一致，一致则进入时间协商
    slots:
      address_confirmed: 地址是否一致（是/否）
      address: 客户口径的维修地址（不一致时记录）
    sub_nodes: [repair_ask_time, repair_end, repair_decline]
  - code: repair_ask_time
    name: 上门时间协商
    description: 核心调度节点：询问客户希望师傅什么时间上门维修；说不出具体时间则主动推荐档期，给出具体日期则记录，要最近的则走最近档期；现在没空暂时不想约则转下次联系时间
    task_description: 收集客户期望的上门时间，按客户口径分发到推荐/具体日期/最近/下次联系
    slots:
      visit_time: 客户期望的上门时间
    sub_nodes: [repair_recommend, repair_specific_date, repair_nearest, repair_ask_callback, repair_decline]
  - code: repair_recommend
    name: 档期推荐
    description: 客户说不出时间或所给时间不可约时，按师傅排班（任务信息 available_slots）主动推荐可约档期，客户选定后进入对应节点
    task_description: 给出2-3个可约档期供客户选择，等待客户挑选
    slots:
      recommended_slots: 已推荐的档期列表
      chosen_slot: 客户选定的推荐档期
    sub_nodes: [repair_specific_date, repair_nearest, repair_ask_time, repair_decline]
  - code: repair_specific_date
    name: 具体日期约定
    description: 客户给出具体日期/时间，复述确认并锁定师傅上门时间；所给时间是否可约由统一阶段后的可约守卫校验（不可约会被改道到档期推荐）
    task_description: 记录具体日期与时间，可约则确认锁定，不可约守卫改道推荐
    slots:
      visit_date: 上门日期
      visit_hour: 上门时间（几点/时段）
    sub_nodes: [repair_confirm_time, repair_ask_time, repair_decline]
  - code: repair_nearest
    name: 最近档期安排
    description: 客户要最近的上门时间，按最近可约档期复述确认
    task_description: 给出最近可约时间并确认，客户不同意则回环重新协商
    slots:
      visit_time: 最近可约的上门时间
    sub_nodes: [repair_confirm_time, repair_ask_time, repair_decline]
  - code: repair_confirm_time
    name: 上门时间确认
    description: 最终确认节点：复述锁定的上门时间与地址；确认无误后不是收尾——维修场景还需继续采集故障信息（师傅要带对配件工具）；客户此时改约则转入改约节点重新协商
    task_description: 复述上门时间等待客户最终确认，确认后转故障信息采集，改约则重新协商
    slots:
      visit_time: 最终确认的上门时间
      address: 上门维修地址
    sub_nodes: [repair_ask_fault, repair_reschedule, repair_decline]
  - code: repair_reschedule
    name: 改约重协商
    description: 客户在时间确认后要求改期：致歉并作废原时间，重新进入上门时间协商（新时间会重新过可约守卫）
    task_description: 确认改约意向后重新协商上门时间
    slots:
      rescheduled: 是否发生改约（是）
      prev_visit_time: 改约前的原上门时间
    sub_nodes: [repair_ask_time, repair_decline]
  - code: repair_ask_fault
    name: 故障信息询问
    description: 维修场景的收尾前置环节：上门时间确认后，询问客户商品的具体故障情况（不制冷/异响/门关不严/部件损坏等），师傅按此带对配件工具；客户说不清时留在本节点继续引导描述现象
    task_description: 询问维修商品的具体故障表现，收集 fault_description 槽位
    slots:
      fault_description: 客户描述的商品故障现象
      fault_since: 故障出现的大致时间（可选）
    sub_nodes: [repair_confirm_fault, repair_decline]
  - code: repair_confirm_fault
    name: 故障信息确认
    description: 复述采集到的故障要点请客户确认（保证报修口径准确），确认后进入通话结束——获得故障信息后才挂机
    task_description: 复述故障要点等待客户确认，确认后礼貌挂机
    slots:
      fault_description: 最终确认的故障描述
    sub_nodes: [repair_end]
  - code: repair_ask_callback
    name: 下次联系时间
    description: 客户现在没空或暂时不想预约时，询问并记录下次来电时间（这是联系时间，不是上门时间，不进可约守卫）。答复分支由统一阶段的确定性守卫裁定：两周内有效时间→直接收尾；太远/过去/未给出→改走默认改约三天
    task_description: 收集下次来电联系时间，按答复分支收尾
    slots:
      callback_time: 下次来电联系时间
      callback_source: 时间来源（customer=客户给定 / default=默认3天）
    sub_nodes: [repair_end, repair_callback_default, repair_decline]
  - code: repair_callback_default
    name: 默认改约三天
    description: 客户给的下次联系时间太远（超过两周）、已是过去时间、或未给出时，改约默认 3 天后再联系：播报默认联系时间并征询客户意见，客户应答后进入通话结束
    task_description: 播报默认3天后再联系，等待客户应答后收尾
    slots:
      callback_time: 默认下次来电联系时间（今天+3天）
      callback_source: 时间来源（default=默认3天）
    sub_nodes: [repair_end]
  - code: repair_decline
    name: 通用拒绝承接
    description: 通用退出通道：客户不想维修/已自行修好/已找别人修过/非本人等意图，按场景共情回应，然后转入通话结束（维修场景没有质量问题/退货出口——质量诉求就是维修诉求本身）
    task_description: 识别拒绝意图并共情回应，转通话结束
    slots:
      decline_reason: 拒绝原因（不想维修/已自修/已找别人修/非本人）
    sub_nodes: [repair_end]
  - code: repair_end
    name: 通话结束语
    description: 通话收尾：预约完成且故障信息已采集、约好下次联系、客户拒绝、地址不符等所有终止路径的礼貌收尾
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
  绝对时间标注的形式（只对未来两周内的时间加标注），写回
  rewritten_queries。守卫据此判断「有标注=可解析的有效时间」，无需第二
  套时间解析。
- 写（graph_state）：rewritten_queries（改写文本+标注）
- 出边影响：无（不改路由，只为下游守卫提供确定性时间事实）

#### 插件卡：repair_unified（stage）
- 绑定位置：pattern.stages 的 nlu 槽
- 触发时机：每轮 FSM 管线 NLU 位；时间增强之后
- 读（graph_state）：task_info（available_slots 排班与订单事实）、rewritten_queries（时间标注）、filled_slots、metadata.time_base（测试注入的时间基准）
- 处理步骤：**子类复用实现**——直接继承 install_booking_agent 的统一阶段
  守卫机器（InstallBookingUnifiedNLU），不重实现任何机制，仅重绑类属性：
  节点码（可约目标=repair_specific_date/repair_nearest、推荐节点、回拨节
  点、默认回拨节点、结束节点全部指向 repair_* 码）与确定性话术（师傅→
  维修师傅）。内建统一阶段单次 LLM 调用产出 reply/next_node/slots 之后
  依次执行继承的三道守卫：①可约守卫——转入可约目标时从改写标注提取客
  户所约窗口与排班做包含匹配，可约注入 bookable/matched_slot 放行，不可
  约强制改道档期推荐（非法承诺永不进图）；排班未注入时 opted-out 放行。
  ②推荐改写——任何转入档期推荐的转移，回复由继承的推荐 NLG（子类化话
  术，独立注册为 repair_recommend_nlg 码、由本卡内部调用，不单独绑定）
  从真实排班零 LLM 整体改写。③联系时间裁定——下次联系时间节点上有时间
  标注（两周内未来）放行收尾并复述客户时间，无标注强制改道默认改约三天
  并零 LLM 播报 3 天后提案；默认改约三天节点应答后复述默认时间收尾。
- 写（graph_state）：filled_slots（bookable/matched_slot/requested_time/
  callback_time/callback_source）、nlu_result.next_node（改道）、
  nlg_result（确定性改写回复）、metadata.unified（booking_guard/
  callback_guard 观测）
- 出边影响：可强制 next_node 改道（不可约→推荐、联系时间不可用→默认改
  约），或维持转移并改写回复；客户听到的档期、锁定与收尾话术全部来自守
  卫确定性产出

#### 插件卡：repair_clarify（stage）
- 绑定位置：pattern.stages 的 clarify 槽；且每个节点 node.stages 的
  clarify 槽重复声明同一码（统一阶段「clarify 可入 next_node 合法值」的
  逐节点准入开关）
- 触发时机：统一阶段输出 next_node=clarify（客户问了与预约待办无关的问
  题）时，本轮管线 clarify 位
- 读（graph_state）：用户问题、统一阶段给出的 topic/keywords 槽、维修
  FAQ 关键词表、task_info、cur_node 与 history
- 处理步骤：**子类复用实现**——继承 install 的关键词卡控 clarify（继承
  KeywordClarifyStage），仅重绑三类类属性：FAQ 关键词表（维修问题族：
  收费/保修/响应时长等）、电话话术 kb/fallback 模板、生成失败兜底话术。
  检索文本纯关键词包含匹配 FAQ 表（具体优先，首个命中生效）：命中→kb
  轨，FAQ 答案（task_info 字段预填充）为唯一事实源，单次 LLM 只做「口语
  化转述 + 拉回主线」；未命中→fallback 轨诚实承接。无 mixed 模糊区。LLM
  失败用兜底话术；触发轮跳过节点跳转与槽位合并。
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

（无——与 install_booking_agent 同构：排班/订单事实由 launch 层以
task_info 注入，时间解析、可约匹配、档期推荐全部是确定性纯函数由守卫
直调；无外部 API、无 LLM 之外的注册工具。）

## 实现注意事项

- **该应用已存在**：`apps/repair_booking_agent/`，本条目为其结构沉淀，
  是「子类复用变体」的参考实现：新场景不 fork 代码、不复制守卫，跨应用
  import install 的守卫类（apps 层间 import，分层契约不受影响）子类化后
  只重绑节点码类属性与话术片段。
- 与 install 的三处业务差异（转写模板时对应增删节点）：①无到货子树
  （删 install_check_arrival/ask_eta/time_window/available 四节点，地址
  核对直达时间协商）；②拒绝意图族变化（去质量问题/退货出口，加已自修/
  已找别人修）；③时间确认后不挂机——新增故障采集两节点（询问/确认），
  挂机推迟到故障信息落定。
- **FSM 轮末转移时序陷阱**：所有确定性改写必须搭统一阶段便车；节点级
  NLG 会晚一拍生效并覆盖当轮回复（pitfalls.md 第 5 条）。
- clarify 准入开关：每个节点 node.stages 重复声明 repair_clarify 表达
  「全程可澄清」，漏声明即关闭该节点澄清通道。
- 故障描述说不清时的停留由统一阶段空 next_node 语义承载，无独立守卫；
  fault_description 经 filled_slots 继承到确认节点复述。
- 密钥：无。测试时间基准经 metadata.time_base 注入。
- 测试范式：离线路由测试（脚本化 provider + 注入排班），断言维修版可约
  放行/不可约改道/推荐改写/联系时间三分支/故障采集两拍，仿照
  tests/test_install_booking_agent_route.py 的结构为 repair 建独立测试。
- fork 改名清单：插件码全局唯一——本应用的 repair_unified/repair_clarify/
  repair_recommend_nlg 均为独立注册码，子类化机制时必须换码注册
  （references/template-index.md）。
