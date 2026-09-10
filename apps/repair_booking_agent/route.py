"""repair_booking_agent pattern — the install-booking outbound-call FSM
adapted to a REPAIR scenario (上门维修预约外呼).

Business background: the customer's furniture / appliance is broken; the
service desk calls THEM to book the technician's visit (上门维修服务).
Same outbound semantics as install_booking_agent (the assistant is always
the caller), with three deliberate differences:

1. NO arrival subtree — the customer already owns the item (到货确认/
   到货时间询问/时间段询问/上门方便确认 are all gone): after the address
   check the flow goes straight into visit-time negotiation;
2. decline intents drop the quality-complaint / return exits (商品质量
   问题 IS the repair reason here, 退货 is out of scope) and add the
   repair-specific ones (已自行修好 / 已找别人修过);
3. after the visit time is CONFIRMED the call does not close — the
   assistant keeps asking for the item's fault information (师傅要带对
   配件工具), records it, and only then hangs up.

Flow (14 nodes, single FSM module):

    repair_greet          外呼开场          Yes→地址核对 / No→结束
    repair_confirm_addr   地址核对          一致→时间协商 / 否→结束
    repair_ask_time       上门时间协商      具体日期→repair_specific_date /
                                              最近→repair_nearest /
                                              都不知道→推荐 /
                                              现在没空→下次联系时间
    repair_recommend      档期推荐          客户选定→具体日期/最近 / 回环再协商
    repair_specific_date  具体日期约定      可约→时间确认（守卫校验）/ 不可约守卫改道推荐
    repair_nearest        最近档期安排      可约→时间确认 / 不可约守卫改道推荐
    repair_confirm_time   上门时间确认      客户确认→故障信息询问 / 改约→改约重协商
    repair_reschedule     改约重协商        重新进入时间协商
    repair_ask_fault      故障信息询问      描述故障→故障信息确认 / 说不清→留待继续问
    repair_confirm_fault  故障信息确认      复述故障要点→通话结束
    repair_ask_callback   下次联系时间      客户时间→结束 / 不可用→默认改约三天
    repair_callback_default 默认改约三天    客户应答→通话结束
    repair_decline        通用拒绝承接      共情回应→通话结束
    repair_end            通话结束语        is_end（获得故障信息后礼貌挂机）

task_info contract (injected by the launch layer): same shape as the
install app — product_name / address / user_name / order_id / available_slots
(the technician's bookable windows). The booking-time guard, schedule-backed
recommend rewrite and callback triage all ride the install machinery
(rebound class attributes — see apps/repair_booking_agent/stages.py).

Wiring (declarative builtin codes; app-local stage codes live in
stages.py, registered module-level and imported by route.py's bottom
import):

    pattern.stages      [{"query": "time_aug_query"}, {"nlu": None}, {"clarify": None}, {"nlg": None}]
    module.stages       {"nlu": "repair_unified", "clarify": "repair_clarify", "nlg": "nlg_pass_through"}

Known deliberate simplifications (beyond the install app's):
    - 故障信息只做口径采集（fault_description 槽位），不接诊断知识库；
      客户说不清时留在故障信息询问节点继续引导，不设 clarify 分支。
    - 无商品质量类拒绝出口（质量诉求就是维修诉求本身）。

Registration: module-level ``registry.register(Pattern(...))``, auto-discovered
by AST scan (apps/repair_booking_agent/route.py).
"""

import logging

from nexus.model.module import FSMModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry
from apps.repair_booking_agent.prompts import REPAIR_UNIFIED_PROMPT

logger = logging.getLogger(__name__)


# ============================================================================
# Nodes — in flow order (module_nodes[0] is the entry)
# ============================================================================

repair_greet = BaseNode(
    node_code="repair_greet",
    node_name="外呼开场",
    node_description=(
        "电话接通后的开场：自报家门（品牌售后客服）、说明来意（客户报修的"
        "商品需要安排师傅上门维修）、确认客户方便接听"
    ),
    node_todo_description="播报外呼开场白，确认客户是否需要上门维修服务",
    node_slots={
        "service_needed": "客户是否需要上门维修服务（是/否）",
    },
    sub_nodes=["repair_confirm_addr", "repair_end", "repair_decline"],
    answer_examples=[
        "您好，请问是{user_name}先生/女士吗？我是{product_name}品牌售后客服，"
        "看到您的报修记录，给您安排师傅上门维修，现在方便聊两句吗？",
        "您好打扰了，这里是{product_name}售后服务中心，给您来电是想帮您"
        "约师傅上门检修，您这边方便吗？",
    ],
)

