# -*- coding: utf-8 -*-
"""Lia - Action-item tracker: one view of every open task across all meetings.

Meeting summaries emit a "משימות" section with `- [ ]` items (verified across
the full meeting archive; the source files are immutable snapshots, so "done"
is tracked HERE, in a separate store, not in the files). This module
parses those sections, aggregates them, persists a done-state, and serves a
small pywebview window.

Shared by:
  - action_items.py (main())  - the pywebview window (spawned by the tray)
  - lia.py            - tray wiring / hotkey

Reuses meetings_index for file enumeration + meeting identity (title/date/id) so
the tracker and the RAG index agree on which file is which. 100% local: plain
text parsing, no LLM / no Ollama / no network.

Store: %APPDATA%/Lia/action_items.json = { task_id: {"done": true, "ts": .. } }

ALWAYS launch with  python -X utf8  (Hebrew stdout crashes under cp1252).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meetings_index as mi

# Done-state store (the source .txt files can't hold it - they're snapshots).
STORE_PATH = os.path.join(mi._appdata_base(), "Lia", "action_items.json")

# The saved .txt has the plain word "משימות" / "Tasks" (the markdown "##" is
# stripped by lia._strip_md_markup - READ, not inferred). Items are
# single-line bullets; the section is followed by the thick ═ separator.
# UNION by design (2026-08): Hebrew and English meeting files share this parser.
_TASK_HEADERS = ("משימות", "Tasks")
_TASK_HEADER = _TASK_HEADERS[0]   # back-compat alias
_BULLET_RE = re.compile(r"^[-*]\s*(?:\[[ xX]?\]\s*)?(.+)$")
_SEP = ({"═"}, {"─"})
_STOP_LINES = ("Full transcript", "Transcript")


def _task_id(entry_id, text):
    h = hashlib.sha1()
    h.update((entry_id or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update((text or "").strip().encode("utf-8", "replace"))
    return h.hexdigest()[:16]


def parse_action_items(path):
    """Return the OPEN task lines from a meeting file's משימות section (text
    only, bullet/checkbox prefix removed). [] when the meeting has no section
    or an empty one. Verified against the full meeting archive."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = [mi._strip_marks(l) for l in f.read().splitlines()]
    except OSError:
        return []
    try:
        i = next(k for k, l in enumerate(lines) if l.strip() in _TASK_HEADERS)
    except StopIteration:
        return []
    items = []
    seen_bullet = False
    for l in lines[i + 1:]:
        s = l.strip()
        if not s:
            continue
        cs = set(s)
        if cs <= _SEP[0] or cs <= _SEP[1] or s in _STOP_LINES:
            break
        m = _BULLET_RE.match(s)
        if m:
            text = m.group(1).strip()
            if text:
                items.append(text)
                seen_bullet = True
        elif seen_bullet:
            break              # a non-bullet line after bullets = next section (defensive)
    return items


def collect_items():
    """Aggregate every meeting's tasks, newest meeting first. Merges the done
    store. Returns a list of meeting groups:
    [{meeting_id, title, date, ts, items:[{task_id, text, done, done_ts}]}]."""
    store = load_store()
    try:
        names = sorted(os.listdir(mi.MEETINGS_SRC_DIR))
    except OSError:
        names = []
    groups = []
    for name in names:
        if not mi.is_indexable_meeting(name):
            continue
        path = os.path.join(mi.MEETINGS_SRC_DIR, name)
        texts = parse_action_items(path)
        if not texts:
            continue
        meta = mi.parse_meeting_file(path) or {}
        entry_id = meta.get("entry_id") or name
        items = []
        for t in texts:
            tid = _task_id(entry_id, t)
            st = store.get(tid) or {}
            items.append({"task_id": tid, "text": t,
                          "done": bool(st.get("done")), "done_ts": st.get("ts", 0)})
        groups.append({
            "meeting_id": entry_id,
            "title": meta.get("subject") or name,
            "ts": meta.get("received_ts") or 0,
            "date": mi._fmt_date(meta.get("received_ts") or 0),
            "items": items,
        })
    groups.sort(key=lambda g: g["ts"], reverse=True)
    return groups


