# 计划④ 改动记录：hooks 清理 + 默认置空

日期：2026-09-09。测试 516 → 498 全绿（-35 行为测试 +13 契约 +4 迁移）。

## 改动清单

### 删除
- `tests/test_agent_hooks.py`（15 用例：声明解析/分发器链式/守卫）
- `tests/test_agent_hooks_loop.py`（20 用例：P1-P7 行为集成）——其中 4 个
  非 hooks 主Flow 用例**迁移保留**（见下）

### 新增
- `tests/test_agent_hooks_contract.py`（13 用例）：HOOK_POINTS 清单 /
  声明解析三形态（str code / callable / legacy dict）+ 降级 / fire 吞异常 /
  空 hooks 直通返回原值（collect/rewrite×2）/ 事件类字段形状
- `tests/test_loop_tool_guards.py`（4 用例，迁移）：幻觉工具名拦截回填、
  ACL 越权拦截、合成错误行回放配对、普通轮回放配对——这些是 loop 主Flow
  的确定性校验/回放行为，不属于 hooks

### 修改
- `nexus/engine/agent_hooks.py`：docstring 重写——删 `docs/plans/2026-09-04-
  agent-loop-hooks.md` 死引用（该目录不存在），压缩为"状态声明 + 点位表
  + 声明形态 + 错误语义 + 边界"五段；代码零改动（机制保留）
- loop 调用点（atoms/executors/loop_executor.py）不动：P1-P7 照常调用，
  无声明时零开销直通（这正是受测契约）

## 语义说明

- **"默认置空" = 无 in-repo hooks 包**：resolve_agent_hooks 在无声明时
  返回空 map，所有分发器对空 map 是纯直通。机制（事件类/分发签名/解析）
  完整保留，恢复实现只需注册 kind="agent_hooks" 插件包。
- **行为测试的归宿**：git 历史保留完整行为套件（本 commit 的父提交）。
  恢复实现时应连带恢复行为测试（从历史 cherry-pick 后适配 str 声明形态）。

## 避坑记录（后续计划必读）

1. **P4/P5 链式改写语义复杂但已无消费者**：契约测试只钉签名与直通。
  计划⑥动 _dispatch_tool_calls（删 transfer 分支）时，hooks 参数
  （hooks/allowed_names/lent_by）保留原样传递——空 map 下全直通，
  不影响删除语义。
2. **TRANSFER_TOOL_PREFIX 与 P4 守卫耦合**：rewrite_tool_call 的
  reserved_prefix 参数防改名走私 transfer 前缀。计划⑥删 transfer 时
  此守卫随 transfer 分支一并处理（届时 prefix 常量去留要在⑥内决策）。
3. **迁移用例的工具名换了前缀**（hook_* → guard_*）并独立 toolset
  （test_loop_guards），避免与未来恢复的 hooks 测试注册撞名。
4. **extra_blocks 契约仍被 messages builder 钉住**：build_system_prompt
  的 extra_blocks 参数与 P1 fragments 的管道保留——契约测试钉了
  collect_fragments 对空 map 返回 []，builder 端无需改。
