"""Tests for ``ExaClient`` — search and reader.

All HTTP is stubbed with an ``httpx.BaseTransport`` routing by path
(api.exa.ai/search vs api.exa.ai/contents). No network is touched.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tools.exa import ExaClient

_KEY = "test-exa-key"
_URL = "https://example.com/article"

_SEARCH_BODY = {
    "results": [
        {"title": "Result One", "url": "https://ex.com/one", "text": "First snippet."},
        {"title": "Result Two", "url": "https://ex.com/two", "text": "Second snippet."},
    ]
}

_CONTENTS_BODY = {
    "results": [
        {"title": "Exa Article", "url": _URL, "text": "This is the **body** text."}
    ]
}


def _transport(
    *,
    search_status: int = 200,
    search_body: Any = _SEARCH_BODY,
    contents_status: int = 200,
    contents_body: Any = _CONTENTS_BODY,
    capture: dict[str, Any] | None = None,
) -> httpx.BaseTransport:
    """Route by path: /search → search, /contents → reader."""

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
            if "/search" in path:
                if search_status != 200:
                    return httpx.Response(search_status, text="err")
                return httpx.Response(search_status, json=search_body)
            if "/contents" in path:
                if contents_status != 200:
                    return httpx.Response(contents_status, text="err")
                return httpx.Response(contents_status, json=contents_body)
            return httpx.Response(404, text="not found")

    return _T()


def _client(transport: httpx.BaseTransport) -> tuple[ExaClient, httpx.Client]:
    http = httpx.Client(transport=transport)
    return ExaClient(api_key=_KEY, client=http), http


# ── search ──────────────────────────────────────────────────────────────────


def test_search_sends_api_key_and_renders_hits() -> None:
    captured: dict[str, Any] = {}
    exa, http = _client(_transport(capture=captured))
    hits = exa.search("python release")
    assert len(hits) == 2
    assert hits[0]["title"] == "Result One"
    assert hits[0]["url"] == "https://ex.com/one"
    assert hits[0]["snippet"] == "First snippet."
    call = captured["calls"][0]
    assert call["method"] == "POST"
    assert call["headers"]["x-api-key"] == _KEY
    assert call["body"]["query"] == "python release"
    assert call["body"]["numResults"] == 5
    http.close()


def test_search_empty_results_returns_empty_list() -> None:
    exa, http = _client(_transport(search_body={"results": []}))
    assert exa.search("nothing here") == []
    http.close()


def test_search_http_error_raises() -> None:
    exa, http = _client(_transport(search_status=500))
    with pytest.raises(httpx.HTTPError):
        exa.search("x")
    http.close()


# ── reader ──────────────────────────────────────────────────────────────────


def test_read_sends_contents_request_and_returns_text() -> None:
    captured: dict[str, Any] = {}
    exa, http = _client(_transport(capture=captured))
    title, markdown = exa.read(_URL)
    assert title == "Exa Article"
    assert "**body** text" in markdown
    call = captured["calls"][0]
    assert call["method"] == "POST"
    assert call["headers"]["x-api-key"] == _KEY
    assert call["body"]["urls"] == [_URL]
    assert "maxCharacters" in call["body"]["text"]
    http.close()


def test_read_empty_text_returns_empty() -> None:
    exa, http = _client(
        _transport(contents_body={"results": [{"url": _URL, "text": ""}]})
    )
    assert exa.read(_URL) == ("", "")
    http.close()


def test_read_no_results_returns_empty() -> None:
    exa, http = _client(_transport(contents_body={"results": []}))
    assert exa.read(_URL) == ("", "")
    http.close()


def test_read_http_error_raises() -> None:
    exa, http = _client(_transport(contents_status=500))
    with pytest.raises(httpx.HTTPError):
        exa.read(_URL)
    http.close()


# ── key handling ────────────────────────────────────────────────────────────


def test_missing_key_raises() -> None:
    http = httpx.Client(transport=_transport())
    exa = ExaClient(api_key="  ", client=http)
    with pytest.raises(httpx.HTTPError):
        exa.search("x")
    with pytest.raises(httpx.HTTPError):
        exa.read(_URL)
    http.close()
