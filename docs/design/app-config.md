# 应用级配置（App Config）设计方案

- 状态：已评审定稿（grill 会话 2026-09-15）
- 范围：模型选择、生成参数、ReAct 轮次、压缩开关、工具护栏、应用自定义参数的**按应用配置**
- 不动：MCP（声明/启动/密钥/授权全部维持全局现状）、工具授权三层收口、args-only-lower 运行期不变式

---

## 1. 问题诊断

现状的乱不在"缺机制"，而在机制碎成四套且各有窟窿：

| 维度 | 现状 | 问题 |
|---|---|---|
| 模型/生成参数 | `llm_default` ⊕ `pattern_llm.<pattern>[.nodes.<node>]` ⊕ `plugins={"llm": code}` ⊕ `cxt.metadata["llm_override"]` 四层 | 四层里 `pattern_llm` 是为按应用配置设计的，但从未用起来（live 配置为空 `{}`）；`plugins["llm"]` 全仓库零使用 |
| ReAct 轮次 | `loop_executor.py:43 _MAX_TOOL_ROUNDS = 10` 模块常量 | 任何层级都改不了 |
| 图级预算 | `pattern.config={"max_steps": N}` 藏在 free-dict | 无词表校验、与轮次预算概念割裂 |
| 应用内部预算 | archify executor 的 `_MAX_AUTHOR_ROUNDS` 等模块常量 | "没有 per-app 配置"逼出来的产物 |
| 工具护栏 | `subagent_tool:` 等 6 个 yaml 段，纯全局 | 无法按应用放宽/收紧 |
| 压缩 | `session_compress_*` 全局 | 无法按应用开关 |
| 死代码 | `allowed_patterns` 键已废弃但 live 配置仍携带；settings.py:128-135 仍校验 | 误导 |

保留的既有优点：所有配置**每轮对话现读**（mtime 指纹缓存），yaml 改完即热生效——新方案必须保住这一特性。

## 2. 目标与非目标

**目标**

1. 每个应用可通过 `apps/<name>/config.yaml` 配置：LLM 编排参数（可细化到节点）、ReAct 轮次（可细化到节点）、图级步数/扇出、压缩、工具护栏覆盖、skills 根、自定义参数 bag。
2. 全局 `local_config.yaml` 继续承担：连接层（含密钥）、全局兜底、MCP、存储。
3. 无 app 配置文件的应用**零改动照跑**；现有 8 个 app 中 6 个无需任何变更。
4. 热重载语义与全局 yaml 一致。

**非目标**

- MCP 按应用拆分（决策：留全局）。
- 工具授权（`allow_toolset` ∩ `use_tools` 三层 deny-by-default）搬进配置——授权仍在代码，配置只管资源参数。
- studio 生成的 pattern（`host/config/patterns/*.yaml`）的 per-app 配置：v1 只走全局默认。预留扩展点：将来可加伴随文件 `host/config/patterns/<code>.config.yaml`，加载器接口按"配置来源列表"设计即可容纳。

## 3. 总体设计：双层载体 + 显式绑定

```
全局 local_config.yaml                 apps/<name>/config.yaml（入库，无密钥）
├─ llm_providers   连接层+密钥          ├─ pattern: <code>        显式绑定（必填）
├─ llm_default     编排兜底层           ├─ llm: {...}             pattern 级 LLM 默认
├─ loop            全局循环默认(新)      ├─ nodes:                 node 级细化
│   └─ max_tool_rounds: 10              │    └─ <node>:
├─ mcp_servers     维持现状             │         llm: {...}
├─ session_*       存储                  │         loop: {max_tool_rounds: N}
├─ subagent_tool:  等 6 护栏段(原名)     ├─ loop: {...}            pattern 级循环预算
├─ skills.dir      全局根               ├─ compression: {...}
└─ ...                                  ├─ guardrails: {...}      同名键覆盖全局 6 段
                                        └─ skills: {dir: ...}
                                        └─ config: {...}          自由 bag
```

