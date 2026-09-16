# ppt_generator_agent — AI PPT 生成助手（skill 转写）

把 `ai-ppt-generator` skill（百度文库 AI PPT 生成）转写成的声明式 FSM
pattern：用户给出 PPT 主题 → 询问是否自己挑模板 →（要）展示**真实**模板
列表等用户选号；（不要）按主题关键词智能匹配模板 → 调百度 AI 生成
（约 2-5 分钟）→ 交付下载链接。

本应用是「确定性守卫承载外部 API」的参考实现：**模板编号、模板列表、
下载链接全部来自 API 数据，在代码里匹配与拼装，模型零编造**——统一
阶段只做语义理解与话术，三处确定性零 LLM 守卫负责一切与真实数据有关
的事。

## 应用组成

| 文件 | 职责 |
|---|---|
| `route.py` | 全部节点定义（6 个）+ FSM Pattern 注册（pattern_type=fsm）+ stages 骨架装配声明 |
| `stages.py` | 应用本地 stage：统一阶段子类（模板选定解析 / 生成守卫 / 模板列表改写，全部零 LLM）+ 插件注册 |
| `tools.py` | 百度 AIPPT API 客户端（模板列表/两段式生成）+ 关键词智能分类器（移植自源 skill）+ 工具注册（toolset `ppt_gen`） |
| `prompts.py` | `PPT_UNIFIED_PROMPT`：统一阶段模板（防编造铁律 + 特殊意图） |
| `__init__.py` | 包标记 |

## 架构

### Pattern 结构（code = `ppt_generator_agent`）

```
ppt_generator_agent (Pattern, pattern_type=fsm, entry: ppt_start)
└── 6 节点单域 FSM
```

```
ppt_start 开场收集主题（槽位 ppt_topic）
  └─ ppt_ask_template 模板选择询问
       ├─要自选─> ppt_show_themes 模板列表展示（真实列表确定性注入）
       │            ├─选定─> ppt_generate
       │            └─拿不定主意─> ppt_ask_template（改自动匹配）
       ├─自动匹配─> ppt_generate PPT生成交付
       │            ├─感谢/确认─> ppt_end（is_end）
       │            ├─换个模板─> ppt_show_themes（自然循环）
       │            ├─重新生成─> ppt_generate（自环重跑）
       │            └─取消────> ppt_decline
       └─取消──────> ppt_decline 通用取消承接 ─> ppt_end 结束语
```

### 确定性守卫（stages.py，全部零 LLM，同轮改写）

FSM 转移发生在轮末，节点级 NLG 会晚一拍生效——所有确定性工作必须搭
统一阶段 `ppt_unified` 的便车（install_booking 同款纪律）：

1. **模板选定解析**：转入生成节点时携带用户所选（tpl_id/风格名）→
   与**真实缓存的模板列表**精确/包含匹配；解析失败 → 确定性停留 +
   如实重新列出可选项。从列表节点出发必须选定，不静默转自动。
2. **生成守卫**：任何转入生成节点的转移 → 真实执行百度两段式生成
   （`asyncio.to_thread` 承载 2-5 分钟阻塞调用）并改写回复：
   成功回复真实标题 + ppt_url；失败确定性停留、如实播报原因，并把该
   模板计入 `failed_tpl_ids`（试败记忆：同一模板失败 ≥2 次确定性拒绝，
   建议换模板——试过什么，回复里列什么）。
3. **模板列表改写**：每次转入模板列表节点 → 拉取（或读缓存）真实
   列表并改写回复。用户永远从 API 真实返回的数据里挑选。

### 状态板（`graph_state["ppt_gen_state"]`，单一名空间键）

| 键 | 写入方 | 循环继承作用 |
|---|---|---|
| `topic` | 生成守卫 | 重新生成轮继承主题 |
| `themes` / `themes_fetched` | 列表改写/选定解析 | 「换个模板」循环复用缓存，不重复拉取 |
| `selected` | 生成守卫 | 本次生成使用的模板（tpl/style/类别） |
| `last_result` | 生成守卫 | 最近一次生成结果（状态/标题/链接/原因） |
| `failed_tpl_ids` | 生成守卫 | **防重放记忆**：生成 ⇄ 重试/换模板循环的收敛闸门 |

## 运行前提

- 环境变量 `BAIDU_API_KEY`（百度千帆 AppBuilder）——密钥只走环境变量，
  不进代码与 config.yaml。未配置时生成守卫确定性如实告知并停留。
- 网络可达 `qianfan.baidubce.com`；生成约 2-5 分钟（读超时 600s）。

## 工具注册说明

`tools.py` 将 `ppt_gen_list_themes` / `ppt_gen_generate` 注册进工具注册表
（toolset `ppt_gen`）以便复用与可见性；本 pattern **未声明**
`allow_toolset`（拒绝式默认，FSM 阶段路径不走 LLM 工具循环），守卫直接
调用函数本体。两个工具保持未授权状态。

## 测试

`tests/test_ppt_generator_agent_route.py`：完全离线（自带脚本化
provider + 打桩 API 客户端），覆盖结构/注册/validate_pattern、自动匹配
与手选两条主路径、选号解析失败停留、生成失败重试继承、防重放拒绝、
换模板循环缓存复用、取消通道与 is_end 收尾、`BAIDU_API_KEY` 缺失路径。
