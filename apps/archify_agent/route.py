"""archify pattern — the engineering discipline of a diagram-generation
skill, translated into a nine-node AGENT graph recipe (declaration +
executor implementation; executor.py carries the nine-station
NodeExecutors, plugin code = node code).

Source: the archify skill's fast authoring path (SKILL.md v2.17).
Translation principle: the skill's anti-cheating / anti-hallucination /
anti-endless-polishing **acceptance discipline** becomes gate nodes on the
graph plus two non-trunk exit edges; semantics belong to the model
(author/repair/perceptual-review stations), geometry belongs to tools (the
archify CLI), acceptance belongs to receipts (validate / deliver /
visual-check, three separated levels; the perceptual review judges
independently from the visual-check screenshots, and the report station is
assembled from receipts — the model never freelances).

    af_route ──> af_author ──┬────────────────> af_validate ──┬─> af_deliver
     five-type routing  artifact-first │                 ↑  │       │    │
                    │         │              repair loop│  │ fail  │    │
                    v         │                    │  v       │    v
             af_update_probe  │               af_repair       af_visual_check
              update probe (side branch)──┘      │  │            │
                                                │  │ 5 stale     │ shot review
                                                │  │ rounds       v
                                                │  └─(honest exit)──┴─> af_percept ──> af_report
                                                │   (deliver-failure escape) image review  three-level report
                                                └────────────────────────┘            (is_end)

- ``pattern code "archify"``: every user message runs the full graph from
  af_route; stations hand off via ``TurnResult.next`` conditional edges
  (the validate gate picks the pass/fail edge by receipt, the repair
  station picks loop vs honest exit by whether the error minimum was
  refreshed, the deliver station picks browser-check vs escape edge by
  exit code);
- ``max_steps=20``: the repair loop consumes graph steps (one repair round
  = validate + repair, two steps); a stale-5 honest exit costs 16 steps
  end to end (6 initial validate + 6 repair visits + the four trunk
  stations; the perceptual review only adds 1 step on deliver-success
  paths), leaving headroom at 20; the step budget, the stale-5 semantic
  stop, and the executor's internal per-station round caps (author ≤10 /
  repair ≤3 rounds per visit, overridable via the app config bag's
  author_rounds / repair_rounds) form three independent guard layers;
- tool grants (deny-by-default): the pattern grants the two built-in
  toolsets shell / filesystem (the archify CLI runs via node + schema /
  candidate JSON reads and writes); tool-carrying nodes narrow via
  ``use_tools`` — af_route / af_percept / af_report are pure semantic
  stations with zero tools; the deterministic stations (probe/gate/
  deliver/browser-check) call bash directly through _execute_tool in the
  executor, going through the same three-layer grant resolution;
- af_deliver's ``af_report`` edge is the deliver-failure escape edge (a
  non-zero exit is never called success; by contract visual-check does not
  run on a failed delivery path — it would inspect the stale previous good
  artifact); legitimate control-flow edges must be declared, same as the
  dr_plan orphan-escape-edge convention;
- semantic contracts (each station's task_description is that station's
  discipline; details in the executor.py module docstring and prompts.py):
  * af_route: five-type selection + Mermaid input takes only topological
    semantics, never the styling;
  * af_author: artifact first (the next action must be writing the
    candidate), examples contribute field shapes but not facts, ≤12 primary
    nodes, quality_profile=showcase, no geometry controls without a
    diagnosis;
  * af_update_probe: probe once after the first candidate; information is
    not permission, the installed version stays unchanged;
  * af_validate: showcase pass = all 9 artifact checks pass + 0 errors
    0 warnings (4 checks is only basic validation); on pass the candidate
    is frozen and never changed again;
  * af_repair: convergence gate first; the zero-LLM label-clearance solver
    runs before the LLM (component overlaps with suggested coordinates +
    label-route-clearance four-way nearest nudges with geometric evidence;
    a real verifier adjudicates, only strict improvements are kept, worse
    results are byte-rolled-back, reaching zero goes straight to the gate)
    — pixel geometry belongs to tools, the LLM only fixes what needs
    semantic judgment; only touch the diagnosed subject, verify evidence,
    at most one geometry control per round; five consecutive rounds without
    a new error minimum → stop polishing and report honestly with the
    unresolved diagnostics (honest-exit edge);
  * af_deliver: one-shot final acceptance, a non-zero exit is never
    described as success;
  * af_visual_check: browser evidence never modifies / re-renders the
    delivered HTML;
  * af_percept: perceptual review — the image-capability reviewer model
    audits the visual-check screenshot sidecars item by item (both themes /
    READ view / edge quality / label masks / card fit / export cleanliness);
    review only the attached screenshots, never judge unattached ones;
    when there is no evidence or the reviewer lacks image capability, mark
    skipped honestly and never fabricate a pass; its judgment does not
    override the deterministic checks or the browser evidence;
  * af_report: three-level evidence separation (deliver / browser /
    perceptual), report assembled from receipts, never claim a check that
    was not performed.
"""

