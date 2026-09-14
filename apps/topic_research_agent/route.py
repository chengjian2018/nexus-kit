"""topic_research pattern — the six-node AGENT-graph deep-research recipe
(preplan → plan/分主题 → search fan-out → merge → report → polish).

One AGENT node per pipeline station, adjacency expressed by ``sub_nodes``
— each user message runs the whole pipeline from the entry node, the
stations relaying **within the same turn** via the executor outputs
(``TurnResult.next`` conditional edge / ``TurnResult.sends`` fan-out
dispatch):

    tr_preplan ──next──> tr_plan ──sends──> tr_search ×N ──join──> tr_merge ──next──> tr_report ──next──> tr_polish
     预规划/预检索          分主题规划          一实例一主题           结构化合并           报告草稿生成          格式美化(终态)

- ``pattern code "topic_research"``: a sibling recipe of deep_research —
  where deep_research folds merge+report+polish into one synthesize
  station, this recipe keeps them as three explicit stations (merge is a
  zero-LLM structural fold; report writes the draft; polish owns the
  final streamed wording), demonstrating a longer fan-out pipeline on the
  engine's runtime fan-out;
- step budget: PREPLAN/PLAN/MERGE/REPORT/POLISH are 5 main-loop steps
  (default max_steps=10; the N search worker instances do NOT consume
  graph steps — three-layer guards: ``max_steps`` graph steps × ``max_fanout``
  width (default 8, the pattern caps PLAN's dispatch) × per-branch
  ``_MAX_SEARCH_ROUNDS``);
- merge resolution: tr_search is the only dispatch target and its
  sub_nodes point at exactly one join (tr_merge) — the intersection is
  unique (the engine's merge intersection semantics);
- tr_plan also declares the tr_merge edge — the orphan bail-out (landing
  on tr_plan without in-flight state skips planning and jumps to the
  degraded merge), a legal control-flow edge the runtime's
  undeclared-edge guard must admit;
- tools authorization (deny-by-default three-layer authorization): the pattern grants the
  ``mcp-websearch`` toolset, the tool-carrying nodes narrow via
  ``use_tools`` (only the websearch server's ``web_search_prime``
  retrieval tool is listed; MCP tools register asynchronously after
  startup, so unregistered names validate as deferred, see
  nexus/model/validation.py);
- inter-station state (question / plan / merged findings / draft) travels
  via ``cxt.graph_state["topic_research_state"]``; the N search instances
  see none of it (branch isolation) — their results settle into
  the engine's ``__fanout_results__`` board, which tr_merge (the join)
  folds; the final trace goes to ``cxt.metadata["topic_research"]``;
- the executors live in apps/topic_research_agent/executor.py (plugin
  codes = node codes, bound via each node's ``plugins={"loop": ...}``).
"""

from apps.topic_research_agent.prompts import TOPIC_BASE_PROMPT
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

tr_preplan = BaseNode(
    code="tr_preplan",
    name="主题研究·预规划",
    description=(
        "研究流水线首站:构建研究工作区,模型自行决定是否先检索一轮"
        "补背景,完成后移交主题规划"
    ),
    task_description="为复杂问题准备研究上下文(可选预检索)",
    sub_nodes=["tr_plan"],
    plugins={"loop": "tr_preplan"},
    use_tools=["web_search_prime"],
    base_prompt=TOPIC_BASE_PROMPT,
)

tr_plan = BaseNode(
    code="tr_plan",
    name="主题研究·分主题规划",
    description=(
        "把问题拆分为 3-5 个互相独立的研究主题(JSON 计划;解析失败自纠"
        "重试,仍失败降级为原问题单主题),随后按主题扇出 N 个检索实例"
        "(引擎运行时扇出,宽度受 max_fanout 约束)"
    ),
    task_description="拆分研究主题并派发检索实例",
    # tr_merge: the orphan bail-out edge (landing on tr_plan without
    # in-flight state skips planning and jumps to the degraded merge) — a
    # legal control-flow edge, so it must be declared for the runtime's
    # undeclared-edge guard to admit it (it is also the sends target
    # edge's join sibling: tr_search is the declared dispatch target)
    sub_nodes=["tr_search", "tr_merge"],
    plugins={"loop": "tr_plan"},
    base_prompt=TOPIC_BASE_PROMPT,
)

tr_search = BaseNode(
    code="tr_search",
    name="主题研究·检索(worker)",
    description=(
        "扇出 worker:一个实例负责一个研究主题的带工具 ReAct 检索循环"
        "(私有工作区,每实例独立轮次守卫);结果经引擎结果板交给合并站"
    ),
    task_description="检索单个研究主题收集研究资料",
    # exactly one successor = the join node (the merge intersection over
    # the fan-out targets resolves tr_merge uniquely)
    sub_nodes=["tr_merge"],
    plugins={"loop": "tr_search"},
    use_tools=["web_search_prime"],
    base_prompt=TOPIC_BASE_PROMPT,
)

tr_merge = BaseNode(
    code="tr_merge",
    name="主题研究·合并(join)",
    description=(
        "扇出 join:结构化合并全部检索分支的资料(失败分支降级不阻塞),"
        "统一引用编号,零 LLM 调用;合并产物移交报告站"
    ),
    task_description="合并各主题检索资料",
    sub_nodes=["tr_report"],
    plugins={"loop": "tr_merge"},
    base_prompt=TOPIC_BASE_PROMPT,
)

tr_report = BaseNode(
    code="tr_report",
    name="主题研究·报告生成",
    description=(
        "基于合并资料撰写研究报告草稿(执行摘要/分主题分析/结论与不确定"
        "性/参考来源,引用 [S1] 标记);草稿不直接回复,移交美化站"
    ),
    task_description="撰写研究报告草稿",
    sub_nodes=["tr_polish"],
    plugins={"loop": "tr_report"},
    base_prompt=TOPIC_BASE_PROMPT,
)

tr_polish = BaseNode(
    code="tr_polish",
    name="主题研究·格式美化",
    description=(
        "终态站:流式输出美化后的最终报告(标题层级/重点加粗/来源列表"
        "对齐,不改事实与引用),本回合回复"
    ),
    task_description="美化报告格式并交付",
    sub_nodes=[],
    is_end=True,
    plugins={"loop": "tr_polish"},
    base_prompt=TOPIC_BASE_PROMPT,
)

topic_research_pattern = Pattern(
    code="topic_research",
    name="主题研究助手",
    description=(
        "Topic research 图配方:PREPLAN/PLAN/SEARCH/MERGE/REPORT/POLISH "
        "各为一个 AGENT 节点;PLAN 按主题扇出 N 个 SEARCH 实例并行检索,"
        "MERGE 结构化合并,REPORT 生成草稿,POLISH 流式美化交付"
        "(引擎运行时扇出的长流水线配方)"
    ),
    pattern_type="agent",
    entry_node_code="tr_preplan",
    nodes=[tr_preplan, tr_plan, tr_search, tr_merge, tr_report, tr_polish],
    allow_toolset=["mcp-websearch"],
)

registry.register(topic_research_pattern)

# The station executor plugins register themselves at the bottom of
# apps/topic_research_agent/executor.py (same idiom as deep_research:
# tying the registration action to this pattern's discovery in the same
# file)
import apps.topic_research_agent.executor  # noqa: E402,F401
