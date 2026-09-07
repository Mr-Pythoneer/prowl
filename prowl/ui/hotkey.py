"""Global hotkey listener via ``pynput``.

This module is only imported by the menu-bar front-end, so a top-level
``pynput`` import is fine here (the core stays importable without it). It wraps
``pynput.keyboard.GlobalHotKeys`` so a single key combination fires a callback.

Public API:
    start_hotkey(hotkey_str, callback) -> listener   start + return a listener
    stop_hotkey(listener) -> None                    stop a running listener

``hotkey_str`` uses pynput's syntax, e.g. ``"<cmd>+<shift>+space"``.
"""
from __future__ import annotations

import logging
import re

from pynput import keyboard

_log = logging.getLogger("prowl")

# pynput requires named keys to be wrapped in angle brackets ("<space>"), while
# ordinary character keys must be bare ("a"). A hand-edited config that writes
# "space" instead of "<space>" raises ValueError and kills the whole listener,
# so normalize before handing the string over.
_NAMED_KEYS = {
    "alt", "alt_l", "alt_r", "alt_gr", "backspace", "caps_lock", "cmd", "cmd_l",
    "cmd_r", "ctrl", "ctrl_l", "ctrl_r", "delete", "down", "end", "enter", "esc",
    "escape", "home", "insert", "left", "menu", "num_lock", "page_down",
    "page_up", "pause", "print_screen", "right", "scroll_lock", "shift",
    "shift_l", "shift_r", "space", "tab", "up", "media_play_pause",
    "media_volume_mute", "media_volume_down", "media_volume_up",
    "media_previous", "media_next",
}


def _normalize(hotkey_str: str) -> str:
    """Return ``hotkey_str`` with bare named keys wrapped in angle brackets.

    ``"<cmd>+<shift>+space"`` -> ``"<cmd>+<shift>+<space>"``. Already-wrapped
    parts and single characters are left alone, so a correct string is a no-op.
    """
    parts = []
    for raw in (hotkey_str or "").split("+"):
        part = raw.strip()
        if not part:
            continue
        if part.startswith("<") and part.endswith(">"):
            parts.append(part.lower())
            continue
        low = part.lower()
        if low in _NAMED_KEYS or re.fullmatch(r"f\d{1,2}", low):
            parts.append(f"<{low}>")
        else:
            parts.append(part)
    return "+".join(parts)



def _guard(callback):
    """Wrap ``callback`` so any exception is logged, never killing the listener."""

    def _wrapped() -> None:
        try:
            callback()
        except Exception:  # noqa: BLE001 - a bad callback must not stop hotkeys
            _log.exception("hotkey callback raised")

    return _wrapped


def start_hotkey(hotkey_str: str, callback) -> "keyboard.GlobalHotKeys":
    """Start listening for ``hotkey_str`` and call ``callback`` on each press.

    Returns a running ``GlobalHotKeys`` listener (a daemon thread). Stop it with
    :func:`stop_hotkey`. The callback runs on the listener thread; its exceptions
    are swallowed and logged so a single failure never tears down the listener.
    """
    normalized = _normalize(hotkey_str)
    if normalized != hotkey_str:
        _log.debug("normalized hotkey %r -> %r", hotkey_str, normalized)
    listener = keyboard.GlobalHotKeys({normalized: _guard(callback)})
    listener.start()
    _log.debug("hotkey listener started for %s", normalized)
    return listener


def stop_hotkey(listener) -> None:
    """Stop a listener returned by :func:`start_hotkey`. Safe on ``None``/dead."""
    if listener is None:
        return
    try:
        listener.stop()
    except Exception:  # noqa: BLE001 - shutdown must never raise
        _log.exception("failed to stop hotkey listener")


if __name__ == "__main__":
    # Demo: press Cmd+Shift+Space to fire; Ctrl+C to quit.
    logging.basicConfig(level=logging.DEBUG)

    def _demo() -> None:
        print("hotkey fired")

    _listener = start_hotkey("<cmd>+<shift>+space", _demo)
    print("Listening for <cmd>+<shift>+space (Ctrl+C to quit)...")
    try:
        _listener.join()
    except KeyboardInterrupt:
        pass
    finally:
        stop_hotkey(_listener)
