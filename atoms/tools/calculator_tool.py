"""Calculator tool — safe arithmetic expression evaluation.

Automatically registered via ``registry.register()`` at module import; no manual setup needed.

Tool name: ``calculator``
Toolset: ``utility``
Permission: all patterns, all modules (``{"*": True}``).
"""

import ast
import json
import math
import operator
from typing import Any, Dict

from nexus.registry.tools import registry, tool_error, tool_result

# ---------------------------------------------------------------------------
# Safe arithmetic evaluation: AST whitelist walker (no eval)
# ---------------------------------------------------------------------------

_SAFE_BINOPS: Dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_SAFE_UNARYOPS: Dict[type, Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_SAFE_FUNCS: Dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
}

_SAFE_CONSTS: Dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
}

# Maximum node count (blocks AST bombs / deep nesting DoS)
_MAX_NODES = 200
# Maximum bit length of power operation operands and result (blocks 9**9**9**9 CPU hang)
_MAX_POW_BITS = 10_000
# Maximum bit length of any intermediate result (blocks overflow chain amplification)
_MAX_RESULT_BITS = 100_000


def _check_bits(value: Any) -> None:
    """Integers exceeding the bit limit raise ValueError (blocks big-int CPU/memory DoS)."""
    if isinstance(value, int) and value.bit_length() > _MAX_RESULT_BITS:
        raise ValueError("结果过大，拒绝计算（超出安全限制）")


def _safe_pow(a: Any, b: Any) -> Any:
    """Exponentiation with size guards: operand/result bit caps + negative-exponent float fallback."""
    if isinstance(a, int) and isinstance(b, int):
        if b < 0:
            return a ** b  # negative exponents produce float, safe magnitude
        if a.bit_length() * max(b, 1) > _MAX_POW_BITS:
            raise ValueError("幂运算规模过大，拒绝计算（超出安全限制）")
        result = a ** b
        _check_bits(result)
        return result
    return a ** b


def _eval_node(node: ast.AST) -> Any:
    """Recursively evaluate an AST node against the arithmetic whitelist.

    Anything outside Number/Constant(num)/Name(whitelisted const)/Call(whitelisted
    func)/BinOp/UnaryOp raises ValueError — attribute access (e.g. ``(1).__class__``),
    subscripts, lambdas, comprehensions etc. are structurally unreachable.
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"不支持的常量类型: {type(node.value).__name__}")
    if isinstance(node, ast.Num):  # pragma: no cover — py3.8 兼容
        return node.n
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        fn = _SAFE_BINOPS.get(op_type)
        if fn is None:
            raise ValueError(f"不支持的运算符: {op_type.__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if op_type is ast.Pow:
            return _safe_pow(left, right)
        result = fn(left, right)
        _check_bits(result)
        return result
    if isinstance(node, ast.UnaryOp):
        fn = _SAFE_UNARYOPS.get(type(node.op))
        if fn is None:
            raise ValueError("不支持的一元运算符")
        result = fn(_eval_node(node.operand))
        _check_bits(result)
        return result
    if isinstance(node, ast.Name):
        if node.id in _SAFE_CONSTS:
            return _SAFE_CONSTS[node.id]
        raise ValueError(f"未知标识符: {node.id!r}")
    if isinstance(node, ast.Call):
        fn = _SAFE_FUNCS.get(getattr(node.func, "id", ""))
        if fn is None:
            raise ValueError("不支持的函数调用")
        args = [_eval_node(a) for a in node.args]
        if node.keywords:
            raise ValueError("不支持关键字参数")
        result = fn(*args)
        _check_bits(result)
        return result
    raise ValueError(f"不支持的表达式节点: {type(node).__name__}")


def _safe_eval(expression: str) -> Any:
    """Safely evaluate an arithmetic expression via AST whitelist walking.

    No ``eval``: only numeric literals, whitelisted math functions/constants and
    arithmetic operators are admitted; node count, power size and result bit
    length are capped (blocks ``9**9**9**9`` style CPU hangs).
    """
    expr = expression.strip()
    if not expr:
        raise ValueError("表达式为空")
    if len(expr) > 500:
        raise ValueError("表达式过长（上限 500 字符）")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"表达式语法错误: {e}") from e

    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > _MAX_NODES:
        raise ValueError(f"表达式过于复杂（节点数 {node_count} 超过上限 {_MAX_NODES}）")

    result = _eval_node(tree)
    _check_bits(result)
    return result


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def _handle_calculator(args: Dict[str, Any]) -> str:
    """Handle calculator tool calls.

    Args:
        args: Dictionary containing the ``expression`` key, e.g. ``"3 * 4 + 2"``.

    Returns:
        JSON string with ``result`` or ``error`` field.
    """
    expression = args.get("expression", "")
    if not expression or not isinstance(expression, str):
        return tool_error("请提供有效的算术表达式", expression=expression)

    try:
        result = _safe_eval(expression)
        # Omit decimal point for integer values
        if isinstance(result, float) and result == int(result) and abs(result) < 1e15:
            result = int(result)
        return tool_result({"expression": expression, "result": result})
    except ZeroDivisionError:
        return tool_error("除零错误", expression=expression)
    except (ValueError, SyntaxError, TypeError) as e:
        return tool_error(f"表达式无效: {e}", expression=expression)
    except Exception as e:
        return tool_error(f"计算错误: {e}", expression=expression)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CALCULATOR_SCHEMA = {
    "name": "calculator",
    "description": (
        "执行算术运算。支持加减乘除 (+, -, *, /)、整除 (//)、取余 (%)、"
        "幂运算 (**)、以及常用数学函数: sqrt, sin, cos, tan, log, log2, "
        "log10, abs, round, min, max。常数: pi, e。"
        "示例表达式: '3 + 4 * 2', 'sqrt(16)', 'abs(-5) + round(3.7)'"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": (
                    "要求值的算术表达式。支持 +, -, *, /, //, %, **, "
                    "以及 sqrt, sin, cos, tan, abs, round, min, max, "
                    "log, log2, log10 等函数。"
                ),
            }
        },
        "required": ["expression"],
    },
}


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

registry.register(
    name="calculator",
    toolset="utility",
    schema=CALCULATOR_SCHEMA,
    handler=_handle_calculator,
    description="安全算术表达式求值，支持加减乘除与常用数学函数",
    emoji="🔢",
    allowed_patterns={"*": True},
)