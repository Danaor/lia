# -*- coding: utf-8 -*-
"""Lia - isolated preview for the meeting-notification card (redesign 2026-09-17).

Runs ONLY the UI with dummy data: no recording, no API calls, no meeting files,
no personal names or paths. Two ways to use it:

  python notification_preview.py
      Opens a small control panel: click a state to show that card, toggle a
      light/dark backdrop, and try Hebrew + long-title variants.

  python notification_preview.py --capture <out_dir> [--dark]
      Renders every demo state, captures each card window to a PNG via Win32
      PrintWindow (no need to steal focus), and exits. Produces the Phase-B
      screenshots.

Buttons in the demo are inert (their callbacks just close the card); they never
start a recording or open a file.
"""
from __future__ import annotations

import os
import sys
import time

# The app configures DPI + tk scaling in lia.py; mirror just enough here so the
# preview reflects real sizing. Import the shared tokens + widget.
import notification_style as ns
import meeting_notifications as mn


# ---- dummy data (no personal names or paths) -----------------------------
def demo_states(hebrew=False, longtext=False):
    """Return [(id, label, state_dict)] covering every meeting state."""
    if hebrew:
        mt = "פגישת צוות שבועית"
        mt_long = "סנכרון שבועי - תכנון רבעון והספרינט הבא של הצוות"
        svc = "Zoom · פגישת צוות שבועית"
        stages = [("diarize", "זיהוי דוברים", "done"),
                  ("transcribe", "תמלול", "active"),
                  ("summarize", "סיכום", "pending")]
        T = dict(detected="פגישה זוהתה", recording="ההקלטה התחילה",
                 starting="מתחיל הקלטה…", still="עדיין מקליט",
                 autostop="ההקלטה נעצרה אוטומטית", proc="מעבד פגישה",
                 summ="יוצר סיכום", sumready="הסיכום מוכן",
                 trready="התמלול מוכן", partial="לא ניתן היה ליצור סיכום",
                 failed="עיבוד הפגישה נכשל", stopped="העיבוד הופסק")
        A = dict(start="התחל הקלטה", stop="עצור הקלטה", keep="המשך הקלטה",
                 opensum="פתח סיכום", opentr="פתח תמלול", folder="פתח תיקייה",
                 details="פרטים")
        msg_ended = "נראה שהשיחה הסתיימה."
        msg_silence = "10 דקות של שקט."
        msg_partial = "התמלול נשמר."
        msg_failed = "שירות התמלול החזיר שגיאה."
        msg_saved = "ההקלטה נשמרה."
        chips = ["מיקרופון", "שמע מערכת"]
    else:
        mt = "Weekly sync"
        mt_long = "Weekly sync - quarterly planning and the team's next sprint kickoff"
        svc = "Zoom · Weekly sync"
        stages = [("diarize", "Identify speakers", "done"),
                  ("transcribe", "Transcribe", "active"),
                  ("summarize", "Summarize", "pending")]
        T = dict(detected="Meeting detected", recording="Recording started",
                 starting="Starting recording…", still="Still recording",
                 autostop="Recording stopped automatically", proc="Processing meeting",
                 summ="Creating summary", sumready="Summary ready",
                 trready="Transcript ready", partial="Summary could not be created",
                 failed="Meeting processing failed", stopped="Processing stopped")
        A = dict(start="Start recording", stop="Stop recording", keep="Keep recording",
                 opensum="Open summary", opentr="Open transcript", folder="Open folder",
                 details="Details")
        msg_ended = "The call seems to have ended."
        msg_silence = "10 min of silence."
        msg_partial = "The transcript was saved."
        msg_failed = "The transcription service returned an error."
        msg_saved = "Recording saved."
        chips = ["Microphone", "System audio"]

    name = mt_long if longtext else mt

    def st(**kw):
        base = dict(allow_close=True)
        base.update(kw)
        return base

    return [
        ("detected", T["detected"], st(
            kind="detected", title=T["detected"], meeting_title=svc,
            actions=[(A["start"], None, True)])),
        ("recording", T["recording"], st(
            kind="recording", title=T["recording"], meeting_title=name,
            dot="rec", chips=chips)),
        ("starting", T["starting"], st(
            kind="starting", title=T["starting"], meeting_title=name, dot="busy",
            stage_label="")),
        ("stop_prompt", T["still"], st(
            kind="stop_prompt", title=T["still"], message=msg_ended,
            actions=[(A["stop"], None, True), (A["keep"], None, False)])),
        ("auto_stop", T["autostop"], st(
            kind="auto_stop", title=T["autostop"], message=msg_silence,
            dot="neutral")),
        ("processing", T["proc"], st(
            kind="processing", title=T["proc"], meeting_title=name, dot="busy",
            chips=["gpt-transcribe"], stage_label=stages[1][1],
            elapsed_seconds=47, stages=stages, allow_min=True,
            on_discard=lambda: None)),
        ("processing_expanded", T["proc"] + " (details)", st(
            kind="processing", title=T["proc"], meeting_title=name, dot="busy",
            chips=["gpt-transcribe"], stage_label=stages[1][1],
            elapsed_seconds=47, stages=stages, allow_min=True, expanded=True,
            on_discard=lambda: None)),
        ("summarizing", T["summ"], st(
            kind="processing", title=T["summ"], meeting_title=name, dot="busy",
            stage_label=A["opentr"], elapsed_seconds=132,
            actions=[(A["opentr"], None, False)])),
        ("success_summary", T["sumready"], st(
            kind="success", title=T["sumready"], meeting_title=name, dot="ok",
            actions=[(A["opensum"], None, True), (A["opentr"], None, False)])),
        ("success_transcript", T["trready"], st(
            kind="success", title=T["trready"], meeting_title=name, dot="ok",
            actions=[(A["opentr"], None, True)])),
        ("partial_fail", T["partial"], st(
            kind="error", title=T["partial"], message=msg_partial, dot="err",
            actions=[(A["opentr"], None, False)])),
        ("error", T["failed"], st(
            kind="error", title=T["failed"], message=msg_failed, dot="err",
            actions=[(A["folder"], None, False)])),
        ("discarded", T["stopped"], st(
            kind="discarded", title=T["stopped"], message=msg_saved, dot="neutral",
            actions=[(A["folder"], None, False)])),
    ]


