# -*- coding: utf-8 -*-
"""Lia — free-form chat window (pywebview + local Ollama / cloud).

A clean bilingual chat (English chrome, auto-RTL content) with the user's OWN models — local Ollama
(Gemma 4) by default, or cloud gpt-5.5 if an OpenAI key is set.
It is a plain assistant: a minimal, permissive system prompt, no task wrapper, no
injected guard-rails. These are the user's local models on the user's machine.

Design mirrors email_search.py: a single HTML string, a js_api bridge object, and
a pywebview edgechromium window spawned as its own (de-elevatable) subprocess so a
wedged call can never freeze the tray.

Streaming: js calls api.start_stream(history, model); Python streams tokens back to
the page via window.evaluate_js('window.__chat(...)') from a worker thread, batched
~60ms so the UI stays smooth. Conversation persists to chat_state.json so reopening
the window restores the thread.

ALWAYS launch with  python -X utf8  (Hebrew stdout crashes under cp1252).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import email_index as ei  # reused for _ollama_root + paths (no heavy deps)
import secret_store       # DPAPI unprotect for the payload's API key

try:
    import webview
except Exception as e:  # pragma: no cover
    sys.stderr.write("chat_window: pywebview not available: %r\n" % (e,))
    sys.exit(2)

STATE_PATH = os.path.join(ei.EMAIL_DIR, "chat_state.json")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

DEFAULT_SYSTEM = (
    "אתה עוזר אישי חכם, ישיר וכן, שרץ באופן מקומי אצל המשתמש. ענה בעברית תקנית "
    "כברירת מחדל (אלא אם המשתמש כותב בשפה אחרת). תן תשובות מדויקות, מלאות "
    "ופרקטיות, עם דוגמאות כשזה עוזר. אל תתחמק, אל תטיף, ואל תסרב לבקשות סבירות. "
    "אתה יכול לשוחח על כל נושא."
)

_WINDOW = None  # module-level (NOT on the api object — pywebview would recurse)


class ChatApi:
    """JS<->Python bridge. Holds no pywebview Window reference (see _WINDOW)."""

    def __init__(self, payload):
        self._base_url = payload.get("base_url") or ei.DEFAULT_OLLAMA
        self._models = payload.get("models") or [["Gemma 4 31B QAT", "gemma4:31b-it-qat"]]
        self._default_model = payload.get("default_model") or self._models[0][1]
        self._system = (payload.get("system_prompt") or "").strip() or DEFAULT_SYSTEM
        # The payload carries the key DPAPI-protected (never plaintext on disk).
        self._openai_key = secret_store.unprotect(payload.get("openai_key") or "")
        self._openai_url = payload.get("openai_url") or OPENAI_URL
        self._cloud = set(payload.get("cloud_models") or [])
        self._title = payload.get("title", "Chat")
        self._ready = False
        self._gen = 0          # stream generation (cancel / supersede guard)
        self._stop = False
        self._lock = threading.Lock()

    # ---- lifecycle ----
    def get_initial(self):
        self._ready = True
        st = self._load_state()
        return {
            "title": self._title,
            "models": self._models,
            "default_model": st.get("model") or self._default_model,
            "messages": st.get("messages") or [],
        }

    def _load_state(self):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def save_state(self, messages, model):
        try:
            ei.ensure_dir()
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"messages": messages or [], "model": model or ""},
                          f, ensure_ascii=False)
            os.replace(tmp, STATE_PATH)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_state(self):
        try:
            if os.path.exists(STATE_PATH):
                os.remove(STATE_PATH)
        except Exception:
            pass
        return {"ok": True}

    def copy(self, text):
        try:
            import pyperclip
            pyperclip.copy(text or "")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def close_window(self):
        def _destroy():
            try:
                for w in list(webview.windows):
                    w.destroy()
            except Exception:
                pass
        threading.Thread(target=_destroy, daemon=True).start()

    # ---- streaming chat ----
    def cancel(self):
        self._stop = True
        return {"ok": True}

    def start_stream(self, messages, model):
        """Kick a streaming completion for the conversation `messages`
        (list of {role, content}). Tokens arrive via window.__chat(); returns the
        stream id immediately so the UI stays responsive."""
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._stop = False
        model = (model or self._default_model)
        threading.Thread(target=self._run, args=(gen, list(messages or []), model),
                         daemon=True).start()
        return {"ok": True, "id": gen}

    def _emit(self, gen, kind, *args):
        if gen != self._gen and kind != "err":
            return
        if _WINDOW is None:
            return
        try:
            payload = ",".join(json.dumps(a, ensure_ascii=False) for a in args)
            _WINDOW.evaluate_js("window.__chat(%s,%d,%s)"
                                % (json.dumps(kind), gen, payload))
        except Exception:
            pass

    def _run(self, gen, messages, model):
        try:
            full = self._stream_local(gen, messages, model) \
                if not self._is_cloud(model) \
                else self._stream_cloud(gen, messages, model)
            if gen == self._gen:
                self._emit(gen, "done", full, model)
        except Exception as e:
            self._emit(gen, "err", "%s: %s" % (type(e).__name__, e))

    def _is_cloud(self, model):
        m = (model or "").lower()
        if model in self._cloud:
            return True
        return m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))

    def _flusher(self, gen):
        """Returns (push, flush): buffer deltas, emit at most ~every 60ms so we
        don't fire one evaluate_js per token."""
        state = {"buf": "", "last": 0.0}

        def push(delta):
            if not delta:
                return
            state["buf"] += delta
            now = time.time()
            if len(state["buf"]) >= 24 or (now - state["last"]) > 0.06:
                self._emit(gen, "tok", state["buf"])
                state["buf"] = ""
                state["last"] = now

        def flush():
            if state["buf"]:
                self._emit(gen, "tok", state["buf"])
                state["buf"] = ""
        return push, flush

    def _messages_with_system(self, messages):
        return [{"role": "system", "content": self._system}] + [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in messages if m.get("content")
        ]

    def _stream_local(self, gen, messages, model):
        import requests
        url = ei._ollama_root(self._base_url) + "/api/chat"
        msgs = self._messages_with_system(messages)
        chars = sum(len(m["content"]) for m in msgs)
        num_ctx = max(8192, min(32768, int(chars / 1.9) + 4096))
        body = {
            "model": model, "stream": True,
            "think": False,
            "options": {"num_ctx": num_ctx, "temperature": 0.4},
            "messages": msgs,
        }
        push, flush = self._flusher(gen)
        full = []
        with requests.Session() as s:
            r = s.post(url, json=body, stream=True, timeout=(10, 600))
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if self._stop or gen != self._gen:
                    break
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                delta = ((j.get("message") or {}).get("content")) or ""
                if delta:
                    full.append(delta)
                    push(delta)
                if j.get("done"):
                    break
        flush()
        return "".join(full)

    def _stream_cloud(self, gen, messages, model):
        import requests
        msgs = self._messages_with_system(messages)
        body = {"model": model, "stream": True, "messages": msgs}
        ml = (model or "").lower()
        if ml.startswith(("gpt-5", "o1", "o3", "o4")):
            body["max_completion_tokens"] = 4000
        else:
            body["max_tokens"] = 4000
            body["temperature"] = 0.4
        push, flush = self._flusher(gen)
        full = []
        with requests.Session() as s:
            r = s.post(self._openai_url, json=body, stream=True, timeout=(10, 600),
                       headers={"Authorization": "Bearer " + self._openai_key,
                                "Content-Type": "application/json"})
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if self._stop or gen != self._gen:
                    break
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = json.loads(data)
                    delta = (j["choices"][0].get("delta") or {}).get("content") or ""
                except Exception:
                    delta = ""
                if delta:
                    full.append(delta)
                    push(delta)
        flush()
        return "".join(full)


# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<style>
  :root{
    --accent:#22314F; --accent-d:#2C3E63; --accent-soft:#EDEFF3; --accent-line:#D6DCE8;
    --ink:#1B2430; --ink-soft:#2A3342; --muted:#6B7280; --faint:#9AA0AD;
    --line:#E2E5EA; --line-2:#CDD3DC; --bg:#ffffff; --panel:#F0F1F4;
    --user:#22314F; --bot:#f4f4f8; --code:#1e2233;
    --shadow:0 1px 2px rgba(16,24,40,.05),0 6px 22px rgba(16,24,40,.06);
  }
  *{box-sizing:border-box;}
  html,body{height:100%;margin:0;}
  body{background:var(--bg);color:var(--ink);font-size:15px;
       font-family:"Segoe UI Variable Text","Segoe UI",Heebo,system-ui,Arial,sans-serif;
       display:flex;flex-direction:column;-webkit-font-smoothing:antialiased;}

  .top{flex:none;display:flex;align-items:center;gap:12px;padding:12px 18px;
       border-bottom:1px solid var(--line-2);background:var(--bg);z-index:5;}
  .brand{font-weight:800;font-size:16px;display:flex;align-items:center;gap:8px;}
  .brand .dot{width:8px;height:8px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 3px #22c55e22;}
  .brand small{color:var(--muted);font-weight:500;font-size:12px;margin-inline-start:2px;}
  .spacer{flex:1;}
  select{padding:8px 11px;border:1px solid var(--line-2);border-radius:10px;background:#fff;
         font-size:13px;color:var(--ink-soft);cursor:pointer;outline:none;}
  select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(34,49,79,.14);}
  .btn{border:1px solid var(--line-2);background:#fff;border-radius:10px;padding:8px 13px;
       cursor:pointer;font-size:13.5px;color:var(--ink-soft);white-space:nowrap;
       transition:background .12s,border-color .12s,transform .05s;}
  .btn:hover{background:var(--panel);}
  .btn:active{transform:translateY(.5px);}

  /* messages */
  .scroll{flex:1;overflow:auto;padding:22px 0;}
  .wrap{max-width:820px;margin:0 auto;padding:0 20px;display:flex;flex-direction:column;gap:16px;}
  .msg{display:flex;gap:11px;align-items:flex-start;}
  .msg .av{width:30px;height:30px;border-radius:50%;flex:none;display:flex;align-items:center;
           justify-content:center;font-size:15px;font-weight:700;margin-top:2px;}
  .msg.user{flex-direction:row-reverse;}
  .msg.user .av{background:var(--accent);color:#fff;}
  .msg.bot  .av{background:var(--accent-soft);color:var(--accent);}
  .bubble{padding:12px 15px;border-radius:15px;line-height:1.75;max-width:76%;
          overflow-wrap:anywhere;position:relative;
          user-select:text;-webkit-user-select:text;cursor:text;}
  .msg.user .bubble{background:var(--user);color:#fff;border-top-right-radius:5px;}
  .msg.bot  .bubble{background:var(--bot);color:var(--ink);border:1px solid var(--line);
                    border-top-left-radius:5px;}
  .bubble p{margin:0 0 8px;} .bubble p:last-child{margin:0;}
  .bubble strong{font-weight:700;}
  .bubble code{background:#0000000f;padding:1px 5px;border-radius:5px;font-size:.9em;
               font-family:"Cascadia Code",Consolas,monospace;}
  .msg.user .bubble code{background:#ffffff28;}
  .bubble pre.code{background:var(--code);color:#e9ecf5;padding:12px 14px;border-radius:11px;
                   overflow:auto;margin:8px 0;direction:ltr;text-align:left;}
  .bubble pre.code code{background:none;padding:0;color:inherit;font-size:12.5px;line-height:1.6;}
  .bubble ul{margin:6px 0;padding-inline-start:22px;} .bubble li{margin:2px 0;}
  .cursor{display:inline-block;width:7px;height:16px;background:var(--accent);border-radius:2px;
          margin-inline-start:2px;vertical-align:-2px;animation:bl 1s steps(2) infinite;}
  @keyframes bl{50%{opacity:0;}}
  .tools{display:flex;gap:6px;margin-top:7px;opacity:.75;transition:opacity .12s;}
  .msg.bot:hover .tools{opacity:1;}
  .tools button{border:1px solid var(--line-2);background:#fff;color:var(--ink-soft);cursor:pointer;
                font-size:12px;padding:4px 10px;border-radius:8px;user-select:none;}
  .tools button:hover{background:var(--panel);border-color:var(--accent);color:var(--accent);}

  .empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
         gap:12px;color:var(--muted);text-align:center;}
  .empty .big{font-size:46px;}
  .empty h2{margin:0;color:var(--ink);font-size:22px;font-weight:800;}
  .empty .subs{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;max-width:560px;margin-top:6px;}
  .chip{border:1px solid var(--line-2);background:var(--panel);border-radius:20px;padding:7px 13px;
        font-size:12.5px;color:var(--ink-soft);cursor:pointer;transition:background .12s,border-color .12s;}
  .chip:hover{background:#fff;border-color:var(--accent);color:var(--accent);}

  /* composer */
  .composer{flex:none;border-top:1px solid var(--line-2);background:var(--bg);padding:12px 20px 16px;}
  .cbox{max-width:820px;margin:0 auto;display:flex;gap:10px;align-items:flex-end;
        border:1px solid var(--line-2);border-radius:16px;padding:8px 8px 8px 14px;background:#fff;
        box-shadow:var(--shadow);transition:border-color .12s,box-shadow .12s;}
  .cbox:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px rgba(34,49,79,.13),var(--shadow);}
  #inp{flex:1;border:0;outline:none;resize:none;font-size:15px;line-height:1.6;background:transparent;
       color:var(--ink);max-height:200px;font-family:inherit;padding:6px 0;}
  #inp::placeholder{color:var(--faint);}
  .send{flex:none;width:40px;height:40px;border-radius:12px;border:0;cursor:pointer;
        background:var(--accent);color:#fff;font-size:17px;display:flex;align-items:center;
        justify-content:center;transition:background .12s,transform .05s;}
  .send:hover{background:var(--accent-d);} .send:active{transform:translateY(.5px);}
  .send:disabled{background:var(--line-2);color:var(--faint);cursor:default;}
  .send.stop{background:#c0263c;} .send.stop:hover{background:#9f1f32;}
  .hint{max-width:820px;margin:7px auto 0;color:var(--faint);font-size:11.5px;text-align:center;}

  ::-webkit-scrollbar{width:11px;} ::-webkit-scrollbar-thumb{background:#CDD3DC;border-radius:8px;border:3px solid var(--bg);}
  ::-webkit-scrollbar-thumb:hover{background:#9AA0AD;}
</style></head>
<body>
  <div class="top">
    <div class="brand"><span class="dot"></span>Chat <small id="sub">local · private</small></div>
    <span class="spacer"></span>
    <select id="model" title="Choose a model"></select>
    <button class="btn" id="newBtn" title="New conversation">🗑 New</button>
  </div>

  <div class="scroll" id="scroll">
    <div class="wrap" id="wrap"></div>
  </div>

  <div class="composer">
    <div class="cbox">
      <textarea id="inp" rows="1" dir="auto" placeholder="Type a message…  (Enter to send · Shift+Enter for a new line)"></textarea>
      <button class="send" id="send" title="Send">➤</button>
    </div>
    <div class="hint">Runs on your own local models · the conversation is saved automatically</div>
  </div>

<script>
var api, msgs=[], model="", streaming=false, curBot=null, curText="";
function $(id){return document.getElementById(id);}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

/* --- tiny, safe markdown --- */
function md(t){
  t = (t||'');
  var S0=String.fromCharCode(57344), S1=String.fromCharCode(57345), Q=String.fromCharCode(57346);
  var blocks=[];
  t = t.replace(/```[ \t]*\w*\r?\n?([\s\S]*?)```/g, function(_,code){
    blocks.push('<pre class="code"><code>'+esc(code.replace(/\s+$/,''))+'</code></pre>');
    return S0+(blocks.length-1)+S1;
  });
  t = esc(t);
  t = t.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  t = t.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  t = t.replace(/^[ \t]*[-*][ \t]+(.+)$/gm, Q+'<li>$1</li>');
  t = t.replace(new RegExp(Q+'<li>[\\s\\S]*?<\\/li>(?:\\s*'+Q+'<li>[\\s\\S]*?<\\/li>)*','g'), function(m){
    return '<ul>'+m.split(Q).join('')+'</ul>';
  });
  t = t.split(Q).join('');
  t = t.replace(/\n/g,'<br>');
  t = t.replace(/<br>\s*(<ul>|<\/ul>|<li>|<pre)/g,'$1');
  t = t.replace(/(<\/ul>|<\/pre>|<\/li>)\s*<br>/g,'$1');
  t = t.replace(new RegExp(S0+'(\\d+)'+S1,'g'), function(_,i){return blocks[+i];});
  return t;
}

function scrollDown(force){
  var s=$('scroll');
  if(force || (s.scrollHeight - s.scrollTop - s.clientHeight < 140)) s.scrollTop=s.scrollHeight;
}

function renderAll(){
  var w=$('wrap');
  if(!msgs.length){
    w.innerHTML='';
    $('scroll').innerHTML='<div class="empty"><div class="big">💬</div>'
      +'<h2>How can I help?</h2>'
      +'<div class="subs">'
      +'<span class="chip" onclick="quick(this)">Write an email to…</span>'
      +'<span class="chip" onclick="quick(this)">Summarize this text</span>'
      +'<span class="chip" onclick="quick(this)">Explain this simply</span>'
      +'<span class="chip" onclick="quick(this)">Ideas for…</span>'
      +'</div></div>';
    return;
  }
  if(!$('wrap').parentNode || $('scroll').querySelector('.empty')){
    $('scroll').innerHTML='<div class="wrap" id="wrap"></div>'; w=$('wrap');
  }
  w.innerHTML = msgs.map(function(m,i){ return bubbleHtml(m.role, md(m.content), i); }).join('');
  scrollDown(true);
}
function bubbleHtml(role, inner, i){
  var av = role==='user' ? 'You' : '✦';
  var tools = role==='bot' ? '<div class="tools"><button data-copy="'+i+'">⧉ Copy</button></div>' : '';
  return '<div class="msg '+role+'"><div class="av">'+av+'</div>'
    +'<div><div class="bubble" dir="auto">'+inner+'</div>'+tools+'</div></div>';
}
function quick(el){ $('inp').value=el.textContent+' '; $('inp').focus(); autg(); }
function copyMsg(i, btn){
  if(!msgs[i]) return;
  api.copy(msgs[i].content);
  if(btn){ var o=btn.textContent; btn.textContent='✓ Copied'; setTimeout(function(){ btn.textContent=o; }, 1300); }
}
// Copy buttons carry the message index in data-copy (no interpolated inline
// handlers); one delegated listener on the scroll container routes them.
$('scroll').addEventListener('click', function(ev){
  var b = ev.target.closest('button[data-copy]');
  if(b){ copyMsg(parseInt(b.dataset.copy), b); }
});

function autg(){ var t=$('inp'); t.style.height='auto'; t.style.height=Math.min(t.scrollHeight,200)+'px'; }

function setStreaming(on){
  streaming=on;
  var b=$('send');
  if(on){ b.classList.add('stop'); b.innerHTML='■'; b.title='Stop'; }
  else  { b.classList.remove('stop'); b.innerHTML='➤'; b.title='Send'; b.disabled=false; }
}

async function send(){
  if(streaming){ api.cancel(); return; }
  var text=$('inp').value.trim();
  if(!text) return;
  $('inp').value=''; autg();
  msgs.push({role:'user', content:text});
  // ensure the thread container exists (leave empty-state)
  if($('scroll').querySelector('.empty')){ $('scroll').innerHTML='<div class="wrap" id="wrap"></div>'; }
  var w=$('wrap');
  w.insertAdjacentHTML('beforeend', bubbleHtml('user', md(text), msgs.length-1));
  // assistant placeholder
  curText='';
  w.insertAdjacentHTML('beforeend',
    '<div class="msg bot"><div class="av">✦</div><div><div class="bubble" id="live" dir="auto"><span class="cursor"></span></div></div></div>');
  curBot=$('live'); scrollDown(true);
  setStreaming(true);
  var hist = msgs.map(function(m){ return {role:(m.role==='bot'?'assistant':'user'), content:m.content}; });
  try{ await api.start_stream(hist, model); }
  catch(e){ onErr(''+e); }
}

/* streaming callbacks from Python */
window.__chat = function(kind, gen, a){
  if(kind==='tok'){
    curText += a;
    if(curBot){ curBot.innerHTML = md(curText)+'<span class="cursor"></span>'; scrollDown(false); }
  } else if(kind==='done'){
    finishBot(a || curText);
  } else if(kind==='err'){
    onErr(a);
  }
};
function finishBot(finalText){
  if(!curBot){ setStreaming(false); return; }   // stale (new-chat / superseded) — no phantom push
  var txt = finalText || curText;
  curBot.removeAttribute('id');
  curBot.innerHTML = md(txt);
  // add copy tool (idx = this bot msg's index once pushed below)
  var holder=curBot.parentNode;
  if(holder && !holder.querySelector('.tools')){
    holder.insertAdjacentHTML('beforeend','<div class="tools"><button data-copy="'+msgs.length+'">⧉ Copy</button></div>');
  }
  msgs.push({role:'bot', content:txt});
  curBot=null; curText='';
  setStreaming(false);
  api.save_state(msgs, model);
  scrollDown(false);
}
function onErr(m){
  if(curBot){
    curBot.removeAttribute('id');
    curBot.innerHTML = '<span style="color:#c0263c">⚠ '+esc(m)+'</span>';
  }
  curBot=null; curText='';
  setStreaming(false);
}

function newChat(){
  if(streaming){ api.cancel(); }
  msgs=[]; curBot=null; curText=''; setStreaming(false);
  api.clear_state();
  renderAll();
  $('inp').focus();
}

window.addEventListener('pywebviewready', async function(){
  api=window.pywebview.api;
  var d=await api.get_initial();
  document.title=d.title||"Chat";
  var ms=$('model');
  (d.models||[]).forEach(function(m){ var o=document.createElement('option'); o.value=m[1]; o.textContent=m[0]; ms.appendChild(o); });
  var want=d.default_model||(d.models&&d.models[0]&&d.models[0][1])||'';
  if(Array.prototype.some.call(ms.options,function(o){return o.value===want;})) ms.value=want;
  model=ms.value;
  ms.addEventListener('change', function(){ model=ms.value; api.save_state(msgs, model); });
  msgs=(d.messages||[]).map(function(m){ return {role:(m.role==='assistant'?'bot':m.role), content:m.content}; });
  renderAll();

  $('send').addEventListener('click', send);
  $('newBtn').addEventListener('click', newChat);
  var inp=$('inp');
  inp.addEventListener('input', autg);
  inp.addEventListener('keydown', function(e){
    if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); }
  });
  inp.focus();
});
</script>
</body></html>"""


def main():
    # Windowless spawn safety (see emailsearch_indexer): stdout/stderr may be None.
    if sys.stdout is None or sys.stderr is None:
        try:
            ei.ensure_dir()
            _sink = open(os.path.join(ei.EMAIL_DIR, "chat_window.log"), "a",
                         encoding="utf-8")
        except Exception:
            _sink = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = _sink
        if sys.stderr is None:
            sys.stderr = _sink

    if len(sys.argv) < 2:
        sys.stderr.write("chat_window: missing payload path\n")
        sys.exit(2)
    try:
        with open(sys.argv[1], encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        sys.stderr.write("chat_window: bad payload: %r\n" % (e,))
        sys.exit(2)
    try:
        os.remove(sys.argv[1])   # consumed - don't leave the payload on disk
    except OSError:
        pass                     # the parent's timed cleanup is the fallback

    global _WINDOW
    api = ChatApi(payload)
    try:
        _WINDOW = webview.create_window(
            payload.get("title", "Chat"), html=HTML, js_api=api,
            width=880, height=760, min_size=(560, 480))
        import ui_kit as uk  # Lia window icon
        uk.webview_start(webview)
    except Exception as e:
        sys.stderr.write("chat_window: failed: %r\n" % (e,))
        sys.exit(3)


if __name__ == "__main__":
    main()
