# customer_agent — 店铺客服助手（Customer-Agent 迁移）

电商店铺客服 pattern，整装迁移自兄弟项目 Customer-Agent：商品/售后知识检索、
商品推荐卡片、超范围转人工。核心特点是**每轮商品目录预取**与**会话信息防编造**；
形态为**两节点 AGENT 图**——「AI 客服 → 人工交接」由条件边 + 回复末尾
`[HANDOFF]` 标记同轮完成，取代旧 defer/底座切换通道。

## 应用组成

| 文件 | 职责 |
|---|---|
| `route.py` | 全部内容：两节点 AGENT 图（两个自定义 executor + 迁移版 MessageBuilder）、Pattern 注册 |
| `__init__.py` | 包标记 |

## 架构

### 图结构（code = `customer_agent`，pattern_type = agent）

```
customer_agent (Pattern, entry: customer_service, allow_toolset: ["knowledge"])
├── customer_service  店铺客服（loop=customer_service_loop，主对话节点）
│     └── 条件边 next="human_handoff"
└── human_handoff     人工交接（loop=human_handoff_reply，is_end，无工具）
```

每条用户消息从 entry 跑全图（单节点自然终止，无需轮转状态）。

- **customer_service**：ReAct 工具循环（复用共享 `DefaultLoopExecutor`
  实例）挂 4 个 knowledge 工具（`search_product_knowledge` /
  `search_customer_service_knowledge` / `list_products` / `send_goods_link`）；
- **human_handoff**：无工具（deny-by-default），安抚话术后收尾。

### 转人工：`[HANDOFF]` 标记 + 同轮条件边

prompt 指示模型「需要人工时先把该说的话说完，回复末尾另起一行附加
`[HANDOFF]`」；`customer_service_loop` 执行器跑完循环后检测标记 →

1. 剥离标记（含空回复兜底话术），置 `cxt.metadata["handoff"]=True`；
2. 返回 `TurnResult(next="human_handoff")` **同轮**路由到交接节点；
3. `human_handoff_reply` 生成安抚话术后 `finally` 清 flag——单轮路由信号，
   不泄漏到下一轮；下一轮照常回到 customer_service。

防御：customer_service 入口发现残留 flag 直接路由 handoff。已知取舍：真流式
provider 下标记会短暂出现在实时 delta 流中（最终回复文本是干净的）。

### 工具授权（deny-by-default 三层收口）

4 个工具 toolset=`knowledge`；`pattern.allow_toolset=["knowledge"]` 是唯一
授权面；`customer_service.use_tools` 收窄到 4 个工具名，`human_handoff`
不声明（无工具）。

### 自定义 messages_builder（插件码 `customer_agent_messages_builder`）

迁移 Customer-Agent 的 MessageBuilder，契约 `(node, cxt, extra_blocks)`，
挂在 customer_service 节点层（node.plugins.messages_builder）：

1. 以 `default_build_messages` 为底（system + 跨轮历史 + 本轮 query 三段，
   hooks 片段随行；base_prompt 经 node.config 注入角色设定）；
2. system 末尾追加**【当前会话信息】块**——逐字段消毒 + `account_id` 取值指引，
   防止模型编造工具参数；
3. 每轮**预取商品目录**（直接调 `atoms/tools/knowledge_tool._handle_list_products`，
   不走 LLM），以 **user 角色不可信行**注入（外部内容不给指令权重）；
   预取失败/空目录静默跳过，绝不阻塞对话。

### 插件注册（route.py 底部，模块级注册，AST 扫描自动发现）

- `executor / customer_service_loop`（含 [HANDOFF] 检测与同轮路由）
- `executor / human_handoff_reply`（无工具安抚话术 + 清 flag）
- `messages_builder / customer_agent_messages_builder`

## 依赖与运行

- 依赖 `atoms/tools/knowledge_tool.py`（知识工具组）；演示数据经运营配置台
  「知识库」页录入（`/console`：商品/客服知识 CRUD + 试搜台）。
- 对话调试：`uvicorn host.main:app` 起服务后打开 studio「模版测试」页
  （`/studio`），选 `customer_agent` 发起多轮对话（SSE 流式，trace 逐行可见）。

- 离线验收测试随 `python -m pytest` 运行（fake provider 打桩 LLM；
  含 [HANDOFF] 同轮转人工与下一轮回主线的回归用例）。