def _dpi_scale():
    """Primary-monitor DPI scale; set per-monitor awareness first (like lia)."""
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        hdc = ctypes.windll.user32.GetDC(0)
        scale = max(1.0, ctypes.windll.gdi32.GetDeviceCaps(hdc, 90) / 96.0)
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return scale
    except Exception:
        return 1.0


# ---- a minimal host: Tk root + a UI-thread queue -------------------------
class _PreviewHost:
    def __init__(self, dark=False):
        import tkinter as tk
        import queue as _q
        self._tk = tk
        self.root = tk.Tk()
        self.root.title("Lia notification preview")
        # Mirror the app's DPI policy so sizing matches production, WITHOUT
        # importing lia.py (heavy). Per-monitor DPI aware + tk scaling bump.
        self.dpi = _dpi_scale()
        if self.dpi > 1.01:
            try:
                self.root.tk.call("tk", "scaling", 1.333 * self.dpi)
            except Exception:
                pass
        self.queue = _q.Queue()
        self._pump()
        self.card = mn.MeetingCard(self.root, self.queue, dpi_scale=self.dpi,
                                   logo_image=None, log=None)
        self.set_backdrop(dark)

    def _pump(self):
        try:
            while True:
                fn = self.queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except Exception:
            pass
        self.root.after(30, self._pump)

    def set_backdrop(self, dark):
        self.root.configure(bg="#11151c" if dark else "#eef1f5")