**为什么 app 文件不带密钥也够用**：连接层（api_base/api_key_env 等）只允许出现在全局 `llm_providers`；app 文件的 `llm.code` 只是引用 provider code。校验层强制拒绝 app 文件出现连接字段（`api_base`/`api_key`/`api_key_env`）。

**为什么显式 `pattern:` 键**：`PatternRegistry` 不记录来源文件（patterns.py:61-121 只存 code → Pattern），且目录名 ≠ pattern code（`apps/archify_agent/` 注册的是 `archify`）。显式绑定出错早爆，不引入"目录内唯一 pattern 则自动绑定"的隐式行为。

## 4. Schema 规范

### 4.1 完整示例（`apps/archify_agent/config.yaml`）

```yaml
pattern: archify            # 必填，绑定 pattern code

llm:                        # pattern 级默认，全体节点继承
  temperature: 0.2          # 词表：code/model/temperature/max_tokens/
  max_tokens: 16000         #   enable_thinking/timeout/max_retries
  enable_thinking: true

nodes:                      # node 级细化：只开放 llm 与 loop.max_tool_rounds
  af_route:
    llm: {temperature: 0.0, max_tokens: 2000}
  af_author:
    llm: {max_tokens: 24000, timeout: 180, max_retries: 3}
    loop: {max_tool_rounds: 20}
  af_repair:
    llm: {temperature: 0.1}
    loop: {max_tool_rounds: 8}
  af_percept:               # 感知评审：视觉模型读截图判定（节点级关思考）
    llm: {code: zai, model: glm-5.3-flash, temperature: 0.0,
      max_tokens: 40000, enable_thinking: false}
  af_report:                # 换 provider：code 与 model 必须一起写
    llm: {code: dashscope, model: qwen3.8-max, temperature: 0.4}
  # 未列出的节点整体继承 pattern 级

loop:                       # pattern 级循环预算
  max_tool_rounds: 12       # 全局默认 10
  max_steps: 20             # 原 route.py config={"max_steps":20}，yaml 赢
  max_fanout: 8

compression:
  threshold: 6000           # 0 = 该应用关闭压缩
  retain_count: 12

guardrails:                 # 键名与全局段同名（subagent_tool/workflow_tool/
  shell_tool:               #   shell_tool/file_tool/tasks_tool/cron_tool）
    timeout_seconds: 120    # 字段级合并覆盖全局，可放宽也可收紧

skills:
  dir: skills

config:                     # 自由 bag：app 自定义参数收口
  skill_dir: ~/.claude/skills/archify
  workspace_root: data/archify
  repo_root: .              # 仓库证据核验根（--repo-root，architecture 声明
                            # sources 时生效；缺省=服务启动目录）
  author_rounds: 10
  repair_rounds: 3
  route_retries: 1
  stale_limit: 5
  percept_retries: 1        # 感知评审判定 JSON 自纠重试
```

### 4.2 层级词表

| 键 | pattern 级 | node 级 | 词表/校验 |
|---|---|---|---|
| `llm` | ✅ 全词表 | ✅ 全词表 | 复用 `_LLM_ALL_FIELDS`（settings.py:48-51）；连接字段（api_base/api_key/api_key_env）拒绝；`code` 出现时 `model` 必须同时出现或可从 `llm_default` 回填（沿用现回填语义） |
| `loop.max_tool_rounds` | ✅ | ✅ | int ≥ 1 |
| `loop.max_steps` | ✅ | ❌ 图级预算 | int ≥ 1 |
| `loop.max_fanout` | ✅ | ❌ 图级预算 | int ≥ 1 |
| `compression.threshold` / `retain_count` | ✅ | ❌ 会话级 | int ≥ 0（threshold 0 = off） |
| `guardrails.<section>` | ✅ | ❌ | section ∈ {subagent_tool, workflow_tool, shell_tool, file_tool, tasks_tool, cron_tool}；字段校验复用全局各节校验器。**cron_tool 例外：app 侧词表窄于全局段（无 `jobs_path`/`tick_seconds`）**——二者是进程级基础设施，app 覆盖会在 add/fire/update 三条路径间拆裂作业仓；写了 warn+剔除，只能配全局段 |
| `skills.dir` | ✅ | ❌ | str |
| `config` | ✅ | ❌ | 自由 dict（执行器自定义键的家） |

