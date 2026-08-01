"""``read_url`` tool — fetch a page and return its content as text.

Resolution order, each a graceful fallback for the next:

  1. Firecrawl (``api.firecrawl.dev/v2/scrape``) via ``FirecrawlClient`` when
     one is provided — renders JS/SPAs server-side and returns markdown.
     Primary backend when ``FIRECRAWL_API_KEY`` is set.
  2. Jina Reader (``r.jina.ai``) via ``JinaClient`` when one is provided —
     renders JS/SPAs server-side and returns markdown. The client manages its
     own key (``JINA_API_KEY`` or an auto-minted trial key).
  3. Exa Contents (``api.exa.ai/contents``) via ``ExaClient`` when one is
     provided — returns the page text server-side.
  4. Direct fetch + local HTML→markdown — a plain ``GET`` on the URL (the
     ``fetch_client`` follows redirects) parsed by BeautifulSoup and converted
     with html2text. Works for static sites; for SPAs it returns only the
     initial HTML shell, which is the accepted limitation of this fallback.
  5. Headless browser — only when step 4 returned an empty JS app shell.

The output is capped (``_MAX_CHARS``) so a single page cannot overflow the
LLM's context window. The tool never raises on expected failures: it returns a
localised message the LLM can relay to the user.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from html2text import HTML2Text

from localization import LocaleStr
from tools import _browser_fetch

if TYPE_CHECKING:
    from tools.exa import ExaClient
    from tools.firecrawl import FirecrawlClient
    from tools.jina import JinaClient

# ── localised strings ───────────────────────────────────────────────────────

_MSG_CONTENT = LocaleStr(
    ru="{title}\n\n{markdown}",
    en="{title}\n\n{markdown}",
)
_MSG_EMPTY = LocaleStr(
    ru="Не удалось получить содержимое страницы {url}.",
    en="Could not read any content from {url}.",
)
_MSG_BAD_URL = LocaleStr(
    ru="Некорректная ссылка: {url}.",
    en="Invalid URL: {url}.",
)
_MSG_SERVICE = LocaleStr(
    ru="Не удалось открыть страницу {url}. Попробуйте позже.",
    en="Could not open the page {url}. Try again later.",
)

# ── tuning ──────────────────────────────────────────────────────────────────

_MAX_CHARS = 4000  # cap so one page can't blow up the LLM context window
# Below this many characters of readable text, a static fetch likely returned a
# JS app shell rather than real content — a cue to try the headless browser.
_SPA_MIN_TEXT = 200
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

# Tags that carry no semantic content for reading; stripped before conversion.
_DROP_TAGS = ("script", "style", "nav", "header", "footer", "noscript", "form", "aside")


# ── backends ────────────────────────────────────────────────────────────────


def _html_to_markdown(html: str) -> tuple[str, str]:
    """Convert raw HTML to (title, markdown), stripping chrome and boilerplate.

    Returns an empty markdown string when the page has no readable body (e.g. a
    JS shell with no SSR content), signalling the caller that the fallback
    yielded nothing usable.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    title = (soup.title.get_text(strip=True) if soup.title else "") or ""

    converter = HTML2Text()
    converter.body_width = 0  # no hard wrapping — the LLM reflows anyway
    converter.ignore_links = False
    converter.ignore_images = True
    converter.protect_links = True
    markdown = converter.handle(str(soup)).strip()
    return title, markdown


def _read_direct(url: str, *, client: httpx.Client) -> tuple[str, str, str] | None:
    """Fetch ``url`` directly and convert to (title, markdown, raw_html).

    The raw HTML is returned too so the caller can run the SPA-shell heuristic
    on it. Returns None on transport failure (caller surfaces a service
    message); returns ("", "", html) for an empty page (separate code path).
    """
    try:
        resp = client.get(
            url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT
        )
    except httpx.HTTPError:
        return None
    resp.raise_for_status()
    title, markdown = _html_to_markdown(resp.text)
    return title, markdown, resp.text


# ── helpers ─────────────────────────────────────────────────────────────────


