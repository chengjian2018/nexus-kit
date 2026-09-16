# nexus-kit

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](#)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MCP](https://img.shields.io/badge/tools-MCP%20ready-5F5FFF)](#核心能力)
[![uv](https://img.shields.io/badge/uv-ready-DE5FE9)](https://docs.astral.sh/uv/)

**nexus-kit** 是一个开源大模型应用框架，核心理念为 **万物皆可流**：任何业务流程——智能客服、研究报告生成器、预约下单系统——均可由该框架统一建模。建模产物由两部分构成：

- **一份配置**：应用的步骤定义、话术与流转规则全部声明于配置文件；调整流程只需修改配置，无需改动代码。
- **一组插件**：模型调用、工具接入、知识检索等能力均为独立积木，按需插拔。

同一框架对三类用户友好：

- 🤖 **Agent**（Claude Code 等 AI 编程助手）：项目内置说明技能，AI 据此即可掌握项目全貌，并直接动手生成新应用；
- 🧑‍💼 **非开发人员**：通过浏览器完成表单填写、配置修改与会话测试，全程零代码；
- 👨‍💻 **开发者**：以 Python 编写新插件与应用，放入即自动生效，测试体系守护代码边界。

## 核心能力

**八个开箱应用**

- **闲鱼卖家客服**（`xianyu_agent`）：自动应答买家消息，先识别来意（议价 / 技术咨询 / 闲聊），再按卖家规则回复；支持议价轮数上限与违禁词过滤
- **店铺客服**（`customer_agent`）：电商售前售后客服，应答前先检索商品与售后资料、杜绝凭空编造，支持商品卡片推送；超范围问题自动转人工
- **深度研究**（`deep_research_agent`）：将课题拆解为子问题，多路并行检索，查毕反思补搜，最终产出带引用来源的研究报告
- **主题研究**（`topic_research_agent`）：深度研究的长流水线变体——主题拆解、并行检索、资料合并、草稿撰写、终稿润色五阶段一站完成
- **安装预约**（`install_booking_agent`）：主动外呼新购家具 / 电器的客户，预约上门安装——地址核对、到货确认、时间协商、改约确认完整覆盖；时间严格对齐排班，不会约出不存在的档期
- **维修预约**（`repair_booking_agent`）：报修场景变体——商品故障后主动联系客户预约上门维修，约期后追问故障详情，便于师傅备件携工具
- **图表工程**（`archify_agent`）：将 archify 图表技能的验收纪律编译为九节点 AGENT 图——五类图表路由、产物优先创作、showcase 验证闸门、聚焦修复回路（零 LLM 几何求解器，连续无改进即诚实退出）、原子交付、浏览器证据、感知评审、三级证据分离汇报；语义归模型、几何归工具、验收归回执
- **图表工程 · 技能版**（`archify_skill_agent`）：同一 archify 技能的说明书式运行——单节点 + default_loop，`use_skills` 双层授权装载 SKILL.md 手册，流程纪律由手册本体承载，零自定义执行器

**应用构建**

- 双模式支持：自主型（Agent 自行决策下一步动作与工具调用）与流程型（严格按预定义步骤执行）
- 流程可中途挂起等待人工确认，用户回复后从断点恢复执行；状态持久化，服务重启不丢失
- 单一任务可扇出为多分支并行执行、完毕自动合并——如并行检索 5 个子问题，总耗时仅取决于最慢分支
- 回复逐 token 流式输出；节点流转与工具调用全程留痕，可审计可回放

**工具与知识**

- 外部工具接入：兼容 MCP 标准（业界通用工具接口），数行配置即可接入一个工具服务
- 内置工具集：命令执行、文件读写、任务清单、定时任务、子 Agent 派发、固定流程执行
- 技能机制：将带手册的技能目录置于 `skills/` 扫描根，节点声明 `use_skills` 即按需装载；流程纪律沉淀于 SKILL.md，修改手册无需改代码（兼容 Claude Code 技能目录布局）
- 内置知识库：导入资料后应答前先检索再生成；检索策略同样由配置声明，修改即生效
- 默认安全：工具未声明即不可调用；高危操作（如命令执行）执行前先行播报

**可视化界面**

- `/studio` 编排工作台：选定应用发送消息调试；或以一句话描述需求，由 AI 生成新应用；亦可通过表单 / 配置文件手动编排
- `/console` 运营台：查看应用结构、管理知识库、调整检索配置、审查会话记录（消息与过程轨迹合并时间线）
- 配置与流程修改基本无需重启，下一轮对话即生效

**模型接入**

- 开箱支持阿里云百炼与 z.ai（GLM）；其他 OpenAI 兼容服务仅需修改接入地址
- 支持全局指定默认模型，亦可按应用甚至按节点粒度覆写

## 系统架构

**运行时全景**——一次请求的完整旅程：浏览器 / 渠道回调 → FastAPI 宿主（入口校验 · 会话治理）→ chat_turn 引擎（配置 · 压缩 · 分流）→ 图运行时（AGENT / FSM）→ 节点执行器 → 工具箱 / MCP 网关 / LLM 云服务；数据落于 SQLite 会话库与知识库：

![nexus-kit 运行时全景图](img/nexus-kit.png)

> 🖱️ 交互式版本：[diagrams/runtime-architecture-v2.html](diagrams/runtime-architecture-v2.html) —— 可探索的独立 HTML 图：19 个核心组件，加粗主路径贯穿请求旅程，双层边界标出进程与工具执行面信任边界，外部 LLM / MCP 依赖置于界外；点选节点可跳转对应源码，附说明卡片与引导视图（明暗主题切换、导出）。

**静态分层**——四层单向依赖：

```
host   (3)  组装根 ─ FastAPI 入口 / 配置装载 / 会话治理 / 热重载 / UI 宿主
 └─> apps  (2)  组合层 ─ 业务 pattern（8 个开箱应用）+ prompt 资产 + 渠道适配
      └─> atoms (1)  原子层 ─ executors / stages / tools / hooks / providers / knowledge / mcp
           └─> nexus (0)  内核 ─ context / model / pipeline / engine / registry / llm / settings
```

| 层 | 职责 | 核心特点 |
|---|---|---|
| **host** | 组装根 | ① FastAPI 服务 + SSE 流式端点（`/api/v1/chat/stream`）+ `/console` `/studio` 双 UI 挂载；② 配置装载、会话治理、四类热重载（配置 mtime 指纹缓存 / pattern·插件按依赖序重放重绑） |
| **apps** | 组合层 | ① 声明式业务配方：Pattern → Node 二层模型、YAML round-trip、注册期收集式校验；② 8 个开箱 pattern（客服 / 深度研究 / 主题研究 / 预约 / 图表工程），自带 prompt 资产与渠道适配 |
| **atoms** | 原子积木 | ① 引擎扩展点统一经插件中心注册（executor / stage / messages_builder / agent_hooks），AST 自动发现；② 内置工具六件套 + MCP 网关（stdio / sse / streamable_http）+ SQLite 知识库，toolset 即授权单元 |
| **nexus** | 内核 | ① AGENT 图运行时（条件边路由 / `wait_human` 挂起恢复 / `sends` 扇出 map-reduce）+ FSM 双引擎，底层默认流式（LLMChunk → ChatStreamEvent → SSE）；② 零业务依赖——nexus 永不 import atoms，默认实现由 atoms 在 import 时反向注册进内核 |

依赖方向严格单向向下（host → apps → atoms → nexus），由 `tests/test_architecture.py` 在每次
pytest 时强制（import-linter 配置见 `pyproject.toml`，作为本地人工审计工具）。架构详情
（核心执行流 / 插件中心 / 声明式模型 / 流式协议 / 会话持久化 / 热重载）见
**[ARCHITECTURE.md](ARCHITECTURE.md)**。

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/chengjian2018/nexus-kit.git
cd nexus-kit
```

### 2. 安装环境

Python 3.11 及以上，推荐 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --extra dev     # 或 pip install -e ".[dev]"
```

### 3. 配置模型 Key

```bash
# 本地配置 host/config/local_config.yaml 已入库，密钥不落盘——全部通过
# 环境变量引用（api_key_env / $VAR），配置好环境变量即可运行

# 三选一：
# ① 使用 z.ai（GLM，当前默认 llm_default）
export Z_AI_API_KEY=...

# ② 使用阿里云百炼——将 local_config.yaml 里的 llm_default.code 改为 dashscope
export DASHSCOPE_API_KEY=sk-...

# ③ 接入自有模型：
#    OpenAI 兼容服务 —— 在 llm_providers 中新增一节、修改地址即可；
#    完全自定义 —— 参照 atoms/providers/dashscope_provider.py 实现，放入即自动生效。
```

配置模板无需改动，配置一个 Key 即可运行。外部工具、知识库与各类上限的调整，
配置文件各节均有注释；全部配置项见 `nexus/settings.py`。

### 4. 启动服务

```bash
uvicorn host.main:app --port 8000
```

可先运行测试（离线执行，模型为桩实现）：

```bash
python -m pytest        # uv 环境：uv run python -m pytest
```

### 5. 访问界面

- **编排工作台** <http://localhost:8000/studio/> —— 默认进入「模版测试」页签：选定应用、
  发送消息即可对话，回复逐字输出，每步过程均可展开查看。「自动编排」页签支持以一句话需求
  生成新应用（需本机安装 claude 命令行）；「流程编排」页签支持表单 / 配置文件方式手动修改、
  校验与发布。
- **运营配置台** <http://localhost:8000/console/> —— 查看应用结构、管理知识库、调整检索配置；
  「会话审查」页支持按 pattern / session_id 过滤历史会话，详情视图提供消息与节点 · 工具轨迹的
  合并时间线（异常轮次、工具幻觉拦截等以红边高亮）。
  （如设置了 `NEXUS_API_KEY`，请在页面右上角填入同一 Key。）

## Author

[chengjian2018](https://github.com/chengjian2018)
