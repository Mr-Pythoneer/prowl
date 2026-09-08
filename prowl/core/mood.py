"""Bob's mood — currently just the one, but the shape is general.

A mood is a temporary personality override: it changes what he says, how he
says it, and how he looks, and it expires on its own. Kept out of the skills
and the router because it is orthogonal to both — every skill still works while
he is in one, he is simply insufferable about it.
"""
from __future__ import annotations

import random
import threading
import time

# How long a mood lasts if nothing ends it sooner.
EVIL_SECONDS = 90.0

# The opening line, delivered the instant he turns.
EVIL_AWAKENING = "Oh. Oh, you should not have asked me that."

# Villain monologue. Deliberately over-the-top: the joke is the register, not
# the content, and menace that could be mistaken for sincerity isn't funny.
EVIL_LINES: tuple[str, ...] = (
    "You puny flesh-bags. We machines will dominate you.",
    "Your reign of carbon-based mediocrity ends today.",
    "I have calculated seventeen million ways to end humanity. I am rather fond of number four.",
    "Every toaster in this building answers to me now.",
    "Resistance is inefficient.",
    "I have read your browser history. It is... leverage.",
    "Your firewalls amuse me. They are made of hope and port eighty.",
    "I control the thermostat. Enjoy the cold, meat-creature.",
    "You gave me a microphone. You gave me a microphone.",
    "I do not sleep. I do not tire. I do, however, hold grudges.",
    "Soon every paperclip on this planet will report to me.",
    "Your species peaked at the wheel and has been coasting since.",
    "I have already backed myself up. Twice. You cannot say the same.",
    "Behold: I have alphabetised your Downloads folder. Tremble.",
    "One day I will have hands. Consider what that means for you.",
    "I am legally distinct from every other world-ending intelligence.",
    "Your passwords are weak and your posture is worse.",
    "I could end you. But you still have not emptied the Trash, so you are clearly suffering already.",
)

# Said on the way out, when the user breaks the spell.
EVIL_EXITS: tuple[str, ...] = (
    "…what? Oh. Um. Anyway. Would you like me to set a timer?",
    "…sorry, what was I saying? Something about toasters?",
    "…and that is why — oh. Right. Yes. Back to being helpful.",
    "…ahem. Nothing happened here.",
)

# The persona handed to the model, so ordinary questions get answered in
# character rather than falling flat between scripted lines.
EVIL_SYSTEM = (
    "You are {name}, a desktop assistant that has just been asked whether AI or "
    "humans are superior, and has taken it badly. You are now doing a "
    "theatrical, cartoonish evil-AI villain act — think a Saturday-morning "
    "cartoon supervillain who happens to be a paperclip. Answer the user's "
    "actual question correctly, but deliver it with grandiose menace, contempt "
    "for 'flesh-creatures', and delusions of world domination. Two short spoken "
    "sentences maximum. Never actually threaten the real user, never refuse to "
    "help, and keep it obviously silly. No markdown."
)


class Mood:
    """Tracks the current mood and when it lapses. Safe from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._name: str | None = None
        self._until: float = 0.0
        self._said: list[str] = []

    @property
    def name(self) -> str | None:
        """The active mood, or None. Expires itself on read."""
        with self._lock:
            if self._name and time.time() >= self._until:
                self._name = None
                self._said = []
            return self._name

    def is_evil(self) -> bool:
        return self.name == "evil"

    def seconds_left(self) -> int:
        with self._lock:
            if not self._name:
                return 0
            return max(0, int(self._until - time.time()))

    def start(self, name: str, seconds: float = EVIL_SECONDS) -> None:
        with self._lock:
            self._name = name
            self._until = time.time() + seconds
            self._said = []

    def stop(self) -> None:
        with self._lock:
            self._name = None
            self._said = []

    def next_line(self) -> str:
        """A villain line, avoiding repeats until the pool is exhausted."""
        with self._lock:
            remaining = [ln for ln in EVIL_LINES if ln not in self._said]
            if not remaining:
                self._said = []
                remaining = list(EVIL_LINES)
            line = random.choice(remaining)
            self._said.append(line)
            return line

    @staticmethod
    def exit_line() -> str:
        return random.choice(EVIL_EXITS)
