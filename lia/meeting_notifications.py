# -*- coding: utf-8 -*-
"""Lia - the light meeting-notification card (redesign 2026-09-17).

A single small WHITE Tk card, bottom-right of the work area, that renders any
meeting state from a plain dict (see `CardState`). It replaces the old purple,
75%-opacity gradient card for MEETING events only. The dictation pill and
waveform are untouched and live elsewhere.

This module owns rendering + a light animation timer. It does NOT own the Tk
root or the event sources: the host passes in the root, a UI-thread queue
(callables run on the Tk thread) and the monitor DPI scale. State/logic that
decides WHAT to show (generation guards, dedup, silent policy) stays in the
host adapter - here we only draw and route button clicks to handler callbacks.

Everything public is Tk-thread-safe via the queue. Actions are real
`tk.Button`s (focusable, Space/Enter activate) so keyboard users can act once
the card is interacted with; the card never steals focus on appearance
(WS_EX_NOACTIVATE), so it can't interrupt typing.

Kinds (title/dot/actions differ; see notification_preview.py for the full set):
  detected · recording · starting · stop_prompt · auto_stop · processing ·
  success · error · discarded
"""
from __future__ import annotations

import time

import notification_style as ns

# Small vector-ish glyphs drawn as text in the state dot slot. Kept to plain
# marks (not emoji) so they render in one colour and align on the baseline.
_SPIN = "◐◓◑◒"


def _dot_color(dot):
    return {
        "rec": ns.REC, "busy": ns.BUSY, "ok": ns.OK, "err": ns.ERR,
        "neutral": ns.NEUTRAL,
    }.get(dot)


