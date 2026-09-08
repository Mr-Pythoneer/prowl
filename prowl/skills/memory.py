"""Short-term memory — things to hold onto for later in the day.

Distinct from the one-turn memory in the Orchestrator (which resolves "close
it"). This is for things the user explicitly hands over: a room number, where
they parked, a command to try later. They persist to disk so a restart doesn't
lose them, and they surface in the buddy's thought cloud so it is obvious what
he is holding — a memory you cannot see is one you cannot trust.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from ..core.config import CONFIG_PATH
from ..core.context import Context
from .base import Skill, SkillResult, SkillSpec, register

_STORE = CONFIG_PATH.parent / "memory.json"
_LOCK = threading.Lock()

# Kept deliberately small: this is a scratchpad, not a database, and the whole
# point is that the current contents fit above his head.
_MAX_NOTES = 8


def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(_STORE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save(notes: list[dict[str, Any]]) -> None:
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(notes, indent=2))
        tmp.replace(_STORE)
    except OSError:
        pass


def active_notes() -> list[str]:
    """The remembered texts, newest first — for the buddy's thought cloud."""
    with _LOCK:
        return [n["text"] for n in reversed(_load())]


def _stems(text: str) -> set[str]:
    """Rough word stems, so "parking" matches a note about having "parked".

    Not linguistics — the first four characters of each word long enough to
    carry meaning. Good enough to find one of eight notes, which is all this
    has to do.
    """
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w[:4] for w in words if len(w) >= 4}


def _refers_to(needle: str, note: str) -> bool:
    """True if *needle* plausibly names *note*."""
    low_needle, low_note = needle.lower().strip(), note.lower()
    if not low_needle:
        return False
    if low_needle in low_note:
        return True
    wanted = _stems(needle)
    return bool(wanted and wanted & _stems(note))


# Leading filler people say before the thing itself.
_STRIP = re.compile(
    r"^(?:that\s+|about\s+|this[:\s]+|the fact that\s+)", re.I)


@register
class Remember(Skill):
    spec = SkillSpec(
        name="remember",
        description="hold onto a short note for later, or recall/forget it",
        examples=[
            "remember that I parked in level 3",
            "remember the wifi password is hunter2",
            "what am I meant to remember",
            "what did I ask you to remember",
            "forget about the parking",
            "forget everything",
        ],
        args={
            "text": "the thing to remember",
            "action": "optional: 'recall' or 'forget'",
        },
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        action = str(args.get("action") or "").strip().lower()
        text = _STRIP.sub("", str(args.get("text") or args.get("note") or
                                 "").strip()).strip(" .!?")

        if action in ("recall", "list", "what"):
            return self._recall()
        if action in ("forget", "delete", "clear"):
            return self._forget(text)

        if not text:
            return SkillResult.fail("What should I remember?")
        if ctx.dry_run:
            return SkillResult.say(f"Would remember: {text}.")

        with _LOCK:
            notes = _load()
            notes.append({"text": text, "at": time.time()})
            notes = notes[-_MAX_NOTES:]
            _save(notes)
        return SkillResult.say(f"Got it — {text}.", detail=text)

    @staticmethod
    def _recall() -> SkillResult:
        notes = active_notes()
        if not notes:
            return SkillResult.say("I'm not holding anything for you.")
        if len(notes) == 1:
            return SkillResult.say(notes[0], detail=notes[0])
        spoken = "; ".join(notes)
        return SkillResult.say(f"{len(notes)} things: {spoken}",
                               detail="\n".join(f"- {n}" for n in notes))

    @staticmethod
    def _forget(text: str) -> SkillResult:
        with _LOCK:
            notes = _load()
            if not notes:
                return SkillResult.say("I'm not holding anything for you.")
            if not text or text in ("everything", "it all", "all of it", "all"):
                _save([])
                return SkillResult.say("Forgotten.")
            kept = [n for n in notes if not _refers_to(text, n["text"])]
            if len(kept) == len(notes):
                return SkillResult.fail(f"I'm not holding anything about {text}.")
            _save(kept)
        return SkillResult.say("Forgotten.")
