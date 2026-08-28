#!/usr/bin/env python
"""Compose editor — standalone WebView2 (pywebview) subprocess.

Spawned by lia.py with a payload-JSON path as argv[1]. Renders the
composed text as HTML so Hebrew/English/number bidi is PERFECT (a browser does
the full Unicode bidi algorithm — Tk 8.6 cannot, which is the whole reason this
exists). Supports inline editing (contenteditable), version history, style
switch + Revise/Regenerate via the SAME OpenAILLMCleaner the main app uses, and
Copy / Save / Close.

REUSABLE by design: the payload controls title, content, transcript (for
Regenerate), style options, save target and which actions are offered — so the
meeting-summary editor (a follow-up) can reuse this with a different payload.

Runs in its own process on purpose: pywebview wants the main thread + a blocking
event loop, which would fight pystray (main thread) and the Tk overlay loop in
the parent. A subprocess also moves the blocking recorder/LLM work fully off the
parent's Tk thread.

Protocol:
  in : argv[1] = path to a UTF-8 JSON payload (see PAYLOAD KEYS below)
  out: a single line on stdout  ->  COMPOSE_RESULT <json>   ({text, exported})

PAYLOAD KEYS:
  title          window title                              (default "Compose")
  text           initial composed text (logical order)     (required)
  style          initial style key                         (default "summary")
  transcript     source transcript for Regenerate          (default "")
  vocabulary     custom vocabulary for the LLM             (default "")
  styles         [[key,label],...] style buttons           (default summary/email/message)
  can_regenerate show Regenerate + style re-compose        (default True)
  save_suffix    filename suffix on Save                   (default "_composed")
"""
import sys
import os
import json

# Reuse the app's tuned cleaner + config + paths. Importing lia runs
# only its module-level defs (~0.14s, no app start, no mutex, no tray) — verified.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import webview
except Exception as e:  # pragma: no cover
    sys.stderr.write("compose_editor: pywebview not available: %r\n" % (e,))
    sys.exit(2)

try:
    from lia import (OpenAILLMCleaner, load_config, SUMMARIES_DIR,
                             _txt_apply_rtl_to_doc)
except Exception as e:  # pragma: no cover
    sys.stderr.write("compose_editor: cannot import lia: %r\n" % (e,))
    OpenAILLMCleaner = None
    SUMMARIES_DIR = os.path.join(os.environ.get("APPDATA", _HERE),
                                 "Lia", "summaries")
    def load_config():
        return {}
    def _txt_apply_rtl_to_doc(t):
        return t


