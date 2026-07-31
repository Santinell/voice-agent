"""Tests for the headless-browser SPA fallback.

No real browser is launched: ``_run_session`` (the subprocess+CDP dance) and
``find_browser`` are monkeypatched so the tests stay deterministic and fast.
"""

from __future__ import annotations

import pytest

from tools import _browser_fetch

# ── find_browser ────────────────────────────────────────────────────────────


def test_find_browser_returns_first_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate only Brave present; later candidates must not be queried.
    def _which(name: str) -> str | None:
        return "/usr/bin/brave" if name == "brave" else None

    monkeypatch.setattr(_browser_fetch.shutil, "which", _which)
    assert _browser_fetch.find_browser() == "/usr/bin/brave"


def test_find_browser_skips_missing_to_next(monkeypatch: pytest.MonkeyPatch) -> None:
    available = {"chromium": "/usr/bin/chromium"}

    def _which(name: str) -> str | None:
        return available.get(name)

    monkeypatch.setattr(_browser_fetch.shutil, "which", _which)
    assert _browser_fetch.find_browser() == "/usr/bin/chromium"


def test_find_browser_returns_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_browser_fetch.shutil, "which", lambda _name: None)
    assert _browser_fetch.find_browser() is None


# ── fetch_rendered ──────────────────────────────────────────────────────────


def test_fetch_rendered_none_when_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_browser_fetch, "find_browser", lambda: None)
    # If find_browser returns None, _run_session must never be called.
    monkeypatch.setattr(
        _browser_fetch, "_run_session", lambda *a, **k: pytest.fail("must not run")
    )
    assert _browser_fetch.fetch_rendered("https://example.com") is None


def test_fetch_rendered_uses_session_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_browser_fetch, "find_browser", lambda: "/usr/bin/brave")
    captured: dict[str, object] = {}

    def _fake_run(browser: str, url: str, *, timeout: float) -> str | None:
        captured["browser"] = browser
        captured["url"] = url
        return "<html><body>rendered</body></html>"

    monkeypatch.setattr(_browser_fetch, "_run_session", _fake_run)
    out = _browser_fetch.fetch_rendered("https://spa.example", timeout=9.0)
    assert out == "<html><body>rendered</body></html>"
    assert captured["browser"] == "/usr/bin/brave"
    assert captured["url"] == "https://spa.example"


def test_fetch_rendered_returns_none_on_session_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_browser_fetch, "find_browser", lambda: "/usr/bin/brave")
    monkeypatch.setattr(_browser_fetch, "_run_session", lambda *a, **k: None)
    assert _browser_fetch.fetch_rendered("https://example.com") is None


# ── _run_session resilience (no real browser: cover the failure paths) ──────


def test_run_session_returns_none_when_browser_exits_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Popen whose stderr never yields a DevTools URL and that "exits" at once.
    class _FakeProc:
        def __init__(self) -> None:
            self._poll = 0  # already terminated

        def poll(self) -> int | None:
            return self._poll

        def kill(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

        @property
        def stderr(self):
            class _EmptyStderr:
                def readline(self) -> str:
                    return ""

            return _EmptyStderr()

    monkeypatch.setattr(
        _browser_fetch.subprocess, "Popen", lambda *a, **k: _FakeProc()
    )
    monkeypatch.setattr(_browser_fetch.tempfile, "mkdtemp", lambda **k: "/tmp/x")
    monkeypatch.setattr(_browser_fetch.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(_browser_fetch.time, "sleep", lambda _s: None)

    assert _browser_fetch._run_session("/usr/bin/brave", "https://x", timeout=2.0) is None
