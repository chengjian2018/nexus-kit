"""deep_research pattern — the four-node AGENT-graph deep-research recipe
(plan-⑧ two-layer form; the pre-merge single-module version is gone).

One AGENT node per research phase, adjacency expressed by ``sub_nodes`` —
each user message runs the whole linear pipeline from the entry node, the
stations relaying **within the same turn** via the routing output
(``TurnResult.next``, the plan-⑧ conditional edge):

    dr_preplan ──next──> dr_plan ──next──> dr_search ──next──> dr_synthesize
     pre-retrieval/init    plan sub-questions  iterative search   synthesize report

- ``pattern code "deep_research"``: the graph version takes over the code
  the deleted single-module route.py used to register (one deep_research
  registration, the "deep_research_multi" code is gone with it);
- step budget: the linear pipeline is 4 node executions, comfortably inside
  the default ``config.max_steps=10`` (the pre-merge ``max_hops=4`` is
  subsumed — no explicit override needed);
- tools authorization (plan-⑧ §4 deny-by-default): the pattern grants the
  two MCP server toolsets (``mcp-websearch`` retrieval / ``mcp-zai``
  vision), the tool-carrying nodes narrow via ``use_tools`` — only the
  websearch server's ``web_search_prime`` retrieval tool is listed today
  (the research prompts drive retrieval only; the zai toolset stays granted
  for future vision-augmented research, no node lists its tools yet —
  MCP tools register asynchronously after startup, so unregistered names
  validate as deferred, see nexus/model/validation.py);
- inter-phase state travels via ``cxt.graph_state["deep_research_state"]``
  (the graph runtime's state board: shared across the run's nodes, cleared
  automatically at graph termination — begin_turn never touches it); the
  final trace still goes to ``cxt.metadata["deep_research"]`` for
  observability / next-turn research continuation;
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
    name="深度研究·规划",
    description=(
        "把问题分解为可检索验证的子问题(JSON 计划;解析失败自纠重试,"
        "仍失败降级为原问题单计划)"
    ),
    task_description="产出研究计划(子问题清单)",
    # dr_synthesize: the orphan bail-out edge (landing on dr_plan without
    # in-flight state skips planning and jumps to the degraded synthesis) —
    # a legal control-flow edge, so it must be declared for the runtime's
    # undeclared-edge guard to admit it
    sub_nodes=["dr_search", "dr_synthesize"],
    plugins={"loop": "dr_plan"},
    base_prompt=DEEP_RESEARCH_BASE_PROMPT,
)

dr_search = BaseNode(
    code="dr_search",
    name="深度研究·检索",
    description=(
        "带工具 ReAct 研究循环:按状态板迭代检索,直至子问题覆盖、"
        "模型判定信息足够或轮次用尽"
    ),
    task_description="迭代检索收集研究资料",
    sub_nodes=["dr_synthesize"],
    plugins={"loop": "dr_search"},
    use_tools=["web_search_prime"],
    base_prompt=DEEP_RESEARCH_BASE_PROMPT,
)

dr_synthesize = BaseNode(
    code="dr_synthesize",
    name="深度研究·综合",
    description="基于全部资料流式生成带引用的研究报告",
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
        " AGENT 节点,sub_nodes 线性邻接,同轮 TurnResult.next 接力"
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
