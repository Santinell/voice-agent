"""Tests for ``JinaClient`` — key management, search and reader.

All HTTP is stubbed with an ``httpx.BaseTransport`` routing by host
(keygen.jina.ai / s.jina.ai / r.jina.ai). A real ``SecretStore`` on a migrated
in-file DB backs trial-key persistence, so the rotate-on-401 path is exercised
end to end (minus the network).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from db import connect, migrate
from tools.jina import JinaClient
from tools.secrets import SecretStore

_STATIC_KEY = "jina_static_key"
_TRIAL_KEY = "jina_trial_key_abc"
_TRIAL_KEY_2 = "jina_trial_key_def"
_URL = "https://example.com/article"

# A successful keygen response body.
def _keygen_body(key: str = _TRIAL_KEY) -> bytes:
    return json.dumps({"api_key": key}).encode()

# A successful search response body (JSON, data array).
_SEARCH_BODY = json.dumps(
    {
        "code": 200,
        "data": [
            {"title": "Result One", "url": "https://ex.com/one", "description": "First snippet."},
            {"title": "Result Two", "url": "https://ex.com/two", "description": "Second snippet."},
        ],
    }
).encode()

# A successful reader response body (markdown with Title preamble).
_READER_BODY = (
    b"Title: Example Article\nURL: https://example.com/article\n"
    b"Markdown Content:\nThis is the **body**.\n"
)


def _make_store(tmp_path: Path) -> SecretStore:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    return SecretStore(connect(db_path))


def _client(
    *,
    secrets: SecretStore,
    api_key: str = "",
    transport: httpx.BaseTransport,
) -> tuple[JinaClient, httpx.Client]:
    http = httpx.Client(transport=transport)
    return JinaClient(secrets=secrets, api_key=api_key, client=http), http


def _transport(
    *,
    keygen_status: int = 200,
    keygen_body: bytes = _keygen_body(),
    search_status: int = 200,
    search_body: bytes = _SEARCH_BODY,
    reader_status: int = 200,
    reader_body: bytes = _READER_BODY,
    capture: dict[str, Any] | None = None,
) -> httpx.BaseTransport:
    """Route by host: keygen → POST trial, s.jina → search, r.jina → reader."""

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
            if "keygen" in host:
                if keygen_status != 200:
                    return httpx.Response(keygen_status, text="rate limited")
                return httpx.Response(keygen_status, content=keygen_body)
            if host.startswith("s.jina.ai"):
                if search_status != 200:
                    return httpx.Response(search_status, text="unauthorized")
                return httpx.Response(search_status, content=search_body)
            if host.startswith("r.jina.ai"):
                if reader_status != 200:
                    return httpx.Response(reader_status, text="unauthorized")
                return httpx.Response(reader_status, content=reader_body)
            return httpx.Response(404, text="not found")

    return _T()


# ── search ──────────────────────────────────────────────────────────────────


def test_search_with_static_key_uses_bearer(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    secrets = _make_store(tmp_path)
    jc, http = _client(
        secrets=secrets, api_key=_STATIC_KEY, transport=_transport(capture=captured)
    )
    hits = jc.search("python release")
    assert len(hits) == 2
    assert hits[0]["title"] == "Result One"
    assert hits[0]["url"] == "https://ex.com/one"
    assert hits[0]["snippet"] == "First snippet."
    # Bearer = the configured static key; no keygen call made.
    call = captured["calls"][0]
    assert call["headers"]["authorization"] == f"Bearer {_STATIC_KEY}"
    hosts = [c["host"] for c in captured["calls"]]
    assert not any("keygen" in h for h in hosts)
    http.close()


def test_search_mints_and_persists_trial_key(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    secrets = _make_store(tmp_path)
    jc, http = _client(secrets=secrets, transport=_transport(capture=captured))
    jc.search("python release")
    hosts = [c["host"] for c in captured["calls"]]
    assert any("keygen" in h for h in hosts)  # minted a trial key
    # The trial key is persisted; a second search reuses it (no new keygen).
    captured["calls"].clear()
    jc.search("python release")
    hosts2 = [c["host"] for c in captured["calls"]]
    assert not any("keygen" in h for h in hosts2)
    assert secrets.get("jina") is not None
    assert secrets.get("jina").value == _TRIAL_KEY  # type: ignore[union-attr]
    http.close()


def test_search_401_refreshes_trial_key(tmp_path: Path) -> None:
    # A persisted trial key that the server rejects (401) → mint a fresh one.
    secrets = _make_store(tmp_path)
    secrets.put("jina", "expired_key")
    captured: dict[str, Any] = {}

    # First search returns 401, the refresh mints a new key; we need the
    # transport to flip to success after the first s.jina call. Use a fresh
    # transport whose search_status changes after one 401.
    class _Flipping(httpx.BaseTransport):
        def __init__(self) -> None:
            self.search_calls = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            if captured is not None:
                captured.setdefault("calls", []).append(
                    {"host": host, "headers": request.headers}
                )
            if "keygen" in host:
                return httpx.Response(200, content=_keygen_body(_TRIAL_KEY_2))
            if host.startswith("s.jina.ai"):
                self.search_calls += 1
                if self.search_calls == 1:
                    return httpx.Response(401, text="unauthorized")
                return httpx.Response(200, content=_SEARCH_BODY)
            return httpx.Response(404, text="nope")

    jc, http = _client(secrets=secrets, transport=_Flipping())
    hits = jc.search("python release")
    assert len(hits) == 2  # succeeded on the retry
    assert secrets.get("jina").value == _TRIAL_KEY_2  # type: ignore[union-attr]
    http.close()


def test_search_keygen_rate_limit_raises(tmp_path: Path) -> None:
    # No key anywhere + keygen 429 → HTTPError (caller falls back).
    secrets = _make_store(tmp_path)
    jc, http = _client(secrets=secrets, transport=_transport(keygen_status=429))
    with pytest.raises(httpx.HTTPError):
        jc.search("python release")
    http.close()


def test_search_empty_results_returns_empty_list(tmp_path: Path) -> None:
    secrets = _make_store(tmp_path)
    jc, http = _client(
        secrets=secrets,
        api_key=_STATIC_KEY,
        transport=_transport(search_body=json.dumps({"code": 200, "data": []}).encode()),
    )
    assert jc.search("nothing here") == []
    http.close()


# ── reader ──────────────────────────────────────────────────────────────────


def test_read_with_static_key_returns_title_and_markdown(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    secrets = _make_store(tmp_path)
    jc, http = _client(
        secrets=secrets, api_key=_STATIC_KEY, transport=_transport(capture=captured)
    )
    title, markdown = jc.read(_URL)
    assert title == "Example Article"
    assert "body" in markdown
    call = captured["calls"][0]
    assert call["headers"]["authorization"] == f"Bearer {_STATIC_KEY}"
    http.close()


def test_read_mints_trial_key_when_none(tmp_path: Path) -> None:
    secrets = _make_store(tmp_path)
    captured: dict[str, Any] = {}
    jc, http = _client(secrets=secrets, transport=_transport(capture=captured))
    jc.read(_URL)
    hosts = [c["host"] for c in captured["calls"]]
    assert any("keygen" in h for h in hosts)
    assert secrets.get("jina") is not None
    http.close()


def test_read_401_refreshes_trial_key(tmp_path: Path) -> None:
    secrets = _make_store(tmp_path)
    secrets.put("jina", "expired")

    class _Flipping(httpx.BaseTransport):
        def __init__(self) -> None:
            self.reader_calls = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            if "keygen" in host:
                return httpx.Response(200, content=_keygen_body(_TRIAL_KEY_2))
            if host.startswith("r.jina.ai"):
                self.reader_calls += 1
                return (
                    httpx.Response(401, text="no")
                    if self.reader_calls == 1
                    else httpx.Response(200, content=_READER_BODY)
                )
            return httpx.Response(404, text="nope")

    jc, http = _client(secrets=secrets, transport=_Flipping())
    title, markdown = jc.read(_URL)
    assert title == "Example Article"
    assert markdown
    assert secrets.get("jina").value == _TRIAL_KEY_2  # type: ignore[union-attr]
    http.close()


def test_read_empty_body_returns_empty(tmp_path: Path) -> None:
    secrets = _make_store(tmp_path)
    jc, http = _client(
        secrets=secrets,
        api_key=_STATIC_KEY,
        transport=_transport(reader_body=b"   "),
    )
    assert jc.read(_URL) == ("", "")
    http.close()


# ── key resolution priority ─────────────────────────────────────────────────


def test_static_key_takes_precedence_over_stored(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    secrets = _make_store(tmp_path)
    secrets.put("jina", "stored_should_be_ignored")
    jc, http = _client(
        secrets=secrets, api_key=_STATIC_KEY, transport=_transport(capture=captured)
    )
    jc.search("x")
    call = captured["calls"][0]
    assert call["headers"]["authorization"] == f"Bearer {_STATIC_KEY}"
    http.close()


def test_stored_unexpired_key_used_without_keygen(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    secrets = _make_store(tmp_path)
    secrets.put("jina", _TRIAL_KEY, expires_at=datetime.now(UTC) + timedelta(hours=1))
    jc, http = _client(secrets=secrets, transport=_transport(capture=captured))
    jc.search("x")
    hosts = [c["host"] for c in captured["calls"]]
    assert not any("keygen" in h for h in hosts)
    http.close()