未知键：warn + 忽略（与全局 yaml 行为一致）。`nodes` 下出现未注册节点码：加载期 warn，读取期回退 pattern 级（对齐现 `pattern_llm` 的静默回退）。

## 5. 合并语义（优先级链）

### 5.1 LLM（字段级覆盖，缺项继承）

从低到高：**① `llm_default` → ② app `llm` → ③ app `nodes.<code>.llm` → ④ `cxt.metadata["llm_override"]`**（整体压过 ①②③，CLI/测试 seam，18+ 测试文件依赖，不动）。合并完成后叠加 `llm_providers[code]` 连接层（`_merge_connection` 现逻辑不变）。

走查示例（af_author）：

| 层 | 值 |
|---|---|
| ① llm_default | code=zai, model=glm-5.3-flash, temperature=0.7, max_tokens=10000, enable_thinking=true |
| ② app llm | temperature=0.2, max_tokens=16000 |
| ③ nodes.af_author.llm | max_tokens=24000, timeout=180, max_retries=3 |
| 生效 | code=zai, model=glm-5.3-flash, **temperature=0.2, max_tokens=24000**, enable_thinking=true, **timeout=180, max_retries=3** + llm_providers.zai 连接层 |

### 5.2 循环预算

- `max_tool_rounds`：全局 `loop.max_tool_rounds`（默认 10）→ app pattern 级 → app `nodes.<code>.loop.max_tool_rounds`。
- `max_steps` / `max_fanout`：**代码声明（`pattern.config`）为默认，app yaml 赢**。读取点从 `pattern.max_steps` 改为 accessor `resolve_max_steps(pattern)` = app `loop.max_steps` ?? `pattern.max_steps`（max_fanout 同理）。

### 5.3 护栏

app `guardrails.<section>` 与全局同名段**字段级合并**（app 字段赢）。方向自由：可放宽也可收紧——app config 是部署者意志的表达；安全边界在别处：

- 工具调用 args 的 only-lower 不变式（subagent/workflow timeout、shell timeout、cron fire_timeout 等"args 只能调小"）**原样保留**——那条防的是 LLM 运行期自提权；
- 工具授权三层 deny-by-default 不动。

### 5.4 压缩 / skills / config bag

- 压缩：app `compression` 字段级覆盖全局 `session_compress_*`。
- skills：app `skills.dir` 覆盖全局 `skills.dir`；`pattern.config.skills_dir` 保留为代码级默认（yaml 赢，与 5.2 同规则）。
- config bag：`get_pattern_custom_config(pattern_code)` 返回 app `config`，**不与 `pattern.config` 合并**——`pattern.config` 中的自定义键（如 archify 的 `skill_dir`）整体迁移至 app `config` bag，代码内保留默认值。

## 6. 文件机制

### 6.1 发现与加载

- 扫描 `apps/*/config.yaml`，锚点 `Path(__file__).resolve().parents[2] / "apps"`（与 discovery 同锚法，patterns.py:34）。
- 加载器 `_load_app_configs() -> Dict[pattern_code, dict]`：逐文件解析 + 校验 + 归一化，构建 pattern_code → 配置视图。重复 pattern_code（两个文件绑同一 pattern）：fail-fast。
- 缓存：独立缓存表，键 = 全部 app 配置文件的 (mtime_ns, size) 指纹元组；`invalidate_config_cache()`（settings.py:448）同时清两张表，`/api/v1/system/reload` 与 `host/reload.py::reload_all` 已有的失效点自动覆盖。文件数 ≤ 8，整体指纹足够。
- 无文件 / `pattern:` 键指向未注册 pattern：不报错，读取期回退全局（每轮现读，pattern 注册后自动生效；cross-check 只 warn——沿用现 `_cross_check_pattern_llm` 的宽松策略，兼容 studio reload 时序）。

