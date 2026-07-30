"""Minimal bilingual helpers (RU/EN) for tool responses.

Mirrors the LocaleStr pattern from the sibling voice-assistant project but
kept intentionally tiny: tool responses only need a one-shot ru/en string.
The string bodies may contain ``{placeholder}`` fields filled at render time.
"""

from __future__ import annotations

from collections.abc import Mapping


class LocaleStr:
    """Bilingual, optionally-templated string holding one value per language.

    ``render(language, **fields)`` picks the side for ``language`` (falling
    back to whichever side is non-empty) and ``str.format``\\ -substitutes any
    ``{placeholder}`` fields. Missing fields raise ``KeyError`` by design —
    a message template and its call sites must agree.
    """

    __slots__ = ("ru", "en")

    def __init__(self, ru: str = "", en: str = "") -> None:
        self.ru = ru
        self.en = en

    def render(self, language: str, **fields: object) -> str:
        template = self.en if language == "en" else self.ru
        if not template:
            template = self.ru or self.en
        return template.format(**fields) if fields else template

    def __str__(self) -> str:
        return self.en or self.ru

    def __repr__(self) -> str:
        return f"LocaleStr(ru={self.ru!r}, en={self.en!r})"


def tr(table: Mapping[str, LocaleStr], key: str, language: str, **fields: object) -> str:
    """Look up a localized message in a ``{key: LocaleStr}`` table and render it."""
    return table[key].render(language, **fields)


__all__ = ["LocaleStr", "tr"]
