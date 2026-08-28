"""Lia - Transcription history window (pywebview, on ui_kit).

Replaces the old "dump history.txt and open Notepad" flow with a real searchable
window: newest-first list, live filter, click-to-copy any entry. 100% local and
READ-ONLY (it reads %APPDATA%/Lia/history.json directly; it never writes it, so
it can't race the running app's history appends). Spawned by the tray/History
item via spawn_helper (fire-and-forget, like the other read-only windows).

ALWAYS launch with  python -X utf8  (Hebrew stdout crashes under cp1252).
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_kit as uk  # noqa: E402

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Lia")
HISTORY_FILE = os.path.join(CONFIG_DIR, "history.json")
LOG_PATH = os.path.join(CONFIG_DIR, "history_window.log")

_READY = False


def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except Exception:
        return []


def _fmt_ts(iso):
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso or ""


class HistoryApi:
    def get_initial(self):
        rows = load_history()
        rows = list(reversed(rows))   # newest first
        out = []
        for e in rows:
            txt = (e.get("text") or "").replace("‏", "").replace("‎", "").strip()
            if not txt:
                continue
            meta = []
            if e.get("duration"):
                meta.append("%ss" % e.get("duration"))
            if e.get("model"):
                meta.append(str(e.get("model")))
            if e.get("source"):
                meta.append(str(e.get("source")))
            if e.get("task") and e.get("task") != "transcribe":
                meta.append(str(e.get("task")))
            out.append({"ts": _fmt_ts(e.get("timestamp")), "text": txt,
                        "meta": "  ·  ".join(meta), "chars": len(txt)})
        return {"entries": out, "count": len(out)}

    def copy(self, text):
        return uk.copy_text(text)

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
<div class="hwrap">
  <div class="htop">
    <h1>History</h1>
    <span class="status ok" id="cnt"><span class="dot"></span></span>
    <span class="grow"></span>
    <input type="search" id="q" placeholder="Search transcriptions…" autocomplete="off">
  </div>
  <div id="list" class="list"></div>
  <div id="empty" class="empty" style="display:none">
    <div class="big">&#128221;</div><div>No transcriptions yet</div>
    <div class="sub">Dictate something and it will show up here.</div>
  </div>
</div>
"""

EXTRA_CSS = """
.hwrap{display:flex; flex-direction:column; height:100vh;}
.htop{display:flex; align-items:center; gap:var(--sp-3); padding:var(--sp-4) var(--sp-5) var(--sp-3);
  border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--bg); z-index:3;}
.htop h1{margin:0; font-size:var(--fs-h1); font-weight:700;}
.htop #q{max-width:280px;}
#list{overflow-y:auto; padding:var(--sp-2) var(--sp-4) var(--sp-5);}
.h-item{border:1px solid var(--line); border-radius:var(--r-m); padding:11px 14px; margin-top:10px;
  background:var(--card); cursor:pointer; transition:border-color var(--tr),background var(--tr);}
.h-item:hover{border-color:var(--accent); background:var(--accent-soft);}
.h-item .h-head{display:flex; align-items:center; gap:10px; margin-bottom:5px;}
.h-item .h-ts{font-size:var(--fs-hint); color:var(--muted); font-variant-numeric:tabular-nums;}
.h-item .h-meta{font-size:var(--fs-small); color:var(--faint); margin-inline-start:auto;}
.h-item .h-text{white-space:pre-wrap; line-height:1.6; overflow-wrap:anywhere;}
.h-item .h-copy{font-size:var(--fs-small); color:var(--accent); opacity:0; transition:opacity var(--tr);}
.h-item:hover .h-copy{opacity:1;}
mark{background:var(--hi, #FFF3BF); border-radius:2px;}
"""

APP_JS = r"""
(function(){
  var ALL = [];
  function esc(s){ return RK.esc(s); }
  function render(filter){
    var list = RK.$("list"), empty = RK.$("empty");
    var f = (filter||"").trim().toLowerCase();
    var rows = f ? ALL.filter(function(e){ return e.text.toLowerCase().indexOf(f)>=0; }) : ALL;
    if(!ALL.length){ list.innerHTML=""; empty.style.display="flex"; return; }
    empty.style.display="none";
    if(!rows.length){ list.innerHTML='<div class="empty"><div class="sub">No matches.</div></div>'; return; }
    list.innerHTML = rows.map(function(e){
      var t = esc(e.text);
      if(f){ try{ var re=new RegExp('('+f.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','ig');
        t = esc(e.text).replace(re,'<mark>$1</mark>'); }catch(_){ } }
      return '<div class="h-item" data-t="'+esc(e.text)+'">'+
        '<div class="h-head"><span class="h-ts">'+esc(e.ts)+'</span>'+
        '<span class="h-copy">Copy</span>'+
        '<span class="h-meta">'+esc(e.meta)+'</span></div>'+
        '<div class="h-text rtl-auto" dir="auto">'+t+'</div></div>';
    }).join('');
  }
  document.addEventListener("click", function(ev){
    var it = ev.target.closest(".h-item");
    if(it){ var txt = it.getAttribute("data-t");
      RK.ready(function(api){ api.copy(txt); }); RK.toast("Copied", "ok"); }
  });
  document.addEventListener("input", function(ev){
    if(ev.target.id==="q") render(ev.target.value);
  });
  RK.ready(function(api){
    try{ api.mark_ready(); }catch(e){}
    api.get_initial().then(function(d){
      ALL = (d && d.entries) || [];
      var c = RK.$("cnt"); if(c) c.innerHTML = '<span class="dot"></span>'+ALL.length+' entries';
      render("");
    });
  });
})();
"""


def build_window(webview, payload):
    api = HistoryApi()
    theme = (payload.get("theme") if isinstance(payload, dict) else None) or None
    html = uk.page("Lia History", BODY, extra_css=EXTRA_CSS, extra_js=APP_JS,
                   theme=theme)
    geo = uk.window_geometry("history", {"width": 760, "height": 680})
    kw = {"width": geo.get("width", 760), "height": geo.get("height", 680),
          "min_size": (520, 420)}
    if "x" in geo:
        kw["x"] = geo["x"]
        kw["y"] = geo["y"]
    win = webview.create_window("Lia History", html=html, js_api=api, **kw)
    uk.attach_geometry_memory(win, "history")
    return win


def main():
    uk.child_main(build_window, log_path=LOG_PATH, ready_check=lambda: _READY,
                  watchdog_s=30)


if __name__ == "__main__":
    main()