repair_confirm_addr = BaseNode(
    node_code="repair_confirm_addr",
    node_name="地址核对",
    node_description="复述订单地址，请客户核对是否一致（师傅按此地址上门）",
    node_todo_description="核对上门维修地址是否一致，一致则进入时间协商",
    node_slots={
        "address_confirmed": "地址是否一致（是/否）",
        "address": "客户口径的维修地址（不一致时记录）",
    },
    # 维修场景无到货环节：地址一致直接进入时间协商
    sub_nodes=["repair_ask_time", "repair_end", "repair_decline"],
    answer_examples=[
        "先跟您核对一下地址：师傅上门是到{address}，对吗？",
        "麻烦确认下，维修地址是{address}这一处吧？",
    ],
)

repair_ask_time = BaseNode(
    node_code="repair_ask_time",
    node_name="上门时间协商",
    node_description=(
        "核心调度节点：询问客户希望师傅什么时间上门维修；说不出具体时间则"
        "主动推荐档期，给出具体日期则记录，要最近的则走最近档期；"
        "现在没空暂时不想约则转下次联系时间"
    ),
    node_todo_description="收集客户期望的上门时间，按客户口径分发到推荐/具体日期/最近/下次联系",
    node_slots={
        "visit_time": "客户期望的上门时间",
    },
    sub_nodes=["repair_recommend", "repair_specific_date", "repair_nearest",
               "repair_ask_callback", "repair_decline"],
    answer_examples=[
        "请问您希望师傅什么时间上门维修呢？方便给个大概日期或时间段吗？",
        "您哪天在家方便？我可以帮您查最近的维修档期～",
    ],
)

repair_recommend = BaseNode(
    node_code="repair_recommend",
    node_name="档期推荐",
    node_description=(
        "客户说不出时间或所给时间不可约时，按师傅排班（任务信息的"
        "available_slots）主动推荐可约档期，客户选定后进入对应节点"
    ),
    node_todo_description="给出2-3个可约档期供客户选择，等待客户挑选",
    node_slots={
        "recommended_slots": "已推荐的档期列表",
        "chosen_slot": "客户选定的推荐档期",
    },
    sub_nodes=["repair_specific_date", "repair_nearest", "repair_ask_time",
               "repair_decline"],
    answer_examples=[
        "要不这样，最近可以约明天上午10点或后天下午2-4点，您看哪个合适？",
        "我这边推荐周末上午的档口，师傅上门检修也从容些，您觉得呢？",
    ],
)

repair_specific_date = BaseNode(
    node_code="repair_specific_date",
    node_name="具体日期约定",
    node_description=(
        "客户给出具体日期/时间，复述确认并锁定师傅上门时间；所给时间是否"
        "可约由统一阶段后的可约守卫校验（不可约会被改道到档期推荐）"
    ),
    node_todo_description="记录具体日期与时间，可约则确认锁定，不可约守卫改道推荐",
    node_slots={
        "visit_date": "上门日期",
        "visit_hour": "上门时间（几点/时段）",
    },
    sub_nodes=["repair_confirm_time", "repair_ask_time", "repair_decline"],
    answer_examples=[
        "好的，那就给您约在{visit_date}{visit_hour}，师傅到时会提前联系您～",
        "收到～{visit_date}这个时间可以安排，我帮您登记上了。",
    ],
)

repair_nearest = BaseNode(
    node_code="repair_nearest",
    node_name="最近档期安排",
    node_description="客户要最近的上门时间，按最近可约档期复述确认",
    node_todo_description="给出最近可约时间并确认，客户不同意则回环重新协商",
    node_slots={
        "visit_time": "最近可约的上门时间",
    },
    sub_nodes=["repair_confirm_time", "repair_ask_time", "repair_decline"],
    answer_examples=[
        "最快可以安排最近的档期上门检修，时间临近师傅会提前联系您～",
        "帮您插了最近的维修档期，您留意下师傅的电话哦。",
    ],
)

repair_confirm_time = BaseNode(
    node_code="repair_confirm_time",
    node_name="上门时间确认",
    node_description=(
        "最终确认节点：复述锁定的上门时间与地址；确认无误后不是收尾——"
        "维修场景还需继续采集故障信息（师傅要带对配件工具）；"
        "客户此时改约则转入改约节点重新协商"
    ),
    node_todo_description="复述上门时间等待客户最终确认，确认后转故障信息采集，改约则重新协商",
    node_slots={
        "visit_time": "最终确认的上门时间",
        "address": "上门维修地址",
    },
    # 维修场景关键差异：确认后 → 故障信息询问（不直接结束）
    sub_nodes=["repair_ask_fault", "repair_reschedule", "repair_decline"],
    answer_examples=[
        "跟您最后确认下：{visit_time}师傅到{address}上门维修，没问题吧？",
        "那就定啦——{visit_time}上门，地址{address}，辛苦您届时在家等一下～",
    ],
)

