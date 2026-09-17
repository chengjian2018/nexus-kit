"""session_reviewer pattern — 会话评审与应用优化，八站 AGENT 图（声明 +
站点执行器；executor.py 承载八个 NodeExecutor，plugin code = node code）。

输入：一条用户消息（"评审会话 <session_id>"）或 launch task_info.session_id；
输出：评审报告（规则指标 + LLM 评审建议）与可选的应用优化（编辑对落盘 +
白名单测试回归 + 失败自修/回滚 + 热加载）。

    sr_route ──> sr_collect ──> sr_metrics ──> sr_review ──> sr_wait_human ──> sr_apply ──┬─> sr_report (is_end)
     提取 id      只读取数+源码    规则指标       LLM评审      wait_human 闸   备份+编辑+测试 │       ↑
                   │(取数失败逃生边)  │(无可评审逃生边)  │(人工拒绝边)      └─> sr_fixloop ──┘
                   └──────────────> sr_report <────────────────────────────── 自修≤2轮,耗尽回滚(自环)

翻译原则（语义归模型、确定性归代码、验收归回执——archify/browser 同款纪律）：
- 语义归模型：sr_review 的质量评审与建议起草、sr_apply/sr_fixloop 的编辑对
  起草（old/new 精确片段，代码负责机械应用与语法兜底）；
- 确定性归代码：session_id 提取（task_info > 正则）、SQLite 只读取数、
  pattern_code → app 目录反查、六项规则指标、审批意图解析（关键词）、
  备份/应用/回滚、白名单 pytest、报告装配（回执驱动，绝不模型散文）、
  热加载（loopback best-effort）；
- 人工闸是确定性站点：sr_wait_human 用引擎原生 wait_human 挂起（browser_agent
  登录墙同款），config 自由袋 auto_approve=true 时跳闸直入实施；下一条用户
  消息作为 resume_input 续跑，意图解析永不交给模型决定；
- 修改边界（deny-by-default 的文件面）：实施环节只允许改目标应用目录下的
  prompts.py / config.yaml / faq.py / slots.py；route.py / tools.py 仅建议；
  写前逐文件字节快照到 data/session_reviewer_agent/<session>/backup_*/，
  回滚=写回快照（不碰 git——用户工作区可能是脏的）；
- 预算（三层独立护栏）：图 max_steps=16 × 自修轮数 fix_rounds（默认 2，
  config 袋可调）× shell 工具超时（guardrails.shell_tool，默认放宽 300s 供
  pytest 回归）；wait_human 挂起/恢复不烧步数（引擎 __step__ 跨挂起记账）。

Node interaction table（状态板 graph_state["session_reviewer_state"]）：

| 站点 | 读 | 写 | 出边 |
|---|---|---|---|
| sr_route | 用户消息 / task_info | session_id, request, workspace；缺 id→本轮提问收束（无路由=终态） | → collect |
| sr_collect | session_id | target{pattern_code,app_dir,可编辑面内容}, messages[], events[], session 行摘要, data_error | → metrics；失败逃生 → report |
| sr_metrics | messages[], events[] | metrics{turns/turn_error/工具/clarify/节点重访…} | → review |
| sr_review | metrics, target 源码, 时间线摘要 | suggestions[](≤12), review_summary, degraded | → wait_human；无可评审逃生 → report |
| sr_wait_human | suggestions；resume_input=下条用户消息 | decision{approved, note}；auto_approve 直通 | 通过→apply；拒绝→report；未识别→再挂起 |
| sr_apply | suggestions, decision, target 文件 | backup_dir(字节快照), edits_log[], applied_files, test_receipt | 测试过/跳过→report；失败→fixloop |
| sr_fixloop | test_receipt, edits_log, backup_dir | fix_history[] +=（防重放：含每轮编辑与结果）, rolled_back | 过→report；轮数未满→自环；耗尽→回滚→report |
| sr_report | 全部回执 | report_path, reload_receipt, cxt.metadata["session_reviewer"]（终迹） | 终态 (is_end) |

回路继承（sr_fixloop ← 第 1..N-1 轮）：fix_history 携带每轮的编辑对与
pytest 结果摘要进入修复 prompt（防重放——第 3 轮不得重提第 1 轮失败的编辑）；
字节快照是唯一回滚源（应用失败的编辑根本不落盘）。
"""

