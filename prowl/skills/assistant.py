"""Open the on-demand assistant (~/voice-assistant) as a web page, and hand
it multi-step website tasks.

The assistant isn't always running. These skills start its page server
(`run.sh --web`, which opens the page in your browser or reuses one that's
already open) and, for a task, post it to the page's session so you can watch
the browser agent work — including its CAPTCHA alerts. Bob never drives the
browser itself; that lives in the assistant.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ..core.context import Context
from .base import Skill, SkillResult, SkillSpec, register

_HOME = Path.home()


def _paths(ctx: Context) -> tuple[Path, Path]:
    root = Path(str(ctx.config.get("assistant_dir", _HOME / "voice-assistant"))).expanduser()
    return root, _HOME / ".voice-assistant" / "web.json"


def _running(state: Path) -> dict | None:
    import urllib.request

    try:
        st = json.loads(state.read_text())
        req = urllib.request.Request(f"http://127.0.0.1:{st['port']}/api/ping", headers={"X-Token": st["token"]})
        with urllib.request.urlopen(req, timeout=1) as r:
            return st if r.status == 200 else None
    except Exception:  # noqa: BLE001
        return None


def _open_page(ctx: Context) -> dict | str:
    """Start (or reuse) the assistant page. Returns its state, or an error."""
    root, state = _paths(ctx)
    run = root / "run.sh"
    if not run.exists():
        return f"I can't find the assistant at {root}."
    if ctx.dry_run:
        return {"port": 0, "token": ""}
    log = open(_HOME / ".voice-assistant" / "web.log", "ab") if (_HOME / ".voice-assistant").exists() else subprocess.DEVNULL
    # Detached: the page server outlives this turn and exits by itself
    # when its tab is closed.
    subprocess.Popen([str(run), "--web"], cwd=str(root), stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):  # up to ~15 s
        if st := _running(state):
            return st
        time.sleep(0.25)
    return "The assistant page didn't start. See ~/.voice-assistant/web.log."


def _send(st: dict, text: str) -> bool:
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{st['port']}/api/send", method="POST",
                                 data=json.dumps({"text": text}).encode(),
                                 headers={"X-Token": st["token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 202
    except Exception:  # noqa: BLE001
        return False


@register
class OpenAssistant(Skill):
    spec = SkillSpec(
        name="open_assistant",
        description="open the assistant's web page (the on-demand assistant with calendar, memory and browser agent)",
        examples=["open assistant", "open the assistant", "show me the assistant"],
        args={},
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        st = _open_page(ctx)
        if isinstance(st, str):
            return SkillResult.fail(st)
        return SkillResult.say("Opening the assistant.")


@register
class Browse(Skill):
    spec = SkillSpec(
        name="browse",
        description=("do a multi-step task on websites — find or compare prices, flights, products, "
                     "look something up on a site, fill in a web form, order online — using the "
                     "assistant's browser agent (it pauses and alerts you on CAPTCHAs)"),
        examples=["find me the cheapest flight to Tokyo in December",
                  "compare AirPods prices on Amazon and Best Buy",
                  "order my usual pizza"],
        args={"task": "the whole task, in plain words"},
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        task = str(args.get("task") or args.get("query") or "").strip()
        if not task:
            return SkillResult.fail("What should I do in the browser?")
        st = _open_page(ctx)
        if isinstance(st, str):
            return SkillResult.fail(st)
        if ctx.dry_run:
            return SkillResult.say(f"Would hand to the assistant: {task}")
        if not _send(st, task):
            return SkillResult.fail("I opened the assistant but couldn't hand it the task.")
        return SkillResult.say("I've handed that to the assistant — watch its page.", detail=task)
