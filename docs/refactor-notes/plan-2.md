# 计划② 改动记录：模型声明式重构

日期：2026-09-09。测试 494 → 493 全绿（test_stage_slots 重写 38→20 用例，
test_module_flag 重写 4→4，test_module_link 8→8；净数变化来自旧用例合并）。

## 改动清单

### 模型层（nexus/model/）
- `pattern.py`：stages 改 `List[Dict[str, Optional[str]]]`（构造期
  `normalize_skeleton` fail-fast）；删 generate/pre_recall/query/post_recall
  参数；messages_builder/agent_hooks 改 `Optional[str]`；modules=None 分支
  修复（kwargs setattr 移出 if）
- `module.py`：**ModuleLink dataclass 删除**，`sub_modules: List[Dict]`
  （`_normalize_links` 接受 dict/str）；stages: Dict[str, str]；删
  generate/pre_recall/query/post_recall/enable_clarify/clarify_stage；
  messages_builder/agent_hooks/agent_stage 改 `Optional[str]`
- `node.py`：stages: Dict[str, str]；删四显式槽位字段（base_nlu/base_nlg_
  prompt 为 prompt 资产，保留）

### 解析层（nexus/pipeline.py 重写）
- 删四个哨兵类（StageSlot/PreRecallSlot/QuerySlot/PostRecallSlot/
  GenerateSlot）与 `_GenerateNLUPart/_GenerateNLGPart/normalize_generate/
  resolve_stage/_layered_values`
- 新 API：`DEFAULT_SKELETON_SLOTS`（六槽）、`default_skeleton()`、
  `normalize_skeleton(stages)`（fail-fast）、`resolve_stage_code(slot, cxt,
  module, pattern, skeleton_value)`（三层声明查找）、
  `resolve_execution_sequence(cxt, module, pattern) -> List[(slot, stage)]`
- builtin 兜底：`builtin:generate:{type}#0/#1` 双元素 marker（nlu 取
  元素0、nlg 取元素1——两阶段默认是不同实例，unified 同实例）；clarify
  **无 builtin 兜底**（声明即启用，否则永不插入）
- **nlg 延迟解析**：`_DeferredNLG` 包装器在执行瞬间按当前节点解析
  （保留旧 GenerateSlot 拆分的 ROUTE 菜单时序修复——菜单级 nlg 同轮生效）
- register_default_generate/_clarify 公开签名不变，存储走插件中心
  （kind="stage_factory"，code=`generate:{module_type}`/`clarify`）

### 引擎层
- `chat.py`：`_run_stages` 消费 `resolve_execution_sequence`（slot+stage
  序列，替代逐 slot resolve_stage）；`_default_skeleton` 保留为锚返回
  `default_skeleton()`
- `messages.py`：投影块/借工具读 dict link；build_agent_messages 支持
  str code（插件中心 kind="messages_builder"）+ 内核注册 `default` +
  transitional callable 兼容
- `agent_hooks.py`：resolve_agent_hooks 支持 str code（kind="agent_hooks"）
  / callable / legacy dict 三形态
- `loop.py`：build_transfer_tools/_resolve_lent_tools 改读 dict link

### atoms/apps 迁移
- `atoms/stages/__init__.py`：新增 9 个具名 stage 注册（kind="stage"）：
  fsm_nlu/fsm_nlg/route_nlu/route_nlg/fsm_unified/route_unified/
  nlg_pass_through/time_aug_query/clarify_default
- `atoms/stages/unified.py`：_valid_next_values 的 clarify 判断改读
  module.stages
- `apps/xianyu_agent/route.py`：generate dict/query 对象 → stages 字符串
  声明 + app 自有 code 注册（xianyu_intent_nlu/xianyu_fixed_nlg）
- `apps/customer_agent/route.py`：messages_builder → str code
  （customer_agent_messages_builder，lambda 包装注册）；sub_modules → dict

### 测试改造（模式：对象内联 → 注册 stub code）
- 新增 `tests/stage_stubs.py::register_stage_stub`（唯一 code + 幂等注册）
- 重写：test_stage_slots（新骨架语义 20 用例）、test_module_link（dict）、
  clarify/test_module_flag（stages 声明）、clarify/test_wiring（骨架形状）
- 机械迁移：test_pattern_graph/test_module_jump/test_chat_reentry/
  test_agent_inject_transfer（ModuleLink→dict literal）、test_llm_refresh
  （R4 stub 注册）、test_unified_stage（stages codes + KB clarify code）、
  clarify/test_integration（KB clarify code）、test_agent_hooks_loop、
  test_time_aug_query、两个 route 测试断言

## 避坑记录（后续计划必读）

1. **builtin generate marker 是双元素**：`builtin:generate:ModuleType.FSM#0`
  取 (nlu, nlg) 元素0，`#1` 取元素1。当初 nlu/nlg 共用一个 marker 被
  unified 去重误杀（两阶段默认的 FSMNLU/FSMNLG 只跑了一个）。任何动
  builtin 兜底逻辑的计划（③校验、⑤流式）注意这个编码。
2. **clarify 无 builtin 兜底是有意的**：原 enable_clarify 默认 False =
  不插入。若给 clarify 加无条件兜底，所有 FSM 模块都会凭空跑
  ClarifyStage（test_clarify_next_node_rejected_when_disabled 会抓到）。
3. **nlg 延迟解析不能丢**：`_DeferredNLG` 在执行瞬间解析是为了 ROUTE
  菜单命中后菜单级 nlg 同轮生效（test_route_menu_node_nlg_same_turn_e2e
  / test_r4 钉死）。计划⑤流式化若改 _run_stages 结构，必须保留 nlg 槽位
  的执行时解析语义。
4. **normalize_skeleton 的 fail-fast 发生在 Pattern 构造期**：测试直接
  `pattern.stages = [对象]` 赋值不会触发（绕过构造），运行时
  resolve_execution_sequence 里 normalize 一样会炸且被 chat_turn 吞成
  "对话处理异常"——测试必须走构造参数声明。
5. **module.stages 是拷贝**：`dict(stages)` 防调用方 dict 后续变异泄漏。
  测试注入（如 clarify KB）用 `buy.stages = {**saved, "clarify": code}`
  并在 finally 恢复。
6. **messages_builder 注册的 factory 必须零参**：PluginRegistry 约定
  factory() 无参调用。customer_agent 的 builder 签名是 (module, cxt,
  extra_blocks)，注册时必须 `lambda: builder_fn` 包装——直接注册函数会
  TypeError。
7. **str 简写的 sub_modules 仍接受**（_normalize_links 自动包装 dict），
  但生产声明建议显式 dict（yml 序列化时形状一致）。
8. **旧字段已物理删除**：`module.generate/.enable_clarify/.clarify_stage/
  pattern.query` 等不再存在。任何残留访问（含测试 monkeypatch 属性赋值）
  是静默创建新属性、不生效——grep `\.\w*(generate|enable_clarify)` 验证。
9. **atoms.stages 的具名 codes 是全局命名空间**：app 自有 stage 用带前缀
  code（xianyu_intent_nlu）防撞。测试 stub 用 register_stage_stub 的
  自增唯一 code。
