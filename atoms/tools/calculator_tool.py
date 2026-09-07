"""Calculator tool — safe arithmetic expression evaluation.

Automatically registered via ``registry.register()`` at module import; no manual setup needed.

Tool name: ``calculator``
Toolset: ``utility``
Permission: all patterns, all modules (``{"*": True}``).
"""

import json
import math
import operator
import re
from typing import Any, Dict

from nexus.registry.tools import registry, tool_error, tool_result

# ---------------------------------------------------------------------------
# Safe arithmetic operations whitelist
# ---------------------------------------------------------------------------

_SAFE_OPS: Dict[str, Any] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "//": operator.floordiv,
    "%": operator.mod,
    "**": operator.pow,
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
    "pi": math.pi,
    "e": math.e,
}

# Only allow letters, digits, spaces, operators, parentheses, decimal point, underscore, comma
_ALLOWED_CHARS_RE = re.compile(r"^[a-zA-Z0-9\s\+\-\*/%=\(\)\._,]+$")


def _safe_eval(expression: str) -> float:
    """Safely evaluate an arithmetic expression.

    Uses Python ``eval()`` with globals restricted to whitelisted math functions
    and ``__builtins__`` disabled. Character whitelist validation runs before eval.
    """
    expr = expression.strip()
    if not expr:
        raise ValueError("表达式为空")

    # Character whitelist validation
    if not _ALLOWED_CHARS_RE.match(expr):
        raise ValueError(f"Expression contains disallowed characters: {expr!r}")

    # Execute with restricted namespace
    return eval(expr, {"__builtins__": {}}, _SAFE_OPS)


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