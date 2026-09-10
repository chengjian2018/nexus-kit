"""install_booking_agent pattern — a hand-drawn FSM template (photo)
transcribed into a declarative FSM pattern for OUTBOUND install-booking
calls.

Business background: the customer just bought furniture / an appliance;
the service desk calls THEM to book the installer's visit (上门安装服务).
The assistant is always the caller — the opening is a connect-event-driven
greeting (self-introduction + purpose), and the flow walks the sketch:
confirm address → check arrival → negotiate visit time → book → close.

task_info contract (injected by the launch layer):
    - product_name / address / user_name / order_id — order facts the call
      grounds in;
    - available_slots: ["YYYY-MM-DD HH:MM-HH:MM", ...] — the installer's
      bookable windows. When present, the app-local unified stage
      (stages.InstallBookingUnifiedNLU, code ``install_unified``) runs a
      deterministic booking-time guard on every visit-time pick: a
      bookable request is annotated and proceeds; an unbookable one is
      rerouted to install_recommend. Every transition into install_recommend
      gets its reply deterministically rewritten from the schedule
      (stages.InstallRecommendNLG, zero extra LLM).

Source: a hand-drawn dialogue-flow sketch (photo) + the supplemented
scenarios. Transcription mapping (sketch oval → node code → branches):

    开始后（问候+确认服务）       install_greet          Yes→地址核对 / No→结束
    询问地址是XX是否一致          install_confirm_addr   一致→到货确认 / 否→结束
    你是否已到货                 install_check_arrival  是→时间协商 / 否→到货时间
    是否知道到货时间             install_ask_eta        知道→时间段 / 不知道→方便确认
    询问时间段                   install_time_window    提供时间→时间协商 / 不提供→方便确认
    是否方便                     install_available      是→时间协商 / 否→下次联系时间
    询问他什么时间上门           install_ask_time       具体日期→install_specific_date /
                                                       最近→install_nearest /
                                                       都不知道→推荐
    推荐                         install_recommend      客户选定→具体日期/最近 / 回环再协商
    具体日期                     install_specific_date  可约→时间确认（守卫校验）/ 不可约守卫改道推荐
    最近                         install_nearest        可约→时间确认 / 不可约守卫改道推荐
    门时间（确定时间）           install_confirm_time   客户确认→结束 / 改约→时间协商
    结束                         install_end            is_end（通话收尾）

Supplemented nodes (beyond the sketch, per the follow-up requirements):

    install_decline     通用拒绝节点：用户不想预约/已安装/质量问题/退货/
                        非本人等意图，共情回应后转结束（每个业务节点都
                        有指向它的边——草图外的通用退出通道）
    install_ask_callback 下次联系时间：现在没空/不想现在预约时，询问并
                        记录下次来电时间，约好后礼貌收尾
    install_reschedule   改约节点：时间确认后客户反悔改期，重新进入时间
                        协商（保留原时间槽位，允许被新时间覆盖）

The sketch's two terminal ovals (地址不符/客户拒绝结束、预约完成结束) merge
into one ``install_end`` node — both are "polite phone close, hang up"; the
reply paradigm for the closing turn comes from install_end's answer_examples
(the unified stage styles the reply after the CHOSEN next node). The generic
decline intents go through install_decline first (a scenario-specific
empathetic line) and then install_end — two beats, because the decline reply
and the goodbye deserve different wordings.

Wiring (mostly declarative builtin codes; the only app-local stage codes
live in stages.py, registered module-level and imported by route.py's
bottom import):

    pattern.stages      [{"query": "time_aug_query"}, {"nlu": None}, {"nlg": None}]
    module.stages       {"nlu": "install_unified", "nlg": "nlg_pass_through"}

    install_unified = FSMUnifiedNLU + booking-time guard + deterministic
    recommend rewrite. The recommend rewrite lives in the unified stage
    (NOT as install_recommend's node-level nlg): FSM node transitions fire
    end-of-turn, so a node-level nlg would only resolve on the NEXT turn —
    after the transition — and clobber that turn's reply; the same-turn
    rewrite must ride the unified stage that chose the transition.

Known deliberate simplifications:
    - 双轨 clarify 未启用（the sketch has no off-topic branch）: off-topic
      turns are handled by the unified stage's empty next_node (stay +
      re-confirm), not a clarify round.
    - 物流到货信息没有外部系统对接：install_ask_eta 的"是否知道到货时间"
      由客户口径给出；task_info 可注入 logistics_eta 字段增强（预留）。
    - 无人接听/占线/挂断等外呼电信事件不在对话层处理（channel 层职责），
      这里只覆盖接通后的对话流。
    - 改约轮次不设上限：install_reschedule → install_ask_time 的回环由
      max_hops 之外的自然对话收敛（每次改约都会重新过可约守卫）。

Registration: module-level ``registry.register(Pattern(...))``, auto-discovered
by AST scan (apps/install_booking_agent/route.py).
"""

