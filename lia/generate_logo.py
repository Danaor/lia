# -*- coding: utf-8 -*-
"""Lia brand asset derivation (2026-09-17 brand refresh).

TWO master inputs (kept safe, never generated):

  lia.png        the FULL logo - the purple "L" mark ABOVE the "lia" wordmark.
                 The app's reference brand artwork.
  lia_mark.png   the MARK ONLY (the L symbol, no wordmark), transparent.

The small icon slots (the OS app icon at 16-48 px, the 28 px Settings-header
brand) can't read the wordmark, so BOTH derived assets come from the MARK:

  lia_logo.png   128 px mark embedded by ui_kit.logo_data_uri() (in-app brand)
  lia.ico        multi-size Windows app icon for the exe resource, window
                 title bars, taskbar, installer and desktop shortcut

The mark is padded to a square with a small margin so it isn't edge-to-edge in
a rounded icon frame. Run `python generate_logo.py` next to lia.py to
regenerate both in place; replace lia_mark.png / lia.png first if the artwork
itself changes.
"""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))

# The classic icon's size set plus the DPI-scaling sizes Windows requests at
# 125% (20 px) and 250% (40 px), so mainstream scaling factors get a native
# frame instead of a blurry neighbor-downsample.
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 72, 96, 128, 256]

# Fraction of the square side left as transparent margin around the mark.
MARGIN = 0.06


def _square_mark(src):
    """Pad the (possibly non-square) mark to a centered transparent square
    with a small margin, so every icon size crops cleanly."""
    w, h = src.size
    side = int(max(w, h) * (1 + 2 * MARGIN))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(src, ((side - w) // 2, (side - h) // 2), src)
    return canvas


def main():
    if len(sys.argv) > 1:
        sys.exit("generate_logo.py takes no arguments - it derives "
                 "lia_logo.png and lia.ico from lia_mark.png, in place.")
    mark_path = os.path.join(HERE, "lia_mark.png")
    if not os.path.exists(mark_path):
        sys.exit("lia_mark.png (the mark-only artwork) is missing next to this "
                 "script. It is an INPUT, not generated - restore it from git.")
    mark = _square_mark(Image.open(mark_path).convert("RGBA"))

    logo_path = os.path.join(HERE, "lia_logo.png")
    mark.resize((128, 128), Image.LANCZOS).save(logo_path)
    print(f"Wrote: {logo_path}  (128 px, from lia_mark.png)")

    ico_path = os.path.join(HERE, "lia.ico")
    mark.save(ico_path, format="ICO", sizes=[(sz, sz) for sz in ICO_SIZES])
    print(f"Wrote: {ico_path}  ({len(ICO_SIZES)} sizes, from lia_mark.png)")


if __name__ == "__main__":
    main()
