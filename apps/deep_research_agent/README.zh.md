# deep_research_agent — 深度研究助手

结构化深度研究 pattern：规划子问题 → MCP 工具迭代检索 → 反思补搜 →
综合带引用的研究报告。plan-⑧ 后为**四节点静态 AGENT 图**（相位即节点，
`TurnResult.next` 同轮接力），是「自定义节点执行器 + 声明式邻接组装
agent 工作流」的示范应用。

## 应用组成

| 文件 | 职责 |
|---|---|
| `route_multi.py` | 图版 pattern（`deep_research`）：四节点静态图 + 工具授权声明 |
| `executor_multi.py` | 四个相位执行器 + 相位方法基类（`DeepResearchExecutor`），`TurnResult.next` 接力 |
| `prompts.py` | 六段 prompt 常量（角色/各相位指令/状态板/报告模板）+ 相位锚文本（测试匹配用） |
| `__init__.py` | 包标记 |

> 历史注：单模块版（route.py / executor.py，四相位挤一个 execute）与
> `deep_research_multi` 双配方注册已随 plan-⑧ 删除——图版即其声明式形态。

## 架构

### 图结构（code = `deep_research`，pattern_type = agent）

```
deep_research (Pattern, entry: dr_preplan,
               allow_toolset: ["mcp-websearch", "mcp-zai"])
├── dr_preplan    预检索/初始化（use_tools: ["web_search_prime"]）
│     └─ next="dr_plan"
├── dr_plan       规划子问题（无工具）
│     └─ next="dr_search"（另声明 →dr_synthesize 孤儿逃生边）
├── dr_search     迭代检索（use_tools: ["web_search_prime"]）
│     └─ next="dr_synthesize"
└── dr_synthesize 综合报告（is_end，无工具）
```

- **静态邻接 = sub_nodes**：每条消息从 entry 跑全图，节点执行器返回
  `TurnResult(content="", next=下一站)` 接力（plan-⑧ 条件边语义）；
  `dr_plan → dr_synthesize` 是孤儿逃生边（挂起游标落在 dr_plan 且无在途
  状态时跳过规划直奔降级综合）；
- **状态板 = `cxt.graph_state["deep_research_state"]`**：相位间共享的
  研究工作区（问题/计划/findings/轮次），图终止由引擎自动清空——取代旧
  `cxt.metadata` 瞬态 + begin_turn 兜底出清的组合；
- 终态 trace 仍写 `cxt.metadata["deep_research"]`（键/形状与旧版同构，
  观测面零迁移）；每轮从 entry 重跑，无需底座复位代码；
- 步数预算用默认 `max_steps=10`（线性 4 步富余；原 `max_hops=4` 由其承担）。

### 四个相位（executor_multi.py）

```
DR_PREPLAN    一次带工具 LLM 调用：模型自行决定是否先检索一轮补背景
              （无 tool_calls 即跳过；检索结果留作 PLAN 上下文）
DR_PLAN       一次无工具调用 → {"sub_questions": [...]}
              （JSON 容错提取；失败把坏输出+错误回填让模型自纠重试，
                再失败降级为 [原问题]，标记 degraded）
DR_SEARCH     带工具 ReAct 循环（≤12 轮）：每轮把「研究状态板」重写进 system
              （子问题勾选进度/剩余轮次/命中统计），无 tool_calls 即收工信号
DR_SYNTHESIZE 精简 messages 流式生成报告 —— 唯一转发 text delta 的相位
```

关键设计：**研究过程住 executor 私有 messages 工作区，不落 `cxt.history`**
（几十条 tool 行进历史会撑爆下一轮 prompt）；对话历史只留
「用户问题 → 研究报告」的 Q/A 对。

防失控预算：SEARCH ≤12 轮、PLAN 重试 1 次、单条结果截 4000 字符、
findings ≤30 条（FIFO）、工作区 ≤60000 字符（超限中段截断最旧 tool 行），
总 LLM 调用硬上限 ≈16。孤儿防御：任一相位入口发现在途状态缺失 →
标记 orphan_* 直奔综合，产出「证据不足」降级报告，流水线不卡死。

### 工具授权（plan-⑧ §4 三层收口）

`pattern.allow_toolset=["mcp-websearch", "mcp-zai"]`（server 级工具集授权）；
`dr_preplan / dr_search` 节点 `use_tools=["web_search_prime"]`（仓库内
实证的 MCP 检索工具名）；plan/search/synthesize 不声明（无工具）。
executor 解析工具前 `await ensure_mcp_ready()` 等待 MCP 连接终态（防首轮
抢跑冻结空工具集）。注册期校验对 MCP 异步注册的工具名降级为 warning
（运行期三层收口仍生效）。

> 跟进项：若要启用 zai 视觉工具（analyze_image 等 8 个，运行时由 server
> 动态上报名字），需把具体工具名补进对应节点 `use_tools`。

### 插件注册（executor_multi.py 底部，AST 扫描自动发现）

- `executor / dr_preplan` `dr_plan` `dr_search` `dr_synthesize`
（相位 code 即执行器 code；route_multi.py 末尾 import 副作用与 pattern
发现绑定）

## 运行

需在 `host/config/local_config.yaml` 配置 `mcp_servers:`（检索类 server）：

```bash
python -m host.cli ask --pattern deep_research --query "2026年固态电池的产业化进展"
```

离线验收测试（`tests/test_deep_research_multi.py`：相位锚文本匹配、降级
路径、预算上限、流式 delta 只来自综合、挂起游标孤儿恢复）随
`python -m pytest` 运行。
