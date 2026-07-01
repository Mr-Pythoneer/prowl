"""File skills — find, open, and reveal files, stdlib-only.

These wrap the macOS command-line tools every Mac already ships with:

* ``mdfind`` — the Spotlight index, for fast name/content search.
* ``open``   — launch a file/folder, or reveal it in Finder with ``-R``.

Everything shells out through :mod:`subprocess` with a timeout and captured
output, and tolerates missing tools (``FileNotFoundError``) and messy args
handed over by the router LLM (values may be strings, keys may be renamed or
absent). A skill never crashes the turn; it returns ``SkillResult.fail`` instead.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from ..core.context import Context
from .base import Skill, SkillResult, SkillSpec, register

HOME = Path(os.path.expanduser("~"))
MAX_RESULTS = 10          # paths shown by find_files
MAX_RECENT = 8            # files shown by recent_downloads


# --- helpers ----------------------------------------------------------------
def _first_str(args: dict[str, Any], *keys: str) -> str:
    """Return the first present, non-empty value among ``keys`` as a string.

    The router LLM sometimes renames args (``query`` vs ``q`` vs ``text``) or
    hands numbers as strings, so we scan a few likely keys and coerce.
    """
    if not isinstance(args, dict):
        return ""
    for key in keys:
        val = args.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return ""


def _expand(path: str) -> Path:
    """Expand ``~`` and env vars into an absolute Path."""
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    """Run a command, capturing text output. None if the tool is missing."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return None
    except subprocess.SubprocessError:
        return None


def _lines(out: subprocess.CompletedProcess[str] | None) -> list[str]:
    """Non-empty stdout lines from a completed process."""
    if out is None or not out.stdout:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


# Map a friendly "kind" word to an mdfind content-type query fragment.
_KIND_MAP = {
    "pdf": 'kMDItemContentType == "com.adobe.pdf"',
    "image": "kMDItemContentTypeTree == 'public.image'",
    "images": "kMDItemContentTypeTree == 'public.image'",
    "photo": "kMDItemContentTypeTree == 'public.image'",
    "audio": "kMDItemContentTypeTree == 'public.audio'",
    "music": "kMDItemContentTypeTree == 'public.audio'",
    "video": "kMDItemContentTypeTree == 'public.movie'",
    "movie": "kMDItemContentTypeTree == 'public.movie'",
    "folder": "kMDItemContentTypeTree == 'public.folder'",
    "app": "kMDItemContentType == 'com.apple.application-bundle'",
    "text": "kMDItemContentTypeTree == 'public.text'",
    "doc": "kMDItemContentTypeTree == 'public.content'",
    "document": "kMDItemContentTypeTree == 'public.content'",
}


