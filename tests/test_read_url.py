"""Tests for the ``read_url`` tool — stubbed httpx.Client, no real network."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest

from tools import _browser_fetch, read_url
from tools.exa import ExaClient
from tools.firecrawl import FirecrawlClient
from tools.jina import JinaClient
from tools.secrets import SecretStore

_FC_KEY = "test-fc-key"
_JINA_KEY = "test-jina-key"
_EXA_KEY = "test-exa-key"
_URL = "https://example.com/article"

# Firecrawl /v2/scrape success body.
_FC_SCRAPE = {
    "success": True,
    "data": {
        "markdown": "This is the **scraped** body text.",
        "metadata": {"title": "Scraped Article", "sourceURL": _URL},
    },
}

# A classic SPA shell: a framework mount point and bundled scripts, with the
# real content produced by JS. A static GET yields almost no readable text.
_SPA_SHELL = (
    "<!DOCTYPE html><html><head><title>SPA App</title></head>"
    '<body><div id="root"></div>'
    "<script src='/app.1.js'></script><script src='/app.2.js'></script>"
    "<script src='/app.3.js'></script></body></html>"
)
# The same page after JS has run (what the headless browser would capture).
_SPA_RENDERED = (
    "<!DOCTYPE html><html><head><title>SPA App</title></head>"
    "<body><div id=\"root\"><main><p>The real SPA content is here.</p>"
    "</main></div></body></html>"
)

# Reader returns markdown with a Title line + body.
_READER_BODY = "Title: Example Article\n\nMarkdown Content:\nThis is the **body** text.\n"

# Exa /contents success body (read path).
_EXA_CONTENTS = {
    "results": [
        {"title": "Exa Article", "url": _URL, "text": "This is the Exa **body** text."}
    ]
}

# A static HTML page (direct-fetch fallback path).
_HTML_PAGE = """
<!DOCTYPE html><html><head><title>Example Article</title></head>
<body>
  <header>Site navigation</header>
  <script>tracking();</script>
  <main><p>This is the body text.</p></main>
  <footer>Copyright</footer>
