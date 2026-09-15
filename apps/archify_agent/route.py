"""archify pattern — 图表生成技能的工程纪律,转译为九节点 AGENT 图配方
(声明 + 执行器实现,executor.py 携带九站 NodeExecutor,plugin code = 节点 code)。

来源:archify skill(SKILL.md v2.17)的快速创作路径。转译原则:skill 里
防 LLM 作弊/防幻觉/防无限打磨的**验收纪律**落为图上的闸门节点与两条
非主干出口边;语义归模型(创作/修复/感知评审站),几何归工具(archify CLI),
验收归回执(validate / deliver / visual-check 三级分离,感知评审按
visual-check 截图独立判定,汇报站从回执组装、绝不模型自由发挥)。

    af_route ──> af_author ──┬────────────────> af_validate ──┬─> af_deliver
     五类路由    产物优先写作   │                    ↑  │        │    │
                    │         │              修复回路│  │未过    │    │
                    v         │                    │  v        │    v
             af_update_probe  │               af_repair       af_visual_check
              更新感知(侧枝)──┘                 │  │            │
                                                │  │连续五轮     │ 附图评审
                                                │  │无改进        v
                                                │  └─(诚实出口)──┴─> af_percept ──> af_report
                                                │    (交付失败逃生边) 图像能力评审   三级分离汇报
                                                └────────────────────────┘            (is_end)

- ``pattern code "archify"``: 每条用户消息从 af_route 跑全图,站间接力经
  ``TurnResult.next`` 条件边(验证闸门按回执选 pass/fail 边、修复站按
  错误数是否刷新下限选回环/诚实出口、交付站按退出码选浏览器检查/逃生边);
- ``max_steps=20``: 修复回路消耗图步数(一次修复 = validate+repair 两步),
  stale-5 诚实退出全程 16 步(6 首值 validate + 6 修复访问 + 主干四站;
  感知评审只在交付成功的路径上多耗 1 步),20 留余量;步数预算与
  stale-5 语义止损、执行器内部轮次上限(author ≤10 / repair 每访 ≤3 轮,
  app config bag 的 author_rounds / repair_rounds 可覆盖)构成三层独立守卫;
- 工具授权(deny-by-default): pattern 授权 shell / filesystem 两个内置
  工具集(archify CLI 经 node 执行 + schema/候选 JSON 读写);携带工具
  的节点经 ``use_tools`` 收窄——af_route / af_percept / af_report 为纯
  语义站,零工具;确定性站点(探针/闸门/交付/浏览器检查)由执行器直接经
  _execute_tool 调 bash,同样走三层授权解析;
- af_deliver 的 ``af_report`` 边是交付失败逃生边(非零退出绝不称成功,
  按契约不对失败交付路径跑 visual-check——会查到陈旧的上一个良好产物),
  合法控制流边须声明,同 dr_plan 孤儿逃生边惯例;
- 语义契约(每站 task_description 即该站纪律,细则见 executor.py 模块
  docstring 与 prompts.py):
  * af_route: 五类选型 + Mermaid 输入只取拓扑语义不搬样式;
  * af_author: 产物优先(下一个动作必须是写出候选)、示例只取字段形态
    不取事实、主节点 ≤12、quality_profile=showcase、几何控制未诊断不加;
  * af_update_probe: 首个候选后探一次;信息非许可,已装版本保持不变;
  * af_validate: showcase 通过 = 9 项检查全过 + 0 错 0 警(4 项只是基础
    验证);通过即冻结候选,此后不改;
  * af_repair: 收敛闸门前置;零 LLM 的标签避让求解器先行(建议坐标的
    组件重叠 + 带几何证据的 label-route-clearance 四向就近挪移,真验证器
    裁决、严格更优才保留、劣化字节回滚,清零直达闸门)——像素几何归
    工具,LLM 只修需要语义判断的;只改诊断 subject、核实 evidence、每轮
    至多一个几何控制;连续五轮不刷新错误数下限 → 停止打磨,带诊断如实
    上报(诚实出口边);
  * af_deliver: 一次性最终验收,非零退出绝不描述为成功;
  * af_visual_check: 浏览器证据不修改/不重渲染已交付 HTML;
  * af_percept: 感知评审——图像能力评审模型按 visual-check 截图侧车逐项
    审查(双主题/READ 视图/连线/标签遮罩/卡片适配/导出整洁);只评所附
    截图,未附不评;无证据/评审模型无图像能力时如实标 skipped,绝不编造
    通过;判定不覆盖确定性检查与浏览器证据;
  * af_report: 三级证明分离(deliver/浏览器/感知),从回执组装汇报,
    未做过的检查不得声称。
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
    # af_report: 交付失败逃生边(非零退出 → 直达汇报站,不跑浏览器检查,
    # 同 dr_plan 孤儿逃生边惯例——合法控制流边必须声明,否则运行时未声明
    # 边守卫会把图终止在告警上)
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
    # 修复回路吃图步数(一轮 = validate+repair 两步):stale-5 诚实退出
    # 全程 16 步,感知评审只在交付成功路径上多耗 1 步,20 留余量
    # (与 stale-5 止损独立兜底)
    config={"max_steps": 20},
)

registry.register(archify_pattern)

# 九站执行器在 apps/archify_agent/executor.py 底部自注册(plugin code =
# 节点 code);此处 import 完成绑定闭环(同 deep_research_agent 惯例)
import apps.archify_agent.executor  # noqa: E402,F401
