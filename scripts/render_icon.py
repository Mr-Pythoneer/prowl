#!/usr/bin/env python3
"""Render the character to a PNG — used to build the app icon.

Draws the same BuddyView the desktop buddy uses, so the icon is literally the
character rather than a separate asset that can drift out of sync.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from AppKit import (  # noqa: E402
    NSApplication, NSBitmapImageFileTypePNG, NSBitmapImageRep, NSColor,
    NSGraphicsContext, NSMakeRect,
)

from prowl.ui.buddy import BuddyView, _CHAR_H, _W, _H  # noqa: E402


def render(path: Path, size: int = 512) -> None:
    NSApplication.sharedApplication()

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)

    NSColor.clearColor().set()
    NSMakeRect(0, 0, size, size)

    view = BuddyView.alloc().initWithFrame_(NSMakeRect(0, 0, _W, _H))
    view.setState_("idle")
    view._t = 0.6          # a pose with the sway near neutral
    view._blink = 0.0

    # Scale and centre the character in a square canvas, with a little padding
    # so macOS's rounded-icon treatment doesn't clip the wire.
    from AppKit import NSAffineTransform

    # The character sits at y=18..18+_CHAR_H inside the view. Scale it to fill
    # most of the canvas, then centre that band exactly — computing the offset
    # rather than eyeballing it keeps the head from clipping at any size.
    char_base_y = 18.0
    scale = (size * 0.60) / _CHAR_H
    drawn_h = _CHAR_H * scale
    tf = NSAffineTransform.transform()
    tf.translateXBy_yBy_(
        size / 2.0 - (_W / 2.0) * scale,
        (size - drawn_h) / 2.0 - char_base_y * scale,
    )
    tf.scaleBy_(scale)
    tf.concat()
    view.drawRect_(NSMakeRect(0, 0, _W, _H))

    NSGraphicsContext.restoreGraphicsState()

    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    path.parent.mkdir(parents=True, exist_ok=True)
    data.writeToFile_atomically_(str(path), True)


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "icon.png")
    px = int(sys.argv[2]) if len(sys.argv) > 2 else 512
    render(out, px)
    print(out)
