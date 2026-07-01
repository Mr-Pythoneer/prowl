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

from pynput import keyboard

_log = logging.getLogger("prowl")


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
    listener = keyboard.GlobalHotKeys({hotkey_str: _guard(callback)})
    listener.start()
    _log.debug("hotkey listener started for %s", hotkey_str)
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
