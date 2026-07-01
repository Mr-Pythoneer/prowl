"""The runtime Context handed to every skill.

A skill never talks to the terminal, the speaker, or the user directly — it goes
through the Context. That keeps skills front-end-agnostic: the same skill works
from the CLI, the menu bar, or a voice turn, because whoever built the Context
supplied the right ``speak`` / ``confirm`` implementations.
"""
from __future__ import annotations

import dataclasses
from typing import Callable

from .config import Config


@dataclasses.dataclass
class Context:
    config: Config
    log: "object"                       # logging.Logger (avoid import cycle in typing)
    speak: Callable[[str], None]        # say + show a short line to the user
    confirm: Callable[[str], bool]      # ask a yes/no question; True = go ahead
    dry_run: bool = False               # when True, skills describe but don't act

    def note(self, msg: str) -> None:
        """Log an info line (not spoken)."""
        try:
            self.log.info(msg)
        except Exception:
            pass