from apps.archify_agent.prompts import ARCHIFY_BASE_PROMPT
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

af_route = BaseNode(
    code="af_route",
    name="图表·类型路由",
    description=(
        "语义选型站:从需求选 architecture/workflow/sequence/dataflow/"
        "lifecycle 五类之一;Mermaid 输入只读拓扑与含义(flowchart→workflow、"
        "sequenceDiagram→sequence、stateDiagram→lifecycle),不照搬样式;"
        "歧义场景可调 guide 取结构性参考"
    ),
    task_description="判定图表类型与输入形态,移交创作",
    sub_nodes=["af_author"],
    plugins={"loop": "af_route"},
    base_prompt=ARCHIFY_BASE_PROMPT,
)

af_author = BaseNode(
    code="af_author",
    name="图表·产物优先创作",
    description=(
        "读一个匹配 schema + 公共 schema + 一个示例(示例只取字段形态,"
        "不取事实),然后下一个动作必须是写出候选 JSON——不在文字里规划"
        "坐标;一条清晰主路径、短侧枝、稀疏标签、主节点至多 12 个,"
        "meta.quality_profile 固定 showcase,从自动路由与标签起步,"
        "未经诊断不添加任何几何控制"
    ),
    task_description="全新创作候选图表规范 JSON",
    sub_nodes=["af_update_probe", "af_validate"],
    plugins={"loop": "af_author"},
    use_tools=["read_text", "write_text", "find_files"],
    base_prompt=ARCHIFY_BASE_PROMPT,
)

af_update_probe = BaseNode(
    name="图表·更新探针",
    code="af_update_probe",
    description=(
        "侧枝(首个候选落地后探一次,仅一次):跑打包的更新检查器;"
        "silent 不提及;update_available 以一条紧凑通知呈现(已装版本/"
        "最新版本/官方发布说明链接),security 级加克制警告标记;通知是"
        "信息不是许可——已装版本保持不变,是否更新由用户决定;呈现后按"
        "eventKey 确认并回到主线"
    ),
    task_description="一次性更新感知探针,随后回验证主线",
    sub_nodes=["af_validate"],
    plugins={"loop": "af_update_probe"},
    use_tools=["bash"],
    base_prompt=ARCHIFY_BASE_PROMPT,
)

af_validate = BaseNode(
    code="af_validate",
    name="图表·验证闸门",
    description=(
        "validate --quality showcase --json:showcase 通过必须 9 项产物"
        "检查全过且 0 组合错误 0 警告(仅 4 项检查只是基础验证);遗漏或"
        "拼错 meta.quality_profile 先修字段再修几何;通过即冻结候选,"
        "此后绝不修改;未过则携诊断进入修复回路"
    ),
    task_description="验证候选规范,通过即冻结",
    sub_nodes=["af_deliver", "af_repair"],
    plugins={"loop": "af_validate"},
    use_tools=["bash"],
    base_prompt=ARCHIFY_BASE_PROMPT,
)

af_repair = BaseNode(
    code="af_repair",
    name="图表·聚焦修复回路",
    description=(
        "只修改被诊断的 subject,核实 evidence,从 supportedFixes 中选取"
        "方案;每轮至多应用一个几何控制;保留一切有意义的标签——删除"
        "语义标签不是几何修复;错误数刷新下限则回验证闸门再试,连续五轮"
        "无改进即停止打磨,带未解决诊断走诚实出口"
    ),
    task_description="按诊断聚焦修复,或如实上报终止",
    sub_nodes=["af_validate", "af_report"],
    plugins={"loop": "af_repair"},
    use_tools=["read_text", "edit_file", "write_text", "bash"],
    base_prompt=ARCHIFY_BASE_PROMPT,
)

