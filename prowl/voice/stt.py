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
    """Path to the compiled Swift helper binary (may not exist yet)."""
    # prowl/voice/stt.py -> parent is prowl/voice, parent.parent is prowl/.
    return Path(__file__).resolve().parent.parent / "helpers" / "prowl-listen"


def is_available() -> bool:
    """True if the helper binary exists and is executable."""
    path = helper_path()
    return path.is_file() and os.access(path, os.X_OK)


def listen_once(cfg) -> str:
    """Capture one spoken utterance and return the recognized transcript.

    Runs the helper as ``[prowl-listen, <max_seconds>, <locale>]``. The helper
    prints the final transcript to stdout (status/errors go to stderr) and exits
    0 on success. Returns "" on any failure: missing binary, timeout, non-zero
    exit, or empty result. Never raises.
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
        return ""
    except subprocess.TimeoutExpired:
        return ""
    except OSError:
        # e.g. binary present but not executable / arch mismatch.
        return ""

    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()
