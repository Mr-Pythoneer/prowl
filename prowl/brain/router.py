"""The router: turn a spoken/typed utterance into a decision.

Uses the fast local model in JSON mode to choose exactly one of:

* ``chat``     — just answer (small talk, quick facts) using the local model.
* ``skill``    — run a named built-in skill with structured args.
* ``escalate`` — hand the whole task to the OpenClaw/Claude agent.

The model only *classifies*; it never executes anything. If classification
fails or is ambiguous, we fail safe toward ``chat`` (never toward a destructive
skill).
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any

from .. import skills as skills_pkg
from ..core.config import Config
from .local import LocalBrain, LocalModelError

_SYSTEM = """You are the router for Prowl, a Mac voice assistant. Classify the user's request.

Choose ONE action:
- "skill": the request maps cleanly onto one built-in skill below. Fill "skill" and "args".
- "escalate": the request is open-ended, multi-step, or needs judgement/agentic work \
(writing/editing files, organizing many files, research, coding, anything not covered by a skill).
- "chat": small talk, a quick factual question, or a greeting. Put the answer in "reply".

Built-in skills:
{catalog}

Rules:
- Only use a skill name from the list. Only include args that skill accepts.
- Prefer "skill" when one clearly fits; prefer "escalate" for anything bigger or vaguer.
- When unsure between skill and escalate, choose "escalate". Never invent a skill.
- Respond with ONLY a JSON object, no prose:
  {{"action":"skill|escalate|chat","skill":<name or null>,"args":{{}},"reply":<string or null>}}
"""


@dataclasses.dataclass
class Decision:
    action: str                       # "chat" | "skill" | "escalate"
    skill: str | None = None
    args: dict[str, Any] = dataclasses.field(default_factory=dict)
    reply: str | None = None
    raw: str = ""                     # the model's raw output (for logging)


class Router:
    def __init__(self, config: Config, local: LocalBrain | None = None):
        self.cfg = config
        self.local = local or LocalBrain(config)

    def decide(self, utterance: str) -> Decision:
        catalog = skills_pkg.specs_text()
        system = _SYSTEM.format(catalog=catalog)
        try:
            raw = self.local.chat(system, utterance, json_mode=True, temperature=0.0)
        except LocalModelError:
            # Local model down: fall back to escalation if available, else chat.
            backend_on = self.cfg.escalation_backend not in ("off", "", None)
            return Decision(
                action="escalate" if backend_on else "chat",
                reply=None if backend_on else "My local brain is offline right now.",
            )

        decision = self._parse(raw)
        return self._validate(decision)

    # -- helpers --------------------------------------------------------------
    def _parse(self, raw: str) -> Decision:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # Try to salvage the first {...} block.
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end > start:
                try:
                    obj = json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    return Decision(action="chat", reply=None, raw=raw)
            else:
                return Decision(action="chat", reply=None, raw=raw)

        return Decision(
            action=str(obj.get("action", "chat")).lower().strip(),
            skill=obj.get("skill") or None,
            args=obj.get("args") or {},
            reply=obj.get("reply") or None,
            raw=raw,
        )

    def _validate(self, d: Decision) -> Decision:
        if d.action not in ("chat", "skill", "escalate"):
            d.action = "chat"
        if d.action == "skill":
            skill = skills_pkg.REGISTRY.get(d.skill or "")
            if skill is None or not skill.spec.enabled:
                # Hallucinated / disabled skill -> escalate instead of guessing.
                backend_on = self.cfg.escalation_backend not in ("off", "", None)
                d.action = "escalate" if backend_on else "chat"
                d.skill = None
        if not isinstance(d.args, dict):
            d.args = {}
        return d
