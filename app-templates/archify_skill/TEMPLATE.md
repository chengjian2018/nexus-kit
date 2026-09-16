# archify_skill — 图表工程助手·技能版（应用模板）

> 元信息：业务形态=说明书式技能执行（单条需求装载技能手册，单节点按手册纪律完成一次图表交付）｜图类型=agent｜借用来源=机制零借用（零自定义执行器的最小应用范式；流程纪律不在图里、在 archify 技能手册 SKILL.md 本体——与 archify 条目构成同一技能的两种消费层级：九节点图编译版 vs 单节点说明书版）｜来源=逆向自 apps/archify_skill_agent

## 节点清单

| code | 名称 | 用途 | sub_nodes | is_end |
|---|---|---|---|---|
| as_work | 图表工程·技能节点 | 装载 archify 技能手册并按其纪律完成一次图表交付（选型→创作→验证→交付→汇报），跑在 default_loop 上 | （无） | ✓ |

## 节点交互表

单节点图：每条需求从入口执行一次节点即终止（is_end），无站点间状态；
工作流数据全部落在文件工作区 `data/archify_skill/`（候选 JSON 与最终
HTML，相对服务启动目录），无 graph_state 业务键。

| 节点 | 读 graph_state | 写 graph_state | 出边与路由 |
|---|---|---|---|
| as_work | —（上下文来自 cxt.history 与已授权技能元数据注入的 system） | —（产物落文件工作区 data/archify_skill/，不写状态板） | 终止（is_end）；节点内 ReAct 工具循环（装载手册→读 schema/示例→写候选→validate→deliver→汇报）全部完成后一次性回复 |

**循环继承（无图内循环）**：单节点单程，无站点循环；迭代收敛语义
（创作→验证→修复）整体内嵌在技能手册的 ReAct 循环纪律里，由
default_loop 的 max_tool_rounds 预算兜底——这正是本模板与 archify 条
目的本质分工：图编译版把收敛闸门显式建成节点，说明书版把纪律托付给手
册。无最佳检查点需求（候选文件即产物本身）。

## Pattern 声明

```yaml
# nexus-pattern: archify_skill
code: archify_skill
name: 图表工程助手·技能版
description: >-
  archify 技能的说明书式运行：单 AGENT 节点 + default_loop，
  use_skills/allow_skills 双层授权启用 archify 技能，流程纪律归 SKILL.md
  手册本体（与九节点 workflow 版 archify 互相独立）。
pattern_type: agent
entry_node_code: as_work
nodes:
  - code: as_work
    name: 图表工程·技能节点
    description: 装载 archify 技能手册并按其纪律完成一次图表交付：类型选型 → 读 schema 与示例 → 产物优先写候选 → validate showcase → deliver → 诚实汇报；工作区 data/archify_skill/，CLI 经 bash 且 workdir=技能目录
    task_description: 按 archify 技能手册完成一次图表工程交付
    sub_nodes: []
    plugins:
      loop: default_loop
    use_skills: [archify]
    use_tools: [bash, read_text, write_text, edit_file, find_files]
    is_end: true
allow_toolset: [shell, filesystem]
allow_skills: [archify]
```

## 插件步骤卡

#### 插件卡：default_loop（executor）
- 绑定位置：as_work 节点 node.plugins 的 loop 槽（内置件，
  atoms/executors/loop_executor.py——本模板**零自定义执行器**，显式绑
  定只为声明清晰，省略时解析结果相同）
- 触发时机：每条需求消息进图，as_work 节点执行时
- 读（graph_state）：cxt.history、node.config.base_prompt、已授权技能元
  数据（use_skills 生效时引擎把技能描述注入 system 并自动挂
  load_skill/read_skill_file 两个只读知识工具）
- 处理步骤：内置 ReAct 工具循环——按 base_prompt 约定：①首个动作必须
  是 load_skill 装载 archify 手册（未装载不得做任何创作判断），手册引用
  的参考文件用 read_skill_file 读；②此后严格按手册流程执行（类型选型→
  读 schema 与示例→产物优先写候选 JSON 到 data/archify_skill/→validate
  showcase→deliver），CLI 命令经 bash 且 workdir=技能目录；③红线不吃手
  册覆盖：showcase 通过=9 项检查全过且 0 错 0 警（4 项只是基础验证）、
  deliver 非零退出绝不能说成功、没做过的检查不声称做过；④汇报交付路
  径、验证结论与回执要点。轮数受 max_tool_rounds 预算约束。
