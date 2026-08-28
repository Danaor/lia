# -*- coding: utf-8 -*-
"""Lia brand asset derivation.

The Lia logo is the classic glossy purple orb with the neon triangles mark
(lia.png, carried over from the app's previous brand). lia.png is the MASTER
ARTWORK and an input here, not an output - keep it safe. This script derives
the other two brand assets from it:

  lia_logo.png   128 px mark embedded by ui_kit.logo_data_uri()
  lia.ico        multi-size Windows app icon for the exe resource, window
                 title bars, taskbar, installer and shortcuts

Run `python generate_logo.py` next to lia.py to regenerate both, in place.
Replace lia.png first if the artwork itself ever changes.
"""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))

# The classic icon's size set plus the DPI-scaling sizes Windows requests at
# 125% (20 px) and 250% (40 px), so mainstream scaling factors get a native
# frame instead of a blurry neighbor-downsample.
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 72, 96, 128, 256]


def main():
    if len(sys.argv) > 1:
        sys.exit("generate_logo.py takes no arguments - it derives "
                 "lia_logo.png and lia.ico from lia.png, in place. "
                 "(The old --variants design-iteration flag is gone with "
                 "the drawn-logo experiment.)")
    src_path = os.path.join(HERE, "lia.png")
    if not os.path.exists(src_path):
        sys.exit("lia.png (the master artwork) is missing next to this "
                 "script. It is an INPUT, not generated - restore it from "
                 "git before rerunning.")
    src = Image.open(src_path).convert("RGBA")

    logo_path = os.path.join(HERE, "lia_logo.png")
    src.resize((128, 128), Image.LANCZOS).save(logo_path)
    print(f"Wrote: {logo_path}  (128 px, from lia.png)")

    ico_path = os.path.join(HERE, "lia.ico")
    src.save(ico_path, format="ICO", sizes=[(sz, sz) for sz in ICO_SIZES])
    print(f"Wrote: {ico_path}  ({len(ICO_SIZES)} sizes, from lia.png)")


if __name__ == "__main__":
    main()
