"""System control skills — the everyday macOS knobs.

Each skill here is a thin, safe wrapper over a native command line tool
(``open``, ``screencapture``, ``pbcopy``/``pbpaste``, ``pmset``) or a short
``osascript`` snippet. All of it is stdlib-only: no pyobjc, no third-party
libraries, so the core stays importable anywhere.

Conventions shared by every skill below:

* Shell-out through :func:`_run`, which always sets a timeout, captures output,
  and turns a missing binary into a clean failure instead of a crash.
* Args arrive from a local LLM as loose strings — treat them tolerantly and
  never raise; return :meth:`SkillResult.fail` on bad input.
* The spoken ``speech`` line stays short; anything longer goes in ``detail``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from ..core.context import Context
from .base import Skill, SkillResult, SkillSpec, register

HOME = Path(os.path.expanduser("~"))


# --- helpers ----------------------------------------------------------------
def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """Run ``cmd`` and return ``(returncode, stdout, stderr)``.

    A missing binary or a timeout is reported as returncode 127/124 with the
    reason in stderr, so callers can handle failure uniformly without a
    try/except of their own.
    """
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out"
    except subprocess.SubprocessError as exc:
        return 1, "", str(exc)


def _osascript(script: str, timeout: int = 15) -> tuple[int, str, str]:
    """Run a one-line AppleScript via ``osascript -e``."""
    return _run(["osascript", "-e", script], timeout=timeout)


def _int_in_range(value: Any, lo: int, hi: int) -> int | None:
    """Parse ``value`` (often a string) to an int clamped to [lo, hi], or None."""
    if value is None:
        return None
    try:
        n = int(round(float(str(value).strip().rstrip("%"))))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


# --- open_app ---------------------------------------------------------------
@register
class OpenApp(Skill):
    spec = SkillSpec(
        name="open_app",
        description="open or launch a macOS application by name",
        examples=[
            "open Safari", "launch Notes", "start Music",
            "open the Terminal", "fire up Xcode",
        ],
        args={"app": "the application name, e.g. 'Safari' or 'Visual Studio Code'"},
    )

    def run(self, args: dict, ctx: Context) -> SkillResult:
        app = str(args.get("app") or args.get("name") or "").strip()
        if not app:
            return SkillResult.fail("Which app should I open?")
        if ctx.dry_run:
            return SkillResult.say(f"Would open {app}.")
        code, _, err = _run(["open", "-a", app])
        if code == 0:
            return SkillResult.say(f"Opening {app}.")
        # No app by that name. "open google" / "open reddit" mean a website far
        # more often than a missing app, so degrade to the web rather than
        # dead-ending. Imported lazily to keep this module import-light.
        from .web import SITES, _open, _normalize_url, _looks_like_url

        key = app.lower()
        if key in SITES:
            return _open(SITES[key], ctx)
        if _looks_like_url(app):
            return _open(_normalize_url(app), ctx)
        return SkillResult.fail(
            f"I couldn't find an app called {app}.",
            detail=err or f"open -a {app}",
        )


# --- set_volume -------------------------------------------------------------
@register
class SetVolume(Skill):
    spec = SkillSpec(
        name="set_volume",
        description="set the output volume (0-100) or mute/unmute the speakers",
        examples=[
            "set volume to 40", "turn it up to 80", "volume 0",
            "mute", "unmute", "silence the sound",
        ],
        args={
            "level": "target volume 0-100, or 'mute' / 'unmute'",
            "direction": "optional: 'up' or 'down' to nudge by 10",
        },
    )

    def run(self, args: dict, ctx: Context) -> SkillResult:
        raw = args.get("level")
        if raw is None:
            raw = args.get("volume")
        word = str(raw).strip().lower() if raw is not None else ""

        # Mute / unmute path.
        if word in ("mute", "muted", "silence", "silent", "off"):
            return self._mute(True, ctx)
        if word in ("unmute", "unmuted", "on"):
            return self._mute(False, ctx)

        level = _int_in_range(raw, 0, 100)

        # Relative nudge: "turn it up" / "quieter" — read the current level and
        # step from there, which is how people ask for volume out loud.
        if level is None:
            direction = str(args.get("direction") or "").strip().lower()
            if direction in ("up", "down", "louder", "quieter"):
                code, out, _ = _osascript("output volume of (get volume settings)")
                current = _int_in_range(out, 0, 100) if code == 0 else None
                if current is None:
                    return SkillResult.fail("I couldn't read the current volume.")
                step = 10 if direction in ("up", "louder") else -10
                level = max(0, min(100, current + step))

        if level is None:
            return SkillResult.fail("Tell me a volume from 0 to 100, or say mute.")
        if ctx.dry_run:
            return SkillResult.say(f"Would set the volume to {level}%.")
        code, _, err = _osascript(f"set volume output volume {level}")
        if code != 0:
            return SkillResult.fail("Couldn't change the volume.", detail=err)
        return SkillResult.say(f"Volume set to {level}%.")

    def _mute(self, on: bool, ctx: Context) -> SkillResult:
        verb = "mute" if on else "unmute"
        if ctx.dry_run:
            return SkillResult.say(f"Would {verb} the sound.")
        flag = "with" if on else "without"
        code, _, err = _osascript(f"set volume {flag} output muted")
        if code != 0:
            return SkillResult.fail(f"Couldn't {verb} the sound.", detail=err)
        return SkillResult.say("Muted." if on else "Unmuted.")


# --- toggle_dark_mode -------------------------------------------------------
@register
class ToggleDarkMode(Skill):
    spec = SkillSpec(
        name="toggle_dark_mode",
        description="toggle macOS Dark Mode on or off",
        examples=[
            "toggle dark mode", "switch to dark mode", "turn on light mode",
            "make it dark", "go light",
        ],
        args={
            "mode": "optional: 'dark', 'light', or omit to toggle",
        },
    )

    _SCRIPT = 'tell application "System Events" to tell appearance preferences'

    def run(self, args: dict, ctx: Context) -> SkillResult:
        want = str(args.get("mode") or args.get("state") or "").strip().lower()
        if want in ("dark", "on"):
            target = "true"
        elif want in ("light", "off"):
            target = "false"
        else:
            target = "not dark mode"  # toggle
        if ctx.dry_run:
            label = {"true": "dark", "false": "light"}.get(target, "the other")
            return SkillResult.say(f"Would switch appearance to {label} mode.")
        code, _, err = _osascript(f"{self._SCRIPT} to set dark mode to {target}")
        if code != 0:
            return SkillResult.fail("Couldn't change the appearance.", detail=err)
        # Read back what we ended up with for an accurate spoken line.
        code2, out, _ = _osascript(f"{self._SCRIPT} to get dark mode")
        now = "dark" if (code2 == 0 and out.strip().lower() == "true") else "light"
        return SkillResult.say(f"Switched to {now} mode.")


# --- screenshot -------------------------------------------------------------
@register
class Screenshot(Skill):
    spec = SkillSpec(
        name="screenshot",
        description="capture the screen to a file on the Desktop",
        examples=[
            "take a screenshot", "grab a screenshot of a region",
            "capture the whole screen", "screenshot this window",
        ],
        args={
            "mode": "'region' (default, pick an area), 'full' (whole screen), "
                    "or 'window' (click a window)",
        },
    )

    def run(self, args: dict, ctx: Context) -> SkillResult:
        mode = str(args.get("mode") or args.get("type") or "region").strip().lower()
        import time

        dest = HOME / "Desktop" / f"Screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"

        cmd = ["screencapture"]
        if mode in ("full", "screen", "all", "fullscreen"):
            spoken = "Captured the full screen."
        elif mode in ("window", "win"):
            cmd += ["-iW"]  # interactive, window-selection mode
            spoken = "Pick a window to capture."
        else:  # default: interactive region select
            cmd += ["-i"]
            spoken = "Select an area to capture."
        cmd.append(str(dest))

        if ctx.dry_run:
            return SkillResult.say(f"Would save a screenshot to {dest.name}.")

        # Interactive captures wait on the user, so allow a generous timeout.
        code, _, err = _run(cmd, timeout=120)
        if code != 0:
            return SkillResult.fail("Screenshot failed.", detail=err or " ".join(cmd))
        if not dest.exists():
            # User pressed Escape during an interactive capture.
            return SkillResult.say("Screenshot cancelled.")
        return SkillResult.say(f"Saved to Desktop as {dest.name}.",
                               detail=str(dest), path=str(dest))


# --- clipboard --------------------------------------------------------------
@register
class Clipboard(Skill):
    spec = SkillSpec(
        name="clipboard",
        description="read the clipboard or copy text onto it",
        examples=[
            "what's on my clipboard", "read the clipboard",
            "copy hello world to the clipboard", "put this on the clipboard",
        ],
        args={
            "action": "'get' to read, 'set' to write (default: get)",
            "text": "text to copy when action is 'set'",
        },
    )

    def run(self, args: dict, ctx: Context) -> SkillResult:
        action = str(args.get("action") or args.get("mode") or "get").strip().lower()

        if action in ("set", "copy", "write", "put"):
            text = args.get("text")
            if text is None:
                text = args.get("value") or args.get("content")
            if text is None:
                return SkillResult.fail("What text should I copy?")
            text = str(text)
            if ctx.dry_run:
                return SkillResult.say("Would copy that to the clipboard.")
            try:
                p = subprocess.run(
                    ["pbcopy"], input=text, text=True,
                    capture_output=True, timeout=15,
                )
            except FileNotFoundError:
                return SkillResult.fail("pbcopy isn't available.")
            except subprocess.SubprocessError as exc:
                return SkillResult.fail("Couldn't copy to the clipboard.", detail=str(exc))
            if p.returncode != 0:
                return SkillResult.fail("Couldn't copy to the clipboard.", detail=p.stderr.strip())
            preview = text if len(text) <= 40 else text[:37] + "..."
            return SkillResult.say(f"Copied: {preview}", detail=text)

        # Default: read.
        code, out, err = _run(["pbpaste"])
        if code != 0:
            return SkillResult.fail("Couldn't read the clipboard.", detail=err)
        if not out:
            return SkillResult.say("The clipboard is empty.")
        preview = out if len(out) <= 60 else out[:57] + "..."
        return SkillResult.say(f"Clipboard: {preview}", detail=out)


# --- system_status ----------------------------------------------------------
@register
class SystemStatus(Skill):
    spec = SkillSpec(
        name="system_status",
        description="report battery level, free disk space and the macOS version",
        examples=[
            "system status", "how's my battery", "how much disk space is left",
            "what version of macos am i on", "give me a status report",
        ],
    )

    def run(self, args: dict, ctx: Context) -> SkillResult:
        battery = self._battery()
        disk = self._free_disk()
        version = self._macos_version()

        parts = [p for p in (battery, disk, version) if p]
        if not parts:
            return SkillResult.fail("Couldn't read the system status.")
        speech = "; ".join(parts) + "."
        detail = "\n".join(filter(None, (battery, disk, version)))
        return SkillResult.say(speech, detail=detail)

    def _battery(self) -> str:
        code, out, _ = _run(["pmset", "-g", "batt"])
        if code != 0 or not out:
            return ""
        # Look for the first "NN%" token in pmset output.
        for token in out.replace(";", " ").split():
            if token.endswith("%") and token[:-1].isdigit():
                pct = token[:-1]
                source = "on AC power" if "AC Power" in out else "on battery"
                return f"Battery {pct}% ({source})"
        return ""

    def _free_disk(self) -> str:
        code, out, _ = _run(["df", "-h", "/"])
        if code != 0 or not out:
            return ""
        lines = out.splitlines()
        if len(lines) < 2:
            return ""
        # df columns: Filesystem Size Used Avail Capacity ... — Avail is index 3.
        cols = lines[1].split()
        if len(cols) >= 4:
            return f"{cols[3]} free of {cols[1]} disk"
        return ""

    def _macos_version(self) -> str:
        code, product, _ = _run(["sw_vers", "-productVersion"])
        if code != 0 or not product:
            return ""
        return f"macOS {product}"


# --- lock_or_sleep ----------------------------------------------------------
@register
class LockOrSleep(Skill):
    spec = SkillSpec(
        name="lock_or_sleep",
        description="lock the screen or put the display to sleep",
        examples=[
            "lock my screen", "lock the mac", "sleep the display",
            "turn off the screen", "put the screen to sleep",
        ],
        args={
            "mode": "'lock' to lock the screen (default), or 'sleep' to sleep the display",
        },
    )

    def run(self, args: dict, ctx: Context) -> SkillResult:
        mode = str(args.get("mode") or args.get("action") or "lock").strip().lower()

        if mode in ("sleep", "display", "screen off", "off"):
            if ctx.dry_run:
                return SkillResult.say("Would sleep the display.")
            code, _, err = _run(["pmset", "displaysleepnow"])
            if code != 0:
                return SkillResult.fail("Couldn't sleep the display.", detail=err)
            return SkillResult.say("Sleeping the display.")

        # Default: lock the screen. Keystroke Ctrl-Cmd-Q locks on modern macOS.
        if ctx.dry_run:
            return SkillResult.say("Would lock the screen.")
        script = (
            'tell application "System Events" to '
            'keystroke "q" using {control down, command down}'
        )
        code, _, err = _osascript(script)
        if code != 0:
            return SkillResult.fail("Couldn't lock the screen.", detail=err)
        return SkillResult.say("Locking the screen.")