### 6.2 启动期 cross-check（替换 host/main.py:170-186）

`_cross_check_app_configs`：对每份 app 配置校验 ① `pattern` 键已注册、② `nodes` 键存在于该 pattern 节点集、③ `guardrails` section 名合法。仅 warning，不 SystemExit。

## 7. 代码接线清单

### 7.1 `nexus/settings.py`（核心改造）

| 位置 | 改造 |
|---|---|
| `_parse_config_file`（463-675） | 删 `pattern_llm` 解析（503-506 及相关校验 334-364）；新增全局 `loop: {max_tool_rounds: 10}` 段解析 |
| 新增 `_load_app_configs` | 见 6.1 |
| `_resolve_layered`（705-730） | 重写：`llm_default ⊕ app.llm ⊕ app.nodes[node].llm`（签名不变，行为替换） |
| `get_llm_config`（733-771） | 签名不变（`pattern_code/node_code/override/config_path`）；`override` 参数保留（④ CLI 路径及 model 回填逻辑原样）；**所有现存无 pattern 调用点零改动自动回退 llm_default**（兼容关键属性） |
| 新增 `resolve_max_steps(pattern)` / `resolve_max_fanout(pattern)` | app loop 值 ?? `pattern.max_steps/max_fanout` |
| 新增 `get_loop_limits(pattern_code, node_code)` | 三层 max_tool_rounds 解析 |
| `get_session_compress_config`（799-805） | 加 `pattern_code=""` 参数，app compression 字段级合并 |
| `get_subagent_tool_config`（808+）等 6 个护栏 accessor | 各加 `pattern_code=""` 参数，app guardrails 字段级合并 |
| 新增 `get_pattern_custom_config(pattern_code)` | config bag accessor |
| `_validate_mcp_servers`（77-140） | 删 `allowed_patterns` 校验残留（128-135），同步 `_MCP_SERVER_FIELDS`（71-74） |

### 7.2 引擎与执行器

| 位置 | 改造 |
|---|---|
| `nexus/engine/chat.py:129-137` | 删 `plugins["llm"]` 解析分支；`_refresh_llm_config` 只剩 metadata override → app 分层链 |
| `nexus/engine/chat.py:528` | `pattern.max_steps` → `resolve_max_steps(pattern)` |
| `nexus/engine/chat.py:349,421-425` | fanout 读取同理换 accessor |
| `atoms/executors/loop_executor.py:43,133` | 删 `_MAX_TOOL_ROUNDS` 常量；`get_loop_limits(pattern.code, node.code)` |
| `nexus/engine/tool_context.py` | `ToolCallContext` 加 `pattern_code: str = ""`；`tool_call_context()` 签名加参。**实施勘误：三个自定义 executor 此前均未发布任何 context（其工具调用一直走脱离调用的全局回退），本次为 archify（两条 dispatch 路径：文件工具 + `_run_cli` bash）与 deep_research（两处调用点，topic_research 继承）新增发布** |
| `nexus/engine/compression.py:201-213` | `maybe_compress` 取 pattern code（launch 时已解析 pattern_code，随 node_map 一并绑定在 session 上下文）传入 `get_session_compress_config` |
| 护栏工具读取点 | `subagent_tool.py:115,169`、`workflow_tool.py:783,834`、`_cron_core.py:44,219`、`shell_tool.py:42`、`file_tool.py:53`、`task_list_tool.py:38`：从 `current_tool_context()` 取 `pattern_code` 传给各 accessor（None → 全局） |
| cron 特例 | 作业创建时随授权快照一并冻结 `pattern_code`（对齐"作业权限 = 创建时刻冻结快照"原则），触发期用它解析护栏与 LLM 配置 |
| `nexus/model/plugins_field.py:39-46` | 删 `"llm"` 词表项；serialization round-trip 自然不再携带 |

