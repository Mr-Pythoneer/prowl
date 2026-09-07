"""Resolving a spoken app name to an app that is actually installed.

Speech gives us what a person says, not what the bundle is called:
"Proton VPN" is `ProtonVPN.app`, "word" is `Microsoft Word.app`, "vs code" is
`Visual Studio Code.app`. Passing the spoken form straight to `open -a` fails,
and the failure reads as "I couldn't find an app called Proton VPN" — which is
wrong, because it is right there.

One resolver, shared by the pre-router and every app skill, so open/quit/switch
all understand the same names.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_SEARCH_DIRS = (
    "/Applications",
    "/Applications/Utilities",
    "/System/Applications",
    "/System/Applications/Utilities",
    "~/Applications",
)


def _normalize(name: str) -> str:
    """Lowercase and strip everything that speech renders inconsistently."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


@lru_cache(maxsize=1)
def installed_apps() -> tuple[str, ...]:
    """Names of installed .app bundles (without the extension), scanned once."""
    names: set[str] = set()
    for raw in _SEARCH_DIRS:
        try:
            for entry in Path(raw).expanduser().iterdir():
                if entry.suffix == ".app":
                    names.add(entry.stem)
        except OSError:
            continue
    return tuple(sorted(names))


def refresh() -> None:
    """Forget the cached list — call after installing an app."""
    installed_apps.cache_clear()


def resolve(name: str) -> str | None:
    """Return the installed app matching *name*, or None.

    Tried in order, most confident first:

    1. exact, case-insensitive            "safari"      -> Safari
    2. ignoring spaces and punctuation    "proton vpn"  -> ProtonVPN
    3. vendor-prefixed, unambiguous       "word"        -> Microsoft Word
    4. initials of a multi-word name      "vs code"     -> Visual Studio Code

    A leading-word match is deliberately *not* accepted: "google" must not
    resolve to "Google Chrome", or "open google" stops meaning the website.
    """
    wanted = (name or "").strip()
    if not wanted:
        return None
    apps = installed_apps()

    for app in apps:
        if app.lower() == wanted.lower():
            return app

    key = _normalize(wanted)
    if not key:
        return None
    hits = [a for a in apps if _normalize(a) == key]
    if len(hits) == 1:
        return hits[0]

    # "outlook" -> "Microsoft Outlook", but never "google" -> "Google Chrome".
    hits = [a for a in apps if _normalize(a).endswith(key)
            and len(_normalize(a)) > len(key)]
    if len(hits) == 1:
        return hits[0]

    # "vs code" -> "Visual Studio Code" via initials.
    squashed = key
    hits = []
    for app in apps:
        words = re.findall(r"[A-Za-z0-9]+", app)
        if len(words) > 1 and "".join(w[0] for w in words).lower() == squashed:
            hits.append(app)
    if len(hits) == 1:
        return hits[0]
    return None


def is_installed(name: str) -> bool:
    """True if *name* names an installed app."""
    return resolve(name) is not None
