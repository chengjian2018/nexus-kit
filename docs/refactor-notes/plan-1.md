# 计划① 改动记录：插件中心 + executor 插件化 + discovery 统一

日期：2026-09-09。测试基线 480 → 494（+14 插件测试）全绿。

## 改动清单

### 新增
- `nexus/registry/discovery.py`：共享 AST 扫描（`module_registers` /
  `import_modules`），收敛四 registry 各自复制的发现逻辑
- `nexus/registry/plugins.py`：PluginRegistry（kind/code/factory、冲突
  防护、实例缓存、has 不实例化）+ `discover_builtin_plugins()` 扫
  atoms/executors/
- `nexus/engine/execution.py`：ExecutionContext（cxt/pattern/module/
  force_close/stream）+ ModuleExecutor 契约
- `nexus/engine/turn_result.py`：TurnResult 独立模块（防 loop↔execution
  循环导入）。字段 `reply→content` 改名 + 新增 `extra` 开放扩展袋
- `atoms/executors/{__init__,loop_executor,fsm_executor,route_executor}.py`：
  三默认执行器（从 chat.py/loop.py 迁移编排主体，行为不变）
- `tests/test_plugins.py`：注册语义/冲突/幂等/缓存/默认码/解析链 14 用例
- `ARCHITECTURE.md`（本文件系列开始）+ 本记录

### 修改
- 四 registry（tools/providers/patterns/channels）：discovery 委托，删除
  各自复制的 `_is_registry_register_call`/`_module_registers_*`
- `nexus/engine/chat.py`：`_handle_module` 改插件分发（ModuleType 硬编码
  分派删除）；`_run_agent/_run_fsm/_run_route_pipeline` 迁 atoms；保留
  R1-R4/`_run_stages`/`_fsm_node_transition` 内核工具箱（patch 锚）
- `nexus/engine/loop.py`：run_agent 主体迁 atoms；保留 TurnResult re-export、
  兼容门面（构造 ec → resolve default_loop → execute）、工具箱
  （`_resolve_tools` 等 test_customer_agent_route import 锚）
- `nexus/engine/agents.py`：旧 AgentRunner Protocol 废弃，改过渡 re-export
  （docstring 声明插件中心取代 "no new global singletons" 旧约定）
- `nexus/pipeline.py`：register_default_* 内部存储改插件中心
  （kind="stage_factory"，code=`generate:{type}`/`clarify`），公开签名不变
- `nexus/model/{pattern,module}.py`：新增 executor 声明字段
  （pattern.executor_loop/fsm/route、module.executor，均 Optional[str]）
- `host/{main,cli}.py`：装配时 `discover_builtin_plugins()`
- `tests/conftest.py`：预热 `import atoms.executors`
- `pyproject.toml`：packages + atoms.executors
- 测试 patch 锚迁移（见下）

## 避坑记录（后续计划必读）

1. **patch 锚迁移**：`patch("nexus.engine.loop.build_provider")` 全部改为
   `patch("atoms.executors.loop_executor.build_provider")`（9 个测试文件，
   grep 清单化批量替换）。计划⑤改流式时 loop_executor 内的 provider 调用
   点还会变，届时同步迁锚。
2. **TurnResult.reply → content**：全仓机械替换（引擎 6 处 + 测试 24 处）。
   `grep -rn '\.reply\b'` 现仅剩 test_compression.py 的 FakeProvider 内部
   字段（无关）与 hooks 事件字段 `outcome="reply"`（事件枚举值，非
   TurnResult 字段，勿动）。
3. **循环导入**：TurnResult 必须放独立模块（turn_result.py）。loop.py 与
   execution.py 互相 import 会炸（executor 契约需要 TurnResult 类型，
   loop 兼容门面需要 ExecutionContext）。
4. **pattern 字段后缀是家族名**：`executor_loop/executor_fsm/executor_route`
   （不是 executor_agent）。`_resolve_executor_code` 里有
   `{"agent": "loop", ...}` 映射——ModuleType 值与 executor 家族名不同构，
   计划③ yml 序列化时保持这个字段名。
5. **executor 不持有 Session**：fsm/route executor 里 R3 刷新用
   `_refresh_llm_config_by_node(ec, module)` 的 _Shim 模式（session 只被
   读 pattern_code/cxt 两个字段）。计划②若动 Session 字段需同步检查。
6. **内核无兜底 executor**：注册表未预热 → KeyError 带指引。任何新测试
   只要走 chat_turn 全链路就必须 `import atoms.executors`（conftest 已做；
   独立子目录 conftest 若有需自查——clarify/ 子包目前没有独立 conftest）。
7. **PluginRegistry 幂等语义**：同 factory 对象重复注册幂等跳过；不同
   factory 占同 (kind,code) → ValueError。测试里写两个 lambda 是不同对象，
   幂等测试必须复用同一函数引用。
8. **pipeline.register_default_* 的 code 编码**：`generate:{module_type}`
   里的 module_type 是 ModuleType 枚举实例（repr 形如
   `ModuleType.FSM`），不是字符串值。 atoms.stages 注册时传的就是枚举，
   两边一致即可，但 yml/校验（计划③）若涉及需注意。
9. **`_FORCE_CLOSE_SUFFIX` 等框架强制项**留在 loop.py（公开别名
   `append_force_close_suffix`/`warn_prompt_length`），loop_executor 反向
   import。计划⑥删 transfer 时这些文案要同步改。
