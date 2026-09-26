# -*- coding: utf-8 -*-
"""Lia - Live meeting transcript: a window that shows the rolling transcript of
the meeting in progress and refreshes itself as new chunks land.

The meeting writes a LIVE .txt file (atomically) after every transcribed chunk,
AND a small status sidecar (<live>.status.json) that reports the AUTHORITATIVE
lifecycle phase + per-artifact readiness. This window reads BOTH directly (no
IPC): the LIVE file for the transcript body, the status file for the real phase.
It never infers "done" from a file disappearing - a vanished LIVE file only means
the rolling copy was cleaned up; the status file says whether the transcript and
summary are actually saved. Read-only: no LLM, no edits, no pipeline.

Phases it renders (from the status file): recording, processing, done,
ended_no_summary, failed, cancelled. It keeps the last transcript visible after
recording ends, and offers Open transcript / Open summary once each artifact is
actually on disk (opened by path, that meeting's files only).

Spawned by lia.py (_open_live_transcript). Geometry remembered ("livetranscript").
RTL-aware for Hebrew content (dir="auto" per line).

ALWAYS launch with  python -X utf8  (Hebrew stdout crashes under cp1252).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TERMINAL = ("done", "ended_no_summary", "failed", "cancelled")


class LiveApi:
    """Reads the LIVE transcript + the status sidecar and reports both cheaply.
    `version()` is the poll hook (stat only, both files). `get()` returns the
    transcript text plus the authoritative phase/readiness. `open_artifact()`
    opens the saved transcript/summary (that meeting's files only)."""

    def __init__(self, payload):
        self._payload = payload or {}
        self._live_path = self._payload.get("live_path") or ""
        self._status_path = (self._payload.get("status_path")
                             or ((self._live_path + ".status.json") if self._live_path else ""))
        self._seen_live = False

    def meta(self):
        return {
            "title": self._payload.get("title") or "Live transcript - Lia",
            "started": self._payload.get("started") or "",
            "chunk_seconds": int(self._payload.get("chunk_seconds") or 15),
        }

    @staticmethod
    def _stat(path):
        try:
            st = os.stat(path)
            return (st.st_mtime, st.st_size)
        except OSError:
            return (0.0, 0)

    def version(self):
        lm, ls = self._stat(self._live_path)
        sm, ss = self._stat(self._status_path)
        if lm:
            self._seen_live = True
        return "%.3f:%d:%.3f:%d:%d" % (lm, ls, sm, ss, 1 if self._seen_live else 0)

    def _read_status(self):
        if not self._status_path:
            return None
        try:
            with open(self._status_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _read_live(self):
        if not self._live_path:
            return None
        try:
            with open(self._live_path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def get(self):
        status = self._read_status()
        text = self._read_live()
        if text is not None:
            self._seen_live = True
        if status:
            tr = status.get("transcript") or {}
            su = status.get("summary") or {}
            return {
                "status": "ok",
                "phase": status.get("phase") or "recording",
                "text": text or "",
                "have_live": text is not None,
                "transcript_ready": bool(tr.get("ready")),
                "transcript_path": tr.get("path"),
                "summary_ready": bool(su.get("ready")),
                "summary_path": su.get("path"),
                "summarize": bool(status.get("summarize")),
                "error": status.get("error"),
            }
        # No status file (older meeting / very early): legacy inference.
        if text is not None:
            return {"status": "ok", "phase": "recording", "text": text, "have_live": True}
        return {"status": "ok",
                "phase": "ended" if self._seen_live else "waiting",
                "text": "", "have_live": False}

    def open_artifact(self, which):
        """Open the saved transcript ('transcript') or summary ('summary'). Only
        opens a file that this meeting's status names AND that sits in the same
        folder as the status file (no arbitrary path)."""
        status = self._read_status() or {}
        art = (status.get("transcript") if which == "transcript"
               else status.get("summary")) or {}
        path = art.get("path")
        if not path:
            return {"ok": False}
        base = os.path.dirname(os.path.abspath(self._status_path or self._live_path))
        try:
            full = os.path.abspath(path)
            if not os.path.exists(full):
                return {"ok": False}
            if os.path.commonpath([base, full]) != base:
                return {"ok": False}
            if os.path.splitext(full)[1].lower() not in (".txt", ".md", ".html", ".htm"):
                return {"ok": False}          # a transcript / summary only (F25)
            os.startfile(full)   # noqa: S606 - user-initiated, path is app-owned
            return {"ok": True}
        except Exception:
            return {"ok": False}

    def copy_text(self, text):
        """'Copy all' - put the transcript on the clipboard. From Python, not
        navigator.clipboard (not reliable in a WebView loaded from an HTML
        string). Capped so a runaway page can't flood the clipboard."""
        text = (text or "")[:2_000_000]
        if not text.strip():
            return {"ok": False}
        try:
            import pyperclip
            pyperclip.copy(text)
            return {"ok": True, "chars": len(text)}
        except Exception:
            return {"ok": False}



def _with_csp(html):
    """The page with ui_kit's Content-Security-Policy (audit 2026-09-26 F13)."""
    import ui_kit
    return ui_kit.with_csp(html)

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{ --bg:#FFFFFF; --panel:#F4F6FA; --line:#E4E7EE; --fg:#1B2430; --muted:#6B7280;
         --accent:#2563EB; --live:#E5484D; --ok:#1a7f37; --warn:#b06a00; --ts:#8A94A6; }
  *{ box-sizing:border-box; }
  body{ margin:0; background:var(--bg); color:var(--fg); height:100vh; display:flex; flex-direction:column;
        font-family:"Segoe UI Variable Text","Segoe UI",Heebo,system-ui,Arial,sans-serif; font-size:15px; }
  header{ padding:10px 14px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:10px;
          background:var(--panel); flex:none; }
  header .dot{ width:9px; height:9px; border-radius:50%; background:var(--live); flex:none;
               box-shadow:0 0 0 0 rgba(229,72,77,.6); animation:pulse 1.6s infinite; }
  header .dot.idle{ animation:none; background:var(--muted); box-shadow:none; }
  header .dot.ok{ animation:none; background:var(--ok); box-shadow:none; }
  @keyframes pulse{ 0%{box-shadow:0 0 0 0 rgba(229,72,77,.55);} 70%{box-shadow:0 0 0 7px rgba(229,72,77,0);}
                    100%{box-shadow:0 0 0 0 rgba(229,72,77,0);} }
  header h1{ font-size:14px; margin:0; font-weight:700; }
  header .sub{ color:var(--muted); font-size:12px; margin-inline-start:auto; text-align:end; }
  .wrap{ flex:1; overflow:auto; padding:12px 16px 22px; scroll-behavior:smooth;
         user-select:text; -webkit-user-select:text; cursor:text; }
  .wrap *{ user-select:text; -webkit-user-select:text; }
  .ln{ line-height:1.6; margin:0 0 7px; word-break:break-word; white-space:pre-wrap; }
  .ln .ts{ color:var(--ts); font-variant-numeric:tabular-nums; font-size:12px; margin-inline-end:8px; }
  .banner{ color:var(--muted); text-align:center; margin-top:48px; font-size:14px; line-height:1.7; padding:0 20px; }
  .banner.ended{ color:var(--fg); }
  .foot{ flex:none; border-top:1px solid var(--line); background:var(--panel); color:var(--muted);
         font-size:12px; padding:8px 14px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .foot .phase{ display:inline-flex; align-items:center; gap:6px; }
  .foot .spin{ width:12px; height:12px; border-radius:50%; border:2px solid var(--line);
               border-top-color:var(--accent); animation:spin .8s linear infinite; }
  @keyframes spin{ to{ transform:rotate(360deg); } }
  .foot .grow{ flex:1; }
  .foot button{ appearance:none; border:1px solid var(--line); background:var(--bg); color:var(--accent);
                cursor:pointer; font-size:12px; font-weight:600; padding:5px 10px; border-radius:8px; font-family:inherit; }
  .foot button:hover{ background:var(--panel); }
  .foot button.follow{ margin-inline-start:auto; border-color:transparent; background:transparent; }
  .foot button.follow.off{ color:var(--muted); }
  .foot .err{ color:var(--warn); }
  @media (prefers-reduced-motion: reduce){ *{ animation-duration:.001ms !important; } }
</style></head>
<body>
  <header>
    <span class="dot" id="dot"></span>
    <h1 id="title">Live transcript</h1>
    <span class="sub" id="sub"></span>
  </header>
  <div class="wrap" id="wrap"><div class="banner">Loading&hellip;</div></div>
  <div class="foot" id="foot"><span id="stat">&nbsp;</span></div>
<script>
// The pywebview api bridge is undefined at parse time (mirrors action_items /
// email_search): assign it only inside handlers, fired by pywebviewready.
var API = null;
var lastVer = null;
var lastText = '';        // cache so the transcript stays visible after cleanup
var follow = true;        // stick to the tail unless the user scrolls up
var stopped = false;      // stop polling once a terminal phase is reached
var META = {chunk_seconds:15, started:'', title:'Live transcript'};

function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

function stripHead(text){
  var i = text.indexOf('\n\n');
  return i >= 0 ? text.slice(i + 2) : text;
}
function lineHtml(raw){
  var m = raw.match(/^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*([\s\S]*)$/);
  if(m){ return '<div class="ln" dir="auto"><span class="ts" dir="ltr">'+esc(m[1])+'</span>'+esc(m[2])+'</div>'; }
  return '<div class="ln" dir="auto">'+esc(raw)+'</div>';
}
function atBottom(el){ return el.scrollHeight - el.scrollTop - el.clientHeight < 40; }

function selectingInWrap(){
  // A non-empty selection inside the transcript: the user is copying.
  var sel = window.getSelection && window.getSelection();
  if(!sel || sel.isCollapsed || !sel.rangeCount) return false;
  var wrap = document.getElementById('wrap');
  return wrap.contains(sel.getRangeAt(0).commonAncestorContainer);
}
var pendingBody = null;   // a redraw held back while the user has text selected
function renderTranscript(body){
  var wrap = document.getElementById('wrap');
  if(selectingInWrap()){ pendingBody = body; return; }
  pendingBody = null;
  body = (body||'').replace(/\s+$/,'');
  if(!body){
    wrap.innerHTML = '<div class="banner">Waiting for the first chunk (~'+META.chunk_seconds+'s)&hellip;</div>';
    return;
  }
  var lines = body.split('\n').filter(function(l){ return l.trim().length; });
  wrap.innerHTML = lines.map(lineHtml).join('');
  if(follow) wrap.scrollTop = wrap.scrollHeight;
}

function setDot(kind){ // 'live' | 'idle' | 'ok'
  var d = document.getElementById('dot');
  d.className = 'dot' + (kind==='idle'?' idle':(kind==='ok'?' ok':''));
  d.style.display = '';
}

function openBtn(which, label){
  return '<button data-open="'+which+'">'+esc(label)+'</button>';
}
function followBtn(){
  return '<button class="follow'+(follow?'':' off')+'" id="followBtn">'+(follow?'▼ Following':'● Paused')+'</button>';
}

function renderFoot(r){
  var foot = document.getElementById('foot');
  var phase = r.phase;
  var html = '';
  if(phase==='recording' || phase==='waiting'){
    setDot('live');
    document.getElementById('title').textContent = META.title || 'Live transcript';
    html = '<span class="phase">Updating every ~'+META.chunk_seconds+'s</span><span class="grow"></span>'+followBtn();
  } else if(phase==='processing'){
    setDot('idle');
    html = '<span class="phase"><span class="spin"></span>Processing'+(r.summarize?' (transcribing / summarizing)':'')+'&hellip;</span>'+
           '<span class="grow"></span>'+followBtn();
  } else if(phase==='done'){
    setDot('ok');
    document.getElementById('title').textContent = 'Meeting saved';
    html = '<span class="phase">✓ Saved</span><span class="grow"></span>'+
           (r.transcript_ready?openBtn('transcript','Open transcript'):'')+
           (r.summary_ready?openBtn('summary','Open summary'):'');
  } else if(phase==='ended_no_summary'){
    setDot('ok');
    document.getElementById('title').textContent = 'Transcript saved';
    html = '<span class="phase">✓ Transcript saved (no summary requested)</span><span class="grow"></span>'+
           (r.transcript_ready?openBtn('transcript','Open transcript'):'');
  } else if(phase==='failed'){
    setDot('idle');
    document.getElementById('title').textContent = 'Meeting';
    html = '<span class="phase err">⚠ '+esc(r.error||'Something went wrong')+'</span><span class="grow"></span>'+
           (r.transcript_ready?openBtn('transcript','Open transcript'):'');
  } else if(phase==='cancelled'){
    setDot('idle');
    document.getElementById('title').textContent = 'Meeting discarded';
    html = '<span class="phase">Discarded - no transcript saved</span>';
  } else { // legacy 'ended'
    setDot('ok');
    document.getElementById('title').textContent = 'Meeting ended';
    html = '<span class="phase">✓ Saved</span>';
  }
  if(phase!=='cancelled' && lastText) html = '<button id="copyAllBtn">Copy all</button>' + html;
  foot.innerHTML = html;
}

function render(r){
  if(r.phase==='cancelled'){
    document.getElementById('wrap').innerHTML =
      '<div class="banner ended">The meeting was discarded.<br>No transcript was saved.</div>';
  } else if(r.phase==='waiting'){
    renderTranscript('');
  } else {
    // keep whatever text we last had if the LIVE file is gone (processing/done)
    var body = stripHead(r.text || '');
    if(body.replace(/\s+/g,'')) lastText = body;
    renderTranscript(lastText);
  }
  renderFoot(r);
}

async function refresh(){
  if(!API) API = window.pywebview.api;
  var r = await API.get();
  render(r);
  if(r.phase && ['done','ended_no_summary','failed','cancelled','ended'].indexOf(r.phase)>=0){
    stopped = true;
    if(window._pollTimer){ clearInterval(window._pollTimer); window._pollTimer=null; }
  }
}
async function poll(){
  try{
    if(!API || stopped) return;
    var v = await API.version();
    if(v !== lastVer){ lastVer = v; await refresh(); }
  }catch(e){}
}

function setFollow(on){
  follow = on;
  var el = document.getElementById('followBtn');
  if(el){ el.classList.toggle('off', !on); el.innerHTML = on ? '▼ Following' : '● Paused'; }
  if(on){ var w = document.getElementById('wrap'); w.scrollTop = w.scrollHeight; }
}

document.addEventListener('selectionchange', function(){
  // selection cleared -> apply the redraw we held back
  if(pendingBody !== null && !selectingInWrap()){ var b = pendingBody; renderTranscript(b); }
});
function transcriptText(){
  // what the user sees: "[m:ss] text" per line
  var out = [];
  document.querySelectorAll('#wrap .ln').forEach(function(ln){
    var ts = ln.querySelector('.ts');
    var t = ts ? ts.textContent : '';
    var body = ln.textContent.slice(t.length).trim();
    out.push(t ? ('['+t+']' + (body ? ' '+body : '')) : body);
  });
  return out.join('\n');
}
async function copyAll(btn){
  if(!API) return;
  var r = {ok:false};
  try{ r = await API.copy_text(transcriptText()); }catch(e){}
  if(btn){ btn.textContent = r.ok ? 'Copied ✓' : 'Nothing to copy';
           setTimeout(function(){ btn.textContent = 'Copy all'; }, 1600); }
}
document.getElementById('wrap').addEventListener('scroll', function(){
  var w = document.getElementById('wrap');
  if(atBottom(w)){ if(!follow) setFollow(true); }
  else if(follow){ setFollow(false); }
});
document.getElementById('foot').addEventListener('click', async function(ev){
  var f = ev.target.closest('#followBtn'); if(f){ setFollow(!follow); return; }
  var c = ev.target.closest('#copyAllBtn'); if(c){ copyAll(c); return; }
  var o = ev.target.closest('[data-open]');
  if(o && API){ try{ await API.open_artifact(o.getAttribute('data-open')); }catch(e){} }
});

window.addEventListener('pywebviewready', async function(){
  if(!API) API = window.pywebview.api;
  try{
    META = await API.meta();
    document.getElementById('title').textContent = META.title || 'Live transcript';
    document.getElementById('sub').textContent = META.started ? ('Started ' + META.started) : '';
  }catch(e){}
  lastVer = await API.version();
  await refresh();
  window._pollTimer = setInterval(poll, 1000);
});
</script>
</body></html>"""


def main():
    try:
        import webview
    except Exception as e:  # pragma: no cover
        sys.stderr.write("live_transcript: pywebview not available: %r\n" % (e,))
        sys.exit(2)

    try:
        import ui_kit as uk
    except Exception:
        uk = None

    payload = uk.read_payload() if uk is not None else _read_payload_fallback()

    if uk is not None:
        try:
            log_dir = os.path.dirname(payload.get("live_path") or "") or os.getcwd()
            uk.install_stdout_sink(os.path.join(log_dir, "live_transcript.log"))
        except Exception:
            pass

    api = LiveApi(payload)
    default_geo = {"width": 560, "height": 680}
    geo = (uk.window_geometry("livetranscript", default_geo)
           if uk is not None else default_geo)
    kw = {"width": int(geo.get("width", 560)), "height": int(geo.get("height", 680)),
          "min_size": (380, 360)}
    if isinstance(geo.get("x"), int) and isinstance(geo.get("y"), int):
        kw["x"], kw["y"] = geo["x"], geo["y"]
    try:
        # text_select=True: pywebview's default (False) injects user-select:none
        # into the WHOLE window - the live transcript could not be copied.
        win = webview.create_window(payload.get("title", "Live transcript - Lia"),
                                    html=_with_csp(HTML), js_api=api, text_select=True, **kw)
        if uk is not None:
            uk.attach_geometry_memory(win, "livetranscript")
            uk.webview_start(webview)
        else:
            webview.start()
    except Exception as e:
        sys.stderr.write("live_transcript: failed: %r\n" % (e,))
        sys.exit(3)


def _read_payload_fallback(argv=None):
    """Minimal payload read when ui_kit is unavailable (mirrors ui_kit.read_payload)."""
    argv = sys.argv if argv is None else argv
    if len(argv) >= 2 and argv[1]:
        try:
            with open(argv[1], encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


if __name__ == "__main__":
    main()
