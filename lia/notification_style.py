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


def button_text(text):
    """The text for a tk.Button label: LOGICAL order, unchanged - Tk 8.6.15
    reorders Hebrew/Arabic in buttons natively, like in Labels (measured
    2026-10-13; a visual-order conversion here printed them backwards). Kept
    as the one seam every Tk button label goes through."""
    return text or ""


# ---- Colours (light card) ------------------------------------------------
# The Lia Lite language (UI redesign 2026-10-13, the ui_kit tokens): warm
# neutral ink, a deep-ink primary, Lia's colour only for "Lia is working",
# red only for recording, failures in a calm amber (never an alarm).
BG = "#FFFFFF"            # card surface, fully opaque
BORDER = "#EAEAE5"        # 1px hairline border
INK = "#17171E"           # primary text (title, meeting name)
SUB = "#6D6F78"           # secondary / meta text
FAINT = "#A3A5AC"         # tertiary (brand name, disabled)

PRIMARY_BG = "#272A4A"    # primary action button (deep ink)
PRIMARY_FG = "#FFFFFF"
PRIMARY_HOVER = "#33375E"
SECONDARY_BG = "#FFFFFF"  # secondary action: light, bordered
SECONDARY_FG = "#3A3C46"
SECONDARY_BORDER = "#DADAD3"
SECONDARY_HOVER = "#FAFAF8"

# State accents - used only on the small dot/icon, never as a card wash.
REC = "#D63543"           # recording (red dot) - red is for recording only
BUSY = "#6A5BE2"          # Lia is working (her colour; the orb when available)
OK = "#23845A"            # success (green check)
ERR = "#B07A22"           # failure: calm amber, never an alarm
NEUTRAL = "#A3A5AC"       # discarded / muted state dot

ROW_HOVER = "#FAFAF8"     # subtle hover on a whole clickable row
CHIP_BG = "#F0F0EC"       # audio-source / meta chip background
CHIP_FG = "#3A3C46"

# DWM rounded-corner border colour, as 0x00BBGGRR (Win11).
DWM_BORDER_BGR = (0xEA, 0xEA, 0xE5)   # matches BORDER; converted in the widget
ORB = 18                  # Lia's orb (the busy indicator), logical px
FADE_MS = 180             # the card fades in when it first appears

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