af_deliver = BaseNode(
    code="af_deliver",
    name="图表·最终交付",
    description=(
        "deliver 一次性最终验收:冻结规范字节为同目录私有快照,渲染并"
        "检查该快照,原子提交 HTML,报告规范与产物的 SHA-256 与字节数;"
        "非零退出绝不能被描述为成功;失败交付保留旧输出——后续不得对"
        "该路径跑浏览器检查(会查到陈旧产物)"
    ),
    task_description="冻结规范并原子提交 HTML 产物",
    # af_report: the delivery-failure escape edge (non-zero exit → straight
    # to the report station, no browser check; the same convention as
    # dr_plan's orphan escape edge — a legal control-flow edge must be
    # declared, otherwise the undeclared-edge guard terminates the graph
    # with a warning)
    sub_nodes=["af_visual_check", "af_report"],
    plugins={"loop": "af_deliver"},
    use_tools=["bash"],
    base_prompt=ARCHIFY_BASE_PROMPT,
)

af_visual_check = BaseNode(
    code="af_visual_check",
    name="图表·浏览器证据",
    description=(
        "visual-check 从确切的已交付 HTML 收集自动化浏览器证据,不修改"
        "不重渲染;机器可读测量与截图不证明感知精致——浏览器证据与感知"
        "审查分开报告"
    ),
    task_description="收集有界浏览器行为证据",
    sub_nodes=["af_percept"],
    plugins={"loop": "af_visual_check"},
    use_tools=["bash"],
    base_prompt=ARCHIFY_BASE_PROMPT,
)

af_percept = BaseNode(
    code="af_percept",
    name="图表·感知评审",
    description=(
        "具备图像能力的评审模型按 visual-check 截图侧车(明/暗主题 × 桌面"
        "视口)逐项审查感知质量:构图收敛、双主题一致、连线质量、标签遮罩、"
        "卡片适配、READ 常态、导出整洁;只评所附截图、未附不评,无证据或"
        "评审模型无图像能力时如实标 skipped,绝不编造通过;判定独立于"
        "确定性检查与浏览器证据"
    ),
    task_description="按截图执行图像能力感知评审",
    sub_nodes=["af_report"],
    plugins={"loop": "af_percept"},
    base_prompt=ARCHIFY_BASE_PROMPT,
)

af_report = BaseNode(
    code="af_report",
    name="图表·三级分离汇报",
    description=(
        "把三种证明分开陈述:deliver=确定性产物检查;visual-check=真实"
        "浏览器中的有界行为;感知审查=图像能力评审站按截图的独立判定"
        "(passed/failed/skipped)——非零退出的命令不声称成功,未实施的"
        "检查不声称已做"
    ),
    task_description="如实汇报三级证明与回执",
    sub_nodes=[],
    is_end=True,
    plugins={"loop": "af_report"},
    base_prompt=ARCHIFY_BASE_PROMPT,
)

archify_pattern = Pattern(
    code="archify",
    name="图表工程助手",
    description=(
        "archify 图表配方:ROUTE/AUTHOR/VALIDATE/REPAIR/DELIVER/"
        "VISUAL_CHECK/PERCEPT/REPORT 各为一个 AGENT 节点;验证闸门与修复"
        "回路构成收敛闭环(连续五轮不改进走诚实出口),更新探针为首个候选"
        "落地后的一次性侧枝,感知评审按 visual-check 截图独立判定;"
        "语义归模型、几何归工具、验收归回执"
    ),
    pattern_type="agent",
    entry_node_code="af_route",
    nodes=[
        af_route, af_author, af_update_probe, af_validate,
        af_repair, af_deliver, af_visual_check, af_percept, af_report,
    ],
    allow_toolset=["shell", "filesystem"],
    # The repair loop consumes graph steps (one cycle = validate + repair,
    # two steps): the stale-5 honest exit takes 16 steps end-to-end, and the
    # perceptual review only adds 1 more step on the successful delivery
    # path — 20 leaves headroom (an independent backstop alongside the
    # stale-5 loss cut)
    config={"max_steps": 20},
)

registry.register(archify_pattern)

# The nine station executors self-register at the bottom of
# apps/archify_agent/executor.py (plugin code = node code); this import
# closes the binding loop (the same convention as deep_research_agent)
import apps.archify_agent.executor  # noqa: E402,F401
