"""Firecrawl integration: web search and page reader.

Firecrawl exposes two endpoints we use:

* ``POST api.firecrawl.dev/v2/search`` — web search; returns JSON with a
  ``data.web`` array of ``{title, url, description, ...}`` results.
* ``POST api.firecrawl.dev/v2/scrape`` — page reader; renders JS/SPAs
  server-side and returns markdown (with ``data.metadata.title``).

Both require a Bearer key (``FIRECRAWL_API_KEY``). Unlike Jina there is no
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
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
# Cap a snippet so one verbose result can't crowd out the others.
_SNIPPET_MAX = 280

# A normalised search hit shared with the web_search tool.
Hit: TypeAlias = dict[str, str]


class FirecrawlClient:
    """Firecrawl search + reader, gated on a configured Bearer key."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        self._api_key = api_key.strip()
        self._client = client

    def _authed_post(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        """POST with the Bearer key; raises ``httpx.HTTPError`` on any failure."""
        if not self._api_key:
            raise httpx.HTTPError("Firecrawl API key not configured")
        resp = self._client.post(
            url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp

    # ── search ───────────────────────────────────────────────────────────────

    def search(self, query: str) -> list[Hit]:
        """Search Firecrawl for ``query``; return up to ``_MAX_RESULTS`` rows.

        Raises ``httpx.HTTPError`` on failure. Returns an empty list when the
        response has no recognisable results.
        """
        resp = self._authed_post(_SEARCH_URL, {"query": query, "limit": _MAX_RESULTS})
        payload = cast(dict[str, Any], resp.json())
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        raw_rows = cast(dict[str, Any], data).get("web")
        if not isinstance(raw_rows, list):
            return []
        rows = cast(list[dict[str, Any]], raw_rows)
        hits: list[Hit] = []
        for r in rows:
            url = str(r.get("url") or "").strip()
            snippet = str(r.get("description") or r.get("markdown") or "").strip()
            hits.append(
                {
                    "title": str(r.get("title") or "").strip() or url,
                    "url": url,
                    "snippet": snippet[:_SNIPPET_MAX],
                }
            )
            if len(hits) >= _MAX_RESULTS:
                break
        return hits

    # ── reader ───────────────────────────────────────────────────────────────

    def read(self, url: str) -> tuple[str, str]:
        """Scrape ``url`` through Firecrawl; return (title, markdown).

        Raises ``httpx.HTTPError`` on failure. Returns ("", "") when the page
        yielded no markdown, signalling the caller to try the next backend.
        """
        resp = self._authed_post(
            _SCRAPE_URL,
            {"url": url, "formats": ["markdown"], "onlyMainContent": True},
        )
        payload = cast(dict[str, Any], resp.json())
        data = payload.get("data")
        if not isinstance(data, dict):
            return "", ""
        data_obj = cast(dict[str, Any], data)
        markdown = str(data_obj.get("markdown") or "").strip()
        metadata = data_obj.get("metadata")
        title = ""
        if isinstance(metadata, dict):
            meta_obj = cast(dict[str, Any], metadata)
            title = str(meta_obj.get("title") or "").strip()
        return title, markdown


__all__ = ["FirecrawlClient", "Hit"]
