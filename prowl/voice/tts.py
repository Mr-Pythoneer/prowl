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
from functools import lru_cache
from typing import Any

# Defaults used when no config is supplied.
_DEFAULT_VOICE = "Samantha"
_DEFAULT_RATE = 175

# macOS ships only "compact" voices out of the box — small, pre-neural synths
# that sound robotic. Apple's Enhanced and Premium voices are a large quality
# jump and are downloaded on demand (System Settings > Accessibility > Spoken
# Content > System Voice > Manage Voices). `say -v '?'` shows them with the
# quality in the name, e.g. "Ava (Premium)". When tts_voice is "auto" we pick
# the best installed voice so Prowl improves the moment one is downloaded.
#
# Ordered by how natural they sound conversationally; the first installed wins.
_PREFERRED = (
    "Ava", "Zoe", "Allison", "Susan", "Samantha", "Joelle",
    "Tom", "Evan", "Nathan", "Noelle", "Serena", "Stephanie",
)

# Hard cap so a runaway `say` (e.g. a very long paragraph) can't block forever.
_SPEAK_TIMEOUT = 120

# The most recently spawned async process, so stop() can kill it.
_proc: subprocess.Popen[bytes] | None = None
_lock = threading.Lock()


@lru_cache(maxsize=4)
def list_voices(lang: str = "en") -> tuple[tuple[str, int], ...]:
    """Installed voices for *lang* as ``(name, quality)``, best quality first.

    Quality is 2 for Premium, 1 for Enhanced, 0 for the stock compact voices —
    read from the parenthesised suffix `say` puts in the voice name.
    """
    try:
        out = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, timeout=15,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return ()
    voices: list[tuple[str, int]] = []
    for line in out.splitlines():
        # "Ava (Premium)       en_US    # Hello! My name is Ava."
        head = line.split("#", 1)[0].rstrip()
        parts = head.rsplit(None, 1)
        if len(parts) != 2:
            continue
        name, locale = parts[0].strip(), parts[1].strip()
        if not name or not locale.lower().startswith(lang.lower()):
            continue
        low = name.lower()
        quality = 2 if "(premium)" in low else 1 if "(enhanced)" in low else 0
        voices.append((name, quality))
    voices.sort(key=lambda v: -v[1])
    return tuple(voices)


@lru_cache(maxsize=4)
def best_voice(lang: str = "en") -> str:
    """The most natural-sounding installed voice, or "" if none can be listed.

    Prefers Premium over Enhanced over compact, and within a quality tier
    prefers the voices in :data:`_PREFERRED` before anything else.
    """
    voices = list_voices(lang)
    if not voices:
        return ""
    best_quality = voices[0][1]
    top = [n for n, q in voices if q == best_quality]
    for wanted in _PREFERRED:
        for name in top:
            # "Ava" matches "Ava (Premium)" as well as a bare "Ava".
            if name == wanted or name.startswith(wanted + " ("):
                return name
    return top[0]


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
    # "auto" (the default) tracks the best voice actually installed, so
    # downloading a Premium voice improves Prowl with no config change.
    if voice.strip().lower() in ("", "auto", "best"):
        voice = best_voice() or _DEFAULT_VOICE
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


def refresh_voices() -> None:
    """Forget the cached voice list — call after installing new system voices."""
    list_voices.cache_clear()
    best_voice.cache_clear()
