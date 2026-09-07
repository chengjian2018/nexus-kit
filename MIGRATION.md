# 迁移计划：hermes-nexus → nexus-kit

原则：**行为保持的搬移优先于重构**。每个阶段一次提交、测试锚定；重构项单列，
不与搬移混在同一提交里。验收标准：全部离线测试在新的分层下通过，且
`tests/test_architecture.py` 分层契约成立。

## 阶段总览

- [x] **S0 骨架与闸门**：仓库、pyproject、四层目录、架构测试（AST 强制
  `host → apps → atoms → nexus` 单向依赖 + 禁止旧扁平包名）、README。
- [x] **S1 内核迁移（nexus/）**：context / model / pipeline / engine / registry /
  llm / settings / channels / visualize。含三处必要解耦：
  - `nexus/pipeline.py`：兜底 stage 从"延迟 import stages.*"改为
    `register_default_generate/register_default_clarify` 注册钩子（内核不再
    依赖原子；未注册时 fail-fast 并提示 import atoms.stages）；
  - `nexus/settings.py`：yaml 路径解析改为 显式传参 > `set_config_path()` >
    环境变量 `NEXUS_CONFIG` > CWD 探测（host/config 启动时注入）；
  - `nexus/registry/*`：四个注册中心的自动发现目录分别指向
    `apps/*/`、`atoms/tools/`、`atoms/providers/`、`atoms/channels/+apps/`。
- [x] **S2 原子迁移（atoms/）**：stages（含 `_prompts.py` 承接框架默认 prompt）、
  tools、providers、knowledge、augmentation；`atoms/stages/__init__.py`
  注册内核兜底工厂；`model_tools` 桩删除（`_run_async`/`_sanitize_tool_error`
  内联进 `nexus/registry/tools.py`）。
- [x] **S3 应用迁移（apps/）**：xianyu_agent（route/prompts/channel）、
  customer_agent（route）；根级 `prompt.py` 拆解消亡（`MODULE_DEFAULT_PROMPT`
  无引用，删除）。
- [x] **S4 宿主迁移（host/）**：main.py、cli.py、config/（启动注入配置路径）、
  governor.py（TTL/LRU 会话治理自 main 析出为 `SessionGovernor`）。
- [x] **S5 测试移植与验收**：30 个测试文件全量移植（import 重写 +
  conftest 预热 atoms.stages），pytest 全绿，含 xianyu / customer_agent 两个
  业务 pattern 的行为验收。

## 后续阶段（本次不实施，按优先级）

- [ ] **S6 IR 投影**：给 Pattern/Module/Node 加 pydantic schema +
  `to_spec()/from_spec()`，Python 声明式写法变成编译到 IR 的语法糖；
- [ ] **S7 分发边界**：泛型 `Registry[T]` 统一四个注册中心、entry_points
  第三方发现、可选 src 布局与命名空间包；
- [ ] **S8 策略析出**：ModuleType 枚举 → ModuleRunner 策略注册表；四槽位
  骨架声明化；transfer 工具生成移到注册期；`ToolGate` 独立；
- [ ] **S9 配置注入深化**：`nexus.settings` 模块级访问 → 构造注入的
  RuntimeConfig/LLMRouter 服务对象；
- [ ] **S10 内核剩余清理**：`nexus/registry/tools.py` 中 hermes-agent 的
  handler 所有权映射逻辑瘦身；`nexus/channels/webhooks.py` 对 fastapi 的
  依赖下沉为可选；`atoms/knowledge` 的 `account_id` 可信注入（旧仓库已知债务）。

## 已知行为差异（有意为之）

1. **兜底 stage 未注册时 fail-fast**：旧代码 `stage_slots` 直接延迟 import
   `stages.*`；新内核在没有任何注册工厂时抛 `RuntimeError`（提示 import
   atoms.stages 或显式配置 stage）。宿主与 conftest 都会预热，正常路径无感。
2. **配置文件搜索路径**：新增 `NEXUS_CONFIG` 环境变量与 CWD 探测分支；
   显式 `config_path` 参数行为与旧版完全一致（离线测试的 override 降级
   语义不变）。
3. `MODULE_DEFAULT_PROMPT`（空串、全仓库无引用）未迁移。
