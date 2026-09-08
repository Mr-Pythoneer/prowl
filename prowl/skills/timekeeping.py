"""Clock, date and timers.

The most common thing anyone says to a voice assistant, and the one thing this
one could not do. "What time is it" fell through to the model — which has no
clock and answered confidently anyway, inventing a time. A wrong answer given
with confidence is worse than no answer, so this reads the system clock.

Timers run on a daemon thread and announce themselves through the same
``Context.speak`` the rest of Prowl uses, so they are heard wherever the user
is: spoken aloud, and shown in the buddy's bubble.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from ..core.context import Context
from .base import Skill, SkillResult, SkillSpec, register

# Live timers, so "how long left" and "cancel the timer" can find them.
_TIMERS: list[dict[str, Any]] = []
_TIMERS_LOCK = threading.Lock()

_UNITS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
}

_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty-five": 45, "forty five": 45, "sixty": 60, "half": 0.5,
}


def parse_duration(text: str) -> int | None:
    """Seconds described by *text*, or None. Handles "5 minutes", "an hour"."""
    if not text:
        return None
    # "half an hour" would otherwise match "an hour" and come out as 60
    # minutes. Normalise the fractions people actually say, first.
    text = re.sub(r"\bhalf an? (hour|minute)\b", r"30 \1s" if False else
                  lambda m: "30 minutes" if m.group(1) == "hour" else "30 seconds",
                  text, flags=re.I)
    text = re.sub(r"\ba quarter of an hour\b", "15 minutes", text, flags=re.I)
    text = re.sub(r"\ban? hour and a half\b", "90 minutes", text, flags=re.I)

    total = 0
    found = False
    # "5 minutes", "90 s", and the spelled-out forms people actually say.
    pattern = re.compile(
        r"(\d+(?:\.\d+)?|" + "|".join(re.escape(w) for w in _WORD_NUMBERS) + r")"
        r"\s*(" + "|".join(re.escape(u) for u in _UNITS) + r")\b", re.I)
    for amount, unit in pattern.findall(text):
        low = amount.lower()
        value = _WORD_NUMBERS[low] if low in _WORD_NUMBERS else float(amount)
        total += value * _UNITS[unit.lower()]
        found = True
    if not found:
        return None
    return max(1, int(round(total)))


def active_timers() -> list[tuple[str, int]]:
    """Live timers as ``(label, seconds_remaining)`` — for the thought cloud.

    A countdown the user cannot see is one they have to keep asking about.
    """
    now = time.time()
    with _TIMERS_LOCK:
        live = [e for e in _TIMERS if not e["cancelled"] and e["ends_at"] > now]
    out = []
    for entry in sorted(live, key=lambda e: e["ends_at"]):
        label = entry["label"] or ("Alarm" if entry.get("is_alarm") else "Timer")
        out.append((label, int(entry["ends_at"] - now)))
    return out


def parse_clock_time(text: str) -> datetime | None:
    """The next occurrence of a clock time in *text* ("7am", "half past six").

    Returns a future ``datetime``: asking for 7am at 9pm means tomorrow's 7am,
    which is what anyone setting an alarm means.
    """
    if not text:
        return None
    low = text.strip().lower()

    hour = minute = None
    meridiem = None

    # "half past six", "quarter to seven" — said far more often than typed.
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
             "twelve": 12, "noon": 12, "midnight": 0}
    m = re.search(r"\b(half past|quarter past|quarter to)\s+(\w+)\b", low)
    if m and m.group(2) in words:
        hour = words[m.group(2)]
        if m.group(1) == "half past":
            minute = 30
        elif m.group(1) == "quarter past":
            minute = 15
        else:
            minute, hour = 45, (hour - 1) % 12 or 12
    else:
        # "7", "7:30", "07.30", each optionally followed by am/pm.
        m = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?|o'?clock)?",
                      low)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            suffix = (m.group(3) or "").replace(".", "").replace("'", "")
            if suffix.startswith("a"):
                meridiem = "am"
            elif suffix.startswith("p"):
                meridiem = "pm"
        elif "noon" in low:
            hour, minute = 12, 0
        elif "midnight" in low:
            hour, minute = 0, 0

    if hour is None:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    if meridiem is None and re.search(r"\bp\.?m\.?\b|\bevening\b|\btonight\b", low):
        meridiem = "pm"
    if meridiem is None and re.search(r"\ba\.?m\.?\b|\bmorning\b", low):
        meridiem = "am"

    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        if meridiem is None and hour < 12:
            # "set an alarm for 7" at 9am plausibly means 7pm today.
            evening = target + timedelta(hours=12)
            if evening > now:
                return evening
        target += timedelta(days=1)
    return target


def _spoken_duration(seconds: int) -> str:
    """"1 hour 5 minutes" — how a person would say it, not 3900."""
    parts = []
    for label, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count} {label}{'s' if count != 1 else ''}")
    return " ".join(parts) or "no time"


@register
class TimeDate(Skill):
    spec = SkillSpec(
        name="time_date",
        description="say the current time or today's date",
        examples=[
            "what time is it", "what's the time", "what's today's date",
            "what day is it", "what's the date",
        ],
        args={"what": "optional: 'time' or 'date' (default: time)"},
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        wanted = str(args.get("what") or args.get("kind") or "").strip().lower()
        now = datetime.now()
        # %-I / %-M drop the leading zero, so it reads as speech rather than
        # a digital clock ("nine twenty-five", not "09:05").
        clock = now.strftime("%-I:%M %p").replace("AM", "AM").replace("PM", "PM")
        date = now.strftime("%A, %B %-d")

        if wanted.startswith("date") or wanted in ("day", "today"):
            return SkillResult.say(f"It's {date}.", detail=now.isoformat())
        if wanted.startswith("both"):
            return SkillResult.say(f"It's {clock} on {date}.",
                                   detail=now.isoformat())
        return SkillResult.say(f"It's {clock}.", detail=now.isoformat())


@register
class Timer(Skill):
    spec = SkillSpec(
        name="timer",
        description="set a countdown timer that speaks when it finishes",
        examples=[
            "set a timer for 10 minutes",
            "remind me in 20 minutes to check the build",
            "timer for an hour and a half",
            "wake me at 7am",
            "set an alarm for half past six",
            "how long is left on my timer",
            "cancel my timer",
        ],
        args={
            "duration": "how long, e.g. '10 minutes' or '1 hour 30 minutes'",
            "at": "a clock time for an alarm, e.g. '7am' or '18:30'",
            "label": "optional: what the timer is for",
            "action": "optional: 'cancel' or 'status'",
        },
    )

    def run(self, args: dict[str, Any], ctx: Context) -> SkillResult:
        action = str(args.get("action") or "").strip().lower()
        if action in ("cancel", "stop", "clear"):
            return self._cancel()
        if action in ("status", "check", "remaining", "left"):
            return self._status()

        # An alarm is a timer that knows its own end time, so both land here.
        at_raw = str(args.get("at") or args.get("clock") or "").strip()
        alarm_at = parse_clock_time(at_raw) if at_raw else None
        if at_raw and alarm_at is None:
            return SkillResult.fail(f"I couldn't read {at_raw!r} as a time.")

        if alarm_at is not None:
            seconds = max(1, int((alarm_at - datetime.now()).total_seconds()))
            raw = at_raw
        else:
            raw = str(args.get("duration") or args.get("time") or
                      args.get("length") or "").strip()
            seconds = parse_duration(raw)
        if seconds is None:
            return SkillResult.fail("How long should I set it for?")
        # Alarms are allowed to run overnight; a plain timer is not.
        limit = 36 * 3600 if alarm_at is not None else 12 * 3600
        if seconds > limit:
            return SkillResult.fail("That's further ahead than I'll hold it for.")

        label = str(args.get("label") or args.get("for") or "").strip()
        if ctx.dry_run:
            return SkillResult.say(
                f"Would set a timer for {_spoken_duration(seconds)}.")

        entry: dict[str, Any] = {
            "ends_at": time.time() + seconds,
            "label": label,
            "is_alarm": alarm_at is not None,
            "cancelled": False,
        }
        thread = threading.Thread(
            target=self._wait, args=(entry, seconds, label, ctx),
            daemon=True, name="prowl-timer")
        entry["thread"] = thread
        with _TIMERS_LOCK:
            _TIMERS.append(entry)
        thread.start()

        ends = datetime.now() + timedelta(seconds=seconds)
        if alarm_at is not None:
            spoken = f"Alarm set for {alarm_at:%-I:%M %p}"
            # Say which day when it isn't today. Built as its own value: a
            # conditional inside a format spec applies the spec to whichever
            # branch wins, and "%A" is meaningless for the string "tomorrow".
            days_ahead = (alarm_at.date() - datetime.now().date()).days
            if days_ahead == 1:
                spoken += " tomorrow"
            elif days_ahead > 1:
                spoken += f" on {alarm_at:%A}"
        else:
            spoken = f"Timer set for {_spoken_duration(seconds)}"
        return SkillResult.say(
            spoken + "." + (f" For {label}." if label else ""),
            detail=f"ends at {ends:%-I:%M %p}")

    @staticmethod
    def _wait(entry: dict[str, Any], seconds: int, label: str,
              ctx: Context) -> None:
        """Sleep in slices so a cancellation is noticed promptly."""
        deadline = entry["ends_at"]
        while time.time() < deadline:
            if entry["cancelled"]:
                return
            time.sleep(min(0.5, max(0.05, deadline - time.time())))
        if entry["cancelled"]:
            return
        with _TIMERS_LOCK:
            if entry in _TIMERS:
                _TIMERS.remove(entry)
        try:
            if entry.get("is_alarm"):
                ctx.speak(f"It's {datetime.now():%-I:%M %p}"
                          + (f" — {label}" if label else "") + ".")
            else:
                ctx.speak(f"Time's up{f' — {label}' if label else ''}.")
        except Exception:  # noqa: BLE001 - a timer must not crash the app
            pass

    @staticmethod
    def _cancel() -> SkillResult:
        with _TIMERS_LOCK:
            live = list(_TIMERS)
            _TIMERS.clear()
        for entry in live:
            entry["cancelled"] = True
        if not live:
            return SkillResult.say("You don't have any timers running.")
        return SkillResult.say(
            f"Cancelled {len(live)} timer{'s' if len(live) != 1 else ''}.")

    @staticmethod
    def _status() -> SkillResult:
        now = time.time()
        with _TIMERS_LOCK:
            live = [e for e in _TIMERS if not e["cancelled"] and e["ends_at"] > now]
        if not live:
            return SkillResult.say("You don't have any timers running.")
        parts = []
        for entry in sorted(live, key=lambda e: e["ends_at"]):
            left = _spoken_duration(int(entry["ends_at"] - now))
            parts.append(f"{left} left" + (f" on {entry['label']}"
                                           if entry["label"] else ""))
        return SkillResult.say("; ".join(parts))
