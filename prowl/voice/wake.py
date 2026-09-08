"""Wake-word listening — "Hey Bob" without touching the keyboard.

The speech helper runs in streaming mode: one resident recogniser that appends
a line to a file for every phrase it hears. This listener tails that file.

The earlier design captured fixed-length windows back to back, and it could not
work: the recogniser is deaf between windows, and it only reports once a window
closes — so a wake word was either missed outright or acted on many seconds
after it was spoken. A single resident recogniser has neither problem.

Two shapes of utterance are handled:

    "Hey Bob, open Safari"      -> wake and command in one line
    "Hey Bob"  then  "open Safari"
                                -> bare wake arms him; the next line is the
                                   command, until `_ARM_SECONDS` passes

Public API::

    listener = WakeListener(cfg, on_wake=..., on_command=...)
    listener.start(); listener.stop()
    listener.mute() / unmute()      # while Bob is speaking, so he doesn't
                                    # answer his own voice
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .stt import app_path, stop_helpers

_log = logging.getLogger("prowl")

# How long a bare "Hey Bob" waits for the command that follows it. Kept short:
# during this window the next thing anyone says in the room is executed, so a
# generous timeout is a generous window for someone else's sentence.
_ARM_SECONDS = 5.0

# How often to check the transcript file for new lines.
_POLL_SECONDS = 0.25

# The greeting is REQUIRED, not optional. With it optional, any sentence merely
# starting with his name woke him — "Bob was asking about it" armed the listener
# and handed the next sentence to the router. Addressing an assistant by name
# almost always carries the greeting, so requiring it costs nothing.
_PREFIX = r"(?:hey|hi|hello|ok|okay|yo)\s+"

# ...except when the name is the entire utterance ("Bob?"), which is
# unambiguously addressing him.
_BARE_NAME_ONLY = True


class WakeListener:
    """Tails the streaming recogniser and fires on the wake word."""

    def __init__(self, cfg, on_wake=None, on_command=None, on_error=None):
        self.cfg = cfg
        self.on_wake = on_wake
        self.on_command = on_command
        self.on_error = on_error
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._muted = threading.Event()
        # Set while a foreground capture (F5, click) owns the microphone.
        self._suspended = threading.Event()
        self._armed_until = 0.0
        self._path: Path | None = None

    # -- lifecycle ------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._muted.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="prowl-wake")
        self._thread.start()
        _log.info("wake listener started (word=%r)", self.wake_word)

    def stop(self) -> None:
        self._stop.set()
        self._thread = None
        stop_helpers()
        _log.info("wake listener stopped")

    # Bob's own speech comes back through the microphone; ignore the stream
    # while he is talking rather than letting him answer himself.
    def mute(self) -> None:
        self._muted.set()

    def unmute(self) -> None:
        self._muted.clear()

    # A manual voice turn needs the microphone to itself. Two recognisers
    # fighting over the input device is what produced the intermittent
    # "I couldn't reach the microphone" — one of them loses, seemingly at
    # random, and reports it as a permission problem.
    def suspend(self) -> None:
        """Hand the microphone to a foreground capture."""
        if self._suspended.is_set():
            return
        self._suspended.set()
        stop_helpers()

    def resume(self) -> None:
        """Take the microphone back and start listening again."""
        if not self._suspended.is_set():
            return
        self._suspended.clear()
        # The loop notices the helper is gone and respawns it.

    # -- config ---------------------------------------------------------------
    @property
    def wake_word(self) -> str:
        default = str(self.cfg.get("assistant_name", "Bob")).lower()
        return str(self.cfg.get("wake_word", default) or default).strip().lower()

    def _pattern(self) -> re.Pattern:
        word = re.escape(self.wake_word)
        # Anchored to the start of the phrase, after optional filler. A short
        # name turns up mid-sentence in ordinary conversation ("I told Bob
        # about it"); addressing someone by name naturally comes first, so
        # anchoring removes that whole class of false wake for free.
        # Either "hey bob ..." or the name alone as the whole utterance.
        return re.compile(
            rf"^[\s,.]*(?:{_PREFIX}{word}\b[\s,.!?-]*(.*)"
            rf"|{word}[\s,.!?-]*$())", re.I)

    def _loose_pattern(self) -> re.Pattern:
        """"bob <something>" — his name first, without a greeting.

        Accepted only when the remainder is recognisably a safe command (see
        :meth:`_safe_bare_command`). "Bob, open Safari" is how people actually
        talk; "Bob was asking about it" is not addressed to him at all, and the
        two are structurally identical.
        """
        word = re.escape(self.wake_word)
        return re.compile(rf"^[\s,.]*{word}\b[\s,.!?-]+(.+)$", re.I)

    @staticmethod
    def _safe_bare_command(text: str) -> bool:
        """True if *text* is a command the fast router maps to a safe skill.

        Deliberately strict: only the deterministic pre-router counts, and only
        for non-destructive skills. Anything the model would have to interpret —
        and anything that could delete, quit or shell out — needs the greeting,
        so an overheard sentence can never reach it.
        """
        try:
            from ..brain import prematch
            from .. import skills as skills_pkg
        except Exception:  # noqa: BLE001 - never break listening on an import
            return False
        hit = prematch.match(text)
        if not hit:
            return False
        skill = skills_pkg.REGISTRY.get(hit[0])
        return skill is not None and not skill.spec.destructive

    # -- the resident recogniser ---------------------------------------------
    def _spawn(self) -> Path | None:
        """Start the streaming helper and return the file it writes to."""
        app = app_path()
        if not app.is_dir():
            if callable(self.on_error):
                self.on_error("The speech helper isn't built "
                              "(run scripts/build_stt.sh).")
            return None
        stop_helpers()
        tmp = tempfile.NamedTemporaryFile(prefix="prowl-wake-", suffix=".txt",
                                          delete=False)
        tmp.close()
        path = Path(tmp.name)
        locale = str(self.cfg.get("stt_locale", "en-US") or "en-US")
        try:
            # Through LaunchServices, so the microphone permission belongs to
            # ProwlListen.app rather than to whatever launched Prowl.
            subprocess.run(
                ["open", "-n", "-a", str(app), "--args",
                 "--stream", "0", locale, str(path)],
                capture_output=True, timeout=20, check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
            if callable(self.on_error):
                self.on_error(f"Couldn't start listening ({exc}).")
            return None
        return path

    @staticmethod
    def _helper_alive() -> bool:
        try:
            out = subprocess.run(["pgrep", "-f", "prowl-listen --stream"],
                                 capture_output=True, text=True, timeout=5)
            return bool(out.stdout.strip())
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return True     # can't tell; assume fine rather than thrash

    # -- loop -----------------------------------------------------------------
    def _loop(self) -> None:
        path = self._spawn()
        if path is None:
            return
        self._path = path
        offset = 0
        last_check = time.time()

        while not self._stop.is_set():
            time.sleep(_POLL_SECONDS)
            if self._stop.is_set():
                return

            if self._suspended.is_set():
                # Drop anything buffered while we were away, so the command
                # just spoken into the foreground capture isn't replayed here.
                try:
                    offset = path.stat().st_size
                except OSError:
                    pass
                last_check = 0.0        # re-check as soon as we resume
                continue

            # If the helper died (crash, or the user revoked the microphone),
            # bring it back rather than going quietly deaf.
            if time.time() - last_check > 10:
                last_check = time.time()
                if not self._helper_alive():
                    _log.info("streaming helper gone; restarting")
                    new_path = self._spawn()
                    if new_path is None:
                        return
                    path, offset, self._path = new_path, 0, new_path
                    continue

            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= offset:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                    offset = fh.tell()
            except OSError:
                continue

            for line in chunk.splitlines():
                line = line.strip()
                if not line or self._stop.is_set():
                    continue
                # Discard anything heard while Bob was talking.
                if self._muted.is_set():
                    continue
                self._handle(line)

        # Leaving the loop means we are done listening.
        stop_helpers()

    def _handle(self, line: str) -> None:
        match = self._pattern().search(line)
        if match is None:
            loose = self._loose_pattern().search(line)
            if loose is not None and self._safe_bare_command(loose.group(1)):
                _log.info("wake (bare name) command: %r", loose.group(1).strip())
                if callable(self.on_wake):
                    self.on_wake()
                if callable(self.on_command):
                    self.on_command(loose.group(1).strip())
                return
        if match:
            # Group 1 is the trailing command after "hey bob ..."; group 2 is
            # the empty alternative matched when the name stands alone.
            trailing = (match.group(1) or "").strip()
            _log.info("wake word heard; trailing=%r", trailing)
            if callable(self.on_wake):
                self.on_wake()
            if trailing:
                self._armed_until = 0.0
                if callable(self.on_command):
                    self.on_command(trailing)
            else:
                # Bare "Hey Bob" — the next thing said is the command.
                self._armed_until = time.time() + _ARM_SECONDS
            return

        # Not a wake word; if he was just called, this is the command.
        if time.time() < self._armed_until:
            self._armed_until = 0.0
            _log.info("wake command: %r", line)
            if callable(self.on_command):
                self.on_command(line)
