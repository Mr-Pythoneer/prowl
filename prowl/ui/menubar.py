"""The always-on macOS menu-bar app.

This is the ``prowl serve`` front-end: a paw in the menu bar with a handful of
actions (talk, type, clean up, toggle voice, doctor). It builds a
:class:`~prowl.core.context.Context` wired to notifications + TTS and drives the
shared :class:`~prowl.executor.Orchestrator` for every request.

``rumps`` is a top-level import here because the whole module exists to host a
rumps app; it is only imported by ``prowl serve``, so the core stays importable
without it. Every menu handler is wrapped so a failure surfaces as a
notification instead of tearing down the app, and the actual work (STT, model
calls, cleanup) always runs on a background thread — never the rumps main
thread.

Public API:
    run_menubar(cfg) -> None    # blocking; starts the rumps app
"""
from __future__ import annotations

import subprocess
import sys
import threading

import rumps

from ..core.context import Context
from ..core.logs import get_logger
from ..executor import Orchestrator
from ..voice import stt
from ..voice.tts import speak_async
from ..voice.wake import WakeListener
from . import hotkey, hud

_TITLE = "🐾"

# Cap the doctor subprocess so a hung check can't wedge the worker thread.
_DOCTOR_TIMEOUT = 60


def _run_bg(target, *args) -> None:
    """Run *target* on a daemon thread so the rumps main loop never blocks."""
    threading.Thread(target=target, args=args, daemon=True).start()


