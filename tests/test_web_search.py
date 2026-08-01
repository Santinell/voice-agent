"""Tests for the ``web_search`` tool — stubbed httpx.Client, no real network."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx

from tools import web_search
from tools.exa import ExaClient
from tools.firecrawl import FirecrawlClient
from tools.jina import JinaClient
from tools.secrets import SecretStore

_EXA_KEY = "test-exa-key"
_FC_KEY = "test-fc-key"
_JINA_KEY = "test-jina-key"

_FC_RESPONSE = {
    "success": True,
    "data": {
        "web": [
            {
                "title": "Firecrawl One",
                "url": "https://fc.example/one",
                "description": "First Firecrawl snippet.",
            },
            {
                "title": "Firecrawl Two",
                "url": "https://fc.example/two",
                "description": "Second Firecrawl snippet.",
            },
        ]
    },
}

_JINA_RESPONSE = {
    "code": 200,
    "data": [
        {
            "title": "Jina One",
            "url": "https://jina.example/one",
            "description": "First Jina snippet.",
        },
        {
            "title": "Jina Two",
            "url": "https://jina.example/two",
            "description": "Second Jina snippet.",
        },
    ],
}

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
    fc_status: int = 200,
    fc_body: Any = _FC_RESPONSE,
    jina_status: int = 200,
    jina_body: Any = _JINA_RESPONSE,
    exa_status: int = 200,
    exa_body: Any = _EXA_RESPONSE,
    ddg_status: int = 200,
    ddg_body: str = _DDG_HTML,
    capture: dict[str, Any] | None = None,
) -> httpx.BaseTransport:
    """Route by host: firecrawl → s.jina.ai → exa → duckduckgo."""

    class _T(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            if capture is not None:
                capture.setdefault("calls", []).append(
                    {
                        "host": host,
                        "method": request.method,
                        "headers": request.headers,
                        "body": json.loads(request.content) if request.content else None,
                    }
                )
            if "firecrawl" in host:
                if fc_status != 200:
                    return httpx.Response(fc_status, text="err")
                return httpx.Response(fc_status, json=fc_body)
            if host.startswith("s.jina.ai"):
                if jina_status != 200:
                    return httpx.Response(jina_status, text="err")
                return httpx.Response(jina_status, json=jina_body)
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


def _mem_store() -> SecretStore:
    """An in-memory SecretStore (schema created inline; yoyo needs a file)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE stored_secrets "
        "(name TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at TEXT, created_at TEXT NOT NULL)"
    )
    return SecretStore(conn)


def _jina(client: httpx.Client) -> JinaClient:
    """A JinaClient with a static key (no keygen call) over the stubbed client."""
    return JinaClient(secrets=_mem_store(), api_key=_JINA_KEY, client=client)


def _fc(client: httpx.Client) -> FirecrawlClient:
    """A FirecrawlClient with a static key over the stubbed client."""
    return FirecrawlClient(api_key=_FC_KEY, client=client)


def _exa(client: httpx.Client) -> ExaClient:
    """An ExaClient with a static key over the stubbed client."""
    return ExaClient(api_key=_EXA_KEY, client=client)


# ── Exa backend ─────────────────────────────────────────────────────────────


def test_exa_search_sends_api_key_and_renders_results() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = web_search.search(
        "python release",
        language="ru",
        exa=_exa(client),
        client=client,
    )
    # API key on the right header, POST with the query body.
    call = captured["calls"][0]
    assert call["method"] == "POST"
    assert call["headers"]["x-api-key"] == _EXA_KEY
    assert call["body"]["query"] == "python release"
    # Results rendered: title + url present.
    assert "First Result" in out
    assert "https://example.com/one" in out
    assert "Найдёл" in out or "Нашёл" in out


def test_exa_search_en_localizes_header() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = web_search.search(
        "python release",
        language="en",
        exa=_exa(client),
        client=client,
    )
    assert out.startswith("Found")


# ── Exa → DDG fallback ──────────────────────────────────────────────────────


def test_exa_failure_falls_back_to_ddg() -> None:
    # Exa returns 500; with a key set we must still get DDG results.
    client = _client(_transport(exa_status=500))
    out = web_search.search(
        "news", language="ru", exa=_exa(client), client=client
    )
    assert "Result A" in out
    assert "https://ddg.example/a" in out


def test_exa_empty_results_fall_back_to_ddg() -> None:
    # Exa succeeds but with no hits — DDG provides the results instead.
    client = _client(_transport(exa_body={"results": []}))
    out = web_search.search(
        "news", language="ru", exa=_exa(client), client=client
    )
    assert "Result A" in out


# ── DDG-only (no key) ───────────────────────────────────────────────────────


def test_no_key_uses_ddg_directly() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = web_search.search("weather", language="ru", client=client)
    hosts = [c["host"] for c in captured["calls"]]
    assert any("duckduckgo" in h for h in hosts)
    ddg = next(c for c in captured["calls"] if "duckduckgo" in c["host"])
    assert ddg["method"] == "GET"
    assert "Result A" in out and "Snippet A text" in out


def test_ddg_empty_html_yields_no_results_message() -> None:
    client = _client(_transport(ddg_body="<html><body>nothing</body></html>"))
    out = web_search.search("x", language="ru", client=client)
    assert "ничего не нашёл" in out


