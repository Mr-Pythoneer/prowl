"""The desktop buddy — a small animated character that floats on screen.

A borderless, transparent, always-on-top panel containing one hand-drawn
character: a bent paperclip with a pair of expressive eyes. It is drawn with
Core Graphics rather than images, so every state is a real animation (springy
wire, blinking, a mouth that moves in time with speech) and the whole thing
stays a few kilobytes with no assets to ship.

States drive the animation:

    idle       gentle sway, occasional blink
    listening  leans in, ears open, a pulsing ring
    thinking   tips back, three bouncing dots
    talking    bobs on each syllable, mouth opens and closes
    sleeping   drooped, eyes closed, slow breathing

Public API::

    buddy = Buddy()             # create (does not show)
    buddy.show() / hide()
    buddy.set_state("talking")
    buddy.say("Opening Safari.")    # bubble text; auto-clears
"""
from __future__ import annotations

import math
import random
import re
import subprocess
import time

import objc
from AppKit import (
    NSApplication, NSBezierPath, NSColor, NSFont, NSFontAttributeName,
    NSAffineTransform, NSColorSpace, NSCompositingOperationSourceOver,
    NSGradient, NSGraphicsContext, NSImage,
    NSMutableParagraphStyle, NSParagraphStyleAttributeName,
    NSLineBreakByWordWrapping, NSStringDrawingUsesLineFragmentOrigin,
    NSForegroundColorAttributeName, NSMakePoint, NSMakeRect, NSPanel, NSScreen,
    NSString, NSTimer, NSView, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary, NSBackingStoreBuffered,
    NSWindowStyleMaskBorderless, NSNonactivatingPanelMask,
)
from Foundation import NSMakeSize, NSPoint, NSPointInRect, NSZeroRect

# Panel geometry. The character occupies the lower portion; the speech bubble
# grows upward into the space above it.
_W, _H = 190, 210
_CHAR_W, _CHAR_H = 60, 84           # character's drawing box

# "Mini" mode: just the character, half size, no speech bubble — for when the
# full buddy is more presence than you want on a small screen.
_MINI_SCALE = 0.55
_MINI_W, _MINI_H = 74, 82
_FPS = 30.0

# Animation rate, in frames per second, per state and power source.
#
# Throttling only the *redraw* turned out not to help: measured in the running
# app, an idle buddy still cost ~6% of a CPU core with redraws at 1fps, because
# the timer itself was firing 30 times a second and every tick crosses the
# Objective-C to Python bridge. So the timer's own interval is what changes
# here. Drawing is cheap by comparison (~0.4 ms/frame).
# On battery the rates drop further: the idle sway does not need to be smooth,
# and this is most of a CPU core over a day. Plugged in there is nothing to
# save, so it runs nicer. Which table applies is decided by _on_ac_power(),
# rechecked every _POWER_POLL_SECONDS — see BuddyView.tick_.
_FPS_BATTERY = {
    "talking": 24.0,     # the mouth moves with speech, so keep it smooth
    "listening": 15.0,   # pulsing rings
    "thinking": 15.0,    # bouncing dots
    "idle": 1.0,         # static pose; this tick only schedules blinks
    "sleeping": 0.5,
}
_FPS_PLUGGED = {
    "talking": 30.0,
    "listening": 30.0,
    "thinking": 30.0,
    "idle": 30.0,        # fully animated — there is no battery to protect
    "sleeping": 12.0,    # slow breathing
}

# While a blink is in progress the rate jumps to this, whatever the state, so
# the eyes close smoothly instead of snapping shut.
_BLINK_FPS = 20.0

# States that hold a fixed pose *on battery*, so a tick with no blink can skip
# the redraw entirely. On wall power these animate normally.
_STATIC_STATES = ("idle", "sleeping")

# How often to re-ask the OS about the power source, in seconds.
_POWER_POLL_SECONDS = 20.0


def _on_ac_power() -> bool:
    """True when the Mac is running on wall power. Never raises."""
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=5).stdout
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return True          # unknown: prefer the nicer animation
    return "AC Power" in out or "AC attached" in out

STATES = ("idle", "listening", "thinking", "talking", "sleeping")


# --- tricks -----------------------------------------------------------------
# Short one-off animations. Each runs for its duration, overriding the state's
# own pose, then hands control back. Durations are tuned so a trick reads as
# deliberate rather than twitchy; anything under ~0.6s just looks like a glitch.
TRICKS: dict[str, float] = {
    "backflip": 1.15,
    "spin": 1.0,
    "jump": 0.7,
    "dance": 2.4,
    "wave": 1.3,
    "nod": 0.8,
    "shake": 0.8,
    "shrug": 1.2,
    "stretch": 1.6,
    "cheer": 1.2,
    "tumble": 1.6,
    "wobble": 0.9,
}

