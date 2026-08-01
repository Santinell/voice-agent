"""Tests for ``FirecrawlClient`` — search and reader.

All HTTP is stubbed with an ``httpx.BaseTransport`` routing by host
(api.firecrawl.dev). No network is touched.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tools.firecrawl import FirecrawlClient

_KEY = "test-fc-key"
_URL = "https://example.com/article"

_SEARCH_BODY = {
    "success": True,
    "data": {
        "web": [
            {"title": "Result One", "url": "https://ex.com/one", "description": "First snippet."},
            {"title": "Result Two", "url": "https://ex.com/two", "description": "Second snippet."},
        ]
    },
}

_SCRAPE_BODY = {
    "success": True,
    "data": {
        "markdown": "This is the **scraped** body.",
        "metadata": {"title": "Scraped Article", "sourceURL": _URL},
    },
}


def _transport(
    *,
    search_status: int = 200,
    search_body: Any = _SEARCH_BODY,
    scrape_status: int = 200,
    scrape_body: Any = _SCRAPE_BODY,
    capture: dict[str, Any] | None = None,
) -> httpx.BaseTransport:
    """Route by path: /v2/search → search, /v2/scrape → reader."""

    class _T(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if capture is not None:
                capture.setdefault("calls", []).append(
                    {
                        "method": request.method,
                        "path": path,
                        "headers": request.headers,
                        "body": json.loads(request.content) if request.content else None,
                    }
                )
            if "/v2/search" in path:
                if search_status != 200:
                    return httpx.Response(search_status, text="err")
                return httpx.Response(search_status, json=search_body)
            if "/v2/scrape" in path:
                if scrape_status != 200:
                    return httpx.Response(scrape_status, text="err")
                return httpx.Response(scrape_status, json=scrape_body)
            return httpx.Response(404, text="not found")

    return _T()


def _client(transport: httpx.BaseTransport) -> tuple[FirecrawlClient, httpx.Client]:
    http = httpx.Client(transport=transport)
    return FirecrawlClient(api_key=_KEY, client=http), http


# ── search ──────────────────────────────────────────────────────────────────


def test_search_sends_bearer_and_renders_hits() -> None:
    captured: dict[str, Any] = {}
    fc, http = _client(_transport(capture=captured))
    hits = fc.search("python release")
    assert len(hits) == 2
    assert hits[0]["title"] == "Result One"
    assert hits[0]["url"] == "https://ex.com/one"
    assert hits[0]["snippet"] == "First snippet."
    call = captured["calls"][0]
    assert call["method"] == "POST"
    assert call["headers"]["authorization"] == f"Bearer {_KEY}"
    assert call["body"]["query"] == "python release"
    http.close()


def test_search_empty_results_returns_empty_list() -> None:
    fc, http = _client(
        _transport(search_body={"success": True, "data": {"web": []}})
    )
    assert fc.search("nothing here") == []
    http.close()


def test_search_unrecognised_shape_returns_empty_list() -> None:
    fc, http = _client(_transport(search_body={"success": True, "data": {}}))
    assert fc.search("x") == []
    http.close()


def test_search_http_error_raises() -> None:
    fc, http = _client(_transport(search_status=500))
    with pytest.raises(httpx.HTTPError):
        fc.search("x")
    http.close()


# ── reader ──────────────────────────────────────────────────────────────────


def test_read_returns_title_and_markdown() -> None:
    captured: dict[str, Any] = {}
    fc, http = _client(_transport(capture=captured))
    title, markdown = fc.read(_URL)
    assert title == "Scraped Article"
    assert "scraped" in markdown
    call = captured["calls"][0]
    assert call["method"] == "POST"
    assert call["headers"]["authorization"] == f"Bearer {_KEY}"
    assert call["body"]["url"] == _URL
    assert call["body"]["formats"] == ["markdown"]
    http.close()


def test_read_empty_markdown_returns_empty() -> None:
    fc, http = _client(
        _transport(scrape_body={"success": True, "data": {"markdown": "", "metadata": {}}})
    )
    assert fc.read(_URL) == ("", "")
    http.close()


def test_read_http_error_raises() -> None:
    fc, http = _client(_transport(scrape_status=500))
    with pytest.raises(httpx.HTTPError):
        fc.read(_URL)
    http.close()


# ── key handling ────────────────────────────────────────────────────────────


def test_missing_key_raises() -> None:
    http = httpx.Client(transport=_transport())
    fc = FirecrawlClient(api_key="  ", client=http)
    with pytest.raises(httpx.HTTPError):
        fc.search("x")
    with pytest.raises(httpx.HTTPError):
        fc.read(_URL)
    http.close()