import logging

from nexus.model.module import FSMModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry
from apps.install_booking_agent.prompts import INSTALL_UNIFIED_PROMPT

logger = logging.getLogger(__name__)


# ============================================================================
# Nodes — the sketch's ovals + supplemented scenarios, in flow order
# (module_nodes[0] is the entry)
# ============================================================================

install_greet = BaseNode(
    node_code="install_greet",
    node_name="外呼开场",
    node_description=(
        "电话接通后的开场：自报家门（品牌售后客服）、说明来意（客户购买"
        "的商品需要上门安装）、确认客户方便接听"
    ),
    node_todo_description="播报外呼开场白，确认客户是否需要上门安装服务",
    node_slots={
        "service_needed": "客户是否需要上门安装服务（是/否）",
    },
    sub_nodes=["install_confirm_addr", "install_end", "install_decline"],
    answer_examples=[
        "您好，请问是{user_name}先生/女士吗？我是{product_name}品牌售后客服，"
        "您购买的商品可以安排师傅上门安装，现在方便聊两句吗？",
        "您好打扰了，这里是{product_name}售后服务中心，给您来电是想帮您"
        "预约上门安装，您这边方便吗？",
    ],
)

install_confirm_addr = BaseNode(
    node_code="install_confirm_addr",
    node_name="地址核对",
    node_description="复述订单收货地址，请客户核对是否一致（师傅按此地址上门）",
    node_todo_description="核对上门安装地址是否一致，一致则进入到货确认",
    node_slots={
        "address_confirmed": "地址是否一致（是/否）",
        "address": "客户口径的安装地址（不一致时记录）",
    },
    sub_nodes=["install_check_arrival", "install_end", "install_decline"],
    answer_examples=[
        "先跟您核对一下地址：师傅上门是到{address}，对吗？",
        "麻烦确认下，安装地址是{address}这一处吧？",
    ],
)

install_check_arrival = BaseNode(
    node_code="install_check_arrival",
    node_name="到货确认",
    node_description="确认商品是否已经送达客户地址（师傅需货到后才能上门安装）",
    node_todo_description="询问商品是否已到货，已到货直接约时间，未到货先问物流",
    node_slots={
        "arrived": "商品是否已到货（是/否）",
    },
    sub_nodes=["install_ask_time", "install_ask_eta", "install_decline"],
    answer_examples=[
        "好的～请问您的{product_name}现在已经送到{address}了吗？",
        "商品这边显示近期送达，您那边签收了吗？",
    ],
)

install_ask_eta = BaseNode(
    node_code="install_ask_eta",
    node_name="到货时间询问",
    node_description="未到货时询问客户是否知道大概的到货时间",
    node_todo_description="询问是否知道到货时间，知道则请客户给个方便的时间段",
    node_slots={
        "eta_known": "客户是否知道到货时间（是/否）",
        "eta": "客户知道的到货时间",
    },
    sub_nodes=["install_time_window", "install_available", "install_decline"],
    answer_examples=[
        "还没到也没关系～您知道大概什么时候能送到吗？",
        "您那边有物流的预计送达时间吗？跟我说说就好。",
    ],
)

install_time_window = BaseNode(
    node_code="install_time_window",
    node_name="时间段询问",
    node_description="客户知道到货时间后，请客户讲一个方便接收/安装的时间段",
    node_todo_description="收集客户方便上门安装的时间段",
    node_slots={
        "time_window": "客户提供的方便时间段",
    },
    sub_nodes=["install_ask_time", "install_available", "install_decline"],
    answer_examples=[
        "那您哪个时间段在家方便？我好帮您约师傅～",
        "您说个大概的时间段（比如周末白天），我来协调师傅上门。",
    ],
)

install_available = BaseNode(
    node_code="install_available",
    node_name="上门方便确认",
    node_description="确认客户近期是否方便安排师傅上门安装",
    node_todo_description="询问客户是否方便上门，方便则进入时间协商",
    node_slots={
        "available": "客户是否方便上门（是/否）",
    },
    # 不方便不再直接结束：supplemented scenario —— 现在没空/不想现在预约
    # → 询问下次联系时间
    sub_nodes=["install_ask_time", "install_ask_callback", "install_decline"],
    answer_examples=[
        "了解～那最近方便安排师傅上门安装吗？",
        "您这边近期方便约个时间安装吗？",
    ],
)

