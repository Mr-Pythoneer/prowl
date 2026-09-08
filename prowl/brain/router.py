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
import re
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
            # No brain reachable at all (no cloud key or network AND no Ollama).
            #
            # This used to escalate, which fails open in the worst possible way:
            # with no classifier running, *every* utterance the microphone picks
            # up becomes an agent turn with shell access. Say so instead — an
            # assistant that admits it is offline is strictly better than one
            # that guesses with a shell.
            return Decision(
                action="chat",
                reply="My brain is offline right now — I couldn't reach the "
                      "cloud model or Ollama.",
            )

        decision = self._parse(raw)
        return self._validate(decision, utterance)

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

    # media_control only drives Music/Spotify. "play a video" is a different
    # job entirely, and a small model reaches for the nearest-looking skill.
    _VIDEO = re.compile(r"\b(video|movie|film|episode|show|youtube|netflix|"
                        r"trailer|clip)\b", re.I)

    # Verbs that operate on files that already exist. "rename all my screenshots
    # by date" is a job for the agent; a small model sees the word "screenshot"
    # and offers to take one, which is both wrong and surprising.
    _MANAGES_FILES = re.compile(
        r"\b(rename|sort|organi[sz]e|move|delete|remove|tidy|group|archive|"
        r"upload|share|convert|compress|resize|batch|back ?up|clean out)\b", re.I)
    # Skills that act immediately and would be the wrong answer to such a task.
    # Searching is as wrong an answer as acting: asked to *move* every .jpg on
    # the desktop, a small model reaches for find_files and reports "I found
    # 4000 images", which is neither what was asked nor obviously a failure.
    _ACTS_NOW = ("screenshot", "open_file", "reveal_in_finder", "open_app",
                 "open_url", "open_site", "web_search", "media_control",
                 "find_files", "recent_downloads", "clipboard",
                 "list_running_apps", "cleanup")

    def _validate(self, d: Decision, utterance: str = "") -> Decision:
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
            if d.skill == "media_control" and self._VIDEO.search(utterance):
                return self._reject(d)
            if d.skill in self._ACTS_NOW and self._MANAGES_FILES.search(utterance):
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
