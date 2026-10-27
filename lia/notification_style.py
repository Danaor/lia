# -*- coding: utf-8 -*-
"""Lia - visual tokens for the meeting-notification card (redesign 2026-09-17).

One place for the light card's colours, spacing, typography and DPI conversion,
so `meeting_notifications.py` (the widget) and `notification_preview.py` (the
isolated demo) render identically. Pure data + tiny helpers; NO tkinter import
at module load, so it is cheap and testable without a display.

Design intent (plans/NOTIFICATION_REDESIGN_OPUS_4_8_2026-09-17.md section 4):
a small, quiet WHITE card that matches Lia's light UI - no purple gradient, no
window transparency over text, no emoji-as-icon. State is carried by a small
dot/icon + words, never by flooding the whole card with colour.

Units: every SIZE below is a LOGICAL pixel at 96 DPI. `Metrics(dpi_scale)`
converts them once - pixel metrics (padding, widths, geometry) are multiplied by
the monitor scale; fonts are given to Tk in POINTS so Tk's own `tk scaling`
(already set to 1.333*dpi on the root) sizes them, and are NOT pre-scaled here
(that would double-count DPI). Verified at 100/125/150/200% via the preview.
"""
from __future__ import annotations

# python-bidi: this Tk (8.6.15) does NATIVE bidi in Label/Message AND in
# tk.Button, so all their text must stay LOGICAL (converting double-reverses).
# The 2026-09-17 belief that Button lacks bidi was wrong: re-measured
# 2026-10-13 (a bare tk.Button + the card's own buttons in notification_preview
# --hebrew): get_display() printed the labels backwards. Kept importable for
# lia._to_visual_rtl callers.
try:
    from bidi.algorithm import get_display as _get_display
except Exception:
    try:
        from bidi import get_display as _get_display
    except Exception:
        _get_display = None


def has_rtl(text) -> bool:
    """True if the string carries Hebrew or Arabic letters."""
    return any(("֐" <= c <= "׿") or ("؀" <= c <= "ۿ")
               for c in (text or ""))


_RLE, _PDF = "‫", "‬"


def rtl_text(text):
    """Text for a Tk Label / Button / canvas item, LOGICAL order. Tk reorders
    the letters of a Hebrew word natively but lays a LINE out left-to-right,
    so a mixed line came out in the wrong word order ("Zoom" on the right of
    "זוהתה פגישת Zoom"). Each line that carries Hebrew/Arabic is wrapped in a
    right-to-left embedding (RLE ... PDF): measured 2026-10-27 by template-
    matching the rendered words - plain and RLM-marked text were wrong, RLE
    right in every case. A line without RTL letters is left alone."""
    if not text or not has_rtl(text):
        return text or ""
    return "\n".join((_RLE + ln + _PDF) if has_rtl(ln) else ln
                     for ln in text.split("\n"))


def wrap_rtl(text, measure, width_px):
    """`text` broken into lines that fit `width_px` (`measure(s)` = its pixel
    width, e.g. a tkinter Font's .measure - this module stays Tk-free), each
    line embedded (rtl_text). Use INSTEAD of a Label's wraplength for RTL
    text: Tk draws every wrapped line as its own run, so one embedding around
    the whole string would only cover the first line."""
    if not text or not has_rtl(text):
        return text or ""
    lines = []
    for para in text.split("\n"):
        line = ""
        for w in para.split(" "):
            cand = (line + " " + w) if line else w
            if line and measure(cand) > width_px:
                lines.append(line)
                line = w
            else:
                line = cand
        lines.append(line)
    return rtl_text("\n".join(lines))


def button_text(text):
    """The text for a tk.Button label: logical order with each RTL line
    embedded (rtl_text) - Tk orders a button's words like a Label's. Kept as
    the one seam every Tk button label goes through."""
    return rtl_text(text)


