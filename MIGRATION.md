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

## 总路线图 v2（统一编号；早期讨论中的 S6-S10 / P 系列 / R 系列标号全部并入本表并作废）

### 第 1 章 · 接缝（纯内核重构，四件互相独立，无新依赖）

- [ ] **1.1 会话 gate**（原 P0.1）：`governor.acquire_turn/release_turn`，
  会话内轮次串行、会话间并行；
- [ ] **1.2 RCU 快照**（原 P0.2）：注册中心改不可变快照 + 原子发布，
  换版不阻塞读者，在飞轮次持旧引用跑完；
- [ ] **1.3 Runner 插件化**（原 S8 第一刀 = R1/R2）：`ModuleRunner` 协议 +
  `ModuleRuntime` 受限门面 + runner 注册表；agent → route → fsm 依次搬到
  `atoms/runners/`；架构测试禁止 nexus import atoms.runners；
- [ ] **1.4 管线与工具策略**（原 S8 剩余 = R3）：四槽骨架声明化（默认
  不变）、transfer 工具注册期预生成、`ToolGate` 从 loop 析出。

### 第 2 章 · 数据化（模版即数据；2.1 依赖 1.3）

- [ ] **2.1 IR 投影**（原 S6）：Pattern/Module/Node 加 pydantic schema +
  `to_spec()/from_spec()`，Python 声明式写法变成编译到 IR 的语法糖；
  `type` 字段引用 runner 注册表（查无此 runner = 注册期 fail-fast）；
- [ ] **2.2 Module 契约**：provides/consumes/slots 插销契约 +
  `module_ref` 库引用（模版 pin 模块版本，模块库单独演进）；
- [ ] **2.3 分发边界**（原 S7）：泛型 `Registry[T]` 统一各注册中心、
  entry_points 第三方发现、可选 src 布局与命名空间包。

### 第 3 章 · 控制面（cordis-py 进场；3.2 依赖 1.2 + 2.1 + 3.1）

- [ ] **3.1 cordis-py 收编**（原 P0/P1 前半）：vendor 进 `nexus/_vendor/`、
  禁用 `!!js` eval、补 shutdown 序列、`nexus/runtime.py` 单写者提交口
  （mount/unmount/update/publish）；发现函数插件化，注册皆可逆 effect；
- [ ] **3.2 模版版本编排**（原 P2 前半）：IR → entry 子树编译器、
  会话钉版 / canary / 回滚（EntryGroup 事务）；
- [ ] **3.3 机器工具面**（原 P2 后半）：`pattern_inspect/get/diff/
  define/simulate/activate` 工具组，走提交口 + 审批闸门 + 历史回放仿真；
- [ ] **3.4 会话 realm**（原 P3，可选实验）：isolate 按会话能力组合
  （dsh agent-presets 式）。

### 第 4 章 · 收尾（低优先 / 条件触发）

- [ ] **4.1 配置注入深化**（原 S9）：`nexus.settings` 模块级访问 → 构造
  注入的 RuntimeConfig/LLMRouter 服务对象；
- [ ] **4.2 内核清理**（原 S10）：`nexus/registry/tools.py` hermes-agent
  所有权映射瘦身、webhooks 对 fastapi 依赖下沉、`atoms/knowledge` 的
  `account_id` 可信注入；
- [ ] **4.3 全 async 统一**（原 P4）：触发条件 = 流式输出 / 海量长连接；
  届时 1.1/1.2 的快照与会话 gate 原样保留，仅执行器换任务。

### 依赖关系

```
1.1 ──┐
1.2 ──┼──→ 3.1 ──→ 3.2 ──→ 3.3        3.4（可随时实验）
1.3 ──┼──→ 2.1 ──→ 2.2 ──→ 2.3
1.4 ──┘
```

当前下一步：**1.3 Runner 插件化**（挡 2.1 的路；纯内部重构，444 测试兜底）；
1.1 / 1.2 体量小，可穿插。

## 已知行为差异（有意为之）

1. **兜底 stage 未注册时 fail-fast**：旧代码 `stage_slots` 直接延迟 import
   `stages.*`；新内核在没有任何注册工厂时抛 `RuntimeError`（提示 import
   atoms.stages 或显式配置 stage）。宿主与 conftest 都会预热，正常路径无感。
2. **配置文件搜索路径**：新增 `NEXUS_CONFIG` 环境变量与 CWD 探测分支；
   显式 `config_path` 参数行为与旧版完全一致（离线测试的 override 降级
   语义不变）。
3. `MODULE_DEFAULT_PROMPT`（空串、全仓库无引用）未迁移。
