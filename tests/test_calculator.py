"""Tests for the calculator tool."""

from __future__ import annotations

import pytest

from tools import calculator


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("2 + 3", "Результат: 5"),
        ("(2 + 3) * 4", "Результат: 20"),
        ("10 / 4", "Результат: 2.5"),
        ("7 // 2", "Результат: 3"),
        ("7 % 3", "Результат: 1"),
        ("2 ** 10", "Результат: 1024"),
        ("-5 + 2", "Результат: -3"),
        ("+5", "Результат: 5"),
        ("2 * -3", "Результат: -6"),
        ("(1 + 2) * (3 + 4)", "Результат: 21"),
        ("3.5 + 1.5", "Результат: 5"),
    ],
)
def test_evaluate_basic_arithmetic_ru(expr: str, expected: str) -> None:
    assert calculator.evaluate(expr, "ru") == expected


def test_evaluate_constants() -> None:
    # pi and e are allowed named constants.
    out = calculator.evaluate("pi", "en")
    assert out.startswith("Result: 3.14")


def test_evaluate_localized_en() -> None:
    assert calculator.evaluate("2 + 2", "en") == "Result: 4"


def test_evaluate_empty() -> None:
    assert calculator.evaluate("", "ru") == "Пустое выражение."
    assert calculator.evaluate("   ", "en") == "Empty expression."


def test_evaluate_division_by_zero() -> None:
    assert calculator.evaluate("1 / 0", "ru") == "Деление на ноль."
    assert calculator.evaluate("5 // 0", "en") == "Division by zero."


def test_evaluate_negative_fractional_power() -> None:
    # (-8) ** 0.5 is undefined over reals → localised refusal.
    assert "отрицательное" in calculator.evaluate("(-8) ** 0.5", "ru")


def test_evaluate_rejects_names() -> None:
    # Arbitrary names are not allowed (no arbitrary code execution).
    out = calculator.evaluate("__import__('os')", "ru")
    assert "Неподдерживаемая" in out


def test_evaluate_rejects_calls() -> None:
    out = calculator.evaluate("open('x')", "en")
    assert "Unsupported" in out


def test_evaluate_rejects_attribute_access() -> None:
    out = calculator.evaluate("(1).bit_length()", "en")
    assert "Unsupported" in out


def test_evaluate_rejects_syntax_error() -> None:
    out = calculator.evaluate("2 +", "ru")
    assert "Неподдерживаемая" in out or "Пустое" in out
