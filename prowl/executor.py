"""The executor + orchestrator.

``Executor`` runs a single chosen skill behind the safety layer (confirmation for
destructive skills, dry-run support, logging, exception isolation).

``Orchestrator`` ties everything together: it takes one utterance, asks the
:class:`Router` what to do, and then either answers locally, runs a skill, or
escalates — reporting back through the :class:`Context` (``speak``/``confirm``).
This is the single entry point every front-end (CLI, menu bar, voice) calls.
"""
from __future__ import annotations

import re
import subprocess
from typing import Any

from . import skills as skills_pkg
from .brain.escalate import Escalator, EscalationError
from .brain.brain import Brain, BrainError
from .brain.router import Decision, Router
from .core.config import Config
from .core.context import Context
from .skills.base import SkillResult

def _with_context(utterance: str) -> str:
    """Attach the clipboard and frontmost app to an escalated request.

    "What does this error mean" is the most natural thing to ask a desktop
    assistant, and it cannot work from words alone — "this" is whatever is on
    screen or on the clipboard. Only added when the request actually refers to
    something, so ordinary tasks aren't padded with irrelevant context.
    """
    if not _REFERS_TO_CONTEXT.search(utterance):
        return utterance

    bits = []
    try:
        front = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first '
             'application process whose frontmost is true'],
            capture_output=True, text=True, timeout=5).stdout.strip()
        if front and front != "Bob":
            bits.append(f"Frontmost app: {front}")
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    try:
        clip = subprocess.run(["pbpaste"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
        if clip:
            if len(clip) > 4000:
                clip = clip[:4000] + "\n… (truncated)"
            bits.append(f"Clipboard contents:\n{clip}")
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass

    if not bits:
        return utterance
    return utterance + "\n\n---\nContext from the user's Mac:\n" + "\n\n".join(bits)


# Requests that point at something the words alone don't carry.
_REFERS_TO_CONTEXT = re.compile(
    r"\b(this|that|it|here|on my screen|on screen|the error|the clipboard|"
    r"what i copied|selected|highlighted)\b", re.I)


# Words that stand in for whatever the last turn acted on.
_PRONOUNS = frozenset({"it", "that", "this", "them", "those", "it again",
                       "that one", "the app", "the same"})

# "again" as a request to redo the last action (not the TTS "say that again",
# which control.py handles before this is ever reached).
_ASKS_REPEAT = re.compile(
    r"^\s*(?:do (?:it|that) again|again|one more time|redo (?:it|that)|"
    r"same again|do the same(?: thing)?(?: again)?)\s*[.!?]*\s*$", re.I)


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
        # One turn of memory, so follow-ups work. Without it "now close it"
        # routed to quit_app with the app literally named "it", and a
        # misrecognition could only be recovered by repeating the whole
        # sentence. Deliberately shallow: the last skill and its arguments,
        # not a conversation history.
        self._last_skill: str | None = None
        self._last_args: dict[str, Any] = {}
        self._last_app: str = ""
        # When set, replaces the chat system prompt entirely — used by moods,
        # which need to change his register rather than append to it.
        self.persona: str = ""

    def handle(self, utterance: str, ctx: Context) -> SkillResult:
        utterance = (utterance or "").strip()
        if not utterance:
            return SkillResult.fail("I didn't catch that.")

        ctx.note(f"heard: {utterance!r}")

        # "do that again" replays the last turn rather than being re-routed —
        # the model would otherwise have to guess what "that" was.
        if _ASKS_REPEAT.match(utterance):
            if not self._last_skill:
                msg = "I haven't done anything yet."
                ctx.speak(msg)
                return SkillResult.fail(msg)
            ctx.note(f"repeating: {self._last_skill} {self._last_args}")
            result = self.executor.run_skill(
                self._last_skill, dict(self._last_args), ctx)
            ctx.speak(result.speech)
            return result

        decision = self.router.decide(utterance)
        self._resolve_references(decision)
        ctx.note(f"decision: {decision.action} skill={decision.skill}")

        if decision.action == "skill" and decision.skill:
            result = self.executor.run_skill(decision.skill, decision.args, ctx)
            self._remember(decision)
            ctx.speak(result.speech)
            return result

        if decision.action == "escalate":
            return self._escalate(utterance, ctx)

        # chat
        return self._chat(utterance, decision, ctx)

    # -- one turn of memory ---------------------------------------------------
    def _remember(self, decision: Decision) -> None:
        """Keep just enough of this turn to resolve the next one's pronouns."""
        self._last_skill = decision.skill
        self._last_args = dict(decision.args or {})
        app = str(self._last_args.get("app") or "").strip()
        if app and app.lower() not in _PRONOUNS:
            self._last_app = app

    def _resolve_references(self, decision: Decision) -> None:
        """Replace "it"/"that" in the decision's args with the last subject.

        Only pronouns are substituted, and only from the immediately preceding
        turn — enough for "open Safari … now close it", without pretending to
        hold a conversation.
        """
        if decision.action != "skill" or not isinstance(decision.args, dict):
            return
        app = str(decision.args.get("app") or "").strip().lower()
        if app in _PRONOUNS and self._last_app:
            decision.args["app"] = self._last_app

    # -- branches -------------------------------------------------------------
    def _chat(self, utterance: str, decision: Decision, ctx: Context) -> SkillResult:
        if decision.reply:
            ctx.speak(decision.reply)
            return SkillResult.say(decision.reply)
        try:
            if self.persona:
                answer = self.local.chat(self.persona, utterance, temperature=0.85)
            else:
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

        # Escalation hands the raw sentence to an agent with shell access, so it
        # is at least as dangerous as any skill marked destructive — and the
        # router deliberately sends destructive phrasings here (see
        # Router._MANAGES_FILES), which made this the one path where "delete my
        # old documents" reached a shell with nothing asked first.
        if self.cfg.confirm_destructive and not ctx.dry_run:
            if not ctx.confirm(
                    f"Hand this to the agent? It can run commands.\n\n{utterance}"):
                msg = "Okay, cancelled — nothing was sent."
                ctx.speak(msg)
                return SkillResult.fail(msg)

        ctx.speak("On it — this one needs the smart agent, give me a moment.")
        try:
            answer = self.escalator.run(_with_context(utterance))
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
