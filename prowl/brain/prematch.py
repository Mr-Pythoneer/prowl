"""Deterministic pre-router.

Small local models are fast but unreliable at classifying obvious commands
("pause the music", "clean up my mac") — they sometimes answer conversationally
instead of routing to a skill. So before we ever ask the model, we try a set of
high-confidence regex rules here. A hit is:

* **instant** (no model round-trip — the Siri-snappy path), and
* **reliable** (independent of whichever local model is loaded).

Anything that doesn't match falls through to the LLM router. Rules are
intentionally conservative: when a phrase is ambiguous (e.g. a bare "find the
capital of France"), we DON'T match and let the model decide. We never pre-match
a destructive action beyond what the skill's own safety layer already guards.

``match(utterance)`` returns ``(skill_name, args)`` or ``None``.
"""
from __future__ import annotations

import re
from typing import Optional

# NB: use typing.Optional (not `X | None`) here because this is a *runtime*
# expression, not an annotation — keeping it importable even under an old
# interpreter, so `prowl doctor` can still tell the user to use Python 3.11+.
Match = Optional["tuple[str, dict]"]

_FILE_NOUN = (
    r"file|files|document|documents|doc|pdf|pdfs|photo|photos|image|images|"
    r"picture|pictures|folder|folders|download|downloads|screenshot|screenshots|"
    r"note|notes|spreadsheet|video|videos|song|songs|"
    # Named document kinds people actually search for by name.
    r"resume|resumes|cv|invoice|invoices|receipt|receipts|essay|essays|"
    r"report|reports|presentation|presentations|slide|slides|contract|"
    r"contracts|assignment|assignments|homework|paper|papers|book|books|"
    r"\.[a-z0-9]{2,4}"
)


def match(utterance: str) -> Match:
    u = (utterance or "").strip()
    if not u:
        return None
    t = u.lower()

    # --- open a URL/website (before open_app, which skips domains) -----------
    m = re.search(r"\b(?:open|go to|goto|visit|navigate to|pull up)\s+(\S*\.\S+)", u, re.I)
    if m:
        return ("open_url", {"url": m.group(1).rstrip(".,!?")})

    # --- cleanup (verb-anchored, so "how much disk space" stays a question) --
    if re.search(r"\bempty (?:the )?trash\b", t):
        return ("cleanup", {"category": "trash"})
    if re.search(
        r"\b(clean\s?up|free up|free some space|reclaim (?:disk )?space|"
        r"clear (?:my |the )?caches?|remove junk|junk files|tidy up|"
        r"delete junk|clear junk)\b", t):
        return ("cleanup", {})

    # --- screenshot ----------------------------------------------------------
    # "rename all my screenshots by date" mentions screenshots but is a task for
    # the agent, not a request to take one. Require the absence of a verb that
    # operates *on* existing files.
    _manages_files = re.search(
        r"\b(rename|sort|organi[sz]e|move|delete|remove|tidy|group|archive|"
        r"upload|share|convert|compress|resize|batch)\b", t)
    if not _manages_files and (
            re.search(r"\b(take (?:a )?)?(?:screenshot|screen shot|screen capture)\b", t)
            or re.search(r"\bcapture (?:the |my )?screen\b", t)):
        mode = "full" if re.search(r"\b(full|whole|entire)\b", t) else "region"
        return ("screenshot", {"mode": mode})

    # --- dark / light mode ---------------------------------------------------
    if re.search(r"\b(dark mode|night mode|light mode)\b", t) \
            or re.search(r"\btoggle (?:the )?(?:theme|appearance|dark mode)\b", t):
        if "light" in t:
            return ("toggle_dark_mode", {"mode": "light"})
        if "dark" in t or "night" in t:
            return ("toggle_dark_mode", {"mode": "dark"})
        return ("toggle_dark_mode", {})

    # --- lock / sleep --------------------------------------------------------
    if re.search(r"\block (?:the |my )?(?:screen|mac|computer|laptop|desktop|it)\b", t):
        return ("lock_or_sleep", {"mode": "lock"})
    if re.search(r"\b(go to sleep|sleep the (?:display|screen)|put .* to sleep)\b", t):
        return ("lock_or_sleep", {"mode": "sleep"})

    # --- volume (only when a number or mute is present) ----------------------
    if re.fullmatch(r"\s*(?:please\s+)?(un)?mute(?:\s+(?:the\s+)?(?:volume|sound|audio))?\s*", t):
        return ("set_volume", {"level": "unmute" if "unmute" in t else "mute"})
    if re.search(r"\bvolume\b|\bsound\b", t):
        num = re.search(r"\b(\d{1,3})\b", t)
        if num:
            return ("set_volume", {"level": max(0, min(100, int(num.group(1))))})
        if re.search(r"\b(mute)\b", t):
            return ("set_volume", {"level": "mute"})
        if re.search(r"\bunmute\b", t):
            return ("set_volume", {"level": "unmute"})

    # Relative / bare "turn it up|down" — no "volume" word needed, which is how
    # people actually say it out loud.
    m = re.search(r"\bturn (?:it|the (?:volume|sound|music|audio)|that)?\s*"
                  r"(up|down)\b(?:\s+to\s+(\d{1,3}))?", t)
    if m:
        if m.group(2):
            return ("set_volume", {"level": max(0, min(100, int(m.group(2))))})
        return ("set_volume", {"direction": m.group(1)})
    if re.fullmatch(r"\s*(?:please\s+)?(louder|quieter|volume up|volume down)\s*", t):
        direction = "up" if t.strip() in ("louder", "volume up") else "down"
        return ("set_volume", {"direction": direction})

    # --- clipboard -----------------------------------------------------------
    if re.search(r"\b(?:what'?s|what is)\s+(?:on|in)\s+(?:my|the)\s+clipboard\b", t) \
            or re.search(r"\b(?:read|show|check)\s+(?:my|the)\s+clipboard\b", t) \
            or re.fullmatch(r"\s*clipboard\s*", t):
        return ("clipboard", {"action": "get"})

    # --- media control -------------------------------------------------------
    media = _media_action(t)
    if media:
        return ("media_control", {"action": media})

    # --- recent downloads ----------------------------------------------------
    if re.search(r"\b(recent|latest|newest|last) downloads?\b", t) \
            or re.search(r"\bwhat did i (?:just )?download\b", t):
        return ("recent_downloads", {})

    # --- system status -------------------------------------------------------
    if re.search(
        r"\b(battery|charge left|how much (?:disk|storage|space)|free space|"
        r"storage left|system status|how'?s my mac|what'?s my battery)\b", t):
        return ("system_status", {})

    # --- find files (needs a file-ish noun to avoid trivia questions) --------
    if re.search(r"\b(find|locate|search for|look for|where'?s|where is)\b", t) \
            and re.search(rf"\b(?:{_FILE_NOUN})\b", t):
        q = re.sub(r"^.*?\b(find|locate|search for|look for|where'?s|where is)\b\s*",
                   "", u, flags=re.I).strip().rstrip("?.!")
        q = re.sub(r"^(my|the|a|all|any|some)\s+", "", q, flags=re.I).strip()
        return ("find_files", {"query": q or u})

    # --- web search (explicit search verbs only) -----------------------------
    # "google X" means search — unless the sentence opened with a launch verb,
    # where "Google" is part of the app's name ("open Google Chrome").
    m = None
    if not re.match(r"\s*(?:please\s+)?(?:open|launch|start|fire up|bring up|switch to)\b",
                    u, re.I):
        m = re.search(r"\b(?:google|search (?:the web|online)(?: for)?|look up)\s+(.+)",
                      u, re.I)
    if m:
        return ("web_search", {"query": m.group(1).strip().rstrip("?.!")})

    # --- quit an app ---------------------------------------------------------
    m = re.match(r"(?:please\s+)?(?:quit|exit|force quit)\s+(?:the\s+)?(.+)", u, re.I)
    if m:
        app = _clean_app(m.group(1))
        if app:
            return ("quit_app", {"app": app})

    # --- open / switch to an app (broad; must look like an app name) ---------
    m = re.match(r"(?:please\s+)?(open|launch|start|fire up|bring up|switch to)\s+(?:the\s+)?(.+)",
                 u, re.I)
    if m:
        verb, target = m.group(1).lower(), _clean_app(m.group(2))
        # A known web shorthand ("open google", "open youtube") is a site, not
        # an app. Reuse the table open_site already owns instead of duplicating
        # it here. Some names are both (Claude, Discord, Spotify) — an installed
        # app wins there, and "switch to" always means a running app.
        if verb != "switch to" and target.lower() in _web_sites() \
                and not _app_installed(target):
            return ("open_site", {"name": target.lower()})
        looks_like_not_app = re.search(
            r"https?://|www\.|\.[a-z]{2,4}(?:/|$)|\b(folder|file|website|site|page|url|link|tab)\b",
            target, re.I)
        if target and not looks_like_not_app and len(target.split()) <= 4:
            skill = "activate_app" if verb == "switch to" else "open_app"
            return (skill, {"app": target})

    return None