# --------------------------------------------------------------------------- #
#  The page. All data is fetched at runtime via pywebview.api.get_initial() —
#  nothing is templated into this string, so there are no escaping pitfalls.
# --------------------------------------------------------------------------- #
HTML = r"""<!DOCTYPE html>
<html lang="he">
<head>
<meta charset="utf-8">
<style>
  :root { --ink:#1B2430; --sub:#6B7280; --accent:#22314F; --accent2:#2C3E63;
          --sec:#EDEFF3; --sec-h:#D6DCE8; --border:#E2E5EA; --ok:#1a7f37;
          --err:#c0263c; }
  * { box-sizing:border-box; }
  html,body { height:100%; }
  body { margin:0; background:#FFFFFF; color:var(--ink);
         font-family:"Segoe UI Variable Text","Segoe UI",Heebo,system-ui,Arial,sans-serif;
         display:flex; flex-direction:column; padding:16px 18px 14px; }
  .titlebar { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
  .titlebar h1 { font-size:19px; margin:0; font-weight:700; }
  .nav { color:var(--sub); font-size:14px; display:flex; gap:6px; align-items:center; }
  .nav .arr { cursor:pointer; font-weight:700; color:var(--ink); user-select:none; padding:0 2px; }
  .nav .arr.off { color:#9AA0AD; cursor:default; }
  .page { flex:1 1 auto; border:1px solid var(--border); border-radius:8px;
          padding:14px 16px; font-size:16px; line-height:1.7; overflow-y:auto;
          outline:none; min-height:180px; }
  .page:focus { border-color:var(--accent); box-shadow:0 0 0 2px rgba(34,49,79,.15); }
  .page div { margin:0 0 .5em; }
  .status { min-height:18px; font-size:13px; color:var(--sub); margin:8px 2px 0; }
  .status.ok { color:var(--ok); } .status.err { color:var(--err); }
  .row { display:flex; align-items:center; gap:8px; margin-top:10px; flex-wrap:wrap; }
  .lbl { color:var(--sub); font-size:13px; }
  .chip { background:var(--sec); color:var(--ink); border:none; border-radius:6px;
          padding:7px 14px; font-size:13px; cursor:pointer; font-family:inherit; }
  .chip:hover { background:var(--sec-h); } .chip.on { background:var(--accent); color:#FFFFFF; }
  .revise { flex:1; min-width:200px; border:1px solid var(--border); border-radius:6px;
            padding:9px 12px; font-size:15px; font-family:inherit; }
  .revise:focus { outline:none; border-color:var(--accent); }
  .hint { color:var(--sub); font-size:12px; margin-top:7px; }
  .btns { display:flex; gap:8px; margin-top:12px; }
  .btn { border:none; border-radius:6px; padding:10px 16px; font-size:14px; cursor:pointer;
         font-family:inherit; background:var(--sec); color:var(--ink); }
  .btn:hover { background:var(--sec-h); }
  .btn.primary { background:var(--accent); color:#FFFFFF; font-weight:600; }
  .btn.primary:hover { background:var(--accent2); }
  .btn.spacer { margin-left:auto; }
  .btn:disabled, .chip:disabled { opacity:.5; cursor:default; }
</style>
</head>
<body>
  <div class="titlebar">
    <h1 id="title">Compose</h1>
    <div class="nav"><span class="arr" id="prev">&#9664;</span>
      <span id="ver">v1/1</span>
      <span class="arr" id="next">&#9654;</span></div>
  </div>

  <div class="page" id="page" contenteditable="true" spellcheck="false"></div>
  <div class="status" id="status"></div>

  <div class="row" id="styleRow"><span class="lbl">Style:</span></div>

  <div class="row">
    <input class="revise" id="instr" dir="auto"
           placeholder="Describe a change (e.g. 'make it warmer', 'remove the last paragraph')...">
    <button class="btn primary" id="reviseBtn">Revise</button>
  </div>
  <div class="hint">Type a change and click Revise. You can also edit the text directly above.</div>

  <div class="btns">
    <button class="btn" id="regenBtn">Regenerate</button>
    <button class="btn primary spacer" id="copyBtn">Copy</button>
    <button class="btn" id="saveBtn">Save</button>
    <button class="btn" id="closeBtn">Close</button>
  </div>

<script>
  var versions = [""], idx = 0, style = "summary", styles = [],
      canRegen = true, busy = false, exported = false;
  var $ = function(id){ return document.getElementById(id); };
  var page=$('page'), statusEl=$('status'), instr=$('instr');

  function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
  function setStatus(msg, kind){ statusEl.textContent=msg||''; statusEl.className='status'+(kind?(' '+kind):''); }

  function render(){
    page.innerHTML = versions[idx].split('\n').map(function(l){
      return (l.trim()==='') ? '<div><br></div>' : '<div dir="auto">'+esc(l)+'</div>';
    }).join('');
    $('ver').textContent = 'v'+(idx+1)+'/'+versions.length;
    $('prev').className = 'arr'+(idx>0?'':' off');
    $('next').className = 'arr'+(idx<versions.length-1?'':' off');
  }
  // Read the (possibly hand-edited) text back out of the editable page.
  function pageText(){ return (page.innerText||'').replace(/ /g,' ').replace(/\r/g,'').replace(/\n+$/,''); }
  // Capture any inline edit as the current version before an action replaces it.
  function commitEdit(){ var t=pageText(); if(t && t!==versions[idx]){ versions[idx]=t; } }

  function setBusy(b){
    busy=b;
    ['reviseBtn','regenBtn','copyBtn','saveBtn','closeBtn'].forEach(function(id){ $(id).disabled=b; });
    document.querySelectorAll('#styleRow .chip').forEach(function(c){ c.disabled=b; });
    page.contentEditable = b ? 'false' : 'true';
  }
  function pushVersion(t){ versions.push(t); idx=versions.length-1; render(); }

  function buildStyles(){
    var row=$('styleRow');
    styles.forEach(function(pair){
      var b=document.createElement('button');
      b.className='chip'+(pair[0]===style?' on':''); b.textContent=pair[1]; b.dataset.key=pair[0];
      b.onclick=function(){ pickStyle(pair[0]); };
      row.appendChild(b);
    });
    if(!canRegen){ $('regenBtn').style.display='none'; }
  }
  function refreshStyleChips(){
    document.querySelectorAll('#styleRow .chip').forEach(function(c){
      c.className='chip'+(c.dataset.key===style?' on':'');
    });
  }

  async function pickStyle(key){
    if(busy || key===style) return;
    commitEdit(); style=key; refreshStyleChips();
    if(canRegen){ await regenerate('Rewriting as '+key+'…'); } else { setStatus('Style set'); }
  }
  async function doRevise(){
    if(busy) return; commitEdit();
    var t=instr.value.trim();
    if(!t){ setStatus('Type a change first', 'err'); return; }
    instr.value=''; setBusy(true); setStatus('Revising…');
    var r = await pywebview.api.revise(versions[idx], t, style);
    setBusy(false);
    if(r && r.ok && r.text){ pushVersion(r.text); setStatus(''); }
    else { instr.value=t; setStatus((r&&r.error)?('Failed: '+r.error):'Revise failed — try again', 'err'); }
  }
  async function regenerate(statusMsg){
    if(busy) return; commitEdit(); setBusy(true); setStatus(statusMsg||'Regenerating…');
    var r = await pywebview.api.regenerate(style);
    setBusy(false);
    if(r && r.ok && r.text){ pushVersion(r.text); setStatus(''); }
    else { setStatus((r&&r.error)?('Failed: '+r.error):'Regenerate failed', 'err'); }
  }
  async function doCopy(){ commitEdit(); var ok=await pywebview.api.copy(versions[idx]);
    exported=exported||ok; setStatus(ok?'Copied to clipboard':'Copy failed', ok?'ok':'err'); }
  async function doSave(){ commitEdit(); setBusy(true); setStatus('Saving…');
    var r=await pywebview.api.save(versions[idx]); setBusy(false);
    if(r && r.ok){ exported=true; setStatus('Saved', 'ok'); }
    else { setStatus((r&&r.error)?('Save failed: '+r.error):'Save failed','err'); } }
  function doClose(){
    commitEdit();
    if(!exported){
      var n=versions.length;
      if(!confirm('Close without copying or saving?\n'+(n>1?'All versions will be discarded.':'The piece will be discarded.'))) return;
    }
    pywebview.api.request_close(versions[idx], exported);
  }
  // Called by the Python window-close (X) guard.
  function askCloseConfirm(){
    commitEdit();
    if(exported || confirm('Close without copying or saving?\nThe piece will be discarded.')){
      pywebview.api.request_close(versions[idx], exported);
    }
  }

  $('prev').onclick=function(){ if(!busy && idx>0){ commitEdit(); idx--; render(); } };
  $('next').onclick=function(){ if(!busy && idx<versions.length-1){ commitEdit(); idx++; render(); } };
  $('reviseBtn').onclick=doRevise;
  $('regenBtn').onclick=function(){ regenerate(); };
  $('copyBtn').onclick=doCopy; $('saveBtn').onclick=doSave; $('closeBtn').onclick=doClose;
  instr.addEventListener('keydown', function(e){ if(e.key==='Enter'){ e.preventDefault(); doRevise(); } });
  document.addEventListener('keydown', function(e){
    if(e.key==='Escape' && document.activeElement!==page){ e.preventDefault(); doClose(); } });

  window.addEventListener('pywebviewready', async function(){
    // Bridge is only populated once pywebview is ready; read it here, never at
    // inline-script parse time (see the pywebview-api-defer rule).
    var api = window.pywebview.api;
    var d = await api.get_initial();
    document.title = d.title; $('title').textContent = d.title;
    versions=[ (d.text||'').replace(/\r/g,'') ]; idx=0;
    style=d.style||'summary'; styles=d.styles||[]; canRegen=!!d.can_regenerate;
    buildStyles(); render(); instr.focus();
  });
</script>
</body>
</html>"""


