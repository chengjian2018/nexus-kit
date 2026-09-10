# deep_research_agent — 深度研究助手

结构化深度研究 pattern：规划子问题 → MCP 工具迭代检索 → 反思补搜 →
综合带引用的研究报告。同一套相位实现提供**两种组织配方**——单模块版
（一次 execute 跑完全部相位）与多模块版（相位即模块，同轮接力），
是「自定义 executor 零 kernel 改动组装 agent」的示范应用。

## 应用组成

| 文件 | 职责 |
|---|---|
| `route.py` | 单模块版 pattern（`deep_research`）：一个 AgentModule 绑定 executor 插件 |
| `executor.py` | `DeepResearchExecutor`：PREPLAN → PLAN → SEARCH → SYNTHESIZE 四相位 + 全部相位方法实现 |
| `route_multi.py` | 多模块版 pattern（`deep_research_multi`）：四个相位模块 + `max_hops=4` |
| `executor_multi.py` | 四个相位 executor（继承复用 `DeepResearchExecutor` 的无状态相位方法），经 `ModuleJumpEvent` 同轮接力 |
| `prompts.py` | 六段 prompt 常量（角色/各相位指令/状态板/报告模板）+ 相位锚文本（测试匹配用） |
| `__init__.py` | 包标记 |

## 架构

### 单模块版（code = `deep_research`）

```
deep_research (Pattern, entry: deep_research)
└── deep_research  深度研究（AgentModule, executor="deep_research", use_tools=None）
```

一次 `execute()` 内完成四个相位：

```
PREPLAN    一次带工具 LLM 调用：模型自行决定是否先检索一轮补背景
           （无 tool_calls 即跳过；检索结果留作 PLAN 上下文）
PLAN       一次无工具调用 → {"sub_questions": [...]}
           （JSON 容错提取；失败把坏输出+错误回填让模型自纠重试，
             再失败降级为 [原问题]，标记 degraded）
SEARCH     带工具 ReAct 循环（≤12 轮）：每轮把「研究状态板」重写进 system
           （子问题勾选进度/剩余轮次/命中统计），无 tool_calls 即收工信号
SYNTHESIZE 精简 messages 流式生成报告 —— 唯一转发 text delta 的相位
```

关键设计：**研究过程住 executor 私有 messages 工作区，不落 `cxt.history`**
（几十条 tool 行进历史会撑爆下一轮 prompt）；对话历史只留
「用户问题 → 研究报告」的 Q/A 对；结构化 trace 写
`cxt.metadata["deep_research"]` 供观测与下一轮续研。

防失控预算：SEARCH ≤12 轮、PLAN 重试 1 次、单条结果截 4000 字符、
findings ≤30 条（FIFO）、工作区 ≤60000 字符（超限中段截断最旧 tool 行），
总 LLM 调用硬上限 ≈16。

### 多模块版（code = `deep_research_multi`）

```
deep_research_multi (Pattern, entry: dr_preplan, max_hops=4)
├── dr_preplan    预检索/初始化   ──jump──┐
├── dr_plan       规划子问题      ←───────┘──jump──┐
├── dr_search     迭代检索        ←────────────────┘──jump──┐
└── dr_synthesize 综合报告+复位底座 ←───────────────────────┘
```

- 每个相位一个 AGENT 模块，各自绑一个 executor 插件（`dr_preplan` 等），
  经 `ModuleJumpEvent` 在 chat 层 hop 循环里**同轮接力**；
  `max_hops=4` = 3 跳 + 最终模块恰好 4 次执行，自然收束；
- 相位间状态经 `cxt.metadata["deep_research_state"]`（轮内瞬态，
  SYNTHESIZE 收尾弹出、begin_turn 兜底出清）；终态 trace 仍写
  `deep_research` 键——与单模块版同键同构；
- 全部模块 `enable_project=False`、不声明 `sub_modules`（跳转目标语义，
  不参与投影/defer 体系）；
- SYNTHESIZE 收尾把底座复位到 `entry_module_code`（否则下一问直接落进综合模块）；
- 相位实现零拷贝：四个 executor 类继承 `DeepResearchExecutor`
  只为复用其无状态相位方法，`execute` 各自只做
  「取状态 → 跑一个相位 → 存状态 → 写跳转」。

### 工具面

`use_tools=None`（模块层不设白名单），可用工具集完全由 pattern ACL 决定——
MCP 工具动态注册（toolset `mcp-*`）即动态可见，授权在 local_config.yaml 的
server 级 `allowed_patterns: ["deep_research"]` 收窄。executor 在解析工具前
`await ensure_mcp_ready()` 等待 MCP 连接终态（防首轮抢跑冻结空工具集）。

### 插件注册

- `executor / deep_research`（executor.py 底部）
- `executor / dr_preplan` `dr_plan` `dr_search` `dr_synthesize`
  （executor_multi.py 底部）
- 均经 route.py / route_multi.py 末尾的 import 副作用与 pattern 发现绑定

## 运行

需在 `host/config/local_config.yaml` 配置 `mcp_servers:`（检索类 server，
`allowed_patterns` 收窄到研究 pattern）：

```bash
python -m host.cli ask --pattern deep_research --query "2026年固态电池的产业化进展"
python -m host.cli ask --pattern deep_research_multi --query "..."
```

离线验收测试（相位锚文本匹配、降级路径、预算上限）随 `python -m pytest` 运行。
