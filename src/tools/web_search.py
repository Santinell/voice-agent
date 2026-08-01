"""``web_search`` tool — internet search with a keyless fallback.

Backends, tried in order (each a graceful fallback for the next):

  1. Firecrawl (https://firecrawl.dev) via ``FirecrawlClient`` when one is
     provided — primary backend when ``FIRECRAWL_API_KEY`` is set.
  2. Jina (https://jina.ai) via ``JinaClient`` when one is provided — it
     manages its own key (``JINA_API_KEY`` or an auto-minted trial key).
  3. Exa (https://exa.ai) via ``ExaClient`` when one is provided — keyed
     backend (``EXA_API_KEY``).
  4. The keyless DuckDuckGo HTML endpoint.

Either way the caller gets a small list of ``{title, url, snippet}`` rendered
into a localised string. All backends are queried over the shared
``httpx.Client``. A slow or hung search must never block the voice loop, so
each request carries its own ``_TIMEOUT``; any ``httpx.HTTPError`` (including
auth/quota failures) is caught and the tool transparently degrades to the
next backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeAlias

import httpx
from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from tools.exa import ExaClient
    from tools.firecrawl import FirecrawlClient
    from tools.jina import JinaClient

from localization import LocaleStr

# ── localised strings ───────────────────────────────────────────────────────

_MSG_RESULTS = LocaleStr(
    ru="Нашёл {count} результатов:\n{items}",
    en="Found {count} results:\n{items}",
)
_MSG_ITEM = LocaleStr(
    ru="{i}. «{title}» — {url}\n{snippet}",
    en='{i}. "{title}" — {url}\n{snippet}',
)
_MSG_NO_RESULTS = LocaleStr(
    ru="По запросу «{query}» ничего не нашёл.",
    en='Found nothing for "{query}".',
)
_MSG_BAD_QUERY = LocaleStr(
    ru="Пустой поисковый запрос.",
    en="Empty search query.",
)
_MSG_SERVICE = LocaleStr(
    ru="Поиск сейчас недоступен. Попробуйте позже.",
    en="Search is unavailable right now. Try again later.",
)

# ── tuning ──────────────────────────────────────────────────────────────────

_MAX_RESULTS = 5
_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
_DDG_URL = "https://html.duckduckgo.com/html/"
# A realistic desktop UA keeps the keyless DDG endpoint from serving a bot wall.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

# Normalised result row shared by all backends.
_Hit: TypeAlias = dict[str, str]


# ── backends ────────────────────────────────────────────────────────────────


def _ddg_search(query: str, *, client: httpx.Client) -> list[_Hit]:
    """Query the keyless DuckDuckGo HTML endpoint and parse results.

    The DDG HTML markup is unofficial and may change; parsing is defensive —
    missing fields simply yield fewer rows rather than raising. Raises only on
    transport-level ``httpx.HTTPError``.
    """
    resp = client.get(
        _DDG_URL,
        params={"q": query},
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    hits: list[_Hit] = []
    for a in soup.select(".result__a"):
        title = a.get_text(" ", strip=True)
        # DDG wraps links in a redirect; the real URL sits in the title attr.
        url = str(a.get("href") or "")
        parent = a.find_parent(class_="result")
        snippet_el = parent.select_one(".result__snippet") if parent else None
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        if title or url:
            hits.append({"title": title or url, "url": url, "snippet": snippet})
        if len(hits) >= _MAX_RESULTS:
            break
    return hits


# ── rendering ───────────────────────────────────────────────────────────────


def _render(hits: list[_Hit], *, language: str, query: str) -> str:
    if not hits:
        return _MSG_NO_RESULTS.render(language, query=query)
    items = "\n".join(
        _MSG_ITEM.render(language, i=i + 1, title=h["title"], url=h["url"], snippet=h["snippet"])
        for i, h in enumerate(hits)
        if h["title"] or h["url"]
    )
    if not items:
        return _MSG_NO_RESULTS.render(language, query=query)
    return _MSG_RESULTS.render(language, count=len(hits), items=items)


# ── public entry point ──────────────────────────────────────────────────────


def search(
    query: str,
    *,
    language: str,
    client: httpx.Client,
    firecrawl: FirecrawlClient | None = None,
    jina: JinaClient | None = None,
    exa: ExaClient | None = None,
) -> str:
    """Search the web for ``query`` and return a localised result list.

    Tries Firecrawl → Jina → Exa → keyless DuckDuckGo, each a graceful fallback
    for the next. Never raises on expected failures — returns a localised
    message for the LLM to relay.
    """
    if not query or not query.strip():
        return _MSG_BAD_QUERY.render(language)
    query = query.strip()

    # 1–3) Keyed backends in priority order. Each raises ``httpx.HTTPError`` on
    # failure and returns an empty list when it has no results.
    for backend in (firecrawl, jina, exa):
        if backend is None:
            continue
        try:
            hits = backend.search(query)
        except httpx.HTTPError:
            continue  # fall through to the next backend
        if hits:
            return _render(hits, language=language, query=query)

    # 4) DuckDuckGo — keyless last resort.
    try:
        hits = _ddg_search(query, client=client)
    except httpx.HTTPError:
        return _MSG_SERVICE.render(language)
    return _render(hits, language=language, query=query)


# JSON-schema argument contract exposed to the LLM via the Realtime session.
WEB_SEARCH_PARAMS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "The search query in the user's language. Example: "
                "'latest Python release', 'мероприятия в Екатеринбурге', 'что нового в мире ИИ'."
            ),
        }
    },
    "required": ["query"],
}


__all__ = ["search", "WEB_SEARCH_PARAMS"]