class ProwlApp(rumps.App):
    """The menu-bar controller: holds the Orchestrator, Context, and handlers."""

    def __init__(self, cfg):
        super().__init__(_TITLE, title=_TITLE, quit_button=None)
        self.cfg = cfg
        self.log = get_logger()

        # One Orchestrator for the whole session (loads skills/brain/router once).
        self.orch = Orchestrator(cfg)

        # A single Context, reused across every request. Its speak/confirm close
        # over ``self`` so a voice-toggle takes effect immediately.
        self.ctx = Context(
            config=cfg,
            log=self.log,
            speak=self._speak,
            confirm=self._confirm,
            dry_run=False,
        )

        # The desktop buddy. Optional: if AppKit drawing fails for any reason
        # the assistant must still work, so a failure here is logged and
        # forgotten rather than fatal.
        self.buddy = None
        try:
            from .buddy import Buddy

            self.buddy = Buddy(on_click=lambda: _run_bg(self._talk))
            if cfg.get("buddy_enabled", True):
                self.buddy.show()
                if cfg.get("buddy_greet", True):
                    # Introduce itself once, after the app loop is up — it is
                    # the only hint that clicking or F5 starts a voice turn.
                    threading.Timer(1.2, lambda: self.buddy.say(
                        "Hey! Press F5 or click me to talk.")).start()
                    threading.Timer(6.0, lambda: self._buddy("idle")).start()
        except Exception:  # noqa: BLE001 - the buddy is a nicety
            self.log.exception("buddy unavailable; menu still works")

        # Wake-word listening ("Hey Prowl"), off unless asked for.
        self.wake = WakeListener(
            cfg,
            on_wake=self._on_wake,
            on_command=self._on_wake_command,
            on_error=self._on_wake_error,
        )

        if cfg.get("always_listening", False):
            self.wake.start()

        self.menu = [
            rumps.MenuItem("Talk (voice)", callback=self.on_talk),
            rumps.MenuItem("Type a command…", callback=self.on_type),
            rumps.MenuItem("Clean up my Mac", callback=self.on_cleanup),
            rumps.MenuItem("Toggle voice (on/off)", callback=self.on_toggle_voice),
            rumps.MenuItem("Toggle offline mode", callback=self.on_toggle_offline),
            rumps.MenuItem("Show/hide buddy", callback=self.on_toggle_buddy),
            rumps.MenuItem('Listen for "Hey Prowl"', callback=self.on_toggle_wake),
            rumps.MenuItem("Run doctor", callback=self.on_doctor),
            None,  # separator
            rumps.MenuItem("Quit", callback=self.on_quit),
        ]

        # Global hotkey → same voice flow as the menu item. A missing pynput (or
        # any listener failure) must not stop the app from launching.
        self._hotkey = None
        try:
            self._hotkey = hotkey.start_hotkey(cfg.hotkey, self._on_hotkey)
        except Exception as exc:  # noqa: BLE001 - hotkey is optional
            self.log.exception("hotkey unavailable; menu still works")
            # Silent failure here is why Prowl "feels dead" — say so out loud.
            hud.notify(
                _TITLE,
                f"Hotkey {cfg.hotkey} could not be registered ({exc}). "
                "Use the menu, or fix `hotkey` in ~/.prowl/config.json.",
            )

    # -- menu handlers: buddy + wake -------------------------------------------
    def on_toggle_buddy(self, _sender) -> None:
        if self.buddy is None:
            hud.notify(_TITLE, "The buddy couldn't start — see the log.")
            return
        try:
            if self.buddy.is_visible():
                self.buddy.hide()
                self.cfg.set("buddy_enabled", False)
            else:
                self.buddy.show()
                self.cfg.set("buddy_enabled", True)
            self.cfg.save()
        except Exception:  # noqa: BLE001
            self.log.exception("toggling buddy failed")

    def on_toggle_wake(self, _sender) -> None:
        if self.wake.running:
            self.wake.stop()
            self.cfg.set("always_listening", False)
            self._buddy("idle")
            hud.notify(_TITLE, "Stopped listening for the wake word.")
        else:
            self.wake.start()
            self.cfg.set("always_listening", True)
            self._buddy("listening")
            hud.notify(_TITLE, f'Listening for "{self.wake.wake_word}".')
        self.cfg.save()

    def _on_wake(self) -> None:
        """Wake word heard — perk up and let the user know we're listening."""
        self._buddy("listening")
        if self.cfg.voice_enabled:
            speak_async("Yes?", self.cfg)

    def _on_wake_command(self, text: str) -> None:
        self.log.info("wake command: %r", text)
        self._handle(text)

    def _on_wake_error(self, problem: str) -> None:
        self.cfg.set("always_listening", False)
        self._buddy("idle")
        hud.notify(_TITLE, f"Stopped listening: {problem}")

    # -- buddy ----------------------------------------------------------------
    def _buddy(self, state: str) -> None:
        """Set the buddy's animation state, if it exists."""
        if self.buddy is not None:
            try:
                self.buddy.set_state(state)
            except Exception:  # noqa: BLE001 - never let the mascot break a turn
                self.log.exception("buddy state failed")

    # -- Context callbacks ---------------------------------------------------
    def _speak(self, text: str) -> None:
        """Show *text* as a notification and, if voice is on, say it aloud."""
        text = (text or "").strip()
        if not text:
            return
        # The buddy shows the words and animates; the notification is the
        # fallback for when it is hidden.
        if self.buddy is not None:
            try:
                self.buddy.say(text)
            except Exception:  # noqa: BLE001
                self.log.exception("buddy say failed")
        else:
            hud.notify(_TITLE, text)
        if self.cfg.voice_enabled:
            speak_async(text, self.cfg)

    def _confirm(self, question: str) -> bool:
        """Ask a yes/no question via an osascript dialog; True = go ahead."""
        script = (
            f'display dialog "{hud._escape(question)}" '
            f'with title "{hud._escape(hud.TITLE)}" '
            'buttons {"No", "Yes"} default button "Yes"'
        )
        result = hud._run(script)
        if result.returncode != 0:
            # User cancelled, timed out, or osascript missing → treat as "no".
            return False
        return "button returned:Yes" in result.stdout

    # -- request plumbing ----------------------------------------------------
    def _handle(self, text: str) -> None:
        """Route *text* through the Orchestrator (background thread only)."""
        text = (text or "").strip()
        if not text:
            return
        try:
            self._buddy("thinking")
            self.orch.handle(text, self.ctx)
        except Exception:  # noqa: BLE001 - a bad turn must not kill the worker
            self.log.exception("handling utterance failed")
            hud.notify(_TITLE, "Sorry — that request failed. See the log.")
        finally:
            # _speak() puts it in "talking"; settle back once the turn is done.
            threading.Timer(2.5, lambda: self._buddy(
                "listening" if self.wake.running else "idle")).start()

    def _talk(self) -> None:
        """Capture one utterance and act on it (background thread only)."""
        self._buddy("listening")
        try:
            text, problem = stt.listen_once_ex(self.cfg)
        except Exception:  # noqa: BLE001 - STT failure is non-fatal
            self.log.exception("listen_once failed")
            self._buddy("idle")
            hud.notify(_TITLE, "Couldn't start listening.")
            return
        if not text:
            # Report the actual reason (denied mic, missing helper) rather
            # than a blanket "didn't catch anything" the user can't act on.
            self._buddy("idle")
            hud.notify(_TITLE, problem or "Didn't catch anything — only silence.")
            return
        hud.notify(_TITLE, f"Heard: {text}")
        self._handle(text)

    def _on_hotkey(self) -> None:
        """Hotkey callback: kick off a voice turn on a worker thread."""
        _run_bg(self._talk)

    # -- menu handlers (all wrapped; heavy work goes to a worker thread) ------
    def on_talk(self, _sender) -> None:
        try:
            _run_bg(self._talk)
        except Exception:  # noqa: BLE001
            self.log.exception("on_talk failed")
            hud.notify(_TITLE, "Couldn't start listening.")

    def on_type(self, _sender) -> None:
        try:
            text = hud.ask_text("What do you need?")
            if text:
                _run_bg(self._handle, text)
        except Exception:  # noqa: BLE001
            self.log.exception("on_type failed")
            hud.notify(_TITLE, "Couldn't read your command.")

    def on_cleanup(self, _sender) -> None:
        # Go through the Orchestrator so the destructive-skill confirm/dry-run
        # safety flow runs exactly as it would for a spoken request.
        try:
            _run_bg(self._handle, "clean up my mac")
        except Exception:  # noqa: BLE001
            self.log.exception("on_cleanup failed")
            hud.notify(_TITLE, "Couldn't start cleanup.")

    def on_toggle_voice(self, _sender) -> None:
        try:
            new_state = not bool(self.cfg.voice_enabled)
            self.cfg.set("voice_enabled", new_state)
            self.cfg.save()
            hud.notify(_TITLE, "Voice on 🔊" if new_state else "Voice off 🔇")
        except Exception:  # noqa: BLE001
            self.log.exception("on_toggle_voice failed")
            hud.notify(_TITLE, "Couldn't change the voice setting.")

    def on_toggle_offline(self, _sender) -> None:
        try:
            new_state = not bool(self.cfg.get("offline"))
            self.cfg.set("offline", new_state)
            self.cfg.save()
            hud.notify(
                _TITLE,
                "Offline mode ON — local model only 💸" if new_state
                else "Offline mode OFF — online agent available",
            )
        except Exception:  # noqa: BLE001
            self.log.exception("on_toggle_offline failed")
            hud.notify(_TITLE, "Couldn't change offline mode.")

    def on_doctor(self, _sender) -> None:
        try:
            _run_bg(self._doctor)
        except Exception:  # noqa: BLE001
            self.log.exception("on_doctor failed")
            hud.notify(_TITLE, "Couldn't run doctor.")

    def _doctor(self) -> None:
        """Run ``python3 -m prowl doctor`` and show the summary (worker thread)."""
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "prowl", "doctor"],
                capture_output=True,
                text=True,
                timeout=_DOCTOR_TIMEOUT,
            )
        except FileNotFoundError:
            hud.notify(_TITLE, "Couldn't find Python to run doctor.")
            return
        except subprocess.TimeoutExpired:
            hud.notify(_TITLE, "Doctor timed out.")
            return
        except OSError:
            hud.notify(_TITLE, "Couldn't run doctor.")
            return
        report = (proc.stdout or proc.stderr or "").strip() or "No output."
        # Show the full report in a dialog; notifications truncate long text.
        script = (
            f'display dialog "{hud._escape(report)}" '
            f'with title "{hud._escape(hud.TITLE)} doctor" '
            'buttons {"OK"} default button "OK"'
        )
        hud._run(script)

    def on_quit(self, _sender) -> None:
        try:
            hotkey.stop_hotkey(self._hotkey)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            self.log.exception("stopping hotkey failed")
        rumps.quit_application()


def run_menubar(cfg) -> None:
    """Start the always-on menu-bar app. Blocks until the user quits."""
    ProwlApp(cfg).run()