def _is_valid_url(url: str) -> bool:
    """True for an absolute http(s) URL with a host."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


# Mount-point ids typical of client-side frameworks. Presence of one is a strong
# signal that the body is filled by JavaScript (React/Vue/Angular/Svelte/Next).
_SPA_ROOT_RE = re.compile(
    r'id\s*=\s*["\'](?:root|app|app-root|__next|__nuxt|q-app)["\']', re.IGNORECASE
)


def _looks_like_spa(html: str, *, text_len: int) -> bool:
    """Heuristic: does this HTML look like a JS-rendered app shell?

    ``text_len`` is the length of the markdown we already extracted from it.
    True when there is little readable text AND the markup shows a framework
    mount point or a script-heavy empty body. A false positive merely launches
    the browser once and still ends up at "empty" — safe by construction.
    """
    if text_len >= _SPA_MIN_TEXT:
        return False
    if _SPA_ROOT_RE.search(html):
        return True
    # Fallback signal: many <script> tags but almost no visible text.
    script_count = html.lower().count("<script")
    soup = BeautifulSoup(html, "html.parser")
    body_text = (soup.body.get_text(strip=True) if soup.body else "") if soup else ""
    return script_count >= 3 and len(body_text) < _SPA_MIN_TEXT


def _truncate(text: str) -> str:
    """Cap text at ``_MAX_CHARS``, keeping it on a sentence-ish boundary."""
    if len(text) <= _MAX_CHARS:
        return text
    cut = text.rfind("\n", 0, _MAX_CHARS)
    if cut < _MAX_CHARS * 0.6:  # no good newline break → cut on a space
        cut = text.rfind(" ", 0, _MAX_CHARS)
    return text[: max(cut, 0)].rstrip() + " …"


# ── public entry point ──────────────────────────────────────────────────────


def read_url(
    url: str,
    *,
    language: str,
    client: httpx.Client,
    fetch_client: httpx.Client,
    firecrawl: FirecrawlClient | None = None,
    jina: JinaClient | None = None,
    exa: ExaClient | None = None,
) -> str:
    """Fetch ``url`` and return its content as a localised text string.

    Resolution order, each a graceful fallback for the next:
      1. Firecrawl — primary backend; renders JS/SPAs server-side.
      2. Jina Reader — renders JS/SPAs server-side (key managed by the client).
      3. Exa Contents — returns the page text server-side.
      4. Direct fetch + local HTML→markdown.
      5. Headless browser — only when step 4 returned an empty JS app shell
         (an SPA whose content is built client-side). Renders the page with a
         system Chromium-based browser, then converts as in step 4.

    Never raises on expected failures.
    """
    if not _is_valid_url(url):
        return _MSG_BAD_URL.render(language, url=url)
    url = url.strip()

    # 1–3) Keyed readers in priority order. Each raises ``httpx.HTTPError`` on
    # failure and returns an empty markdown when the page yielded no content.
    for reader in (firecrawl, jina, exa):
        if reader is None:
            continue
        try:
            title, markdown = reader.read(url)
        except httpx.HTTPError:
            continue  # fall through to the next backend
        if markdown:
            return _MSG_CONTENT.render(
                language, title=title or url, markdown=_truncate(markdown)
            )

    # 4) Direct fetch + local conversion.
    try:
        result = _read_direct(url, client=fetch_client)
    except httpx.HTTPError:
        return _MSG_SERVICE.render(language, url=url)
    if result is None:
        return _MSG_SERVICE.render(language, url=url)
    title, markdown, raw_html = result

    # 5) Headless browser — only for a detected SPA shell with no static text.
    if not markdown and _looks_like_spa(raw_html, text_len=len(markdown)):
        rendered = _browser_fetch.fetch_rendered(url)
        if rendered:
            title, markdown = _html_to_markdown(rendered)

    if not markdown:
        return _MSG_EMPTY.render(language, url=url)
    return _MSG_CONTENT.render(
        language, title=title or url, markdown=_truncate(markdown)
    )


# JSON-schema argument contract exposed to the LLM via the Realtime session.
READ_URL_PARAMS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "url": {
            "type": "string",
            "description": (
                "Absolute http(s) URL of the page to read, exactly as given by "
                "the user or returned by web_search. Example: "
                "'https://example.com/article'."
            ),
        }
    },
    "required": ["url"],
}


__all__ = ["read_url", "READ_URL_PARAMS"]