from apps.session_reviewer_agent.prompts import SESSION_REVIEWER_BASE_PROMPT
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

sr_route = BaseNode(
    code="sr_route",
    name="评审·入口提取",
    description=(
        "入口站：从 launch task_info 或用户消息提取待评审 session_id"
        "（task_info.session_id 优先，其次关键词正则），初始化运行状态板；"
        "缺失或歧义时本轮直接提问收束（无路由输出=终态，下条消息从头再来）"
    ),
    task_description="提取 session_id 并初始化评审状态板",
    sub_nodes=["sr_collect"],
    plugins={"loop": "sr_route"},
    base_prompt=SESSION_REVIEWER_BASE_PROMPT,
)

sr_collect = BaseNode(
    code="sr_collect",
    name="评审·确定性取数",
    description=(
        "只读取数站（无 LLM）：SQLite 只读直连会话库取 sessions/messages/"
        "trace_events 三表；config.yaml 绑定优先、route.py 声明正则兜底反查"
        " pattern_code → apps/<dir>；读目标应用可编辑面（prompts.py/config."
        "yaml/faq.py/slots.py）与结构摘要（route.py/tools.py）；全量截断后落"
        " workspace/input.json，状态板只留有界摘要；库缺失/会话不存在走诚实"
        "逃生边直报"
    ),
    task_description="取会话轨迹与应用实现，落盘有界摘要",
    sub_nodes=["sr_metrics", "sr_report"],
    plugins={"loop": "sr_collect"},
    base_prompt=SESSION_REVIEWER_BASE_PROMPT,
)

sr_metrics = BaseNode(
    code="sr_metrics",
    name="评审·规则指标",
    description=(
        "确定性指标站（无 LLM）：从消息与轨迹算六类可计算信号——轮数、"
        "turn_error 计数、工具调用/失败数、clarify 次数、节点重访直方图、"
        "挂起/恢复计数；对载荷形状保持防御性（未知形状记 0 并标注近似）"
    ),
    task_description="从轨迹计算规则指标",
    sub_nodes=["sr_review"],
    plugins={"loop": "sr_metrics"},
    base_prompt=SESSION_REVIEWER_BASE_PROMPT,
)

sr_review = BaseNode(
    code="sr_review",
    name="评审·LLM 评审站",
    description=(
        "一次无工具 LLM 调用（质量敏感，部署可在 config.yaml nodes.sr_review "
        "pin 强模型）：规则指标 + 时间线摘要 + 应用源码进 prompt，按五维 "
        "rubric（routing/slots/tools/reply/prompt）出结构化建议（问题+证据"
        "引用+建议+目标文件+target_kind）；JSON 协议一次自纠重试，仍败则诚实"
        "降级为空建议走逃生边直报"
    ),
    task_description="五维 rubric 评审，产出结构化建议",
    sub_nodes=["sr_wait_human", "sr_report"],
    plugins={"loop": "sr_review"},
    base_prompt=SESSION_REVIEWER_BASE_PROMPT,
)

sr_wait_human = BaseNode(
    code="sr_wait_human",
    name="评审·人工确认闸",
    description=(
        "确定性闸站（无 LLM）：引擎原生 wait_human 挂起在本节点，本轮回复即"
        "建议清单（含证据引用与修改边界提示）；下一条用户消息作为 resume_input"
        "重执行本节点，关键词解析审批意图——通过（可带批注）/拒绝（仅报告）/"
        "未识别（简短重问再挂起）；config 自由袋 auto_approve=true 时免闸直通"
        "（批注记为自动通过）；挂起幂等：闸内无外部副作用"
    ),
    task_description="挂起等待人工审批建议，解析续跑意图",
    sub_nodes=["sr_apply", "sr_report"],
    plugins={"loop": "sr_wait_human"},
    base_prompt=SESSION_REVIEWER_BASE_PROMPT,
)

