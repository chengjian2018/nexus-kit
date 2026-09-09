# 计划⑤ 改动记录：LLM 默认流式 + 引擎流式协议

日期：2026-09-09。测试 498 → 515 全绿（+9 provider/聚合 + 8 引擎流式）。

## 改动清单

### Provider 层
- 新增 `nexus/llm/types.py::LLMChunk`（text / tool_calls delta 片段 /
  finish_reason / usage）
- 新增 `nexus/llm/aggregate.py::collect_stream`：text 拼接、tool_calls
  按 index 合并（arguments **字符串增量拼接**）、finish/usage 末值
- `nexus/llm/provider.py`：
  - `chat_completion` 重写为 `collect_stream(_chat_completion_stream_impl(...))`
    ——非流式 = 聚合流式；`stream` 参数保留兼容但不再分流
  - `_chat_completion_impl` 角色变为默认流桥的后备（docstring 更新）
  - `chat_completion_stream` yield LLMChunk；默认桥把旧非流式结果包装为
    单 chunk
  - `ProviderEntry.chat_completion_stream` 类型同步
- `atoms/providers/openai_provider.py`：原生流升级——解析 delta.tool_calls
  片段（原实现丢弃！）、usage-only 尾包（choices=[] 特形）、finish_reason

### 引擎层
- 新增 `nexus/engine/streaming.py`：ChatStreamEvent（delta/round/done）+
  StreamEmitter（emit_delta/emit_round/drain）+ aggregate_turn
- `nexus/engine/chat.py`：`chat_turn_stream` generator 主体（发射器注入
  ExecutionContext + 每步 drain 转发）；`chat_turn` 变聚合包装；
  `_handle_module` 加 stream 参数
- `atoms/executors/loop_executor.py`：每轮经 `_stream_round` 流式消费
  （无发射器时纯聚合；duck-typed provider 无 chat_completion_stream 时
  回退 chat_completion）；round 事件四处（final/tool/transfer/max_rounds）

### host
- `POST /api/v1/chat/stream` SSE 调试端点（NEXUS_STREAM_DEBUG=1 门控
  挂载；同步 generator 占线程，非生产 API）

### 测试
- `tests/test_llm_streaming.py`（9）：聚合等价（与旧 dict 逐字段相等——
  本计划安全网）/ tool_calls 片段合成（串行+并行+混合文本）/ usage 尾包 /
  默认桥（真基类子类验证）
- `tests/test_chat_stream.py`（8）：事件序列 / done==chat_turn 等价 /
  工具轮 round 标记 / 旧 provider 回退 / SSE 端点（挂载+未挂载）

## 避坑记录（后续计划必读）

1. **tool_calls arguments 是字符串增量拼接**，不是 JSON 合并——chunk 里
  `{"ci` + `ty": "杭州"}` 拼 complete JSON，解析是消费端的事。测试
  test_tool_call_fragments_merge_by_index 钉死。
2. **usage 尾包 choices=[]**：include_usage 的最后一包没有 choices，
   按"有 usage 就发 chunk"处理（openai_provider 两处消费点）。
3. **_stream_round 的 duck-typing 回退**：`hasattr(provider,
   "chat_completion_stream")` 不存在→回退 chat_completion。这是存量测试
   桩（ScriptedProvider/FakeProvider）的存活路径，也是旧自定义 provider
   的兼容路径——计划⑥动 loop 时保留。
4. **发射器是 pull-drain 不是 push-callback**：executor 写 emitter，
   chat_turn_stream 在每个 _handle_module 后 drain 并 yield。保持纯函数
   组合、无线程。若未来 executor 内部再开并发（如并行工具），drain 点
   要重估。
5. **done 事件的 _finish 先 end_turn 再 build_chat_result**：会话不存在/
   模板缺失等早退分支也要走 _finish（保持 end_turn 的 history 一致性）
   ——除了 session 不存在（没有 cxt 可写）。
6. **SSE 端点在 turn_lock 内跑完整 generator**：与 _run_chat_turn_core
   同样的 per-session 串行化。reload 挂载（importlib.reload(host.main)）
   的测试模式会重跑模块级 discovery——副作用是 registry 幂等注册，
   已验证安全。
7. **nlg 延迟解析与流式无冲突**：_DeferredNLG 在 stage.execute 内解析，
   流式只覆盖 agent loop 的 LLM 轮；FSM/ROUTE 的 stage 内 LLM 调用
   （NLU/NLG/澄清）仍走 chat_completion（=聚合），未流式化——这是
   计划边界（stage 内部流式化留待后续）。
