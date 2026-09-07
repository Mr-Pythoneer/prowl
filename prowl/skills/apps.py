"""App-control skills — drive running apps via AppleScript / ``osascript``.

Everything here shells out through ``osascript`` (AppleScript) or ``open``, so
the module stays stdlib-only and importable on any machine. Skills are tolerant
of the loose, string-valued args a local LLM produces: a missing or misspelled
key returns a friendly :class:`SkillResult.fail` rather than raising.

Skills:

* ``quit_app``          — quit a named app.
* ``activate_app``      — bring a named app to the front (``open -a``).
* ``list_running_apps`` — the visible apps right now (via System Events).
* ``media_control``     — play/pause/skip in Music or Spotify, whichever runs.
"""
from __future__ import annotations

import time

import subprocess
from typing import Any

from ..core.context import Context
from .base import Skill, SkillResult, SkillSpec, register

# Media players we know how to drive, in preference order.
MEDIA_APPS = ("Music", "Spotify")

# Map loose action words to AppleScript player commands.
_MEDIA_ACTIONS = {
    "play": "play",
    "pause": "pause",
    "playpause": "playpause",
    "toggle": "playpause",
    "next": "next track",
    "skip": "next track",
    "forward": "next track",
    "previous": "previous track",
    "prev": "previous track",
    "back": "previous track",
}


