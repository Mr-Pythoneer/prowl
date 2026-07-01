"""Text-to-speech via the macOS ``say`` command.

Stdlib only. ``say`` ships with macOS, so no install or network is needed.
Nothing here ever raises: TTS is a nicety, and a broken speech synth should
never take down the agent. Failures are swallowed silently (the UI still shows
the text).

Public API::

    speak(text, cfg=None)        # blocking; speaks and waits
    speak_async(text, cfg=None)  # non-blocking; spawns and returns
    stop()                       # terminate any running speech
"""
from __future__ import annotations

import subprocess
import threading
from typing import Any

# Defaults used when no config is supplied.
_DEFAULT_VOICE = "Samantha"
_DEFAULT_RATE = 190

# Hard cap so a runaway `say` (e.g. a very long paragraph) can't block forever.
_SPEAK_TIMEOUT = 120

# The most recently spawned async process, so stop() can kill it.
_proc: subprocess.Popen[bytes] | None = None
_lock = threading.Lock()


def _settings(cfg: Any) -> tuple[str, int]:
    """Return (voice, rate) from *cfg*, falling back to defaults."""
    if cfg is None:
        return _DEFAULT_VOICE, _DEFAULT_RATE
    try:
        voice = cfg.get("tts_voice", _DEFAULT_VOICE)
    except Exception:
        voice = _DEFAULT_VOICE
    try:
        rate_raw = cfg.get("tts_rate", _DEFAULT_RATE)
        rate = int(rate_raw)
    except Exception:
        rate = _DEFAULT_RATE
    if not isinstance(voice, str):
        voice = _DEFAULT_VOICE
    return voice, rate


def _build_cmd(text: str, cfg: Any) -> list[str]:
    """Assemble the ``say`` argv for *text*."""
    voice, rate = _settings(cfg)
    cmd = ["say"]
    if voice:
        cmd += ["-v", voice]
    cmd += ["-r", str(rate), text]
    return cmd


def speak(text: str, cfg: Any = None) -> None:
    """Speak *text* and block until ``say`` finishes. Never raises."""
    text = (text or "").strip()
    if not text:
        return
    try:
        subprocess.run(
            _build_cmd(text, cfg),
            capture_output=True,
            timeout=_SPEAK_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    except Exception:
        pass


def speak_async(text: str, cfg: Any = None) -> None:
    """Speak *text* without waiting. Replaces any in-flight speech. Never raises."""
    text = (text or "").strip()
    if not text:
        return
    # A new utterance supersedes the old one.
    stop()
    try:
        proc = subprocess.Popen(
            _build_cmd(text, cfg),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return
    except Exception:
        return
    with _lock:
        global _proc
        _proc = proc


def stop() -> None:
    """Terminate the current async speech, if any. Never raises."""
    global _proc
    with _lock:
        proc = _proc
        _proc = None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
