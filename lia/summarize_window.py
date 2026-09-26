"""Lia - "Summarize a meeting file" input window (pywebview, on ui_kit).

For a meeting saved as a transcript only: it collects the transcript (Load
meeting file, opened in the meetings folder, or paste) and hands it back to
the parent app, which summarizes it AS A MEETING (the chosen summary model and
meeting template, _do_meeting_summary) and opens the result in the compose
editor. On Summarize it writes {"text": ..., "path": ...} to the result_path
from its payload and closes ("path" = the loaded file, only while the text is
unchanged - the parent then saves the summary with that meeting); on Cancel /
window-close it writes nothing, so the parent does nothing.

ALWAYS launch with  python -X utf8  (Hebrew stdout crashes under cp1252).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_kit as uk  # noqa: E402

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Lia")
LOG_PATH = os.path.join(CONFIG_DIR, "summarize_window.log")

_READY = False
_RESULT_PATH = None
_MEETINGS_DIR = os.path.join(CONFIG_DIR, "meetings")


class SummarizeApi:
    """JS-facing API (js_api). JS calls pywebview.api.<method> via RK.ready."""

    def loadfile(self):
        """Open a file picker in the child and return the file's text."""
        try:
            import webview
            wins = list(getattr(webview, "windows", []) or [])
            win = wins[0] if wins else None
            if win is None:
                return {"ok": False, "error": "no window"}
            ft = ("Meeting transcripts (*_meeting.txt;*_meeting_diarized.txt)",
                  "Text files (*.txt;*.md;*.markdown;*.log;*.csv;*.json;*.srt;*.vtt)",
                  "All files (*.*)")
            start = _MEETINGS_DIR if os.path.isdir(_MEETINGS_DIR) else ""
            res = win.create_file_dialog(webview.OPEN_DIALOG, directory=start,
                                         allow_multiple=False, file_types=ft)
            if not res:
                return {"ok": False}
            path = res[0] if isinstance(res, (list, tuple)) else res
            if os.path.getsize(path) > 5 * 1024 * 1024:       # audit F15
                return {"ok": False, "error": "That file is larger than 5 MB - "
                        "not a meeting transcript."}
            with open(path, encoding="utf-8", errors="replace") as f:
                return {"ok": True, "text": f.read(), "name": os.path.basename(path),
                        "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def submit(self, text, path=""):
        """Hand the text (+ the meeting file it came from) to the parent (write
        the result file), then close."""
        try:
            if _RESULT_PATH:
                uk.atomic_write_json(_RESULT_PATH, {"text": text or "",
                                                    "path": path or ""})
        except Exception as e:
            uk._warn("summarize submit write failed: %r\n" % (e,))
        self.close_window()
        return True

    def cancel(self):
        self.close_window()
        return True

    def mark_ready(self):
        global _READY
        _READY = True
        return True

    def close_window(self):
        import threading
        try:
            import webview
            wins = list(getattr(webview, "windows", []) or [])
        except Exception:
            wins = []

        def _kill():
            for w in wins:
                try:
                    w.destroy()
                except Exception:
                    pass
        threading.Thread(target=_kill, daemon=True).start()
        return True


BODY = """
<div class="swrap">
  <div class="stop">
    <h1>Summarize a meeting</h1>
    <div class="sub">Load a meeting you saved without a summary (or paste a transcript). It is
      summarized like a live meeting: your summary model and meeting template. A meeting from
      your meetings folder gets the summary saved with it.</div>
  </div>
  <textarea id="src" class="src rtl-auto" dir="auto"
    placeholder="Load meeting file&#8230;, or paste a transcript here (Ctrl+V)"></textarea>
  <div class="sfoot">
    <button class="btn ghost" id="load">Load meeting file&#8230;</button>
    <button class="btn ghost" id="clear">Clear</button>
    <span class="grow"></span>
    <button class="btn ghost" id="cancel">Cancel</button>
    <button class="btn primary" id="go">Summarize</button>
  </div>
</div>
"""

EXTRA_CSS = """
.swrap{display:flex; flex-direction:column; height:100vh;
  padding:var(--sp-5) var(--sp-5) var(--sp-4);}
.stop h1{margin:0; font-size:var(--fs-h1); font-weight:700;}
.stop .sub{color:var(--muted); font-size:var(--fs-hint); margin-top:2px;}
.src{flex:1 1 auto; margin:var(--sp-4) 0; min-height:180px; resize:none;
  line-height:1.6; font-size:var(--fs-base);}
.sfoot{display:flex; align-items:center; gap:var(--sp-2);}
.sfoot .grow{flex:1 1 auto;}
"""

APP_JS = r"""
(function(){
  var loaded = {text: null, path: ""};   // the meeting file, while the text is unchanged
  function go(){
    var t = (RK.$("src").value || "").trim();
    if(!t){ RK.toast("Nothing to summarize - load a meeting file or paste a transcript", "err"); return; }
    var p = (loaded.text !== null && t === loaded.text.trim()) ? loaded.path : "";
    RK.ready(function(api){ api.submit(t, p); });
  }
  RK.$("go").addEventListener("click", go);
  RK.$("cancel").addEventListener("click", function(){
    RK.ready(function(api){ api.cancel(); });
  });
  RK.$("clear").addEventListener("click", function(){
    RK.$("src").value = ""; loaded = {text: null, path: ""}; RK.$("src").focus();
  });
  RK.$("load").addEventListener("click", function(){
    RK.ready(function(api){ api.loadfile().then(function(r){
      if(r && r.ok){ RK.$("src").value = r.text || ""; RK.$("src").focus();
        loaded = {text: r.text || "", path: r.path || ""};
        RK.toast((r.name || "File") + " loaded", "ok"); }
      else if(r && r.error){ RK.toast("Load failed: " + r.error, "err"); }
    }); });
  });
  RK.$("src").addEventListener("keydown", function(e){
    if((e.ctrlKey || e.metaKey) && e.key === "Enter"){ e.preventDefault(); go(); }
  });
  RK.ready(function(api){ try{ api.mark_ready(); }catch(e){} RK.$("src").focus(); });
})();
"""


def build_window(webview, payload):
    global _RESULT_PATH
    global _MEETINGS_DIR
    if isinstance(payload, dict):
        _RESULT_PATH = payload.get("result_path")
        _MEETINGS_DIR = payload.get("meetings_dir") or _MEETINGS_DIR
    theme = (payload.get("theme") if isinstance(payload, dict) else None) or None
    html = uk.page("Summarize a meeting", BODY, extra_css=EXTRA_CSS,
                   extra_js=APP_JS, theme=theme)
    geo = uk.window_geometry("summarize", {"width": 640, "height": 560})
    kw = {"width": geo.get("width", 640), "height": geo.get("height", 560),
          "min_size": (460, 380)}
    if "x" in geo:
        kw["x"] = geo["x"]
        kw["y"] = geo["y"]
    win = webview.create_window("Summarize a meeting", html=html,
                                js_api=SummarizeApi(), **kw)
    uk.attach_geometry_memory(win, "summarize")
    return win


def main():
    uk.child_main(build_window, log_path=LOG_PATH, ready_check=lambda: _READY,
                  watchdog_s=30)


if __name__ == "__main__":
    main()
