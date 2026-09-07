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

import dataclasses
import subprocess
import sys
import threading

import rumps

from ..core.context import Context
from ..core.logs import get_logger
from ..executor import Orchestrator
from ..voice import stt
from ..voice.tts import speak_async, stop as tts_stop
from ..voice.control import match_control
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
        # Last thing spoken, for "say that again".
        self._last_said = ""

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
            if cfg.get("buddy_mini", False):
                self.buddy.set_mini(True)
            if cfg.get("buddy_enabled", True):
                self.buddy.show()
                if cfg.get("buddy_greet", True):
                    # Introduce itself once, after the app loop is up — it is
                    # the only hint that clicking or F5 starts a voice turn.
                    name = cfg.get("assistant_name", "Bob")
                    # Through _speak, so the bubble clears when he stops
                    # talking rather than after a guessed delay.
                    threading.Timer(1.2, lambda: self._speak(
                        f"Hi, I'm {name}. Press F5 or click me to talk.")).start()
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
            rumps.MenuItem("Mini buddy (⌘⇧Z)", callback=self.on_toggle_mini),
            rumps.MenuItem('Listen for "Hey Prowl"', callback=self.on_toggle_wake),
            rumps.MenuItem("Run doctor", callback=self.on_doctor),
            None,  # separator
            rumps.MenuItem("Quit", callback=self.on_quit),
        ]

        # Global hotkey → same voice flow as the menu item. A missing pynput (or
        # any listener failure) must not stop the app from launching.
        # One listener for every hotkey: a second event tap in the same process
        # is killed by macOS and takes the whole app with it.
        self._hotkey = None
        bindings = {cfg.hotkey: self._on_hotkey}
        type_key = cfg.get("type_hotkey", "<f4>")
        if type_key:
            bindings[type_key] = self._on_type_hotkey
        mini_key = cfg.get("buddy_mini_hotkey", "<cmd>+<shift>+z")
        if mini_key and self.buddy is not None:
            bindings[mini_key] = self._on_mini_hotkey
        try:
            self._hotkey = hotkey.start_hotkeys(bindings)
        except Exception as exc:  # noqa: BLE001 - hotkey is optional
            self.log.exception("hotkey unavailable; menu still works")
            # Silent failure here is why Prowl "feels dead" — say so out loud.
            self._tell(f"Hotkey {cfg.hotkey} could not be registered ({exc}). "
                "Use the menu, or fix `hotkey` in ~/.prowl/config.json.",
            )

    # -- menu handlers: buddy + wake -------------------------------------------
    def on_toggle_buddy(self, _sender) -> None:
        if self.buddy is None:
            self._tell("The buddy couldn't start — see the log.")
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

    def on_toggle_mini(self, _sender) -> None:
        self._on_mini_hotkey()

    def _on_mini_hotkey(self) -> None:
        """Toggle compact mode and remember the choice."""
        if self.buddy is None:
            return
        try:
            self.buddy.toggle_mini()
            self.cfg.set("buddy_mini", not self.cfg.get("buddy_mini", False))
            self.cfg.save()
        except Exception:  # noqa: BLE001
            self.log.exception("toggling mini buddy failed")

    def on_toggle_wake(self, _sender) -> None:
        if self.wake.running:
            self.wake.stop()
            self.cfg.set("always_listening", False)
            self._buddy("idle")
            self._tell("Stopped listening for the wake word.")
        else:
            self.wake.start()
            self.cfg.set("always_listening", True)
            self._buddy("listening")
            self._tell(f'Listening for "{self.wake.wake_word}".')
        self.cfg.save()

    def _on_wake(self) -> None:
        """Wake word heard — perk up and let the user know we're listening."""
        self._buddy("listening")
        if self.cfg.voice_enabled:
            speak_async("Yes?", self.cfg)

    def _on_wake_command(self, text: str) -> None:
        self.log.info("wake command: %r", text)
        if self._handle_control(text):
            return
        self._handle(text)

    def _handle_control(self, text: str) -> bool:
        """Act on a control phrase ("stop", "hide", ...). True if handled.

        These bypass the router entirely: "stop" that waits on a model call has
        already failed at its job.
        """
        action = match_control(text)
        if action is None:
            return False
        self.log.info("control: %s", action)

        if action == "stop":
            tts_stop()
            self._after_speaking()
        elif action == "sleep":
            self.wake.stop()
            self.cfg.set("always_listening", False)
            self._buddy("sleeping")
            self._tell("Sleeping. Press F5 or click me to wake me.")
        elif action == "wake":
            self.cfg.set("always_listening", True)
            self.wake.start()
            self._speak("I'm listening.")
        elif action == "hide":
            if self.buddy is not None:
                self.buddy.hide_threadsafe()
            self.cfg.set("buddy_enabled", False)
        elif action == "show":
            if self.buddy is not None:
                self.buddy.show_threadsafe()
            self.cfg.set("buddy_enabled", True)
            self._buddy("idle")
        elif action == "repeat":
            if self._last_said:
                self._speak(self._last_said)
            else:
                self._speak("I haven't said anything yet.")
        elif action == "help":
            self._speak(
                "Try: open Safari, turn it up, take a screenshot, what's on my "
                "clipboard, clean up my Mac, or ask me anything. Say stop to "
                "cut me off.")
        self.cfg.save()
        return True

    def _on_wake_error(self, problem: str) -> None:
        self.cfg.set("always_listening", False)
        self._buddy("idle")
        self._tell(f"Stopped listening: {problem}")

    # -- buddy ----------------------------------------------------------------
    def _tell(self, text: str) -> None:
        """Show a short message. Prefers the buddy's bubble.

        macOS notification banners for every step (heard-you, voice-on, ...)
        were more annoying than useful, so they are now only the fallback for
        when the buddy is hidden.
        """
        text = (text or "").strip()
        if not text:
            return
        if self.buddy is not None:
            try:
                if self.buddy.is_visible():
                    self.buddy.say(text)
                    return
            except Exception:  # noqa: BLE001
                self.log.exception("buddy say failed")
        hud.notify(_TITLE, text)

    def _buddy(self, state: str) -> None:
        """Set the buddy's animation state, if it exists."""
        if self.buddy is not None:
            try:
                self.buddy.set_state(state)
            except Exception:  # noqa: BLE001 - never let the mascot break a turn
                self.log.exception("buddy state failed")

    # -- Context callbacks ---------------------------------------------------
    def _show_only(self, text: str) -> None:
        """Show *text* without speaking it — the typed path's reply channel."""
        text = (text or "").strip()
        if not text:
            return
        self._last_said = text
        self._tell(text)
        if self.buddy is not None:
            # Hold the bubble long enough to read, then settle.
            self.buddy.say(text)

    def _speak(self, text: str) -> None:
        """Show *text* (buddy bubble, else a banner) and say it aloud.

        The bubble is cleared when the speech itself finishes rather than on a
        guessed timer, so the words are on screen exactly as long as Bob is
        saying them. The wake listener is muted meanwhile — the microphone
        hears him perfectly well, and without this he answers himself.
        """
        text = (text or "").strip()
        if not text:
            return
        self._last_said = text
        self._tell(text)
        if not self.cfg.voice_enabled:
            return

        self.wake.mute()
        speak_async(text, self.cfg, on_done=self._after_speaking)

    def _after_speaking(self) -> None:
        """Called when TTS finishes (or is stopped): clear bubble, hear again."""
        self.wake.unmute()
        if self.buddy is not None:
            self.buddy.say("")          # empty text clears the bubble
            # Back to idle even while the wake listener runs — see note below.
            self.buddy.set_state("idle")

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
    def _handle(self, text: str, silent: bool = False) -> None:
        """Route *text* through the Orchestrator (background thread only).

        With ``silent`` the reply is shown but not spoken — the typed path,
        for when you don't want the room to hear the answer.
        """
        text = (text or "").strip()
        if not text:
            return
        try:
            self._buddy("thinking")
            if silent:
                # Swap in a Context whose speak() only shows text. Building a
                # new one (rather than toggling a flag) keeps a concurrent
                # spoken turn unaffected.
                ctx = dataclasses.replace(self.ctx, speak=self._show_only)
            else:
                ctx = self.ctx
            self.orch.handle(text, ctx)
        except Exception:  # noqa: BLE001 - a bad turn must not kill the worker
            self.log.exception("handling utterance failed")
            self._tell("Sorry — that request failed. See the log.")
        finally:
            # _speak() puts it in "talking"; settle back once the turn is done.
            threading.Timer(2.5, lambda: self._buddy(
                "idle")).start()

    def _talk(self) -> None:
        """Capture one utterance and act on it (background thread only)."""
        self._buddy("listening")
        # The wake listener holds the microphone continuously; two recognisers
        # on one input device is what caused the intermittent (and misleading)
        # "I couldn't reach the microphone".
        self.wake.suspend()
        try:
            text, problem = stt.listen_once_ex(self.cfg)
        except Exception:  # noqa: BLE001 - STT failure is non-fatal
            self.log.exception("listen_once failed")
            self._buddy("idle")
            self._tell("Couldn't start listening.")
            return
        finally:
            self.wake.resume()
        if not text:
            # Report the actual reason (denied mic, missing helper) rather
            # than a blanket "didn't catch anything" the user can't act on.
            self._buddy("idle")
            self._tell(problem or "Didn't catch anything — only silence.")
            return
        if self._handle_control(text):
            return
        self._handle(text)

    def _on_hotkey(self) -> None:
        """Hotkey callback: kick off a voice turn on a worker thread."""
        _run_bg(self._talk)

    def _on_type_hotkey(self) -> None:
        """Type-hotkey callback: ask for text, answer in text only."""
        _run_bg(self._type_turn)

    def _type_turn(self) -> None:
        """One typed request, answered silently in the bubble.

        Same brain and skills as a spoken turn — only the reply channel
        differs. Nothing is said aloud, so this is usable in a meeting or with
        headphones off.
        """
        try:
            text = hud.ask_text("What do you need?")
        except Exception:  # noqa: BLE001 - the dialog is not critical
            self.log.exception("type prompt failed")
            self._tell("Couldn't open the text box.")
            return
        if not text:
            return
        if self._handle_control(text):
            return
        self._handle(text, silent=True)

    # -- menu handlers (all wrapped; heavy work goes to a worker thread) ------
    def on_talk(self, _sender) -> None:
        try:
            _run_bg(self._talk)
        except Exception:  # noqa: BLE001
            self.log.exception("on_talk failed")
            self._tell("Couldn't start listening.")

    def on_type(self, _sender) -> None:
        _run_bg(self._type_turn)

    def on_cleanup(self, _sender) -> None:
        # Go through the Orchestrator so the destructive-skill confirm/dry-run
        # safety flow runs exactly as it would for a spoken request.
        try:
            _run_bg(self._handle, "clean up my mac")
        except Exception:  # noqa: BLE001
            self.log.exception("on_cleanup failed")
            self._tell("Couldn't start cleanup.")

    def on_toggle_voice(self, _sender) -> None:
        try:
            new_state = not bool(self.cfg.voice_enabled)
            self.cfg.set("voice_enabled", new_state)
            self.cfg.save()
            self._tell("Voice on 🔊" if new_state else "Voice off 🔇")
        except Exception:  # noqa: BLE001
            self.log.exception("on_toggle_voice failed")
            self._tell("Couldn't change the voice setting.")

    def on_toggle_offline(self, _sender) -> None:
        try:
            new_state = not bool(self.cfg.get("offline"))
            self.cfg.set("offline", new_state)
            self.cfg.save()
            self._tell("Offline mode ON — local model only 💸" if new_state
                else "Offline mode OFF — online agent available",
            )
        except Exception:  # noqa: BLE001
            self.log.exception("on_toggle_offline failed")
            self._tell("Couldn't change offline mode.")

    def on_doctor(self, _sender) -> None:
        try:
            _run_bg(self._doctor)
        except Exception:  # noqa: BLE001
            self.log.exception("on_doctor failed")
            self._tell("Couldn't run doctor.")

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
            self._tell("Couldn't find Python to run doctor.")
            return
        except subprocess.TimeoutExpired:
            self._tell("Doctor timed out.")
            return
        except OSError:
            self._tell("Couldn't run doctor.")
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
            # Leaves no detached helper holding the microphone.
            self.wake.stop()
            stt.stop_helpers()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            self.log.exception("shutdown cleanup failed")
        rumps.quit_application()


def _claim_app_identity(name: str) -> None:
    """Present as *name* rather than "Python", and stay out of the app switcher.

    Run from a bundle the process is still the Python interpreter, so macOS
    labels it "Python" in the app menu, Force Quit and Activity Monitor — and a
    stray Cmd-Q on it kills the assistant. Patching the main bundle's info
    dictionary renames it, and the accessory activation policy removes the Dock
    icon and the Cmd-Tab entry entirely, so there is nothing to quit by
    accident: the menu's own Quit item is the only way out.
    """
    try:
        from AppKit import (
            NSApplication, NSApplicationActivationPolicyAccessory, NSBundle,
        )

        bundle = NSBundle.mainBundle()
        for info in (bundle.localizedInfoDictionary(), bundle.infoDictionary()):
            if info is not None:
                info["CFBundleName"] = name
                info["CFBundleDisplayName"] = name
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory)
    except Exception:  # noqa: BLE001 - cosmetic; never block startup
        get_logger().debug("could not set app identity", exc_info=True)


def run_menubar(cfg) -> None:
    """Start the always-on menu-bar app. Blocks until the user quits."""
    _claim_app_identity(str(cfg.get("assistant_name", "Bob")))
    ProwlApp(cfg).run()