install_ask_time = BaseNode(
    node_code="install_ask_time",
    node_name="上门时间协商",
    node_description=(
        "核心调度节点：询问客户希望师傅什么时间上门；说不出具体时间则"
        "主动推荐档期，给出具体日期则记录，要最近的则走最近档期"
    ),
    node_todo_description="收集客户期望的上门时间，按客户口径分发到推荐/具体日期/最近",
    node_slots={
        "visit_time": "客户期望的上门时间",
    },
    sub_nodes=["install_recommend", "install_specific_date", "install_nearest",
               "install_decline"],
    answer_examples=[
        "请问您希望师傅什么时间上门呢？方便给个大概日期或时间段吗？",
        "您哪天在家方便？我可以帮您查最近的安装档期～",
    ],
)

install_recommend = BaseNode(
    node_code="install_recommend",
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
    sub_nodes=["install_specific_date", "install_nearest", "install_ask_time",
               "install_decline"],
    answer_examples=[
        "要不这样，最近可以约明天上午10点或后天下午2-4点，您看哪个合适？",
        "我这边推荐周末上午的档口，师傅上门安装也从容些，您觉得呢？",
    ],
)

install_specific_date = BaseNode(
    node_code="install_specific_date",
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
    sub_nodes=["install_confirm_time", "install_ask_time", "install_decline"],
    answer_examples=[
        "好的，那就给您约在{visit_date}{visit_hour}，师傅到时会提前联系您～",
        "收到～{visit_date}这个时间可以安排，我帮您登记上了。",
    ],
)

install_nearest = BaseNode(
    node_code="install_nearest",
    node_name="最近档期安排",
    node_description="客户要最近的上门时间，按最近可约档期复述确认",
    node_todo_description="给出最近可约时间并确认，客户不同意则回环重新协商",
    node_slots={
        "visit_time": "最近可约的上门时间",
    },
    sub_nodes=["install_confirm_time", "install_ask_time", "install_decline"],
    answer_examples=[
        "最快可以安排最近的档期上门，时间临近师傅会提前联系您～",
        "帮您插了最近的安装档期，您留意下师傅的电话哦。",
    ],
)

install_confirm_time = BaseNode(
    node_code="install_confirm_time",
    node_name="上门时间确认",
    node_description=(
        "最终确认节点：复述锁定的上门时间与地址，确认无误后收尾；"
        "客户此时改约则转入改约节点重新协商"
    ),
    node_todo_description="复述上门时间等待客户最终确认，改约则重新协商",
    node_slots={
        "visit_time": "最终确认的上门时间",
        "address": "上门安装地址",
    },
    # supplemented scenario: 确认后改约 → install_reschedule
    sub_nodes=["install_end", "install_reschedule", "install_decline"],
    answer_examples=[
        "跟您最后确认下：{visit_time}师傅到{address}上门安装，没问题吧？",
        "那就定啦——{visit_time}上门，地址{address}，辛苦您届时在家等一下～",
    ],
)

install_reschedule = BaseNode(
    node_code="install_reschedule",
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
    sub_nodes=["install_ask_time", "install_decline"],
    answer_examples=[
        "没问题，改期很方便～那我们重新约一下，您什么时间方便？",
        "好的好的，原来的时间帮您取消，您看约到什么时候合适？",
    ],
)

install_ask_callback = BaseNode(
    node_code="install_ask_callback",
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
    sub_nodes=["install_end", "install_callback_default", "install_decline"],
    answer_examples=[
        "理解理解～那您看我们什么时候再联系您方便？我记一下时间。",
        "好的，那不打扰了，您方便的时候我们什么时候再打给您？",
    ],
)

install_callback_default = BaseNode(
    node_code="install_callback_default",
    node_name="默认改约三天",
    node_description=(
        "客户给的下次联系时间太远（超过两周）、已是过去时间、或说都行/"
        "未给出时间时，改约默认 3 天后再联系：播报默认联系时间并征询"
        "客户意见，客户应答后进入通话结束（两拍收尾，与通用拒绝承接"
        "同构；分支裁定与默认时间由统一阶段的确定性守卫给出）"
    ),
    node_todo_description="播报默认3天后再联系，等待客户应答后收尾",
    node_slots={
        "callback_time": "默认下次来电联系时间（今天+3天）",
        "callback_source": "时间来源（default=默认3天）",
    },
    sub_nodes=["install_end"],
    answer_examples=[
        "那我们先约 3 天后左右再给您来电话确认，您看可以吗？",
        "您说的这个时间有点远呢，我们先 3 天后再联系您方便吗？",
    ],
)