# Phrases that ask for one, matched whole. Kept here next to the animations so
# adding a trick means touching one file.
TRICK_PHRASES: tuple[tuple[str, str], ...] = (
    ("backflip", r"(?:do|make|perform)?\s*(?:a|an)?\s*back\s*-?\s*flip"),
    ("backflip", r"flip (?:out|over)"),
    ("spin", r"(?:do|make)?\s*(?:a|an)?\s*(?:360|three sixty|spin(?: around)?|twirl)"),
    ("jump", r"(?:jump|hop|bounce)(?: up)?"),
    ("dance", r"(?:dance|boogie|bust a move|get down)"),
    ("wave", r"(?:wave|say hi|say hello|greet me)"),
    ("nod", r"(?:nod|say yes)"),
    ("shake", r"(?:shake your head|say no)"),
    ("shrug", r"(?:shrug|i dunno|dunno)"),
    ("stretch", r"(?:stretch|wake up your body|limber up)"),
    ("cheer", r"(?:cheer|celebrate|yay|hooray|nice one)"),
    # "roll" is often transcribed as "row", and "do a" as "do as" — matching
    # the mishearing costs nothing and saves the trick from failing silently.
    ("tumble", r"(?:barrel|barell|barrell)\s*-?\s*(?:roll|role|row)"),
    ("tumble", r"(?:tumble|roll over|fall over|somersault)"),
    ("wobble", r"(?:wobble|wiggle|shimmy)"),
)


def match_trick(utterance: str) -> str | None:
    """Return the trick *utterance* asks for, or None.

    Anchored whole-phrase matching: "jump" is a trick, but "jump to the next
    song" is a media command and must not be stolen.
    """
    text = (utterance or "").strip().strip(".!?,")
    if not text:
        return None
    for name, body in TRICK_PHRASES:
        if re.fullmatch(
                rf"\s*(?:bob[,\s]+)?(?:can you |could you |please )?"
                rf"(?:do as |do a |do an )?{body}\s*", text, re.I):
            return name
    return None


# How far he tips over when asleep, in degrees — lying on his side.
_SLEEP_ROT = -78.0


def _ease(p: float) -> float:
    """Ease-in-out on 0..1, so a spin starts and ends gently."""
    return p * p * (3.0 - 2.0 * p)


# --- palette -----------------------------------------------------------------
# Warm metal for the wire, near-black ink for the eyes. Deliberately not a
# gradient-heavy look: flat colours with one soft shadow read better at this
# size and stay legible on any wallpaper.
_WIRE = (0.62, 0.65, 0.70)
_WIRE_DARK = (0.42, 0.45, 0.51)
_INK = (0.20, 0.21, 0.25)          # softer than black; pure black reads cold
_BUBBLE = (1.0, 1.0, 1.0, 0.97)
_BUBBLE_EDGE = (0.0, 0.0, 0.0, 0.10)
_ACCENT = (0.24, 0.52, 0.96)        # the listening ring


def _color(rgb, alpha=1.0):
    if len(rgb) == 4:
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb)
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, alpha)


def _on_main(fn):
    """Run `fn` on the main thread, now if we are already there."""
    from Foundation import NSThread

    if NSThread.isMainThread():
        fn()
        return
    from PyObjCTools import AppHelper

    AppHelper.callAfter(fn)


