# -*- coding: utf-8 -*-
"""Lia's orb as raster frames (PIL only) for the Tk surfaces.

The webview windows draw the orb as SVG (ui_kit.ORB_EGG / ORB_INNER). Tk has no
SVG, so the meeting card's "Lia is working" indicator draws the SAME geometry
here: one egg path at 0/120/240 degrees on a 76x76 box, each petal filled with
its own diagonal gradient (the ORB_DEFS colours), the whole group turning while
each petal breathes - the .orb.busy animation of the windows, as frames.

Pure PIL + math: no tkinter, no browser, no files. Frames are cached per
(size, count); converting them to Tk PhotoImages is the caller's job (Tk thread).
"""
from __future__ import annotations

import math

# ui_kit.ORB_EGG, parsed: a start point + 5 cubic Bezier segments (76x76 box)
_EGG_START = (37.0, 3.5)
_EGG_CUBICS = (
    ((48.5, 2.5), (57.0, 7.0), (56.5, 16.0)),
    ((56.0, 24.0), (47.0, 30.5), (41.5, 33.5)),
    ((39.5, 34.6), (37.5, 34.7), (35.5, 33.8)),
    ((30.0, 31.2), (20.0, 25.5), (19.8, 17.0)),
    ((19.6, 8.5), (26.5, 4.4), (37.0, 3.5)),
)
# (rotation, gradient start, gradient end) - the ORB_DEFS gradients A, C, B
_PETALS = (
    (0.0, (0xA3, 0x8A, 0xF3), (0x76, 0x56, 0xE3)),
    (120.0, (0x6A, 0xA6, 0xF4), (0x43, 0x80, 0xEA)),
    (240.0, (0x6A, 0x78, 0xEE), (0x4C, 0x58, 0xD8)),
)
_CENTER = (38.0, 38.0)
_SS = 4                      # supersampling factor (anti-aliased edges)
_CACHE = {}


def _egg_points(steps=24):
    pts = [_EGG_START]
    p0 = _EGG_START
    for c1, c2, p3 in _EGG_CUBICS:
        for i in range(1, steps + 1):
            t = i / steps
            mt = 1 - t
            x = (mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p3[0])
            y = (mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p3[1])
            pts.append((x, y))
        p0 = p3
    return pts


_EGG = _egg_points()
_EGG_BBOX = (min(p[0] for p in _EGG), min(p[1] for p in _EGG),
             max(p[0] for p in _EGG), max(p[1] for p in _EGG))


def _petal_layer(Image, ImageDraw, px, rot, c0, c1, scale, spin):
    """One petal as an RGBA image of px x px: the egg scaled about its own
    centre (breathing), turned by rot+spin about the orb centre, filled with a
    diagonal gradient across the egg's own box (SVG objectBoundingBox)."""
    k = px / 76.0
    ex = (_EGG_BBOX[0] + _EGG_BBOX[2]) / 2
    ey = (_EGG_BBOX[1] + _EGG_BBOX[3]) / 2
    a = math.radians(rot + spin)
    ca, sa = math.cos(a), math.sin(a)
    cx, cy = _CENTER
    pts = []
    for x, y in _EGG:
        x = ex + (x - ex) * scale
        y = ey + (y - ey) * scale
        dx, dy = x - cx, y - cy
        pts.append(((cx + dx * ca - dy * sa) * k, (cy + dx * sa + dy * ca) * k))
    mask = Image.new("L", (px, px), 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    # the gradient: along the egg box diagonal, turned with the petal (each
    # pixel is turned back into the egg's own frame, then projected)
    import numpy as np
    bx0, by0, bx1, by1 = _EGG_BBOX
    ux, uy = (bx1 - bx0), (by1 - by0)
    J, I = np.mgrid[0:px, 0:px].astype(np.float32)
    X, Y = I / k - cx, J / k - cy
    lx = cx + X * ca + Y * sa
    ly = cy - X * sa + Y * ca
    t = np.clip(((lx - bx0) * ux + (ly - by0) * uy) / (ux * ux + uy * uy), 0.0, 1.0)[..., None]
    c0a, c1a = np.array(c0, np.float32), np.array(c1, np.float32)
    rgb = (c0a + (c1a - c0a) * t).astype(np.uint8)
    g = Image.fromarray(rgb, "RGB").convert("RGBA")
    g.putalpha(mask)
    return g


def orb_frame(size, spin=0.0, breathe=(1.0, 1.0, 1.0), colors=None):
    """One orb image (PIL RGBA, size x size), turned by `spin` degrees, each
    petal scaled by its `breathe` factor. `colors` = three (start, end) RGB
    gradients to tint the petals (the tray states); None = Lia's own."""
    from PIL import Image, ImageDraw
    px = size * _SS
    out = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    petals = _PETALS if not colors else tuple(
        (rot, c[0], c[1]) for (rot, _a, _b), c in zip(_PETALS, colors))
    for (rot, c0, c1), sc in zip(petals, breathe):
        out.alpha_composite(_petal_layer(Image, ImageDraw, px, rot, c0, c1, sc, spin))
    return out.resize((size, size), Image.LANCZOS)


def busy_frames(size, count=36):
    """The .orb.busy loop as `count` frames: one full turn, each petal
    breathing (scale 1 -> .86 -> 1) three times, staggered by a third - the
    windows' lwSpin + lwBreathe, sampled. Cached per (size, count)."""
    key = (int(size), int(count))
    if key in _CACHE:
        return _CACHE[key]
    frames = []
    for f in range(count):
        u = f / count
        br = tuple(1.0 - 0.07 * (1 - math.cos(2 * math.pi * (3 * u + ph))) for ph in (0.0, 1 / 3, 2 / 3))
        frames.append(orb_frame(size, spin=360.0 * u, breathe=br))
    _CACHE[key] = frames
    return frames
