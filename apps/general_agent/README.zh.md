# general_agent — 通用 AI Agent（单节点·技能挂载版）

单节点 AGENT 图 + 零自定义 executor：`assistant` 节点跑 `default_loop`
（ReAct 工具循环），一条用户消息触发端到端自主工作（理解 → 规划 → 工具
循环 → 汇报），节点终止即图终止。

## 能力面（全部复用内置，零新注册）

| 层 | 声明 | 内容 |
|---|---|---|
| 工具（三层授权） | `allow_toolset=["shell","filesystem"]` × `use_tools` | `bash` / `run_python` + `read_text` / `write_text` / `edit_file` / `list_dir` / `search_files` / `find_files` |
| 技能（两层授权） | `allow_skills` × `use_skills` | `nexus-app-builder-skill`（app builder）+ `nexus-app-template-skill`（template builder） |

技能生效集非空时，`default_loop` 自动附带 `load_skill` / `read_skill_file`
两个只读知识工具，并把「可用技能」元数据块注入 system prompt（description
即触发器）。技能授予知识不授予权限：写 `apps/`、跑 pytest 仍走三层工具
授权。

## base_prompt（借镜 dsh）

逐工具一节祈使句使用规则（"读文本用 read_text 而非 bash cat"、"局部
修改优先 edit_file"、"bash 结果必查 exit_code"）+ 技能装载 / 落位 /
汇报 / 安全四条横向纪律；两个 builder 技能的**流程纪律归 SKILL.md
手册本体**，prompt 不复述（避免两份拷贝漂移）。见 `prompts.py` 头注。

## 配置（`config.yaml`）

- `llm:` pattern 级默认 → `zai / glm-5.3`（temperature 0.7，max_tokens
  10000，开 thinking）；
- `loop.max_tool_rounds: 50`：通用 Agent 单轮任务跨度大，放宽全局默认
  10 → 50（单节点图，`max_steps` 不构成约束）。

## 技能扫描根

默认 `skills/`（服务启动目录相对，settings `skills.dir` 可全局改）；部署
要换目录时在 app `config.yaml` 写 `skills.dir:` 覆盖（最高优先级）。

## 文件

- `route.py` — pattern + 节点声明与注册（模块级 `registry.register`，
  host AST 自动发现）
- `prompts.py` — base_prompt 资产
- `tests/test_general_agent_route.py` — 离线冒烟：结构/校验、L0 技能块
  与知识工具自动附带、脚本化整轮（装载手册 → 写产物 → bash 验证 →
  汇报）、越权技能拦截与自纠