class Api:
    """JS<->Python bridge. Only the METHODS below are meant for JS. Data is held
    in underscore attributes and we NEVER store a pywebview Window here — doing
    so makes pywebview recurse into Window.native (a WinForms object) forever
    when it introspects the api. The window is reached via webview.windows."""

    def __init__(self, payload, cleaner):
        self._payload = payload
        self._cleaner = cleaner
        self._allow_close = False
        self._result = {"text": payload.get("text", ""), "exported": False}

    # ---- data ----
    def get_initial(self):
        p = self._payload
        styles = p.get("styles") or [["summary", "Summary"], ["email", "Email"],
                                     ["message", "Message"]]
        return {
            "title": p.get("title", "Compose"),
            "text": p.get("text", ""),
            "style": p.get("style", "summary"),
            "styles": styles,
            "can_regenerate": bool(p.get("can_regenerate", True)
                                   and (p.get("transcript") or "").strip()
                                   and self._cleaner is not None),
        }

    # ---- LLM ----
    def revise(self, current, instruction, style):
        if self._cleaner is None:
            return {"ok": False, "error": "no OpenAI key"}
        try:
            vocab = self._payload.get("vocabulary", "") or ""
            out = self._cleaner.revise(current, instruction, vocabulary=vocab)
            return {"ok": bool(out), "text": out or ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def regenerate(self, style):
        if self._cleaner is None:
            return {"ok": False, "error": "no OpenAI key"}
        transcript = (self._payload.get("transcript") or "").strip()
        if not transcript:
            return {"ok": False, "error": "nothing to regenerate from"}
        try:
            vocab = self._payload.get("vocabulary", "") or ""
            out = self._cleaner.compose(transcript, style=style, vocabulary=vocab)
            return {"ok": bool(out), "text": out or ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- export ----
    def copy(self, text):
        try:
            import pyperclip
            pyperclip.copy(text or "")
            self._result["exported"] = True
            return True
        except Exception:
            return False

    def save(self, text):
        import datetime
        try:
            os.makedirs(SUMMARIES_DIR, exist_ok=True)
            suffix = self._payload.get("save_suffix", "_composed")
            fname = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + suffix + ".txt"
            path = os.path.join(SUMMARIES_DIR, fname)
            with open(path, "w", encoding="utf-8") as f:
                f.write(_txt_apply_rtl_to_doc((text or "").strip()))
            self._result["exported"] = True
            try:
                os.startfile(path)
            except Exception:
                pass
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- close ----
    def request_close(self, text, exported):
        self._result = {"text": text or "", "exported": bool(exported)}
        self._allow_close = True
        try:
            for w in list(webview.windows):
                w.destroy()
        except Exception:
            pass
        return True


# =========================================================================== #
#  summary_html mode — WYSIWYG editor for a meeting-summary *.html             #
#                                                                             #
#  Reuses the SAME look as the rendered doc: we load the meeting's            #
#  *_meeting_summary.html (produced by lia._summary_to_html /         #
#  _HTML_SUMMARY_DOC), pull its own <style> + the .card interior, and show    #
#  that interior in a contenteditable .card so it renders identically (perfect#
#  Hebrew RTL — the browser does full Unicode bidi; Tk can't). On Save we     #
#  swap ONLY the edited card-interior back into the original file's shell so   #
#  the <head>/CSS/.wrap/.footer chrome is preserved byte-for-byte and the doc #
#  stays a clean, shareable file. A one-time *.bak.html keeps the original.    #
# =========================================================================== #

# Used only if the loaded file somehow has no <style> block (shouldn't happen
# for a real Lia summary). Keeps the card readable + RTL regardless.
_FALLBACK_SUMMARY_CSS = (
    ":root{--accent:#22314F;--ink:#1B2430;--muted:#6B7280;--line:#E2E5EA;"
    "--bg:#F4F5F7;--card:#FFFFFF;}*{box-sizing:border-box;}"
    "body{margin:0;background:var(--bg);color:var(--ink);"
    "font-family:'Segoe UI Variable Text','Segoe UI',Heebo,system-ui,Arial,sans-serif;line-height:1.75;font-size:16px;}"
    ".wrap{max-width:740px;margin:44px auto;padding:0 20px;}"
    ".card{background:var(--card);border:1px solid var(--line);border-radius:16px;"
    "padding:34px 38px;}.eyebrow{color:var(--accent);font-weight:700;font-size:12.5px;}"
    "h1{font-size:23px;}h2{font-size:16.5px;border-bottom:2px solid var(--line);"
    "padding-bottom:7px;}p{margin:9px 0;}strong{font-weight:700;}"
    "ul{margin:8px 0;padding:0;list-style:none;}"
    "li{position:relative;padding-inline-start:22px;margin:8px 0;}"
    "li::before{content:'\\2022';color:var(--accent);position:absolute;"
    "inset-inline-start:0;font-weight:700;}"
    "li.task{padding-inline-start:30px;}li.task::before{content:'\\2610';"
    "color:var(--accent);}"
)


# The editor page. Nothing is templated in — the doc CSS + card HTML are fetched
# at runtime via pywebview.api.get_initial(), so there are no escaping pitfalls
# (same approach as the compose page above). The doc's own <style> is injected
# as the FIRST element of <head> so the .editorchrome block below (later in the
# document) wins on the few layout rules we override for editing.
SUMMARY_HTML = r"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
<meta charset="utf-8">
<style id="editorchrome">
  html,body{height:100%;}
  .toolbar{position:sticky;top:0;z-index:10;display:flex;gap:6px;align-items:center;
           flex-wrap:wrap;padding:8px 12px;background:#FFFFFF;
           border-bottom:1px solid #E2E5EA;box-shadow:0 1px 3px rgba(16,24,40,.06);}
  .toolbar .grp{display:flex;gap:4px;align-items:center;}
  .toolbar .sp{flex:1 1 auto;}
  .tb{border:1px solid #E2E5EA;background:#EDEFF3;color:#1B2430;border-radius:6px;
      padding:6px 10px;font-size:14px;cursor:pointer;min-width:34px;
      font-family:"Segoe UI Variable Text","Segoe UI",Heebo,system-ui,Arial,sans-serif;}
  .tb:hover{background:#D6DCE8;}
  .tb.primary{background:#22314F;color:#FFFFFF;border-color:#22314F;font-weight:600;}
  .tb.primary:hover{background:#2C3E63;}
  .tb:disabled{opacity:.5;cursor:default;}
  .status{font-size:13px;color:#6B7280;padding:6px 14px 0;min-height:20px;
          font-family:"Segoe UI Variable Text","Segoe UI",Heebo,system-ui,Arial,sans-serif;}
  .status.ok{color:#1a7f37;} .status.err{color:#c0263c;}
  /* editing-context overrides (win over the injected doc CSS) */
  .wrap{margin:14px auto 70px !important;}
  .card[contenteditable]{outline:none;}
  .card[contenteditable]:focus{box-shadow:0 0 0 2px rgba(34,49,79,.18),
        0 1px 2px rgba(16,24,40,.06),0 12px 32px rgba(16,24,40,.05);}
</style>
</head>
<body>
  <div class="toolbar" dir="ltr">
    <div class="grp">
      <button class="tb" id="bBold" title="Bold (Ctrl+B)"><b>B</b></button>
      <button class="tb" id="bH2" title="Section heading">H2</button>
      <button class="tb" id="bP" title="Normal text">&#182;</button>
      <button class="tb" id="bUl" title="Bullet list">&#8226;</button>
      <button class="tb" id="bTask" title="Toggle task checkbox">&#9744;</button>
    </div>
    <div class="grp">
      <button class="tb" id="bUndo" title="Undo (Ctrl+Z)">&#8634;</button>
      <button class="tb" id="bRedo" title="Redo (Ctrl+Y)">&#8635;</button>
    </div>
    <div class="sp"></div>
    <div class="grp">
      <button class="tb primary" id="bSave" title="Save (Ctrl+S)">Save</button>
      <button class="tb" id="bEmailBody"
              title="Open a new Outlook email with the summary formatted IN the message body">&#9993; In email body</button>
      <button class="tb" id="bEmail"
              title="Open a new Outlook email with the summary attached as an .html file">&#128206; As attachment</button>
      <button class="tb" id="bCopy" title="Copy as text">Copy</button>
      <button class="tb" id="bSaveAs" title="Save a separate copy">Save a copy</button>
      <button class="tb" id="bClose" title="Close">Close</button>
    </div>
  </div>
  <div class="status" id="status"></div>
  <div class="wrap">
    <div class="card" id="card" contenteditable="true" spellcheck="false"></div>
  </div>

<script>
  var card, statusEl, exported=false, dirty=false;
  function $(id){ return document.getElementById(id); }
  function setStatus(m,k){ statusEl.textContent=m||''; statusEl.className='status'+(k?(' '+k):''); }
  // Mirror dirty→Python only on the clean→dirty transition (cheap), so the
  // native close guard can decide without an evaluate_js round-trip.
  function markDirty(){ if(!dirty){ dirty=true; try{ pywebview.api.set_dirty(true); }catch(e){} } }

  // No markDirty() here: real edits fire the card's 'input' event (wired below),
  // so a no-op undo/redo/format click never falsely flags unsaved changes.
  function exec(cmd, val){ card.focus(); document.execCommand(cmd, false, val||null); }

  // Every <li> the selection touches, so a multi-line action-items block toggles
  // together (not just the caret's line).
  function selectedLis(){
    var sel=window.getSelection(); if(!sel.rangeCount) return [];
    var range=sel.getRangeAt(0), out=[];
    card.querySelectorAll('li').forEach(function(li){
      if(range.intersectsNode(li)) out.push(li);
    });
    if(!out.length){ // collapsed caret not intersecting an <li> — walk up
      var n=sel.anchorNode;
      while(n && n!==card){ if(n.nodeName==='LI'){ out.push(n); break; } n=n.parentNode; }
    }
    return out;
  }
  function makeTask(){
    card.focus();
    var lis=selectedLis();
    if(!lis.length){ document.execCommand('insertUnorderedList'); lis=selectedLis(); }
    if(lis.length){
      var on = !lis[0].classList.contains('task'); // flip the whole block consistently
      lis.forEach(function(li){ li.classList.toggle('task', on); });
      markDirty(); // a class mutation doesn't fire 'input'
    }
  }

  async function doSave(){
    setStatus('Saving…');
    var r = await pywebview.api.save_summary(card.innerHTML);
    if(r && r.ok){ exported=true; dirty=false; try{ pywebview.api.set_dirty(false); }catch(e){}
                   setStatus('Saved ✓', 'ok'); }
    else { setStatus((r&&r.error)?('Save failed: '+r.error):'Save failed', 'err'); }
  }
  async function doSaveAs(){
    setStatus('Saving a copy…');
    var r = await pywebview.api.save_copy(card.innerHTML);
    if(r && r.ok){ exported=true; setStatus('Copy saved ✓', 'ok'); }
    else { setStatus((r&&r.error)?('Save failed: '+r.error):'Save failed', 'err'); }
  }
  async function doEmail(){
    setStatus('Saving & opening Outlook…');
    var r = await pywebview.api.email_summary(card.innerHTML);
    if(r && r.ok){ exported=true; dirty=false; try{ pywebview.api.set_dirty(false); }catch(e){}
                   setStatus('Saved — opening a new Outlook email (attached)…', 'ok'); }
    else { setStatus((r&&r.error)?('Email failed: '+r.error):'Email failed', 'err'); }
  }
  function cardSubject(){ var h=card.querySelector('h1'); return h ? (h.innerText||'').trim() : ''; }
  // Convert the live card DOM into Outlook-safe HTML. Outlook's Word engine
  // ignores CSS vars, ::before, flex and border-radius — so we use a centered
  // TABLE "card", inline hex colors, real •/☐ glyphs, and hanging-indent bullet
  // rows built from tables. Renders cleanly in Outlook AND looks polished
  // (rounded card + accent bar) in Gmail/Apple Mail/preview.
  var EM_INK='#111827', EM_BODY='#1f2330', EM_MUTED='#6b7280', EM_ACCENT='#4f46e5',
      EM_LINE='#eceef2', EM_FONT="font-family:'Segoe UI',Arial,sans-serif;";
  function emailBlock(el){
    var inner=el.innerHTML, t=el.tagName;
    if(t==='H1')
      return '<h1 style="'+EM_FONT+'font-size:23px;font-weight:bold;color:'+EM_INK+';margin:6px 0 2px;line-height:1.3;">'+inner+'</h1>';
    if(t==='H2')
      return '<h2 style="'+EM_FONT+'font-size:16px;font-weight:bold;color:'+EM_INK+';margin:24px 0 10px;padding-bottom:7px;border-bottom:2px solid '+EM_LINE+';line-height:1.4;">'+inner+'</h2>';
    if(el.classList && el.classList.contains('eyebrow'))
      return '<div style="'+EM_FONT+'color:'+EM_ACCENT+';font-weight:bold;font-size:12px;letter-spacing:.05em;">'+inner+'</div>';
    if(el.classList && el.classList.contains('meta'))
      return '<div style="'+EM_FONT+'color:'+EM_MUTED+';font-size:13px;margin:2px 0 18px;">'+inner+'</div>';
    if(t==='UL'){
      var rows=[];
      el.querySelectorAll(':scope > li').forEach(function(li){
        var mark = li.classList.contains('task') ? '☐' : '•';
        rows.push('<tr>'
          +'<td valign="top" style="'+EM_FONT+'width:22px;padding:0 0 7px;color:'+EM_ACCENT+';font-weight:bold;font-size:15px;line-height:1.6;">'+mark+'</td>'
          +'<td valign="top" style="'+EM_FONT+'padding:0 0 7px;color:'+EM_BODY+';font-size:15px;line-height:1.6;">'+li.innerHTML+'</td>'
          +'</tr>');
      });
      return '<table role="presentation" cellpadding="0" cellspacing="0" border="0" dir="rtl" '
           + 'style="border-collapse:collapse;margin:6px 0 4px;">'+rows.join('')+'</table>';
    }
    if(t==='P')
      return '<p style="'+EM_FONT+'margin:9px 0;color:'+EM_BODY+';font-size:15px;line-height:1.65;">'+inner+'</p>';
    var txt=(el.innerText||'').trim();
    return txt ? '<p style="'+EM_FONT+'margin:9px 0;color:'+EM_BODY+';font-size:15px;line-height:1.65;">'+inner+'</p>' : '';
  }
  function buildEmailHtml(){
    var parts=[];
    Array.prototype.forEach.call(card.childNodes, function(n){
      if(n.nodeType===3){ var s=(n.textContent||'').trim();
        if(s) parts.push('<p style="'+EM_FONT+'margin:9px 0;color:'+EM_BODY+';font-size:15px;line-height:1.65;">'+s+'</p>'); return; }
      if(n.nodeType===1){ var h=emailBlock(n); if(h) parts.push(h); }
    });
    var body = parts.join('\n');
    // Leading blank line = a place to type a greeting above the card.
    return '<p style="'+EM_FONT+'font-size:15px;color:'+EM_BODY+';margin:0 0 4px;">&#160;</p>'
      + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;background:#f4f5f8;">'
      + '<tr><td align="center" style="padding:10px;">'
      + '<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" dir="rtl" '
      +   'style="width:640px;max-width:640px;background:#ffffff;border:1px solid #e6e7eb;'
      +   'border-radius:14px;border-collapse:separate;'+EM_FONT+'">'
      + '<tr><td style="background:'+EM_ACCENT+';height:4px;line-height:4px;font-size:4px;'
      +   'border-radius:14px 14px 0 0;">&#160;</td></tr>'
      + '<tr><td align="right" style="padding:28px 32px 32px;">' + body + '</td></tr>'
      + '</table>'
      + '<div style="'+EM_FONT+'color:#9ca3af;font-size:11px;margin-top:12px;">Generated by Lia</div>'
      + '</td></tr></table>';
  }
  async function doEmailBody(){
    setStatus('Saving & opening Outlook…');
    var s = await pywebview.api.save_summary(card.innerHTML);
    if(!(s && s.ok)){ setStatus((s&&s.error)?('Save failed: '+s.error):'Save failed','err'); return; }
    dirty=false; try{ pywebview.api.set_dirty(false); }catch(e){}
    var r = await pywebview.api.email_body(buildEmailHtml(), cardSubject());
    if(r && r.ok){ exported=true;
      setStatus(r.fellback ? 'Saved — opening a new Outlook email (summary attached)…'
                           : 'Saved — opening a new Outlook email (summary in body)…','ok'); }
    else { setStatus((r&&r.error)?('Email failed: '+r.error):'Email failed','err'); }
  }
  async function doCopy(){
    var ok = await pywebview.api.copy(card.innerText||'');
    exported = exported || ok;
    setStatus(ok?'Copied to clipboard':'Copy failed', ok?'ok':'err');
  }
  // Close = hand off to Python. Python shows a NATIVE confirm (using the
  // mirrored dirty flag) and destroys the window on its own thread. We must NOT
  // call window.confirm() or rely on evaluate_js in the close path — both can
  // deadlock WebView2's UI thread while the window is closing.
  function doClose(){ try{ pywebview.api.close_window(); }catch(e){} }

  function wire(){
    function tb(id, fn){
      var b=$(id);
      b.addEventListener('mousedown', function(e){ e.preventDefault(); }); // keep caret in editor
      b.addEventListener('click', fn);
    }
    tb('bBold', function(){ exec('bold'); });
    tb('bH2',   function(){ exec('formatBlock','H2'); });
    tb('bP',    function(){ exec('formatBlock','P'); });
    tb('bUl',   function(){ exec('insertUnorderedList'); });
    tb('bTask', makeTask);
    tb('bUndo', function(){ exec('undo'); });
    tb('bRedo', function(){ exec('redo'); });
    tb('bSave', doSave); tb('bEmailBody', doEmailBody); tb('bEmail', doEmail);
    tb('bCopy', doCopy); tb('bSaveAs', doSaveAs); tb('bClose', doClose);
    card.addEventListener('input', markDirty);
    document.addEventListener('keydown', function(e){
      if((e.ctrlKey||e.metaKey) && (e.key==='s'||e.key==='S')){ e.preventDefault(); doSave(); }
      if(e.key==='Escape' && document.activeElement!==card){ e.preventDefault(); doClose(); }
    });
  }

  window.addEventListener('pywebviewready', async function(){
    // Bridge is only populated once pywebview is ready; read it here, never at
    // inline-script parse time (see the pywebview-api-defer rule).
    var api = window.pywebview.api;
    card=$('card'); statusEl=$('status');
    wire();
    var d = await api.get_initial();
    document.title = d.title || 'Edit summary';
    document.documentElement.dir = d.dir || 'rtl';
    document.documentElement.lang = d.lang || 'he';
    // Inject the document's OWN css first so the card looks identical to the file.
    var s=document.createElement('style'); s.textContent=d.css||'';
    document.head.insertBefore(s, document.head.firstChild);
    card.innerHTML = d.card_html || '';
    try{ document.execCommand('styleWithCSS', false, false); }catch(e){} // semantic <b>/<i>
    card.focus();
    setStatus('');
  });
</script>
</body>
</html>"""


class SummaryApi:
    """JS<->Python bridge for summary_html mode. Loads the meeting summary file,
    serves its CSS + card interior to the page, and writes edits back into the
    SAME file shell (with a one-time .bak.html). Like Api, it never stores a
    pywebview Window (that makes pywebview recurse into Window.native forever)."""

    def __init__(self, payload):
        self._payload = payload
        self._path = payload.get("path", "")
        self._ready = False        # set True once the page calls get_initial()
        self._init_failed = False  # set by the startup watchdog on a hung load
        self._dirty = False        # mirrored from JS; read by the close guard
        self._result = {"text": "", "exported": False}
        # filled by _parse()
        self._original = ""
        self._css = _FALLBACK_SUMMARY_CSS
        self._dir = "rtl"
        self._lang = "he"
        self._card_inner = ""
        self._prefix = ""   # original up to & incl. <div class="card">
        self._suffix = ""   # original from the card's closing </div> onward
        self._parse()
        self._disk = self._original   # what we believe is currently on disk

    # ---- load ----
    def _parse(self):
        import re as _re
        with open(self._path, "r", encoding="utf-8") as f:
            self._original = f.read()
        o = self._original
        m = _re.search(r"<style[^>]*>(.*?)</style>", o, _re.S | _re.I)
        if m and m.group(1).strip():
            self._css = m.group(1)
        dm = _re.search(r'<html[^>]*\bdir="([^"]+)"', o, _re.I)
        if dm:
            self._dir = dm.group(1)
        lm = _re.search(r'<html[^>]*\blang="([^"]+)"', o, _re.I)
        if lm:
            self._lang = lm.group(1)

        CARD = '<div class="card">'
        i = o.find(CARD)
        if i == -1:
            raise ValueError("not a Lia summary (no .card)")
        open_end = i + len(CARD)
        # Find the .card's OWN closing </div> by depth-balancing div tags from
        # the open tag — independent of any footer marker and robust to nested
        # <div>s the browser may introduce while editing.
        depth = 1
        k = -1
        for mt in _re.finditer(r"<\s*(/?)div\b", o[open_end:], _re.I):
            if mt.group(1):          # a </div...
                depth -= 1
                if depth == 0:
                    k = open_end + mt.start()
                    break
            else:                    # a <div ...>
                depth += 1
        if k == -1:
            raise ValueError("malformed .card (unbalanced <div>)")
        self._card_inner = o[open_end:k].strip("\n")
        self._prefix = o[:open_end]
        self._suffix = o[k:]

    def get_initial(self):
        self._ready = True   # page rendered & WebView2 alive — clears the watchdog
        return {
            "title": self._payload.get("title", "Edit summary"),
            "css": self._css,
            "dir": self._dir,
            "lang": self._lang,
            "card_html": self._card_inner,
        }

    # ---- build / clean ----
    @staticmethod
    def _clean_body(body):
        """Light cleanup so the saved doc stays clean & shareable: prefer the
        semantic <strong>/<em> over <b>/<i> that some engines emit."""
        import re as _re
        b = body or ""
        b = _re.sub(r"<b>", "<strong>", b, flags=_re.I)
        b = _re.sub(r"</b>", "</strong>", b, flags=_re.I)
        b = _re.sub(r"<i>", "<em>", b, flags=_re.I)
        b = _re.sub(r"</i>", "</em>", b, flags=_re.I)
        return b.strip()

    def _build_doc(self, body_html):
        body = self._clean_body(body_html)
        return self._prefix + "\n      " + body + "\n    " + self._suffix

    # ---- save ----
    def save_summary(self, html):
        """Overwrite the original *_meeting_summary.html (atomic), keeping a
        one-time *.bak.html of the very first original."""
        import re as _re
        try:
            # Refuse to clobber edits made to the file since we loaded it (e.g. a
            # second editor window saved meanwhile) — last-writer-wins would lose
            # work silently.
            try:
                with open(self._path, "r", encoding="utf-8") as cf:
                    on_disk = cf.read()
            except Exception:
                on_disk = self._disk
            if on_disk != self._disk:
                return {"ok": False,
                        "error": "file changed on disk — reopen to edit"}
            new_doc = self._build_doc(html)
            bak = _re.sub(r"\.html?$", "", self._path, flags=_re.I) + ".bak.html"
            if not os.path.exists(bak):
                try:
                    with open(bak, "w", encoding="utf-8") as bf:
                        bf.write(self._original)
                except Exception:
                    pass  # backup is best-effort; don't block the save
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_doc)
            os.replace(tmp, self._path)
            self._disk = new_doc
            self._dirty = False
            self._result["exported"] = True
            return {"ok": True, "path": self._path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def save_copy(self, html):
        """Write a separate copy into SUMMARIES_DIR and open it."""
        import datetime
        try:
            os.makedirs(SUMMARIES_DIR, exist_ok=True)
            new_doc = self._build_doc(html)
            base = os.path.splitext(os.path.basename(self._path))[0]
            fname = (datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                     + "_" + base + "_edited.html")
            path = os.path.join(SUMMARIES_DIR, fname)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_doc)
            self._result["exported"] = True
            try:
                os.startfile(path)
            except Exception:
                pass
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def copy(self, text):
        try:
            import pyperclip
            pyperclip.copy(text or "")
            self._result["exported"] = True
            return True
        except Exception:
            return False

    # ---- email ----
    def email_summary(self, html):
        """Save, then open a NEW Outlook email with the summary .html attached
        and the meeting title as the subject — the user picks recipients + sends.
        Never auto-sends."""
        save = self.save_summary(html)
        if not (save and save.get("ok")):
            return {"ok": False, "error": (save or {}).get("error", "save failed")}
        subject = self._subject_from(html)
        path = self._path
        import threading as _th
        out = {}

        def _work():
            try:
                self._open_outlook_attach(path, subject)
                out["ok"] = True
            except Exception as e:
                out["error"] = str(e)

        t = _th.Thread(target=_work, daemon=True)
        t.start()
        t.join(timeout=6)   # warm Outlook returns fast; a cold start keeps going
        if out.get("ok"):
            self._result["exported"] = True
            return {"ok": True}
        if t.is_alive():
            # Outlook is still launching (cold start) — assume the window appears.
            self._result["exported"] = True
            return {"ok": True}
        return {"ok": False, "error": out.get("error", "could not open Outlook")}

    def email_body(self, email_html, subject):
        """Open a NEW Outlook email with the summary formatted IN the body
        (Outlook-safe inline HTML built by the page). Never auto-sends. The
        caller (JS) saves the doc first."""
        subj = (subject or "").strip() or self._subject_from("")
        import threading as _th
        out = {}

        def _work():
            try:
                self._open_outlook_body(email_html or "", subj)
                out["ok"] = True
            except Exception as e:
                # Body path failed — fall back to attaching the saved file so
                # the user still gets a composed email instead of nothing.
                try:
                    self._open_outlook_attach(self._path, subj)
                    out["ok"] = True
                    out["fellback"] = True
                except Exception as e2:
                    out["error"] = "%s (attach fallback: %s)" % (e, e2)

        t = _th.Thread(target=_work, daemon=True)
        t.start()
        t.join(timeout=6)
        if out.get("ok"):
            self._result["exported"] = True
            return {"ok": True, "fellback": bool(out.get("fellback"))}
        if t.is_alive():
            self._result["exported"] = True
            return {"ok": True}
        return {"ok": False, "error": out.get("error", "could not open Outlook")}

    def _subject_from(self, html):
        """Meeting title for the email subject: the edited <h1>, else the file
        name, else a generic fallback."""
        import re as _re
        import html as _htmlmod
        m = _re.search(r"<h1[^>]*>(.*?)</h1>", html or "", _re.S | _re.I)
        if m:
            t = _re.sub(r"<[^>]+>", "", m.group(1))
            t = _htmlmod.unescape(t).replace("‏", "").replace("‎", "").strip()
            if t:
                return t
        base = os.path.splitext(os.path.basename(self._path))[0]
        base = base.replace("_meeting_summary", "").strip("_ ")
        return base or "Meeting summary"

    def _open_outlook_attach(self, path, subject):
        """Open a new Outlook compose window with `path` attached. Prefer COM
        (sets the subject too); fall back to OUTLOOK.EXE /a. Raises on failure."""
        last = None
        com_inited = False
        # 1) Outlook COM. CreateItem(0)=olMailItem; Display(False)=modeless, NO
        #    send. CoInitialize() is tolerated if this thread is already in an
        #    apartment (WebView2/.NET) — COM still works either way.
        try:
            import pythoncom
            import win32com.client
            try:
                pythoncom.CoInitialize()
                com_inited = True
            except Exception:
                pass
            try:
                ol = win32com.client.Dispatch("Outlook.Application")
                mail = ol.CreateItem(0)
                if subject:
                    mail.Subject = subject
                # Display FIRST, then attach. Outlook applies the default
                # signature on Display and resets the item, which DROPS any
                # attachment added beforehand (Attachments.Count goes 1 -> 0 on
                # Display). Attaching after the window is open keeps the file
                # (verified: the .html survives alongside the signature images).
                mail.Display(False)
                mail.Attachments.Add(path)
                try:
                    mail.GetInspector.Activate()   # pop the compose window to front
                except Exception:
                    pass
                return
            finally:
                if com_inited:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass
        except Exception as e:
            last = e
        # 2) OUTLOOK.EXE /a "<file>" — opens a new message with the attachment.
        exe = self._find_outlook_exe()
        if exe:
            import subprocess
            try:
                subprocess.Popen([exe, "/a", path], creationflags=0x08000000)
                return
            except Exception as e:
                last = e
        raise RuntimeError("Outlook unavailable: %s" % (last,))

    def _open_outlook_body(self, html, subject):
        """Open a new Outlook compose window with `html` as the HTML body.
        COM only — the CLI can't set a body; raise so the page shows an error.

        Order matters: Display the (empty) inspector FIRST, THEN assign
        HTMLBody. Assigning the body BEFORE Display makes Outlook render the
        whole document synchronously inside the Display() COM call — for a long
        summary (a ~60KB+ body) that HANGS Outlook indefinitely and wedges its
        COM server, so even the attach path then fails ("nothing opens"). With
        the window already open, the body renders in the inspector's own message
        loop and Display+assign stay sub-second even past 250KB (measured)."""
        com_inited = False
        try:
            import pythoncom
            import win32com.client
            try:
                pythoncom.CoInitialize()
                com_inited = True
            except Exception:
                pass   # already in an apartment (WebView2/.NET) — proceed
            try:
                ol = win32com.client.Dispatch("Outlook.Application")
                mail = ol.CreateItem(0)        # olMailItem
                if subject:
                    mail.Subject = subject
                mail.Display(False)            # open the empty inspector FIRST
                mail.HTMLBody = html           # then populate (renders in-window)
                try:
                    mail.GetInspector.Activate()   # bring the window to the front
                except Exception:
                    pass
                return
            finally:
                if com_inited:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass
        except Exception as e:
            raise RuntimeError("Outlook COM unavailable: %s" % (e,))

    @staticmethod
    def _find_outlook_exe():
        try:
            import winreg
        except Exception:
            return None
        for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\OUTLOOK.EXE",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\OUTLOOK.EXE"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                    val, _ = winreg.QueryValueEx(k, None)
                    if val and os.path.exists(val):
                        return val
            except Exception:
                continue
        return None

    # ---- dirty mirror + close ----
    def set_dirty(self, value):
        # Mirror the JS dirty flag so the close guard (which runs on the UI
        # thread during the closing event) can decide WITHOUT an evaluate_js
        # round-trip — that round-trip deadlocks WebView2 while closing.
        self._dirty = bool(value)
        return True

    def close_window(self):
        # Destroy on a fresh thread so this JS bridge call returns immediately
        # and destroy() never runs while the API/UI thread is held.
        import threading as _th

        def _destroy():
            try:
                for w in list(webview.windows):
                    w.destroy()
            except Exception:
                pass
        _th.Thread(target=_destroy, daemon=True).start()
        return True


def _run_summary_editor(payload):
    """summary_html mode: open the WYSIWYG meeting-summary editor."""
    try:
        api = SummaryApi(payload)
    except Exception as e:
        sys.stderr.write("compose_editor: cannot load summary: %r\n" % (e,))
        sys.exit(3)  # parent treats non-zero as failure -> os.startfile fallback

    try:
        win = webview.create_window(payload.get("title", "Edit summary"),
                                    html=SUMMARY_HTML, js_api=api,
                                    width=900, height=860, min_size=(560, 520))

        def _on_closing(*_a):
            # Runs on the WebView2 UI thread during the form's closing event.
            # MUST NOT call evaluate_js here — it deadlocks (the UI thread would
            # block waiting for JS that only this same blocked thread can run),
            # which is exactly the "not responding" hang on Close/X. Use the
            # mirrored dirty flag + a native MessageBox instead.
            if not api._dirty:
                return True
            try:
                import ctypes
                # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
                r = ctypes.windll.user32.MessageBoxW(
                    0, "Close without saving? Your edits will be lost.",
                    "Lia", 0x4 | 0x30 | 0x10000 | 0x40000)
                return r == 6  # IDYES → allow close; else cancel
            except Exception:
                return True   # never block the close on a dialog failure
        try:
            win.events.closing += _on_closing
        except Exception:
            pass

        # Startup watchdog: if the page never becomes ready (a missing/broken
        # WebView2 runtime can make pywebview HANG rather than raise), force the
        # window shut so this process exits non-zero and the parent falls back
        # to opening the HTML in the browser. Long EDITING sessions are fine —
        # this deadline only covers initial load (get_initial flips _ready).
        import threading as _th
        import time as _time

        def _watchdog():
            _time.sleep(30)
            if not api._ready:
                api._init_failed = True
                try:
                    for w in list(webview.windows):
                        w.destroy()
                except Exception:
                    pass
        _th.Thread(target=_watchdog, daemon=True).start()

        import ui_kit as uk  # Lia window icon
        uk.webview_start(webview)
    except Exception as e:
        sys.stderr.write("compose_editor: summary editor failed: %r\n" % (e,))
        sys.exit(3)

    if api._init_failed:
        sys.stderr.write("compose_editor: summary editor init timed out\n")
        sys.exit(3)

    try:
        sys.stdout.write("COMPOSE_RESULT "
                         + json.dumps(api._result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("compose_editor: missing payload path\n")
        sys.exit(2)
    try:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        sys.stderr.write("compose_editor: bad payload: %r\n" % (e,))
        sys.exit(2)

    # summary_html mode — the WYSIWYG meeting-summary editor (no LLM needed).
    if payload.get("mode") == "summary_html":
        _run_summary_editor(payload)
        return

    cfg = load_config()
    cleaner = None
    key = (cfg.get("openai_api_key", "") or "").strip()
    if key and OpenAILLMCleaner is not None:
        try:
            cleaner = OpenAILLMCleaner(api_key=key,
                                       model=cfg.get("summary_model", "gpt-5.5"))
        except Exception as e:
            sys.stderr.write("compose_editor: cleaner init failed: %r\n" % (e,))

    api = Api(payload, cleaner)
    win = webview.create_window(payload.get("title", "Compose"), html=HTML,
                                js_api=api, width=880, height=820,
                                min_size=(560, 480))

    # Confirm-on-discard for the window's X button: cancel the first close and
    # let the page run its confirm(); JS re-triggers via request_close if OK.
    def _on_closing(*_a):
        if api._allow_close or api._result.get("exported"):
            return True
        try:
            win.evaluate_js("askCloseConfirm()")
        except Exception:
            return True
        return False
    try:
        win.events.closing += _on_closing
    except Exception:
        pass

    import ui_kit as uk  # Lia window icon
    uk.webview_start(webview)

    # Window closed — hand the final text back to the parent (one line).
    try:
        sys.stdout.write("COMPOSE_RESULT " + json.dumps(api._result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


if __name__ == "__main__":
    main()
