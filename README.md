# nexus-kit

积木式对话 / Agent 编排框架。从 [hermes-nexus](../hermes-nexus) 迁移而来，
按"目标形态"重组为四层，用一条可执行的分层契约（`tests/test_architecture.py`）钉死边界：

```
host  (3)  组装根：FastAPI 入口 / CLI / 配置装载 / 会话治理
 └─> apps (2)  组合层：业务 pattern（xianyu_agent、customer_agent）+ 各自 prompt 资产
      └─> atoms (1)  原子积木：stages / tools / providers / knowledge / augmentation
           └─> nexus (0)  内核：context / model / pipeline / engine / registry / llm / settings
```

## 布局

| 包 | 职责 | 来源（旧路径） |
|---|---|---|
| `nexus/context.py` | DialogueContext / SessionMessage / ModuleJumpEvent / PipelineStage / prompt 模板工具 | `dialogue/base.py` |
| `nexus/model/` | Pattern→Module→Node 三级模型 + 注册期 fail-fast 校验 | `dialogue/{module,node,pattern}.py` |
| `nexus/pipeline.py` | 四槽位骨架 + 三层延迟解析；内置兜底 stage 经注册钩子注入（内核不 import 原子） | `dialogue/stage_slots.py` |
| `nexus/engine/` | 轮次编排：hop 循环、run_agent、hooks、消息构建、压缩、持久化 | `chat/*` |
| `nexus/registry/` | patterns / tools / providers / channels 四个注册中心 + AST 自动发现 | `dialogue|tools|llm|channel /register.py` |
| `nexus/llm/` | Provider 抽象 + 解析 | `llm/{provider,resolve}.py` |
| `nexus/settings.py` | 运行时设置（LLM 三级配置、压缩、DB 路径）；宿主经 `set_config_path()` 注入文件 | `config/config.py` |
| `nexus/channels/` | ChannelSpec 协议 + 通用 webhook 装配 | `channel/{base,webhooks}.py` |
| `atoms/stages/` | nlu / nlg / unified / query / recaller / clarify + 框架默认 prompt（`_prompts.py`） | `stages/` |
| `atoms/tools/` | calculator / weather / knowledge 工具 | `tools/*_tool.py` |
| `atoms/providers/` | OpenAICompatible Provider | `llm/openai_provider.py` |
| `atoms/knowledge/` | SQLite 知识库 | `database/knowledge_store.py` |
| `apps/<name>/` | 业务 pattern（route.py）+ prompt 资产（prompts.py）+ 渠道适配（channel.py） | `dialogue/*_route.py`、`channel/xianyu.py` |
| `host/` | main.py / cli.py / governor.py（TTL+LRU 会话治理）/ config/ | 根目录 `main.py`、`cli.py` |

## 迁移中的关键解耦（对照旧仓库的五个断点）

1. **应用物理迁出框架包**：pattern 发现从"扫 `dialogue/` 自身"改为"扫 `apps/*/`"；
2. **prompt 资产归位**：框架默认 prompt 随 stage 原子（`atoms/stages/_prompts.py`），
   业务 prompt 随应用（`apps/*/prompts.py`），根级 `prompt.py` 消亡；
3. **内核纯净**：`nexus/pipeline.py` 的内置兜底 stage 不再延迟 import `stages.*`，
   改为 `register_default_generate / register_default_clarify` 注册钩子，由
   `atoms.stages` 包在导入时注册（宿主/测试经 conftest 预热）；
4. **配置单向注入**：内核与原子只读 `nexus.settings`（内核自己的设置契约），
   yaml 文件路径由 `host/config` 在启动时 `set_config_path()` 注入；
5. **hermes-agent 血统清理**：`model_tools.py` 桩删除，`_run_async` /
   `_sanitize_tool_error` 内联进 `nexus/registry/tools.py`；
6. **会话治理析出**：main.py 中的 TTL/LRU 逻辑成为 `host/governor.py` 的
   `SessionGovernor`。

## 运行

```bash
# 测试（离线，fake provider 打桩 LLM；444 个用例，含分层契约与两个业务 pattern 验收）
~/miniforge3/envs/hermes_nexus/bin/python -m pytest

# 服务（需要 host/config/local_config.yaml，参考旧仓库 config/local_config.yaml）
uvicorn host.main:app --port 8000

# CLI 调试（fire 子命令：ask / sessions / repl）
python -m host.cli ask --pattern xianyu_agent --query "还在吗"
```

分层契约：`tests/test_architecture.py` 在每次 pytest 时强制
`host → apps → atoms → nexus` 单向依赖，并禁止任何旧扁平包名回流。
本地人工审计可再跑 `lint-imports`（配置在 `pyproject.toml [tool.importlinter]`）。

迁移过程与后续步骤见 [MIGRATION.md](MIGRATION.md)。
