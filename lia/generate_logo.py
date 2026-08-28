# -*- coding: utf-8 -*-
"""Lia brand asset derivation.

The Lia logo is the classic glossy purple orb with the neon triangles mark
(lia.png, carried over from the app's previous brand). This script derives
the other two brand assets from that master artwork:

  lia_logo.png   128 px mark embedded by ui_kit.logo_data_uri()
  lia.ico        multi-size Windows app icon (16..256) for the exe resource,
                 window title bars, taskbar, installer and shortcuts

Run `python generate_logo.py` next to lia.py to regenerate both, in place.
Replace lia.png first if the artwork itself ever changes.
"""
import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))

# Full Windows size set (matches the legacy multi-size icon exactly).
ICO_SIZES = [16, 24, 32, 48, 64, 72, 96, 128, 256]


def main():
    src_path = os.path.join(HERE, "lia.png")
    src = Image.open(src_path).convert("RGBA")

    logo_path = os.path.join(HERE, "lia_logo.png")
    src.resize((128, 128), Image.LANCZOS).save(logo_path)
    print(f"Wrote: {logo_path}  (128 px, from lia.png)")

    ico_path = os.path.join(HERE, "lia.ico")
    src.save(ico_path, format="ICO", sizes=[(sz, sz) for sz in ICO_SIZES])
    print(f"Wrote: {ico_path}  ({len(ICO_SIZES)} sizes, from lia.png)")


if __name__ == "__main__":
    main()