repair_reschedule = BaseNode(
    node_code="repair_reschedule",
    node_name="改约重协商",
    node_description=(
        "客户在时间确认后要求改期：致歉并作废原时间，重新进入上门时间"
        "协商（新时间会重新过可约守卫）"
    ),
    node_todo_description="确认改约意向后重新协商上门时间",
    node_slots={
        "rescheduled": "是否发生改约（是）",
        "prev_visit_time": "改约前的原上门时间",
    },
    sub_nodes=["repair_ask_time", "repair_decline"],
    answer_examples=[
        "没问题，改期很方便～那我们重新约一下，您什么时间方便？",
        "好的好的，原来的时间帮您取消，您看约到什么时候合适？",
    ],
)

repair_ask_fault = BaseNode(
    node_code="repair_ask_fault",
    node_name="故障信息询问",
    node_description=(
        "维修场景的收尾前置环节：上门时间确认后，询问客户商品的具体故障"
        "情况（不制冷/异响/门关不严/部件损坏等），师傅按此带对配件工具；"
        "客户说不清时留在本节点继续引导描述现象"
    ),
    node_todo_description="询问维修商品的具体故障表现，收集 fault_description 槽位",
    node_slots={
        "fault_description": "客户描述的商品故障现象",
        "fault_since": "故障出现的大致时间（可选）",
    },
    sub_nodes=["repair_confirm_fault", "repair_decline"],
    answer_examples=[
        "好的～那顺便跟您确认下，您的{product_name}具体是什么问题呢？"
        "比如哪里响、不制冷还是门关不严？",
        "为了师傅上门带对配件，您跟我说说{product_name}的故障现象呗～",
    ],
)

repair_confirm_fault = BaseNode(
    node_code="repair_confirm_fault",
    node_name="故障信息确认",
    node_description=(
        "复述采集到的故障要点请客户确认（保证报修口径准确），确认后进入"
        "通话结束——获得故障信息后才挂机（两拍收尾，与通用拒绝承接同构）"
    ),
    node_todo_description="复述故障要点等待客户确认，确认后礼貌挂机",
    node_slots={
        "fault_description": "最终确认的故障描述",
    },
    sub_nodes=["repair_end"],
    answer_examples=[
        "好的，您的{product_name}是{fault_description}对吧，我记录好了，"
        "师傅上门会带好相应配件工具～",
        "明白啦，{fault_description}这个情况我帮您登记了，师傅会提前准备，"
        "您放心～",
    ],
)

repair_ask_callback = BaseNode(
    node_code="repair_ask_callback",
    node_name="下次联系时间",
    node_description=(
        "客户现在没空或暂时不想预约时，询问并记录下次来电时间（这是联系"
        "时间，不是上门时间，不进可约守卫）。答复三分支由统一阶段的确定"
        "性守卫裁定：时间合适（未来两周内）→直接进通话结束播报客户时间；"
        "太远（超过两周）/过去/未给出→改走「默认改约三天」节点"
    ),
    node_todo_description="收集下次来电联系时间，按答复分支收尾",
    node_slots={
        "callback_time": "下次来电联系时间",
        "callback_source": "时间来源（customer=客户给定 / default=默认3天）",
    },
    sub_nodes=["repair_end", "repair_callback_default", "repair_decline"],
    answer_examples=[
        "理解理解～那您看我们什么时候再联系您方便？我记一下时间。",
        "好的，那不打扰了，您方便的时候我们什么时候再打给您？",
    ],
)

repair_callback_default = BaseNode(
    node_code="repair_callback_default",
    node_name="默认改约三天",
    node_description=(
        "客户给的下次联系时间太远（超过两周）、已是过去时间、或说都行/"
        "未给出时间时，改约默认 3 天后再联系：播报默认联系时间并征询"
        "客户意见，客户应答后进入通话结束（两拍收尾；分支裁定与默认时间"
        "由统一阶段的确定性守卫给出）"
    ),
    node_todo_description="播报默认3天后再联系，等待客户应答后收尾",
    node_slots={
        "callback_time": "默认下次来电联系时间（今天+3天）",
        "callback_source": "时间来源（default=默认3天）",
    },
    sub_nodes=["repair_end"],
    answer_examples=[
        "那我们先约 3 天后左右再给您来电话确认，您看可以吗？",
        "您说的这个时间有点远呢，我们先 3 天后再联系您方便吗？",
    ],
)

