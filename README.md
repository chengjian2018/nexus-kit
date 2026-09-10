# nexus-kit

积木式对话 / Agent 编排框架。从 [hermes-nexus](../hermes-nexus) 迁移而来，
按"目标形态"重组为四层，用一条可执行的分层契约（`tests/test_architecture.py`）钉死边界：

```
host  (3)  组装根：FastAPI 入口 / CLI / 配置装载 / 会话治理
 └─> apps (2)  组合层：业务 pattern（apps/ 下 5 个：xianyu_agent / customer_agent /
      │        deep_research_agent / install_booking_agent / repair_booking_agent）+ 各自 prompt 资产
      └─> atoms (1)  原子积木：executors / stages / tools / providers / knowledge / augmentation
           └─> nexus (0)  内核：context / model / pipeline / engine / registry / llm / settings
```

架构详情（插件中心 / 声明式模型 / 流式协议 / 模块间移动模型）见
**[ARCHITECTURE.md](ARCHITECTURE.md)**；各重构计划的改动清单与避坑记录见
`docs/refactor-notes/plan-{1..6}.md`。

## 核心机制一览

| 机制 | 入口 | 说明 |
|---|---|---|
| **插件中心** | `nexus/registry/plugins.py` | 引擎扩展点统一注册：executor（loop/FSM/ROUTE 三默认执行器 + 自定义）、stage、messages_builder、agent_hooks。字符串 (kind, code)，同名冲突 fail-fast，AST 自动发现（`atoms/executors/` 等） |
| **声明式模型** | `nexus/model/` | node/module/pattern 全字段 str/bool/list/dict 无对象引用。stages 六槽有序骨架（pre_recall/query/post_recall/nlu/clarify/nlg），三层解析 node>module>骨架值，clarify 声明即启用；ModuleLink→dict；yml round-trip（`pattern_to_yaml` / CLI `pattern-export`/`pattern-load`）+ 收集式校验（`nexus/model/validation.py`） |
| **默认流式** | `nexus/llm/` + `nexus/engine/streaming.py` | provider 层 LLMChunk 结构化流（非流式=聚合流式，双向桥兼容旧 provider）；引擎层 `chat_turn_stream` generator（delta/round/done 事件，乐观转发，done 权威）；SSE 调试端点 `POST /api/v1/chat/stream`（`NEXUS_STREAM_DEBUG=1`） |
| **模块间移动** | `enable_project` + 两类事件 | 投影代答 + `defer_to_module` 延迟切换（轮末换底座，防乒乓 forced_projection）；ROUTE 侧同轮跳转（`ModuleJumpEvent`）。transfer_to_XX 已删除；自定义 executor 可按配方写两类事件实现 agent-as-tool / delegate |
| **hooks（保留待实现）** | `nexus/engine/agent_hooks.py` | 7 点位机制完整，默认 no-op 直通（受测契约）；恢复实现只需注册 kind="agent_hooks" 插件包 |

## 布局

| 包 | 职责 | 来源（旧路径） |
|---|---|---|
| `nexus/context.py` | DialogueContext / SessionMessage / ModuleJumpEvent / DeferredModuleSwitch / PipelineStage | `dialogue/base.py` |
| `nexus/model/` | Pattern→Module→Node 三级声明式模型 + serialization（yml round-trip）+ validation（收集式校验）+ 注册期 fail-fast 图校验 | `dialogue/{module,node,pattern}.py` |
| `nexus/pipeline.py` | stages 有序骨架 + 三层延迟解析 + unified 去重 + nlg 延迟解析；兜底 stage 经插件中心注入 | `dialogue/stage_slots.py` |
| `nexus/engine/` | chat（轮次编排 + 流式 generator）/ execution（Executor 契约）/ turn_result / loop（工具箱）/ streaming（ChatStreamEvent）/ agents / hooks / messages / 压缩 / 持久化 | `chat/*` |
| `nexus/registry/` | discovery（共享 AST 扫描）/ plugins（插件中心）/ patterns / tools / providers / channels | `dialogue|tools|llm|channel /register.py` |
| `nexus/llm/` | Provider 抽象 + 聚合流式 + LLMChunk 协议 + 解析 | `llm/{provider,resolve}.py` |
| `nexus/settings.py` | 运行时设置（LLM 三级配置、压缩、DB 路径） | `config/config.py` |
| `nexus/channels/` | ChannelSpec 协议 + 通用 webhook 装配 | `channel/{base,webhooks.py}` |
| `atoms/executors/` | 三默认执行器插件（default_loop / default_fsm / default_route） | 新增（重构计划①） |
| `atoms/stages/` | nlu / nlg / unified / query / recaller / clarify + 默认 prompt（具名 stage codes 注册进插件中心） | `stages/` |
| `atoms/tools/` | knowledge / mcp 工具（MCP 经 `mcp_servers:` 配置动态注册 toolset `mcp-*`） | `tools/*_tool.py` |
| `atoms/providers/` | OpenAICompatible Provider（原生 LLMChunk 流） | `llm/openai_provider.py` |
| `atoms/knowledge/` | SQLite 知识库 | `database/knowledge_store.py` |
| `apps/<name>/` | 业务 pattern（route.py，声明式 stages/executor/sub_modules）+ prompt 资产 + 渠道适配 | `dialogue/*_route.py`、`channel/xianyu.py` |
| `host/` | main.py（含 SSE 调试端点）/ cli.py（含 pattern-export/load）/ governor.py / config/ | 根目录 `main.py`、`cli.py` |