sr_apply = BaseNode(
    code="sr_apply",
    name="评审·实施优化",
    description=(
        "实施站：逐文件字节快照到 backup_*/ → 按批准建议起草编辑对（LLM，"
        "old_string 逐字契约）→ 确定性应用（未命中即弃，绝不模糊落盘）→ "
        "白名单 pytest 回归（bash 工具，命令仅由 tests/ 目录 glob 拼装）；"
        "无可实施目标或测试通过/跳过 → 汇报；失败 → 修复站"
    ),
    task_description="快照、落编辑、跑白名单测试",
    sub_nodes=["sr_fixloop", "sr_report"],
    plugins={"loop": "sr_apply"},
    use_tools=["bash"],
    base_prompt=SESSION_REVIEWER_BASE_PROMPT,
)

sr_fixloop = BaseNode(
    code="sr_fixloop",
    name="评审·测试修复回路",
    description=(
        "经验继承站：携带 pytest 尾部输出与 fix_history（防重放）修复已改"
        "文件，重跑白名单测试；自修轮数 ≤ fix_rounds（默认 2）；耗尽则从字节"
        "快照回滚全部已改文件并如实标注，绝不把失败写成成功"
    ),
    task_description="按测试失败证据自修，耗尽即回滚",
    sub_nodes=["sr_fixloop", "sr_report"],
    plugins={"loop": "sr_fixloop"},
    use_tools=["bash"],
    base_prompt=SESSION_REVIEWER_BASE_PROMPT,
)

sr_report = BaseNode(
    code="sr_report",
    name="评审·回执报告",
    description=(
        "终点站（无 LLM）：从回执确定性装配报告——规则指标/建议清单/人工"
        "决定/编辑与 unified diff/测试回执/修复与回滚记录，落盘 data/"
        "session_reviewer_agent/<session>/report_*.md，聊天内回摘要；已应用"
        "且未回滚时 best-effort 热加载目标 pattern（loopback POST /api/v1/"
        "reload，失败如实提示手动）；app-templates/<code> 存在时提醒模板已"
        "过期（提醒不改）"
    ),
    task_description="从回执装配报告并尝试热加载",
    sub_nodes=[],
    is_end=True,
    plugins={"loop": "sr_report"},
    base_prompt=SESSION_REVIEWER_BASE_PROMPT,
)

session_reviewer_pattern = Pattern(
    code="session_reviewer",
    name="会话评审与应用优化助手",
    description=(
        "按 session_id 评审 nexus-kit 应用：只读取会话库轨迹+应用实现源码，"
        "规则指标+LLM 五维评审出结构化建议，wait_human 人工闸（auto_approve "
        "可跳过）后对可编辑面（prompts/config/faq/slots）落编辑对，白名单"
        "pytest 回归，失败自修≤2轮、耗尽字节快照回滚，报告落盘并 best-effort "
        "热加载；语义归模型、取数/指标/应用/回滚归代码、验收归回执"
    ),
    pattern_type="agent",
    entry_node_code="sr_route",
    nodes=[
        sr_route, sr_collect, sr_metrics, sr_review,
        sr_wait_human, sr_apply, sr_fixloop, sr_report,
    ],
    # The only tool substrate: whitelisted pytest via bash in the apply/fix
    # stations (data collection and file edits are direct deterministic IO in
    # the executor — no tool layer, no grant widening).
    allow_toolset=["shell"],
    # Worst honest walk ≈ route+collect+metrics+review+gate+apply+fix×2+report
    # ≈ 9-10 steps; 16 leaves headroom alongside fix_rounds (semantic cap) and
    # the shell timeout guard. Suspension/resume does not burn steps (the
    # engine keeps the __step__ budget across wait_human).
    config={"max_steps": 16},
)

registry.register(session_reviewer_pattern)

# The eight station executors self-register at the bottom of
# apps/session_reviewer_agent/executor.py (plugin code = node code); this
# import closes the binding loop (the same convention as archify/browser).
import apps.session_reviewer_agent.executor  # noqa: E402,F401
