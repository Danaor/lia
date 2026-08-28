"""Lia - "Summarize Text / File" input window (pywebview, on ui_kit).

Replaces the old dark Tk input dialog with a light, navy pywebview window
consistent with Settings / History. It ONLY collects the text (paste/type or
Load file) and hands it back to the parent app, which runs the summary and
opens the result in the compose editor (the same beautiful surface as Edit
Meeting Summary). On Summarize it writes {"text": ...} to the result_path from
its payload and closes; on Cancel / window-close it writes nothing, so the
parent does nothing.

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
            ft = ("Text files (*.txt;*.md;*.markdown;*.log;*.csv;*.json;*.srt;*.vtt)",
                  "All files (*.*)")
            res = win.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                         file_types=ft)
            if not res:
                return {"ok": False}
            path = res[0] if isinstance(res, (list, tuple)) else res
            with open(path, encoding="utf-8", errors="replace") as f:
                return {"ok": True, "text": f.read(), "name": os.path.basename(path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def submit(self, text):
        """Hand the text to the parent (write the result file), then close."""
        try:
            if _RESULT_PATH:
                uk.atomic_write_json(_RESULT_PATH, {"text": text or ""})
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
    <h1>Summarize text or file</h1>
    <div class="sub">Paste or type text, or load a file. It is summarized exactly as given.</div>
  </div>
  <textarea id="src" class="src rtl-auto" dir="auto"
    placeholder="Paste text here (Ctrl+V), type, or use Load file&#8230;"></textarea>
  <div class="sfoot">
    <button class="btn ghost" id="load">Load file&#8230;</button>
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
  function go(){
    var t = (RK.$("src").value || "").trim();
    if(!t){ RK.toast("Nothing to summarize - paste or load text first", "err"); return; }
    RK.ready(function(api){ api.submit(t); });
  }
  RK.$("go").addEventListener("click", go);
  RK.$("cancel").addEventListener("click", function(){
    RK.ready(function(api){ api.cancel(); });
  });
  RK.$("clear").addEventListener("click", function(){
    RK.$("src").value = ""; RK.$("src").focus();
  });
  RK.$("load").addEventListener("click", function(){
    RK.ready(function(api){ api.loadfile().then(function(r){
      if(r && r.ok){ RK.$("src").value = r.text || ""; RK.$("src").focus();
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
    if isinstance(payload, dict):
        _RESULT_PATH = payload.get("result_path")
    theme = (payload.get("theme") if isinstance(payload, dict) else None) or None
    html = uk.page("Summarize Text / File", BODY, extra_css=EXTRA_CSS,
                   extra_js=APP_JS, theme=theme)
    geo = uk.window_geometry("summarize", {"width": 640, "height": 560})
    kw = {"width": geo.get("width", 640), "height": geo.get("height", 560),
          "min_size": (460, 380)}
    if "x" in geo:
        kw["x"] = geo["x"]
        kw["y"] = geo["y"]
    win = webview.create_window("Summarize Text / File", html=html,
                                js_api=SummarizeApi(), **kw)
    uk.attach_geometry_memory(win, "summarize")
    return win


def main():
    uk.child_main(build_window, log_path=LOG_PATH, ready_check=lambda: _READY,
                  watchdog_s=30)


if __name__ == "__main__":
    main()