# --- find_files -------------------------------------------------------------
@register
class FindFiles(Skill):
    spec = SkillSpec(
        name="find_files",
        description="Search for files by name or contents using Spotlight.",
        examples=[
            "find my tax pdf",
            "search for files about invoices",
            "find images with the word beach",
        ],
        args={
            "query": "words to search for in file names or contents",
            "kind": "optional file kind: pdf, image, audio, video, folder, app",
        },
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        query = _first_str(args, "query", "q", "text", "name", "term")
        if not query:
            return SkillResult.fail("What should I search for?")
        kind = _first_str(args, "kind", "type", "filetype").lower()

        paths = self._mdfind(query, kind)
        source = "Spotlight"
        if not paths:
            paths = self._find_fallback(query)
            source = "a name search"

        if not paths:
            return SkillResult.fail(
                f"No files found for {query!r}.",
                f"Searched with {source}.",
            )

        shown = paths[:MAX_RESULTS]
        detail = "\n".join(shown)
        more = len(paths) - len(shown)
        if more > 0:
            detail += f"\n... and {more} more"
        count = "1 file" if len(paths) == 1 else f"{len(paths)} files"
        return SkillResult.say(
            f"Found {count} for {query!r}.",
            detail,
            count=len(paths),
            paths=shown,
        )

    def _mdfind(self, query: str, kind: str) -> list[str]:
        """Query Spotlight. Combine a text search with an optional kind filter."""
        frag = _KIND_MAP.get(kind)
        if frag:
            expr = f'(kMDItemDisplayName == "*{query}*"cd || '
            expr += f'kMDItemTextContent == "*{query}*"cd) && {frag}'
            cmd = ["mdfind", expr]
        else:
            cmd = ["mdfind", query]
        return _lines(_run(cmd, timeout=20))

    def _find_fallback(self, query: str) -> list[str]:
        """Bounded name search under HOME when Spotlight returns nothing."""
        cmd = [
            "find", str(HOME),
            "-iname", f"*{query}*",
            "-not", "-path", "*/.*",       # skip dotfiles/dirs
        ]
        out = _run(cmd, timeout=15)
        return _lines(out)[:MAX_RESULTS]


# --- open_file --------------------------------------------------------------
@register
class OpenFile(Skill):
    spec = SkillSpec(
        name="open_file",
        description="Open a file or folder in its default app.",
        examples=[
            "open ~/Downloads/report.pdf",
            "open my Documents folder",
        ],
        args={"path": "path to the file or folder to open"},
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        raw = _first_str(args, "path", "file", "target", "name")
        if not raw:
            return SkillResult.fail("Which file should I open?")
        path = _expand(raw)
        if not path.exists():
            return SkillResult.fail("That path doesn't exist.", str(path))
        if ctx.dry_run:
            return SkillResult.say(f"Would open {path.name}.", str(path))

        out = _run(["open", str(path)], timeout=15)
        if out is None:
            return SkillResult.fail("Couldn't run 'open'.")
        if out.returncode != 0:
            return SkillResult.fail(
                f"Couldn't open {path.name}.",
                (out.stderr or "").strip(),
            )
        return SkillResult.say(f"Opened {path.name}.", str(path))


# --- reveal_in_finder -------------------------------------------------------
@register
class RevealInFinder(Skill):
    spec = SkillSpec(
        name="reveal_in_finder",
        description="Show a file or folder in Finder (select it in a window).",
        examples=[
            "reveal ~/Downloads/report.pdf in Finder",
            "show that file in Finder",
        ],
        args={"path": "path to reveal in Finder"},
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        raw = _first_str(args, "path", "file", "target", "name")
        if not raw:
            return SkillResult.fail("Which file should I reveal?")
        path = _expand(raw)
        if not path.exists():
            return SkillResult.fail("That path doesn't exist.", str(path))
        if ctx.dry_run:
            return SkillResult.say(f"Would reveal {path.name} in Finder.", str(path))

        out = _run(["open", "-R", str(path)], timeout=15)
        if out is None:
            return SkillResult.fail("Couldn't run 'open'.")
        if out.returncode != 0:
            return SkillResult.fail(
                f"Couldn't reveal {path.name}.",
                (out.stderr or "").strip(),
            )
        return SkillResult.say(f"Revealed {path.name} in Finder.", str(path))


# --- recent_downloads -------------------------------------------------------
@register
class RecentDownloads(Skill):
    spec = SkillSpec(
        name="recent_downloads",
        description="List the newest files in the Downloads folder.",
        examples=[
            "what did I just download",
            "show my recent downloads",
        ],
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        folder = HOME / "Downloads"
        if not folder.is_dir():
            return SkillResult.fail("No Downloads folder found.", str(folder))

        try:
            entries = [p for p in folder.iterdir() if not p.name.startswith(".")]
        except OSError as exc:
            return SkillResult.fail("Couldn't read Downloads.", str(exc))

        def _mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        entries.sort(key=_mtime, reverse=True)
        newest = entries[:MAX_RECENT]
        if not newest:
            return SkillResult.say("Downloads is empty.", str(folder))

        detail = "\n".join(p.name for p in newest)
        noun = "download" if len(newest) == 1 else "downloads"
        return SkillResult.say(
            f"Your {len(newest)} most recent {noun}.",
            detail,
            paths=[str(p) for p in newest],
        )
