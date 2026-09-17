"""browser_agent pattern — the browser-automation-toolbox skill transcribed
into a four-station AGENT graph (declaration + station executors; the engine
substrate is the embedded orchestrator.py, a verbatim copy of the source
skill's scripts/browser_orchestrator.py — see its attribution header).

Source skill: browser-automation-toolbox v2.0.11 (MIT) — 4-engine crawl
fallback chain (cloak > browser-act > kimi > playwright) + platform-aware
priority overrides + action-chain contract + failure classification +
evidence collection + platform experience recording.

Translation principle (the skill's discipline → graph stations):

    ba_plan ──> ba_run ──┬──成功──────────────────> ba_report (is_end)
     任务理解+计划   单次执行+确定性路由 │    ↑
                        │    ├──登录墙/验证码→ wait_human 挂起在本节点，
                        │    │   用户在可见浏览器完成接管后回复恢复
                        ├──selector/能力缺口→ ba_repair ──┘(修好重跑同引擎)
                        ├──依赖缺失/配置缺失→ 跳下一引擎（不盲目重试）
                        ├──网络/超时→ 同引擎重试（≤max_attempts_per_engine）
                        └──引擎耗尽→ ba_report（诚实失败报告）

- semantics belong to the model (ba_plan composes the action chain, ba_repair
  revises selectors from failure evidence); orchestration belongs to code
  (engine order via the platform table, one attempt per visit, failure
  classification via the orchestrator's classify_error, the routing table in
  the run station — never model-decided); acceptance belongs to receipts (the
  report station is assembled from EngineResult receipts, the model never
  freelances);
- the graph IS the fallback loop, one attempt per node visit — finer-grained
  than the source CLI script: a selector-drift failure goes straight to
  repair instead of burning the remaining attempts of every engine on a
  broken plan; a dependency-missing engine is skipped without retry;
- login/captcha takeover is the framework-native wait_human: the run station
  suspends AT itself with visible-browser guidance as the turn reply; the
  next user message re-executes the same node (resume), retrying the SAME
  engine once, then switching — the source skill's "恢复同一引擎一次" rule;
- budgets (three independent guards): graph max_steps=20 × repair_rounds cap
  (default 2) × per-engine attempt cap (default 2); a human takeover is
  capped too (default 2 per run);
- runtime artifacts land under data/browser_agent/<sanitized-session>/
  (plan.json + per-attempt run dirs); cross-session platform experience
  accumulates in data/browser_agent/experience/<platform>.md (the source
  skill's 经验沉淀协议, deterministically recorded on fallback-success and
  repair-success outcomes).

Node interaction table (state board: graph_state["browser_agent_state"]):

| Station | Reads | Writes | Routing out |
|---|---|---|---|
| ba_plan | — (entry: user query) | request/workspace/plan/plan_path/platform/engine_order/engine_idx=0/attempt_no=1/attempts[]/repair_log[]… | → ba_run |
| ba_run | plan,plan_path,engine_order,engine_idx,attempt_no,attempts[],wait_reason,human_takeovers,repair_rounds | attempts[] += receipt; outputs/artifacts (on success); engine_idx/attempt_no cursor; wait_reason; success/done_reason | ok→report; login/captcha→wait_human (self); selector/capability→repair; dep/setup→next engine; network→retry/self; exhausted→report |
| ba_repair | plan,attempts[] (latest evidence),repair_log[] (anti-replay),repair_rounds | plan (revised)+plan_path rewritten; repair_log[] += summary; repair_rounds += 1; attempt_no=1 | → ba_run (always; unparseable repair keeps old plan and lets run advance) |
| ba_report | attempts[],outputs,artifacts,selected_engine,done_reason,repair_log[],platform | cxt.metadata["browser_agent"] (final trace); experience file appends | terminal (is_end) |

Loop inheritance (ba_repair ← rounds 1..N-1): the full attempts trail +
repair_log of applied fixes (anti-replay: round 3 must not re-propose round
1's failed selector); the engine cursor is never reset by repair (a repaired
plan retries the same engine with attempt_no reset — a new plan deserves
fresh attempts, bounded by the global repair_rounds cap).
"""

from apps.browser_agent.prompts import BROWSER_AGENT_BASE_PROMPT
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

