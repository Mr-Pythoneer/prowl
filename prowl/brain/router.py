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
from . import prematch
from .brain import Brain, BrainError

_SYSTEM = """You are the router for Prowl, a Mac voice assistant. Decide how to handle the \
user's message and output ONLY one JSON object.

Actions:
- "skill": it maps onto exactly ONE built-in skill below. Set "skill" (exact name) and "args".
- "chat": a factual question you can answer, small talk, or a greeting. Put the answer in "reply".
- "escalate": an open-ended or MULTI-STEP task that needs an autonomous agent — organizing / \
renaming / sorting many files, writing or running code, research, editing documents, or anything \
not covered by a single skill.

Built-in skills:
{catalog}

Guidelines:
- Use a skill ONLY when one clearly matches; use its exact name and only args it accepts.
- If the task needs several steps, judgement, or code, choose "escalate" — even if a skill name \
looks vaguely related. Do NOT force it into find_files/open_url just because it mentions files or a topic.
- Answer simple factual questions yourself with "chat" + "reply". Do NOT open a web page for a fact you know.
- Never invent a skill name.

Output ONLY: {"action":"skill|chat|escalate","skill":<name or null>,"args":{},"reply":<string or null>}

Examples:
User: what's the capital of France
{"action":"chat","skill":null,"args":{},"reply":"Paris."}
User: set volume to 20
{"action":"skill","skill":"set_volume","args":{"level":20}}
User: organize my downloads into folders by type and rename them
{"action":"escalate","skill":null,"args":{},"reply":null}
User: write a python script to resize my images and run it
{"action":"escalate","skill":null,"args":{},"reply":null}
User: summarize this PDF and email it to my boss
{"action":"escalate","skill":null,"args":{},"reply":null}
User: tell me a joke
{"action":"chat","skill":null,"args":{},"reply":"Why did the developer go broke? He used up all his cache."}
"""


@dataclasses.dataclass
class Decision:
    action: str                       # "chat" | "skill" | "escalate"
    skill: str | None = None
    args: dict[str, Any] = dataclasses.field(default_factory=dict)
    reply: str | None = None
    raw: str = ""                     # the model's raw output (for logging)


class Router:
    def __init__(self, config: Config, local: Brain | None = None):
        self.cfg = config
        self.local = local or Brain(config)

    def decide(self, utterance: str) -> Decision:
        # 1) Fast, model-independent path: high-confidence phrase -> skill.
        pm = prematch.match(utterance)
        if pm:
            skill, args = pm
            reg = skills_pkg.REGISTRY.get(skill)
            if reg is not None and reg.spec.enabled:
                return Decision(action="skill", skill=skill, args=args, raw="prematch")

        # 2) Otherwise, ask the local model to classify.
        catalog = skills_pkg.specs_text()
        # Use replace, not str.format: the prompt's JSON examples contain braces.
        system = _SYSTEM.replace("{catalog}", catalog)
        try:
            raw = self.local.chat(system, utterance, json_mode=True, temperature=0.0)
        except BrainError:
            # No brain reachable at all (no cloud key/network AND no Ollama):
            # fall back to escalation if available, else say so plainly.
            backend_on = self.cfg.escalation_enabled()
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
        if not isinstance(d.args, dict):
            d.args = {}
        if d.action == "skill":
            skill = skills_pkg.REGISTRY.get(d.skill or "")
            if skill is None or not skill.spec.enabled:
                # Hallucinated / disabled skill -> escalate instead of guessing
                # (or answer locally when escalation is off / offline mode).
                return self._reject(d)
            # A small model sometimes picks a plausible-looking skill but leaves
            # its arguments empty ("sort my Downloads folder" -> find_files with
            # query=""). Running that asks the user a nonsense question, so
            # treat a skill with no usable argument as a bad guess.
            if _required_args(skill) and not _has_usable_arg(skill, d.args):
                return self._reject(d)
        return d

    def _reject(self, d: Decision) -> Decision:
        """Discard a bad skill choice: hand it to the agent, or answer locally."""
        d.action = "escalate" if self.cfg.escalation_enabled() else "chat"
        d.skill = None
        d.args = {}
        return d


def _required_args(skill) -> list[str]:
    """Arg names the skill genuinely needs.

    ``SkillSpec.args`` has no required/optional flag, so read it from the
    description the skill author wrote: anything mentioning "optional" or a
    "default" is not required.
    """
    required = []
    for name, desc in (skill.spec.args or {}).items():
        text = (desc or "").lower()
        if "optional" in text or "default" in text or "omitted" in text:
            continue
        required.append(name)
    return required


def _has_usable_arg(skill, args: dict) -> bool:
    """True if at least one required arg arrived with a non-empty value."""
    for name in _required_args(skill):
        val = args.get(name)
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        return True
    return False