- 写（graph_state）：—（候选与产物写文件工作区；对话回复即 TurnResult.content）
- 出边影响：终止（is_end，next=None）

## 工具描述卡

#### 工具卡：bash（shell）
- 用途：执行 archify CLI（node bin/archify.mjs …，validate/deliver 等），
  必须带 workdir=技能目录
- 参数：命令行 + workdir + timeout
- 返回：stdout/stderr+退出码（validate/deliver 输出机器可读回执）
- 为什么是工具而非 prompt：验收是确定性变换，退出码与 JSON 回执是「非
  零退出不说成功」红线的物理载体

#### 工具卡：read_text（filesystem）
- 用途：读技能目录内 schema、示例与手册引用的参考文件（配合
  read_skill_file）
- 参数：绝对路径
- 返回：文件文本
- 为什么是工具而非 prompt：schema 字段形态必须实读，凭记忆重构会漂移

#### 工具卡：write_text（filesystem）
- 用途：写候选 JSON 与最终交付物到 data/archify_skill/（自动建父目录）
- 参数：路径+内容
- 返回：写入确认
- 为什么是工具而非 prompt：产物优先——候选必须落盘，CLI 只认文件

#### 工具卡：edit_file（filesystem）
- 用途：按 validate 诊断对候选 JSON 做局部修复
- 参数：路径+旧串/新串
- 返回：编辑确认
- 为什么是工具而非 prompt：手册的聚焦修复纪律要求最小 diff

#### 工具卡：find_files（filesystem）
- 用途：在技能目录/工作区定位 schema、示例与已有候选
- 参数：目录+模式
- 返回：匹配路径列表
- 为什么是工具而非 prompt：文件存在性是可验证事实

#### 工具卡：load_skill（skills）
- 用途：装载已授权技能（name=archify）的手册，返回手册正文+技能目录路
  径；use_skills 生效时由引擎自动挂上（授权走 use_skills/allow_skills
  双层，不走工具集三层闸）
- 参数：name（技能名）
- 返回：SKILL.md 正文（头部含技能目录路径，作 bash workdir）
- 为什么是工具而非 prompt：手册是运行时装载的技能资产而非节点内联提示
  词——手册升级即刻生效，节点声明零改动

#### 工具卡：read_skill_file（skills）
- 用途：读技能目录内手册引用的参考文件（schema/示例等）
- 参数：技能内相对路径
- 返回：文件文本
- 为什么是工具而非 prompt：参考文件是技能资产的一部分，经授权读取路径
  受技能目录约束

## 实现注意事项

- **该应用已存在**：`apps/archify_skill_agent/`（route.py + prompts.py
  两个文件），本条目为其结构沉淀，是「最小应用」的参考实现：一个
  default_loop 节点 + use_skills 授权，无 executor.py、无 stages、无
  config.yaml。
- **两种消费层级**：与 archify 条目（pattern code `archify`，九节点）互
  相独立、互不 import——图编译版把 SKILL.md 的验收纪律编译成闸门节点与
  确定性回执站；本模板把纪律留给手册本体、节点只装载与执行。选型指引：
  需要硬收敛保证（防打磨不止/防编造验收）选图编译版；要最小维护面（手
  册升级即生效）选说明书版。
- **职责分界防双源漂移**：base_prompt 只写框架侧无法从手册推断的三件事
  （装载入口、工作区落位、CLI workdir 语义）+ 不可协商红线；其余一切以
  手册为准，不复制手册内容。
- 技能资产随仓库分发（skills/archify/），settings 的 skills.dir 为默认
  根；部署想换技能目录时在 config 覆盖（如 ~/.claude/skills）。
- 授权双层：allow_skills（pattern 面）× use_skills（节点面），非空即自
  动获得两个只读技能知识工具；技能的执行面（CLI/文件）仍走
  allow_toolset × use_tools 三层收口——拒绝式默认语义未放宽。
- 测试范式：图结构/授权断言 + 脚本化 provider 的单节点路由测试（手册装
  载后按纪律走完工具序列、红线文案出现在 base_prompt）。
