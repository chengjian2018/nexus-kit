# customer_agent — 店铺客服助手（Customer-Agent 迁移）

电商店铺客服 pattern，整装迁移自兄弟项目 Customer-Agent：商品/售后知识检索、
商品推荐卡片、超范围转人工。核心特点是**每轮商品目录预取**与**会话信息防编造**，
以及经邻接投影实现的「AI 客服 → 人工交接」模块切换。

## 应用组成

| 文件 | 职责 |
|---|---|
| `route.py` | 全部内容：两个 AgentModule、Pattern 注册、迁移版 MessageBuilder（messages_builder 插件） |
| `__init__.py` | 包标记 |

## 架构

### Pattern 结构（code = `customer_agent`）

```
customer_agent (Pattern, entry: customer_service)
├── customer_service  店铺客服（AgentModule，主对话底座）
└── human_handoff     人工交接（AgentModule，is_end）
```

- **customer_service**：挂 5 个工具（`search_product_knowledge` /
  `search_customer_service_knowledge` / `list_products` / `send_goods_link`），
  是唯一被 ACL 授权访问 knowledge_tool 工具组的 pattern；
  `sub_modules=[{"target": "human_handoff", "lend_knowledge": True}]` 声明邻接投影——
  人工交接范围内的问题本轮由 customer_service 带着投影知识代答，
  真需要人工时 `defer_to_module` 轮末切换底座（无同轮 transfer）。
- **human_handoff**：`is_end` 终态模块，告知买家问题已登记、人工将接入，
  每轮直接回应不再移交。

### 自定义 messages_builder（插件码 `customer_agent_messages_builder`）

迁移 Customer-Agent 的 MessageBuilder，组装顺序：

1. 以 `default_build_messages` 为底（system + 跨轮历史 + 本轮 query 三段，
  hooks 片段随行）；
2. system 末尾追加**【当前会话信息】块**——逐字段消毒 + `account_id` 取值指引，
  防止模型编造工具参数；
3. 每轮**预取商品目录**（直接调 `atoms/tools/knowledge_tool._handle_list_products`，
  不走 LLM），以 **user 角色不可信行**注入（外部内容不给指令权重）；
  预取失败/空目录静默跳过，绝不阻塞对话。

### 插件注册

- `messages_builder / customer_agent_messages_builder`（文件底部，模块级注册，
  与 pattern 注册同一 idiom，AST 扫描自动发现）

## 依赖与运行

- 依赖 `atoms/tools/knowledge_tool.py`（知识工具组）；演示前需灌知识库：

```bash
python -m host.cli knowledge-seed                    # 前置：灌演示知识库
python -m host.cli ask --pattern customer_agent --query "这个阅读器电池怎么样"
```

- 离线验收测试随 `python -m pytest` 运行（fake provider 打桩 LLM）。
