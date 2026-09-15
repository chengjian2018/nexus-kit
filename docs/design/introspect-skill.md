# 设计方案：nexus-introspect —— 面向 agent 的应用/插件内省 skill

> 状态：设计稿（未实现）｜日期：2026-09-10
> 目标读者：实现者 + 需要在此仓库工作的 coding agent

## 1. 背景与目标

agent 在本仓库工作时的四个高频问题，目前都只能靠全仓 grep + 人工读文件：

1. 项目有哪些应用？每个应用注册了什么？
2. pattern `install_booking_agent` 的结构 / stages 装配长什么样？
3. 插件码 `install_unified` 背后的实现代码是什么？（声明里只有字符串码）
4. 改一个插件前，哪些 pattern 在用它？（反向索引，现在无从查起）

本设计提供一组**确定性只读查询函数**（数据层 = 注册表 + 源码定位，插件层 =
源码纯文本提取），暴露为 CLI 子命令，再包一层极薄的 agent skill。

**非目标**：不做编辑/热重载（`host/reload.py` 已有）；不替代 ARCHITECTURE.md
（那是叙事文档，本 skill 是结构化事实查询）；不解析 prompt 资产语义
（`prompts.py` 只跟到文件级引用）。

## 2. 分层

```
暴露层  .claude/skills/nexus-introspect/SKILL.md（用例→命令表，极薄）
          └─ CLI: python nexus-introspect-skill/introspect.py <子命令> [--json]
查询层  nexus/introspect.py（新，纯函数、只读）
          list_apps / describe_pattern / describe_plugin
          plugin_graph / who_uses / list_plugins
数据层  ① 注册表快照（pattern/plugin/tool/channel registry）+ 归属推断
          ② 源码定位：inspect + lambda/工厂 unwrap 规则（§4.2）
          ③ 装配口径复用：pipeline.resolve_stage_code + executor 解析链（§4.3）
```

