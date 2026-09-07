"""Web skills — open URLs, run web searches, jump to well-known sites.

All three skills just hand a URL to the default browser via ``open`` (the macOS
launcher). Nothing here touches the network itself, so it stays stdlib-only and
is cheap and fast. Args come from a local LLM, so every entry point is tolerant
of missing/renamed keys and stringy values, and returns a :class:`SkillResult`
instead of raising.
"""
from __future__ import annotations

import difflib
import subprocess
from urllib.parse import quote_plus, urlsplit

from ..core.context import Context
from .base import Skill, SkillResult, SkillSpec, register

# Shorthand -> canonical URL for common destinations.
SITES: dict[str, str] = {
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "maps": "https://maps.google.com",
    "gmaps": "https://maps.google.com",
    "drive": "https://drive.google.com",
    "calendar": "https://calendar.google.com",
    "gcal": "https://calendar.google.com",
    "docs": "https://docs.google.com",
    "sheets": "https://sheets.google.com",
    "reddit": "https://www.reddit.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "wikipedia": "https://www.wikipedia.org",
    "wiki": "https://www.wikipedia.org",
    "amazon": "https://www.amazon.com",
    "netflix": "https://www.netflix.com",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "hackernews": "https://news.ycombinator.com",
    "hn": "https://news.ycombinator.com",
    "linkedin": "https://www.linkedin.com",
    "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai",
    "spotify": "https://open.spotify.com",
    "gpt": "https://chat.openai.com",
    "translate": "https://translate.google.com",
    "outlook": "https://outlook.office.com",
    "notion": "https://www.notion.so",
    "discord": "https://discord.com/app",
    "twitch": "https://www.twitch.tv",
}

# Query template per search engine.
ENGINES: dict[str, str] = {
    "google": "https://www.google.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "ddg": "https://duckduckgo.com/?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
}
DEFAULT_ENGINE = "google"


# --- helpers ----------------------------------------------------------------
def _first(args: dict, *keys: str) -> str:
    """Return the first present, non-empty arg among `keys`, stripped."""
    if not isinstance(args, dict):
        return ""
    for key in keys:
        val = args.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return ""


def _looks_like_url(text: str) -> bool:
    """Heuristic: does `text` resemble a URL or bare domain (e.g. example.com)?"""
    if not text or " " in text:
        return False
    # A scheme like https:// or a dotted host with a plausible TLD.
    if "://" in text:
        return bool(urlsplit(text).netloc)
    host = text.split("/", 1)[0]
    if "." not in host or host.startswith(".") or host.endswith("."):
        return False
    tld = host.rsplit(".", 1)[-1]
    return len(tld) >= 2 and tld.isalpha()


def _normalize_url(text: str) -> str:
    """Add an https:// scheme if `text` has none. Assumes URL-ish input."""
    text = text.strip()
    if "://" in text:
        return text
    return "https://" + text.lstrip("/")


def _open(url: str, ctx: Context) -> SkillResult:
    """Hand `url` to the default browser via `open`. Central success/fail path."""
    if ctx.dry_run:
        return SkillResult.say(f"Would open {url}.", detail=url, url=url)
    try:
        proc = subprocess.run(
            ["open", url], capture_output=True, text=True, timeout=15
        )
    except FileNotFoundError:
        return SkillResult.fail("Couldn't find the `open` command on this Mac.")
    except subprocess.SubprocessError:
        return SkillResult.fail(f"Couldn't open {url}.")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        return SkillResult.fail(f"Couldn't open {url}.", detail=detail)
    return SkillResult.say(f"Opening {url}.", detail=url, url=url)


# --- skills -----------------------------------------------------------------
@register
class OpenUrl(Skill):
    spec = SkillSpec(
        name="open_url",
        description="open a URL in the default web browser",
        examples=[
            "open example.com",
            "go to https://news.ycombinator.com",
            "open the apple website",
        ],
        args={"url": "the address to open (scheme optional, e.g. example.com)"},
    )

    def run(self, args: dict, ctx: Context) -> SkillResult:
        raw = _first(args, "url", "address", "link", "site", "query")
        if not raw:
            return SkillResult.fail("Tell me which URL to open.")
        if not _looks_like_url(raw):
            return SkillResult.fail(f"That doesn't look like a URL: {raw}")
        return _open(_normalize_url(raw), ctx)


@register
class WebSearch(Skill):
    spec = SkillSpec(
        name="web_search",
        description="run a web search in the browser (Google by default)",
        examples=[
            "search for the weather in Tokyo",
            "google best ramen near me",
            "duckduckgo python asyncio tutorial",
        ],
        args={
            "query": "what to search for",
            "engine": "optional: google, duckduckgo, or bing",
        },
    )

    def run(self, args: dict, ctx: Context) -> SkillResult:
        query = _first(args, "query", "q", "search", "text", "term")
        if not query:
            return SkillResult.fail("What should I search for?")
        engine = _first(args, "engine", "provider", "search_engine").lower()
        template = ENGINES.get(engine, ENGINES[DEFAULT_ENGINE])
        url = template.format(q=quote_plus(query))
        result = _open(url, ctx)
        if result.ok and not ctx.dry_run:
            return SkillResult.say(f"Searching for {query}.", detail=url, url=url)
        return result


@register
class OpenSite(Skill):
    spec = SkillSpec(
        name="open_site",
        description="open a well-known site by shorthand name (github, gmail, youtube, maps, ...)",
        examples=[
            "open github",
            "take me to gmail",
            "open youtube",
        ],
        args={"name": "a known shorthand (github, gmail, maps) or a domain"},
    )

    def run(self, args: dict, ctx: Context) -> SkillResult:
        name = _first(args, "name", "site", "url", "query", "target")
        if not name:
            return SkillResult.fail("Which site should I open?")
        key = name.strip().lower()
        url = SITES.get(key)
        if url is None:
            # Tolerate a near-miss ("youtub", "githbu") before giving up.
            close = difflib.get_close_matches(key, list(SITES), n=1, cutoff=0.82)
            if close:
                url = SITES[close[0]]
        if url is None:
            # Unknown shorthand: treat it as a domain if it looks like one.
            if _looks_like_url(name):
                url = _normalize_url(name)
            else:
                known = ", ".join(sorted(SITES)[:8])
                return SkillResult.fail(
                    f"I don't know the site '{name}'.",
                    detail=f"Known shorthands include: {known}, ...",
                )
        return _open(url, ctx)