</body></html>
"""


def _transport(
    *,
    fc_status: int = 200,
    fc_body: Any = _FC_SCRAPE,
    reader_status: int = 200,
    reader_body: str = _READER_BODY,
    exa_status: int = 200,
    exa_body: Any = _EXA_CONTENTS,
    direct_status: int = 200,
    direct_body: str = _HTML_PAGE,
    capture: dict[str, Any] | None = None,
) -> httpx.BaseTransport:
    """Route by host: firecrawl → scrape, jina → Reader, exa → contents, target → direct."""

    class _T(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            if capture is not None:
                capture.setdefault("calls", []).append(
                    {
                        "host": host,
                        "method": request.method,
                        "url": str(request.url),
                        "headers": request.headers,
                        "body": json.loads(request.content) if request.content else None,
                    }
                )
            if "firecrawl" in host:
                if fc_status != 200:
                    return httpx.Response(fc_status, text="err")
                return httpx.Response(fc_status, json=fc_body)
            if "jina.ai" in host:
                if reader_status != 200:
                    return httpx.Response(reader_status, text="err")
                return httpx.Response(reader_status, text=reader_body)
            if "exa.ai" in host:
                if exa_status != 200:
                    return httpx.Response(exa_status, text="err")
                return httpx.Response(exa_status, json=exa_body)
            if "example.com" in host:
                if direct_status != 200:
                    return httpx.Response(direct_status, text="err")
                return httpx.Response(direct_status, text=direct_body)
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


# ── Jina Reader (primary path) ──────────────────────────────────────────────


def test_reader_with_key_sends_bearer_auth() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = read_url.read_url(
        _URL, language="ru", jina=_jina(client),
        client=client, fetch_client=client,
    )
    call = captured["calls"][0]
    assert "jina.ai" in call["host"]
    assert call["headers"]["authorization"] == f"Bearer {_JINA_KEY}"
    assert str(_URL) in call["url"]  # target URL appended to reader base
    assert "Example Article" in out  # parsed title surfaced
    assert "body" in out and "text" in out  # markdown body surfaced (may be **bold**)


def test_without_jina_skips_to_direct_fetch() -> None:
    # No JinaClient provided → the Jina step is skipped entirely; the page is
    # fetched directly (no auth header), like the old anonymous path.
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = read_url.read_url(
        _URL, language="ru",
        client=client, fetch_client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert not any("jina.ai" in h for h in hosts)  # Jina never queried
    assert any("example.com" in h for h in hosts)  # direct fetch instead
    assert "authorization" not in {k.lower() for k in captured["calls"][0]["headers"]}
    assert "body" in out and "text" in out


def test_jina_empty_body_falls_back_to_direct() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(reader_body="   ", capture=captured))
    out = read_url.read_url(
        _URL, language="ru", jina=_jina(client),
        client=client, fetch_client=client,
    )
    # Reader returned nothing → a second call hit the target host directly.
    hosts = [c["host"] for c in captured["calls"]]
    assert any("jina.ai" in h for h in hosts)
    assert any("example.com" in h for h in hosts)
    assert "body text" in out  # from the direct HTML conversion


# ── Reader failure → direct fetch fallback ──────────────────────────────────


def test_reader_failure_falls_back_to_direct() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(reader_status=500, capture=captured))
    out = read_url.read_url(
        _URL, language="ru", jina=_jina(client),
        client=client, fetch_client=client,
    )
    assert "body text" in out  # recovered via direct fetch


def test_direct_fetch_strips_chrome() -> None:
    # Direct-only path (no Jina client): navigation/script/footer must be gone.
    client = _client(_transport(reader_status=500))
    out = read_url.read_url(
        _URL, language="ru",
        client=client, fetch_client=client,
    )
    assert "body text" in out
    assert "Site navigation" not in out
    assert "tracking" not in out
    assert "Copyright" not in out


# ── error / input paths ─────────────────────────────────────────────────────


def test_invalid_url_returns_bad_url_message() -> None:
    client = _client(_transport())
    out = read_url.read_url(
        "not-a-url", language="ru",
        client=client, fetch_client=client,
    )
    assert "Некорректная ссылка" in out


def test_all_backends_fail_returns_service_message() -> None:
    client = _client(
        _transport(fc_status=500, reader_status=500, exa_status=500, direct_status=502)
    )
    out = read_url.read_url(
        _URL, language="ru", firecrawl=_fc(client), jina=_jina(client),
        exa=_exa(client), client=client, fetch_client=client,
    )
    assert "не удалось открыть" in out.lower()


def test_empty_page_returns_empty_message() -> None:
    # Both backends return an effectively empty body.
    empty_html = "<html><head><title>x</title></head><body></body></html>"
    client = _client(_transport(reader_body="", direct_body=empty_html))
    out = read_url.read_url(
        _URL, language="ru",
        client=client, fetch_client=client,
    )
    assert "Не удалось получить содержимое" in out


# ── truncation ──────────────────────────────────────────────────────────────


def test_long_content_is_truncated() -> None:
    # Well over _MAX_CHARS (4000): the Reader body must be capped.
    big_body = "Title: t\nMarkdown Content:\n" + ("word " * 4000)
    client = _client(_transport(reader_body=big_body))
    out = read_url.read_url(
        _URL, language="ru", jina=_jina(client),
        client=client, fetch_client=client,
    )
    assert out.endswith("…")
    # Output is title + blank line + capped body — far shorter than the input.
    assert len(out) < len(big_body)
    assert len(out) < 5000


# ── SPA fallback to the headless browser ────────────────────────────────────


def test_spa_shell_triggers_browser_and_returns_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reader down + direct fetch returns an empty SPA shell → browser runs.
    monkeypatch.setattr(_browser_fetch, "fetch_rendered", lambda url: _SPA_RENDERED)
    client = _client(_transport(reader_status=500, direct_body=_SPA_SHELL))
    out = read_url.read_url(
        _URL, language="ru",
        client=client, fetch_client=client,
    )
    assert "real SPA content is here" in out


def test_spa_shell_browser_returns_none_falls_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Browser unavailable/failed → graceful "could not read" message.
    monkeypatch.setattr(_browser_fetch, "fetch_rendered", lambda url: None)
    client = _client(_transport(reader_status=500, direct_body=_SPA_SHELL))
    out = read_url.read_url(
        _URL, language="ru",
        client=client, fetch_client=client,
    )
    assert "Не удалось получить содержимое" in out


def test_static_page_does_not_trigger_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A normal page has enough text → the browser must never be consulted.
    monkeypatch.setattr(
        _browser_fetch, "fetch_rendered", lambda url: pytest.fail("must not run")
    )
    client = _client(_transport(reader_status=500, direct_body=_HTML_PAGE))
    out = read_url.read_url(
        _URL, language="ru",
        client=client, fetch_client=client,
    )
    assert "body text" in out  # served from the static fetch


# ── looks_like_spa heuristic ────────────────────────────────────────────────


def test_looks_like_spa_detects_framework_mount() -> None:
    shell = '<html><body><div id="root"></div></body></html>'
    assert read_url._looks_like_spa(shell, text_len=0) is True


def test_looks_like_spa_false_for_text_rich_page() -> None:
    html = "<html><body>" + ("<p>content</p>" * 100) + "</body></html>"
    assert read_url._looks_like_spa(html, text_len=10_000) is False


# ── Firecrawl backend (primary) ─────────────────────────────────────────────


def test_firecrawl_scrape_sends_bearer_and_renders_markdown() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = read_url.read_url(
        _URL, language="ru", firecrawl=_fc(client),
        client=client, fetch_client=client,
    )
    # Single Firecrawl call, POST with Bearer auth and the url in the body.
    assert len(captured["calls"]) == 1
    call = captured["calls"][0]
    assert "firecrawl" in call["host"]
    assert call["method"] == "POST"
    assert call["headers"]["authorization"] == f"Bearer {_FC_KEY}"
    assert call["body"]["url"] == _URL
    # Title + markdown surfaced from data.metadata / data.markdown.
    assert "Scraped Article" in out
    assert "scraped" in out and "body text" in out  # markdown may keep **bold**


def test_firecrawl_failure_falls_back_to_reader() -> None:
    captured: dict[str, Any] = {}
    # Firecrawl down, Reader up → Reader serves the content.
    client = _client(_transport(fc_status=500, capture=captured))
    out = read_url.read_url(
        _URL, language="ru", firecrawl=_fc(client), jina=_jina(client),
        client=client, fetch_client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any("firecrawl" in h for h in hosts)
    assert any("jina.ai" in h for h in hosts)
    assert "body" in out and "text" in out  # from Reader markdown


def test_firecrawl_empty_markdown_falls_back_to_reader() -> None:
    captured: dict[str, Any] = {}
    empty_scrape = {"success": True, "data": {"markdown": "", "metadata": {}}}
    client = _client(_transport(fc_body=empty_scrape, capture=captured))
    out = read_url.read_url(
        _URL, language="ru", firecrawl=_fc(client), jina=_jina(client),
        client=client, fetch_client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any("jina.ai" in h for h in hosts)
    assert "body" in out and "text" in out  # from Reader


def test_firecrawl_without_key_skips_to_reader() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    read_url.read_url(
        _URL, language="ru", jina=_jina(client),
        client=client, fetch_client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert not any("firecrawl" in h for h in hosts)
    assert any("jina.ai" in h for h in hosts)


# ── Exa backend (third fallback) ────────────────────────────────────────────


def test_exa_read_sends_contents_request_and_renders() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = read_url.read_url(
        _URL, language="ru", exa=_exa(client),
        client=client, fetch_client=client,
    )
    # Single Exa call, POST with x-api-key and the target in the body.
    assert len(captured["calls"]) == 1
    call = captured["calls"][0]
    assert "exa.ai" in call["host"]
    assert call["method"] == "POST"
    assert call["headers"]["x-api-key"] == _EXA_KEY
    assert call["body"]["urls"] == [_URL]
    # Title + text surfaced from the contents response.
    assert "Exa Article" in out
    assert "Exa **body** text" in out


def test_reader_failure_falls_back_to_exa() -> None:
    captured: dict[str, Any] = {}
    # Reader down, Exa up → Exa serves the content.
    client = _client(_transport(reader_status=500, capture=captured))
    out = read_url.read_url(
        _URL, language="ru", jina=_jina(client), exa=_exa(client),
        client=client, fetch_client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any("jina.ai" in h for h in hosts)
    assert any("exa.ai" in h for h in hosts)
    assert "Exa Article" in out


def test_firecrawl_short_circuits_before_exa() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    read_url.read_url(
        _URL, language="ru", firecrawl=_fc(client), exa=_exa(client),
        client=client, fetch_client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any("firecrawl" in h for h in hosts)
    # Firecrawl succeeded → Exa never queried.
    assert not any("exa.ai" in h for h in hosts)


def test_firecrawl_empty_markdown_falls_back_to_exa() -> None:
    captured: dict[str, Any] = {}
    empty_scrape = {"success": True, "data": {"markdown": "", "metadata": {}}}
    client = _client(_transport(fc_body=empty_scrape, capture=captured))
    out = read_url.read_url(
        _URL, language="ru", firecrawl=_fc(client), exa=_exa(client),
        client=client, fetch_client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any("exa.ai" in h for h in hosts)
    assert "Exa Article" in out  # served by Exa


def test_exa_empty_falls_back_to_direct() -> None:
    captured: dict[str, Any] = {}
    client = _client(
        _transport(exa_body={"results": [{"url": _URL, "text": ""}]}, capture=captured)
    )
    out = read_url.read_url(
        _URL, language="ru", exa=_exa(client),
        client=client, fetch_client=client,
    )
    hosts = [c["host"] for c in captured["calls"]]
    assert any("exa.ai" in h for h in hosts)
    assert any("example.com" in h for h in hosts)  # direct fetch served it
    assert "body text" in out


def test_exa_failure_falls_back_to_direct() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(exa_status=500, capture=captured))
    out = read_url.read_url(
        _URL, language="ru", exa=_exa(client),
        client=client, fetch_client=client,
    )
    assert "body text" in out  # recovered via direct fetch


# ── schema ──────────────────────────────────────────────────────────────────


def test_read_url_params_is_url_only() -> None:
    assert read_url.READ_URL_PARAMS["type"] == "object"
    assert read_url.READ_URL_PARAMS["additionalProperties"] is False
    assert list(read_url.READ_URL_PARAMS["properties"]) == ["url"]
    assert read_url.READ_URL_PARAMS["required"] == ["url"]
