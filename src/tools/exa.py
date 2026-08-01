"""Exa integration: web search and page reader.

Exa exposes two endpoints we use:

* ``POST api.exa.ai/search`` — web search; returns JSON with a ``results``
  array of ``{title, url, text}`` objects.
* ``POST api.exa.ai/contents`` — page reader; returns the text content of one
  or more URLs as ``{title, url, text}`` rows.

Both require an ``x-api-key`` (``EXA_API_KEY``). Like Firecrawl there is no
trial-key path, so with an empty key every public method raises
``httpx.HTTPError`` and the calling tool falls back to the next backend.

Every public method raises ``httpx.HTTPError`` on failure (transport error or
non-2xx); the callers wrap it in ``try/except`` and fall back. We never raise
on the expected "no results"/"empty body" case — that is returned as an empty
list / empty string respectively.
"""

from __future__ import annotations

from typing import Any, TypeAlias, cast

import httpx

# ── tuning ──────────────────────────────────────────────────────────────────

_MAX_RESULTS = 5
_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
_SEARCH_URL = "https://api.exa.ai/search"
_CONTENTS_URL = "https://api.exa.ai/contents"
# Cap a snippet so one verbose result can't crowd out the others.
_SNIPPET_MAX = 280
# How many characters of page text to request; the calling tool truncates
# further to fit the LLM context window.
_READ_MAX_CHARS = 8000

# A normalised search hit shared with the web_search tool.
Hit: TypeAlias = dict[str, str]


class ExaClient:
    """Exa search + reader, gated on a configured API key."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        self._api_key = api_key.strip()
        self._client = client

    def _authed_post(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        """POST with the ``x-api-key`` header; raises on any failure."""
        if not self._api_key:
            raise httpx.HTTPError("Exa API key not configured")
        resp = self._client.post(
            url,
            headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp

    @staticmethod
    def _results(payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw = payload.get("results")
        return cast(list[dict[str, Any]], raw if isinstance(raw, list) else [])

    # ── search ───────────────────────────────────────────────────────────────

    def search(self, query: str) -> list[Hit]:
        """Search Exa for ``query``; return up to ``_MAX_RESULTS`` rows.

        Raises ``httpx.HTTPError`` on failure. Returns an empty list when Exa
        reports no results.
        """
        resp = self._authed_post(
            _SEARCH_URL,
            {
                "query": query,
                "numResults": _MAX_RESULTS,
                "contents": {"text": {"maxCharacters": _SNIPPET_MAX}},
            },
        )
        payload = cast(dict[str, Any], resp.json())
        hits: list[Hit] = []
        for r in self._results(payload):
            url = str(r.get("url") or "").strip()
            text = str(r.get("text") or "").strip()
            hits.append(
                {
                    "title": str(r.get("title") or "").strip() or url,
                    "url": url,
                    "snippet": text,
                }
            )
            if len(hits) >= _MAX_RESULTS:
                break
        return hits

    # ── reader ───────────────────────────────────────────────────────────────

    def read(self, url: str) -> tuple[str, str]:
        """Read ``url`` through Exa Contents; return (title, markdown).

        Raises ``httpx.HTTPError`` on failure. Returns ("", "") when the page
        yielded no text, signalling the caller to try the next backend.
        """
        resp = self._authed_post(
            _CONTENTS_URL,
            {"urls": [url], "text": {"maxCharacters": _READ_MAX_CHARS}},
        )
        payload = cast(dict[str, Any], resp.json())
        for r in self._results(payload):
            text = str(r.get("text") or "").strip()
            if not text:
                continue
            return str(r.get("title") or "").strip(), text
        return "", ""


__all__ = ["ExaClient", "Hit"]
