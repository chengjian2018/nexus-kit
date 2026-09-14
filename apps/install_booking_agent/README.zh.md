# install_booking_agent — 安装预约外呼助手（手绘 FSM 转写）

家具/电器**外呼**场景的声明式 FSM pattern：客户刚购买商品，客服主动致电
预约师傅上门安装。助手永远是主叫方，流程转写自一张手绘对话流程草图（照片）：
开场 → 地址核对 → 到货确认 → 上门时间协商（推荐/具体日期/最近三路，
可约守卫校验）→ 时间确认（支持改约）→ 通话结束。

本应用是仓库中守卫机制最重的一个：**三处确定性零 LLM 守卫**保证
「模型永远无法对客户承诺一个排班上不存在的时间」。

## 应用组成

| 文件 | 职责 |
|---|---|
| `route.py` | 全部节点定义（16 个）+ FSM Pattern 注册（pattern_type=fsm）+ stages 骨架装配声明 |
| `stages.py` | 应用本地 stage：统一阶段子类（可约守卫/推荐改写/联系时间裁定）+ 关键词卡控 clarify + 插件注册 |
| `slots.py` | 纯函数槽位算术（排班解析/时间标注提取/可约匹配/档期推荐），零 LLM、零框架依赖 |
| `faq.py` | 外呼 FAQ 关键词表（费用/保修/安装时长/自装/改地址/催物流） |
| `prompts.py` | `INSTALL_UNIFIED_PROMPT`：外呼语义的统一阶段模板（任务信息 section + 特殊意图） |
| `__init__.py` | 包标记 |

## 架构

### Pattern 结构（code = `install_booking_agent`）

```
install_booking_agent (Pattern, pattern_type=fsm, entry: install_greet)
└── 16 节点单域 FSM（原单模块流程，节点/模块合并后直接挂 pattern）
```

主流程（`nodes[0]` / entry_node_code 为入口）：

```
install_greet 外呼开场
  └─ install_confirm_addr 地址核对 ──不一致──> install_end
       └─ install_check_arrival 到货确认
            ├─已到货─> install_ask_time
            └─未到货─> install_ask_eta 到货时间询问
                 ├─知道──> install_time_window 时间段询问
                 └─不知道─> install_available 方便确认
install_ask_time 上门时间协商（核心调度节点）
  ├─具体日期──> install_specific_date ─┐
  ├─最近──────> install_nearest ───────┤ 可约守卫校验
  └─都不知道──> install_recommend 档期推荐
install_confirm_time 时间确认 ──改约──> install_reschedule 改约重协商（回环）
install_end 通话结束语（is_end）
```

草图外补充节点：`install_decline` 通用拒绝承接（不想预约/已安装/质量问题/
退货/非本人，所有业务节点都有指向它的边）、`install_ask_callback` 下次联系
时间、`install_callback_default` 默认改约三天。

### stages 装配

```
pattern.stages  [{"query": "time_aug_query"}, {"nlu": "install_unified"},
                 {"clarify": "install_clarify"}, {"nlg": "nlg_pass_through"}]
node.stages     每节点 {"clarify": "install_clarify"}（统一阶段 admit "clarify"
                 的节点级开关；原模块级声明上提为逐节点声明）
```

- `time_aug_query`（内置，零 LLM）：把「明天下午3点」改写为带绝对时间标注的
  「明天下午3点(2026-09-10 15:00)」，在统一 prompt 与可约守卫**之前**生效；
- `install_unified`（应用本地）：`FSMUnifiedNLU` 子类，单次 LLM 调用产出
  reply/next_node/slots 后做三段确定性后处理；
- `install_clarify`（应用本地）：关键词卡控双轨 clarify；
- `nlg_pass_through`（内置）：统一阶段已生成回复，NLG 直通。

### 三段确定性守卫（stages.py，全部零额外 LLM）

1. **可约守卫**（`_apply_booking_guard`）：模型选了 install_specific_date /
   install_nearest 且话中带时间实体 → 从时间增强标注提取请求时间，
   与 `task_info["available_slots"]` 做包含匹配；可约 → 槽位标注放行；
   不可约 → **next_node 强制改道 install_recommend**（非法选择永远到不了节点图）。
   无排班注入时守卫不生效（opt-in）；callback 节点的时间是联系时间，跳过。
2. **推荐改写**（`InstallRecommendNLG`，插件码 `install_recommend_nlg`）：
   任何进入 install_recommend 的转移，回复都被确定性改写为真实排班的前几档。
   必须住在统一阶段而非节点级 NLG——FSM 节点转移在轮末触发，节点级 NLG
   只会在**下一轮**解析并砸掉当轮回复。
3. **联系时间裁定**（`_apply_callback_close`）：install_ask_callback 的答复
   按时间标注三分支——有标注（未来两周内有效）→ 直接收尾复述客户时间；
   无标注（太远/过去/没给）→ 改道 install_callback_default 播报默认 3 天后
   再联系。「有标注 vs 无标注」即分支决策（time_aug 只标注两周内未来时间），
   无需第二层解析。

### 关键词卡控 clarify（`KeywordClarifyStage`，插件码 `install_clarify`）

客户在流程中问业务外问题（「安装要钱吗」）时：统一阶段输出
`next_node="clarify"` + topic/keywords 槽位 → clarify stage 用**纯关键词包含**
匹配 FAQ 表（specific-first，命中即 kb 轨、未中即 fallback 轨，无混合区），
kb 模板预填 FAQ 答案（模型只做口语化承接 + 拉回主线，不重新回答），
fallback 轨诚实告知稍后核实 + 拉回。电话节奏：每轮两句话以内。

### task_info 契约（launch 层注入）

`product_name / address / user_name / order_id / available_slots`
（`"YYYY-MM-DD HH:MM-HH:MM"` 数组，师傅可约窗口）。无 available_slots
则守卫整体不生效。

### 插件注册（stages.py 底部，kind="stage"）

`install_unified` / `install_recommend_nlg`（独立可挂载的 stage 码）/
`install_clarify`（工厂注册，占位 recaller）。

## 运行

```bash
# 对话调试：uvicorn 起服务后打开 studio「模版测试」页（/studio），选
# install_booking_agent；守卫 opt-in——available_slots 在页面 task_info 框
# 填入后生效：
#   {"product_name": "衣柜", "address": "XX路1号", "user_name": "张三",
#    "available_slots": ["2026-09-11 09:00-12:00", "2026-09-12 14:00-17:00"]}
uvicorn host.main:app --port 8000
```

守卫/改道/裁定路径的验收测试随 `python -m pytest` 运行（测试经
`metadata.time_base` 注入可复现时间基准）。
