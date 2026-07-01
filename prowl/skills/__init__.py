"""Skill registry.

Importing this package imports every skill module, which triggers each module's
``@register`` decorators and populates :data:`REGISTRY`.

To add a skill: create ``prowl/skills/<name>.py``, define ``@register`` classes,
and add the module to ``_SKILL_MODULES`` below.
"""
from __future__ import annotations

import importlib

from .base import REGISTRY, Skill, SkillResult, SkillSpec, register  # noqa: F401

# Order is cosmetic (affects listing order for the router prompt).
_SKILL_MODULES = [
    "system",    # open apps, volume, brightness, sleep, lock, screenshot, clipboard
    "files",     # find / reveal files
    "apps",      # control the frontmost / named app via AppleScript
    "web",       # open URL, web search
    "cleanup",   # reclaim disk / junk (destructive, dry-run by default)
    "shell",     # guarded free-form shell (destructive)
]


def load_all() -> dict[str, Skill]:
    """Import every skill module and return the populated registry."""
    for mod in _SKILL_MODULES:
        try:
            importlib.import_module(f"{__name__}.{mod}")
        except Exception as exc:  # a broken skill must not sink the whole app
            import logging

            logging.getLogger("prowl").warning("skill module %s failed to load: %s", mod, exc)
    return REGISTRY


def specs_text() -> str:
    """Render the enabled skills as a compact catalog for the router LLM."""
    lines = []
    for skill in REGISTRY.values():
        s = skill.spec
        if not s.enabled:
            continue
        arglist = ", ".join(f"{k} ({v})" for k, v in s.args.items()) or "none"
        lines.append(f"- {s.name}: {s.description} | args: {arglist}")
    return "\n".join(lines)
