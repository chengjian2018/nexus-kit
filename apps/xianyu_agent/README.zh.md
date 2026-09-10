# xianyu_agent — 闲鱼卖家客服助手

复刻 tmp_xianyu.XianyuReplyBot 的闲鱼卖家客服 pattern：对每条买家消息做
**每轮独立的意图检测**（本地规则优先 + LLM 兜底），分发到议价/技术/通用
菜单生成回复，附议价轮数控制、按意图调温与违禁词过滤。本应用独有的
**渠道声明**（channel.py）使其可直连 xianyu-auto-reply 的默认回复 API。

## 应用组成

| 文件 | 职责 |
|---|---|
| `route.py` | 本地意图规则、`XianyuIntentNLU` / `FixedNLG` 两个 stage、RouteModule 节点图、Pattern 注册 |
| `channel.py` | `XianyuChannel` 声明式渠道（ChannelSpec）：payload 契约、session 派生、响应契约 |
| `prompts.py` | 四段 prompt 资产：意图分类 + 议价/技术/通用三个 NLG 模板 |
| `__init__.py` | 包标记 |

## 架构

### Pattern 结构（code = `xianyu_agent`，ROUTE 执行器）

```
xianyu_agent (Pattern, entry: xianyu_root)
└── xianyu_root  闲鱼总路由（RouteModule，节点不出模块）
    ├── xy_route_root        路由根（sub_nodes = 意图菜单）
    ├── xy_menu_price        议价菜单（未达轮数上限）
    ├── xy_menu_price_refuse 议价拒绝（达上限 → 固定话术，零 LLM）
    ├── xy_menu_tech         技术问答菜单
    └── xy_menu_default      通用客服菜单（no_reply 也落此，NLG 短路空回复）
```

stages 装配：

```
pattern.stages  [{"query": "time_aug_query"}, {"nlu": None}, {"nlg": None}]
module.stages   {"nlu": "xianyu_intent_nlu", "nlg": "xianyu_fixed_nlg"}
```

### NLU：三层意图检测（`XianyuIntentNLU`，插件码 `xianyu_intent_nlu`）

复刻 IntentRouter 的三级策略，**tech 优先**（金额与技术词并存归 tech）：

1. 本地 tech 关键词/正则（`detect_intent`，零 LLM）；
2. 本地 price 关键词/正则（含「刀」「包个邮」等闲鱼语境砍价词）；
3. LLM 兜底（`XIANYU_NLU_PROMPT`，四类 price/tech/no_reply/default；
   无效输出回落 default）——no_reply 覆盖提示词爆破/与商品无关的刷屏消息。

议价轮数控制：NLU 把 intent 回填到当轮 user 消息的 metadata，
`_count_bargain_rounds` 回溯统计历史中 `intent=price` 的条数；
达到 `max_bargain_rounds`（默认 3，可经 `metadata["bargain_settings"]` 注入）
即路由到拒绝菜单。议价参数（轮数/上限/折扣）经 filled_slots 注入 NLG prompt。

### NLG：三条路径（`FixedNLG`，插件码 `xianyu_fixed_nlg`）

1. **no_reply → 空回复**（零 LLM）：渠道契约「空回复 = 不发送」，
   复刻原实现的 "-" 返回；
2. **议价拒绝 → 固定话术**（零 LLM）：拒绝节点的 answer_examples 携带
   标记文本，命中即短路输出固定拒绝语（比原实现的动态温度软守线更可测）；
3. **意图菜单 → LLM 生成**：节点级 `base_nlg_prompt`（意图模板）+
   price 意图追加议价设置块（▲当前议价轮次）+ 买家消息独立 section，
   单次调用后过 `_safe_filter`（微信/QQ/支付宝/银行卡/线下 → 安全提醒）。

按意图调温（`_tuned_llm_config`，改写 llm_config 副本）：
议价动态温度 `min(0.3 + 0.15×轮次, 0.9)`、技术 0.4、通用 0.7，
max_tokens 统一 500——复刻三个领域 Agent 的采样策略。

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

## 运行

```bash
# CLI 调试
python -m host.cli ask --pattern xianyu_agent --query "还在吗"
python -m host.cli ask --pattern xianyu_agent --query "便宜50卖吗"

# 服务端挂渠道（channel=xianyu 的 webhook 端点由内核通用装配提供）
XIANYU_CHANNEL_PATTERN=xianyu_agent uvicorn host.main:app --port 8000
```

意图路由 / 轮数控制 / 短路路径的验收测试随 `python -m pytest` 运行。