# ── error / input paths ─────────────────────────────────────────────────────


def test_ddg_transport_error_returns_service_message() -> None:
    def _raise(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=httpx.MockTransport(_raise))
    out = web_search.search("x", language="ru", client=client)
    assert "недоступен" in out


def test_empty_query_returns_bad_query_message() -> None:
    client = _client(_transport())
    out = web_search.search(
        "   ",
        language="ru",
        exa=_exa(client),
        client=client,
    )
    assert "Пустой" in out


# ── Firecrawl backend (primary) ─────────────────────────────────────────────


def test_firecrawl_search_sends_bearer_and_renders_results() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = web_search.search(
        "python release",
        language="ru",
        firecrawl=_fc(client),
        client=client,
    )
    # Single Firecrawl call, POST with Bearer auth and the query body.
    assert len(captured["calls"]) == 1
    call = captured["calls"][0]
    assert "firecrawl" in call["host"]
    assert call["method"] == "POST"
    assert call["headers"]["authorization"] == f"Bearer {_FC_KEY}"
    assert call["body"]["query"] == "python release"
    # Results rendered from data.web.
    assert "Firecrawl One" in out
    assert "https://fc.example/one" in out
    assert "Найдёл" in out or "Нашёл" in out


def test_firecrawl_en_localizes_header() -> None:
    client = _client(_transport())
    out = web_search.search(
        "python release",
        language="en",
        firecrawl=_fc(client),
        client=client,
    )
    assert out.startswith("Found")


def test_firecrawl_failure_falls_back_to_exa() -> None:
    captured: dict[str, Any] = {}
    # Firecrawl down, Exa up → Exa serves the results.
    client = _client(_transport(fc_status=500, capture=captured))
    out = web_search.search(
        "news",
        language="ru",
        firecrawl=_fc(client),
        exa=_exa(client),
        client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any("firecrawl" in h for h in hosts)
    assert any("exa.ai" in h for h in hosts)
    assert "First Result" in out  # from Exa


def test_firecrawl_empty_falls_back_to_exa() -> None:
    captured: dict[str, Any] = {}
    client = _client(
        _transport(fc_body={"success": True, "data": {"web": []}}, capture=captured)
    )
    web_search.search(
        "news",
        language="ru",
        firecrawl=_fc(client),
        exa=_exa(client),
        client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any("exa.ai" in h for h in hosts)


def test_firecrawl_without_key_skips_to_exa() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    web_search.search(
        "news",
        language="ru",
        exa=_exa(client),
        client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert not any("firecrawl" in h for h in hosts)
    assert any("exa.ai" in h for h in hosts)


# ── Jina backend (first fallback after Firecrawl) ───────────────────────────


def test_jina_search_renders_results_when_no_firecrawl() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = web_search.search(
        "python release",
        language="ru",
        jina=_jina(client),
        client=client,
    )
    # Jina was the first keyed backend tried; results rendered from data[].
    hosts = [c["host"] for c in captured["calls"]]
    assert any(h.startswith("s.jina.ai") for h in hosts)
    assert "Jina One" in out
    assert "https://jina.example/one" in out
    assert "Найдёл" in out or "Нашёл" in out


def test_jina_search_en_localizes() -> None:
    client = _client(_transport())
    out = web_search.search(
        "python release",
        language="en",
        jina=_jina(client),
        client=client,
    )
    assert out.startswith("Found")


def test_jina_short_circuits_before_exa() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    web_search.search(
        "news",
        language="ru",
        jina=_jina(client),
        exa=_exa(client),
        client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any(h.startswith("s.jina.ai") for h in hosts)
    # Jina succeeded → Exa never queried.
    assert not any("exa.ai" in h for h in hosts)


def test_firecrawl_short_circuits_before_jina() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    web_search.search(
        "news",
        language="ru",
        firecrawl=_fc(client),
        jina=_jina(client),
        client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any("firecrawl" in h for h in hosts)
    # Firecrawl succeeded → Jina never queried.
    assert not any(h.startswith("s.jina.ai") for h in hosts)


def test_jina_failure_falls_back_to_exa() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(jina_status=500, capture=captured))
    out = web_search.search(
        "news",
        language="ru",
        jina=_jina(client),
        exa=_exa(client),
        client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any(h.startswith("s.jina.ai") for h in hosts)
    assert any("exa.ai" in h for h in hosts)
    assert "First Result" in out  # served by Exa


def test_jina_empty_falls_back_to_exa() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(jina_body={"code": 200, "data": []}, capture=captured))
    web_search.search(
        "news",
        language="ru",
        jina=_jina(client),
        exa=_exa(client),
        client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any("exa.ai" in h for h in hosts)


# ── schema ──────────────────────────────────────────────────────────────────


def test_web_search_params_is_query_only() -> None:
    assert web_search.WEB_SEARCH_PARAMS["type"] == "object"
    assert web_search.WEB_SEARCH_PARAMS["additionalProperties"] is False
    assert list(web_search.WEB_SEARCH_PARAMS["properties"]) == ["query"]
    assert web_search.WEB_SEARCH_PARAMS["required"] == ["query"]
