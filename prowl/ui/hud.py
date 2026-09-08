"""Quick text input, notifications, and choosers via ``osascript`` (AppleScript).

A tiny, stdlib-only HUD for the front-end. Everything shells out through
``osascript`` so this module imports cleanly anywhere and needs no third-party
GUI toolkit. Every entry point is defensive: a missing ``osascript`` binary, a
user cancel, or a timeout returns ``None`` (or does nothing) rather than
raising.

Public API:

* :func:`ask_text` — a one-line text prompt; returns the typed text or ``None``.
* :func:`notify`   — a transient macOS notification.
* :func:`choose`   — pick one item from a list; returns the choice or ``None``.
"""
from __future__ import annotations

import subprocess

TITLE = "Prowl"

# AppleScript's ``display dialog`` exits non-zero (-128) when the user cancels;
# a successful "OK" prints ``button returned:OK, text returned:<text>``.
_TEXT_MARKER = "text returned:"
_DEFAULT_TIMEOUT = 120  # seconds a modal may sit waiting on the user


def _escape(text: str) -> str:
    """Escape backslashes and double quotes for embedding in an AS string literal.

    Backslashes must be doubled first so the quote-escaping below isn't itself
    re-escaped. Newlines are flattened to spaces so a pasted blob can't smuggle
    extra AppleScript statements onto their own lines.
    """
    text = str(text).replace("\\", "\\\\").replace('"', '\\"')
    return text.replace("\r", " ").replace("\n", " ")


def _run(script: str, timeout: int = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """Run one AppleScript snippet. Never raises; failures show in returncode."""
    try:
        return subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess([], 127, "", "osascript not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], 124, "", "timed out")
    except subprocess.SubprocessError as exc:  # pragma: no cover - defensive
        return subprocess.CompletedProcess([], 1, "", str(exc))


def ask_text(prompt: str = "What do you need?", default: str = "") -> str | None:
    """Show a one-line text prompt and return the typed text.

    Returns the entered string (possibly empty) on OK, or ``None`` if the user
    cancels, the dialog times out, or ``osascript`` is unavailable.
    """
    script = (
        f'display dialog "{_escape(prompt)}" '
        f'default answer "{_escape(default)}" '
        f'with title "{_escape(TITLE)}"'
    )
    result = _run(script)
    if result.returncode != 0:
        return None
    # stdout looks like: "button returned:OK, text returned:hello there"
    out = result.stdout.rstrip("\n")
    idx = out.find(_TEXT_MARKER)
    if idx == -1:
        return None
    return out[idx + len(_TEXT_MARKER):]


def show_text(title: str, body: str, limit: int = 4000) -> None:
    """Show a block of text the user can read and scroll.

    A spoken one-liner is the wrong shape for a file list or an agent's full
    answer, and a notification banner truncates. This is a plain dialog, which
    is not elegant but is readable and selectable — and needs no extra
    dependency or permission.
    """
    body = (body or "").strip()
    if not body:
        return
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + "\n\n… (truncated)"
    script = (
        f'display dialog "{_escape(body)}" '
        f'with title "{_escape(title)}" '
        'buttons {"Copy", "Done"} default button "Done"'
    )
    result = _run(script)
    if result.returncode == 0 and "button returned:Copy" in result.stdout:
        try:
            subprocess.run(["pbcopy"], input=body, text=True,
                           capture_output=True, timeout=10)
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            pass


def notify(title: str, message: str) -> None:
    """Post a transient macOS notification. Best-effort; never raises."""
    script = (
        f'display notification "{_escape(message)}" '
        f'with title "{_escape(title)}"'
    )
    # Notifications are fleeting and non-interactive; keep the wait short.
    _run(script, timeout=10)


def choose(prompt: str, options: list[str]) -> str | None:
    """Present a chooser and return the selected item.

    Returns the chosen string, or ``None`` if the list is empty, the user
    cancels, or ``osascript`` is unavailable. ``choose from list`` prints the
    literal ``false`` (not a non-zero exit) when cancelled.
    """
    items = [str(o) for o in (options or []) if str(o).strip() != ""]
    if not items:
        return None
    as_list = ", ".join(f'"{_escape(item)}"' for item in items)
    script = (
        f"choose from list {{{as_list}}} "
        f'with prompt "{_escape(prompt)}" with title "{_escape(TITLE)}"'
    )
    result = _run(script)
    if result.returncode != 0:
        return None
    choice = result.stdout.strip()
    if not choice or choice == "false":
        return None
    return choice