def _media_action(t: str) -> str | None:
    if re.search(r"\b(next|skip)\b(?:\s+(?:track|song|this))?\b", t) \
            and not re.search(r"\bnext downloads?\b", t):
        return "next"
    if re.search(r"\b(previous|prev|last)\s+(?:track|song)\b", t) or re.search(r"\bgo back a (?:track|song)\b", t):
        return "previous"
    if re.search(r"\bpause\b(?:\s+(?:the\s+)?(?:music|song|track|playback|it|spotify))?\b", t):
        return "pause"
    if re.search(r"\b(resume|play)\b\s+(?:the\s+)?(?:music|song|track|playback|spotify)\b", t):
        return "play"
    if re.fullmatch(r"\s*(?:please\s+)?(?:pause|resume|play|skip)\s*", t):
        return {"pause": "pause", "resume": "play", "play": "play", "skip": "next"}[
            re.sub(r"[^a-z]", "", t)]
    return None


def _web_sites() -> dict:
    """The open_site shorthand table, imported lazily to avoid a cycle."""
    try:
        from ..skills.web import SITES
    except Exception:  # noqa: BLE001 - routing must survive a bad import
        return {}
    return SITES


def _app_installed(name: str) -> bool:
    """True if `name` names an installed app — see prowl.core.macapps."""
    try:
        from ..core.macapps import is_installed
    except Exception:  # noqa: BLE001 - routing must survive a bad import
        return False
    return is_installed(name)


def _clean_app(raw: str) -> str:
    app = raw.strip().rstrip(".,!?")
    # Leading filler: people say "open the app Proton VPN" as often as
    # "open Proton VPN", and the recogniser transcribes it faithfully.
    app = re.sub(r"^(?:the\s+)?(?:app|application|program)\s+", "", app, flags=re.I)
    app = re.sub(r"^(?:my|the)\s+", "", app, flags=re.I)
    app = re.sub(r"\s+(app|application)$", "", app, flags=re.I)
    app = re.sub(r"\s+(please|now|for me)$", "", app, flags=re.I)
    return app.strip()