install_decline = BaseNode(
    node_code="install_decline",
    node_name="通用拒绝承接",
    node_description=(
        "通用退出通道：客户不想预约/已安装过/商品有质量问题/已退货/"
        "非本人等意图，按场景共情回应，然后转入通话结束"
    ),
    node_todo_description="识别拒绝意图并共情回应，转通话结束",
    node_slots={
        "decline_reason": "拒绝原因（不想预约/已安装/质量问题/退货/非本人）",
    },
    sub_nodes=["install_end"],
    answer_examples=[
        "好的，那就不安排上门安装了～有需要您随时联系我们，感谢接听。",
        "了解，既然已经安装好了就不打扰了，祝您使用愉快～",
        "非常抱歉给您带来困扰，质量问题我这边帮您记录反馈，稍后会有"
        "专人联系您处理，请您留意来电。",
        "好的，退货的话安装预约就帮您取消了，祝您生活愉快。",
        "不好意思打扰了，那我跟{user_name}先生/女士再确认时间，感谢您的接听。",
    ],
)

install_end = BaseNode(
    node_code="install_end",
    node_name="通话结束语",
    node_description=(
        "通话收尾：预约完成、约好下次联系、客户拒绝、地址不符等所有"
        "终止路径的礼貌收尾（is_end 终节点，进入即结束会话）"
    ),
    node_todo_description="礼貌收尾，感谢客户接听，结束通话",
    node_slots={},
    sub_nodes=[],
    is_end=True,
    answer_examples=[
        "好的，那就不打扰您了～稍后有需要随时来电，祝您生活愉快，再见～",
        "感谢您的接听与配合，那我们{visit_time}见，祝您使用愉快，再见～",
        "好的，地址问题建议您联系卖家或平台核实哦，感谢接听，再见～",
    ],
)


# ============================================================================
# FSMModule — the whole flow is one module (single business domain)
# ============================================================================

install_booking = FSMModule(
    module_code="install_booking",
    module_name="安装预约外呼",
    module_description=(
        "手绘FSM模板转写（外呼语义）：家具/电器购买客户回访——外呼开场→"
        "地址核对→到货确认→上门时间协商（推荐/具体日期/最近三路，可约"
        "守卫校验）→时间确认（支持改约）→通话结束；附通用拒绝承接与"
        "下次联系时间两条补充通道"
    ),
    module_todo_description=(
        "按节点图推进安装预约外呼流程，逐节点收集槽位，最终锁定师傅上门时间"
    ),
    module_nodes=[
        install_greet,
        install_confirm_addr,
        install_check_arrival,
        install_ask_eta,
        install_time_window,
        install_available,
        install_ask_time,
        install_recommend,
        install_specific_date,
        install_nearest,
        install_confirm_time,
        install_reschedule,
        install_ask_callback,
        install_callback_default,
        install_decline,
        install_end,
    ],
    # App-local unified stage (guarded booking) + builtin pass-through NLG;
    # install_recommend overrides nlg at node level (schedule-backed);
    # clarify slot: the keyword-gated custom clarify stage (install_clarify)
    # — declaring the slot IS the switch, "clarify" enters the unified stage's
    # valid next_node set and the clarify-turn guard skips node jumps
    stages={
        "nlu": "install_unified",
        "clarify": "install_clarify",
        "nlg": "nlg_pass_through",
    },
    # Module-level template override: builtin unified prompt + ### 任务信息
    # section + outbound-call persona (greeting / address / time ground in
    # task_basic_info)
    base_nlu_prompt=INSTALL_UNIFIED_PROMPT,
)


# ============================================================================
# Pattern registration — module-level registry.register, auto-discovered
# by AST scan
# ============================================================================

install_booking_agent_pattern = Pattern(
    code="install_booking_agent",
    name="安装预约外呼助手（手绘FSM转写）",
    description=(
        "对话管理：FSM 统一阶段推进安装预约外呼——核对地址、确认到货、"
        "协商师傅上门时间（可约守卫 + 档期推荐/具体日期/最近三路）、最终"
        "确认与改约；通用拒绝与下次联系时间补充通道；相对时间先经时间"
        "增强改写为绝对时间"
    ),
    entry_module_code="install_booking",
    modules=[install_booking],
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

registry.register(install_booking_agent_pattern)


# ============================================================================
# App-local stages import — registers install_unified / install_recommend_nlg
# (kind="stage") at module level. Imported AFTER the pattern registration so
# the pattern object graph (whose stages declarations reference the codes)
# exists even if a stale registry is inspected mid-import; the codes resolve
# at execution time, and validation (host startup / CLI) runs after this
# whole module has been imported.
# ============================================================================

import apps.install_booking_agent.stages  # noqa: E402,F401
