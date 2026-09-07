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
import time

import objc
from AppKit import (
    NSApplication, NSBezierPath, NSColor, NSFont, NSFontAttributeName,
    NSForegroundColorAttributeName, NSMakePoint, NSMakeRect, NSPanel, NSScreen,
    NSString, NSTimer, NSView, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary, NSBackingStoreBuffered,
    NSWindowStyleMaskBorderless, NSNonactivatingPanelMask,
)
from Foundation import NSMakeSize, NSPoint

# Panel geometry. The character occupies the lower portion; the speech bubble
# grows upward into the space above it.
_W, _H = 260, 300
_CHAR_W, _CHAR_H = 96, 132          # character's drawing box
_FPS = 30.0

STATES = ("idle", "listening", "thinking", "talking", "sleeping")

# --- palette -----------------------------------------------------------------
# Warm metal for the wire, near-black ink for the eyes. Deliberately not a
# gradient-heavy look: flat colours with one soft shadow read better at this
# size and stay legible on any wallpaper.
_WIRE = (0.62, 0.65, 0.70)
_WIRE_DARK = (0.42, 0.45, 0.51)
_INK = (0.11, 0.12, 0.14)
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
        self._drag_origin = None
        self._on_click = None
        return self

    # -- state ----------------------------------------------------------------
    def setState_(self, state):
        state = state if state in STATES else "idle"
        if state != self._state:
            self._state = state
            self._t = 0.0
            # _t drives the blink schedule; resetting it mid-blink would leave
            # the eyes stuck shut until the next cycle.
            self._blink = 0.0
            self._blink_at = random.uniform(1.5, 4.0)
        self.setNeedsDisplay_(True)

    def state(self):
        return self._state

    def setText_(self, text):
        self._text = (text or "").strip()
        # Roughly reading speed, clamped: long answers shouldn't pin the bubble.
        self._text_until = time.time() + max(2.5, min(9.0, len(self._text) / 14.0))
        self.setNeedsDisplay_(True)

    def tick_(self, _timer):
        dt = 1.0 / _FPS
        self._t += dt

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

        self.setNeedsDisplay_(True)

    # -- geometry -------------------------------------------------------------
    @objc.python_method
    def _pose(self):
        """Return (dx, dy, lean, squash) for the current state and time."""
        t = self._t
        if self._state == "idle":
            return (math.sin(t * 1.1) * 3.0, math.sin(t * 2.2) * 2.0,
                    math.sin(t * 1.1) * 0.04, 1.0)
        if self._state == "listening":
            # Leans toward the user and holds still, so it reads as attentive.
            return (0.0, 2.0 + math.sin(t * 3.0) * 1.5, 0.16, 1.0)
        if self._state == "thinking":
            return (math.sin(t * 0.8) * 2.0, math.sin(t * 1.6) * 1.5, -0.12, 1.0)
        if self._state == "talking":
            bob = abs(math.sin(t * 7.5))
            return (math.sin(t * 3.1) * 2.0, bob * 5.0, math.sin(t * 3.1) * 0.05,
                    1.0 - bob * 0.05)
        # sleeping — slow breathing
        return (0.0, math.sin(t * 0.9) * 2.0, -0.22, 1.0 + math.sin(t * 0.9) * 0.02)

    # -- drawing --------------------------------------------------------------
    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        _color((0, 0, 0), 0.0).set()
        NSBezierPath.fillRect_(rect)

        dx, dy, lean, squash = self._pose()
        cx = _W / 2.0 + dx
        base_y = 18.0 + dy

        if self._state == "listening":
            self._draw_listening_ring(cx, base_y + _CHAR_H * 0.45)
        self._draw_clip(cx, base_y, lean, squash)
        if self._state == "thinking":
            self._draw_thought_dots(cx + 16, base_y + _CHAR_H + 4)
        if self._text:
            self._draw_bubble(self._text, base_y + _CHAR_H + 14)

    # -- the character --------------------------------------------------------
    @objc.python_method
    def _draw_clip(self, cx, base_y, lean, squash):
        """A paperclip: one continuous wire folded into three legs.

        Proportions matter more than cleverness here — a clip only reads as a
        clip when it is clearly taller than it is wide, the wire is thin, and
        the inner fold sits off-centre. Drawn as a polyline (with the bends
        sampled as short segments) so the whole body can be sheared by `lean`.
        """
        h = _CHAR_H * squash
        a = _CHAR_W * 0.24            # outer half-width — narrow reads as wire
        b = a * 0.38                  # inner leg, offset left of centre
        bot = base_y
        top = bot + h
        r_top = a                     # top fold spans the full width
        r_bot = (a + b) / 2.0         # bottom fold is tighter

        pts = []

        def add(x, y):
            frac = max(0.0, min(1.0, (y - bot) / max(1.0, h)))
            pts.append(NSMakePoint(cx + x + lean * frac * 30.0, y))

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

        # Contact shadow on the desktop.
        _color(_INK, 0.12).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - a - 8, bot - 8, (a + 8) * 2, 11)).fill()

        # Dark under-stroke gives the wire a rounded, metallic edge.
        wire.setLineWidth_(7.0)
        _color(_WIRE_DARK).set()
        wire.stroke()
        wire.setLineWidth_(4.2)
        _color(_WIRE).set()
        wire.stroke()

        self._draw_face(cx, bot, h, lean)

    @objc.python_method
    def _draw_face(self, cx, bot, h, lean):
        """Eyes and mouth, riding near the top of the wire."""
        eye_y = bot + h * 0.815
        frac = (eye_y - bot) / max(1.0, h)
        ex = cx + lean * frac * 34.0
        gap = 9.0
        r = 6.4

        for side in (-1, 1):
            x = ex + side * gap
            # Sclera
            _color((1, 1, 1)).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(x - r, eye_y - r, r * 2, r * 2)).fill()
            _color(_INK, 0.18).set()
            ring = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(x - r, eye_y - r, r * 2, r * 2))
            ring.setLineWidth_(1.2)
            ring.stroke()

            # Pupil, offset slightly toward the lean so it looks where it leans.
            pr = 3.1
            px = x + lean * 10.0
            py = eye_y - 0.5
            _color(_INK).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(px - pr, py - pr, pr * 2, pr * 2)).fill()
            # Catch-light
            _color((1, 1, 1), 0.9).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(px - pr + 1.0, py + 0.4, 2.0, 2.0)).fill()

            # Eyelid closes over the top for a blink.
            if self._blink > 0.01:
                _color(_WIRE).set()
                lid = NSMakeRect(x - r - 1, eye_y + r - (2 * r + 2) * self._blink,
                                 r * 2 + 2, (2 * r + 2) * self._blink)
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    lid, r, r * 0.6).fill()

        # Mouth: a line at rest, an oval while speaking.
        my = eye_y - 12.5
        if self._mouth > 0.05:
            mh = 3.0 + self._mouth * 9.0
            mw = 12.0 + self._mouth * 3.0
            _color(_INK).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(ex - mw / 2, my - mh / 2, mw, mh)).fill()
        else:
            path = NSBezierPath.bezierPath()
            path.setLineWidth_(2.4)
            path.setLineCapStyle_(1)
            smile = 2.5 if self._state in ("idle", "listening") else 0.0
            path.moveToPoint_(NSMakePoint(ex - 6, my))
            path.curveToPoint_controlPoint1_controlPoint2_(
                NSMakePoint(ex + 6, my),
                NSMakePoint(ex - 2, my - smile),
                NSMakePoint(ex + 2, my - smile))
            _color(_INK, 0.75).set()
            path.stroke()

    @objc.python_method
    def _draw_listening_ring(self, cx, cy):
        """A pulse that expands and fades — the visual 'I'm hearing you'."""
        for i in range(3):
            phase = (self._t * 1.1 + i / 3.0) % 1.0
            radius = 42 + phase * 34
            alpha = (1.0 - phase) * 0.30
            _color(_ACCENT, alpha).set()
            ring = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - radius, cy - radius, radius * 2, radius * 2))
            ring.setLineWidth_(2.0)
            ring.stroke()

    @objc.python_method
    def _draw_thought_dots(self, x, y):
        for i in range(3):
            bounce = abs(math.sin(self._t * 3.2 - i * 0.6)) * 5.0
            r = 3.0 + i * 0.8
            _color(_INK, 0.30 + i * 0.14).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(x + i * 12, y + bounce, r * 2, r * 2)).fill()

    @objc.python_method
    def _draw_bubble(self, text, y):
        """A rounded speech bubble above the character, sized to the text."""
        font = NSFont.systemFontOfSize_(12.5)
        attrs = {NSFontAttributeName: font,
                 NSForegroundColorAttributeName: _color(_INK)}
        max_w = _W - 36
        ns = NSString.stringWithString_(text)
        size = ns.sizeWithAttributes_(attrs)
        # Wrap by estimating lines; NSString drawing handles the actual layout.
        lines = max(1, int(size.width / max_w) + 1)
        box_w = min(max_w, size.width + 20)
        box_h = size.height * lines + 16
        bx = (_W - box_w) / 2.0
        by = min(y, _H - box_h - 6)

        rect = NSMakeRect(bx, by, box_w, box_h)
        bubble = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, 12, 12)
        _color(_INK, 0.10).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(bx, by - 1.5, box_w, box_h), 12, 12).fill()
        _color(_BUBBLE).set()
        bubble.fill()
        _color(_BUBBLE_EDGE).set()
        bubble.setLineWidth_(1.0)
        bubble.stroke()

        # Tail pointing down at the character.
        tail = NSBezierPath.bezierPath()
        tail.moveToPoint_(NSMakePoint(_W / 2 - 7, by + 1))
        tail.lineToPoint_(NSMakePoint(_W / 2 + 1, by - 8))
        tail.lineToPoint_(NSMakePoint(_W / 2 + 8, by + 1))
        _color(_BUBBLE).set()
        tail.fill()

        ns.drawInRect_withAttributes_(
            NSMakeRect(bx + 10, by + 8, box_w - 20, box_h - 16), attrs)

    # -- interaction ----------------------------------------------------------
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

    # -- lifecycle ------------------------------------------------------------
    def show(self):
        self.panel.orderFrontRegardless()
        if self._timer is None:
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / _FPS, self.view, "tick:", None, True)

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
        def _do():
            self.view.setText_(text)
            self.view.setState_("talking")
        _on_main(_do)

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
