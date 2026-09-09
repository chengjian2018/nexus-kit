# 计划⑥ 改动记录：跳转/投影重构 + README 收尾

日期：2026-09-09。测试 515 → 519 全绿（transfer 套件 12 删除，新
test_projection_defer 14 + reentry 重写 6 + 保留净增）。

## 改动清单

### 模型/上下文层
- `nexus/model/module.py`：新增 `enable_project: bool = True`（子模块声明
  投影服务 or 跳转目标；默认投影——transfer 删除后投影是唯一有默认行为
  的分支）
- `nexus/context.py`：新增 `DeferredModuleSwitch` dataclass（轮末底座切换
  事件，to_dict 观察形态 `{"module_switch": ...}`）；ModuleJumpEvent 保留
- `nexus/engine/context_lifecycle.py`：`forced_projection` 归入
  PERSISTENT_METADATA_KEYS（跨轮保留）
- `nexus/model/serialization.py`：_MODULE_FIELDS + enable_project
- `nexus/model/validation.py`：投影边一致性（enable_project=True 的边须
  lend_knowledge 或 lend_tools）

### 引擎层
- `nexus/engine/chat.py`：
  - `_effective_enable_project(module, cxt)`（声明 or forced_projection）
  - `_record_forced_projection(cxt, code)`（幂等记录）
  - `_apply_deferred_switch(session, pattern)`：轮末消费（hop 后、end_turn
    前）：改 current_module_code、清 node、记 forced、事件 re-append 保
    观察可见；幻觉目标仅告警保持原位
  - `ModuleJumpChannel.reroute` 跳转时也记录 forced_projection（源模块）
- `nexus/engine/loop.py`：**transfer 工具族删除**（build_transfer_tools /
  TRANSFER_TOOL_PREFIX / _transfer_reason）；_dispatch_tool_calls 的
  error-backfill 分支改按 `_DEFER_TOOL_NAME` 判断
- `atoms/executors/loop_executor.py`：
  - `_build_defer_tool(module, pattern, cxt)`：从**有效投影邻接**生成单一
    通用 `defer_to_module` 工具（enum 限定投影目标；无投影邻接时不生成；
    force_close 不生成）
  - defer 分支：本轮**继续回答**（不像 transfer 静默）——defer 登记后
    `continue` loop，模型下一轮正常出文本；同响应的普通工具调用真实执行；
    幻觉目标错误回填继续 loop
  - `_projection_targets`：有效投影目标集合（enable_project or forced）
- `nexus/engine/messages.py`：
  - AGENT_TEAM_RULES_PROMPT / AGENT_PROJECTION_RECALL_PROMPT 重写为
    defer 语义（"登记切换，本轮继续回答"）
  - build_projection_block：按有效 enable_project 过滤（False 目标不投影）；
    加 cxt 参数；投影块加 defer 指引行
- `nexus/visualize.py`：邻接边区分样式（`..>|投影·defer|` vs
  `-.->|跳转目标|`）

### 业务迁移
- `apps/customer_agent/route.py`：human_handoff 邻接 = 投影 + 延迟切换
  （不再 transfer_to_human_handoff）；handoff base_prompt 措辞更新

### 测试
- 删 `tests/test_agent_inject_transfer.py`（12 用例，git 历史可恢复）
- 新 `tests/test_projection_defer.py`（14）：投影块过滤（含
  forced）/defer 工具生成/defer 事件与继续回答/轮末切换/幻觉回填×2/
  force_close 无 defer/保留的 lent 工具·回放配对·回看块用例
- 重写 `tests/test_chat_reentry.py`（6）：ROUTE 同轮跳转/force_close×2/
  **两轮 defer 底座切换 e2e**/防乒乓/事件 json 导出
- test_customer_agent_route：工具断言 transfer→defer

## 避坑记录

1. **不得 mutate Pattern/Module 单例**：forced_projection 只走
  cxt.metadata（模块对象跨会话共享，运行时改字段会泄漏到其它会话）。
  测试里想临时改 enable_project 也要走 metadata 或构造新对象。
2. **defer 与 transfer 的根本差异**：defer 后 loop `continue`——本轮
  必须继续回答（transfer 是 return TurnResult() 静默）。defer 分支不写
  suppressed 元数据；defer 工具行是普通 ack 行（非 synthetic）。
3. **_apply_deferred_switch 的时序**：hop 循环之后、end_turn 之前——
  end_turn 的 history append 与 store.save_snapshot 必须看到切换后的
  current_module_code（测试 test_defer_end_of_turn_switch_next_turn_base
  的轮2直接单次 LLM 调用钉死了这一点）。
4. **事件消费后 re-append**：DeferredModuleSwitch 被 pop 消费后 re-append
  回 actions，让 build_chat_result 的快照可见（观察通道）。ModuleJumpEvent
  是消费即移除（不同！jump 在 ChatResult 里只有 hop 耗尽残留才可见）。
5. **_dispatch_tool_calls 的 transfer_error 语义泛化**：参数名未改
  （transfer_error），但现在按 `== _DEFER_TOOL_NAME` 匹配。改名会动
  4 个调用点+2 个测试，收益低，留作后续清理。
6. **hooks P6（on_transfer）复用**：defer 分支仍 fire on_transfer/
  outcome="transfer"——机制名是移交史，事件内容是 defer。hooks 默认
  no-op（计划④）不影响。
7. **投影块过滤依赖 cxt**：build_projection_block 签名加了 cxt=None
  （向后兼容 None=不做 forced 过滤）。直接调用（无 cxt）时只按声明过滤。
8. **CLI 两轮验收脚本**（投影代答→底座切换→次轮新底座）在 plan-6 记录
  里，可复跑；注意 ScriptedProvider 桩无 chat_completion_stream 会走
  _stream_round 的 duck-typing 回退（计划⑤避坑 #3）。
