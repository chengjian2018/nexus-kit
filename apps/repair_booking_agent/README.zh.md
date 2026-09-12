# repair_booking_agent — 维修预约外呼助手（安装场景变体）

家具/电器**报修**外呼场景的 FSM pattern：客户商品坏了，客服主动致电预约
师傅上门维修。与 [install_booking_agent](../install_booking_agent/README.zh.md)
同为外呼语义（助手是主叫方），**守卫机制全部经子类复用，只重绑节点码与措辞**，
是「apps 层跨包复用应用机制」的示范。

## 与安装场景的三大业务差异

1. **无到货子树**——客户已持有商品（到货确认/到货时间/时间段/方便确认四个
   节点全部去掉），地址核对后直接进入时间协商；
2. **拒绝意图收窄**——去掉质量投诉/退货出口（商品质量问题在这里**就是**
   维修诉求本身，不是退出通道），新增已自行修好/已找别人修过；
3. **确认后不挂机**——上门时间确认后继续采集故障信息
   （师傅要带对配件工具），复述确认后才收尾。

## 应用组成

| 文件 | 职责 |
|---|---|
| `route.py` | 全部节点定义（14 个）+ FSM Pattern 注册（pattern_type=fsm）+ stages 骨架装配声明 |
| `stages.py` | 三个子类：重绑节点码/措辞的统一阶段、推荐 NLG、关键词 clarify + 插件注册 |
| `faq.py` | 维修 FAQ 关键词表（费用/保修/维修时长/配件/自修咨询…，问题族替换为维修语境） |
| `prompts.py` | `REPAIR_UNIFIED_PROMPT`：维修场景的外呼统一模板（无到货环节 + 故障采集 section） |
| `__init__.py` | 包标记 |

## 架构

### Pattern 结构（code = `repair_booking_agent`）

```
repair_booking_agent (Pattern, pattern_type=fsm, entry: repair_greet)
└── 14 节点单域 FSM（原单模块流程，plan-⑧ 节点/模块合并后直接挂 pattern）
```

主流程：

```
repair_greet 外呼开场
  └─ repair_confirm_addr 地址核对 ──不一致──> repair_end
       └─ repair_ask_time 上门时间协商（无到货环节，直入）
            ├─具体日期──> repair_specific_date ─┐
            ├─最近──────> repair_nearest ───────┤ 可约守卫校验
            ├─都不知道──> repair_recommend 档期推荐
            └─现在没空─> repair_ask_callback 下次联系时间
repair_confirm_time 时间确认 ──改约──> repair_reschedule 改约重协商
  └─（确认后不挂机）repair_ask_fault 故障信息询问
       └─ repair_confirm_fault 故障信息确认（两拍收尾）
            └─ repair_end 通话结束语（is_end）
```

补充通道：`repair_decline` 通用拒绝承接（不想维修/已自修/已找别人修/非本人）、
`repair_ask_callback` + `repair_callback_default`（下次联系/默认改约三天）。

### stages 装配

```
pattern.stages  [{"query": "time_aug_query"}, {"nlu": "repair_unified"},
                 {"clarify": "repair_clarify"}, {"nlg": "nlg_pass_through"}]
node.stages     每节点 {"clarify": "repair_clarify"}（统一阶段 admit "clarify"
                 的节点级开关；原模块级声明上提为逐节点声明）
```

与安装应用逐槽相同（query 时间增强 → 统一阶段 → 关键词 clarify → 直通 NLG）。

### 子类复用（stages.py，零机制重实现）

| 类 | 插件码 | 继承自 | 重绑内容 |
|---|---|---|---|
| `RepairBookingUnifiedNLU` | `repair_unified` | `InstallBookingUnifiedNLU` | 节点码五常量（BOOKING_TARGETS / RECOMMEND_NODE / CALLBACK_NODE / CALLBACK_DEFAULT_NODE / END_NODE → repair 节点图）+ 确定性回复措辞（「维修师傅档期排不开了」等） |
| `RepairRecommendNLG` | `repair_recommend_nlg` | `InstallRecommendNLG` | 推荐话术措辞（维修档期） |
| `RepairKeywordClarifyStage` | `repair_clarify` | `KeywordClarifyStage` | FAQ 匹配器（维修表）、kb/fallback 模板、生成失败兜底话术 |

可约守卫 / 排班驱动推荐改写 / 联系时间三分支裁定 / 关键词卡控 clarify
的执行路径全部走 install 父类——守卫机制本身与节点图无关
（apps 层跨包 import，importlinter 分层契约不受影响）。

### task_info 契约

与安装应用同形：`product_name / address / user_name / order_id /
available_slots`（师傅可约窗口）。

## 已知刻意简化

- 故障信息只做口径采集（`fault_description` 槽位），不接诊断知识库；
  客户说不清时留在故障询问节点继续引导，不设 clarify 分支；
- 无商品质量类拒绝出口（质量诉求就是维修诉求本身）。

## 运行

```bash
# CLI 调试；task_info（含 available_slots）经 --task-info 注入，同安装应用
python -m host.cli ask --pattern repair_booking_agent --query "是的我，冰箱不制冷了" \
  --task-info '{"product_name": "冰箱", "address": "XX路1号", "user_name": "李四",
                "available_slots": ["2026-09-11 09:00-12:00"]}'
```

守卫复用路径的验收测试随 `python -m pytest` 运行。
