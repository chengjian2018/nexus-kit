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

- **开箱即用**：内置 8 个参考应用，覆盖客服、研究、预约外呼、图表工程等典型业务形态，配好模型 Key 即可运行
- **应用构建**：支持基于 Skill 的构建——一句业务需求直接生成应用模板（声明式蓝图）或可运行应用（Pattern 图 + 能力插件），二者可接力互转；自主 / 流程双模式，挂起恢复、并行扇出、流式输出、全程留痕
- **工具与知识**：兼容 MCP 接入外部工具，内置常用工具集与技能装载机制；SQLite 知识库先检索后应答；工具拒绝式默认授权
- **可视化界面**：`/studio` 编排工作台 + `/console` 运营台，对话调试、知识库管理、会话审查一站完成；配置热更新，下一轮对话即生效
- **模型接入**：开箱支持阿里云百炼与 z.ai（GLM），OpenAI 兼容服务改地址即接；模型可按全局 / 应用 / 节点粒度覆写

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

### 6. 用 Skill 构建应用

应用构建方法论沉淀为仓库根目录下三个**与助手无关**的自包含 skill 资产（各含 SKILL.md
说明书与参考文档）：

- `nexus-app-builder-skill/` —— 业务需求 → **可运行应用**（`apps/` 下的 Pattern 图 + 插件 + 测试）
- `nexus-app-template-skill/` —— 业务需求 / 现有应用 → **应用模板**（`app-templates/` 蓝图，零实现）
- `nexus-introspect-skill/` —— 内省 CLI，供 AI 交叉核对源码事实

这三个目录即唯一事实源，随仓库提交；各助手的挂载目录不入库（`.zcode/` 在
`.gitignore` 中），新环境自行建立软链即可，例如 ZCode：

```bash
mkdir -p .zcode/skills
ln -s ../../nexus-app-builder-skill .zcode/skills/nexus-app-builder
ln -s ../../nexus-app-template-skill .zcode/skills/nexus-app-template-skill
```

Claude Code、Cursor 等同理，将软链或拷贝放入各自技能目录（如 `.claude/skills/`）。
对 AI 说一句需求即可触发，全程自主推进：

- 「把 <业务需求> 做成 nexus-kit 应用」→ 先产出选型陈述与五件套方案，再落地代码与离线测试，放入即自动注册
- 「把 <业务需求> 沉淀为应用模板」或「把 apps/<name> 逆向为模板」→ 落成 TEMPLATE.md 蓝图，校验后入模板知识库

模板与应用双向接力：蓝图照卡实现为应用，应用逆向沉淀回模板。

此外，`skills/` 是框架运行时的技能扫描根（settings `skills.dir`，约定见
[skills/README.md](skills/README.md)）——上述 skill 与 `archify` 在其中备有拷贝，
应用节点可经 `load_skill` 只读装载。

## Author

[chengjian2018](https://github.com/chengjian2018)
