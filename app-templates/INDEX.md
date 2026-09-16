# 应用模板知识库（app-templates）

 nexus-kit 应用的结构模板库：每个条目是一个 Pattern 图 + 以描述占位的
插件（处理步骤卡 / 工具描述卡），**不含实现代码**。条目由
`nexus-app-template-skill` 产出与维护；实现者（人或 agent）照卡即可
落地代码。

结构校验（入库前必过）：

```bash
python nexus-app-template-skill/references/verify_template.py app-templates/<code>/TEMPLATE.md
```

| 模板 code | 名称 | 业务形态 | 图类型 | 来源 | 条目 |
|---|---|---|---|---|---|
| ppt_generator_agent | AI PPT 生成助手 | 外部 API 交付型对话：收集参数 → 逐轮确认 → 调用外部服务 → 交付产物链接 | fsm | 逆向自 apps/ppt_generator_agent | [TEMPLATE.md](ppt_generator_agent/TEMPLATE.md) |
| install_booking_agent | 安装预约外呼助手 | 外呼预约型对话：开场自报 → 地址/到货核对 → 上门时间协商（可约守卫）→ 确认收尾，改约/回拨/拒绝旁路 | fsm | 逆向自 apps/install_booking_agent | [TEMPLATE.md](install_booking_agent/TEMPLATE.md) |
| repair_booking_agent | 维修预约外呼助手 | 外呼预约型对话变体：时间协商守卫同安装版，确认后采集故障信息再挂机；守卫机器子类复用示范 | fsm | 逆向自 apps/repair_booking_agent | [TEMPLATE.md](repair_booking_agent/TEMPLATE.md) |
| customer_agent | 店铺客服助手 | 知识检索型客服：ReAct 工具循环答咨询/荐商品，[HANDOFF] 标记同轮转人工；messages_builder 目录预取范式 | agent | 逆向自 apps/customer_agent | [TEMPLATE.md](customer_agent/TEMPLATE.md) |
| deep_research | 深度研究助手 | 端到端研究管线：预检索 → 规划 → 按子问题扇出并行检索 → join 综合报告；引擎运行时 fan-out 验收配方 | agent | 逆向自 apps/deep_research_agent | [TEMPLATE.md](deep_research/TEMPLATE.md) |
| topic_research | 主题研究助手 | 端到端主题研究管线：规划按主题扇出检索，汇聚拆为零 LLM 合并/草稿/美化三站；长扇出流水线配方 | agent | 逆向自 apps/topic_research_agent | [TEMPLATE.md](topic_research/TEMPLATE.md) |
| archify | 图表工程助手 | 图表工程交付管线：选型 → 创作 → 验证⇄修复收敛闭环（stale-N 诚实出口）→ 交付 → 浏览器证据 → 感知评审 → 三级汇报；agent+修复回路纪律之源 | agent | 逆向自 apps/archify_agent | [TEMPLATE.md](archify/TEMPLATE.md) |
| archify_skill | 图表工程助手·技能版 | 说明书式技能执行：单节点 default_loop + use_skills 装载手册完成交付；最小应用范式 | agent | 逆向自 apps/archify_skill_agent | [TEMPLATE.md](archify_skill/TEMPLATE.md) |
| xianyu_agent | 闲鱼卖家客服助手 | 意图菜单型客服：每轮归根重跑，本地规则+LLM 兜底分类分发四个菜单节点，议价轮数控制走零 LLM 固定拒绝；路由式 agent 图范式 | agent | 逆向自 apps/xianyu_agent | [TEMPLATE.md](xianyu_agent/TEMPLATE.md) |
