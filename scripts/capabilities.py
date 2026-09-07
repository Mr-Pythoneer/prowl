"""Capability report — what Prowl actually does with real-world requests.

Runs a corpus of natural phrasings through the full router + executor in
dry-run and prints where each one lands. Unlike scripts/shakedown.py (which
asserts a fixed expectation per case), this is for reading: it shows the
spread across skills, the agent, and plain chat, and is how the "takes a
screenshot when asked to rename screenshots" class of bug gets spotted.

Note the media rows report "nothing is running" here: launching a music app is
deliberately skipped under dry-run.

    python scripts/capabilities.py
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.CRITICAL)
from prowl import skills as sp
from prowl.core.config import Config
from prowl.core.context import Context
from prowl.executor import Orchestrator

REQUESTS = [
    # apps
    "open Proton VPN", "open Discord", "open Blender", "open Steam",
    "open LM Studio", "quit Discord", "switch to Chrome",
    # web
    "open youtube", "open github", "search the web for pytorch tutorials",
    "open reddit.com",
    # media
    "play some music", "pause the music", "next track", "turn it up", "mute",
    # system
    "take a screenshot", "turn on dark mode", "lock my mac",
    "how much disk space do I have", "what's on my clipboard",
    # files
    "find my resume", "show me recent downloads", "clean up my mac",
    # things that should go to the agent
    "play a video", "rename all my screenshots by date",
    "summarise the PDFs on my desktop",
    # chat
    "what's the capital of Japan",
]

cfg = Config.load(); sp.load_all()
orch = Orchestrator(cfg)
spoken = []
ctx = Context(config=cfg, log=logging.getLogger("cap"),
              speak=lambda t: spoken.append(t),
              confirm=lambda q: False, dry_run=True)

print(f"{'request':42} {'route':>10}  result")
print("-" * 108)
rows = []
for r in REQUESTS:
    spoken.clear()
    d = orch.router.decide(r)
    if d.action == "skill" and d.skill:
        res = orch.executor.run_skill(d.skill, d.args, ctx)
        route, out, ok = d.skill, res.speech, res.ok
    elif d.action == "escalate":
        route, out, ok = "→ agent", "(would hand to Claude)", True
    else:
        route, out, ok = "chat", (d.reply or "(local answer)")[:60], True
    mark = "  " if ok else "！"
    print(f"{mark}{r:41} {route:>10}  {out[:52]}")
    rows.append((r, route, ok))

print("-" * 108)
from collections import Counter
c = Counter(rt for _, rt, _ in rows)
print("routes:", dict(c))
print("failures:", [r for r, _, ok in rows if not ok])
