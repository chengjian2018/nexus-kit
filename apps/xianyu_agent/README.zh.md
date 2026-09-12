# xianyu_agent — 闲鱼卖家客服助手

复刻 tmp_xianyu.XianyuReplyBot 的闲鱼卖家客服 pattern：对每条买家消息做
**每轮独立的意图检测**（本地规则优先 + LLM 兜底），路由到议价/技术/通用
菜单节点生成回复，附议价轮数控制、按意图调温与违禁词过滤。plan-⑧ 后为
**AGENT 图**——路由节点条件边分发，每条消息从入口重跑（原 ROUTE「轮末回根」
天然成立）。本应用独有的**渠道声明**（channel.py）使其可直连
xianyu-auto-reply 的默认回复 API。

## 应用组成

| 文件 | 职责 |
|---|---|
| `route.py` | 本地意图规则、三个节点执行器（路由/生成/规则回复）、五节点图、Pattern 注册 |
| `channel.py` | `XianyuChannel` 声明式渠道（ChannelSpec）：payload 契约、session 派生、响应契约 |
| `prompts.py` | 四段 prompt 资产：意图分类 + 议价/技术/通用三个 NLG 模板 |
| `__init__.py` | 包标记 |

## 架构

### 图结构（code = `xianyu_agent`，pattern_type = agent）

```
xianyu_agent (Pattern, entry: xy_route_root)
└── xy_route_root 闲鱼总路由（loop=xianyu_router）
    ├── xy_menu_price         议价（loop=xianyu_reply，未达轮数上限）
    ├── xy_menu_price_refuse  议价拒绝（loop=xianyu_rule_reply → 固定话术，零 LLM）
    ├── xy_menu_tech          技术问答（loop=xianyu_reply）
    └── xy_menu_default       通用客服（loop=xianyu_reply；no_reply 也落此）
```

- **静态邻接**：根节点 `sub_nodes` = 四个菜单节点；菜单节点无后继——
  执行器返回 `TurnResult(content=..., next=None)` 图自然终止；
- **路由轮不产文本**：`xianyu_router` 返回 `TurnResult(content="",
  next=<命中节点>)`，回复由命中的菜单节点产出；
- 无 stages 声明（AGENT 不跑 stages 管线；原 `xianyu_intent_nlu` /
  `xianyu_fixed_nlg` stage 已改写为执行器内部逻辑）；
- 意图级 NLG 模板在节点 `config["base_nlg_prompt"]`；拒绝话术以节点
  `answer_examples` 承载（原「短路标记」转为文案本体）。

### 路由执行器（`xianyu_router`，三层意图检测，tech 优先）

1. 轮首内联 `TimeAugQueryRewriter`（零 LLM）：相对时间（「明天下午3点」）
   改写为带绝对时间标注的消息，落 `cxt.rewritten_queries` 供分类与 NLG
   双方消费（原 pattern 骨架 query 槽的内联化）；
2. 本地 tech 关键词/正则（`detect_intent`，零 LLM）→ 本地 price 关键词
   （含「刀」「包个邮」等闲鱼语境砍价词）→ LLM 兜底（`XIANYU_NLU_PROMPT`，
   四类 price/tech/no_reply/default；无效输出回落 default）——no_reply
   覆盖提示词爆破/与商品无关的刷屏消息；
3. 意图回填当轮 user 消息 metadata；`_count_bargain_rounds` 回溯统计
   `intent=price` 条数，达到 `max_bargain_rounds`（默认 3，可经
   `metadata["bargain_settings"]` 注入）即改路由拒绝节点；
4. 议价参数（轮数/上限/折扣）经 filled_slots 注入 NLG prompt；路由决策写
   `cxt.nlu_result`（保持 `{next_node, intent, slots}` 形态，观测面零迁移）。

### 生成执行器（`xianyu_reply`）与规则执行器（`xianyu_rule_reply`）

- **no_reply → 空回复**（零 LLM）：渠道契约「空回复 = 不发送」；
- **议价拒绝 → 固定话术**（`xianyu_rule_reply`，零 LLM）：直接取节点
  `answer_examples[0]` 返回；
- **意图菜单 → LLM 生成**（`xianyu_reply`）：节点模板 + price 意图追加
  议价设置块（▲当前议价轮次）+ 「### 买家消息」独立段，单次调用后过
  `_safe_filter`（微信/QQ/支付宝/银行卡/线下 → 安全提醒）；生成走流式
  （emitter 附着时逐 chunk 转 delta）。

按意图调温（改写 llm_config 副本）：议价动态温度 `min(0.3+0.15×轮次, 0.9)`、
技术 0.4、通用 0.7，max_tokens 统一 500——复刻三个领域 Agent 的采样策略。

### 渠道声明（channel.py）

`XianyuChannel` 对接 xianyu-auto-reply 的「默认回复 API」：

- payload：`account_id / message / chat_id / item_id / send_user_* / msg_time`
  （ID 字段做长度 + 字符集校验，防 session_key 前缀碰撞伪造）；
- session：`session_key = account_id:chat_id`（卖家账号 × 会话维度）；
  `msg_time` 尽力解析（毫秒时间戳或常见日期串），过期 300s 的消息丢弃；
- 响应契约（对端 parse_api_reply 决定）：非 200 → 不发送（所有错误路径
  必须走非 200）；200 + 非空 `reply` → 发送；空 `reply` → 不发送；
  响应体不得携带 data/content/message 键（防调试信息泄露给买家）；
- 环境变量：`XIANYU_CHANNEL_PATTERN`（默认挂载的 pattern）、
  `XIANYU_CHANNEL_TOKEN`（鉴权 token）。

通用流程（token 校验/过期判断/会话 get-or-create/错误码）住在内核
`nexus/channels/webhooks.py`，本应用只做声明。

### 插件注册（route.py 底部，模块级注册，AST 扫描自动发现）

- `executor / xianyu_router`（意图检测 + 时间增强 + 议价计数路由）
- `executor / xianyu_reply`（意图模板生成 + 违禁词过滤）
- `executor / xianyu_rule_reply`（answer_examples 固定话术）

## 运行

```bash
# CLI 调试
python -m host.cli ask --pattern xianyu_agent --query "还在吗"
python -m host.cli ask --pattern xianyu_agent --query "便宜50卖吗"

# 服务端挂渠道（channel=xianyu 的 webhook 端点由内核通用装配提供）
XIANYU_CHANNEL_PATTERN=xianyu_agent uvicorn host.main:app --port 8000
```

意图路由 / 轮数控制 / 零 LLM 短路路径的验收测试随 `python -m pytest`
运行（`tests/test_xianyu_agent_route.py`，22 用例）。
