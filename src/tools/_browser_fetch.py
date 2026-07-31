"""Headless-browser fallback for ``read_url`` — renders JS-heavy SPA pages.

The Reader and direct-fetch backends cannot read pages whose content is built
client-side by JavaScript (React/Vue/Angular apps return an almost-empty shell
from a plain HTTP GET). This module drives an **already-installed** system
Chromium-based browser (Brave, Yandex Browser, Chromium, Google Chrome — the
first found via ``shutil.which``) in headless mode over the DevTools Protocol
(CDP) so JS executes and we capture the rendered DOM.

Design notes
------------
* **No bundled browser.** We reuse the user's system browser; ``pychrome`` is
  only a lightweight CDP client (pure Python + websocket-client).
* **Ephemeral session.** A fresh browser process is launched per call with a
  throwaway ``--user-data-dir`` (temp) and a random debug port, then killed in a
  ``finally``. This avoids conflicting with the user's open browser and leaks no
  state into the long-lived voice agent.
* **Never raises.** Any failure (no browser, launch error, CDP timeout) returns
  ``None`` so the caller degrades to the "could not read" message.
* **Testable.** The whole subprocess+CDP dance lives behind ``_run_session``,
  which tests monkeypatch; ``find_browser`` is also a seam.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, cast

import pychrome  # type: ignore[import-untyped,reportMissingTypeStubs]  # no stubs

# Chromium-based browsers in priority order. The first one present on PATH is
# used; Firefox is intentionally absent (it speaks its own protocol, not CDP).
_CHROME_CANDIDATES = (
    "brave",
    "yandex-browser",
    "yandex-browser-stable",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "microsoft-edge",
)

# How long to wait for the browser to print its DevTools URL on stderr, and how
# long to wait for the page to reach a stable rendered state.
_DEVTOOLS_WAIT_SEC = 4.0
_SETTLE_SEC = 1.5

# Matches "DevTools listening on ws://127.0.0.1:PORT/devtools/..." in stderr.
_WS_RE = re.compile(r"ws://127\.0\.0\.1:(\d+)/")


def find_browser() -> str | None:
    """Return the path of the first available Chromium-based browser, or None."""
    for name in _CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _run_session(
    browser: str,
    url: str,
    *,
    timeout: float,
    settle_sec: float = _SETTLE_SEC,
) -> str | None:
    """Launch ``browser``, render ``url``, and return the page HTML.

    Returns None on any failure. Isolated so tests can monkeypatch it without a
    real browser. Owns the subprocess lifetime entirely (always kills it).
    """
    profile_dir = tempfile.mkdtemp(prefix="voice-agent-browser-")
    proc: subprocess.Popen[str] | None = None
    try:
        # --remote-debugging-port=0 lets the OS pick a free port, which we then
        # read from the browser's stderr ("DevTools listening on ws://…:PORT/").
        proc = subprocess.Popen(  # noqa: S603 - binary path comes from shutil.which
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile_dir}",
                url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for the DevTools WebSocket URL to appear on stderr.
        deadline = time.monotonic() + _DEVTOOLS_WAIT_SEC
        port: str | None = None
        assert proc.stderr is not None
        while time.monotonic() < deadline:
            line = proc.stderr.readline()
            if not line:
                if proc.poll() is not None:  # browser exited early
                    return None
                time.sleep(0.05)
                continue
            m = _WS_RE.search(line)
            if m:
                port = m.group(1)
                break
        if port is None:
            return None

        browser_obj = pychrome.Browser(url=f"http://127.0.0.1:{port}")
        tab = browser_obj.new_tab()  # type: ignore[unknown-member-type]
        try:
            tab.start()
            tab.call_method("Page.enable")  # type: ignore[unknown-member-type]
            tab.call_method(  # type: ignore[unknown-member-type]
                "Page.navigate", url=url, _timeout=timeout
            )
            # Let the SPA framework hydrate / fetch its data after load.
            time.sleep(settle_sec)
            result = cast(
                dict[str, Any],
                tab.call_method(  # type: ignore[unknown-member-type]
                    "Runtime.evaluate",
                    expression="document.documentElement.outerHTML",
                    _timeout=timeout,
                ),
            )
            html = result.get("result", {}).get("value")
            return html if isinstance(html, str) and html.strip() else None
        finally:
            with contextlib.suppress(Exception):
                tab.stop()
                browser_obj.close_tab(tab)  # type: ignore[unknown-member-type]
    except Exception:  # noqa: BLE001 - never propagate; caller treats None as failure
        return None
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
        shutil.rmtree(profile_dir, ignore_errors=True)


def fetch_rendered(url: str, *, timeout: float = 12.0) -> str | None:
    """Render ``url`` with a system headless browser; return HTML or None.

    Returns None when no browser is available or any step fails. The caller is
    expected to convert the HTML to markdown and treat an empty result as a
    graceful "could not read".
    """
    browser = find_browser()
    if browser is None:
        return None
    return _run_session(browser, url, timeout=timeout)


__all__ = ["fetch_rendered", "find_browser"]
