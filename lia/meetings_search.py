# -*- coding: utf-8 -*-
"""Lia - "Ask your meetings" window (pywebview, RTL, Hebrew-first).

A focused RAG panel over the local meeting archive: type a question, get a
grounded answer with inline [n] citations and a clickable source list (click a
source to open the meeting file). Retrieval is always local (bge-m3 via Ollama);
the answer model is whatever the user picked (local gemma4 / free Gemini or Groq
/ paid OpenAI), passed in via the payload.

Design mirrors chat_window.py: a single HTML string + a js_api bridge + a
pywebview edgechromium window, spawned as its own subprocess by the tray.

ALWAYS launch with  python -X utf8  (Hebrew stdout crashes under cp1252).
"""

from __future__ import annotations

import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meetings_index as mi
import email_index as ei
import secret_store       # DPAPI unprotect for the payload's provider keys

try:
    import webview
except Exception as e:  # pragma: no cover
    sys.stderr.write("meetings_search: pywebview not available: %r\n" % (e,))
    sys.exit(2)

_WINDOW = None


class MeetingsApi:
    """JS<->Python bridge for the ask panel."""

    def __init__(self, payload):
        self._embed_url = payload.get("embed_base_url") or ei.DEFAULT_OLLAMA
        # providers: [{label, base_url, model, key}] - the answer-model dropdown.
        # Keys arrive DPAPI-protected (never plaintext on disk) - unprotect here.
        self._providers = [dict(p, key=secret_store.unprotect(p.get("key") or ""))
                           for p in (payload.get("providers") or [])] or [
            {"label": "Gemma 4 31B (local)",
             "base_url": ei.DEFAULT_OLLAMA, "model": "gemma4:31b-it-qat", "key": "ollama"}]
        self._default = int(payload.get("default_provider", 0) or 0)
        self._top_k = int(payload.get("top_k", 20) or 20)
        # Auto-route factual / synthesis / action-item questions (kill-switch).
        self._route = bool(payload.get("intent_routing", True))
        self._lock = threading.Lock()

    # ---- initial state ----
    def get_initial(self):
        conn, _v = mi.open_read()
        count = 0
        recent = []
        if conn is not None:
            try:
                count = ei.stats(conn)["messages"]
                for r in mi.recent(conn, 30):
                    recent.append({
                        "entry_id": r["entry_id"], "title": r["subject"],
                        "date": mi._fmt_date(r["received_ts"]), "kind": r["folder"]})
            except Exception:
                pass
            finally:
                conn.close()
        return {
            "count": count,
            "recent": recent,
            "providers": [p["label"] for p in self._providers],
            "default_provider": min(self._default, len(self._providers) - 1),
            "status": self._status(),
        }

    def _status(self):
        try:
            with open(mi.STATUS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def status(self):
        return self._status()

    # ---- the ask (multi-turn: `history` is prior [{q,a}] turns) ----
    def ask(self, question, provider_idx, history=None):
        question = (question or "").strip()
        if not question:
            return {"error": "empty"}
        with self._lock:
            conn, vconn = mi.open_read()
            if conn is None:
                return {"error": "no_index"}
            try:
                p = self._providers[int(provider_idx)]
            except (IndexError, ValueError, TypeError):
                p = self._providers[0]
            try:
                res = mi.ask(
                    conn, vconn, question,
                    embed_base_url=self._embed_url,
                    answer_base_url=p["base_url"], answer_model=p["model"],
                    answer_key=p.get("key") or "ollama", top_k=self._top_k,
                    history=history or [], route=self._route)
            except Exception as e:
                return {"error": "ask_failed", "detail": str(e)}
            finally:
                try:
                    conn.close()
                    if vconn is not None:
                        vconn.close()
                except Exception:
                    pass
            sources = [{
                "n": i + 1, "entry_id": s["entry_id"], "title": s["subject"],
                "date": mi._fmt_date(s["received_ts"]),
                "ts": s.get("ts") or "",
                "speakers": s.get("sender_name") or "",
                "kind": s.get("folder") or "",
                "score": s.get("score", 0.0),
            } for i, s in enumerate(res["sources"])]
            return {"answer": res["answer"], "sources": sources,
                    "model": res["model"], "mode": res.get("mode", "")}

    # ---- open a meeting file in the default editor ----
    def open_meeting(self, entry_id):
        try:
            base = os.path.basename(entry_id or "")   # never allow path escape
            path = os.path.join(mi.MEETINGS_SRC_DIR, base)
            if os.path.exists(path):
                os.startfile(path)
                return True
        except Exception as e:
            sys.stderr.write("open_meeting failed: %r\n" % (e,))
        return False



def _with_csp(html):
    """The page with ui_kit's Content-Security-Policy (audit 2026-09-26 F13)."""
    import ui_kit
    return ui_kit.brand_html(html)

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  /* the Lia Lite language (ui_kit.brand_html adds the typeface, Lia's tokens + orb) */
  :root{ --bg:#F6F6F3; --panel:#FFFFFF; --line:#EAEAE5; --line-2:#DADAD3; --fg:#17171E; --fg-2:#3A3C46;
         --muted:#6D6F78; --faint:#A3A5AC; --accent:#272A4A; --chip:#F0F0EC; }
  *{ box-sizing:border-box; }
  body{ margin:0; background:var(--bg); color:var(--fg);
        font-family:"Heebo","Segoe UI Variable Text","Segoe UI",system-ui,Arial,sans-serif;
        -webkit-font-smoothing:antialiased; height:100vh; display:flex; flex-direction:column; }
  header{ padding:14px 22px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;
          background:rgba(255,255,255,.72); backdrop-filter:saturate(160%) blur(14px);
          border-bottom:1px solid rgba(23,23,40,.06); }
  header h1{ font-size:17px; margin:0; font-weight:750; letter-spacing:-.2px; display:flex; align-items:center; gap:10px; }
  header h1 .orb{ width:26px; height:26px; }
  header .count{ color:var(--faint); font-size:12px; font-variant-numeric:tabular-nums; }
  select{ background:var(--panel); color:var(--fg); border:1px solid var(--line-2); border-radius:10px;
          padding:7px 10px; font-size:13px; font-family:inherit; margin-inline-start:auto; cursor:pointer; }
  select:focus{ outline:none; border-color:rgba(106,91,226,.55); box-shadow:var(--ring); }
  .wrap{ flex:1; overflow:auto; padding:22px 26px; }
  .askbar{ padding:14px 22px 18px; display:flex; gap:10px; align-items:flex-end; }
  .askbox{ flex:1; display:flex; gap:8px; align-items:center; background:var(--panel); border:1px solid var(--line-2);
           border-radius:16px; padding:6px 6px 6px 16px; box-shadow:var(--shadow);
           transition:border-color .16s var(--ease), box-shadow .16s var(--ease); }
  .askbox:focus-within{ border-color:rgba(106,91,226,.55); box-shadow:var(--ring), var(--shadow); }
  textarea{ flex:1; resize:none; background:transparent; color:var(--fg); border:0; outline:none;
            padding:9px 0; font-size:15px; font-family:inherit; height:42px; line-height:1.5; }
  textarea::placeholder{ color:var(--faint); }
  button{ background:var(--accent); color:#FFFFFF; border:0; border-radius:12px; padding:0 20px;
          font-size:14px; font-weight:600; font-family:inherit; cursor:pointer; height:40px; }
  button:disabled{ opacity:.5; cursor:default; }
  .atext{ display:block; }
  .answer{ background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:18px 20px;
           font-size:15px; line-height:1.75; white-space:pre-wrap; box-shadow:var(--shadow);
           animation:rise .42s var(--ease) both; }
  .answer .cite{ display:inline-block; background:var(--lia-soft); color:var(--lia-ink); border-radius:6px;
                 padding:0 7px; font-size:12px; font-weight:700; cursor:pointer; margin:0 2px;
                 font-variant-numeric:tabular-nums; transition:background .16s; }
  .answer .cite:hover{ background:#E6E1FB; }
  .modelline{ color:var(--faint); font-size:12px; margin:10px 2px 18px; }
  .srchead{ color:var(--faint); font-size:12px; font-weight:700; margin:6px 2px 8px; letter-spacing:.2px; }
  .src{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:11px 14px;
        margin-bottom:8px; cursor:pointer; transition:border-color .16s, box-shadow .16s, transform .16s; }
  .src:hover{ border-color:var(--line-2); box-shadow:var(--shadow); transform:translateY(-1px); }
  .src .t{ font-weight:600; } .src .m{ color:var(--muted); font-size:12px; margin-top:3px; }
  .src .num{ color:var(--lia-ink); font-weight:700; margin-inline-end:6px; font-variant-numeric:tabular-nums; }
  .empty{ color:var(--muted); text-align:center; margin:9vh auto 0; max-width:520px; }
  .empty .orb{ width:64px; height:64px; display:block; margin:0 auto 16px; }
  .empty .lead{ font-size:15px; color:var(--fg-2); }
  .recent{ margin-top:18px; text-align:start; } .recent .r{ padding:7px 12px; border-radius:10px; cursor:pointer; color:var(--muted); font-size:13.5px; }
  .recent .r:hover{ background:var(--panel); color:var(--fg); }
  .spin{ color:var(--muted); display:inline-flex; align-items:center; gap:10px; }
  .spin .orb{ width:20px; height:20px; }
  .turn{ margin-bottom:24px; }
  .q{ font-weight:650; font-size:15px; margin:0 2px 10px; padding-inline-start:12px; position:relative; }
  .q::before{ content:""; position:absolute; inset-inline-start:0; top:2px; bottom:2px; width:3px; border-radius:3px;
              background:var(--lia-grad); }
  .ts{ color:var(--lia-ink); font-weight:600; font-variant-numeric:tabular-nums; }
  .badge{ display:inline-flex; align-items:center; gap:6px; font-size:11px; font-weight:700; border-radius:999px;
          padding:2px 10px; margin-bottom:6px; background:var(--chip); color:var(--fg-2); }
  .badge::before{ content:""; width:6px; height:6px; border-radius:50%; background:var(--faint); }
  .badge-synthesis::before{ background:var(--lia); }
  .badge-action::before{ background:#23845A; }
  .clr{ background:transparent; color:var(--muted); border:1px solid var(--line-2); border-radius:10px;
        padding:0 12px; height:32px; font-size:12.5px; cursor:pointer; margin-inline-start:8px; }
  .clr:hover{ background:var(--panel); color:var(--fg); }
  .hint{ color:var(--faint); font-size:11.5px; margin-inline-end:auto; }
  ::-webkit-scrollbar{ width:12px; } ::-webkit-scrollbar-thumb{ background:#D0D0C9; border-radius:8px; border:4px solid var(--bg); }
</style></head>
<body>
  <header>
    <h1><span class="orb">__LIA_ORB__</span>Ask your meetings</h1>
    <span class="count" id="count"></span>
    <span class="hint">Filters: <bdi dir="ltr" class="no-i18n">after:2026-08-01 · speaker:"Speaker A"</bdi></span>
    <select id="model"></select>
    <button class="clr" id="clr" title="New conversation">Clear</button>
  </header>
  <div class="wrap" id="wrap">
    <div id="thread"></div>
    <div class="empty" id="empty"><span class="orb">__LIA_ORB__</span><span class="lead">Ask anything about your meetings - every answer is backed by cited sources.</span>
      <div class="recent" id="recent"></div>
    </div>
  </div>
  <div class="askbar">
    <div class="askbox">
      <textarea id="q" dir="auto" placeholder="What was decided about...? (follow-ups keep the thread)" autofocus></textarea>
      <button id="go" class="lia-btn">Ask</button>
    </div>
  </div>
<script>
// window.pywebview is NOT populated when this inline script first runs; touching
// .api here throws and aborts the ENTIRE script (dead dropdown/buttons/Enter).
// Assign it inside the pywebviewready handler, like email_search / chat_window do.
var API = null;
var busy = false;
var convo = [];   // [{q, a}] prior turns for follow-up context
// Full escape incl. quotes (mirrors RK.esc) - values land in data-* attributes.
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// No interpolated inline handlers (a quote in a meeting title/id must never
// reach executable JS): ids ride in data-* attributes, one delegated click
// listener on #wrap routes cites + source cards + recent rows.
function renderAnswer(text){
  return esc(text).replace(/\[(\d+)\]/g, function(_,n){
    return '<span class="cite" data-n="'+n+'">['+n+']</span>'; });
}
function jump(n){ var el=document.getElementById('src-'+n); if(el){ el.scrollIntoView({behavior:'smooth',block:'center'}); el.style.borderColor='var(--lia)'; } }

var MODE_BADGE = { factual:'Factual', synthesis:'Synthesis', action:'Action items' };
function modeBadge(mode){
  var m = MODE_BADGE[mode];
  if(!m) return '';
  return '<span class="badge badge-'+mode+'">'+m+'</span>\n';
}
var ORB = '__LIA_ORB__';

function srcCard(s){
  var ts = s.ts ? (' · <span class="ts">~'+esc(s.ts)+'</span>') : '';
  return '<div class="src" id="src-'+s.n+'" data-eid="'+esc(s.entry_id)+'">'
    + '<div class="t" dir="auto"><span class="num">['+s.n+']</span>'+esc(s.title)+'</div>'
    + '<div class="m"><span dir="auto">'+esc(s.date)+ts+(s.speakers?(' · '+esc(s.speakers)):'')+'</span> · <span>'+esc(s.kind)+'</span></div></div>';
}
document.getElementById('wrap').addEventListener('click', function(ev){
  var cite = ev.target.closest('.cite');
  if(cite && cite.dataset.n){ jump(parseInt(cite.dataset.n)); return; }
  var card = ev.target.closest('[data-eid]');
  if(card){ API.open_meeting(card.dataset.eid); }
});

function scrollEnd(){ var w=document.getElementById('wrap'); w.scrollTop = w.scrollHeight; }
/* "GPU busy - ..." while a local answer waits for the GPU (ollama_gate) */
window.__gpuWait = function(t){
  var spins = document.querySelectorAll('#thread .answer .spin');
  var s = spins.length ? spins[spins.length-1] : null;
  var st = s ? s.querySelector('.spin-t') : null;
  if(st){ st.textContent = t; } else if(s){ s.textContent = t; }
};

async function doAsk(){
  var qEl = document.getElementById('q');
  var q = qEl.value.trim();
  if(!q || busy) return;
  busy = true; document.getElementById('go').disabled = true;
  document.getElementById('empty').style.display = 'none';
  var midx = document.getElementById('model').value;
  var thread = document.getElementById('thread');
  var turn = document.createElement('div'); turn.className = 'turn';
  turn.innerHTML = '<div class="q" dir="auto">'+esc(q)+'</div><div class="answer"><span class="spin"><span class="orb busy">'+ORB+'</span><span class="spin-t">Thinking…</span></span></div>';
  thread.appendChild(turn); qEl.value=''; scrollEnd();
  try{
    var r = await API.ask(q, parseInt(midx), convo.slice(-3));
    var ansEl = turn.querySelector('.answer');
    if(r.error === 'no_index'){ ansEl.innerHTML = 'The index is still building. Try again in a moment.'; }
    else if(r.error === 'empty'){ turn.remove(); }
    else if(r.error){ ansEl.innerHTML = 'Error: '+esc(r.error)+(r.detail?(' - '+esc(r.detail)):''); }
    else {
      var h = modeBadge(r.mode) + '<div class="atext" dir="auto">' + renderAnswer(r.answer) + '</div></div><div class="modelline">Model: '+esc(r.model)+'</div>';
      if(r.sources && r.sources.length){
        h += '<div class="srchead">Sources ('+r.sources.length+'):</div>' + r.sources.map(srcCard).join('');
      }
      ansEl.innerHTML = h;
      convo.push({q:q, a:r.answer});
    }
  }catch(e){ turn.querySelector('.answer').innerHTML = 'Error: '+esc(''+e); }
  busy = false; document.getElementById('go').disabled = false; scrollEnd(); qEl.focus();
}

function clearConvo(){ convo=[]; document.getElementById('thread').innerHTML=''; document.getElementById('empty').style.display=''; }

document.getElementById('go').onclick = doAsk;
document.getElementById('clr').onclick = clearConvo;
document.getElementById('q').addEventListener('keydown', function(e){
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); doAsk(); }
});

function fillRecent(list){
  var el = document.getElementById('recent');
  el.innerHTML = (list||[]).slice(0,12).map(function(m){
    return '<div class="r" dir="auto" data-eid="'+esc(m.entry_id)+'">'+esc(m.title)+'  ·  '+esc(m.date)+'</div>';
  }).join('');
}

window.addEventListener('pywebviewready', async function(){
  API = window.pywebview.api;
  var init = await API.get_initial();
  document.getElementById('count').textContent = init.count ? (init.count+' meetings') : '';
  var sel = document.getElementById('model');
  sel.innerHTML = (init.providers||[]).map(function(p,i){ return '<option value="'+i+'">'+esc(p)+'</option>'; }).join('');
  sel.value = init.default_provider || 0;
  fillRecent(init.recent);
  if(!init.count){ document.querySelector('#empty .lead').textContent = 'The index is still building - your meetings are loading…'; }
});
</script>
</body></html>"""


def main():
    global _WINDOW
    if sys.stdout is None or sys.stderr is None:
        try:
            mi.ensure_dir()
            _sink = open(os.path.join(mi.MEET_INDEX_DIR, "meetings_search.log"), "a",
                         encoding="utf-8")
        except Exception:
            _sink = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = _sink
        if sys.stderr is None:
            sys.stderr = _sink

    payload = {}
    if len(sys.argv) >= 2:
        try:
            with open(sys.argv[1], encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            sys.stderr.write("meetings_search: bad payload: %r\n" % (e,))
        try:
            os.remove(sys.argv[1])   # consumed - don't leave it on disk
        except OSError:
            pass                     # the parent's timed cleanup is the fallback

    api = MeetingsApi(payload)
    import ui_i18n  # the interface language (window title)
    try:
        _WINDOW = webview.create_window(
            ui_i18n.t(payload.get("title", "Ask your meetings")), html=_with_csp(HTML), js_api=api,
            width=900, height=780, min_size=(560, 480))
        import ollama_gate
        ollama_gate.UI["on_wait"] = lambda t: _WINDOW.evaluate_js(
            "window.__gpuWait && window.__gpuWait(%s)" % json.dumps(t))
        import ui_kit as uk  # Lia window icon
        uk.webview_start(webview)
    except Exception as e:
        sys.stderr.write("meetings_search: failed: %r\n" % (e,))
        sys.exit(3)


if __name__ == "__main__":
    main()
