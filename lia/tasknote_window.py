# -*- coding: utf-8 -*-
"""Lia - Task note: a small, always-on-top sticky for the things you need to do.

A frictionless personal to-do pad (distinct from action_items.py, which tracks
tasks EXTRACTED from meetings). Add by typing here, or by voice from the parent
app (a hotkey -> speak -> the line is appended to the same tasks.json). This
window reads/writes tasks_store.py DIRECTLY (no IPC); it polls the store's
version so a voice-add from the parent shows up within ~1.5s.

Spawned by lia.py (_show_task_note). Small, on_top, geometry remembered
("tasknote"). RTL-aware for Hebrew content (dir="auto" throughout).

ALWAYS launch with  python -X utf8  (Hebrew stdout crashes under cp1252).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tasks_store as ts


class TasksApi:
    def __init__(self, payload):
        self._payload = payload or {}

    def get(self):
        tasks = ts.all_tasks()
        return {
            "open": [t for t in tasks if not t.get("done")],
            "done": [t for t in tasks if t.get("done")],
            "counts": ts.counts(),
            "version": ts.version(),
        }

    def version(self):
        return ts.version()

    def add(self, text):
        t = ts.add(text)
        return {"ok": bool(t), "task": t, "version": ts.version()}

    def set_done(self, task_id, done):
        ts.set_done(task_id, bool(done))
        return {"version": ts.version()}

    def delete(self, task_id):
        ts.delete(task_id)
        return {"version": ts.version()}

    def clear_done(self):
        n = ts.clear_done()
        return {"cleared": n, "version": ts.version()}


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{ --bg:#FFFDF3; --panel:#FFFFFF; --line:#ECE7D3; --fg:#232019; --muted:#8A8570;
         --accent:#C99A2E; --done:#B4AF9C; }
  *{ box-sizing:border-box; }
  body{ margin:0; background:var(--bg); color:var(--fg); height:100vh; display:flex; flex-direction:column;
        font-family:"Segoe UI Variable Text","Segoe UI",Heebo,system-ui,Arial,sans-serif; font-size:14px; }
  header{ padding:10px 12px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:8px; }
  header h1{ font-size:14px; margin:0; font-weight:700; }
  header .count{ color:var(--muted); font-size:12px; }
  .addbar{ padding:8px 10px; border-bottom:1px solid var(--line); display:flex; gap:6px; }
  .addbar input{ flex:1; border:1px solid var(--line); border-radius:8px; padding:8px 10px; font-size:14px;
                 background:var(--panel); color:var(--fg); }
  .addbar input:focus{ outline:none; border-color:var(--accent); }
  .addbar button{ border:1px solid var(--line); background:var(--panel); border-radius:8px; width:36px;
                  font-size:18px; color:var(--accent); cursor:pointer; }
  .wrap{ flex:1; overflow:auto; padding:6px 8px 12px; }
  .it{ display:flex; align-items:flex-start; gap:9px; padding:7px 6px; border-radius:8px; }
  .it:hover{ background:#00000008; }
  .it input[type=checkbox]{ margin-top:2px; width:16px; height:16px; accent-color:var(--accent); cursor:pointer; flex:none; }
  .it .t{ flex:1; line-height:1.5; word-break:break-word; }
  .it.done .t{ color:var(--done); text-decoration:line-through; }
  .it .del{ visibility:hidden; border:none; background:none; color:var(--muted); cursor:pointer; font-size:15px;
            line-height:1; padding:2px 4px; flex:none; }
  .it:hover .del{ visibility:visible; }
  .empty{ color:var(--muted); text-align:center; margin-top:40px; font-size:13px; }
  .donehdr{ margin:12px 4px 2px; color:var(--muted); font-size:12px; font-weight:600; cursor:pointer;
            display:flex; align-items:center; gap:6px; user-select:none; }
  .donehdr .clr{ margin-inline-start:auto; color:var(--accent); font-weight:600; }
  .donelist.collapsed{ display:none; }
</style></head>
<body>
  <header>
    <h1>&#128221; Tasks</h1>
    <span class="count" id="count"></span>
  </header>
  <div class="addbar">
    <input id="add" type="text" dir="auto" placeholder="Add a task, press Enter&hellip;" autocomplete="off">
    <button id="addbtn" title="Add">+</button>
  </div>
  <div class="wrap" id="wrap"><div class="empty">Loading&hellip;</div></div>
<script>
// window.pywebview.api is not populated at parse time (mirrors action_items /
// email_search): assign it inside load(), fired by pywebviewready.
var API = null;
var doneOpen = false;
var lastVer = null;
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

function itemRow(it, done){
  return '<div class="it'+(done?' done':'')+'">'
    + '<input type="checkbox" '+(done?'checked':'')+' data-task="'+esc(it.id)+'">'
    + '<span class="t" dir="auto">'+esc(it.text)+'</span>'
    + '<button class="del" data-del="'+esc(it.id)+'" title="Delete">&times;</button></div>';
}

function render(r){
  var wrap = document.getElementById('wrap');
  var c = r.counts||{open:0,done:0,total:0};
  document.getElementById('count').textContent = c.open + ' open' + (c.done? ' · '+c.done+' done':'');
  var html = '';
  if(!(r.open||[]).length){
    html += '<div class="empty">'+(c.done? 'All done 🎉' : 'No tasks yet. Add one above,\nor say one with your voice hotkey.')+'</div>';
  } else {
    html += r.open.map(function(it){ return itemRow(it,false); }).join('');
  }
  if((r.done||[]).length){
    html += '<div class="donehdr" id="donehdr"><span>'+(doneOpen?'▾':'▸')+'</span>'
      + 'Done ('+r.done.length+')<span class="clr" id="clrdone">Clear</span></div>'
      + '<div class="donelist'+(doneOpen?'':' collapsed')+'" id="donelist">'
      + r.done.map(function(it){ return itemRow(it,true); }).join('') + '</div>';
  }
  wrap.innerHTML = html;
}

async function reload(){
  if(!API) API = window.pywebview.api;
  var r = await API.get();
  lastVer = r.version;
  render(r);
}

// Poll the store version so a voice-add from the parent app shows up. Skip the
// re-render while the user is typing in the add box (never yank focus/text).
async function poll(){
  try{
    if(!API) return;
    var addEl = document.getElementById('add');
    if(document.activeElement === addEl && addEl.value) return;
    var v = await API.version();
    if(v !== lastVer) await reload();
  }catch(e){}
}

async function addTask(){
  var el = document.getElementById('add');
  var v = (el.value||'').trim();
  if(!v) return;
  el.value = '';
  el.focus();                                  // stay ready for the next one
  await API.add(v);
  await reload();
}

document.getElementById('addbtn').onclick = addTask;
document.getElementById('add').addEventListener('keydown', function(ev){
  if(ev.key === 'Enter' || ev.keyCode === 13){ ev.preventDefault(); addTask(); }
});

document.getElementById('wrap').addEventListener('change', async function(ev){
  var cb = ev.target;
  if(cb && cb.type === 'checkbox' && cb.dataset.task !== undefined){
    await API.set_done(cb.dataset.task, cb.checked);
    await reload();
  }
});
document.getElementById('wrap').addEventListener('click', async function(ev){
  var del = ev.target.closest('[data-del]');
  if(del){ await API.delete(del.getAttribute('data-del')); await reload(); return; }
  var clr = ev.target.closest('#clrdone');
  if(clr){ ev.stopPropagation(); await API.clear_done(); await reload(); return; }
  var hdr = ev.target.closest('#donehdr');
  if(hdr){ doneOpen = !doneOpen;
    var l = document.getElementById('donelist'); if(l) l.classList.toggle('collapsed', !doneOpen);
    var caret = hdr.querySelector('span'); if(caret) caret.textContent = doneOpen?'▾':'▸'; }
});

window.addEventListener('pywebviewready', function(){
  reload();
  setInterval(poll, 1500);
  var el = document.getElementById('add'); if(el) el.focus();
});
</script>
</body></html>"""


