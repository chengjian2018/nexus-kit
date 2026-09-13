# topic_research_agent — 六站主题深度研究配方

`deep_research` 的姊妹配方：把综合环节拆开为**合并 / 报告生成 / 格式美化**
三个显式站点，形成一条更长的扇出流水线（plan-⑨ 运行时扇出）。

## 图拓扑

```
tr_preplan ──next──> tr_plan ──sends──> tr_search ×N ──join──> tr_merge ──next──> tr_report ──next──> tr_polish（终态）
 预规划/预检索          分主题规划          一实例一主题           结构化合并           报告草稿生成          格式美化（流式）
```

| 站点 | 职责 | LLM | 工具 |
|---|---|---|---|
| `tr_preplan` | 研究状态初始化 + 可选预检索（模型自行决定是否先检索补背景） | 1 次带工具调用 | `web_search_prime` |
| `tr_plan` | 把问题拆为 3-5 个研究主题（JSON，自纠重试，降级为原问题单主题），按主题 `sends` 扇出 | 1 次（+1 重试） | 无 |
| `tr_search` | 扇出 worker：一个实例研究一个主题，私有工作区 ReAct 检索（每实例独立轮次守卫） | ≤6 轮/实例 | `web_search_prime` |
| `tr_merge` | join：把 `__fanout_results__` 结果板折叠进状态板（统一 [S1..Sn] 编号、FIFO 上限、失败分支计数不阻塞） | **0 次（纯结构合并）** | 无 |
| `tr_report` | 基于合并资料撰写报告草稿（执行摘要/分主题分析/结论与不确定性/参考来源） | 1 次 | 无 |
| `tr_polish` | 格式美化并流式交付最终报告（标题层级/重点加粗/来源列表对齐；不改事实与引用），写终态 trace | 1 次（流式） | 无 |

## 关键机制

- **运行时扇出**：`tr_plan` 返回 `TurnResult.sends=[Send("tr_search",
  {"theme": 问题, "sub_question": 主题}), ...]`；引擎 `asyncio.gather`
  并发执行 N 个实例（检索延迟 = 最慢分支），全部落定后执行 join
  （`tr_search` 的唯一后继 `tr_merge`，plan-⑨ §9 merge 交集解析）。
- **分支隔离**：worker 实例跑在私有工作区（空 history / message_sink
  切断 / `Send.input` 作显式查询），看不到站点间状态；结果仅经
  `TurnResult.extra` 落引擎结果板。分支失败 = error 条目，join 照常。
- **站点间状态**：`cxt.graph_state["topic_research_state"]`（question /
  plan / merged findings / draft），图终止清空；终态 trace 落
  `cxt.metadata["topic_research"]`（phases / themes / per_theme /
  branches / sources / tool_stats / degraded）。
- **预算三层**（plan-⑨ §3.3）：主循环 5 步（默认 `max_steps=10`；worker
  不占图步数）× `max_fanout=8` 宽度 × 每分支 `_MAX_SEARCH_ROUNDS=6`。
- **相位复用**：PREPLAN/SEARCH 直接复用 `deep_research_agent.
  executor_multi.DeepResearchExecutor` 的无状态相位方法；分主题规划、
  结构化合并、报告草稿、格式美化为本应用自有站点。

## 与 deep_research 的差异

| | deep_research | topic_research |
|---|---|---|
| 规划粒度 | 子问题（sub_questions） | 研究主题（themes） |
| 汇聚 | `dr_synthesize` 一站完成合并+报告 | `tr_merge`（结构合并）→ `tr_report`（草稿）→ `tr_polish`（美化交付）三站 |
| 流式相位 | synthesize | polish |
| 状态键 | `deep_research_state` / `deep_research` | `topic_research_state` / `topic_research` |

## 测试

`tests/test_topic_research.py`：图结构/发现/插件绑定、全流水线（含 sends
扇出与 merge 零 LLM 断言）、主题自纠重试与降级、单分支失败降级、流式
delta 只来自 polish + fanout 事件词汇。
