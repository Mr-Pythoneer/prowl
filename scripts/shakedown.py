#!/usr/bin/env python3
"""Shakedown — exercise Prowl end to end and report what's broken.

Two passes, no side effects:

1. **Routing** — run a corpus of utterances through the deterministic
   pre-router (and optionally the local model) and check each lands on the
   expected skill. Catches "open google opens an app" class bugs.
2. **Skills** — invoke every registered skill with representative args under
   ``dry_run=True``, so destructive ones describe instead of acting. Catches
   import errors, crashes, and skills that fail on valid input.

Usage:
    python scripts/shakedown.py             # pre-router only (fast, offline)
    python scripts/shakedown.py --full      # also route misses through Ollama
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prowl import skills as skills_pkg          # noqa: E402
from prowl.brain.prematch import match          # noqa: E402
from prowl.core.config import Config            # noqa: E402
from prowl.core.context import Context          # noqa: E402
from prowl.executor import Executor             # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# (utterance, expected skill or None) — None means "must NOT prematch"
# (a question or an open-ended task that belongs to the model / escalation).
ROUTING_CASES: list[tuple[str, str | None]] = [
    # system
    ("open Safari", "open_app"),
    ("launch Xcode", "open_app"),
    ("fire up Terminal", "open_app"),
    ("set volume to 40", "set_volume"),
    ("turn it up to 80", "set_volume"),
    ("mute", "set_volume"),
    ("turn it up", "set_volume"),
    ("turn the volume down", "set_volume"),
    ("louder", "set_volume"),
    ("unmute", "set_volume"),
    ("turn on dark mode", "toggle_dark_mode"),
    ("switch to light mode", "toggle_dark_mode"),
    ("take a screenshot", "screenshot"),
    ("grab a screenshot of the screen", "screenshot"),
    ("what's on my clipboard", "clipboard"),
    ("read the clipboard", "clipboard"),
    ("how's my battery", "system_status"),
    ("how much disk space do I have", "system_status"),
    ("lock my mac", "lock_or_sleep"),
    ("lock the screen", "lock_or_sleep"),
    ("go to sleep", "lock_or_sleep"),
    # files
    ("find my tax documents", "find_files"),
    ("where's my resume file", "find_files"),
    ("show me recent downloads", "recent_downloads"),
    ("what did I download recently", "recent_downloads"),
    # apps
    ("quit Music", "quit_app"),
    ("force quit Photos", "quit_app"),
    ("switch to Safari", "activate_app"),
    ("pause", "media_control"),
    ("next track", "media_control"),
    ("skip this song", "media_control"),
    ("previous track", "media_control"),
    # web
    ("open google", "open_site"),
    ("open youtube", "open_site"),
    ("open gmail", "open_site"),
    ("open github", "open_site"),
    ("open claude", "open_app"),      # installed app beats the site shorthand
    ("open discord", "open_app"),
    ("open example.com", "open_url"),
    ("go to https://news.ycombinator.com", "open_url"),
    ("google best ramen near me", "web_search"),
    ("search the web for python asyncio", "web_search"),
    ("look up the weather in Tokyo", "web_search"),
    # cleanup
    ("clean up my mac", "cleanup"),
    ("empty the trash", "cleanup"),
    ("free up some disk space", "cleanup"),
    # must NOT prematch — these belong to the model or the smart agent
    ("what is the capital of France", None),
    ("what is 2 plus 2", None),
    ("why is the sky blue", None),
    ("tell me a joke", None),
    ("sort my Downloads folder by file type and rename them by date", None),
    ("summarize the PDFs on my desktop into a note", None),
    ("write a python script that renames my screenshots", None),
    ("how are you", None),
]

# Representative args per skill for the dry-run smoke test.
# Substrings that mean "the Mac isn't in that state right now", not "broken".
ENV_DEPENDENT: dict[str, tuple[str, ...]] = {
    "media_control": ("is running",),
    "clipboard": ("clipboard is empty",),
}

SKILL_ARGS: dict[str, dict] = {
    "open_app": {"app": "Safari"},
    "set_volume": {"level": 40},
    "toggle_dark_mode": {"mode": "dark"},
    "screenshot": {},
    "clipboard": {"action": "read"},
    "system_status": {},
    "lock_or_sleep": {"action": "lock"},
    "find_files": {"query": "prowl"},
    "open_file": {"path": "~/.prowl/config.json"},
    "reveal_in_finder": {"path": "~/.prowl"},
    "recent_downloads": {},
    "quit_app": {"app": "TextEdit"},
    "activate_app": {"app": "Finder"},
    "list_running_apps": {},
    "media_control": {"action": "pause"},
    "open_url": {"url": "example.com"},
    "web_search": {"query": "hello"},
    "open_site": {"name": "github"},
    "cleanup": {"categories": ["trash"]},
    "run_shell": {"command": "echo hello"},
}


# (utterance, expected trick or None). Tricks bypass the router entirely, so
# they need their own corpus — the routing set above would never exercise them.
TRICK_CASES: list[tuple[str, str | None]] = [
    ("do a backflip", "backflip"),
    ("backflip", "backflip"),
    ("do a barrel roll", "tumble"),
    ("do as barrel row", "tumble"),          # how it actually gets transcribed
    ("do a 360", "spin"),
    ("spin around", "spin"),
    ("jump", "jump"),
    ("dance", "dance"),
    ("wave", "wave"),
    ("nod", "nod"),
    ("shrug", "shrug"),
    ("stretch", "stretch"),
    ("cheer", "cheer"),
    ("wobble", "wobble"),
    # Must NOT be stolen from the router.
    ("jump to the next song", None),
    ("open safari", None),
    ("dance music playlist", None),
    ("stop", None),
]

# (utterance, expected control action or None).
CONTROL_CASES: list[tuple[str, str | None]] = [
    ("stop", "stop"),
    ("shut up", "stop"),
    ("never mind", "stop"),
    ("cancel", "stop"),
    ("stop listening", "sleep"),
    ("take a break", "sleep"),
    ("wake up", "wake"),
    ("hide", "hide"),
    ("come back", "show"),
    ("say that again", "repeat"),
    ("what can you do", "help"),
    # Must stay with the router / skills.
    ("stop the music", None),
    ("go to sleep", None),                   # means sleep the Mac
    ("open safari", None),
    ("hide my downloads folder", None),
]

# (utterance, should it wake him?) — the wake matcher, which gates everything.
WAKE_CASES: list[tuple[str, bool]] = [
    ("hey bob", True),
    ("hey bob open safari", True),
    ("ok bob what time is it", True),
    ("bob", True),
    ("bob open safari", True),               # bare name + safe command
    ("bob turn it up", True),
    # Overheard conversation must never wake him.
    ("Bob was asking about it", False),
    ("I told bob about it", False),
    ("the bobcat ran", False),
    ("bobbing along", False),
    # Bare name must not carry a destructive command.
    ("bob delete my documents", False),
    ("bob quit safari", False),
]


def run_tricks() -> list[str]:
    from prowl.ui.buddy import match_trick

    failures = []
    print(f"\n{DIM}── tricks ──{RESET}")
    for utterance, expected in TRICK_CASES:
        got = match_trick(utterance)
        ok = got == expected
        if not ok:
            failures.append(f"trick {utterance!r}: expected {expected}, got {got}")
        mark = f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {DIM}{utterance!r} -> {got}{RESET}")
    return failures


def run_controls() -> list[str]:
    from prowl.voice.control import match_control

    failures = []
    print(f"\n{DIM}── control phrases ──{RESET}")
    for utterance, expected in CONTROL_CASES:
        got = match_control(utterance)
        ok = got == expected
        if not ok:
            failures.append(f"control {utterance!r}: expected {expected}, got {got}")
        mark = f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {DIM}{utterance!r} -> {got}{RESET}")
    return failures


def run_wake() -> list[str]:
    from prowl.core.config import Config
    from prowl.voice.wake import WakeListener

    failures = []
    print(f"\n{DIM}── wake word ──{RESET}")
    listener = WakeListener(Config.load())
    strict, loose = listener._pattern(), listener._loose_pattern()
    for utterance, expected in WAKE_CASES:
        if strict.search(utterance):
            got = True
        else:
            m = loose.search(utterance)
            got = bool(m and listener._safe_bare_command(m.group(1)))
        ok = got == expected
        if not ok:
            failures.append(
                f"wake {utterance!r}: expected {'wake' if expected else 'ignore'}")
        mark = f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {DIM}{utterance!r} -> "
              f"{'wake' if got else 'ignore'}{RESET}")
    return failures


def run_routing(full: bool) -> list[str]:
    failures = []
    print(f"\n{DIM}── routing ──{RESET}")
    router = None
    if full:
        from prowl.brain.brain import Brain
        from prowl.brain.router import Router
        cfg = Config.load()
        router = Router(cfg, Brain(cfg))

    for utterance, expected in ROUTING_CASES:
        got = match(utterance)
        got_skill = got[0] if got else None
        via = "prematch"
        if got_skill is None and router is not None:
            d = router.decide(utterance)
            got_skill = d.skill if d.action == "skill" else None
            via = f"model:{d.action}"

        if expected is None:
            ok = got_skill is None
        else:
            ok = got_skill == expected
        mark = f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}"
        if not ok:
            failures.append(f"{utterance!r}: expected {expected}, got {got_skill} ({via})")
            print(f"  {mark}  {utterance!r}\n        expected={expected} got={got_skill} via={via}")
        else:
            print(f"  {mark}  {DIM}{utterance!r} -> {got_skill or 'chat/escalate'}{RESET}")
    return failures


def run_skills() -> list[str]:
    failures = []
    print(f"\n{DIM}── skills (dry run) ──{RESET}")
    cfg = Config.load()
    skills_pkg.load_all()
    log = logging.getLogger("prowl.shakedown")
    ctx = Context(
        config=cfg, log=log,
        speak=lambda t: None,
        confirm=lambda q: False,     # a dry run must never be asked to confirm
        dry_run=True,
    )
    ex = Executor(cfg)

    for name in sorted(skills_pkg.REGISTRY):
        args = SKILL_ARGS.get(name)
        if args is None:
            print(f"  {YELLOW}skip{RESET}  {name} {DIM}(no test args){RESET}")
            failures.append(f"{name}: no test args defined in shakedown")
            continue
        try:
            res = ex.run_skill(name, args, ctx)
        except Exception as exc:  # noqa: BLE001 - that's what we're hunting
            print(f"  {RED}RAISE{RESET} {name}: {exc!r}")
            failures.append(f"{name}: raised {exc!r}")
            continue
        if res.ok:
            print(f"  {GREEN}ok{RESET}    {DIM}{name} -> {res.speech}{RESET}")
        elif any(k in res.speech.lower() for k in ENV_DEPENDENT.get(name, ())):
            print(f"  {YELLOW}n/a{RESET}   {DIM}{name} -> {res.speech} "
                  f"(machine state, not a defect){RESET}")
        else:
            print(f"  {RED}FAIL{RESET}  {name} -> {res.speech} {DIM}{res.detail[:120]}{RESET}")
            failures.append(f"{name}: {res.speech}")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true",
                    help="also send pre-router misses to the local model")
    args = ap.parse_args()
    logging.basicConfig(level=logging.CRITICAL)

    skills_pkg.load_all()          # wake checks consult the skill registry
    failures = (run_routing(args.full) + run_tricks() + run_controls()
                + run_wake() + run_skills())

    print(f"\n{DIM}── summary ──{RESET}")
    if failures:
        print(f"{RED}{len(failures)} finding(s):{RESET}")
        for f in failures:
            print(f"  • {f}")
        return 1
    print(f"{GREEN}All clear.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
