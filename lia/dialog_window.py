"""Lia - small dialogs on the shared kit (UI redesign 2026-10-13, stage 6).

Replaces the dark Tk dialogs with windows in the app's own design language:
  mode "summary" - a summary to read and copy (Copy / Open full file / Save /
                   Close), shown even in silent mode: the summary IS the
                   deliverable.
  mode "rename"  - Rename speakers: one field per speaker, the invited names
                   as chips that fill the focused field, an optional voice
                   preview per speaker.
  mode "title"   - Name this meeting: one field, Save / Skip, auto-skip after
                   30 s without a keystroke.

The parent (lia.py) owns every action: the window only reports what the user
did. Child -> parent: one line per event on STDOUT ("@@LIA " + JSON):
  {"t":"action", "a":"open"|"save"|"play"|"rename"|"done", "data": ...}
  {"t":"closed"}
stdout stays the channel (never redirected); stderr goes to the log. Any
failure to open falls back, in the parent, to the old Tk dialog.

Launch: python -X utf8 dialog_window.py <payload.json>
ALWAYS launch with -X utf8 (Hebrew on a cp1252 stdout crashes).
"""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_kit as uk  # noqa: E402

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Lia")
LOG_PATH = os.path.join(CONFIG_DIR, "dialog_window.log")

_READY = False
_OUT_LOCK = threading.Lock()


def _emit(obj):
    """One event to the parent. Never raises (a closed pipe ends quietly)."""
    try:
        line = "@@LIA " + json.dumps(obj, ensure_ascii=True) + "\n"
        with _OUT_LOCK:
            sys.stdout.write(line)
            sys.stdout.flush()
    except Exception:
        pass


class DialogApi:
    def __init__(self, payload):
        self._p = payload or {}

    def get_initial(self):
        p = self._p
        return {k: p.get(k) for k in ("mode", "title", "text", "speakers", "suggestions",
                                      "has_play", "has_open", "has_save", "default")}

    def mark_ready(self):
        global _READY
        _READY = True
        return True

    def action(self, a, data=None):
        """Report a user action. Only the known names pass."""
        if a in ("open", "save", "play", "rename", "done"):
            _emit({"t": "action", "a": a, "data": data})
            return True
        return False

    def copy(self, text):
        return uk.copy_text(text)

    def close_window(self):
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
<div class="dwrap" id="dwrap">
  <div class="dtop"><h1 id="dtitle">__ORB__<span id="dttl" dir="auto"></span></h1>
    <span class="grow"></span><span class="dsub" id="dsub"></span></div>
  <div class="dbody" id="dbody"></div>
  <div class="dfoot" id="dfoot"></div>
</div>
"""

EXTRA_CSS = """
html,body{height:100%;}
.dwrap{display:flex; flex-direction:column; height:100vh;}
.dtop{display:flex; align-items:center; gap:12px; padding:14px 20px 12px; flex:none;
  background:rgba(255,255,255,.72); backdrop-filter:saturate(160%) blur(14px);
  border-bottom:1px solid rgba(23,23,40,.06);}
.dtop h1{margin:0; font-size:17px; font-weight:750; letter-spacing:-.2px; display:flex; align-items:center; gap:10px;
  min-width:0;}
