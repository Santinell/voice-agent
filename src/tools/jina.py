"""Jina AI integration: API-key management, search and reader.

Jina exposes two endpoints we use:

* ``s.jina.ai/<query>`` — web search; returns JSON with a ``data`` array of
  ``{title, url, description}`` results.
* ``r.jina.ai/<url>`` — page reader; renders JS/SPAs server-side and returns
  the page as markdown (with a ``Title:`` metadata preamble).

Both require a Bearer key. If ``JINA_API_KEY`` is configured we use it directly;
otherwise we mint a short-lived **trial key** from ``keygen.jina.ai/trial`` and
persist it in :class:`~tools.secrets.SecretStore`, refreshing it lazily when a
``401``/``403`` shows it has expired. Should the keygen endpoint refuse (rate
limit) or fail, the request surfaces as an ``httpx.HTTPError`` so the calling
tool can fall back to the next backend — we never send an unauthenticated
request, since Jina rejects those outright.

Every public method raises ``httpx.HTTPError`` on failure (transport error,
non-2xx, or an exhausted key); the callers wrap it in ``try/except`` and fall
back. We never raise on the expected "no results"/"empty body" case — that is
returned as an empty list / empty string respectively.
"""

from __future__ import annotations

from typing import Any, TypeAlias, cast

import httpx

from tools.secrets import SecretStore

# ── tuning ──────────────────────────────────────────────────────────────────

_MAX_RESULTS = 5
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_SEARCH_BASE = "https://s.jina.ai/"
_READER_BASE = "https://r.jina.ai/"
_KEYGEN_URL = "https://keygen.jina.ai/trial"
_SECRET_NAME = "jina"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

# A normalised search hit shared with the web_search tool.
Hit: TypeAlias = dict[str, str]


# ── helpers ─────────────────────────────────────────────────────────────────


def _parse_reader(body: str, fallback_title: str) -> tuple[str, str]:
    """Split a Jina Reader response into (title, markdown).

    Reader prefixes metadata lines (``Title:``, ``URL:``, ``Markdown Content:``)
    before the page content. We extract the title and return the remaining body
    as markdown, so the final output is a clean ``{title}\\n\\n{markdown}``.
    """
    title = fallback_title
    content_lines: list[str] = []
    seen_marker = False  # True once we pass the "Markdown Content:" preamble
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("Title:"):
            title = stripped[len("Title:") :].strip() or fallback_title
            continue
        if stripped.startswith(("URL:", "Markdown Content:")):
            seen_marker = seen_marker or stripped.startswith("Markdown Content:")
            continue
        content_lines.append(line)
    markdown = "\n".join(content_lines).strip() if seen_marker else body.strip()
    return title, markdown


class JinaClient:
    """Jina search + reader with transparent trial-key management."""

    def __init__(
        self,
        *,
        secrets: SecretStore,
        api_key: str,
        client: httpx.Client,
    ) -> None:
        self._secrets = secrets
        self._static_key = api_key.strip()
        self._client = client

    # ── key management ───────────────────────────────────────────────────────

    def _key(self) -> str:
        """Resolve a usable Jina key.

        A configured ``JINA_API_KEY`` wins; otherwise the persisted trial key
        from the secrets store. If neither is present, mint a fresh trial key
        and store it. Raises ``httpx.HTTPError`` if the keygen endpoint fails.
        """
        if self._static_key:
            return self._static_key
        stored = self._secrets.get(_SECRET_NAME)
        if stored is not None:
            return stored.value
        return self._mint_trial_key()

    def _mint_trial_key(self) -> str:
        """Request a new trial key from keygen and persist it.

        Raises ``httpx.HTTPError`` on any failure (incl. rate-limit 429) so the
        caller falls back rather than retrying unauthenticated.
        """
        resp = self._client.post(_KEYGEN_URL, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = cast(dict[str, Any], resp.json())
        key = str(payload.get("api_key") or "").strip()
        if not key:
            raise httpx.HTTPError("keygen returned no api_key")
        self._secrets.put(_SECRET_NAME, key)
        return key

    def _refresh_after_auth_failure(self) -> str:
        """Drop the expired key and mint a new one (single attempt)."""
        self._secrets.delete(_SECRET_NAME)
        return self._mint_trial_key()

    def _is_auth_error(self, resp: httpx.Response) -> bool:
        return resp.status_code in (401, 403)

    def _authed_get(
        self, url: str, *, extra_headers: dict[str, str] | None = None
    ) -> httpx.Response:
        """GET with the resolved key, retrying once on a 401/403.

        A configured ``JINA_API_KEY`` is assumed good — we only refresh
        persisted trial keys, which is the only expiry path we control.
        ``raise_for_status`` runs here, so callers get an ``httpx.HTTPError`` on
        any non-2xx.
        """
        headers = {"Authorization": f"Bearer {self._key()}", "User-Agent": _USER_AGENT}
        if extra_headers:
            headers.update(extra_headers)
        resp = self._client.get(url, headers=headers, timeout=_TIMEOUT)
        if self._is_auth_error(resp) and not self._static_key:
            headers["Authorization"] = f"Bearer {self._refresh_after_auth_failure()}"
            resp = self._client.get(url, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp

    # ── search ───────────────────────────────────────────────────────────────

    def search(self, query: str) -> list[Hit]:
        """Search Jina for ``query``; return up to ``_MAX_RESULTS`` rows.

        Raises ``httpx.HTTPError`` on failure. Returns an empty list when Jina
        reports no results.
        """
        resp = self._authed_get(
            _SEARCH_BASE + query,
            extra_headers={"Accept": "application/json", "X-Respond-With": "no-content"},
        )
        payload = cast(dict[str, Any], resp.json())
        raw = payload.get("data")
        if not isinstance(raw, list):
            return []
        rows = cast(list[dict[str, Any]], raw)
        hits: list[Hit] = []
        for r in rows:
            url = str(r.get("url") or "").strip()
            hits.append(
                {
                    "title": str(r.get("title") or "").strip() or url,
                    "url": url,
                    "snippet": str(r.get("description") or "").strip(),
                }
            )
            if len(hits) >= _MAX_RESULTS:
                break
        return hits

    # ── reader ───────────────────────────────────────────────────────────────

    def read(self, url: str) -> tuple[str, str]:
        """Read ``url`` through Jina Reader; return (title, markdown).

        Raises ``httpx.HTTPError`` on failure. Returns ("", "") for an empty
        page so the caller can fall through to the next backend.
        """
        resp = self._authed_get(
            _READER_BASE + url,
            extra_headers={"X-Return-Format": "markdown"},
        )
        body = resp.text.strip()
        if not body:
            return "", ""
        return _parse_reader(body, fallback_title=url)


__all__ = ["JinaClient", "Hit"]
