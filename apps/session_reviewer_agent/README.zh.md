# session_reviewer —— 会话评审与应用优化助手

按 session_id 评审本仓库（nexus-kit）中任意应用的运行轨迹与执行结果，产出
结构化改进建议；人工确认（可配置自动通过）后，对目标应用的**可编辑面**落
编辑对、跑白名单回归测试、失败自修、耗尽回滚，并 best-effort 热加载。

## 图（AGENT，8 节点）

```
sr_route ──> sr_collect ──> sr_metrics ──> sr_review ──> sr_wait_human ──> sr_apply ──┬─> sr_report (is_end)
 提取 id      只读取数+源码    规则指标       LLM评审      wait_human 闸   备份+编辑+测试 │       ↑
              │(逃生)          │(逃生)        │(拒绝/空建议)     └─> sr_fixloop ──┘
              └──────────────> sr_report <────────────────────── 自修≤2轮,耗尽回滚(自环)
```

| 站点 | 职责 | LLM |
|---|---|---|
| sr_route | session_id 提取（task_info > 关键词正则），缺 id 本轮提问收束 | 无 |
| sr_collect | SQLite 只读取数（sessions/messages/trace_events）+ pattern→app 反查 + 源码读取 | 无 |
| sr_metrics | 六类规则指标（轮数/turn_error/工具/clarify/节点重访/挂起恢复） | 无 |
| sr_review | 五维 rubric（routing/slots/tools/reply/prompt）结构化建议 | 1 次调用 |
| sr_wait_human | 引擎原生 wait_human 人工闸；auto_approve 可跳过 | 无 |
| sr_apply | 字节快照 → 编辑对（LLM 起草，代码应用）→ 白名单 pytest | 每文件 1 次 |
| sr_fixloop | 测试失败自修（fix_history 防重放），耗尽回滚 | 每文件 1 次 |
| sr_report | 回执装配报告落盘 + 摘要回复 + 热加载 + 模板过期提醒 | 无 |

## 用法

```bash
# 1) 启动 host（会话库 data/dialogue.db 已有目标应用的运行轨迹）
uvicorn host.main:app --port 8000

# 2) 发起评审会话（对话里给 session_id，或 launch 时放 task_info）
curl -X POST localhost:8000/api/v1/launch -H 'Content-Type: application/json' \
  -d '{"pattern_code": "session_reviewer", "session_id": "rev-1", "task_info": {"session_id": "<待评审会话id>"}}'
curl -X POST localhost:8000/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"session_id": "rev-1", "query": "评审会话 <待评审会话id>"}'
#    → 本轮回复即建议清单，图挂起在 sr_wait_human

# 3) 人工闸：下一条消息
#    "通过"（可附批注）→ 实施优化（编辑+测试+热加载）
#    "取消/拒绝"       → 仅出报告
```

产物：`data/session_reviewer_agent/<评审会话id>/`（`input.json` 全量取数、
`backup_*/` 字节快照、`report_*.md` 报告）。

## 修改边界与安全

- **可编辑面（deny-by-default）**：仅目标应用目录下 `prompts.py` /
  `config.yaml` / `faq.py` / `slots.py`；`route.py` / `tools.py` 仅出建议。
- **回滚**：写前逐文件字节快照，回滚=写回快照，**不碰 git**（工作区可能
  脏）；测试失败自修 ≤ `fix_rounds`（默认 2）轮，耗尽自动回滚。
- **测试白名单**：命令仅由 `tests/test*<app>*.py` glob 拼装，模型不可触
  命令行；shell 超时走 guardrails（本应用放宽 300s）。
- **热加载**：实施成功后 best-effort `POST {reload_url}`（默认本机
  8000）；失败如实提示手动 reload。新会话生效，在跑会话持旧对象跑完。
- **数据只读**：会话库以 `mode=ro` 打开，评审绝不写审计库。

## 配置（config.yaml 自由袋）

| 键 | 默认 | 说明 |
|---|---|---|
| auto_approve | false | true 跳过人工闸 |
| fix_rounds | 2 | 自修轮数上限（0=失败即回滚） |
| run_tests | true | 实施后是否跑白名单 pytest |
| reload_url | 本机 8000 | 置空禁用热加载 |
| session_db_path | 全局 | 待评审会话库路径 |
| workspace_root | data/session_reviewer_agent | 产物根 |

评审/优化节点可在 `nodes:` 下 pin 强模型（质量敏感，见 config.yaml 注释）。

## 测试

`tests/test_session_reviewer_agent_route.py`（离线，剧本 provider + bash
stub）：结构/配置、评审→挂起→通过→实施、拒绝→仅报告、测试失败→自修→
通过、失败→耗尽→回滚、auto_approve 直通、取数失败逃生、单元（提取/映射/
编辑对/审批解析/指标）。