# ---- Colours (light card) ------------------------------------------------
# The Lia Lite language (UI redesign 2026-10-13, the ui_kit tokens): warm
# neutral ink, a deep-ink primary, Lia's colour only for "Lia is working",
# red only for recording, failures in a calm amber (never an alarm).
# The surface is a soft warm off-white, not paper white (Naor 2026-10-27: a
# pure white card in the corner pulled the eye). Fully opaque on purpose - a
# Tk window can only fade as a whole, text and buttons included.
BG = "#F6F5F1"            # card surface, fully opaque
BORDER = "#E3E2DC"        # 1px hairline border
INK = "#17171E"           # primary text (title, meeting name)
SUB = "#6D6F78"           # secondary / meta text
FAINT = "#A3A5AC"         # tertiary (brand name, disabled)

PRIMARY_BG = "#272A4A"    # primary action button (deep ink)
PRIMARY_FG = "#FFFFFF"
PRIMARY_HOVER = "#33375E"
SECONDARY_BG = "#F6F5F1"  # secondary action: the surface, bordered
SECONDARY_FG = "#3A3C46"
SECONDARY_BORDER = "#D9D8D1"
SECONDARY_HOVER = "#EEEDE8"

# State accents - used only on the small dot/icon, never as a card wash.
REC = "#D63543"           # recording (red dot) - red is for recording only
BUSY = "#6A5BE2"          # Lia is working (her colour; the orb when available)
OK = "#23845A"            # success (green check)
ERR = "#B07A22"           # failure: calm amber, never an alarm
NEUTRAL = "#A3A5AC"       # discarded / muted state dot

ROW_HOVER = "#EEEDE8"     # subtle hover on a whole clickable row
CHIP_BG = "#EBEAE4"       # audio-source / meta chip background
CHIP_FG = "#3A3C46"

# DWM rounded-corner border colour, as 0x00BBGGRR (Win11).
DWM_BORDER_BGR = (0xE3, 0xE2, 0xDC)   # matches BORDER; converted in the widget
ORB = 18                  # Lia's orb (the busy indicator), logical px
FADE_MS = 180             # the card fades in when it first appears
# A processing card folds to its one-line form after this long (unless the
# user already chose Minimize/Restore or opened Details); hovering opens it,
# leaving folds it again; the finished card always opens in full.
AUTO_COLLAPSE_MS = 8000
HOVER_CLOSE_MS = 350      # grace before a hover-opened card folds back
# The bottom-right cards rest half-transparent and come to full opacity while
# the pointer is over them (Naor, 2026-10-27: "too loud"). An error card
# stays opaque - a failure must never be easy to miss.
REST_ALPHA = 0.65
FULL_ALPHA = 1.0
HOVER_LEAVE_MS = 150      # grace so moving between child widgets never flickers

# ---- Sizes (logical px @ 96 DPI) -----------------------------------------
WIDTH = 384
WIDTH_MIN = 340
PAD = 16                  # card inner padding
GAP_SM = 8               # tight content gap
GAP_MD = 12              # section gap / gap between stacked cards
CORNER = 14              # corner radius (native DWM)
MARGIN = 24              # distance from the work-area edge (bottom-right)
DOT = 8                  # state dot diameter
HIT = 30                 # min/close clickable box (icon sits inside)
ICON = 16               # min/close glyph box
BTN_H = 35               # action button height
BTN_PAD_X = 14           # action button horizontal padding

# ---- Typography (Tk POINT sizes; scaled by the root's tk scaling) --------
# Calibrated in the preview so the rendered heights match the spec's logical
# px at 100%: title ~15px, body ~13-14px, meta ~12px. Segoe UI Variable Text
# is the Win11 UI face; it falls back to Segoe UI where unavailable.
FONT_FACE = "Segoe UI Variable Text"
# ...but Segoe UI Variable has NO full Hebrew: Tk pulled the missing letters
# (ת, ן) from a fallback font, and the per-font runs scrambled the word order
# ("תפעילו" for "פעילות" on a real meeting name, 2026-09-23). Any card with
# Hebrew/RTL text renders in classic Segoe UI, which covers Hebrew.
FONT_FACE_RTL = "Segoe UI"
PT_TITLE = 12            # ~ spec 15px semibold
PT_NAME = 10             # ~ spec 13-14px
PT_BODY = 10             # message / stage label
PT_META = 8              # ~ spec 12px secondary
PT_BRAND = 8             # brand name
PT_BTN = 9               # action button label
PT_ICON = 11             # min/close glyph