.dtop h1 .orb{width:26px; height:26px;}
.dtop h1 span{overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.dtop .grow{flex:1 1 auto;}
.dtop .dsub{color:var(--faint); font-size:12px;}
.dbody{flex:1 1 auto; overflow:auto; padding:18px 20px;}
.dfoot{flex:none; display:flex; align-items:center; gap:8px; padding:12px 20px 16px; flex-wrap:wrap;}
.dfoot .grow{flex:1 1 auto;}
/* summary: the text as a document (escaped first, then a few line shapes) */
.sdoc{background:var(--card); border:1px solid var(--line); border-radius:var(--r-l); box-shadow:var(--shadow);
  padding:20px 24px; line-height:1.7; animation:rise .42s var(--ease) both;}
.sdoc h4{margin:16px 0 6px; font-size:13.5px; font-weight:700; unicode-bidi:plaintext; text-align:start;}
.sdoc h4:first-child{margin-top:0;}
.sdoc p{margin:0 0 8px; unicode-bidi:plaintext; text-align:start;}
.sdoc ul{margin:0 0 8px; padding:0; list-style:none;}
.sdoc li{position:relative; padding-inline-start:16px; margin:0 0 5px; unicode-bidi:plaintext; text-align:start;}
.sdoc li::before{content:""; position:absolute; inset-inline-start:3px; top:.7em; width:5px; height:5px;
  border-radius:50%; background:var(--faint);}
.sdoc li.task{padding-inline-start:24px;}
.sdoc li.task::before{display:none;}
.sdoc li.task .tb{position:absolute; inset-inline-start:0; top:.3em; width:15px; height:15px; border-radius:5px;
  border:1.5px solid var(--line-2); background:var(--card);}
.sdoc li.task.done{color:var(--muted); text-decoration:line-through;}
.sdoc li.task.done .tb{background:var(--ok); border-color:var(--ok);}
.sdoc.forced-rtl *{direction:rtl; text-align:right;}
.sdoc.forced-ltr *{direction:ltr; text-align:left;}
/* rename: one row per speaker */
.lead{color:var(--muted); font-size:13px; margin:0 0 14px;}
.spk{display:flex; flex-direction:column; gap:6px; padding:12px 14px; margin-bottom:10px; background:var(--card);
  border:1px solid var(--line); border-radius:var(--r-m); box-shadow:var(--shadow);
  animation:rise .36s var(--ease) both; animation-delay:var(--rd,0s);}
.spk .cap{font-size:12.5px; color:var(--muted); unicode-bidi:plaintext; overflow:hidden; text-overflow:ellipsis;}
.spk .cap b{color:var(--ink-2); font-weight:650;}
.spk .row{display:flex; gap:8px; align-items:center;}
.chips{display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 2px;}
.chips .chipb{appearance:none; border:1px solid var(--line-2); background:var(--card); color:var(--ink-2);
  border-radius:999px; padding:5px 12px; font:inherit; font-size:12.5px; cursor:pointer;
  transition:background var(--tr), transform var(--tr);}
.chips .chipb:hover{background:var(--card-2); transform:translateY(-1px);}
.subh{font-size:12px; font-weight:700; color:var(--faint); margin:14px 2px 4px; letter-spacing:.2px;}
/* title */
.tbox{max-width:520px;}
.tbox input{font-size:16px; padding:11px 14px; border-radius:var(--r-m);}
.tcount{color:var(--faint); font-size:12px; font-variant-numeric:tabular-nums;}
"""

APP_JS = r"""
(function(){
  var D = {}, done = false, autoskip = null, focused = null;
  var esc = RK.esc, $ = RK.$;
  function act(a, data){ RK.ready(function(api){ api.action(a, data === undefined ? null : data); }); }
  function closeWin(){ RK.ready(function(api){ api.close_window(); }); }

  // the summary as safe HTML: every line escaped FIRST, then "## x" -> heading,
  // "- [ ] x" -> task, "- x" -> bullet, **x** -> bold; nothing else interpreted
  function summaryHtml(md){
    var out = [], list = false;
    function close(){ if(list){ out.push('</ul>'); list = false; } }
    function inl(t){ return esc(t).replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>'); }
    String(md||'').split(/\r?\n/).forEach(function(line){
      var l = line.trim(), m;
      if(!l){ close(); return; }
      if((m = /^#{1,4}\s+(.*)$/.exec(l))){ close(); out.push('<h4 dir="auto">'+inl(m[1])+'</h4>'); return; }
      if((m = /^[-*]\s+\[( |x|X)\]\s+(.*)$/.exec(l))){
        if(!list){ out.push('<ul>'); list = true; }
        out.push('<li class="task'+(m[1]===' '?'':' done')+'" dir="auto"><span class="tb"></span>'+inl(m[2])+'</li>'); return; }
      if((m = /^[-*•]\s+(.*)$/.exec(l))){
        if(!list){ out.push('<ul>'); list = true; }
        out.push('<li dir="auto">'+inl(m[1])+'</li>'); return; }
      close(); out.push('<p dir="auto">'+inl(l)+'</p>');
    });
    close();
    return out.join('');
  }

  function renderSummary(){
    $("dttl").textContent = D.title || T("Summary");
    $("dbody").innerHTML = '<div class="sdoc" id="sdoc">'+summaryHtml(D.text)+'</div>';
    var f = '<button class="btn ghost sm" id="bDir" title="Switch the text direction">RTL / LTR</button>'+
            '<span class="grow"></span>'+
            '<button class="btn primary" id="bCopy">Copy</button>'+
            (D.has_open ? '<button class="btn" id="bOpen">Open full file</button>' : '')+
            (D.has_save ? '<button class="btn" id="bSave">Save</button>' : '')+
            '<button class="btn" id="bClose">Close</button>';
    $("dfoot").innerHTML = f;
    var dirs = ['', 'forced-rtl', 'forced-ltr'], di = 0;
    $("bDir").onclick = function(){ var s = $("sdoc"); s.classList.remove(dirs[di]); di = (di+1) % 3;
      if(dirs[di]) s.classList.add(dirs[di]); };
    $("bCopy").onclick = function(){ RK.ready(function(api){ Promise.resolve(api.copy(D.text||"")).then(function(ok){
      RK.toast(ok === false ? "Copy failed" : "Copied", ok === false ? "err" : "ok"); }); }); };
    if($("bOpen")) $("bOpen").onclick = function(){ act("open"); };
    if($("bSave")) $("bSave").onclick = function(){ act("save"); RK.toast("Saving...", "ok"); };
    $("bClose").onclick = closeWin;
    document.addEventListener("keydown", function(e){ if(e.key === "Escape") closeWin(); });
  }

  function renderRename(){
    $("dttl").textContent = T("Rename speakers");
    var sp = D.speakers || [];
    var h = '<p class="lead">Give each speaker a name. Leave a field empty to keep it as is.</p>';
    sp.forEach(function(s, i){
      var lab = s[0], hint = s[1];
      h += '<div class="spk" style="--rd:'+(i*40)+'ms"><div class="cap"><b>Speaker '+esc(lab)+'</b>'+
           (hint ? ' &middot; <span dir="auto">"'+esc(hint)+'&hellip;"</span>' : '')+'</div>'+
           '<div class="row">'+
           (D.has_play ? '<button class="btn icon sm ghost" data-play="'+esc(lab)+'" title="Play a sample of this voice" aria-label="Play a sample">'+
             RK.icon('play',{size:14})+'</button>' : '')+
           '<input type="text" dir="auto" data-lab="'+esc(lab)+'" placeholder="Name" autocomplete="off" spellcheck="false">'+
           '</div></div>';
    });
    if((D.suggestions||[]).length){
      h += '<div class="subh">Invited - click to fill the field you are on</div><div class="chips">'+
        D.suggestions.slice(0, 16).map(function(n){ return '<button class="chipb" data-fill="'+esc(n)+'" dir="auto">'+esc(n)+'</button>'; }).join('')+
        '</div>';
    }
    $("dbody").innerHTML = h;
    $("dfoot").innerHTML = '<span class="grow"></span><button class="btn" id="bCancel">Cancel</button>'+
                           '<button class="btn primary" id="bSave">Save</button>';
    var inputs = Array.prototype.slice.call(document.querySelectorAll('input[data-lab]'));
    focused = inputs[0] || null;
    inputs.forEach(function(inp){ inp.addEventListener("focus", function(){ focused = inp; }); });
    document.addEventListener("mousedown", function(e){
      var c = e.target.closest("[data-fill]");
      if(c){ e.preventDefault(); if(focused){ focused.value = c.getAttribute("data-fill"); focused.focus(); } return; }
      var p = e.target.closest("[data-play]");
      if(p){ e.preventDefault(); act("play", p.getAttribute("data-play")); }
    });
    function save(){
      var m = {}; inputs.forEach(function(inp){ m[inp.getAttribute("data-lab")] = inp.value; });
      act("rename", m); setTimeout(closeWin, 120);
    }
    $("bSave").onclick = save;
    $("bCancel").onclick = closeWin;
    document.addEventListener("keydown", function(e){
      if(e.key === "Enter"){ e.preventDefault(); save(); }
      if(e.key === "Escape"){ closeWin(); }
    });
    if(focused) setTimeout(function(){ focused.focus(); }, 60);
  }

  function renderTitle(){
    var ren = !!D.rename;            // Home's "Rename": no auto-skip, Cancel
    $("dttl").textContent = T(ren ? "Rename meeting" : "Name this meeting");
    $("dbody").innerHTML = '<div class="tbox"><p class="lead">'+(ren
        ? 'The new name is used for the meeting files.'
        : 'Used for the transcript file name. Skip to keep the date.')+'</p>'+
      '<input type="text" id="tIn" dir="auto" autocomplete="off" spellcheck="false" placeholder="Meeting name"></div>';
    $("dfoot").innerHTML = '<span class="tcount" id="tCount"></span><span class="grow"></span>'+
      '<button class="btn" id="bSkip">'+(ren ? 'Cancel' : 'Skip')+'</button><button class="btn primary" id="bSave">Save</button>';
    var inp = $("tIn"); inp.value = D.default || "";
    function fire(v){ if(done) return; done = true; act("done", v); setTimeout(closeWin, 120); }
    // auto-skip after 30 s, but every keystroke restarts the countdown
    function arm(){ if(ren) return;
      if(autoskip) clearTimeout(autoskip); autoskip = setTimeout(function(){ fire(""); }, 30000); }
    $("bSave").onclick = function(){ fire(inp.value); };
    $("bSkip").onclick = function(){ fire(""); };
    inp.addEventListener("keydown", function(e){
      arm();
      if(e.key === "Enter"){ e.preventDefault(); fire(inp.value); }
      if(e.key === "Escape"){ e.preventDefault(); fire(""); }
    });
    arm();
    setTimeout(function(){ inp.focus(); inp.select(); }, 60);
  }

  RK.ready(function(api){
    try{ api.mark_ready(); }catch(e){}
    api.get_initial().then(function(d){
      D = d || {};
      document.title = D.mode === "summary" ? (D.title || "Summary") :
                       D.mode === "rename" ? "Rename speakers" : "Name this meeting";
      if(D.mode === "summary") renderSummary();
      else if(D.mode === "rename") renderRename();
      else renderTitle();
    });
  });
})();
"""

_SIZES = {"summary": (720, 640, (460, 360)), "rename": (560, 560, (440, 340)),
          "title": (520, 300, (420, 260))}


def build_window(webview, payload):
    # the payload can carry a meeting summary: it lives on disk only until read
    try:
        if len(sys.argv) >= 2 and sys.argv[1] and os.path.basename(sys.argv[1]).startswith("lia_dlg_"):
            os.remove(sys.argv[1])
    except OSError:
        pass                      # the parent's own cleanup is the fallback
    api = DialogApi(payload)
    mode = payload.get("mode") if payload.get("mode") in _SIZES else "title"
    body = BODY.replace("__ORB__", uk.orb_svg())
    html = uk.page(payload.get("title") or "Lia", body, extra_css=EXTRA_CSS, extra_js=APP_JS)
    w, h, mn = _SIZES[mode]
    key = "dialog_" + mode
    geo = uk.window_geometry(key, {"width": w, "height": h})
    kw = {"width": geo.get("width", w), "height": geo.get("height", h), "min_size": mn}
    if "x" in geo:
        kw["x"], kw["y"] = geo["x"], geo["y"]
    win = webview.create_window(uk.ui_i18n.t(payload.get("title") or "Lia"), html=html, js_api=api,
                                text_select=True, **kw)
    uk.attach_geometry_memory(win, key)

    def _closed():
        _emit({"t": "closed"})
    try:
        win.events.closed += _closed
    except Exception:
        pass
    return win


def main():
    uk.install_stderr_sink(LOG_PATH)      # stdout stays the channel to the parent
    uk.child_main(build_window, ready_check=lambda: _READY, watchdog_s=30)


if __name__ == "__main__":
    main()
