# browser_agent — 浏览器自动化助手（skill 转写）

把 skill_kb 的 **browser-automation-toolbox**（v2.0.11, MIT）转写为 nexus-kit
应用：一条消息 → 计划 → 多引擎降级执行 →（按证据修复）→ 回执报告，端到端
交付采集数据 / 截图证据。

## 图（pattern_type=agent，`browser_agent`）

```
ba_plan ──> ba_run ──┬──成功──────────────────> ba_report (is_end)
 任务理解+计划   单次执行+确定性路由 │    ↑
                 （图即降级循环）    ├── selector/能力缺口 → ba_repair
                                    ├── 依赖缺失/配置缺失 → 下一引擎
                                    ├── 网络/超时 → 同引擎重试（自环）
                                    ├── 登录墙/验证码 → wait_human 人工接管
                                    └── 引擎耗尽 → 诚实失败报告
```

- **ba_plan**：LLM 产出 action chain 计划 JSON；代码校验结构 + 按域名解析
  平台与引擎顺序（小红书→browser-act 优先；AI 平台→cloak>playwright；
  其余 cloak>browser-act>kimi>playwright）。解析失败诚实降级为最小采集
  计划。
- **ba_run**：每次访问跑**一次**引擎尝试（子进程调内嵌 orchestrator），
  按 EngineResult 回执的 failure_kind 确定性路由——模型永不裁决收敛。
  登录墙/验证码时挂起（wait_human），提示用户在可见浏览器里完成接管，
  回复后同引擎重试一次再换引擎。
- **ba_repair**：携带完整尝试轨迹 + 修复日志（防重放）修订计划；修复输出
  不可用时保留旧计划并换引擎，绝不卡死循环。
- **ba_report**：从回执确定性装配（无 LLM）：成功给输出/产物绝对路径，
  失败如实列每次尝试与安装提示；并按源 skill 经验沉淀协议把「引擎降级
  命中 / 选择器修复有效 / 全引擎失败信号」追加到
  `data/browser_agent/experience/<platform>.md`（跨会话、去重）。

## 与源 skill 的对应关系

| 源 skill 机制 | 应用内落点 |
|---|---|
| 4 引擎降级链 + 平台优先级覆盖 | 图即降级循环（每次访问一尝试）+ `orchestrator.py` 的 `PLATFORM_PRIORITY` |
| action chain 契约（plan JSON） | `prompts.py` 计划提示词 + `executor._validate_plan` |
| 失败分类（failure_kind 8 类） | 内嵌 orchestrator `classify_error`；`executor` 路由表 |
| 证据采集（截图/日志/错误） | 每次尝试的 run 目录 `data/browser_agent/<session>/run_NN_<engine>/` |
| 登录墙/验证码人工接管 | `ba_run` 的 wait_human（挂起即回复接管指引） |
| 经验沉淀协议 | `_record_experience`（确定性触发条件） |
| 嵌入式集成（integration guide 模式 4） | `orchestrator.py`（逐字复制 + 头注两处偏差：attribution 头、AI 平台优先级表补录） |
| 平台采集实战模式 | `references/platform-scraping-patterns.md`（复制自源） + prompts 速查表 |

**未纳入**（源 skill 的元能力，非采集任务本身）：skill 评估与能力合入、
SkillHub 更新触发、BrowserSkill 互补工具推荐（决策表已写入 prompts 速查的
相邻知识，运行时由 general_agent 类应用承载更合适）。

## 依赖（全部惰性，默认零安装）

引擎按需安装，应用默认**不**自动安装（`config.yaml: install_missing:
false`，缺失即诚实报告安装提示）：

```bash
pip install cloakbrowser && python -m cloakbrowser install   # 反检测，默认首选
uv tool install browser-act-cli --python 3.12                # 云端池，小红书优先
pip install playwright && python -m playwright install chromium  # 确定性兜底
# Kimi WebBridge：设 KIMI_WEBBRIDGE_URL / KIMI_WEBBRIDGE_COMMAND
```

运维工具：`python scripts/bootstrap_browser_engines.py`（复制自源 skill）。
引擎结果契约见 `references/engine-contract.md`；依赖策略见
`references/dependencies.md`。

## 验证

```bash
pytest tests/test_browser_agent_route.py   # 离线：结构 + 假provider全链路走线
pytest tests/test_architecture.py          # 分层门
```

离线测试用脚本化 bash 桩替代真实浏览器；绿灯证明注册/分发/执行器接线，
不证明语义质量——真实试点需关注：引擎可用率、修复成功率（selector 漂移
一轮修好的比例）、人工接管恢复率。