## 安装

要求 Python ≥ 3.11，推荐 [uv](https://docs.astral.sh/uv/)：

```bash
git clone <repo> && cd nexus-kit
uv sync --extra cli --extra dev     # 或 pip install -e ".[cli,dev]"
```

可选依赖组：`cli`（fire / prompt-toolkit，CLI 与交互式 REPL 需要）、
`dev`（pytest / import-linter）。服务端依赖（fastapi/uvicorn 等）在主依赖中。

## 配置

```bash
# 1. 从模板创建本地配置（该文件含密钥，已被 .gitignore 忽略、绝不入库）
cp host/config/local_config.example.yaml host/config/local_config.yaml

# 2. 按需修改 llm_default / llm_providers（模板内有逐节注释）

# 3. 导出 API key（默认 provider 经 DashScope 兼容模式调用，从该环境变量取 key）
export DASHSCOPE_API_KEY=sk-...
```

最小可用配置 = 模板原样 + `DASHSCOPE_API_KEY`。pattern 级模型覆盖
（`pattern_llm`）、MCP 工具网关（`mcp_servers`）、DB 路径与压缩阈值均见
模板注释；完整 schema 见 `nexus/settings.py`。

### 环境变量

| 变量 | 默认 | 用途 |
|---|---|---|
| `DASHSCOPE_API_KEY` | — | 默认 provider（openai，DashScope 兼容模式）的 API key |
| `NEXUS_CONFIG` | 自动探测 | local_config.yaml 路径覆盖（默认探测 `host/config/`、`config/`） |
| `NEXUS_LOG` | `WARNING` | 根日志级别；排障时 `NEXUS_LOG=INFO` 可见轮次 / MCP / 工具分派日志 |
| `NEXUS_API_KEY` | 未设置 | 核心 API 鉴权；**未设置时服务无认证**（启动时每分钟告警） |
| `NEXUS_STREAM_DEBUG` | 未设置 | `=1` 挂载 SSE 流式调试端点 `POST /api/v1/chat/stream` |

## 运行

```bash
# 测试（离线，fake provider 打桩 LLM；含分层契约与各业务 pattern 验收）
python -m pytest        # uv 环境：uv run python -m pytest

# 服务（需要 host/config/local_config.yaml）
uvicorn host.main:app --port 8000
# 流式调试端点（可选）：NEXUS_STREAM_DEBUG=1 后 POST /api/v1/chat/stream（SSE）

# CLI 调试（fire 子命令：chat / ask / list / sessions / pattern-export / pattern-load / knowledge-seed）
python -m host.cli ask --pattern xianyu_agent --query "还在吗"
python -m host.cli pattern-export xianyu_agent --out xianyu.yml   # yml round-trip
python -m host.cli pattern-load xianyu.yml                        # 构造+校验+注册
python -m host.cli knowledge-seed                                 # customer_agent 演示前置：灌知识库
```

分层契约：`tests/test_architecture.py` 在每次 pytest 时强制
`host → apps → atoms → nexus` 单向依赖，并禁止任何旧扁平包名回流。
本地人工审计可再跑 `lint-imports`（配置在 `pyproject.toml [tool.importlinter]`）。
