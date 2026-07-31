"""Tests for the ``web_search`` tool — stubbed httpx.Client, no real network."""

from __future__ import annotations

import json
from typing import Any

import httpx

from tools import web_search

_EXA_KEY = "test-exa-key"

_EXA_RESPONSE = {
    "results": [
        {
            "title": "First Result",
            "url": "https://example.com/one",
            "text": "Snippet about the first result.",
        },
        {
            "title": "Second Result",
            "url": "https://example.com/two",
            "text": "Another snippet here.",
        },
    ]
}

# Minimal DuckDuckGo HTML markup with the classes the parser relies on.
_DDG_HTML = """
<div class="result">
  <h2><a class="result__a" href="https://ddg.example/a">Result A</a></h2>
  <a class="result__snippet">Snippet A text</a>
</div>
<div class="result">
  <h2><a class="result__a" href="https://ddg.example/b">Result B</a></h2>
  <a class="result__snippet">Snippet B text</a>
</div>
"""


def _transport(
    *,
    exa_status: int = 200,
    exa_body: Any = _EXA_RESPONSE,
    ddg_status: int = 200,
    ddg_body: str = _DDG_HTML,
    capture: dict[str, Any] | None = None,
) -> httpx.BaseTransport:
    """Route by host: api.exa.ai → Exa, html.duckduckgo.com → DDG."""

    class _T(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            if capture is not None:
                capture["host"] = host
                capture["method"] = request.method
                capture["headers"] = request.headers
                if request.content:
                    capture["body"] = json.loads(request.content)
            if "exa.ai" in host:
                if exa_status != 200:
                    return httpx.Response(exa_status, text="err")
                return httpx.Response(exa_status, json=exa_body)
            if "duckduckgo" in host:
                return httpx.Response(ddg_status, text=ddg_body)
            return httpx.Response(404, text="not found")

    return _T()


def _client(transport: httpx.BaseTransport) -> httpx.Client:
    return httpx.Client(transport=transport)


# ── Exa backend ─────────────────────────────────────────────────────────────


def test_exa_search_sends_api_key_and_renders_results() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = web_search.search(
        "python release", language="ru", exa_api_key=_EXA_KEY, client=client
    )
    # API key on the right header, POST with the query body.
    assert captured["method"] == "POST"
    assert captured["headers"]["x-api-key"] == _EXA_KEY
    assert captured["body"]["query"] == "python release"
    # Results rendered: title + url present.
    assert "First Result" in out
    assert "https://example.com/one" in out
    assert "Найдёл" in out or "Нашёл" in out


def test_exa_search_en_localizes_header() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = web_search.search(
        "python release", language="en", exa_api_key=_EXA_KEY, client=client
    )
    assert out.startswith("Found")


# ── Exa → DDG fallback ──────────────────────────────────────────────────────


def test_exa_failure_falls_back_to_ddg() -> None:
    # Exa returns 500; with a key set we must still get DDG results.
    client = _client(_transport(exa_status=500))
    out = web_search.search(
        "news", language="ru", exa_api_key=_EXA_KEY, client=client
    )
    assert "Result A" in out
    assert "https://ddg.example/a" in out


def test_exa_empty_results_fall_back_to_ddg() -> None:
    # Exa succeeds but with no hits — DDG provides the results instead.
    client = _client(_transport(exa_body={"results": []}))
    out = web_search.search(
        "news", language="ru", exa_api_key=_EXA_KEY, client=client
    )
    assert "Result A" in out


# ── DDG-only (no key) ───────────────────────────────────────────────────────


def test_no_key_uses_ddg_directly() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = web_search.search("weather", language="ru", exa_api_key="", client=client)
    assert "duckduckgo" in captured["host"]
    assert captured["method"] == "GET"
    assert "Result A" in out and "Snippet A text" in out


def test_ddg_empty_html_yields_no_results_message() -> None:
    client = _client(_transport(ddg_body="<html><body>nothing</body></html>"))
    out = web_search.search("x", language="ru", exa_api_key="", client=client)
    assert "ничего не нашёл" in out


# ── error / input paths ─────────────────────────────────────────────────────


def test_ddg_transport_error_returns_service_message() -> None:
    def _raise(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=httpx.MockTransport(_raise))
    out = web_search.search("x", language="ru", exa_api_key="", client=client)
    assert "недоступен" in out


def test_empty_query_returns_bad_query_message() -> None:
    client = _client(_transport())
    out = web_search.search("   ", language="ru", exa_api_key=_EXA_KEY, client=client)
    assert "Пустой" in out


# ── schema ──────────────────────────────────────────────────────────────────


def test_web_search_params_is_query_only() -> None:
    assert web_search.WEB_SEARCH_PARAMS["type"] == "object"
    assert web_search.WEB_SEARCH_PARAMS["additionalProperties"] is False
    assert list(web_search.WEB_SEARCH_PARAMS["properties"]) == ["query"]
    assert web_search.WEB_SEARCH_PARAMS["required"] == ["query"]
