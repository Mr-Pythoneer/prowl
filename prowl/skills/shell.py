"""Guarded read-only shell skill.

Runs a shell command the router produced from a spoken request. Because the
input is a *transcript* — something the microphone thought it heard — the
guardrail here is an **allowlist**, not a denylist:

* only a short list of read-only programs may run (``ls``, ``df``, ``git``,
  ``grep`` ...), matched on the program name after real argv parsing;
* shell metacharacters are refused outright, so there is no chaining,
  substitution, redirection or piping to smuggle something past the check;
* the command is executed as an argv list with ``shell=False``, which removes
  injection as a category rather than trying to pattern-match it away;
* the executor confirms first, because ``spec.destructive`` is ``True``;
* ``shell_skill_enabled`` in config turns the whole thing off.

**Why not a denylist.** There was one, and it did not work. Blocking ``rm -rf ~``
is easy; the bypasses are ``rm -rf ~ ;``, ``rm -rf ~ && echo done``,
``rm -rf "$HOME"``, ``mv ~/Documents /tmp/gone``, ``find ~ -mindepth 1 -delete``
and ``curl … | bash`` — every one of which the old list allowed. A denylist over
a full shell has to anticipate every phrasing of harm; an allowlist only has to
name the things that are safe.

Anything not on the list is refused with a pointer to escalation, which runs a
real agent that can reason about each step instead of firing one opaque string.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess

from ..core.context import Context
from .base import Skill, SkillResult, SkillSpec, register

HOME = os.path.expanduser("~")
MAX_OUTPUT = 1500  # chars of combined stdout+stderr kept in `detail`
TIMEOUT = 60       # seconds before the command is killed

# --- allowlist --------------------------------------------------------------
# Programs that only read. Anything that writes, deletes, installs, or reaches
# the network belongs in escalation, where an agent can reason about it.
_ALLOWED: frozenset[str] = frozenset({
    # files and directories
    "ls", "find", "stat", "file", "du", "df", "pwd", "readlink", "basename",
    "dirname", "tree",
    # reading contents. `awk` and `sed` are absent deliberately: awk has
    # system(), and `sed -i` writes in place.
    "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "diff", "sort",
    "uniq", "cut", "column", "jq",
    # version control (read-only subcommands are checked separately)
    "git",
    # system information
    "ps", "uptime", "date", "cal", "whoami", "id", "hostname", "uname",
    "sw_vers", "system_profiler", "pmset", "vm_stat", "sysctl",
    # environment. `env` is absent: `env rm -rf ~` runs rm while argv[0] is
    # env, which defeats the whole check. `top` and `man` are absent because
    # they are interactive and would just hang.
    "which", "type", "echo", "printenv", "whereis",
    # package managers, query subcommands only (see _SUBCOMMAND_READONLY).
    # Interpreters are absent: `python3 -c` and `node -e` are arbitrary code.
    "brew", "pip", "pip3", "npm",
})

# Flags that turn an otherwise read-only program into one that writes or
# executes. Checked per program, since the same flag means different things.
_BLOCKED_FLAGS: dict[str, frozenset[str]] = {
    "find": frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir",
                       "-fprint", "-fprintf", "-fls"}),
    "sort": frozenset({"-o", "--output"}),
    "sysctl": frozenset({"-w"}),
    "grep": frozenset(),
}

# Git subcommands that only read. `git` is on the list above because it is the
# single most useful thing to ask about, but `git push`/`reset`/`clean` are not
# read-only, so the subcommand is checked too.
_GIT_READONLY: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "branch", "remote", "describe",
    "blame", "shortlog", "rev-parse", "ls-files", "tag",
})

# Subcommands that read, for tools whose default action writes.
_SUBCOMMAND_READONLY: dict[str, frozenset[str]] = {
    "git": _GIT_READONLY,
    "brew": frozenset({"list", "info", "outdated", "config", "--version", "doctor"}),
    "npm": frozenset({"list", "ls", "view", "outdated", "config", "--version"}),
    "pip": frozenset({"list", "show", "freeze", "--version"}),
    "pip3": frozenset({"list", "show", "freeze", "--version"}),
    "pmset": frozenset({"-g"}),
}

# Characters that let one command become several, or reach outside its own
# argv: chaining, piping, redirection, substitution. Globs (* ? [ ]) and ~ are
# deliberately NOT here — with shell=False they are passed through literally,
# so they cannot expand into anything, and `find . -name "*.py"` is a perfectly
# ordinary thing to ask for.
_SHELL_META = re.compile(r"[;&|<>`$(){}\\\n]")


def _vet(command: str) -> tuple[list[str] | None, str]:
    """Return ``(argv, "")`` if *command* may run, else ``(None, reason)``."""
    if _SHELL_META.search(command):
        return None, ("That uses shell operators I won't run. Ask me to hand it "
                      "to the agent instead.")
    try:
        argv = shlex.split(command)
    except ValueError:
        return None, "I couldn't make sense of that command."
    if not argv:
        return None, "I didn't get a command to run."

    # Without a shell nothing expands ~, so do it ourselves — otherwise
    # "du -sh ~/Downloads" looks for a directory literally named "~".
    argv = [os.path.expanduser(a) if a.startswith("~") else a for a in argv]

    program = os.path.basename(argv[0])
    if program not in _ALLOWED:
        return None, (f"I only run a short list of read-only commands, and "
                      f"{program} isn't one. Ask me to hand it to the agent.")

    allowed_subs = _SUBCOMMAND_READONLY.get(program)
    if allowed_subs is not None:
        sub = next((a for a in argv[1:] if not a.startswith("-")), None)
        if sub is None:
            sub = next((a for a in argv[1:] if a.startswith("--")), "")
        if sub and sub not in allowed_subs:
            return None, (f"I'll only run read-only {program} commands, "
                          f"and {sub} isn't one.")

    blocked = _BLOCKED_FLAGS.get(program)
    if blocked:
        for arg in argv[1:]:
            if arg in blocked or arg.split("=", 1)[0] in blocked:
                return None, (f"I won't run {program} with {arg} — that writes "
                              f"or executes. Ask me to hand it to the agent.")
    return argv, ""


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

        argv, reason = _vet(command)
        if argv is None:
            ctx.note(f"run_shell refused: {command!r} ({reason})")
            return SkillResult.fail(reason, detail=f"Refused command: {command}")

        if ctx.dry_run:
            return SkillResult.say(
                "Dry run — I would run that command.",
                detail=f"Would run: {command}",
            )

        try:
            proc = subprocess.run(
                argv,                      # no shell: see the module docstring
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