def main():
    try:
        import webview
    except Exception as e:  # pragma: no cover
        sys.stderr.write("tasknote: pywebview not available: %r\n" % (e,))
        sys.exit(2)

    try:
        import ui_kit as uk
    except Exception:
        uk = None

    if uk is not None:
        uk.install_stdout_sink(os.path.join(os.path.dirname(ts.STORE_PATH), "tasknote.log"))

    payload = uk.read_payload() if uk is not None else {}

    api = TasksApi(payload)
    geo = (uk.window_geometry("tasknote", {"width": 360, "height": 480})
           if uk is not None else {"width": 360, "height": 480})
    kw = {"width": int(geo.get("width", 360)), "height": int(geo.get("height", 480)),
          "min_size": (300, 300), "on_top": True}
    if isinstance(geo.get("x"), int) and isinstance(geo.get("y"), int):
        kw["x"], kw["y"] = geo["x"], geo["y"]
    try:
        win = webview.create_window(payload.get("title", "Tasks - Lia"),
                                    html=HTML, js_api=api, **kw)
        if uk is not None:
            uk.attach_geometry_memory(win, "tasknote")
            uk.webview_start(webview)
        else:
            webview.start()
    except Exception as e:
        sys.stderr.write("tasknote: failed: %r\n" % (e,))
        sys.exit(3)


if __name__ == "__main__":
    main()
