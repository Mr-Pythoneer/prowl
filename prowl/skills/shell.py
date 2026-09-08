"""Guarded free-form shell skill.

This runs an arbitrary shell command the router/LLM produced from a spoken
request. It is powerful and inherently risky, so a few guardrails apply:

* The command is refused outright if it matches a small **denylist** of clearly
  catastrophic patterns (``sudo``, ``rm`` of ``/`` or ``~``, ``mkfs``, ``dd``,
  fork bombs, writes to raw disk devices, ``shutdown``/``reboot`` ...).
* The executor confirms first, because ``spec.destructive`` is ``True``.
* The skill obeys ``shell_skill_enabled`` in config and can be turned off.

**The denylist is a backstop, not a sandbox.** A determined command can still do
damage — this is a thin seat belt over a real shell running as the user, with no
containment, capability dropping, or filesystem isolation. For anything that
resembles real agentic work (multi-step edits, long-running jobs, tasks that
need judgement), escalation to OpenClaw is strongly preferred: it runs a full
agent that can reason about each step rather than firing one opaque string.
"""
from __future__ import annotations

import os
import re
import subprocess

from ..core.context import Context
from .base import Skill, SkillResult, SkillSpec, register

HOME = os.path.expanduser("~")
MAX_OUTPUT = 1500  # chars of combined stdout+stderr kept in `detail`
TIMEOUT = 60       # seconds before the command is killed

# --- denylist ---------------------------------------------------------------
# Each entry is (pattern, spoken reason). Patterns are compiled case-insensitively
# and searched against the raw command string. They are deliberately broad: when
# in doubt, refuse and let the user escalate to a real agent.
_DENY_PATTERNS: list[tuple[str, str]] = [
    (r"\bsudo\b",
     "I won't run anything with sudo."),
    (r"\b(shutdown|reboot|halt)\b",
     "I won't shut down or reboot the machine."),
    (r"\bmkfs(\.\w+)?\b",
     "I won't format a filesystem."),
    (r"(^|[\s|;&])dd\s",
     "I won't run dd — it can overwrite disks."),
    (r":\s*\(\s*\)\s*\{",
     "That looks like a fork bomb."),
    # rm -rf (any flag order) targeting root, home, or a bare glob at root-ish.
    # The target must be root/home/glob *itself* — a specific subpath such as
    # ~/Downloads/junk or /tmp/foo is allowed through.
    (r"\brm\b[^\n]*-[a-z]*r[a-z]*f?[a-z]*[^\n]*\s"
     r"(/|~|~/|\$HOME|\$HOME/|/\*|~/\*|\$HOME/\*|\./?|\*)\s*$",
     "I won't recursively delete root, home, or everything."),
    # Redirecting into raw disk devices.
    (r">\s*/dev/(disk|sd|rdisk|hd)\w*",
     "I won't write to a raw disk device."),
    # chmod -R on root.
    (r"\bchmod\b\s+-R\s+0*\s*/\s",
     "I won't recursively chmod the root directory."),
    (r"\bchmod\b[^\n]*-R[^\n]*\s/(\s|$)",
     "I won't recursively chmod the root directory."),
]

_DENYLIST: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pat, re.IGNORECASE), reason) for pat, reason in _DENY_PATTERNS
]


def _denied(command: str) -> str | None:
    """Return a spoken refusal reason if `command` hits the denylist, else None."""
    text = command.strip()
    for pattern, reason in _DENYLIST:
        if pattern.search(text):
            return reason
    return None


@register
class RunShell(Skill):
    spec = SkillSpec(
        name="run_shell",
        description="run a guarded free-form shell command in the home directory",
        examples=[
            "run ls -la in my home folder",
            "show me disk usage with df -h",
            "count the files in Downloads",
            "what's my current git branch",
        ],
        args={"command": "the shell command to run"},
        destructive=True,  # executor confirms before this runs
    )

    def confirm_prompt(self, args: dict) -> str | None:
        """Show the exact command so the user approves what actually runs."""
        command = str(args.get("command") or args.get("cmd") or "").strip()
        # The dialog flattens newlines (hud._escape), so use a visible
        # separator instead — the whole point is that the command is readable.
        return f"Run this command?   →   {command}" if command else None

    def run(self, args: dict, ctx: Context) -> SkillResult:
        if not ctx.config.get("shell_skill_enabled", True):
            return SkillResult.fail("The shell skill is disabled.")

        command = str(args.get("command") or args.get("cmd") or "").strip()
        if not command:
            return SkillResult.fail("I didn't get a command to run.")

        reason = _denied(command)
        if reason:
            ctx.note(f"run_shell refused: {command!r} ({reason})")
            return SkillResult.fail(reason, detail=f"Refused command: {command}")

        if ctx.dry_run:
            return SkillResult.say(
                "Dry run — I would run that command.",
                detail=f"Would run: {command}",
            )

        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                cwd=HOME,
            )
        except subprocess.TimeoutExpired:
            return SkillResult.fail(
                f"That command took longer than {TIMEOUT} seconds, so I stopped it.",
                detail=f"Timed out: {command}",
            )
        except FileNotFoundError:
            return SkillResult.fail(
                "I couldn't find the shell to run that.",
                detail=f"Command: {command}",
            )
        except OSError as exc:
            return SkillResult.fail(
                "That command couldn't be run.",
                detail=f"{type(exc).__name__}: {exc}",
            )

        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + "\n… (output truncated)"

        code = proc.returncode
        speech = f"Done (exit {code})."
        return SkillResult.say(speech, detail=output or f"(no output, exit {code})")