ba_plan = BaseNode(
    code="ba_plan",
    name="浏览器·任务计划",
    description=(
        "入口站：初始化运行状态板，一次 LLM 调用把浏览器自动化任务翻译成"
        " action chain 计划 JSON（目标页、动作链、抽取脚本）；确定性代码"
        "校验计划结构并按目标域名解析平台与引擎优先级（小红书→browser-act"
        " 优先，AI 平台→cloak>playwright，其余走默认链），解析失败诚实降级"
        "为最小采集计划"
    ),
    task_description="理解任务，产出并校验 action chain 计划",
    sub_nodes=["ba_run"],
    plugins={"loop": "ba_plan"},
    base_prompt=BROWSER_AGENT_BASE_PROMPT,
)

ba_run = BaseNode(
    code="ba_run",
    name="浏览器·单次执行",
    description=(
        "确定性执行站：按状态板游标（引擎×第 N 次尝试）子进程调用内嵌"
        " orchestrator（cloak/browser-act/kimi/playwright）执行当前计划，"
        "解析 EngineResult 回执并按失败分类确定性路由——成功→报告；登录墙/"
        "验证码→挂起等待人工在可见浏览器中接管（wait_human，恢复后同引擎"
        "重试一次）；选择器漂移/能力缺口→修复站；依赖缺失→跳下一引擎；网络"
        "/超时→同引擎重试；引擎耗尽→诚实失败报告"
    ),
    task_description="执行单次引擎尝试并按回执路由",
    sub_nodes=["ba_report", "ba_repair", "ba_run"],
    plugins={"loop": "ba_run"},
    use_tools=["bash"],
    base_prompt=BROWSER_AGENT_BASE_PROMPT,
)

ba_repair = BaseNode(
    code="ba_repair",
    name="浏览器·计划修复",
    description=(
        "经验继承站：携带完整尝试轨迹与已试修复日志（防重放——第 3 轮不得"
        "重提第 1 轮失败的选择器），一次 LLM 调用按失败证据最小修订计划"
        "（selector_drift 只换漂移选择器、超时加强等待）；确定性代码校验"
        "修订后的计划 JSON，解析失败则保留旧计划由执行站换引擎；登录墙"
        "不是计划问题，不为它修改计划"
    ),
    task_description="按失败证据修订计划，携带防重放记忆",
    sub_nodes=["ba_run"],
    plugins={"loop": "ba_repair"},
    base_prompt=BROWSER_AGENT_BASE_PROMPT,
)

ba_report = BaseNode(
    code="ba_report",
    name="浏览器·回执报告",
    description=(
        "终点站（无 LLM）：从 EngineResult 回执确定性装配报告——成功时给出"
        "选中引擎、尝试轨迹、输出 JSON 摘要与产物绝对路径；失败时如实列出"
        "每次尝试的引擎/分类/错误与安装提示，绝不把失败写成成功；并按源"
        " skill 经验沉淀协议把可复用结论（引擎降级命中、选择器修复）追加到"
        "平台经验文件"
    ),
    task_description="从回执装配诚实报告并沉淀平台经验",
    sub_nodes=[],
    is_end=True,
    plugins={"loop": "ba_report"},
    base_prompt=BROWSER_AGENT_BASE_PROMPT,
)

browser_agent_pattern = Pattern(
    code="browser_agent",
    name="浏览器自动化助手（skill 转写）",
    description=(
        "多引擎降级浏览器自动化：cloak/browser-act/kimi/playwright 四引擎"
        "按平台感知优先级逐个尝试（小红书 browser-act 优先、AI 平台 cloak"
        " 优先），计划由模型创作与按证据修复，执行/分类/路由/报告全部确定"
        "性；登录墙验证码走 wait_human 人工接管；产物与证据落 data/"
        "browser_agent/<session>/，平台经验跨会话沉淀"
    ),
    pattern_type="agent",
    entry_node_code="ba_plan",
    nodes=[ba_plan, ba_run, ba_repair, ba_report],
    # The run station's only tool: drive the embedded orchestrator CLI via
    # bash (per-attempt budget enforced by the shell guardrail, app config
    # loosens it to 300s for real browser chains). Plan/repair/report are
    # tool-less — placement is deterministic executor code.
    allow_toolset=["shell"],
    # Worst honest walk ≈ plan(1) + 4 engines × 2 attempts + 2 post-repair
    # runs + ≤2 human-takeover resumes + ≤2 repairs + report(1) ≈ 16 steps;
    # 20 leaves headroom alongside the semantic caps (repair_rounds,
    # attempts-per-engine, takeover cap).
    config={"max_steps": 20},
)

registry.register(browser_agent_pattern)

# The four station executors self-register at the bottom of
# apps/browser_agent/executor.py (plugin code = node code); this import
# closes the binding loop (the same convention as archify / toy-app).
import apps.browser_agent.executor  # noqa: E402,F401