class Metrics:
    """Converts the logical-px tokens above to device px for one monitor's
    DPI scale. Fonts stay in points (Tk `tk scaling` handles their DPI)."""

    def __init__(self, dpi_scale: float = 1.0):
        self.scale = float(dpi_scale) if dpi_scale and dpi_scale > 0 else 1.0
        self.rtl = False     # set per card: True -> FONT_FACE_RTL

    def px(self, logical: float) -> int:
        """Logical px -> device px (rounded), never below 1 for positive input."""
        v = int(round(logical * self.scale))
        return max(1, v) if logical > 0 else v

    # Convenience accessors used by the widget / preview.
    def font(self, pt: int, *, bold: bool = False, semibold: bool = False):
        weight = "bold" if (bold or semibold) else "normal"
        return (FONT_FACE_RTL if self.rtl else FONT_FACE, pt, weight)


def pointer_over(top) -> bool:
    """True while the mouse pointer is inside the window `top`."""
    try:
        x, y = top.winfo_pointerxy()
        # the requested geometry is right even before the window is mapped
        # (a card rebuilt under the pointer); winfo_* lag until then
        import re
        g = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", top.wm_geometry() or "")
        if g and int(g.group(1)) > 1:
            w, h, x0, y0 = (int(v) for v in g.groups())
        else:
            x0, y0 = top.winfo_rootx(), top.winfo_rooty()
            w, h = top.winfo_width(), top.winfo_height()
        return x0 <= x < x0 + w and y0 <= y < y0 + h
    except Exception:
        return False


class HoverOpacity:
    """Rest `top` at `rest` opacity; full opacity while the pointer is over
    it. Bindings on a Toplevel fire for every child, and a Leave re-checks the
    pointer after HOVER_LEAVE_MS, so moving between widgets never dims it.
    Duck-typed on the Tk window (this module stays tk-free).

    apply=False leaves the current alpha alone (a fade-in that ends on
    `alpha()` is running)."""

    def __init__(self, top, rest=REST_ALPHA, apply=True):
        self.top, self.rest = top, rest
        self.full = pointer_over(top)
        self._pending = None
        for seq, fn in (("<Enter>", self._enter), ("<Motion>", self._enter),
                        ("<Leave>", self._leave), ("<Destroy>", self._destroyed)):
            try:
                top.bind(seq, fn, add="+")
            except Exception:
                pass
        if apply:
            self._apply()

    def alpha(self) -> float:
        return FULL_ALPHA if self.full else self.rest

    def _apply(self):
        try:
            self.top.attributes("-alpha", self.alpha())
        except Exception:
            pass

    def _cancel(self):
        if self._pending is not None:
            try:
                self.top.after_cancel(self._pending)
            except Exception:
                pass
            self._pending = None

    def _enter(self, _e=None):
        self._cancel()
        if not self.full:
            self.full = True
            self._apply()

    def _check(self):
        self._pending = None
        if self.full and not pointer_over(self.top):
            self.full = False
            self._apply()

    def _leave(self, _e=None):
        if self._pending is None and self.full:
            try:
                self._pending = self.top.after(HOVER_LEAVE_MS, self._check)
            except Exception:
                pass

    def _destroyed(self, e=None):
        if e is None or str(getattr(e, "widget", "")) == str(self.top):
            self._cancel()


def is_reduced_motion() -> bool:
    """Windows 'Show animations' off -> honour reduced motion (best-effort)."""
    try:
        import ctypes
        val = ctypes.c_int(0)
        # SPI_GETCLIENTAREAANIMATION = 0x1042
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0,
                                                      ctypes.byref(val), 0):
            return not bool(val.value)
    except Exception:
        pass
    return False
