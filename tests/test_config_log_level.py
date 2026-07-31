"""Tests for the ``log_level`` setting (LOG_LEVEL env var).

Covers default, env override, case-insensitivity and the fallback for unknown
values. ``Settings.from_env`` reads ``LOG_LEVEL`` via ``_resolve_log_level``.
"""

from __future__ import annotations

import pytest

from config import Settings


def test_log_level_defaults_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert Settings.from_env().log_level == "INFO"


def test_log_level_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert Settings.from_env().log_level == "DEBUG"


@pytest.mark.parametrize("value", ["warning", "Error", "crITICAL"])
def test_log_level_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LOG_LEVEL", value)
    assert Settings.from_env().log_level == value.upper()


@pytest.mark.parametrize("value", ["", "  ", "verbose", "11", "DBG"])
def test_log_level_falls_back_to_info_on_unknown(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # A typo must not silently disable logging.
    monkeypatch.setenv("LOG_LEVEL", value)
    assert Settings.from_env().log_level == "INFO"


def test_log_level_trims_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "  debug  ")
    assert Settings.from_env().log_level == "DEBUG"
