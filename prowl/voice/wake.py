"""Wake-word listening — "Hey Prowl" without touching the keyboard.

There is no always-on keyword spotter here. macOS already gives us a good
on-device recognizer, so this simply runs short capture windows back to back
and checks each transcript for the wake word. That is cheap enough to leave
running, needs no model download, and reuses the exact speech path the hotkey
uses.

Two shapes of utterance are handled:

    "Hey Prowl"                 -> wake, then listen again for the command
    "Hey Prowl, open Safari"    -> wake and command in one breath

Public API::

    listener = WakeListener(cfg, on_wake=..., on_command=...)
    listener.start()
    listener.stop()
"""
from __future__ import annotations

import logging
import re
import threading

from .stt import listen_once_ex, stop_helpers

_log = logging.getLogger("prowl")

# How long each listening window runs while waiting for the wake word.
#
# Long, deliberately. The recognizer already returns as soon as you stop
# talking, so a long window costs nothing while the room is quiet and still
# reacts immediately when you speak. What it avoids is the ~0.8s relaunch gap
# between windows: at 5s windows Prowl would be deaf ~13% of the time and miss
# you calling it; at 45s that drops to under 2%. Kept below Apple's ~60s cap on
# a single recognition request.
_WINDOW_SECONDS = 45

# Filler that may precede the wake word.
_PREFIX = r"(?:hey|hi|hello|ok|okay|yo)?\s*"


class WakeListener:
    """Runs capture windows on a background thread until the wake word appears."""

    def __init__(self, cfg, on_wake=None, on_command=None, on_error=None):
        self.cfg = cfg
        self.on_wake = on_wake
        self.on_command = on_command
        self.on_error = on_error
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Consecutive failures, so a denied microphone doesn't spin forever.
        self._failures = 0

    # -- lifecycle ------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._failures = 0
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="prowl-wake")
        self._thread.start()
        _log.info("wake listener started (word=%r)", self.wake_word)

    def stop(self) -> None:
        self._stop.set()
        self._thread = None
        # The in-flight capture is a detached app; without this it keeps the
        # microphone until its window ends.
        stop_helpers()
        _log.info("wake listener stopped")

    # -- config ---------------------------------------------------------------
    @property
    def wake_word(self) -> str:
        return str(self.cfg.get("wake_word", "prowl") or "prowl").strip().lower()

    def _pattern(self) -> re.Pattern:
        word = re.escape(self.wake_word)
        # Allow the recognizer's common mishearings of a short name by matching
        # on a word boundary rather than the whole utterance.
        return re.compile(rf"\b{_PREFIX}{word}\b[\s,.!?-]*(.*)", re.I)

    # -- loop -----------------------------------------------------------------
    def _loop(self) -> None:
        # Each window is its own recognizer session; between windows we check
        # the stop flag, so stopping is immediate from the user's point of view.
        cfg = _WindowConfig(self.cfg, _WINDOW_SECONDS)
        while not self._stop.is_set():
            text, problem = listen_once_ex(cfg)
            if self._stop.is_set():
                return
            if problem:
                self._failures += 1
                # Three strikes: something is wrong (permissions, no device)
                # and retrying in a tight loop helps nobody.
                if self._failures >= 3:
                    _log.warning("wake listener stopping: %s", problem)
                    if callable(self.on_error):
                        self.on_error(problem)
                    return
                continue
            self._failures = 0
            if not text:
                continue

            match = self._pattern().search(text)
            if not match:
                continue

            trailing = (match.group(1) or "").strip()
            _log.info("wake word heard; trailing=%r", trailing)
            if callable(self.on_wake):
                self.on_wake()
            if trailing:
                # "Hey Prowl, open Safari" — the command came with the wake word.
                if callable(self.on_command):
                    self.on_command(trailing)
                continue
            # Bare wake word: listen again for the actual request.
            command, problem = listen_once_ex(self.cfg)
            if self._stop.is_set():
                return
            if command and callable(self.on_command):
                self.on_command(command)
            elif problem and callable(self.on_error):
                self.on_error(problem)


class _WindowConfig:
    """Config view with a shorter capture window, for the waiting phase."""

    def __init__(self, cfg, seconds: int):
        self._cfg, self._seconds = cfg, seconds

    def get(self, key, default=None):
        if key == "stt_max_seconds":
            return self._seconds
        return self._cfg.get(key, default)