repair_decline = BaseNode(
    node_code="repair_decline",
    node_name="通用拒绝承接",
    node_description=(
        "通用退出通道：客户不想维修/已自行修好/已找别人修过/非本人等意图，"
        "按场景共情回应，然后转入通话结束（维修场景没有质量问题/退货"
        "出口——质量诉求就是维修诉求本身）"
    ),
    node_todo_description="识别拒绝意图并共情回应，转通话结束",
    node_slots={
        "decline_reason": "拒绝原因（不想维修/已自修/已找别人修/非本人）",
    },
    sub_nodes=["repair_end"],
    answer_examples=[
        "好的，那就不安排上门维修了～有需要您随时联系我们，感谢接听。",
        "了解，您自己已经修好了就不打扰了，祝您使用愉快～",
        "好的，您已经安排了别的师傅，我们这边就不重复上门了，祝您生活愉快。",
        "不好意思打扰了，那我跟{user_name}先生/女士再确认时间，感谢您的接听。",
    ],
)

repair_end = BaseNode(
    node_code="repair_end",
    node_name="通话结束语",
    node_description=(
        "通话收尾：预约完成且故障信息已采集、约好下次联系、客户拒绝、"
        "地址不符等所有终止路径的礼貌收尾（is_end 终节点，进入即结束会话）"
    ),
    node_todo_description="礼貌收尾，感谢客户接听，结束通话",
    node_slots={},
    sub_nodes=[],
    is_end=True,
    answer_examples=[
        "好的，那就不打扰您了～稍后有需要随时来电，祝您生活愉快，再见～",
        "感谢您的接听与配合，那我们{visit_time}见，师傅会带好配件，祝您使用愉快，再见～",
        "好的，地址问题建议您联系卖家或平台核实哦，感谢接听，再见～",
    ],
)


# ============================================================================
# FSMModule — the whole flow is one module (single business domain)
# ============================================================================

repair_booking = FSMModule(
    module_code="repair_booking",
    module_name="维修预约外呼",
    module_description=(
        "安装预约外呼的维修变体：家具/电器报修客户外呼——外呼开场→地址"
        "核对→上门时间协商（推荐/具体日期/最近三路，可约守卫校验）→时间"
        "确认（支持改约）→故障信息采集与确认→通话结束；无到货环节，通用"
        "拒绝承接与下次联系时间两条补充通道"
    ),
    module_todo_description=(
        "按节点图推进维修预约外呼流程，逐节点收集槽位，最终锁定师傅上门"
        "时间并采集故障信息后挂机"
    ),
    module_nodes=[
        repair_greet,
        repair_confirm_addr,
        repair_ask_time,
        repair_recommend,
        repair_specific_date,
        repair_nearest,
        repair_confirm_time,
        repair_reschedule,
        repair_ask_fault,
        repair_confirm_fault,
        repair_ask_callback,
        repair_callback_default,
        repair_decline,
        repair_end,
    ],
    # App-local unified stage (install 守卫机制子类复用) + keyword clarify +
    # builtin pass-through NLG; clarify slot: the keyword-gated custom
    # clarify stage (repair_clarify) — declaring the slot IS the switch
    stages={
        "nlu": "repair_unified",
        "clarify": "repair_clarify",
        "nlg": "nlg_pass_through",
    },
    # Module-level template override: builtin unified prompt + ### 任务信息
    # section + outbound-call repair persona (greeting / address / time /
    # fault-collection ground in task_basic_info)
    base_nlu_prompt=REPAIR_UNIFIED_PROMPT,
)


# ============================================================================
# Pattern registration — module-level registry.register, auto-discovered
# by AST scan
# ============================================================================

repair_booking_agent_pattern = Pattern(
    code="repair_booking_agent",
    name="维修预约外呼助手（安装场景变体）",
    description=(
        "对话管理：FSM 统一阶段推进维修预约外呼——核对地址、协商师傅上门"
        "时间（可约守卫 + 档期推荐/具体日期/最近三路）、最终确认与改约、"
        "确认后继续采集商品故障信息再挂机；通用拒绝与下次联系时间补充"
        "通道；相对时间先经时间增强改写为绝对时间"
    ),
    entry_module_code="repair_booking",
    modules=[repair_booking],
    stages=[
        # Query rewrite slot: time augmentation (zero LLM) — visit-time
        # negotiation is full of relative times ("明天下午3点方便吗"),
        # resolved into absolute-time annotations before the unified prompt
        # AND before the booking guard parses them
        {"query": "time_aug_query"},
        {"nlu": None},
        # Clarify slot: carried (value None at skeleton level — the module
        # layer installs the keyword-gated custom stage) so the module's
        # clarify declaration has a skeleton slot to bind to
        {"clarify": None},
        {"nlg": None},
    ],
)

registry.register(repair_booking_agent_pattern)


# ============================================================================
# App-local stages import — registers repair_unified / repair_recommend_nlg
# / repair_clarify (kind="stage") at module level. Imported AFTER the
# pattern registration so the pattern object graph (whose stages
# declarations reference the codes) exists even if a stale registry is
# inspected mid-import; the codes resolve at execution time, and
# validation (host startup / CLI) runs after this whole module has been
# imported.
# ============================================================================

import apps.repair_booking_agent.stages  # noqa: E402,F401
