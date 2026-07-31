"""Tests for the ``read_url`` tool — stubbed httpx.Client, no real network."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tools import _browser_fetch, read_url

_READER_KEY = "test-reader-key"
_URL = "https://example.com/article"

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
    reader_status: int = 200,
    reader_body: str = _READER_BODY,
    direct_status: int = 200,
    direct_body: str = _HTML_PAGE,
    capture: dict[str, Any] | None = None,
) -> httpx.BaseTransport:
    """Route by host: r.jina.ai → Reader, the target host → direct fetch."""

    class _T(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            if capture is not None:
                capture.setdefault("calls", []).append(
                    {
                        "host": host,
                        "url": str(request.url),
                        "headers": request.headers,
                    }
                )
            if "jina.ai" in host:
                if reader_status != 200:
                    return httpx.Response(reader_status, text="err")
                return httpx.Response(reader_status, text=reader_body)
            if "example.com" in host:
                if direct_status != 200:
                    return httpx.Response(direct_status, text="err")
                return httpx.Response(direct_status, text=direct_body)
            return httpx.Response(404, text="not found")

    return _T()


def _client(transport: httpx.BaseTransport) -> httpx.Client:
    return httpx.Client(transport=transport)


# ── Jina Reader (primary path) ──────────────────────────────────────────────


def test_reader_with_key_sends_bearer_auth() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = read_url.read_url(
        _URL, language="ru", reader_api_key=_READER_KEY,
        client=client, fetch_client=client,
    )
    call = captured["calls"][0]
    assert "jina.ai" in call["host"]
    assert call["headers"]["authorization"] == f"Bearer {_READER_KEY}"
    assert str(_URL) in call["url"]  # target URL appended to reader base
    assert "Example Article" in out  # parsed title surfaced
    assert "body" in out and "text" in out  # markdown body surfaced (may be **bold**)


def test_reader_without_key_omits_auth() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(capture=captured))
    out = read_url.read_url(
        _URL, language="ru", reader_api_key="",
        client=client, fetch_client=client,
    )
    assert "authorization" not in {k.lower() for k in captured["calls"][0]["headers"]}
    assert "body" in out and "text" in out


def test_reader_empty_body_falls_back_to_direct() -> None:
    captured: dict[str, Any] = {}
    client = _client(_transport(reader_body="   ", capture=captured))
    out = read_url.read_url(
        _URL, language="ru", reader_api_key="",
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
        _URL, language="ru", reader_api_key=_READER_KEY,
        client=client, fetch_client=client,
    )
    assert "body text" in out  # recovered via direct fetch


def test_direct_fetch_strips_chrome() -> None:
    # Direct-only path (Reader fails): navigation/script/footer must be gone.
    client = _client(_transport(reader_status=500))
    out = read_url.read_url(
        _URL, language="ru", reader_api_key="",
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
        "not-a-url", language="ru", reader_api_key="",
        client=client, fetch_client=client,
    )
    assert "Некорректная ссылка" in out


def test_all_backends_fail_returns_service_message() -> None:
    client = _client(_transport(reader_status=500, direct_status=502))
    out = read_url.read_url(
        _URL, language="ru", reader_api_key=_READER_KEY,
        client=client, fetch_client=client,
    )
    assert "не удалось открыть" in out.lower()


def test_empty_page_returns_empty_message() -> None:
    # Both backends return an effectively empty body.
    empty_html = "<html><head><title>x</title></head><body></body></html>"
    client = _client(_transport(reader_body="", direct_body=empty_html))
    out = read_url.read_url(
        _URL, language="ru", reader_api_key="",
        client=client, fetch_client=client,
    )
    assert "Не удалось получить содержимое" in out


# ── truncation ──────────────────────────────────────────────────────────────


def test_long_content_is_truncated() -> None:
    # Well over _MAX_CHARS (4000): the Reader body must be capped.
    big_body = "Title: t\nMarkdown Content:\n" + ("word " * 4000)
    client = _client(_transport(reader_body=big_body))
    out = read_url.read_url(
        _URL, language="ru", reader_api_key="",
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
        _URL, language="ru", reader_api_key="",
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
        _URL, language="ru", reader_api_key="",
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
        _URL, language="ru", reader_api_key="",
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


# ── schema ──────────────────────────────────────────────────────────────────


def test_read_url_params_is_url_only() -> None:
    assert read_url.READ_URL_PARAMS["type"] == "object"
    assert read_url.READ_URL_PARAMS["additionalProperties"] is False
    assert list(read_url.READ_URL_PARAMS["properties"]) == ["url"]
    assert read_url.READ_URL_PARAMS["required"] == ["url"]
