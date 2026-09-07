"""Escalation backend — the "smart" half of Prowl.

When the fast local model decides a request is too open-ended for a built-in
skill (e.g. "sort my screenshots into folders by month and rename them"), Prowl
hands the whole task to a full agent:

* ``openclaw`` — ``openclaw agent -m <task> --json`` runs one turn of the
  already-configured OpenClaw agent (Claude Opus 4.8, with shell access).
* ``claude``   — ``claude -p <task>`` runs Claude Code non-interactively.

Both are invoked as subprocesses; no API keys live in Prowl.
"""
from __future__ import annotations

import json
import subprocess

from ..core.config import Config


class EscalationError(RuntimeError):
    pass


class Escalator:
    def __init__(self, config: Config):
        self.cfg = config
        self.backend = config.escalation_backend

    @property
    def enabled(self) -> bool:
        # Off if the backend is disabled OR offline mode is on.
        return self.cfg.escalation_enabled()

    def run(self, task: str) -> str:
        if not self.enabled:
            raise EscalationError("Escalation is disabled (escalation_backend = off).")
        if self.backend == "openclaw":
            return self._openclaw(task)
        if self.backend == "claude":
            return self._claude(task)
        raise EscalationError(f"Unknown escalation backend: {self.backend!r}")

    # -- backends -------------------------------------------------------------
    def _openclaw(self, task: str) -> str:
        cmd = [
            "openclaw", "agent",
            "-m", task,
            "--json",
            "--thinking", str(self.cfg.escalation_thinking),
            "--timeout", str(self.cfg.escalation_timeout),
        ]
        proc = self._exec(cmd)
        text = proc.stdout.strip()
        # `--json` prints a JSON envelope; pull the human reply out of it, but
        # degrade gracefully if the shape changes between OpenClaw versions.
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return text
        for key in ("reply", "text", "message", "output", "content", "result"):
            val = obj.get(key) if isinstance(obj, dict) else None
            if isinstance(val, str) and val.strip():
                return val.strip()
        return text

    def _claude(self, task: str) -> str:
        proc = self._exec(["claude", "-p", "--output-format", "text", task])
        return proc.stdout.strip()

    def _exec(self, cmd: list[str]) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.cfg.escalation_timeout + 30,
            )
        except FileNotFoundError as exc:
            raise EscalationError(f"`{cmd[0]}` is not installed or not on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise EscalationError("The agent took too long and was stopped.") from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            # The CLI login expires periodically; that is a re-login, not a bug.
            # Say so in words the user can act on instead of an exit code.
            low = err.lower()
            if any(k in low for k in (
                "not logged in", "/login", "failed to authenticate",
                "oauth", "session expired", "unauthorized",
            )):
                raise EscalationError(
                    "The Claude CLI is signed out. Run `claude` in a terminal "
                    "and use /login, then try again."
                )
            raise EscalationError(f"Agent exited with {proc.returncode}: {err}")
        return proc
