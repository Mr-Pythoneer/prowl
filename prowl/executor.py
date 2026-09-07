"""The executor + orchestrator.

``Executor`` runs a single chosen skill behind the safety layer (confirmation for
destructive skills, dry-run support, logging, exception isolation).

``Orchestrator`` ties everything together: it takes one utterance, asks the
:class:`Router` what to do, and then either answers locally, runs a skill, or
escalates — reporting back through the :class:`Context` (``speak``/``confirm``).
This is the single entry point every front-end (CLI, menu bar, voice) calls.
"""
from __future__ import annotations

from typing import Any

from . import skills as skills_pkg
from .brain.escalate import Escalator, EscalationError
from .brain.brain import Brain, BrainError
from .brain.router import Decision, Router
from .core.config import Config
from .core.context import Context
from .skills.base import SkillResult


class Executor:
    """Runs one skill with the safety layer applied."""

    def __init__(self, config: Config):
        self.cfg = config

    def run_skill(self, name: str, args: dict[str, Any], ctx: Context) -> SkillResult:
        skill = skills_pkg.REGISTRY.get(name)
        if skill is None:
            return SkillResult.fail(f"I don't have a skill called {name}.")
        if not skill.spec.enabled:
            return SkillResult.fail(f"The {name} skill is turned off.")

        ctx.note(f"skill={name} args={args} dry_run={ctx.dry_run}")

        # Safety gate: destructive skills confirm before doing anything, unless
        # we're in dry-run (they'll only describe) or confirmation is disabled.
        if skill.spec.destructive and not ctx.dry_run and self.cfg.confirm_destructive:
            # A skill may show a specific prompt (e.g. the exact shell command);
            # otherwise fall back to a generic description of what it will do.
            question = skill.confirm_prompt(args) or \
                f"This will {skill.spec.description.lower()}. Go ahead?"
            if not ctx.confirm(question):
                return SkillResult.fail("Okay, cancelled — nothing was changed.")

        try:
            result = skill.run(args, ctx)
        except Exception as exc:  # a skill must never crash the whole assistant
            ctx.note(f"skill {name} raised: {exc!r}")
            return SkillResult.fail(f"That didn't work: {exc}")

        ctx.note(f"skill={name} ok={result.ok} -> {result.speech}")
        return result


class Orchestrator:
    """One utterance in, one action taken, reported through the Context."""

    def __init__(self, config: Config | None = None):
        self.cfg = config or Config.load()
        skills_pkg.load_all()
        self.local = Brain(self.cfg)
        self.router = Router(self.cfg, self.local)
        self.executor = Executor(self.cfg)
        self.escalator = Escalator(self.cfg)

    def handle(self, utterance: str, ctx: Context) -> SkillResult:
        utterance = (utterance or "").strip()
        if not utterance:
            return SkillResult.fail("I didn't catch that.")

        ctx.note(f"heard: {utterance!r}")
        decision = self.router.decide(utterance)
        ctx.note(f"decision: {decision.action} skill={decision.skill}")

        if decision.action == "skill" and decision.skill:
            result = self.executor.run_skill(decision.skill, decision.args, ctx)
            ctx.speak(result.speech)
            return result

        if decision.action == "escalate":
            return self._escalate(utterance, ctx)

        # chat
        return self._chat(utterance, decision, ctx)

    # -- branches -------------------------------------------------------------
    def _chat(self, utterance: str, decision: Decision, ctx: Context) -> SkillResult:
        if decision.reply:
            ctx.speak(decision.reply)
            return SkillResult.say(decision.reply)
        try:
            answer = self.local.reply(utterance)
        except BrainError as exc:
            msg = ("I can't reach a model right now — no cloud connection, and "
                   "Ollama isn't running locally.")
            ctx.speak(msg)
            return SkillResult.fail(msg, detail=str(exc))
        ctx.speak(answer)
        return SkillResult.say(answer)

    def _escalate(self, utterance: str, ctx: Context) -> SkillResult:
        if not self.escalator.enabled:
            # Escalation is off (offline mode, or no backend configured).
            return self._offline_fallback(utterance, ctx)
        ctx.speak("On it — this one needs the smart agent, give me a moment.")
        try:
            answer = self.escalator.run(utterance)
        except EscalationError as exc:
            msg = f"I couldn't reach the smart agent: {exc}"
            ctx.speak("I couldn't reach the smart agent.")
            return SkillResult.fail(msg, detail=str(exc))
        # The escalated answer can be long; speak a trimmed version, show the rest.
        spoken = answer if len(answer) <= 400 else answer[:380].rsplit(" ", 1)[0] + "…"
        ctx.speak(spoken)
        return SkillResult.say(spoken, detail=answer)

    def _offline_fallback(self, utterance: str, ctx: Context) -> SkillResult:
        """Handle an 'escalate' decision without any online call.

        In offline mode we tell the local model to answer if it can, or to say
        honestly (in one line) that the task needs online mode — rather than
        pretending to run an agent it can't reach.
        """
        if self.cfg.get("offline"):
            system = (
                f"You are {self.cfg.get('assistant_name', 'Bob')} running in OFFLINE "
                "mode: a local model only, with no "
                "online agent and no ability to run multi-step tool tasks. "
                "If the user's request is a question, answer in one or two short spoken "
                "sentences. If it needs actions/tools you don't have offline, say briefly "
                "that it needs online mode (they can run `prowl offline off`). No markdown."
            )
            try:
                answer = self.local.chat(system, utterance, temperature=0.4)
            except BrainError:
                answer = "My local brain is offline — start Ollama and try again."
            ctx.speak(answer)
            return SkillResult.say(answer)
        # No escalation backend configured (but not offline): best-effort local answer.
        return self._chat(utterance, Decision(action="chat"), ctx)