class BuddyView(NSView):
    """Draws the character. All animation state lives here."""

    def initWithFrame_(self, frame):
        self = objc.super(BuddyView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._state = "idle"
        self._t = 0.0                 # seconds since the state began
        self._blink_at = 2.0          # next blink time
        self._blink = 0.0             # 0 open .. 1 shut
        self._mouth = 0.0             # 0 shut .. 1 open
        self._text = ""
        self._text_until = 0.0
        self._bubble_rect = NSMakeRect(0, 0, 0, 0)
        self._mini = False
        self._scale = 1.0
        self._interval = 1.0 / 10.0     # replaced by _retime() on show()
        self._was_blinking = False
        self._trick = None           # name of the running trick, or None
        self._trick_t = 0.0
        self._last_activity = time.time()
        self._sleep_after = 0.0      # seconds of idleness before napping
        self._power_elapsed = 0.0
        self._owner = None              # set by Buddy, so state can re-time
        self._on_ac = _on_ac_power()
        self._drag_origin = None
        self._on_click = None
        return self

    # -- state ----------------------------------------------------------------
    def playTrick_(self, name):
        """Start a one-off animation. Unknown names are ignored."""
        if name not in TRICKS:
            return
        self._trick = name
        self._trick_t = 0.0
        self._last_activity = time.time()
        self._retime()               # tricks always run at full frame rate
        self.setNeedsDisplay_(True)

    def noteActivity(self):
        """Reset the idle timer — he only nods off when actually left alone."""
        self._last_activity = time.time()

    def setState_(self, state):
        state = state if state in STATES else "idle"
        if state != self._state:
            if state != "sleeping":
                self._last_activity = time.time()
            self._state = state
            self._t = 0.0
            # _t drives the blink schedule; resetting it mid-blink would leave
            # the eyes stuck shut until the next cycle.
            self._blink = 0.0
            self._blink_at = random.uniform(1.5, 4.0)
            self._retime()
        self.setNeedsDisplay_(True)

    def state(self):
        return self._state

    @objc.python_method
    def _pose_is_fixed(self) -> bool:
        """True when the current pose won't change, so a redraw is pointless."""
        if self._trick is not None:
            return False
        return self._state in _STATIC_STATES and not self._on_ac

    @objc.python_method
    def desired_interval(self) -> float:
        """Seconds between frames for the current state and power source."""
        if self._trick is not None:
            return 1.0 / 30.0
        if self._blink > 0.01:
            return 1.0 / _BLINK_FPS
        table = _FPS_PLUGGED if self._on_ac else _FPS_BATTERY
        return 1.0 / max(0.25, table.get(self._state, 8.0))

    @objc.python_method
    def _retime(self) -> None:
        """Ask the owning Buddy to reschedule its timer at the new rate."""
        owner = self._owner
        if owner is not None:
            owner.retime()

    def setMini_(self, flag):
        """Shrink to just the character (no bubble), or restore full size."""
        self._mini = bool(flag)
        self._scale = _MINI_SCALE if self._mini else 1.0
        if self._mini:
            self._text = ""
        self.setNeedsDisplay_(True)

    def isMini(self):
        return self._mini

    def setText_(self, text):
        self._text = (text or "").strip()
        # Roughly reading speed, clamped: long answers shouldn't pin the bubble.
        self._text_until = time.time() + max(2.5, min(9.0, len(self._text) / 14.0))
        self.setNeedsDisplay_(True)

    def tick_(self, _timer):
        dt = self._interval
        self._t += dt

        if self._trick is not None:
            self._trick_t += dt
            if self._trick_t >= TRICKS.get(self._trick, 1.0):
                self._trick = None
                self._trick_t = 0.0
                self._retime()
            self.setNeedsDisplay_(True)

        # Blinking: a quick shut/open, then a new random delay. Sleeping eyes
        # stay closed, so skip it entirely.
        if self._state != "sleeping":
            if self._t >= self._blink_at:
                phase = self._t - self._blink_at
                if phase < 0.06:
                    self._blink = phase / 0.06
                elif phase < 0.14:
                    self._blink = 1.0 - (phase - 0.06) / 0.08
                else:
                    self._blink = 0.0
                    self._blink_at = self._t + random.uniform(2.0, 6.0)
        else:
            self._blink = 1.0

        # Mouth: only moves while talking, at a syllable-ish rate with enough
        # irregularity that it doesn't look like a metronome.
        if self._state == "talking":
            base = math.sin(self._t * 15.0) * 0.5 + 0.5
            jitter = math.sin(self._t * 6.3 + 1.7) * 0.25 + 0.75
            self._mouth = max(0.0, min(1.0, base * jitter))
        else:
            self._mouth = 0.0

        if self._text and time.time() > self._text_until:
            self._text = ""
            self._interval = 1.0 / 10.0     # replaced by _retime() on show()
        self._was_blinking = False
        self._trick = None           # name of the running trick, or None
        self._trick_t = 0.0
        self._last_activity = time.time()
        self._power_elapsed = 0.0
        self._owner = None              # set by Buddy, so state can re-time
        self._on_ac = _on_ac_power()          # the bubble vanishing must be drawn now

        # Re-check the power source now and then; plugging in should smooth the
        # animation out without restarting anything.
        self._power_elapsed += dt
        if self._power_elapsed >= _POWER_POLL_SECONDS:
            self._power_elapsed = 0.0
            was = self._on_ac
            self._on_ac = _on_ac_power()
            if was != self._on_ac:
                self._retime()

        # A static state with nothing happening needs no redraw at all: the
        # tick is then only here to schedule the next blink.
        blinking = self._blink > 0.01
        if blinking != self._was_blinking:
            self._was_blinking = blinking
            self._retime()          # burst for the blink, then back down
        # Left alone for long enough, he lies down for a nap. Any state change
        # or trick counts as activity, so this only fires when genuinely idle.
        if (self._state == "idle" and self._trick is None and not self._text
                and self._sleep_after > 0
                and time.time() - self._last_activity > self._sleep_after):
            self.setState_("sleeping")

        if not self._pose_is_fixed() or blinking or self._text:
            self.setNeedsDisplay_(True)

    # -- geometry -------------------------------------------------------------
    @objc.python_method
    def _pose(self):
        """Return (dx, dy, lean, squash, rot) for right now.

        A running trick takes over completely — it is a deliberate performance
        and shouldn't be muddied by the idle sway underneath it.
        """
        if self._trick is not None:
            return self._trick_pose()
        t = self._t
        if self._state == "idle":
            if not self._on_ac:
                # On battery, hold still. Animating a transparent always-on-top
                # window costs ~8% of a CPU core continuously — not a fair price
                # for a sway nobody is watching. He still blinks, and comes
                # fully alive the moment he has something to do.
                return (0.0, 0.0, 0.0, 1.0, 0.0)
            # Plugged in there is nothing to save, so he breathes: a slow sway
            # with a second, slower component so the motion never looks like a
            # loop, plus a gentle bob.
            return (math.sin(t * 0.9) * 3.4 + math.sin(t * 0.37) * 1.6,
                    math.sin(t * 1.7) * 1.8,
                    math.sin(t * 0.9) * 0.045,
                    1.0 + math.sin(t * 1.7) * 0.012, 0.0)
        if self._state == "listening":
            # Leans toward the user and holds still, so it reads as attentive.
            return (0.0, 2.0 + math.sin(t * 3.0) * 1.5, 0.16, 1.0, 0.0)
        if self._state == "thinking":
            return (math.sin(t * 0.8) * 2.0, math.sin(t * 1.6) * 1.5, -0.12, 1.0, 0.0)
        if self._state == "talking":
            bob = abs(math.sin(t * 7.5))
            return (math.sin(t * 3.1) * 2.0, bob * 5.0, math.sin(t * 3.1) * 0.05,
                    1.0 - bob * 0.05, 0.0)
        # sleeping — lying on his side, breathing slowly.
        if not self._on_ac:
            return (0.0, 0.0, 0.0, 1.0, _SLEEP_ROT)
        return (0.0, math.sin(t * 0.7) * 1.2, 0.0,
                1.0 + math.sin(t * 0.7) * 0.02, _SLEEP_ROT)

    # -- tricks ---------------------------------------------------------------
    @objc.python_method
    def _trick_pose(self):
        """Pose for the running trick, as (dx, dy, lean, squash, rot)."""
        name = self._trick
        dur = TRICKS.get(name, 1.0)
        # p runs 0..1 across the trick.
        p = max(0.0, min(1.0, self._trick_t / dur))
        t = self._trick_t

        if name == "backflip":
            # Crouch, launch, rotate a full turn at the top, land and settle.
            if p < 0.16:                       # anticipation
                k = p / 0.16
                return (0.0, -4.0 * k, 0.0, 1.0 + 0.10 * k, 0.0)
            if p < 0.82:
                k = (p - 0.16) / 0.66
                height = math.sin(k * math.pi) * 46.0
                return (0.0, height, 0.0, 0.97, -360.0 * k)
            k = (p - 0.82) / 0.18              # landing squash
            return (0.0, 0.0, 0.0, 1.0 + 0.14 * math.sin(k * math.pi), 0.0)

        if name == "spin":
            # Flat spin: squash horizontally through the turn so it reads as
            # rotating on the spot rather than tipping over.
            k = _ease(p)
            return (0.0, math.sin(p * math.pi) * 4.0, 0.0, 1.0, -360.0 * k)

        if name == "jump":
            height = math.sin(p * math.pi) * 34.0
            squash = 1.0 + (0.16 * (1.0 - math.sin(p * math.pi)) if p < 0.12 or p > 0.88 else 0.0)
            return (0.0, height, 0.0, squash, 0.0)

        if name == "dance":
            side = math.sin(t * 7.0) * 9.0
            return (side, abs(math.sin(t * 14.0)) * 5.0, math.sin(t * 7.0) * 0.16,
                    1.0, math.sin(t * 7.0) * 7.0)

        if name == "wave":
            # Tips side to side from the base, like an arm waving.
            return (math.sin(t * 9.0) * 3.0, 2.0, math.sin(t * 9.0) * 0.34, 1.0,
                    math.sin(t * 9.0) * 9.0)

        if name == "nod":
            return (0.0, -math.sin(t * 11.0) * 5.0, 0.0,
                    1.0 + abs(math.sin(t * 11.0)) * 0.05, 0.0)

        if name == "shake":
            return (math.sin(t * 13.0) * 6.0, 0.0, math.sin(t * 13.0) * 0.10,
                    1.0, math.sin(t * 13.0) * 5.0)

        if name == "shrug":
            lift = math.sin(p * math.pi) * 9.0
            return (0.0, lift, 0.0, 1.0 - 0.05 * math.sin(p * math.pi),
                    math.sin(p * math.pi * 2.0) * 6.0)

        if name == "stretch":
            k = math.sin(p * math.pi)
            return (0.0, 3.0 * k, -0.10 * k, 1.0 + 0.16 * k, -5.0 * k)

        if name == "cheer":
            hop = abs(math.sin(t * 9.0))
            return (0.0, hop * 16.0, 0.0, 1.0 - hop * 0.04,
                    math.sin(t * 9.0) * 12.0)

        if name == "tumble":
            # A barrel roll: travels sideways and back while turning over once.
            k = _ease(p)
            return (math.sin(p * math.pi * 2.0) * 30.0,
                    math.sin(p * math.pi) * 10.0,
                    0.0, 1.0, -360.0 * k)

        if name == "wobble":
            damp = 1.0 - p
            return (math.sin(t * 16.0) * 7.0 * damp, 0.0,
                    math.sin(t * 16.0) * 0.18 * damp, 1.0,
                    math.sin(t * 16.0) * 8.0 * damp)

        return (0.0, 0.0, 0.0, 1.0, 0.0)

    # -- drawing --------------------------------------------------------------
    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        _color((0, 0, 0), 0.0).set()
        NSBezierPath.fillRect_(rect)

        bounds = self.bounds()
        scale = self._scale
        char_w, char_h = _CHAR_W * scale, _CHAR_H * scale

        dx, dy, lean, squash, rot = self._pose()
        cx = bounds.size.width / 2.0 + dx * scale
        base_y = 14.0 * scale + dy * scale

        # Nothing is drawn behind the character. He carries his own contrast
        # (dark under-stroke, light core), which works on any wallpaper without
        # a backdrop — see _draw_clip.

        if self._state == "listening":
            self._draw_listening_ring(cx, base_y + char_h * 0.45, scale)

        # Contact shadow: drawn here, outside any rotation, because a shadow
        # stays on the ground while he flips. It shrinks as he rises, which is
        # most of what sells a jump as leaving the floor.
        lift = max(0.0, dy * scale)
        shrink = max(0.35, 1.0 - lift / (char_h * 0.9))
        sw = (char_w * 0.62) * shrink
        _color(_INK, 0.12 * shrink).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - sw, 14.0 * scale - 8 * scale, sw * 2, 11 * scale)).fill()

        # Asleep he lies on a pillow; the pillow is drawn first, under him.
        if self._state == "sleeping" and self._trick is None:
            self._draw_pillow(cx, base_y, char_w, char_h)

        if abs(rot) > 0.01:
            # Rotate about the character's middle so a flip turns on the spot
            # instead of swinging around its feet.
            NSGraphicsContext.saveGraphicsState()
            pivot_x, pivot_y = cx, base_y + char_h * 0.5
            tf = NSAffineTransform.transform()
            tf.translateXBy_yBy_(pivot_x, pivot_y)
            tf.rotateByDegrees_(rot)
            tf.translateXBy_yBy_(-pivot_x, -pivot_y)
            tf.concat()
            self._draw_clip(cx, base_y, lean, squash, char_w, char_h)
            NSGraphicsContext.restoreGraphicsState()
        else:
            self._draw_clip(cx, base_y, lean, squash, char_w, char_h)

        if self._state == "sleeping" and self._trick is None:
            self._draw_zzz(cx + char_w * 0.55, base_y + char_h * 0.75, scale)
        if self._state == "thinking":
            self._draw_thought_dots(cx + 16 * scale, base_y + char_h + 4, scale)
        # Mini mode is deliberately silent: the bubble is the bulky part.
        if self._text and not self._mini:
            self._draw_bubble(self._text, base_y + char_h + 14)

    # -- the character --------------------------------------------------------
    @objc.python_method
    def _draw_clip(self, cx, base_y, lean, squash, char_w, char_h):
        """A paperclip: one continuous wire folded into three legs.

        Proportions matter more than cleverness here — a clip only reads as a
        clip when it is clearly taller than it is wide, the wire is thin, and
        the inner fold sits off-centre. Drawn as a polyline (with the bends
        sampled as short segments) so the whole body can be sheared by `lean`.
        """
        h = char_h * squash
        scale = char_h / _CHAR_H
        a = char_w * 0.24             # outer half-width — narrow reads as wire
        b = a * 0.38                  # inner leg, offset left of centre
        bot = base_y
        top = bot + h
        r_top = a                     # top fold spans the full width
        r_bot = (a + b) / 2.0         # bottom fold is tighter

        pts = []

        def add(x, y):
            frac = max(0.0, min(1.0, (y - bot) / max(1.0, h)))
            pts.append(NSMakePoint(cx + x + lean * frac * 30.0 * scale, y))

        def fold(cx_local, cy_local, radius, a0, a1, steps=20):
            for i in range(steps + 1):
                ang = math.radians(a0 + (a1 - a0) * (i / steps))
                add(cx_local + math.cos(ang) * radius,
                    cy_local + math.sin(ang) * radius)

        # Left outer leg, bottom -> up.
        add(-a, bot + h * 0.26)
        add(-a, top - r_top)
        # Fold over the top, left -> right.
        fold(0.0, top - r_top, r_top, 180, 0)
        # Right outer leg, down (stops short of the bottom).
        add(a, bot + r_bot * 0.85)
        # Fold under the bottom, right -> inner left.
        fold((a - b) / 2.0, bot + r_bot * 0.85, r_bot, 0, -180)
        # Inner leg, back up — ends high, which is the clip's signature.
        add(-b, top - r_top * 2.25)

        wire = NSBezierPath.bezierPath()
        wire.setLineCapStyle_(1)      # round
        wire.setLineJoinStyle_(1)
        wire.moveToPoint_(pts[0])
        for pt in pts[1:]:
            wire.lineToPoint_(pt)


        # Dark under-stroke, light core. That pairing is what makes him legible
        # on any wallpaper — the light core reads against a dark desktop, the
        # dark edge against a bright one. A glow behind him used to do this job
        # and was removed: a soft ellipse cannot fit a panel this shape without
        # either being clipped by an edge or reading as a grey oval.
        wire.setLineWidth_(7.2 * scale)
        _color(_WIRE_DARK).set()
        wire.stroke()
        wire.setLineWidth_(4.2 * scale)
        _color(_WIRE).set()
        wire.stroke()

        self._draw_face(cx, bot, h, lean, scale)

    @objc.python_method
    def _draw_face(self, cx, bot, h, lean, scale=1.0):
        """Eyes and mouth, riding near the top of the wire."""
        eye_y = bot + h * 0.80
        frac = (eye_y - bot) / max(1.0, h)
        ex = cx + lean * frac * 34.0 * scale
        gap = 7.6 * scale
        r = 5.6 * scale

        for side in (-1, 1):
            x = ex + side * gap
            # Sclera
            _color((1, 1, 1)).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(x - r, eye_y - r, r * 2, r * 2)).fill()
            _color(_INK, 0.18).set()
            ring = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(x - r, eye_y - r, r * 2, r * 2))
            ring.setLineWidth_(1.2 * scale)
            ring.stroke()

            # Pupil, offset slightly toward the lean so it looks where it leans.
            # A large pupil filling most of the eye is the whole difference
            # between "friendly" and "staring". Small pupils read as alarm.
            pr = r * 0.74
            px = x + lean * 10.0 * scale
            py = eye_y - 0.5 * scale
            _color(_INK).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(px - pr, py - pr, pr * 2, pr * 2)).fill()
            # Catch-light
            _color((1, 1, 1), 0.95).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(px - pr * 0.45, py + pr * 0.15, pr * 0.7, pr * 0.7)).fill()

            # Eyelid closes over the top for a blink.
            if self._blink > 0.01:
                _color(_WIRE).set()
                lid = NSMakeRect(x - r - scale, eye_y + r - (2 * r + 2 * scale) * self._blink,
                                 r * 2 + 2 * scale, (2 * r + 2 * scale) * self._blink)
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    lid, r, r * 0.6).fill()

        # Cheeks: a hint of warmth. Tiny, but it stops the face reading as cold.
        for side in (-1, 1):
            _color((0.94, 0.60, 0.58), 0.30).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(ex + side * (gap + r * 0.8) - 3.0 * scale,
                           eye_y - r - 3.0 * scale, 6.0 * scale, 4.0 * scale)).fill()

        # Mouth: a curve at rest, an oval while speaking.
        my = eye_y - 10.0 * scale
        if self._mouth > 0.05:
            mh = (2.4 + self._mouth * 6.5) * scale
            mw = (8.0 + self._mouth * 2.5) * scale
            _color(_INK).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(ex - mw / 2, my - mh / 2, mw, mh)).fill()
        else:
            path = NSBezierPath.bezierPath()
            path.setLineWidth_(2.4 * scale)
            path.setLineCapStyle_(1)
            # A real upward curve, not a flat line — the flat mouth was most of
            # why it looked unsettling.
            smile = (3.4 if self._state in ("idle", "listening", "talking") else 1.2) * scale
            path.moveToPoint_(NSMakePoint(ex - 5.5 * scale, my + smile * 0.45))
            path.curveToPoint_controlPoint1_controlPoint2_(
                NSMakePoint(ex + 5.5 * scale, my + smile * 0.45),
                NSMakePoint(ex - 2.0 * scale, my - smile),
                NSMakePoint(ex + 2.0 * scale, my - smile))
            _color(_INK, 0.8).set()
            path.stroke()

    @objc.python_method
    def _draw_pillow(self, cx, base_y, char_w, char_h):
        """A small pillow, placed under his head once he has tipped over.

        He rotates about his middle, so his head swings out to one side; a
        pillow drawn at the centre ends up under his waist. Rotating the head's
        offset by the same angle puts it where a pillow actually belongs.
        """
        angle = math.radians(_SLEEP_ROT)
        head_offset = char_h * 0.32           # eyes sit this far above centre
        head_dx = -head_offset * math.sin(angle)
        w = char_h * 0.52
        h = char_w * 0.40
        x = cx + head_dx - w * 0.5
        y = base_y + char_h * 0.07
        rect = NSMakeRect(x, y, w, h)
        _color(_INK, 0.10).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x, y - 1.5, w, h), h * 0.5, h * 0.5).fill()
        _color((0.90, 0.91, 0.94), 0.97).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, h * 0.5, h * 0.5).fill()
        _color(_INK, 0.10).set()
        edge = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, h * 0.5, h * 0.5)
        edge.setLineWidth_(1.0)
        edge.stroke()
        # A seam, so it reads as a pillow rather than a pill.
        seam = NSBezierPath.bezierPath()
        seam.setLineWidth_(1.0)
        seam.moveToPoint_(NSMakePoint(x + w * 0.5, y + h * 0.16))
        seam.lineToPoint_(NSMakePoint(x + w * 0.5, y + h * 0.84))
        _color(_INK, 0.07).set()
        seam.stroke()

    @objc.python_method
    def _draw_zzz(self, x, y, scale=1.0):
        """Three Zs drifting up, each fading as it rises."""
        for i in range(3):
            phase = ((self._t * 0.42) + i / 3.0) % 1.0
            size = (8.0 + i * 2.0) * scale
            alpha = math.sin(phase * math.pi) * 0.55
            if alpha <= 0.01:
                continue
            font = NSFont.systemFontOfSize_(size)
            attrs = {NSFontAttributeName: font,
                     NSForegroundColorAttributeName: _color(_INK, alpha)}
            ns = NSString.stringWithString_("z")
            ns.drawAtPoint_withAttributes_(
                NSMakePoint(x + phase * 12.0 * scale,
                            y + phase * 26.0 * scale), attrs)

    @objc.python_method
    def _draw_listening_ring(self, cx, cy, scale=1.0):
        """A pulse that expands and fades — the visual 'I'm hearing you'."""
        for i in range(3):
            phase = (self._t * 1.1 + i / 3.0) % 1.0
            radius = (42 + phase * 34) * scale
            alpha = (1.0 - phase) * 0.30
            _color(_ACCENT, alpha).set()
            ring = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - radius, cy - radius, radius * 2, radius * 2))
            ring.setLineWidth_(2.0 * scale)
            ring.stroke()

    @objc.python_method
    def _draw_thought_dots(self, x, y, scale=1.0):
        for i in range(3):
            bounce = abs(math.sin(self._t * 3.2 - i * 0.6)) * 5.0 * scale
            r = (3.0 + i * 0.8) * scale
            _color(_INK, 0.30 + i * 0.14).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(x + i * 12 * scale, y + bounce, r * 2, r * 2)).fill()

    @objc.python_method
    def _draw_bubble(self, text, y):
        """A rounded speech bubble above the character, sized to fit its text.

        The height is measured rather than estimated — guessing the line count
        from the single-line width was what let long answers spill out of the
        bubble and off the panel. Anything still too tall for the panel is
        truncated, because a bubble that runs off screen shows nothing useful.
        """
        font = NSFont.systemFontOfSize_(11.5)
        para = NSMutableParagraphStyle.alloc().init()
        para.setLineBreakMode_(NSLineBreakByWordWrapping)
        attrs = {NSFontAttributeName: font,
                 NSForegroundColorAttributeName: _color(_INK),
                 NSParagraphStyleAttributeName: para}

        pad_x, pad_y = 10.0, 7.0
        max_w = _W - 24
        text_w = max_w - pad_x * 2
        # Room between the character's head and the top of the panel.
        max_box_h = max(30.0, _H - y - 6)
        max_text_h = max_box_h - pad_y * 2

        ns, size = self._fit_text(text, attrs, text_w, max_text_h)
        box_w = min(max_w, max(70.0, size.width + pad_x * 2))
        box_h = size.height + pad_y * 2
        bx = (_W - box_w) / 2.0
        by = min(y, _H - box_h - 4)

        rect = NSMakeRect(bx, by, box_w, box_h)
        self._bubble_rect = rect

        _color(_INK, 0.10).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(bx, by - 1.5, box_w, box_h), 11, 11).fill()
        bubble = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, 11, 11)
        _color(_BUBBLE).set()
        bubble.fill()
        _color(_BUBBLE_EDGE).set()
        bubble.setLineWidth_(1.0)
        bubble.stroke()

        # Tail pointing down at the character.
        tail = NSBezierPath.bezierPath()
        tail.moveToPoint_(NSMakePoint(_W / 2 - 6, by + 1))
        tail.lineToPoint_(NSMakePoint(_W / 2, by - 7))
        tail.lineToPoint_(NSMakePoint(_W / 2 + 6, by + 1))
        _color(_BUBBLE).set()
        tail.fill()

        ns.drawWithRect_options_attributes_(
            NSMakeRect(bx + pad_x, by + pad_y, text_w, size.height),
            NSStringDrawingUsesLineFragmentOrigin, attrs)

    @objc.python_method
    def _fit_text(self, text, attrs, width, max_height):
        """Return (attributed-ready string, size) trimmed to fit `max_height`."""
        def measure(candidate):
            ns = NSString.stringWithString_(candidate)
            rect = ns.boundingRectWithSize_options_attributes_(
                NSMakeSize(width, 10000.0),
                NSStringDrawingUsesLineFragmentOrigin, attrs)
            return ns, rect.size

        ns, size = measure(text)
        if size.height <= max_height:
            return ns, size
        # Too tall: binary-search the longest prefix that fits, then ellipsize.
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            _, trial = measure(text[:mid].rstrip() + "…")
            if trial.height <= max_height:
                lo = mid
            else:
                hi = mid - 1
        return measure(text[:lo].rstrip() + "…")

    # -- interaction ----------------------------------------------------------
    @objc.python_method
    def _char_rect(self):
        """Screen-ish rect the character actually occupies, plus a small margin.

        The panel is much larger than the character (it has to leave room for
        the speech bubble), so without this the whole empty area swallows
        clicks meant for the windows behind it.
        """
        scale = self._scale
        a = _CHAR_W * scale * 0.24
        pad = 8.0 * scale
        bounds = self.bounds()
        base_y = 14.0 * scale
        return NSMakeRect(bounds.size.width / 2.0 - a - pad, base_y - pad,
                          (a + pad) * 2, _CHAR_H * scale + pad * 2)

    def hitTest_(self, point):
        # `point` arrives in the superview's coordinates.
        sup = self.superview()
        local = self.convertPoint_fromView_(point, sup) if sup else point
        if NSPointInRect(local, self._char_rect()):
            return self
        if self._text and NSPointInRect(local, self._bubble_rect):
            return self
        return None                      # click falls through to what's behind

    def mouseDown_(self, event):
        self._drag_origin = event.locationInWindow()
        self._down_at = time.time()

    def mouseDragged_(self, event):
        if self._drag_origin is None:
            return
        win = self.window()
        origin = win.frame().origin
        here = event.locationInWindow()
        win.setFrameOrigin_(NSPoint(
            origin.x + here.x - self._drag_origin.x,
            origin.y + here.y - self._drag_origin.y))

    def mouseUp_(self, event):
        # A click (not a drag) starts a voice turn — tap the buddy to talk.
        if self._drag_origin is not None and time.time() - self._down_at < 0.35:
            moved = abs(event.locationInWindow().x - self._drag_origin.x) + \
                abs(event.locationInWindow().y - self._drag_origin.y)
            if moved < 6 and callable(self._on_click):
                self._on_click()
        self._drag_origin = None