# ---------------------------------------------------------------------------
# Done-state store (atomic, mirrors meetings_index._save_state)
# ---------------------------------------------------------------------------
def load_store():
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_store(store):
    try:
        os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
        tmp = STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp, STORE_PATH)
        return True
    except OSError:
        return False


def set_done(task_id, done, ts=0):
    """Persist a single item's done-state. Returns True on write."""
    store = load_store()
    if done:
        store[task_id] = {"done": True, "ts": int(ts or 0)}
    else:
        store.pop(task_id, None)
    return save_store(store)


# ---------------------------------------------------------------------------
# pywebview bridge + window
# ---------------------------------------------------------------------------
class ActionsApi:
    def __init__(self, payload):
        self._payload = payload or {}

    def get_items(self, open_only=True):
        groups = collect_items()
        total = sum(len(g["items"]) for g in groups)
        open_n = sum(1 for g in groups for it in g["items"] if not it["done"])
        if open_only:
            groups = [dict(g, items=[it for it in g["items"] if not it["done"]])
                      for g in groups]
            groups = [g for g in groups if g["items"]]
        return {"groups": groups, "total": total, "open": open_n}

    def set_done(self, task_id, done):
        import time
        return set_done(task_id, bool(done),
                        ts=(int(time.time()) if done else 0))

    def open_meeting(self, meeting_id):
        try:
            base = os.path.basename(meeting_id or "")   # no path escape
            path = os.path.join(mi.MEETINGS_SRC_DIR, base)
            if os.path.exists(path):
                os.startfile(path)
                return True
        except Exception as e:
            sys.stderr.write("open_meeting failed: %r\n" % (e,))
        return False

    def copy_open(self):
        """Plain-text list of all OPEN items (JS copies it to the clipboard)."""
        out = []
        for g in collect_items():
            open_items = [it for it in g["items"] if not it["done"]]
            if not open_items:
                continue
            out.append("## %s (%s)" % (g["title"], g["date"]))
            out += ["- [ ] " + it["text"] for it in open_items]
            out.append("")
        return "\n".join(out).strip()


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{ --bg:#F4F5F7; --panel:#FFFFFF; --line:#E2E5EA; --fg:#1B2430; --muted:#6B7280;
         --accent:#22314F; --chip:#EDEFF3; --done:#9AA0AD; }
  *{ box-sizing:border-box; }
  body{ margin:0; background:var(--bg); color:var(--fg); height:100vh; display:flex; flex-direction:column;
        font-family:"Segoe UI Variable Text","Segoe UI",Heebo,system-ui,Arial,sans-serif; }
  header{ padding:14px 18px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:12px; }
  header h1{ font-size:16px; margin:0; font-weight:600; }
  header .count{ color:var(--muted); font-size:12px; }
  .btn{ background:var(--chip); color:var(--fg); border:1px solid var(--line); border-radius:8px;
        padding:6px 12px; font-size:13px; cursor:pointer; }
  .btn.on{ border-color:var(--accent); color:var(--accent); }
  .spacer{ margin-inline-start:auto; }
  .wrap{ flex:1; overflow:auto; padding:16px 18px; }
  .grp{ margin-bottom:20px; }
  .grp .h{ font-weight:600; font-size:14px; cursor:pointer; padding:6px 2px; border-bottom:1px solid var(--line); }
  .grp .h:hover{ color:var(--accent); }
  .grp .h .d{ color:var(--muted); font-weight:400; font-size:12px; margin-inline-start:8px; }
  .it{ display:flex; align-items:flex-start; gap:10px; padding:8px 4px; }
  .it input{ margin-top:3px; width:16px; height:16px; accent-color:var(--accent); cursor:pointer; }
  .it .t{ font-size:14px; line-height:1.55; }
  .it.done .t{ color:var(--done); text-decoration:line-through; }
  .empty{ color:var(--muted); text-align:center; margin-top:48px; }
</style></head>
<body>
  <header>
    <h1>📋 Action items</h1>
    <span class="count" id="count"></span>
    <span class="spacer"></span>
    <button class="btn on" id="toggle">Open only</button>
    <button class="btn" id="copy">Copy open</button>
  </header>
  <div class="wrap" id="wrap"><div class="empty">Loading…</div></div>
<script>
// window.pywebview is not populated at inline-script parse time; touching .api
// here throws and kills the whole script. Assign it inside load() (fired by
// pywebviewready), matching email_search / chat_window.
var API = null;
var openOnly = true;
// Full escape incl. quotes (mirrors RK.esc) - values land in data-* attributes.
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// No interpolated inline handlers (a quote in a meeting title/id must never
// reach executable JS): ids ride in data-* attributes, one delegated listener
// per event type on #wrap handles clicks/toggles.
function itemRow(it){
  return '<label class="it'+(it.done?' done':'')+'">'
    + '<input type="checkbox" '+(it.done?'checked':'')+' data-task="'+esc(it.task_id)+'">'
    + '<span class="t" dir="auto">'+esc(it.text)+'</span></label>';
}
function grpCard(g){
  return '<div class="grp"><div class="h" dir="auto" data-mid="'+esc(g.meeting_id)+'">'
    + esc(g.title)+'<span class="d">'+esc(g.date)+'</span></div>'
    + g.items.map(itemRow).join('') + '</div>';
}
document.getElementById('wrap').addEventListener('click', function(ev){
  var h = ev.target.closest('.h');
  if(h && h.dataset.mid !== undefined){ API.open_meeting(h.dataset.mid); }
});
document.getElementById('wrap').addEventListener('change', function(ev){
  var cb = ev.target;
  if(cb && cb.type === 'checkbox' && cb.dataset.task !== undefined){
    toggle(cb.dataset.task, cb.checked, cb);
  }
});

async function toggle(id, done, el){
  await API.set_done(id, done);
  var row = el.closest('.it');
  if(done) row.classList.add('done'); else row.classList.remove('done');
  if(openOnly && done){ setTimeout(load, 250); }   // it leaves the open view
  refreshCount();
}

async function refreshCount(){
  var r = await API.get_items(false);
  document.getElementById('count').textContent = r.open + ' open · ' + r.total + ' total';
}

async function load(){
  if(!API) API = window.pywebview.api;
  var r = await API.get_items(openOnly);
  document.getElementById('count').textContent = r.open + ' open · ' + r.total + ' total';
  var wrap = document.getElementById('wrap');
  if(!r.groups.length){ wrap.innerHTML = '<div class="empty">'+(openOnly?'No open action items 🎉':'No action items found')+'</div>'; return; }
  wrap.innerHTML = r.groups.map(grpCard).join('');
}

document.getElementById('toggle').onclick = function(){
  openOnly = !openOnly;
  this.textContent = openOnly ? 'Open only' : 'All';
  this.classList.toggle('on', openOnly);
  load();
};
document.getElementById('copy').onclick = async function(){
  var txt = await API.copy_open();
  try{ await navigator.clipboard.writeText(txt); this.textContent='Copied ✓'; var b=this; setTimeout(function(){b.textContent='Copy open';},1200);}catch(e){}
};

window.addEventListener('pywebviewready', load);
</script>
</body></html>"""


def main():
    try:
        import webview
    except Exception as e:  # pragma: no cover
        sys.stderr.write("action_items: pywebview not available: %r\n" % (e,))
        sys.exit(2)

    if sys.stdout is None or sys.stderr is None:
        try:
            os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
            _sink = open(os.path.join(os.path.dirname(STORE_PATH), "action_items.log"),
                         "a", encoding="utf-8")
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
            sys.stderr.write("action_items: bad payload: %r\n" % (e,))

    api = ActionsApi(payload)
    try:
        webview.create_window(
            payload.get("title", "Action items"), html=HTML, js_api=api,
            width=820, height=760, min_size=(520, 460))
        import ui_kit as uk  # Lia window icon
        uk.webview_start(webview)
    except Exception as e:
        sys.stderr.write("action_items: failed: %r\n" % (e,))
        sys.exit(3)


if __name__ == "__main__":
    main()
