"""Pattern → Module(s) 转换（声明式，纯数据变换）。

把一个已有 pattern 收敛为可直接嵌入其它 pattern 的 Module 集——典型用法：
一个独立运行的 pattern 要作为更大 pattern 的能力模块复用时，经
:func:`pattern_to_modules` 拿到等价声明（模块列表，头模块居首）。

规则（全部按 Pattern 与 BaseModule 的字段定义做映射）：

- **整组随行**：pattern 的全部模块都转换（拓扑不丢——模块间
  ``sub_modules`` 邻接边与节点 ``jump_module`` 跳转目标都在组内，
  宿主构造期图校验天然通过）。每个模块经
  ``serialization.module_to_dict → _module_from_dict`` 走一次声明式
  round-trip（deepcopy）——产物是全新对象，不与源 pattern 共享引用。
- **头模块 = 入口模块**：承接 pattern 身份（``module_code = pattern.code``、
  ``module_name = pattern.name``、``module_description =
  pattern.description``，入参可显式覆盖）。头模块 code 变化时，组内指向
  旧 code 的邻接边与节点跳转一并改写，图保持一致。
- **pattern 属性分发给全部模块**（依赖统一 plugins 合并字段）：pattern
  层的 executor 族 / messages_builder / agent_hooks 声明折入**每个**模块
  的空槽，模块自己声明的槽位优先（与运行时解析链 module > pattern
  同序）——转换后在没有任何同级声明的宿主 pattern 里行为不变。模块的
  ``executor`` 直配字段（类型无关、最高优先级）原样随行。

不随行（Module 层无对应物，宿主 pattern 自行声明）：

- ``pattern.stages`` 槽位骨架（module 只有 ``stages`` 槽位覆盖表，已随行）
- ``max_hops`` 跳数预算
"""

import copy
import logging
from typing import Any, List, Optional

from nexus.model.module import BaseModule
from nexus.model.serialization import _module_from_dict, module_to_dict

logger = logging.getLogger(__name__)


def pattern_to_modules(
    pattern: Any,
    *,
    module_code: Optional[str] = None,
    module_name: Optional[str] = None,
    module_description: Optional[str] = None,
) -> List[BaseModule]:
    """把一个 pattern 转换为可嵌入的 Module 集（声明式，见模块 docstring）。

    Args:
        pattern: 源 Pattern 对象（入口模块必须可解析）。
        module_code: 头模块 module_code 覆盖（默认 ``pattern.code``，再回退
            入口模块自己的 module_code；改名时组内指向旧 code 的边与
            跳转一并改写）。
        module_name: 头模块 module_name 覆盖（默认 ``pattern.name``，回退
            同上）。
        module_description: 头模块 module_description 覆盖（默认
            ``pattern.description``，回退同上）。

    Returns:
        转换后的模块列表，**头模块（源入口模块）居首**，其余按源声明序；
        宿主 pattern 以 ``entry_module_code=result[0].module_code`` 接入。

    Raises:
        ValueError: 入口模块不可解析（entry_module_code 为空或不在
            module_map 中）。
    """
    head = None
    if pattern is not None:
        head = pattern.module_map.get(
            getattr(pattern, "entry_module_code", ""))
    if head is None:
        raise ValueError(
            f"pattern {getattr(pattern, 'code', '?')!r} 的入口模块不可解析"
            f"（entry_module_code="
            f"{getattr(pattern, 'entry_module_code', None)!r}）"
        )

    # 头模块身份：pattern 身份（module 代表整个 pattern），入参可覆盖。
    # code 变化时需要改写组内指向旧 code 的引用，保持图一致
    new_head_code = (module_code or getattr(pattern, "code", None)
                     or head.module_code)
    code_rewrite = ({head.module_code: new_head_code}
                    if new_head_code != head.module_code else {})

    # pattern 层属性：分发给全部模块的 plugins 声明
    pattern_plugins = dict(getattr(pattern, "plugins", None) or {})

    def _convert(module: Any) -> BaseModule:
        # 声明式深拷贝：module（含节点树）→ dict → 新实例，零对象引用。
        # deepcopy 是必须的：module_to_dict 对 list/dict 字段（use_tools /
        # stages / node_slots 等）透传引用，浅 round-trip 会与源共享可变
        # 状态（transitional callable 对 deepcopy 是原子，原样保留）
        data = copy.deepcopy(module_to_dict(module))

        # plugins 折入：pattern 层声明填空槽，模块已声明的槽位优先
        # （与运行时解析链 module > pattern 同序）
        folded = dict(pattern_plugins)
        for slot, value in (getattr(module, "plugins", None) or {}).items():
            if value is not None:
                folded[slot] = value
        if folded:
            data["plugins"] = folded
        else:
            data.pop("plugins", None)

        # 头模块改名 → 组内指向旧 code 的邻接边 / 节点跳转一并改写
        if code_rewrite:
            for link in data.get("sub_modules", []):
                if link.get("target") in code_rewrite:
                    link["target"] = code_rewrite[link["target"]]
            for node_data in data.get("nodes", []):
                if node_data.get("jump_module") in code_rewrite:
                    node_data["jump_module"] = code_rewrite[
                        node_data["jump_module"]]

        if module is head:
            data["module_code"] = new_head_code
            data["module_name"] = (
                module_name or getattr(pattern, "name", None)
                or head.module_name
            )
            data["module_description"] = (
                module_description or getattr(pattern, "description", None)
                or head.module_description
            )
        return _module_from_dict(data)

    # 头模块居首（宿主以 result[0] 为入口）；其余保持源声明序
    head_converted: Optional[BaseModule] = None
    rest: List[BaseModule] = []
    for module in (getattr(pattern, "modules", None) or []):
        converted = _convert(module)
        if module is head:
            head_converted = converted
        else:
            rest.append(converted)
    if head_converted is None:  # 防御：module_map 由 modules 构建，不可达
        head_converted = _convert(head)
    return [head_converted] + rest


def pattern_to_module(
    pattern: Any,
    *,
    module_code: Optional[str] = None,
    module_name: Optional[str] = None,
    module_description: Optional[str] = None,
) -> BaseModule:
    """:func:`pattern_to_modules` 的单模块便捷入口：只返回头模块。

    多模块 pattern 请用 :func:`pattern_to_modules` 拿全量模块集（兄弟
    模块与组内邻接边/跳转是行为的一部分，只取头模块会丢拓扑）。
    """
    return pattern_to_modules(
        pattern, module_code=module_code, module_name=module_name,
        module_description=module_description,
    )[0]
