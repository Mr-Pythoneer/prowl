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

import re
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
# Callback for the utterance currently being spoken (see speak_async).
_done_cb = None
_lock = threading.Lock()


# Escalated answers come back as markdown, and `say` reads the punctuation out
# loud ("asterisk asterisk thirty Python files"). Strip the markup to what a
# person would actually say.
_MD_PATTERNS = (
    (re.compile(r"```.*?```", re.S), " "),            # fenced code blocks
    (re.compile(r"`([^`]*)`"), r"\1"),                # inline code
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),        # images
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),    # links -> link text
    (re.compile(r"(\*\*|__)(.+?)\1", re.S), r"\2"),   # bold
    (re.compile(r"(?<![\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])", re.S), r"\1"),  # italics
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),     # headings
    (re.compile(r"^\s{0,3}[-*+]\s+", re.M), ""),      # bullets
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),          # block quotes
    (re.compile(r"^\s*\|.*\|\s*$", re.M), " "),        # table rows
    (re.compile(r"^\s*[-=]{3,}\s*$", re.M), " "),      # rules
)


def for_speech(text: str) -> str:
    """Reduce markdown to plain prose suitable for a speech synthesizer."""
    out = text or ""
    for pattern, repl in _MD_PATTERNS:
        out = pattern.sub(repl, out)
    # Collapse the whitespace the substitutions leave behind.
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{2,}", ". ", out)
    out = out.replace("\n", " ")
    return re.sub(r"\s+", " ", out).strip()


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
    cmd += ["-r", str(rate), for_speech(text)]
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


def speak_async(text: str, cfg: Any = None, on_done=None) -> None:
    """Speak *text* without waiting. Replaces any in-flight speech. Never raises.

    ``on_done`` is called once the speech ends — whether it finished naturally
    or was cut off by :func:`stop`. Callers use it to clear the on-screen text
    exactly when the voice stops, instead of guessing at a duration.
    """
    text = (text or "").strip()
    if not text:
        if callable(on_done):
            on_done()
        return
    # A new utterance supersedes the old one.
    stop(notify=False)
    try:
        proc = subprocess.Popen(
            _build_cmd(text, cfg),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        if callable(on_done):
            on_done()
        return
    except Exception:
        if callable(on_done):
            on_done()
        return
    with _lock:
        global _proc, _done_cb
        _proc = proc
        _done_cb = on_done

    if callable(on_done):
        # Waiting on the process is the only reliable "speech ended" signal
        # `say` gives us; a thread per utterance is cheap and short-lived.
        threading.Thread(target=_wait_then_notify, args=(proc, on_done),
                         daemon=True, name="prowl-tts-wait").start()


def _wait_then_notify(proc, callback) -> None:
    try:
        proc.wait()
    except Exception:  # noqa: BLE001 - a dead process is still "done"
        pass
    with _lock:
        global _done_cb
        # Only the newest utterance's callback should fire.
        if _done_cb is not callback:
            return
        _done_cb = None
    try:
        callback()
    except Exception:  # noqa: BLE001 - a bad callback must not kill the thread
        pass


def stop(notify: bool = True) -> None:
    """Terminate the current async speech, if any. Never raises.

    The waiter thread sees the process exit and fires ``on_done``, so a stopped
    utterance clears its text just like a finished one.
    """
    global _proc, _done_cb
    with _lock:
        proc = _proc
        _proc = None
        if not notify:
            _done_cb = None
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
