"""``calculate`` tool — a safe arithmetic evaluator.

We deliberately do NOT use ``eval``. The expression is parsed with ``ast`` and
walked, allowing only numbers, the four basic operators, parentheses, unary
minus/plus, and ``**`` (power). Anything else (names, attribute access,
calls, comprehensions, ...) raises ``CalcError`` so the LLM gets a clear,
localised message instead of arbitrary code execution.
"""

from __future__ import annotations

import ast
import math
from typing import Any

from localization import LocaleStr

# Localised user-facing messages surfaced to the LLM (and onward to TTS).
_MSG_OK = LocaleStr(
    ru="Результат: {result}",
    en="Result: {result}",
)
_MSG_EMPTY = LocaleStr(
    ru="Пустое выражение.",
    en="Empty expression.",
)
_MSG_UNSUPPORTED = LocaleStr(
    ru="Неподдерживаемая конструкция в выражении: {detail}",
    en="Unsupported construct in expression: {detail}",
)
_MSG_DIV_ZERO = LocaleStr(
    ru="Деление на ноль.",
    en="Division by zero.",
)
_MSG_NEG_BASE = LocaleStr(
    ru="Нельзя возводить отрицательное число в дробную степень.",
    en="Cannot raise a negative number to a fractional power.",
)


class CalcError(ValueError):
    """Raised for any unsupported or invalid arithmetic expression."""


def _eval_node(node: ast.AST) -> float:
    """Recursively evaluate an AST node to a float, rejecting unsafe nodes."""
    # Literals: numbers only (bool is int subclass in py, allow it).
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool | int | float):
            return float(node.value)
        raise CalcError(f"literal of type {type(node.value).__name__!r}")

    # Named constants we explicitly permit.
    if isinstance(node, ast.Name):
        const = {"pi": math.pi, "tau": math.tau, "e": math.e}.get(node.id.lower())
        if const is None:
            raise CalcError(f"name {node.id!r}")
        return float(const)

    # Unary + / -.
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise CalcError(f"unary operator {type(node.op).__name__!r}")

    # Binary ops restricted to a safe set.
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            if right == 0:
                raise CalcError("division by zero")
            return left / right
        if isinstance(op, ast.FloorDiv):
            if right == 0:
                raise CalcError("division by zero")
            return left // right
        if isinstance(op, ast.Mod):
            if right == 0:
                raise CalcError("division by zero")
            return left % right
        if isinstance(op, ast.Pow):
            if left < 0 and right != int(right):
                raise CalcError("negative base, fractional exponent")
            try:
                return float(left**right)
            except (OverflowError, ValueError) as exc:
                raise CalcError(str(exc)) from exc
        raise CalcError(f"binary operator {type(op).__name__!r}")

    raise CalcError(f"node of type {type(node).__name__!r}")


def _format_result(value: float) -> str:
    """Render a float result without a trailing ``.0`` when it is integral."""
    if math.isfinite(value) and value == int(value):
        return str(int(value))
    return repr(value)


def evaluate(expression: str, language: str) -> str:
    """Evaluate ``expression`` and return a localised result string.

    Returns a user-facing message (always succeeds — errors are localised
    rather than raised, so the LLM can relay them to the user).
    """
    if not expression or not expression.strip():
        return _MSG_EMPTY.render(language)

    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        return _MSG_UNSUPPORTED.render(language, detail=str(exc))

    try:
        result = _eval_node(tree.body)
    except CalcError as exc:
        # Map a few well-known internal details to dedicated messages.
        detail = str(exc)
        if "division by zero" in detail:
            return _MSG_DIV_ZERO.render(language)
        if "negative base" in detail:
            return _MSG_NEG_BASE.render(language)
        return _MSG_UNSUPPORTED.render(language, detail=detail)

    return _MSG_OK.render(language, result=_format_result(result))


# Public, JSON-schema-validated argument contract.
CALCULATOR_PARAMS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "expression": {
            "type": "string",
            "description": (
                "Arithmetic expression using + - * / // % **, parentheses, "
                "and constants pi, e, tau. Example: (2 + 3) * 4"
            ),
        }
    },
    "required": ["expression"],
}


__all__ = ["evaluate", "CALCULATOR_PARAMS", "CalcError"]
