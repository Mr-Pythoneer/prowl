"""The Skill contract.

A *skill* is one thing Prowl can do on the desktop: open an app, set volume,
find a file, clean junk, etc. Every skill declares a :class:`SkillSpec` (what it
is, so the router LLM can pick it) and implements :meth:`Skill.run`.

Skills are registered with the ``@register`` decorator and collected in
``prowl.skills.REGISTRY``.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from ..core.context import Context


@dataclasses.dataclass
class SkillResult:
    """What a skill hands back to the router."""

    ok: bool
    speech: str                    # one short line to speak / show
    detail: str = ""               # optional longer text (shown, not spoken)
    data: dict[str, Any] | None = None

    @classmethod
    def say(cls, speech: str, detail: str = "", **data: Any) -> "SkillResult":
        return cls(ok=True, speech=speech, detail=detail, data=data or None)

    @classmethod
    def fail(cls, speech: str, detail: str = "") -> "SkillResult":
        return cls(ok=False, speech=speech, detail=detail)


@dataclasses.dataclass
class SkillSpec:
    """Metadata the router shows the LLM so it can route an utterance."""

    name: str                             # unique id, e.g. "open_app"
    description: str                      # one line: what it does
    examples: list[str] = dataclasses.field(default_factory=list)
    args: dict[str, str] = dataclasses.field(default_factory=dict)  # name -> description
    destructive: bool = False             # if True, executor confirms first
    enabled: bool = True


class Skill:
    """Base class. Subclasses set ``spec`` and implement ``run``."""

    spec: SkillSpec

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:  # pragma: no cover
        raise NotImplementedError

    # Optional: a skill can veto itself at load time (e.g. tool not installed).
    def available(self, ctx: Context | None = None) -> bool:
        return True


# --- registry ---------------------------------------------------------------
REGISTRY: dict[str, Skill] = {}


def register(skill_cls: type[Skill]) -> type[Skill]:
    """Class decorator: instantiate and add to the global registry."""
    inst = skill_cls()
    if not getattr(inst, "spec", None):
        raise TypeError(f"{skill_cls.__name__} has no .spec")
    REGISTRY[inst.spec.name] = inst
    return skill_cls
