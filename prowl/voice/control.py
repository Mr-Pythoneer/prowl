"""Control phrases — the things Bob must obey instantly.

These never reach the router. "Stop" has to cut speech off mid-word; waiting on
a model call (or worse, escalating to the agent, which is what used to happen)
makes it useless. Matching is deterministic regex, resolved in microseconds.

    match_control("stop")            -> "stop"
    match_control("open safari")     -> None

The caller decides what each action means, since they need the front-end's
speech, buddy and listener — see ``ProwlApp._handle_control``.
"""
from __future__ import annotations

import re

# Ordered: the first pattern that matches wins, so put the specific ones first.
_CONTROLS: tuple[tuple[str, str], ...] = (
    # Silence him right now.
    ("stop", r"^\s*(?:stop|shut up|be quiet|quiet|silence|hush|shush|"
             r"never ?mind|nevermind|cancel|forget it|stop talking|"
             r"stop it|that's enough|thats enough|enough)\s*[.!]?\s*$"),
    # Stand down — stay running, stop listening. Deliberately NOT "go to
    # sleep" or a bare "sleep": those already mean *put the Mac to sleep*, and
    # that reading is the more natural one.
    ("sleep", r"^\s*(?:stop listening|pause listening|take a break|"
              r"leave me alone|stand down)\s*[.!]?\s*$"),
    # Start listening again.
    ("wake", r"^\s*(?:wake up|start listening|listen up|i'?m back|"
             r"resume listening)\s*[.!]?\s*$"),
    # Get off my screen (he keeps listening).
    ("hide", r"^\s*(?:hide|go away|hide yourself|get out of the way|"
             r"disappear|minimi[sz]e)\s*[.!]?\s*$"),
    ("show", r"^\s*(?:come back|show yourself|there you are|"
             r"where are you|come here)\s*[.!]?\s*$"),
    # Show / copy the detail from the last result. Every skill produces a
    # `detail` — the file list, the size breakdown, the full agent answer — and
    # until now only the CLI ever displayed it.
    ("details", r"^\s*(?:show (?:me )?(?:the )?(?:details|list|them|it|more)"
                r"|details|the list|show me more|what did you find"
                r"|show (?:me )?the (?:files|results))\s*[.!?]?\s*$"),
    ("copy", r"^\s*(?:copy (?:that|it|this|the (?:list|results|details))"
             r"|put (?:that|it) on (?:my )?(?:the )?clipboard)\s*[.!?]?\s*$"),
    # Say the last thing again.
    ("repeat", r"^\s*(?:repeat|say that again|what did you say|"
               r"come again|pardon|say again)\s*[.!]?\s*$"),
    # What am I allowed to ask for?
    ("help", r"^\s*(?:help|what can you do|what do you do|"
             r"what can i (?:say|ask)|show me what you can do|"
             r"list your skills|your commands)\s*[.!?]?\s*$"),
)

_COMPILED = tuple((name, re.compile(pattern, re.I)) for name, pattern in _CONTROLS)


def match_control(utterance: str) -> str | None:
    """Return the control action for *utterance*, or None if it isn't one.

    Deliberately strict — these must match the whole phrase. "stop" is a
    control; "stop the music" is a media command and belongs to the router.
    """
    text = (utterance or "").strip()
    if not text:
        return None
    for name, pattern in _COMPILED:
        if pattern.match(text):
            return name
    return None