def _run_panel(dark=False):
    """Interactive control panel."""
    host = _PreviewHost(dark=dark)
    tk = host._tk
    state = {"hebrew": False, "long": False, "dark": dark}

    panel = tk.Frame(host.root, bg=host.root["bg"], padx=16, pady=16)
    panel.pack(fill="both", expand=True)
    tk.Label(panel, text="Meeting notification preview (dummy data)",
             bg=host.root["bg"], fg="#e6e9ef" if dark else "#1b2430",
             font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 10))

    btns = tk.Frame(panel, bg=host.root["bg"])
    btns.pack(fill="x")

    def show(sid):
        for _id, _lbl, s in demo_states(state["hebrew"], state["long"]):
            if _id == sid:
                host.card.render(s)
                return

    def rebuild_list():
        for w in btns.winfo_children():
            w.destroy()
        for i, (sid, label, _s) in enumerate(demo_states(state["hebrew"], state["long"])):
            tk.Button(btns, text=label, command=lambda x=sid: show(x),
                      anchor="w", width=34,
                      ).grid(row=i % 7, column=i // 7, sticky="w", padx=4, pady=3)

    rebuild_list()

    toggles = tk.Frame(panel, bg=host.root["bg"])
    toggles.pack(fill="x", pady=(12, 0))

    def toggle(key):
        state[key] = not state[key]
        if key == "dark":
            host.set_backdrop(state["dark"])
        rebuild_list()

    tk.Button(toggles, text="Hebrew", command=lambda: toggle("hebrew")).pack(side="left", padx=4)
    tk.Button(toggles, text="Long title", command=lambda: toggle("long")).pack(side="left", padx=4)
    tk.Button(toggles, text="Dark backdrop", command=lambda: toggle("dark")).pack(side="left", padx=4)
    tk.Button(toggles, text="Hide card", command=host.card.hide).pack(side="left", padx=4)

    host.root.geometry("+80+80")
    host.root.mainloop()


def _capture(out_dir, dark=False, hebrew=False):
    """Render each state and PrintWindow-capture the card to a PNG."""
    import ctypes
    from ctypes import wintypes
    os.makedirs(out_dir, exist_ok=True)
    host = _PreviewHost(dark=dark)
    host.root.geometry("400x120+40+40")
    host.root.update()

    def grab(hwnd, path):
        # PrintWindow the card HWND to a bitmap, save via PIL if available.
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        hdc = user32.GetWindowDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        # PW_RENDERFULLCONTENT = 0x2 (captures DWM-composited content)
        user32.PrintWindow(hwnd, mdc, 0x2)
        try:
            from PIL import Image
            class BMI(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD)]
            bmi = BMI()
            bmi.biSize = ctypes.sizeof(BMI)
            bmi.biWidth, bmi.biHeight = w, -h
            bmi.biPlanes, bmi.biBitCount = 1, 32
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
            img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
            bg = Image.new("RGB", (w, h),
                           (0x11, 0x15, 0x1c) if dark else (0xee, 0xf1, 0xf5))
            bg.paste(img, (0, 0), img)
            bg.save(path)
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            user32.ReleaseDC(hwnd, hdc)

    tag = ("he" if hebrew else "en") + ("_dark" if dark else "_light")
    for sid, _label, s in demo_states(hebrew=hebrew):
        host.card.render(s)
        for _ in range(8):
            host.root.update()
            time.sleep(0.05)
        top = host.card._top
        if top is not None:
            hwnd = ctypes.windll.user32.GetParent(top.winfo_id()) or top.winfo_id()
            path = os.path.join(out_dir, f"{sid}_{tag}.png")
            try:
                grab(hwnd, path)
                print("wrote", path)
            except Exception as e:
                print("capture failed", sid, repr(e))
        host.card.hide()
        host.root.update()
    host.root.destroy()


if __name__ == "__main__":
    if "--capture" in sys.argv:
        i = sys.argv.index("--capture")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "."
        _capture(out, dark="--dark" in sys.argv, hebrew="--hebrew" in sys.argv)
    else:
        _run_panel(dark="--dark" in sys.argv)
