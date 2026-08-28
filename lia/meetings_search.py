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


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{ --bg:#F4F5F7; --panel:#FFFFFF; --line:#E2E5EA; --fg:#1B2430; --muted:#6B7280;
         --accent:#22314F; --chip:#EDEFF3; }
  *{ box-sizing:border-box; }
  body{ margin:0; background:var(--bg); color:var(--fg);
        font-family:"Segoe UI Variable Text","Segoe UI",Heebo,system-ui,Arial,sans-serif; height:100vh; display:flex; flex-direction:column; }
  header{ padding:14px 18px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:12px; }
  header h1{ font-size:16px; margin:0; font-weight:600; }
  header .count{ color:var(--muted); font-size:12px; }
  select{ background:var(--chip); color:var(--fg); border:1px solid var(--line); border-radius:8px;
          padding:6px 10px; font-size:13px; margin-inline-start:auto; }
  .wrap{ flex:1; overflow:auto; padding:18px; }
  .askbar{ padding:14px 18px; border-top:1px solid var(--line); display:flex; gap:10px; }
  textarea{ flex:1; resize:none; background:var(--panel); color:var(--fg); border:1px solid var(--line);
            border-radius:10px; padding:11px 13px; font-size:15px; font-family:inherit; height:48px; }
  button{ background:var(--accent); color:#FFFFFF; border:0; border-radius:10px; padding:0 20px;
          font-size:15px; font-weight:600; cursor:pointer; }
  button:disabled{ opacity:.5; cursor:default; }
  .atext{ display:block; }
  .answer{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px 18px;
           font-size:15px; line-height:1.7; white-space:pre-wrap; }
  .answer .cite{ display:inline-block; background:var(--chip); color:var(--accent); border-radius:6px;
                 padding:0 7px; font-size:12px; font-weight:700; cursor:pointer; margin:0 2px; }
  .modelline{ color:var(--muted); font-size:12px; margin:8px 2px 18px; }
  .srchead{ color:var(--muted); font-size:12px; margin:6px 2px; }
  .src{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 13px;
        margin-bottom:8px; cursor:pointer; }
  .src:hover{ border-color:var(--accent); }
  .src .t{ font-weight:600; } .src .m{ color:var(--muted); font-size:12px; margin-top:3px; }
  .src .num{ color:var(--accent); font-weight:700; margin-inline-end:6px; }
  .empty{ color:var(--muted); text-align:center; margin-top:40px; }
  .recent{ margin-top:6px; } .recent .r{ padding:7px 10px; border-radius:8px; cursor:pointer; color:var(--muted); }
  .recent .r:hover{ background:var(--panel); color:var(--fg); }
  .spin{ color:var(--muted); }
  .turn{ margin-bottom:22px; }
  .q{ font-weight:600; font-size:15px; margin:0 2px 10px; padding-inline-start:10px; border-inline-start:3px solid var(--accent); }
  .ts{ color:var(--accent); font-weight:600; }
  .badge{ display:inline-block; font-size:11px; font-weight:700; border-radius:999px;
          padding:2px 11px; margin-bottom:4px; }
  .badge-factual{ background:#EDEFF3; color:#2A3342; }
  .badge-synthesis{ background:#EAF0FD; color:#2f5fd0; }
  .badge-action{ background:#E8F5EC; color:#1a7f37; }
  .clr{ background:var(--chip); color:var(--muted); border:1px solid var(--line); border-radius:8px;
        padding:6px 12px; font-size:12px; cursor:pointer; margin-inline-start:8px; }
  .hint{ color:var(--muted); font-size:11px; margin-inline-end:auto; }
</style></head>
<body>
  <header>
    <h1>🔎 Ask your meetings</h1>
    <span class="count" id="count"></span>
    <span class="hint">Filters: after:2026-08-01 · speaker:"Speaker A"</span>
    <select id="model"></select>
    <button class="clr" id="clr" title="New conversation">Clear</button>
  </header>
  <div class="wrap" id="wrap">
    <div id="thread"></div>
    <div class="empty" id="empty">Ask anything about your meetings - every answer is backed by cited sources.
      <div class="recent" id="recent"></div>
    </div>
  </div>
  <div class="askbar">
    <textarea id="q" dir="auto" placeholder="What was decided about...? (follow-ups keep the thread)" autofocus></textarea>
    <button id="go">Ask</button>
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
function jump(n){ var el=document.getElementById('src-'+n); if(el){ el.scrollIntoView({behavior:'smooth',block:'center'}); el.style.borderColor='var(--accent)'; } }

var MODE_BADGE = { factual:['🔍','Factual'], synthesis:['🧭','Synthesis'], action:['✅','Action items'] };
function modeBadge(mode){
  var m = MODE_BADGE[mode];
  if(!m) return '';
  return '<span class="badge badge-'+mode+'">'+m[0]+' '+m[1]+'</span>\n';
}

function srcCard(s){
  var ts = s.ts ? (' · <span class="ts">~'+esc(s.ts)+'</span>') : '';
  return '<div class="src" id="src-'+s.n+'" data-eid="'+esc(s.entry_id)+'">'
    + '<div class="t" dir="auto"><span class="num">['+s.n+']</span>'+esc(s.title)+'</div>'
    + '<div class="m">'+esc(s.date)+ts+(s.speakers?(' · '+esc(s.speakers)):'')+' · '+esc(s.kind)+'</div></div>';
}
document.getElementById('wrap').addEventListener('click', function(ev){
  var cite = ev.target.closest('.cite');
  if(cite && cite.dataset.n){ jump(parseInt(cite.dataset.n)); return; }
  var card = ev.target.closest('[data-eid]');
  if(card){ API.open_meeting(card.dataset.eid); }
});

function scrollEnd(){ var w=document.getElementById('wrap'); w.scrollTop = w.scrollHeight; }

async function doAsk(){
  var qEl = document.getElementById('q');
  var q = qEl.value.trim();
  if(!q || busy) return;
  busy = true; document.getElementById('go').disabled = true;
  document.getElementById('empty').style.display = 'none';
  var midx = document.getElementById('model').value;
  var thread = document.getElementById('thread');
  var turn = document.createElement('div'); turn.className = 'turn';
  turn.innerHTML = '<div class="q" dir="auto">'+esc(q)+'</div><div class="answer"><span class="spin">Thinking…</span></div>';
  thread.appendChild(turn); qEl.value=''; scrollEnd();
  try{
    var r = await API.ask(q, parseInt(midx), convo.slice(-3));
    var ansEl = turn.querySelector('.answer');
    if(r.error === 'no_index'){ ansEl.innerHTML = 'The index is still building. Try again in a moment.'; }
    else if(r.error === 'empty'){ turn.remove(); }
    else if(r.error){ ansEl.innerHTML = 'Error: '+esc(r.error)+(r.detail?(' — '+esc(r.detail)):''); }
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
  if(!init.count){ document.getElementById('empty').firstChild.textContent = 'The index is still building - your meetings are loading…'; }
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
    try:
        _WINDOW = webview.create_window(
            payload.get("title", "Ask your meetings"), html=HTML, js_api=api,
            width=900, height=780, min_size=(560, 480))
        import ui_kit as uk  # Lia window icon
        uk.webview_start(webview)
    except Exception as e:
        sys.stderr.write("meetings_search: failed: %r\n" % (e,))
        sys.exit(3)


if __name__ == "__main__":
    main()
