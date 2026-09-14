"""deep_research pattern — the four-node AGENT-graph deep-research recipe
(two-layer form; the SEARCH station runs on the engine's runtime fan-out —
this app is the fan-out acceptance case).

One AGENT node per research phase, adjacency expressed by ``sub_nodes`` —
each user message runs the whole pipeline from the entry node, the
stations relaying **within the same turn** via the executor outputs
(``TurnResult.next`` conditional edge / ``TurnResult.sends`` fan-out
dispatch):

    dr_preplan ──next──> dr_plan ──sends──> dr_search ×N ──join──> dr_synthesize
     pre-retrieval/init    plan sub-questions  one per sub-question   merge & report

- ``pattern code "deep_research"``: the graph version takes over the code
  the deleted single-module route.py used to register (one deep_research
  registration, the "deep_research_multi" code is gone with it);
- step budget: PREPLAN/PLAN/SYNTHESIZE are 3 main-loop steps (the N search
  worker instances do NOT consume graph steps — the three-layer guards: ``max_steps`` graph steps × ``max_fanout`` width (default 8, the
  pattern caps PLAN's dispatch) × per-branch ``_MAX_SEARCH_ROUNDS``);
- tools authorization (deny-by-default three-layer authorization): the pattern grants the
  two MCP server toolsets (``mcp-websearch`` retrieval / ``mcp-zai``
  vision), the tool-carrying nodes narrow via ``use_tools`` — only the
  websearch server's ``web_search_prime`` retrieval tool is listed today
  (the research prompts drive retrieval only; the zai toolset stays granted
  for future vision-augmented research, no node lists its tools yet —
  MCP tools register asynchronously after startup, so unregistered names
  validate as deferred, see nexus/model/validation.py);
- inter-phase state (question / plan / pre-retrieval findings) travels via
  ``cxt.graph_state["deep_research_state"]``; the N search instances see
  none of it (branch isolation) — their results settle into the
  engine's ``__fanout_results__`` board, which dr_synthesize (the join)
  merges; the final trace still goes to ``cxt.metadata["deep_research"]``
  for observability / next-turn research continuation;
- the executors live in apps/deep_research_agent/executor_multi.py (plugin
  codes = node codes, bound via each node's ``plugins={"loop": ...}``).
"""

from apps.deep_research_agent.prompts import DEEP_RESEARCH_BASE_PROMPT
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

dr_preplan = BaseNode(
    code="dr_preplan",
    name="深度研究·预检索",
    description=(
        "研究流水线首站:构建研究工作区,模型自行决定是否先检索一轮"
        "补背景,完成后移交规划"
    ),
    task_description="为复杂问题准备研究上下文(可选预检索)",
    sub_nodes=["dr_plan"],
    plugins={"loop": "dr_preplan"},
    use_tools=["web_search_prime"],
    base_prompt=DEEP_RESEARCH_BASE_PROMPT,
)

dr_plan = BaseNode(
    code="dr_plan",
    name="深度研究·规划与派发",
    description=(
        "把问题分解为可检索验证的子问题(JSON 计划;解析失败自纠重试,"
        "仍失败降级为原问题单计划),随后按子问题扇出 N 个检索实例"
        "(引擎运行时扇出,宽度受 max_fanout 约束)"
    ),
    task_description="产出研究计划并派发检索实例",
    # dr_synthesize: the orphan bail-out edge (landing on dr_plan without
    # in-flight state skips planning and jumps to the degraded synthesis) —
    # a legal control-flow edge, so it must be declared for the runtime's
    # undeclared-edge guard to admit it (it is also the sends target edge's
    # sibling: dr_search is the declared dispatch target)
    sub_nodes=["dr_search", "dr_synthesize"],
    plugins={"loop": "dr_plan"},
    base_prompt=DEEP_RESEARCH_BASE_PROMPT,
)

dr_search = BaseNode(
    code="dr_search",
    name="深度研究·检索(worker)",
    description=(
        "扇出 worker:一个实例负责一个子问题的带工具 ReAct 检索循环"
        "(私有工作区,每实例独立轮次守卫);结果经引擎结果板交给综合站"
    ),
    task_description="检索单个子问题收集研究资料",
    # exactly one successor = the join node (the engine's join resolution
    # rule: the common-successor intersection of the fan-out targets)
    sub_nodes=["dr_synthesize"],
    plugins={"loop": "dr_search"},
    use_tools=["web_search_prime"],
    base_prompt=DEEP_RESEARCH_BASE_PROMPT,
)

dr_synthesize = BaseNode(
    code="dr_synthesize",
    name="深度研究·综合(join)",
    description=(
        "扇出 join:合并预检索资料与全部检索分支的成果(失败分支降级"
        "不阻塞),流式生成带引用的研究报告"
    ),
    task_description="综合资料产出研究报告",
    sub_nodes=[],
    is_end=True,
    plugins={"loop": "dr_synthesize"},
    base_prompt=DEEP_RESEARCH_BASE_PROMPT,
)

deep_research_pattern = Pattern(
    code="deep_research",
    name="深度研究助手",
    description=(
        "Deep research 图配方:PREPLAN/PLAN/SEARCH/SYNTHESIZE 各为一个"
        " AGENT 节点;PLAN 按子问题扇出 N 个 SEARCH 实例并行检索,"
        "SYNTHESIZE 作 join 汇聚综合(引擎运行时扇出的验收配方)"
    ),
    pattern_type="agent",
    entry_node_code="dr_preplan",
    nodes=[dr_preplan, dr_plan, dr_search, dr_synthesize],
    allow_toolset=["mcp-websearch", "mcp-zai"],
)

registry.register(deep_research_pattern)

# The phase executor plugins register themselves at the bottom of
# apps/deep_research_agent/executor_multi.py (same idiom as before: tying
# the registration action to this pattern's discovery in the same file)
import apps.deep_research_agent.executor_multi  # noqa: E402,F401