### 7.3 应用迁移与清理

| 位置 | 改造 |
|---|---|
| 新建 `apps/archify_agent/config.yaml` | 4.1 示例即首个真实落地 |
| `apps/archify_agent/executor.py:119-126` | `_MAX_AUTHOR_ROUNDS`/`_MAX_REPAIR_ROUNDS`/`_ROUTE_RETRIES`/`_STALE_LIMIT` → `get_pattern_custom_config` 读取，常量改名 `_DEFAULT_*` 保留为代码默认值；读取收口为模块内单一 helper `_runtime_settings()` |
| `apps/archify_agent/executor.py:153-157` | `skill_dir`/`workspace_root` 改读 config bag（默认值不变）；顺带修正过期 docstring（"author ≤6" 实为 10） |
| `apps/archify_agent/route.py:207` | `config={"max_steps": 20}` 保留作代码默认 |
| `host/config/local_config.example.yaml` | 删 `pattern_llm` 文档段；新增 `loop` 段；新增 app config 使用指引；删 `allowed_patterns` 废弃注释 |
| `host/config/local_config.yaml`（gitignored，手工迁移） | 删 `pattern_llm: {}` 行与 `allowed_patterns` 死键 |
| `ARCHITECTURE.md` | 配置章节重写为双层模型 |

## 8. 实施步骤（每阶段独立可验证）

**Phase 1 — settings 层**：`_load_app_configs` + 校验 + 缓存 + 新 accessor（loop/compress/guardrails/custom bag）+ 全局 `loop` 段 + 删 `pattern_llm`。
验收：新增单测全绿；**全量现有测试零改动通过**（无 app 文件 = 行为不变的证明）。

**Phase 2 — 引擎接线**：loop_executor 轮次、chat.py max_steps/fanout accessor、删 `plugins["llm"]`、压缩 pattern 化、`ToolCallContext.pattern_code` + 护栏工具读取点 + cron 冻结。
验收：现有测试绿（`plugins["llm"]` 相关用例迁移）；新增轮次三层解析测试。

**Phase 3 — 迁移与清理**：archify 常量收口、example yaml 重写、死键清理、cross-check 替换、ARCHITECTURE.md。
验收：archify 现有测试（test_archify_agent.py / test_archify_skill_agent.py）绿；启动日志 cross-check 正常。

**Phase 4 — 测试补全**：`tests/test_app_config.py` 覆盖加载/校验/重复绑定 fail-fast/优先级链/热重载指纹失效/未注册 pattern 回退/连接字段拒绝/护栏双向覆盖/node 级轮次覆盖。

## 9. 风险与边界

1. **连接字段泄漏**：app 文件入库，若误写 api_key 会泄密——校验层硬拒（warn 不够，直接 fail-fast 该文件）。
2. **自定义 executor 漏发 pattern_code**：回退全局护栏，方向偏严，不构成安全问题；cross-check 无法覆盖（运行期才知道），靠 executor 代码评审。
3. **studio pattern**：v1 只有全局默认 + `llm_override` seam；加载器按"配置来源列表"设计，将来加伴随文件不动核心。
4. **`get_llm_config()` 无 pattern 调用点**（stages、studio api、subagent core）：Phase 1/2 不改它们，回退 llm_default 行为与今日一致；pattern 化留作后续增量。
5. **执行器 `.get()` fallback**（temperature 0.7 / max_tokens 2048）：合并链已保证 llm_default 值注入 llm_config，fallback 仅防御，不动。