class Buddy:
    """The floating panel, plus the small API the rest of Prowl calls."""

    def __init__(self, on_click=None):
        NSApplication.sharedApplication()
        screen = NSScreen.mainScreen().visibleFrame()
        # Bottom-right by default, clear of the Dock.
        x = screen.origin.x + screen.size.width - _W - 24
        y = screen.origin.y + 24

        style = NSWindowStyleMaskBorderless | NSNonactivatingPanelMask
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, _W, _H), style, NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setLevel_(3)                      # floating, above normal windows
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces |
            NSWindowCollectionBehaviorStationary)
        panel.setIgnoresMouseEvents_(False)
        panel.setMovableByWindowBackground_(False)

        view = BuddyView.alloc().initWithFrame_(NSMakeRect(0, 0, _W, _H))
        view._on_click = on_click
        panel.setContentView_(view)

        self.panel = panel
        self.view = view
        self._timer = None
        self._interval = 0.0
        view._owner = self

    # -- lifecycle ------------------------------------------------------------
    def show(self):
        self.panel.orderFrontRegardless()
        if self._timer is None:
            self.retime()

    def retime(self) -> None:
        """(Re)install the animation timer at the rate the current state wants.

        The timer's own frequency is the expensive part — not the drawing — so
        this is what actually saves the battery.
        """
        interval = self.view.desired_interval()
        if self._timer is not None:
            if abs(self._interval - interval) < 1e-6:
                return
            self._timer.invalidate()
        self._interval = interval
        self.view._interval = interval
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, self.view, "tick:", None, True)

    def hide(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        self.panel.orderOut_(None)

    def is_visible(self) -> bool:
        return bool(self.panel.isVisible())

    # -- api ------------------------------------------------------------------
    # Prowl does its thinking on a worker thread, but AppKit may only be touched
    # from the main thread, so every public call hops back onto it.
    def set_state(self, state: str):
        _on_main(lambda: self.view.setState_(state))

    def say(self, text: str):
        """Show *text* in the bubble and start talking. Empty text clears it."""
        def _do():
            self.view.setText_(text or "")
            if (text or "").strip():
                self.view.setState_("talking")
        _on_main(_do)

    def set_mini(self, mini: bool):
        """Switch between the full buddy and the compact character-only one.

        The panel shrinks with it, staying pinned to its bottom-right corner so
        the character does not appear to jump across the screen.
        """
        def _do():
            frame = self.panel.frame()
            w, h = (_MINI_W, _MINI_H) if mini else (_W, _H)
            right = frame.origin.x + frame.size.width
            self.panel.setFrame_display_(
                NSMakeRect(right - w, frame.origin.y, w, h), True)
            self.view.setMini_(mini)
        _on_main(_do)

    def is_mini(self) -> bool:
        return bool(self.view.isMini())

    def toggle_mini(self):
        self.set_mini(not self.is_mini())

    def play_trick(self, name: str):
        """Run a one-off animation (see TRICKS). Safe from any thread."""
        _on_main(lambda: self.view.playTrick_(name))

    def note_activity(self):
        """Reset the nap timer — he only sleeps when genuinely left alone."""
        _on_main(self.view.noteActivity)

    def set_sleep_after(self, seconds: float):
        """Idle seconds before he lies down. 0 disables napping."""
        _on_main(lambda: setattr(self.view, "_sleep_after", max(0.0, seconds)))

    def show_threadsafe(self):
        _on_main(self.show)

    def hide_threadsafe(self):
        _on_main(self.hide)


def _demo():
    """Cycle through every state so the animation can be eyeballed."""
    from PyObjCTools import AppHelper

    app = NSApplication.sharedApplication()
    buddy = Buddy(on_click=lambda: print("clicked"))
    buddy.show()

    order = ["idle", "listening", "thinking", "talking", "sleeping"]
    state = {"i": 0}

    def cycle():
        s = order[state["i"] % len(order)]
        state["i"] += 1
        buddy.set_state(s)
        if s == "talking":
            buddy.say("Opening Safari for you.")
        print("state:", s)
        AppHelper.callLater(3.0, cycle)

    AppHelper.callLater(0.5, cycle)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    _demo()
