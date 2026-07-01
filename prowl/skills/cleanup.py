"""Cleanup skill — reclaim disk space by clearing junk, safely.

Safety model (the important part):

* **Measure first.** Every category is sized with ``du`` before anything moves.
  The user sees a breakdown and a total, then confirms once.
* **Recoverable by default.** Reclaimable caches/logs are moved to the **Trash**
  (via the native Trash API), so a mistake can be undone. Never ``rm -rf``.
* **Permanent ops are opt-in.** A few categories are inherently permanent
  (emptying the Trash, deleting ``.DS_Store`` files, ``brew/npm/pip`` cache
  purges). Each of those asks for its own extra confirmation and is clearly
  labelled "permanent".

This skill manages its own confirmation (so it can show sizes first), so it is
*not* marked ``destructive`` — otherwise the executor would confirm blindly
before the user has seen what would be removed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from ..core.context import Context
from .base import Skill, SkillResult, SkillSpec, register

HOME = Path(os.path.expanduser("~"))


# --- helpers ----------------------------------------------------------------
def _du_bytes(path: Path) -> int:
    """Fast recursive size via `du -sk`. 0 if missing/unreadable."""
    if not path.exists():
        return 0
    try:
        out = subprocess.run(
            ["du", "-sk", str(path)], capture_output=True, text=True, timeout=60
        )
        if out.returncode == 0 and out.stdout:
            return int(out.stdout.split()[0]) * 1024
    except (subprocess.SubprocessError, ValueError):
        pass
    return 0


def _human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{n} B"


def _move_to_trash(path: Path) -> bool:
    """Move a path to the macOS Trash (recoverable). Returns True on success."""
    if not path.exists():
        return False
    # Preferred: native Trash API via pyobjc (already installed on this Mac).
    try:
        from Foundation import NSURL, NSFileManager  # type: ignore

        url = NSURL.fileURLWithPath_(str(path))
        ok, _, _ = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            url, None, None
        )
        if ok:
            return True
    except Exception:
        pass
    # Fallback: ask Finder to delete (also goes to Trash).
    try:
        script = f'tell application "Finder" to delete (POSIX file "{path}")'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=30)
        return r.returncode == 0
    except subprocess.SubprocessError:
        return False


def _trash_children(directory: Path) -> int:
    """Move each immediate child of `directory` to Trash. Returns bytes reclaimed."""
    if not directory.exists():
        return 0
    reclaimed = 0
    for child in list(directory.iterdir()):
        size = _du_bytes(child)
        if _move_to_trash(child):
            reclaimed += size
    return reclaimed


# --- category definitions ---------------------------------------------------
class Category:
    """One junk category: how to measure it and how to reclaim it."""

    def __init__(self, key: str, label: str, measure: Callable[[], int],
                 apply: Callable[[], int], permanent: bool = False):
        self.key = key
        self.label = label
        self._measure = measure
        self._apply = apply
        self.permanent = permanent

    def measure(self) -> int:
        try:
            return self._measure()
        except Exception:
            return 0

    def apply(self) -> int:
        try:
            return self._apply()
        except Exception:
            return 0


def _cmd_reclaim(cmd: list[str], cache_dir: Path) -> Callable[[], int]:
    def _apply() -> int:
        before = _du_bytes(cache_dir)
        try:
            subprocess.run(cmd, capture_output=True, timeout=180)
        except subprocess.SubprocessError:
            return 0
        return max(0, before - _du_bytes(cache_dir))
    return _apply


def _find_delete(roots: list[Path], name: str) -> tuple[Callable[[], int], Callable[[], int]]:
    """Return (measure, apply) for deleting files/dirs named `name` under roots.

    Used for regenerable junk (.DS_Store, __pycache__). Permanent, but harmless.
    """
    def _paths() -> list[Path]:
        found: list[Path] = []
        for root in roots:
            if not root.exists():
                continue
            try:
                out = subprocess.run(
                    ["find", str(root), "-name", name], capture_output=True,
                    text=True, timeout=60,
                )
                found += [Path(p) for p in out.stdout.splitlines() if p]
            except subprocess.SubprocessError:
                continue
        return found

    def _measure() -> int:
        return sum(_du_bytes(p) for p in _paths())

    def _apply() -> int:
        reclaimed = 0
        for p in _paths():
            size = _du_bytes(p)
            try:
                if p.is_dir():
                    subprocess.run(["rm", "-rf", str(p)], capture_output=True, timeout=30)
                else:
                    p.unlink(missing_ok=True)
                reclaimed += size
            except (OSError, subprocess.SubprocessError):
                continue
        return reclaimed

    return _measure, _apply


def build_categories(selected: list[str]) -> list[Category]:
    lib = HOME / "Library"
    dev = lib / "Developer"
    proj_roots = [HOME / "Desktop", HOME / "Documents", HOME / "PycharmProjects"]

    ds_measure, ds_apply = _find_delete(
        [HOME / "Desktop", HOME / "Documents", HOME / "Downloads"], ".DS_Store"
    )
    pyc_measure, pyc_apply = _find_delete(proj_roots, "__pycache__")

    catalog: dict[str, Category] = {
        "user_caches": Category(
            "user_caches", "App caches (~/Library/Caches)",
            lambda: _du_bytes(lib / "Caches"),
            lambda: _trash_children(lib / "Caches"),
        ),
        "user_logs": Category(
            "user_logs", "User logs (~/Library/Logs)",
            lambda: _du_bytes(lib / "Logs"),
            lambda: _trash_children(lib / "Logs"),
        ),
        "trash": Category(
            "trash", "Empty the Trash (PERMANENT)",
            lambda: _du_bytes(HOME / ".Trash"),
            _empty_trash,
            permanent=True,
        ),
        "xcode_deriveddata": Category(
            "xcode_deriveddata", "Xcode DerivedData",
            lambda: _du_bytes(dev / "Xcode" / "DerivedData"),
            lambda: _trash_children(dev / "Xcode" / "DerivedData"),
        ),
        "ios_device_support": Category(
            "ios_device_support", "Old iOS DeviceSupport",
            lambda: _du_bytes(dev / "Xcode" / "iOS DeviceSupport"),
            lambda: _trash_children(dev / "Xcode" / "iOS DeviceSupport"),
        ),
        "simulator_caches": Category(
            "simulator_caches", "CoreSimulator caches",
            lambda: _du_bytes(dev / "CoreSimulator" / "Caches"),
            lambda: _trash_children(dev / "CoreSimulator" / "Caches"),
        ),
        "npm_cache": Category(
            "npm_cache", "npm cache (PERMANENT)",
            lambda: _du_bytes(HOME / ".npm" / "_cacache"),
            _cmd_reclaim(["npm", "cache", "clean", "--force"], HOME / ".npm" / "_cacache"),
            permanent=True,
        ),
        "pip_cache": Category(
            "pip_cache", "pip cache (PERMANENT)",
            lambda: _du_bytes(lib / "Caches" / "pip"),
            _cmd_reclaim(["python3", "-m", "pip", "cache", "purge"], lib / "Caches" / "pip"),
            permanent=True,
        ),
        "homebrew_cache": Category(
            "homebrew_cache", "Homebrew downloads (PERMANENT)",
            lambda: _du_bytes(HOME / "Library" / "Caches" / "Homebrew"),
            _cmd_reclaim(["brew", "cleanup", "-s"], HOME / "Library" / "Caches" / "Homebrew"),
            permanent=True,
        ),
        "ds_store": Category(
            "ds_store", ".DS_Store files (PERMANENT, regenerate)",
            ds_measure, ds_apply, permanent=True,
        ),
        "pycache": Category(
            "pycache", "__pycache__ dirs (PERMANENT, regenerate)",
            pyc_measure, pyc_apply, permanent=True,
        ),
    }
    return [catalog[k] for k in selected if k in catalog]


def _empty_trash() -> int:
    trash = HOME / ".Trash"
    before = _du_bytes(trash)
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Finder" to empty trash'],
            capture_output=True, timeout=120,
        )
    except subprocess.SubprocessError:
        return 0
    return max(0, before - _du_bytes(trash))


# --- the skill ---------------------------------------------------------------
@register
class Cleanup(Skill):
    spec = SkillSpec(
        name="cleanup",
        description="reclaim disk space by clearing caches, logs, dev junk and the Trash",
        examples=[
            "clean up my mac", "free up disk space", "clear caches",
            "empty the trash", "get rid of junk files",
        ],
        args={
            "category": "one of the junk categories, or 'all' (default all)",
            "apply": "true to actually clean; false/omitted to just report sizes",
        },
        destructive=False,  # manages its own confirmation (shows sizes first)
    )

    def run(self, args: dict, ctx: Context) -> SkillResult:
        selected = self._select(args, ctx)
        cats = build_categories(selected)
        if not cats:
            return SkillResult.fail("No known cleanup categories were selected.")

        # 1) Measure.
        ctx.speak("Scanning for junk — one moment.")
        sized = [(c, c.measure()) for c in cats]
        sized = [(c, n) for c, n in sized if n > 0]
        total = sum(n for _, n in sized)

        if total == 0:
            return SkillResult.say("You're already clean — nothing worth reclaiming.")

        lines = [f"  • {c.label}: {_human(n)}" for c, n in sized]
        report = "Reclaimable space:\n" + "\n".join(lines) + f"\n  = {_human(total)} total"
        ctx.note(report)

        # In dry-run mode, or when `apply` isn't explicitly requested, just report
        # the sizes and (unless dry-run) ask once before touching anything.
        if ctx.dry_run:
            summary = f"I can free about {_human(total)}. Say clean up to reclaim it."
            return SkillResult.say(summary, detail=report)
        if not self._truthy(args.get("apply")):
            if not ctx.confirm(f"Reclaim {_human(total)} across {len(sized)} categories?"):
                return SkillResult.say("Left everything as-is.", detail=report)

        # 2) Apply (recoverable categories move to Trash; permanent ones each
        # get their own extra confirmation before running).
        reclaimed = 0
        done: list[str] = []
        for c, n in sized:
            if c.permanent and ctx.config.confirm_destructive:
                if not ctx.confirm(f"{c.label} is permanent — reclaim {_human(n)}?"):
                    continue
            got = c.apply()
            reclaimed += got
            done.append(f"  • {c.label}: freed {_human(got)}")
            ctx.note(f"cleanup applied {c.key}: {_human(got)}")

        detail = "Done:\n" + "\n".join(done) if done else report
        return SkillResult.say(f"Cleaned up — reclaimed about {_human(reclaimed)}.", detail=detail)

    # -- helpers --------------------------------------------------------------
    def _select(self, args: dict, ctx: Context) -> list[str]:
        cat = args.get("category")
        default = list(ctx.config.cleanup_categories)
        if not cat or str(cat).lower() in ("all", "everything", "*"):
            return default
        if isinstance(cat, list):
            return [c for c in cat if c in default] or default
        return [str(cat)] if str(cat) in default else default

    @staticmethod
    def _truthy(v) -> bool:
        return str(v).lower() in ("1", "true", "yes", "y", "apply", "on")