# --- helpers ----------------------------------------------------------------
def _osascript(script: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run one AppleScript snippet. Never raises; a failure shows in returncode."""
    try:
        return subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess([], 127, "", "osascript not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], 124, "", "timed out")
    except subprocess.SubprocessError as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))


def _first_str(args: dict[str, Any], *keys: str) -> str:
    """First non-empty string among the given arg keys (LLM key-name drift)."""
    for key in keys:
        val = args.get(key)
        if val is not None:
            text = str(val).strip()
            if text:
                return text
    return ""


def _app_name(args: dict[str, Any]) -> str:
    """Pull an app name out of the loosely-shaped args dict."""
    return _first_str(args, "app", "app_name", "name", "application", "target")


def _resolve_app(name: str) -> str:
    """Spoken app name -> the installed app's real name.

    "Proton VPN" is ProtonVPN.app; speech gives us what was said, not what the
    bundle is called. Left unchanged when nothing matches, so the caller still
    reports the name the user actually used.
    """
    if not name:
        return name
    try:
        from ..core.macapps import resolve
    except Exception:  # noqa: BLE001 - a skill must never crash on an import
        return name
    return resolve(name) or name


def _sanitize(name: str) -> str:
    """Strip quotes so a name can't break out of the AppleScript string."""
    return name.replace('"', "").replace("\\", "").strip()


def _is_running(app: str) -> bool:
    """True if the named app is currently a running process."""
    safe = _sanitize(app)
    if not safe:
        return False
    r = _osascript(f'tell application "System Events" to (name of processes) contains "{safe}"')
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def _running_media_app() -> str:
    """Return the first known media app that is running, or ''."""
    for app in MEDIA_APPS:
        if _is_running(app):
            return app
    return ""


# --- skills -----------------------------------------------------------------
@register
class QuitApp(Skill):
    spec = SkillSpec(
        name="quit_app",
        description="quit a running application by name",
        examples=["quit Safari", "close Spotify", "exit Mail"],
        args={"app": "name of the app to quit, e.g. Safari"},
        destructive=True,
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        app = _resolve_app(_sanitize(_app_name(args)))
        if not app:
            return SkillResult.fail("Which app should I quit?")
        if ctx.dry_run:
            return SkillResult.say(f"Would quit {app}.")
        r = _osascript(f'quit app "{app}"')
        if r.returncode == 0:
            return SkillResult.say(f"Quit {app}.")
        return SkillResult.fail(f"Couldn't quit {app}.", detail=r.stderr.strip())


@register
class ActivateApp(Skill):
    spec = SkillSpec(
        name="activate_app",
        description="bring an application to the front (launching it if needed)",
        examples=["switch to Safari", "bring Notes to front", "open Music"],
        args={"app": "name of the app to focus, e.g. Notes"},
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        app = _resolve_app(_app_name(args))
        if not app:
            return SkillResult.fail("Which app should I switch to?")
        if ctx.dry_run:
            return SkillResult.say(f"Would bring {app} to the front.")
        # `open -a` both launches and focuses; it also resolves fuzzy app names.
        try:
            r = subprocess.run(
                ["open", "-a", app],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            return SkillResult.fail("The `open` command isn't available.")
        except subprocess.SubprocessError as exc:
            return SkillResult.fail(f"Couldn't switch to {app}.", detail=str(exc))
        if r.returncode == 0:
            return SkillResult.say(f"Switched to {app}.")
        return SkillResult.fail(f"Couldn't find an app named {app}.", detail=r.stderr.strip())


@register
class ListRunningApps(Skill):
    spec = SkillSpec(
        name="list_running_apps",
        description="list the applications currently running with a visible window",
        examples=["what apps are open", "list running apps", "what's running"],
        args={},
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        # `visible is true` filters out background/agent processes.
        script = (
            'tell application "System Events" to get the name of '
            "(every process whose background only is false)"
        )
        r = _osascript(script)
        if r.returncode != 0:
            return SkillResult.fail(
                "Couldn't read the running apps.", detail=r.stderr.strip()
            )
        # osascript returns a comma-separated list on one line.
        names = [n.strip() for n in r.stdout.split(",") if n.strip()]
        names = sorted(set(names), key=str.lower)
        if not names:
            return SkillResult.say("No visible apps are running.")
        detail = "\n".join(names)
        return SkillResult.say(f"{len(names)} apps are open.", detail=detail, apps=names)


@register
class MediaControl(Skill):
    spec = SkillSpec(
        name="media_control",
        description="control playback in Music or Spotify: play, pause, next, previous",
        examples=[
            "pause the music", "play", "next track", "skip this song",
            "previous song", "pause Spotify",
        ],
        args={
            "action": "play | pause | playpause | next | previous",
            "app": "optional: Music or Spotify (defaults to whichever is running)",
        },
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        action_word = _first_str(args, "action", "command", "cmd", "control").lower()
        if not action_word:
            return SkillResult.fail("What should I do — play, pause, next, or previous?")
        command = _MEDIA_ACTIONS.get(action_word)
        if command is None:
            return SkillResult.fail(
                f"I don't know how to '{action_word}' playback.",
                detail="Try play, pause, next, or previous.",
            )

        app = self._resolve_app(args)
        if not app:
            # "play some music" with nothing running should start playing, not
            # report that nothing is running. Only for play — pausing or
            # skipping when nothing is open is genuinely a no-op.
            if command in ("play", "playpause") and not ctx.dry_run:
                app = self._launch_default(args)
            if not app:
                return SkillResult.fail(
                    "Neither Music nor Spotify is running.",
                    detail="Open one of them and try again.")

        if ctx.dry_run:
            return SkillResult.say(f"Would {action_word} in {app}.")

        r = _osascript(f'tell application "{app}" to {command}')
        if r.returncode == 0:
            return SkillResult.say(self._confirmation(command, app))
        return SkillResult.fail(f"Couldn't control {app}.", detail=r.stderr.strip())

    @staticmethod
    def _launch_default(args: dict[str, Any]) -> str:
        """Open the requested (or default) media app and wait for it to answer."""
        wanted = _sanitize(_app_name(args))
        target = None
        for known in MEDIA_APPS:
            if wanted and wanted.lower() == known.lower():
                target = known
                break
        target = target or MEDIA_APPS[0]
        try:
            proc = subprocess.run(["open", "-a", target], capture_output=True,
                                  text=True, timeout=15)
        except (FileNotFoundError, subprocess.SubprocessError):
            return ""
        if proc.returncode != 0:
            return ""
        # AppleScript on a cold-started app fails until it is ready.
        for _ in range(20):
            if _is_running(target):
                return target
            time.sleep(0.25)
        return ""

    @staticmethod
    def _resolve_app(args: dict[str, Any]) -> str:
        """Honour an explicit, running app choice; else auto-detect."""
        requested = _sanitize(_app_name(args))
        if requested:
            # Match a known media app case-insensitively.
            for known in MEDIA_APPS:
                if requested.lower() == known.lower() and _is_running(known):
                    return known
            # A named-but-not-running (or unknown) app: fall through to detection.
        return _running_media_app()

    @staticmethod
    def _confirmation(command: str, app: str) -> str:
        phrases = {
            "play": f"Playing in {app}.",
            "pause": f"Paused {app}.",
            "playpause": f"Toggled playback in {app}.",
            "next track": f"Skipped ahead in {app}.",
            "previous track": f"Back a track in {app}.",
        }
        return phrases.get(command, f"Done in {app}.")