查询层放 **nexus/**（registry 旁）：它只 import nexus（registry / pipeline /
model / serialization），而归属信息（apps/atoms 前缀）从数据里推断，不需要
import apps/atoms —— 分层契约 `host → apps → atoms → nexus` 零冲突。
注册副作用由现有的 discovery 机制触发（`_ensure_discovery()` 同源；import
只做纯注册，不连 LLM/DB，安全）。

## 3. 数据层

### 3.1 注册面盘点（设计输入，实现时以此验收）

| 注册表 | 位置 | 现有内容 |
|---|---|---|
| patterns | `nexus/registry/patterns.py` | 6 个：customer_agent / deep_research / deep_research_multi / install_booking_agent / repair_booking_agent / xianyu_agent |
| plugins | `nexus/registry/plugins.py` | kind ∈ {executor, stage, messages_builder, stage_factory}；executor 8 个（3 默认 + deep_research + 4 个 dr_* 相位）、stage 8 个应用本地 + 内置 stage 码、messages_builder 1 个 |
| tools / channels / llm providers | 各自 registry | 一期不展开（tools 已有 `cli list tools`），接口预留 |

### 3.2 归属（provenance）

现状：registry 只存对象，不记注册方。两视角都有用：

- **实现文件**（主视角，查询期推断，零改动）：`inspect.getfile(factory)`
  → 路径含 `apps/<name>/` 即归属该 app；含 `atoms/` 即内核积木。
- **注册点**（辅视角，注册期记录）：`PluginRegistry.register` 内
  `sys._getframe(1).f_globals.get("__name__")` 记录注册方模块（一行改动，
  零调用方侵入）。例：`default_loop` 实现文件是
  `atoms/executors/loop_executor.py`，注册点是 `atoms/executors/__init__.py`
  ——两个都对，回答不同问题。

一期只做实现文件视角；注册点记录为增量项。

### 3.3 数据结构（查询层返回值）

```python
@dataclass
class PluginInfo:
    kind: str            # executor / stage / messages_builder / ...
    code: str            # install_unified
    file: str            # 相对仓库根路径
    lineno: int          # 定义起始行
    owner: str           # "apps" | "atoms" | "nexus"
    owner_app: str | None  # apps.install_booking_agent（atoms/nexus 为 None）
    registered_by: str | None  # 注册方模块（二期）
    source: str          # 源码纯文本（with_source=False 时为空）
    used_by: list[str]   # 引用它的 pattern 码（who_uses 填充）

@dataclass
class AppInfo:
    name: str            # install_booking_agent
    files: list[str]     # 目录内源文件
    patterns: list[str]  # 注册的 pattern 码
    plugins: list[tuple[str, str]]  # (kind, code)
```

## 4. 关键机制

### 4.1 pattern 格式化（易）

三个视图，前两个近乎免费：

- `view="yaml"`：复用 `nexus/model/serialization.pattern_to_yaml`；
- `view="tree"`：人类可读结构树（pattern → modules → nodes，标注
  entry / is_end / stages 声明原文）；
- `view="resolved"`：见 4.3，逐模块给出**生效装配**（区别于声明）。

### 4.2 插件源码提取（`source_of(factory)`，本设计的核心难点)

仓库里 factory 有三种注册形态，unwrap 规则依次尝试：

| 形态 | 例 | 规则 |
|---|---|---|
| 类/函数本体 | `InstallBookingUnifiedNLU`、`DeepResearchExecutor` | `inspect.getsource(factory)`（类→全类含装饰器） |
| lambda 包装 | `lambda: customer_agent_messages_builder`（customer_agent） | `factory.__closure__[i].cell_contents` unwrap 到真实对象 → 上一行；输出附注「经 lambda 工厂注册」 |
| 工厂函数 | `_keyword_clarify_factory`（install/repair clarify） | `getsource(factory)` 拿工厂体；再 AST 扫体内 return 调用的类名，从工厂所在模块取产物类源码一并输出 |

兜底：unwrap 失败（builtins / C 扩展）→ 返回
`"{module}:{qualname}"` + 文件行号定位，不抛错。
闭包 cell 定位真实实现：遍历 `__closure__` 取第一个 `isclass/isfunction`
的 cell（当前两种 lambda 用法都是单 cell 直指，够用；复杂形态走兜底）。

### 4.3 pattern → 插件引用图（装配口径 = 运行时口径）

不手工遍历声明字段——三层解析（node > module > 骨架）、unified 去重、
nlg 延迟解析都在 `nexus/pipeline.py`，手工复刻必漂移。做法：

- stages：抽 `resolve_declared_stages(module, pattern)` —— 复用
  `resolve_stage_code(slot, cxt, module, pattern)`，构造最小
  `DialogueContext(session_id="__introspect__", current_module_code=...)`
  逐槽解析。需要把 pipeline 内相关函数的 cxt 依赖收窄为
  「只读 cxt 的少数字段」（现实现本就只用 current_node_code 等少数几项，
  签名不动，传假 cxt 即可，先验证再决定是否抽 Optional 化）。
- executor：按 `module.executor > pattern.executor_<type> >
  DEFAULT_EXECUTOR_CODES` 链复刻（与 `chat._resolve_executor_code` 同序；
  实现时抽公共函数避免双写）。
- messages_builder / sub_modules / use_tools：直接字段读取。

`plugin_graph` 输出形如：

```
install_booking_agent (fsm pattern, entry: install_booking)
└─ install_booking [fsm, 16 nodes]
   ├─ executor     default_fsm        [默认链尾]  atoms/executors/fsm_executor.py
   ├─ stage query  time_aug_query     [pattern]  atoms/augmentation/…（零 LLM）
   ├─ stage nlu    install_unified    [module]   apps/install_booking_agent/stages.py:101
   ├─ stage clarify install_clarify   [module]   apps/install_booking_agent/stages.py:433
   └─ stage nlg    nlg_pass_through   [module]   atoms/stages/…
```

方括号标注来源层 —— agent 一眼看出「谁决定了这个装配」。

### 4.4 反向索引（who_uses）

遍历 `pattern_registry.list_patterns()` 的声明字段（module.stages /
module.executor / pattern.stages / messages_builder）收集引用。
**语义界定：只显示直接声明引用** —— repair 变体经继承复用 install 守卫，
不构成声明引用，如实不显示；继承关系由 `describe_plugin` 的源码自然可见
（`class RepairBookingUnifiedNLU(InstallBookingUnifiedNLU)`）。

## 5. 查询层 API（nexus/introspect.py）

| 函数 | 签名 | 说明 |
|---|---|---|
| `list_apps` | `() -> list[AppInfo]` | apps/ 目录 × 注册表交集 |
| `list_plugins` | `(kind=None, owner=None) -> list[PluginInfo]` | 插件总览（无源码） |
| `describe_pattern` | `(code, view="tree"\|"yaml"\|"resolved") -> str` | §4.1 |
| `describe_plugin` | `(kind, code, with_source=True) -> PluginInfo` | 元数据 + 源码 |
| `plugin_graph` | `(pattern_code) -> str` | §4.3 |
| `who_uses` | `(kind, code) -> list[str]` | §4.4 |

全部纯函数、只读、无 LLM/DB 依赖；除 `source`/`used_by` 外字段在
`--json` 下结构稳定（可测试契约）。

## 6. 暴露层

### 6.1 CLI 子命令（原设计为 host/cli.py 的 fire dict 扩展；该通道已移除，现行实现：独立脚本 nexus-introspect-skill/introspect.py）

```bash
python nexus-introspect-skill/introspect.py apps                            # 应用总览
python nexus-introspect-skill/introspect.py plugins --kind stage            # 插件总览
python nexus-introspect-skill/introspect.py pattern install_booking_agent --view resolved
python nexus-introspect-skill/introspect.py plugin stage install_unified   # 含源码纯文本
python nexus-introspect-skill/introspect.py graph deep_research_multi       # 装配图（设计子命令，现行脚本未实现）
python nexus-introspect-skill/introspect.py who-uses stage install_unified  # 反向索引
```

默认 text（人读），`--json` 供 agent/程序消费。与既有
`cli list patterns` 的关系：`list` 保持概览不动，`inspect` 是深查；
后续可让 `list` 输出里提示「深查用 inspect …」。

### 6.2 skill（.claude/skills/nexus-introspect/SKILL.md）

内容极薄，一张「想知道 X → 跑 Y」用例表 + 指向 `inspect --help`。
**不内嵌任何输出快照**（会过期），一切查询走 CLI 保证新鲜。
触发描述（description）：agent 需要了解本项目应用结构、pattern 装配、
查插件实现、评估插件改动影响面时。

（可选进阶）同一查询面包装为 nexus tool 注册进 toolset
`nexus-introspect`，让**运行时** agent 也能自查 —— 查询函数纯只读，
直接可复用；一期不做。

## 7. 决策与权衡

| 决策 | 选择 | 放弃项及理由 |
|---|---|---|
| 查询层位置 | nexus/（registry 旁） | atoms/ —— 需 import nexus.registry+pipeline，而 atoms 不能被 nexus import，归属信息从数据推断即可，无必要反向依赖 |
| 源码获取 | 运行时 inspect（import 后取） | 纯 AST 静态扫描（不 import）—— lambda/工厂形态下静态定位实现类很脆；本仓库 import 副作用为纯注册，成本可控 |
| 装配口径 | 复用 pipeline 解析链 | 手工遍历声明 —— 三层解析/unified 去重/nlg 延迟逻辑易与运行时漂移 |
| provenance | 查询期 getfile 为主 | 一期即上注册期 frame 记录 —— 「实现文件」视角已覆盖主需求，注册点视角增量加 |
| skill 形态 | 薄 SKILL.md + CLI | 把查询结果写进 skill 文档 —— 立即过期；把函数做成 MCP —— 过重，CLI+skill 先验证价值 |

## 8. 实现拆分（供排期）

1. `nexus/introspect.py`：`source_of`（§4.2 unwrap 规则）+
   `list_plugins` / `describe_plugin` / `who_uses` —— 最小可用
2. `describe_pattern` 三视图 + `resolve_declared_stages`（§4.3，
   含 executor 解析链抽公共）
3. CLI `inspect` 子命令族 + `list_apps`
4. SKILL.md + 验收：对 6 个 pattern 逐个跑 `inspect graph`，断言所有
   声明引用的插件码都能取到源码（含 lambda/工厂两形态）
5. （可选）注册期 provenance、nexus toolset 包装、tools/channels 面

1–3 为核心（半天级）；4–5 增量。

## 9. 验收样例（设计即契约）

- `inspect plugin stage install_unified` →
  `apps/install_booking_agent/stages.py:101` 起的 `InstallBookingUnifiedNLU` 全类源码
- `inspect plugin messages_builder customer_agent_messages_builder` →
  经 lambda unwrap 输出 `route.py` 中函数源码（附「经 lambda 工厂注册」注记）
- `inspect plugin stage install_clarify` →
  工厂函数 + 产物类 `KeywordClarifyStage` 双段源码
- `inspect graph repair_booking_agent` →
  `nlu=repair_unified [module]` + `executor=default_fsm [默认链尾]` +
  `query=time_aug_query [pattern]`
- `inspect who-uses stage install_unified` → 仅 `install_booking_agent`
  （repair 的继承复用不构成声明引用，语义见 §4.4）