class MeetingCard:
    """One reusable card. `render(state)` rebuilds it; the host calls it on the
    UI thread (directly, or via `post` which hops through the queue)."""

    def __init__(self, root, tk_queue, dpi_scale=1.0, logo_image=None,
                 wordmark_image=None, log=None):
        self._root = root
        self._queue = tk_queue
        self._m = ns.Metrics(dpi_scale)
        self._logo = logo_image           # a Tk PhotoImage of the mark, or None
        self._wordmark = wordmark_image   # a Tk PhotoImage of 'lia', or None
        self._log = log
        self._top = None                  # the Toplevel, or None when hidden
        self._state = None                # last rendered CardState (dict)
        self._widgets = {}                # live refs updated by the timer
        self._anim_id = None
        self._t0 = None                   # time.monotonic at (re)build
        self._spin = 0
        self._rtl = False
        self._reduced = ns.is_reduced_motion()

    # ---- thread hop ------------------------------------------------------
    def post(self, fn):
        """Run `fn` on the Tk thread via the host queue."""
        try:
            self._queue.put(fn)
        except Exception:
            pass

    # ---- public API (call on the Tk thread, or wrap with post) -----------
    def render(self, state):
        """(Re)build the card for `state` (a dict). Tk-thread only."""
        if not self._root:
            return
        self._build(dict(state or {}))

    def update_meta(self, *, elapsed_seconds=None, stage_label=None):
        """Cheap in-place refresh of elapsed / active-stage text. Tk-thread."""
        c = self._state
        if not c or not self._top:
            return
        if elapsed_seconds is not None:
            c["elapsed_seconds"] = elapsed_seconds
        if stage_label is not None:
            c["stage_label"] = stage_label
        self._refresh_dynamic()

    def hide(self):
        """Destroy the card. Tk-thread only."""
        self._teardown()

    # ---- build -----------------------------------------------------------
    def _build(self, c):
        import tkinter as tk
        self._teardown(keep_state=True)
        self._state = c
        # Whole-card direction from its visible text. Label/Message bidi
        # natively (keep text logical); we only flip anchors so Hebrew reads
        # from the right. Buttons are handled per-widget via ns.button_text.
        self._rtl = (ns.has_rtl(c.get("title")) or ns.has_rtl(c.get("meeting_title"))
                     or ns.has_rtl(c.get("message")))
        m = self._m
        pad = m.px(ns.PAD)

        top = tk.Toplevel(self._root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=ns.BORDER)          # 1px border = a hairline frame
        self._top = top
        self._no_activate(top)               # never steal focus on show

        # 1px hairline: the Toplevel bg is the border colour; an inner frame
        # inset by 1px carries the white surface.
        b = m.px(1)
        surface = tk.Frame(top, bg=ns.BG)
        surface.pack(fill="both", expand=True, padx=b, pady=b)

        body = tk.Frame(surface, bg=ns.BG, padx=pad, pady=pad)
        body.pack(fill="both", expand=True)

        self._widgets = {}
        self._build_brand(tk, body, c)
        self._build_title(tk, body, c)
        self._build_meeting(tk, body, c)
        self._build_info(tk, body, c)
        if c.get("expanded") and c.get("stages"):
            self._build_stages(tk, body, c)
        self._build_actions(tk, body, c)

        # `anchor` (a time.monotonic reference) lets elapsed survive rebuilds
        # so a stage change or Details toggle doesn't reset the timer.
        self._t0 = c.get("anchor") if c.get("anchor") is not None else time.monotonic()
        self._spin = 0
        self._position()
        self._round()
        self._refresh_dynamic()
        if c.get("dot") in ("busy", "rec") and not self._reduced:
            self._tick()

    def _build_brand(self, tk, parent, c):
        m = self._m
        lead = "right" if self._rtl else "left"    # brand side
        trail = "left" if self._rtl else "right"   # controls side
        row = tk.Frame(parent, bg=ns.BG)
        row.pack(fill="x")
        if self._logo is not None:
            tk.Label(row, image=self._logo, bg=ns.BG).pack(side=lead)
        gap = (m.px(6) if self._logo is not None else 0, 0)
        if self._wordmark is not None:
            tk.Label(row, image=self._wordmark, bg=ns.BG).pack(side=lead, padx=gap)
        else:
            tk.Label(row, text="Lia", bg=ns.BG, fg=ns.FAINT,
                     font=m.font(ns.PT_BRAND, semibold=True)).pack(side=lead, padx=gap)

        # close (always) + minimize (processing only), as focusable buttons in
        # a HIT-sized box, mirrored to the leading edge for RTL.
        if c.get("allow_close", True):
            self._icon_btn(tk, row, "✕", self._on_close, "Close notification"
                           ).pack(side=trail)
        if c.get("allow_min"):
            self._widgets["min_btn"] = self._icon_btn(
                tk, row, "–", self._on_min,
                "Minimize" if not c.get("minimized") else "Restore")
            self._widgets["min_btn"].pack(side=trail, padx=(0, m.px(2)))

    def _build_title(self, tk, parent, c):
        m = self._m
        row = tk.Frame(parent, bg=ns.BG)
        row.pack(fill="x", pady=(m.px(ns.GAP_SM), 0))
        dot = c.get("dot")
        col = _dot_color(dot)
        if col:
            # A small round dot (busy uses a spinner glyph instead of a disc).
            if dot == "busy":
                d = tk.Label(row, text=_SPIN[0], bg=ns.BG, fg=col,
                             font=m.font(ns.PT_BODY))
                self._widgets["spin"] = d
            elif dot in ("ok", "err", "neutral"):
                glyph = {"ok": "✓", "err": "✕", "neutral": "⊘"}[dot]
                d = tk.Label(row, text=glyph, bg=ns.BG, fg=col,
                             font=m.font(ns.PT_BODY, semibold=True))
            else:  # rec: a filled disc
                d = tk.Canvas(row, width=m.px(ns.DOT + 4), height=m.px(ns.PT_TITLE),
                              bg=ns.BG, highlightthickness=0)
                r = m.px(ns.DOT)
                y = m.px(ns.PT_TITLE) // 2
                d.create_oval(2, y - r // 2, 2 + r, y + r // 2, fill=col, outline="")
            # RTL: dot on the right, next to the right-aligned title.
            if self._rtl:
                d.pack(side="right", padx=(m.px(8), 0))
            else:
                d.pack(side="left", padx=(0, m.px(8)))
        anc = "e" if self._rtl else "w"
        jus = "right" if self._rtl else "left"
        tk.Label(row, text=c.get("title", ""), bg=ns.BG, fg=ns.INK,
                 font=m.font(ns.PT_TITLE, semibold=True), anchor=anc,
                 justify=jus).pack(side="left", fill="x", expand=True)

    def _build_meeting(self, tk, parent, c):
        m = self._m
        name = (c.get("meeting_title") or "").strip()
        if not name:
            return
        lbl = tk.Label(parent, text=name, bg=ns.BG, fg=ns.INK,
                       font=m.font(ns.PT_NAME),
                       anchor="e" if self._rtl else "w",
                       justify="right" if self._rtl else "left",
                       wraplength=m.px(ns.WIDTH - 2 * ns.PAD))
        lbl.pack(fill="x", pady=(m.px(6), 0))

    def _build_info(self, tk, parent, c):
        m = self._m
        chips = list(c.get("chips") or [])
        msg = (c.get("message") or "").strip()
        if not chips and not msg and c.get("dot") != "busy":
            return
        lead = "right" if self._rtl else "left"
        trail = "left" if self._rtl else "right"
        row = tk.Frame(parent, bg=ns.BG)
        row.pack(fill="x", pady=(m.px(ns.GAP_SM), 0))
        for ch in chips:
            tk.Label(row, text=f" {ch} ", bg=ns.CHIP_BG, fg=ns.CHIP_FG,
                     font=m.font(ns.PT_META), padx=m.px(4), pady=m.px(1)
                     ).pack(side=lead, padx=(0, m.px(6)))
        if c.get("dot") == "busy":
            # active stage (leading) + elapsed (trailing), refreshed by timer
            self._widgets["stage"] = tk.Label(
                row, text="", bg=ns.BG, fg=ns.SUB, font=m.font(ns.PT_META),
                anchor="e" if self._rtl else "w")
            self._widgets["stage"].pack(side=lead)
            self._widgets["elapsed"] = tk.Label(
                row, text="", bg=ns.BG, fg=ns.SUB, font=m.font(ns.PT_META),
                anchor="w" if self._rtl else "e")
            self._widgets["elapsed"].pack(side=trail)
        elif msg:
            tk.Label(row, text=msg, bg=ns.BG, fg=ns.SUB, font=m.font(ns.PT_META),
                     anchor="e" if self._rtl else "w",
                     justify="right" if self._rtl else "left",
                     wraplength=m.px(ns.WIDTH - 2 * ns.PAD)).pack(fill="x")

    def _build_stages(self, tk, parent, c):
        """Full stage list (only when expanded via Details). Tk-thread."""
        m = self._m
        box = tk.Frame(parent, bg=ns.BG)
        box.pack(fill="x", pady=(m.px(ns.GAP_SM), 0))
        for key, label, st in c.get("stages") or []:
            row = tk.Frame(box, bg=ns.BG)
            row.pack(fill="x", pady=(m.px(2), 0))
            glyph, col = {
                "done": ("✓", ns.OK), "active": ("•", ns.BUSY),
                "error": ("✕", ns.ERR),
            }.get(st, ("○", ns.FAINT))
            side = "right" if self._rtl else "left"
            tk.Label(row, text=glyph, bg=ns.BG, fg=col,
                     font=m.font(ns.PT_META), width=2).pack(side=side)
            tk.Label(row, text=label, bg=ns.BG,
                     fg=ns.INK if st != "pending" else ns.FAINT,
                     font=m.font(ns.PT_META),
                     anchor="e" if self._rtl else "w").pack(side=side)

    def _build_actions(self, tk, parent, c):
        m = self._m
        actions = list(c.get("actions") or [])
        # A Details toggle for processing cards with hidden stages.
        if c.get("stages") and c.get("dot") == "busy":
            lbl = "Hide details" if c.get("expanded") else "Details"
            actions = actions + [(lbl, self._on_toggle_details, False)]
        if not actions:
            return
        row = tk.Frame(parent, bg=ns.BG)
        row.pack(fill="x", pady=(m.px(ns.GAP_MD), 0))
        # Primary sits on the trailing edge (right in LTR, left in RTL);
        # secondaries fall inward from there in given order.
        trail = "left" if self._rtl else "right"
        pad = (0, m.px(8)) if self._rtl else (m.px(8), 0)
        for label, cb, primary in actions:
            self._action_btn(tk, row, label, cb, primary).pack(
                side=trail, padx=pad)

    # ---- small widgets ---------------------------------------------------
    def _action_btn(self, tk, parent, label, cb, primary):
        m = self._m
        bg = ns.PRIMARY_BG if primary else ns.SECONDARY_BG
        fg = ns.PRIMARY_FG if primary else ns.SECONDARY_FG
        hov = ns.PRIMARY_HOVER if primary else ns.SECONDARY_HOVER
        b = tk.Button(
            parent, text=ns.button_text(label), bg=bg, fg=fg,
            activebackground=hov, activeforeground=fg,
            font=m.font(ns.PT_BTN, semibold=primary),
            relief="flat", bd=0, cursor="hand2",
            highlightthickness=m.px(1),
            highlightbackground=bg if primary else ns.SECONDARY_BORDER,
            highlightcolor=ns.BUSY,
            padx=m.px(ns.BTN_PAD_X), pady=m.px(6),
            command=lambda: self._fire(cb, b))
        b.bind("<Enter>", lambda _e: b.configure(bg=hov))
        b.bind("<Leave>", lambda _e: b.configure(bg=bg))
        return b

    def _icon_btn(self, tk, parent, glyph, cb, name):
        m = self._m
        box = m.px(ns.HIT)
        b = tk.Button(
            parent, text=glyph, bg=ns.BG, fg=ns.FAINT,
            activebackground=ns.ROW_HOVER, activeforeground=ns.INK,
            font=m.font(ns.PT_ICON), relief="flat", bd=0, cursor="hand2",
            width=2, highlightthickness=m.px(1),
            highlightbackground=ns.BG, highlightcolor=ns.BUSY,
            command=lambda: self._fire(cb, b))
        b.bind("<Enter>", lambda _e: b.configure(fg=ns.INK, bg=ns.ROW_HOVER))
        b.bind("<Leave>", lambda _e: b.configure(fg=ns.FAINT, bg=ns.BG))
        try:
            b.configure(takefocus=1)
        except Exception:
            pass
        return b

    def _fire(self, cb, widget):
        """Disable the control until the handler returns (no double-fire),
        then run the callback off the UI thread. A callback of None just
        closes info cards; explicit handlers decide their own teardown."""
        try:
            widget.configure(state="disabled")
        except Exception:
            pass
        if cb is None:
            self._teardown()
            return
        import threading
        threading.Thread(target=self._safe_cb, args=(cb,), daemon=True).start()

    def _safe_cb(self, cb):
        try:
            cb()
        except Exception as e:
            if self._log:
                self._log.warning("notification action failed: %r", e)

    # ---- button handlers -------------------------------------------------
    def _on_close(self):
        pol = (self._state or {}).get("on_close")
        if pol:
            self._safe_cb(pol)       # e.g. the stop-prompt's "keep recording"
        self._teardown()

    def _on_min(self):
        c = self._state
        if not c:
            return
        c["minimized"] = not c.get("minimized")
        self._build(c)

    def _on_toggle_details(self):
        c = self._state
        if not c:
            return
        c["expanded"] = not c.get("expanded")
        self._build(c)

    # ---- dynamic refresh + animation ------------------------------------
    def _refresh_dynamic(self):
        c = self._state or {}
        el = c.get("elapsed_seconds")
        if el is None and self._t0 is not None and c.get("dot") in ("busy", "rec"):
            el = time.monotonic() - self._t0
        w = self._widgets
        if "elapsed" in w and el is not None:
            w["elapsed"].configure(text=_fmt_elapsed(el))
        if "stage" in w:
            sl = (c.get("stage_label") or "").strip()
            w["stage"].configure(text=sl)

    def _tick(self):
        if not self._top or not self._state:
            return
        try:
            if "spin" in self._widgets:
                self._spin = (self._spin + 1) % len(_SPIN)
                self._widgets["spin"].configure(text=_SPIN[self._spin])
            self._refresh_dynamic()
            self._anim_id = self._top.after(180, self._tick)
        except Exception:
            return

    # ---- geometry --------------------------------------------------------
    def _position(self):
        top = self._top
        if not top:
            return
        m = self._m
        top.update_idletasks()
        w = max(top.winfo_reqwidth(), m.px(ns.WIDTH_MIN))
        h = top.winfo_reqheight()
        ax, ay, aw, ah = self._work_area()
        margin = m.px(ns.MARGIN)
        x = ax + aw - w - margin
        y = ay + ah - h - margin
        # Keep fully inside the work area (small screens / large scale).
        x = max(ax, min(x, ax + aw - w))
        y = max(ay, min(y, ay + ah - h))
        top.geometry(f"{w}x{h}+{x}+{y}")

    def _work_area(self):
        """(x, y, w, h) of the monitor work area holding the card, in device
        px. Falls back to the Tk screen size (minus a taskbar guess)."""
        try:
            import ctypes
            from ctypes import wintypes

            class RECT(ctypes.Structure):
                _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                            ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                            ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

            hwnd = self._top.winfo_id() if self._top else 0
            mon = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)  # NEAREST
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                r = mi.rcWork
                return (r.left, r.top, r.right - r.left, r.bottom - r.top)
        except Exception:
            pass
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        return (0, 0, sw, sh - self._m.px(48))

    def _round(self):
        """Native Win11 rounded corners + hairline border via DWM. No-op on
        older Windows (the 1px frame border still shows)."""
        top = self._top
        if not top:
            return
        try:
            import ctypes
            from ctypes import wintypes
            top.update_idletasks()
            hwnd = top.winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd) or hwnd
            dwm = ctypes.windll.dwmapi
            pref = ctypes.c_int(2)   # DWMWA_WINDOW_CORNER_PREFERENCE=33, ROUND=2
            b, g, r = ns.DWM_BORDER_BGR[2], ns.DWM_BORDER_BGR[1], ns.DWM_BORDER_BGR[0]
            col = ctypes.c_int((b << 16) | (g << 8) | r)
            for h in {hwnd, parent}:
                dwm.DwmSetWindowAttribute(wintypes.HWND(h), 33,
                                          ctypes.byref(pref), ctypes.sizeof(pref))
                dwm.DwmSetWindowAttribute(wintypes.HWND(h), 34,
                                          ctypes.byref(col), ctypes.sizeof(col))
        except Exception:
            pass

    def _no_activate(self, top):
        """Set WS_EX_NOACTIVATE so showing the card never steals focus /
        interrupts typing (still clickable). Best-effort."""
        try:
            import ctypes
            top.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(top.winfo_id()) or top.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            gwl = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                                gwl | WS_EX_NOACTIVATE)
        except Exception:
            pass

    # ---- teardown --------------------------------------------------------
    def _teardown(self, keep_state=False):
        if self._anim_id and self._top:
            try:
                self._top.after_cancel(self._anim_id)
            except Exception:
                pass
        self._anim_id = None
        if self._top:
            try:
                self._top.destroy()
            except Exception:
                pass
        self._top = None
        self._widgets = {}
        if not keep_state:
            self._state = None


def _fmt_elapsed(sec):
    sec = int(max(0, sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"
