"""plugins 合并声明字段 — Pattern 与 BaseModule 共用的插件声明 dict。

把原先散落的独立字段合并为一个 ``plugins: Dict[str, str]``（pattern 层的
executor_loop / executor_fsm / executor_route / messages_builder /
agent_hooks，module 层的 messages_builder / agent_hooks）——与 stages 的
dict 形态同款：槽位名 → 插件 code，纯声明式、可序列化、构造期 fail-fast。

槽位表（槽位名 → 插件中心 kind）：

=================  ===================  =================================
槽位               kind                 说明
=================  ===================  =================================
loop               executor             AGENT 族 executor（旧 executor_loop）
fsm                executor             FSM 族 executor（旧 executor_fsm）
route              executor             ROUTE 族 executor（旧 executor_route）
messages_builder   messages_builder     AGENT 消息构建器
agent_hooks        agent_hooks          agent loop hooks 包
=================  ===================  =================================

解析链（与 stages 的三层同型，module 层压 pattern 层）：

- executor：``module.executor``（类型无关直配，最高）>
  ``module.plugins[family]`` > ``pattern.plugins[family]`` > 类型默认码
  （family = loop/fsm/route，由模块类型映射；见 chat 层
  ``_resolve_executor_code``）
- messages_builder / agent_hooks：``module.plugins[key]`` >
  ``pattern.plugins[key]`` > 内核默认（messages_builder 注册码
  "default"）/ 空直通（agent_hooks）

兼容：旧独立字段仍是合法构造参数（折入 dict，同名槽位以 dict 值优先），
并以只读 property 继续可读（``pattern.executor_loop`` 等）——存量声明、
消费端 getattr、yml 旧形状全部无感；序列化只输出规范的 plugins 形态。
"""

from typing import Any, Dict, Optional

# 槽位名 → 插件中心 kind（新扩展点加一行即可）
PLUGIN_KINDS: Dict[str, str] = {
    "loop": "executor",
    "fsm": "executor",
    "route": "executor",
    "messages_builder": "messages_builder",
    "agent_hooks": "agent_hooks",
}

# executor 族槽位（pattern 层旧字段名 executor_<family> 的 family 部分）
EXECUTOR_FAMILY_SLOTS = ("loop", "fsm", "route")

# 允许 transitional 内联 callable 的槽位（messages.build_agent_messages /
# agent_hooks.resolve_agent_hooks 判断 callable 直调——与旧独立字段时代的
# transitional 语义一致；executor 族只收 str code）
_CALLABLE_OK = {"messages_builder", "agent_hooks"}


def plugins_slot_label(slot: str) -> str:
    """槽位名的报错标签：executor 族带旧字段前缀（executor_fsm 等），
    其余用槽位名本身。"""
    return f"executor_{slot}" if slot in EXECUTOR_FAMILY_SLOTS else slot


def normalize_plugins(plugins: Optional[Dict[str, Any]],
                      legacy: Optional[Dict[str, Any]] = None,
                      ) -> Dict[str, Any]:
    """规范化 plugins 声明：合并 legacy 独立字段 + 结构 fail-fast。

    Args:
        plugins: dict 形声明（槽位名 → str code / None / transitional
          callable）。同名槽位以 dict 值为准（dict 是新范式的权威载体）。
        legacy: 旧独立字段的名值对（构造参数收进来后传入）；仅填补 dict
          中缺失的槽位（值为 None 的跳过）。

    Raises:
        ValueError: 未知槽位名，或值类型非法（executor 族只收 str/None；
            messages_builder/agent_hooks 额外收 callable）。
    """
    merged: Dict[str, Any] = dict(plugins or {})
    for slot, value in (legacy or {}).items():
        if value is None:
            continue
        merged.setdefault(slot, value)

    for slot, value in merged.items():
        if slot not in PLUGIN_KINDS:
            raise ValueError(
                f"plugins 槽位名非法: {slot!r}"
                f"（合法: {sorted(PLUGIN_KINDS)}）"
            )
        if value is None or isinstance(value, str):
            continue
        if callable(value) and slot in _CALLABLE_OK:
            continue  # transitional 内联 callable（同旧独立字段语义）
        raise ValueError(
            f"plugins[{slot!r}] 的值必须是 str/None"
            f"（{slot!r} 另收 transitional callable）: {value!r}"
        )
    return merged
