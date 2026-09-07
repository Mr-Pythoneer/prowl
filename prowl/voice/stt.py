"""Speech-to-text via the native Swift helper ``prowl-listen``.

The heavy lifting (microphone capture + on-device ``SFSpeechRecognizer``) lives
in a tiny Swift binary that we ship alongside the package at
``prowl/helpers/prowl-listen`` and compile separately (``scripts/build_stt.sh``).
This module is a thin, stdlib-only wrapper: it locates the binary, runs it as a
subprocess, and hands back the recognized text.

Public API:
    helper_path() -> Path       location of the ``prowl-listen`` binary
    is_available() -> bool      True if that binary exists and is executable
    listen_once(cfg) -> str     capture one utterance ("" on failure/timeout)
    listen_once_ex(cfg)         same, as ``(text, problem)`` so a caller can say
                                *why* nothing was heard
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Extra wall-clock slack over the helper's own capture cap, to allow for
# recognizer warm-up and final flush before we give up on the subprocess.
_TIMEOUT_SLACK = 8

_BUILD_HINT = (
    "prowl-listen helper not found. Build it with: scripts/build_stt.sh"
)

# Clamp a single capture to a sane window even if the config is garbage.
_MIN_SECONDS = 1
_MAX_SECONDS = 120
_DEFAULT_SECONDS = 12


def _coerce_seconds(value) -> int:
    """Best-effort int in [_MIN_SECONDS, _MAX_SECONDS]; tolerant of bad config.

    A hand-edited config may hold a string ("12"), a float, or junk (""/None).
    Fall back to the default rather than let ``int()`` raise into ``listen_once``.
    """
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        seconds = _DEFAULT_SECONDS
    return max(_MIN_SECONDS, min(_MAX_SECONDS, seconds))


def helper_path() -> Path:
    """Path to the compiled Swift helper binary (may not exist yet).

    The helper ships inside a minimal ``ProwlListen.app`` bundle: macOS only
    reads microphone/speech usage descriptions from a *bundle's* Info.plist, and
    a bare binary is killed on first mic access (see scripts/build_stt.sh). The
    pre-bundle path is still accepted so an old build keeps working.
    """
    helpers = Path(__file__).resolve().parent.parent / "helpers"
    bundled = helpers / "ProwlListen.app" / "Contents" / "MacOS" / "prowl-listen"
    if bundled.is_file():
        return bundled
    return helpers / "prowl-listen"


def is_available() -> bool:
    """True if the helper binary exists and is executable."""
    path = helper_path()
    return path.is_file() and os.access(path, os.X_OK)


# The helper's exit codes, mapped to something worth saying out loud. A denied
# microphone is indistinguishable from silence unless we report it.
_EXIT_REASONS = {
    2: ("Speech Recognition permission is off. Turn it on in System Settings → "
        "Privacy & Security → Speech Recognition."),
    3: ("Microphone permission is off. Turn it on in System Settings → "
        "Privacy & Security → Microphone."),
    4: "The speech recognizer isn't available for that language right now.",
    6: "I couldn't read the microphone — check the input device in Sound settings.",
}


def listen_once(cfg) -> str:
    """Capture one spoken utterance and return the recognized transcript.

    Returns "" on any failure. Use :func:`listen_once_ex` when you want to tell
    the user *why* nothing came back. Never raises.
    """
    return listen_once_ex(cfg)[0]


def listen_once_ex(cfg) -> tuple[str, str]:
    """Capture one utterance as ``(transcript, problem)``.

    Exactly one of the two is meaningful: a transcript on success, or a short
    human-readable ``problem`` explaining the failure (permissions, missing
    helper, timeout). Both empty means the mic worked but heard nothing.

    Runs the helper as ``[prowl-listen, <max_seconds>, <locale>]``. The helper
    prints the final transcript to stdout (status/errors go to stderr) and exits
    0 on success. Never raises.
    """
    path = helper_path()
    max_seconds = _coerce_seconds(cfg.get("stt_max_seconds", _DEFAULT_SECONDS))
    locale = str(cfg.get("stt_locale", "en-US") or "en-US")
    cmd = [str(path), str(max_seconds), locale]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max_seconds + _TIMEOUT_SLACK,
        )
    except FileNotFoundError:
        print(_BUILD_HINT, file=sys.stderr)
        return "", _BUILD_HINT
    except subprocess.TimeoutExpired:
        return "", "The microphone didn't respond in time."
    except OSError as exc:
        # e.g. binary present but not executable / arch mismatch.
        return "", f"Couldn't run the speech helper ({exc})."

    if proc.returncode != 0:
        reason = _EXIT_REASONS.get(proc.returncode)
        if reason is None:
            stderr = (proc.stderr or "").strip().splitlines()
            reason = stderr[-1] if stderr else (
                f"The speech helper exited with code {proc.returncode}."
            )
        return "", reason
    return (proc.stdout or "").strip(), ""
