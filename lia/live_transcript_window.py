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
import ui_i18n  # noqa: E402  the interface language (window title)

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
        # live speaker labels (2026-11-08): the meeting writes <live>.voices.json
        # (which voice each paragraph is), this window writes <live>.names.json
        # (the names the user gives and "same person as" merges)
        self._voices_path = (self._live_path + ".voices.json") if self._live_path else ""
        self._names_path = (self._live_path + ".names.json") if self._live_path else ""
        self._seen_live = False

    def meta(self):
        return {
            "title": self._payload.get("title") or "Live transcript - Lia",
            "started": self._payload.get("started") or "",
            "started_at": _started_at(self._live_path),
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
        vm, vs = self._stat(self._voices_path) if self._voices_path else (0.0, 0)
        if lm:
            self._seen_live = True
        return "%.3f:%d:%.3f:%d:%d:%.3f:%d" % (lm, ls, sm, ss, 1 if self._seen_live else 0,
                                              vm, vs)

    def _read_voices(self):
        if not self._voices_path:
            return None
        try:
            with open(self._voices_path, encoding="utf-8") as f:
                v = json.load(f)
            return v if isinstance(v, dict) else None
        except (OSError, ValueError):
            return None

    _LABEL = __import__("re").compile(r"^[A-H]$")

    def _names(self):
        try:
            with open(self._names_path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return {"names": dict(d.get("names") or {}), "merge": dict(d.get("merge") or {})}
        except (OSError, ValueError):
            pass
        return {"names": {}, "merge": {}}

    def _save_names(self, d):
        tmp = self._names_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, self._names_path)

    def set_name(self, v, name):
        """Name a voice (label A-H); "" clears the name. The meeting picks it up
        within a second and writes it on every paragraph of that voice."""
        import re
        if not self._names_path or not self._LABEL.match(str(v or "")):
            return {"ok": False}
        name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()[:40]
        try:
            d = self._names()
            if name:
                d["names"][v] = name
            else:
                d["names"].pop(v, None)
            self._save_names(d)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def merge(self, v, into):
        """"Same person as": voice `v` is shown as voice `into` from now on."""
        if not self._names_path or not (self._LABEL.match(str(v or ""))
                                        and self._LABEL.match(str(into or ""))) or v == into:
            return {"ok": False}
        try:
            d = self._names()
            root, seen = into, set()
            while root in d["merge"] and root not in seen:
                seen.add(root)
                root = d["merge"][root]
            if root == v:
                return {"ok": False}
            d["merge"][v] = root
            d["names"].pop(v, None)
            self._save_names(d)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def _read_status(self):
        if not self._status_path:
            return None
        try:
            with open(self._status_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def draft(self):
        """The live draft (<live>.draft.json): the words of the last seconds,
        before the meeting model's text. '' when there is none."""
        if not self._live_path:
            return {"text": ""}
        try:
            with open(self._live_path + ".draft.json", encoding="utf-8") as f:
                d = json.load(f)
            return {"text": str((d or {}).get("text") or "")[:4000]}
        except (OSError, ValueError):
            return {"text": ""}

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
                "voices": self._read_voices(),
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
            return {"status": "ok", "phase": "recording", "text": text, "have_live": True,
                    "voices": self._read_voices()}
        return {"status": "ok",
                "phase": "ended" if self._seen_live else "waiting",
                "text": "", "have_live": False, "voices": self._read_voices()}

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

    DIAG_MAX = 256 * 1024

    def diag(self, msg):
        """One line in <meetings>/live_transcript.log (2026-11-09: a window
        showed its title and buttons but no text, and nothing recorded why).
        The page reports its first render, any script error, and a render
        whose lines are not visible. Never raises; no transcript text."""
        try:
            import time
            d = os.path.dirname(self._live_path or "")
            if not d:
                return False
            p = os.path.join(d, "live_transcript.log")
            try:
                if os.path.getsize(p) > self.DIAG_MAX:
                    os.replace(p, p + ".1")
            except OSError:
                pass
            line = "%s pid=%d %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(),
                                         os.path.basename(self._live_path)[:19],
                                         str(msg).replace("\n", " ")[:400])
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
            return True
        except Exception:
            return False

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

    def default_filename(self):
        """'<meeting title> <yyyy-mm-dd HH-MM>.txt', safe for Windows."""
        import re
        import time
        title = (self._payload.get("title") or "Live transcript").strip()
        title = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title)
        title = re.sub(r"\s+", " ", title).strip(" .") or "Live transcript"
        return "%s %s.txt" % (title[:80], time.strftime("%Y-%m-%d %H-%M"))

    def save_txt(self, text):
        """'Save .txt' (Naor, 2026-10-05): a Save-as dialog, then the text the
        window shows ("[m:ss] line" per line) as UTF-8 with a short header.
        The user picks the path; only a .txt is written."""
        text = (text or "")[:2_000_000]
        if not text.strip():
            return {"ok": False, "error": "Nothing to save yet"}
        try:
            import webview
            wins = list(getattr(webview, "windows", []) or [])
            win = wins[0] if wins else None
            if win is None:
                return {"ok": False, "error": "no window"}
            start = os.path.join(os.path.expanduser("~"), "Documents")
            if not os.path.isdir(start):
                start = os.path.expanduser("~")
            res = win.create_file_dialog(webview.SAVE_DIALOG, directory=start,
                                         save_filename=self.default_filename(),
                                         file_types=("Text files (*.txt)",))
            if not res:
                return {"ok": False, "cancelled": True}
            path = res[0] if isinstance(res, (list, tuple)) else res
            if not str(path).lower().endswith(".txt"):
                path = str(path) + ".txt"
            meta = self.meta()
            head = meta["title"] + ((" - started " + meta["started"]) if meta["started"] else "")
            with open(path, "w", encoding="utf-8") as f:
                f.write(head + "\n\n" + text.rstrip() + "\n")
            return {"ok": True, "path": path, "name": os.path.basename(path)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}



def _started_at(live_path):
    """The meeting's start as 'YYYY-MM-DDTHH:MM:SS' from the LIVE file name
    ('2026-10-12_09-55-55_meeting_LIVE.txt'), for the window's clock; "" when
    the name has no stamp (the clock then stays hidden)."""
    import re
    m = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})_", os.path.basename(live_path or ""))
    return ("%sT%s:%s:%s" % m.groups()) if m else ""


def _with_csp(html):
    """The page with ui_kit's Content-Security-Policy (audit 2026-09-26 F13)."""
    import ui_kit
    return ui_kit.brand_html(html)

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  /* the Lia Lite language (ui_kit.brand_html adds the typeface, Lia's tokens + orb) */
  :root{ --bg:#F6F6F3; --panel:#FFFFFF; --line:#EAEAE5; --line-2:#DADAD3; --fg:#17171E; --muted:#6D6F78;
         --faint:#A3A5AC; --accent:#5546C9; --live:#D63543; --ok:#23845A; --warn:#B07A22; --ts:#A3A5AC; }
  *{ box-sizing:border-box; }
  body{ margin:0; background:var(--bg); color:var(--fg); height:100vh; display:flex; flex-direction:column;
        font-family:"Heebo","Segoe UI Variable Text","Segoe UI",system-ui,Arial,sans-serif; font-size:15px;
        -webkit-font-smoothing:antialiased; }
  header{ padding:12px 18px; display:flex; align-items:center; gap:12px; flex:none;
          background:rgba(255,255,255,.72); backdrop-filter:saturate(160%) blur(14px);
          border-bottom:1px solid rgba(23,23,40,.06); }
  header .orb{ width:28px; height:28px; }
  header .ttl{ display:flex; flex-direction:column; min-width:0; }
  header .ttl-r{ display:flex; align-items:center; gap:8px; }
  header .dot{ width:8px; height:8px; border-radius:50%; background:var(--live); flex:none;
               animation:recPing 1.6667s steps(16) infinite; }
  header .dot.idle{ animation:none; background:var(--faint); box-shadow:none; }
  header .dot.ok{ animation:none; background:var(--ok); box-shadow:none; }
  header h1{ font-size:15px; margin:0; font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  header .sub{ color:var(--faint); font-size:12px; font-variant-numeric:tabular-nums; }
  /* the stage clock: thin, large, digits that never jump */
  header .clock{ margin-inline-start:auto; font-size:30px; font-weight:250; letter-spacing:-.5px; line-height:1;
                 font-variant-numeric:tabular-nums; color:var(--fg); }
  header .clock .c{ animation:blink 2s steps(1) infinite; }
  header .clock[hidden]{ display:none; }
  @keyframes blink{ 50%{ opacity:.25; } }
  .wrap{ flex:1; overflow:auto; padding:18px 22px 26px; scroll-behavior:smooth;
         user-select:text; -webkit-user-select:text; cursor:text; }
  .wrap *{ user-select:text; -webkit-user-select:text; }
  .ln{ line-height:1.7; margin:0 auto 10px; word-break:break-word; white-space:pre-wrap; max-width:820px; }
  /* one centred reading column: English starts and Hebrew ends on its edges
     (a left-anchored column left right-aligned Hebrew floating mid-window) */
  .ln.rise{ animation:rise .5s var(--ease) both; }
  /* the live draft: the last seconds, before the accurate text (never copied or saved) */
  .draft{ line-height:1.7; margin:0 auto 10px; max-width:820px; word-break:break-word;
          color:var(--muted); font-style:italic; user-select:none; -webkit-user-select:none; }
  .ln .ts{ color:var(--accent); font-variant-numeric:tabular-nums; font-size:11.5px; font-weight:600;
           opacity:.75; font-family:"Cascadia Code","Cascadia Mono",Consolas,monospace; }
  .ln .tsg{ display:inline-block; width:10px; }
  /* a speaker's tag, as in Lia Lite (2026-11-16): a soft pill in the speaker's own colour, the user's in
     Lia's colour; every named paragraph has one, a voice's tag is clicked to name it */
  .ln .v{ display:inline-block; font-size:.78em; font-weight:700; line-height:1.55; padding:1px 10px; margin-inline-end:8px;
          border-radius:999px; vertical-align:.08em; max-width:15em; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
          background:var(--cb, #F1ECE4); color:var(--cf, #6B5537); }
  .ln .v.me{ background:#ECEAFB; color:#3B2FA8; }
  .ln .v.edit{ cursor:pointer; }
  .ln .v.edit:hover{ filter:brightness(.95); }
  /* a voice nobody named yet: an outline that asks for a name */
  .ln .v.unnamed{ background:transparent; border:1px dashed var(--cf, #6B5537); padding:0 9px; }
  .ln .v.unnamed.edit::after{ content:"+"; margin-inline-start:5px; font-weight:700; opacity:.7; }
  .ln .v .fd{ display:inline-block; width:5px; height:5px; border-radius:50%; background:var(--accent); margin-inline-start:5px; vertical-align:2px; }
  .ln .vs{ font-size:0; }
  header .incall{ color:var(--muted); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  header .incall[hidden]{ display:none; }
  .pop{ position:fixed; z-index:10; background:var(--panel); border:1px solid var(--line-2); border-radius:12px;
        box-shadow:0 8px 28px rgba(23,23,40,.14); padding:10px; width:260px; font-size:13px; }
  .pop .row{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }
  .pop .h{ color:var(--muted); font-size:11.5px; margin:2px 0 6px; }
  .pop button{ appearance:none; border:1px solid var(--line-2); background:var(--panel); color:var(--fg); cursor:pointer;
               font:inherit; font-size:12.5px; padding:3px 10px; border-radius:999px; }
  .pop button.hint{ border-color:var(--accent); color:var(--accent); }
  .pop button.me{ font-weight:700; }
  .pop button.pri{ background:var(--accent); border-color:var(--accent); color:#fff; }
  .pop input{ width:100%; font:inherit; font-size:13px; padding:6px 9px; border:1px solid var(--line-2); border-radius:8px; margin-bottom:8px; }
  .pop .acts{ display:flex; gap:6px; justify-content:flex-end; }
  .banner{ color:var(--muted); text-align:center; margin-top:12vh; font-size:14px; line-height:1.7; padding:0 20px; }
  .banner .orb{ width:56px; height:56px; display:block; margin:0 auto 14px; }
  .banner.ended{ color:var(--fg); }
  .foot{ flex:none; background:var(--panel); color:var(--muted); border-top:1px solid var(--line);
         font-size:12.5px; padding:9px 16px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .foot .phase{ display:inline-flex; align-items:center; gap:8px; }
  .foot .phase .orb{ width:18px; height:18px; }
  .foot .spin{ display:none; }
  .foot .grow{ flex:1; }
  .foot button{ appearance:none; border:1px solid var(--line-2); background:var(--panel); color:var(--fg);
                cursor:pointer; font-size:12.5px; font-weight:600; padding:5px 12px; border-radius:999px; font-family:inherit;
                transition:background .16s; }
  .foot button:hover{ background:#FAFAF8; }
  .foot button.follow{ margin-inline-start:auto; border-color:transparent; background:transparent; color:var(--accent); }
  .foot button.follow.off{ color:var(--muted); }
  .foot .err{ color:var(--warn); }
  ::-webkit-scrollbar{ width:12px; } ::-webkit-scrollbar-thumb{ background:#D0D0C9; border-radius:8px; border:4px solid var(--bg); }
</style></head>
<body>
  <header>
    <span class="orb" id="orb">__LIA_ORB__</span>
    <div class="ttl">
      <div class="ttl-r"><span class="dot" id="dot"></span><h1 id="title" dir="auto">Live transcript</h1></div>
      <span class="sub" id="sub"></span>
      <span class="incall" id="incall" hidden></span>
    </div>
    <span class="clock" id="clock" hidden></span>
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
var ORB = '__LIA_ORB__';
var SHOWN = -1;           // lines already on screen: newer ones rise in once
var CLOCK = null;         // the stage clock's timer while recording
var VOICES = null;        // <live>.voices.json: paras [{ts,v,name}], voices, people
var PHASE = '';
var POP = null;           // the open naming popup
// the speakers' colours, the same eight as Lia Lite's transcript: background + text
var CB = ['#FDECE7','#E3F2F1','#EEF0FB','#FFF4DE','#F6E8F4','#E8F2E3','#E7EEF7','#F1ECE4'];
var CF = ['#9A4A2E','#2F6F6A','#4A4FA0','#8A5A12','#8B3A7E','#3F6B2E','#355C86','#6B5537'];

function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

function stripHead(text){
  var i = text.indexOf('\n\n');
  return i >= 0 ? text.slice(i + 2) : text;
}
function voiceOf(v){
  var vs = (VOICES && VOICES.voices) || [];
  for(var i = 0; i < vs.length; i++) if(vs[i].v === v) return vs[i];
  return null;
}
function editable(){ return PHASE === 'recording' && VOICES && VOICES.editable !== false; }
function hue(s){ var h = 0; s = String(s); for(var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h) % CB.length; }
// with no voices file (speaker detection is not running) the name is read from
// the paragraph itself: a short "<name>: " at its start, as the meeting writes it
var NAME_RX = /^([^\s:.,!?\d][^:.,!?\n]{0,38}): /;
function tagHtml(para, text){
  // the paragraph starts with "<name>: " (written by the meeting): show the
  // name as a tag; the ": " stays in the text (copy / save keep it). A
  // paragraph of a VOICE has a tag that is clicked to name it; a name that
  // came with no voice (Teams captions before the voice was heard) has a
  // tag too, in the colour of the name.
  para = para || {};
  var vo = para.v ? (voiceOf(para.v) || {v:para.v, n:0, name:para.name, src:''}) : null;
  // a Hebrew paragraph starts with a direction mark (RLM) before the name:
  // keep it in front (it sets the line's direction), match after it
  var lead = (text.match(/^[\u200e\u200f\u202a-\u202e\u2066-\u2069]*/) || [''])[0];
  text = text.slice(lead.length);
  var pre = para.name || (vo && vo.name) || '';
  if(!pre && !VOICES){
    var m = text.match(NAME_RX);
    if(m && m[1].trim().split(/\s+/).length <= 4) pre = m[1];
  }
  var shown = para.shown || pre;
  if(pre && text.indexOf(pre + ': ') === 0) text = text.slice(pre.length + 2);
  else return null;
  var me = (VOICES && VOICES.me) || '';
  var mine = (vo && vo.self) || (me && shown === me);
  // a voice of a room ("<the room>, Speaker 2") asks for a name too
  var unnamed = !!vo && ((!vo.src && /^Speaker \d+$/.test(shown)) || vo.src === 'room');
  var i = vo ? ((vo.n || 1) - 1) % CB.length : hue(shown);
  var edit = !!vo && editable();
  var title = vo && vo.src==='found' ? 'Lia found this name' :
              vo && vo.src==='bank' ? 'Lia knows this voice from an earlier meeting' :
              (edit && unnamed) ? 'Click to name this speaker' : '';
  return lead + '<span class="v'+(mine?' me':'')+(unnamed?' unnamed':'')+(edit?' edit':'')+'"'+
         (vo ? ' data-v="'+esc(para.v)+'"' : '')+' style="--cb:'+CB[i]+';--cf:'+CF[i]+'"'+
         (title ? ' title="'+title+'"' : '')+'>'+esc(shown)+
         ((vo && (vo.src==='found' || vo.src==='bank')) ? '<span class="fd"></span>' : '')+'</span><span class="vs">: </span>'+esc(text);
}
// the side a paragraph reads from: its direction mark, else its first letter
function rtlText(s){
  s = String(s || '');
  if(/^[\u200f\u202b\u2067]/.test(s)) return true;
  if(/^[\u200e\u202a\u2066]/.test(s)) return false;
  var c = s.match(/[A-Za-z\u0590-\u08ff]/);
  return !!c && c[0] >= '\u0590';
}
function lineHtml(raw, para, next){
  var m = raw.match(/^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*([\s\S]*)$/);
  // a time stamp alone on its line stands on the side its paragraph starts from (as in Lia Lite)
  if(m){ return '<div class="ln"'+(m[2] ? ' dir="auto"' : (next && rtlText(next) ? ' dir="rtl"' : ' dir="ltr"'))+
                '><span class="ts" dir="ltr">'+esc(m[1])+'</span><span class="tsg"></span>'+esc(m[2])+'</div>'; }
  if(para || !VOICES){ var h = tagHtml(para, raw); if(h) return '<div class="ln" dir="auto">'+h+'</div>'; }
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
var DRAFT = '';           // the live draft text (grey, after the last line)
function renderDraft(text){
  if(text !== undefined) DRAFT = text || '';
  var wrap = document.getElementById('wrap');
  if(selectingInWrap() || POP) return;
  var el = wrap.querySelector('.draft');
  var show = DRAFT && PHASE === 'recording';
  if(!show){ if(el) el.remove(); return; }
  if(!el){
    el = document.createElement('div'); el.className = 'draft'; el.setAttribute('dir', 'auto');
    wrap.appendChild(el);
  }
  if(el.textContent !== DRAFT){
    el.textContent = DRAFT;
    if(follow) wrap.scrollTop = wrap.scrollHeight;
  }
}
function renderTranscript(body){
  var wrap = document.getElementById('wrap');
  if(selectingInWrap() || POP){ pendingBody = body; return; }
  pendingBody = null;
  body = (body||'').replace(/\s+$/,'');
  if(!body){
    wrap.innerHTML = '<div class="banner"><span class="orb">'+ORB+'</span>Waiting for the first chunk (~'+META.chunk_seconds+'s)&hellip;</div>';
    return;
  }
  var lines = body.split('\n').filter(function(l){ return l.trim().length; });
  var first = SHOWN < 0;
  // pair each paragraph with its voice: the k-th "[m:ss]" line <-> paras[k]
  var paras = (VOICES && VOICES.paras) || [], k = -1, para = null;
  wrap.innerHTML = lines.map(function(l, i){
    var tm = l.match(/^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*$/);
    if(tm){ k++; para = (paras[k] && paras[k].ts === tm[1]) ? paras[k] : null; }
    var h = lineHtml(l, tm ? null : para, tm ? lines[i + 1] : null);
    return (!first && i >= SHOWN) ? h.replace('<div class="ln"', '<div class="ln rise"') : h;
  }).join('');
  SHOWN = lines.length;
  if(follow) wrap.scrollTop = wrap.scrollHeight;
  // A "rise" line starts at opacity 0. While Windows reports the window as
  // hidden (behind another window, minimised) animations pause, and a line
  // could stay invisible: drop the class once the animation had its time.
  clearTimeout(window._riseT);
  window._riseT = setTimeout(function(){
    wrap.querySelectorAll('.ln.rise').forEach(function(e){ e.classList.remove('rise'); });
    checkVisible(lines.length);
  }, 900);
}
var DIAGGED = {};
function diag(key, msg){
  // once per key per window - a small log, never the transcript text
  if(DIAGGED[key] || !API || !API.diag) return;
  DIAGGED[key] = 1;
  try{ API.diag(msg); }catch(e){}
}
function checkVisible(n){
  var w = document.getElementById('wrap');
  var lns = w.querySelectorAll('.ln'), vis = 0;
  lns.forEach(function(e){
    var cs = getComputedStyle(e);
    if(parseFloat(cs.opacity) > 0.5 && cs.visibility !== 'hidden' && e.offsetHeight > 0) vis++;
  });
  if(n > 0 && vis === 0){
    diag('invisible', 'lines not visible: lines=' + n + ' ln=' + lns.length + ' hidden=' + document.hidden +
         ' wrapH=' + w.clientHeight + ' scroll=' + w.scrollTop + '/' + w.scrollHeight);
    lns.forEach(function(e){ e.classList.remove('rise'); e.style.opacity = '1'; });
  }
}
window.addEventListener('error', function(e){
  diag('err:' + (e && e.message), 'js error: ' + (e && e.message) + ' @' + (e && e.lineno));
});
window.addEventListener('unhandledrejection', function(e){
  var m = e && e.reason ? String(e.reason.message || e.reason) : '?';
  diag('rej:' + m, 'js rejection: ' + m);
});

function setDot(kind){ // 'live' | 'idle' | 'ok'
  var d = document.getElementById('dot');
  d.className = 'dot' + (kind==='idle'?' idle':(kind==='ok'?' ok':''));
  d.style.display = '';
  if(kind==='live') startClock(); else stopClock();
}
function startClock(){
  var el = document.getElementById('clock');
  if(!el || CLOCK || !META.started_at) return;
  var t0 = new Date(META.started_at).getTime();
  if(isNaN(t0)) return;
  function tick(){
    var s = Math.max(0, Math.floor((Date.now() - t0) / 1000));
    var h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, ss = s % 60;
    el.innerHTML = (h ? h + '<span class="c">:</span>' + ('0'+m).slice(-2) : m) +
                   '<span class="c">:</span>' + ('0'+ss).slice(-2);
  }
  tick(); el.hidden = false; CLOCK = setInterval(tick, 1000);
}
function stopClock(){ if(CLOCK){ clearInterval(CLOCK); CLOCK = null; } }

function openBtn(which, label){
  return '<button data-open="'+which+'">'+esc(label)+'</button>';
}
function followBtn(){
  return '<button class="follow'+(follow?'':' off')+'" id="followBtn">'+(follow?'Following':'Paused')+'</button>';
}

function renderFoot(r){
  var foot = document.getElementById('foot');
  var phase = r.phase;
  // Lia turns while the meeting is processed, rests otherwise
  var o = document.getElementById('orb');
  if(o) o.className = 'orb' + (phase==='processing' ? ' busy' : '');
  var html = '';
  if(phase==='recording' || phase==='waiting'){
    setDot('live');
    document.getElementById('title').textContent = META.title || T('Live transcript');
    html = '<span class="phase">Updating every ~'+META.chunk_seconds+'s</span><span class="grow"></span>'+followBtn();
  } else if(phase==='processing'){
    setDot('idle');
    html = '<span class="phase"><span class="orb busy">'+ORB+'</span>Processing'+(r.summarize?' (transcribing / summarizing)':'')+'&hellip;</span>'+
           '<span class="grow"></span>'+followBtn();
  } else if(phase==='done'){
    setDot('ok');
    document.getElementById('title').textContent = T('Meeting saved');
    html = '<span class="phase">✓ Saved</span><span class="grow"></span>'+
           (r.transcript_ready?openBtn('transcript','Open transcript'):'')+
           (r.summary_ready?openBtn('summary','Open summary'):'');
  } else if(phase==='ended_no_summary'){
    setDot('ok');
    document.getElementById('title').textContent = T('Transcript saved');
    html = '<span class="phase">✓ Transcript saved (no summary requested)</span><span class="grow"></span>'+
           (r.transcript_ready?openBtn('transcript','Open transcript'):'');
  } else if(phase==='failed'){
    setDot('idle');
    document.getElementById('title').textContent = T('Meeting');
    html = '<span class="phase err">'+esc(r.error||'Something went wrong')+'</span><span class="grow"></span>'+
           (r.transcript_ready?openBtn('transcript','Open transcript'):'');
  } else if(phase==='cancelled'){
    setDot('idle');
    document.getElementById('title').textContent = T('Meeting discarded');
    html = '<span class="phase">Discarded - no transcript saved</span>';
  } else { // legacy 'ended'
    setDot('ok');
    document.getElementById('title').textContent = T('Meeting ended');
    html = '<span class="phase">✓ Saved</span>';
  }
  if(phase!=='cancelled' && lastText)
    html = '<button id="copyAllBtn">Copy all</button><button id="saveTxtBtn">Save .txt</button>' + html;
  foot.innerHTML = html;
}

function renderInCall(){
  var el = document.getElementById('incall');
  var vs = ((VOICES && VOICES.voices) || []).filter(function(x){ return !x.self && x.s >= 10; });
  if(!vs.length){ el.hidden = true; return; }
  el.innerHTML = '<span>In the call:</span> <span dir="auto">' +
    vs.map(function(x){ return esc(x.name); }).join(' &middot; ') + '</span>';
  el.hidden = false;
}
function closePop(){
  if(POP){ POP.remove(); POP = null; }
  if(pendingBody !== null && !selectingInWrap()){ var b = pendingBody; renderTranscript(b); }
}
function openPop(tag){
  closePop();
  var v = tag.getAttribute('data-v'), vo = voiceOf(v) || {v:v, name:tag.textContent, src:''};
  var cur = (vo.src === 'user' || vo.src === 'captions' || vo.src === 'found' || vo.src === 'self' || vo.src === 'bank') ? vo.name : '';
  var sug = [], seen = {};
  function add(n, cls){ if(n && !seen[n.toLowerCase()]){ seen[n.toLowerCase()] = 1; sug.push([n, cls]); } }
  var me = (VOICES && VOICES.me) || '';
  if(me) seen[me.toLowerCase()] = 1;      // offered as the "Me" button
  add(vo.hint, 'hint');
  ((VOICES && VOICES.people) || []).forEach(function(n){ add(n, ''); });
  var others = ((VOICES && VOICES.voices) || []).filter(function(x){ return x.v !== v; });
  var p = document.createElement('div');
  p.className = 'pop';
  p.innerHTML =
    ((sug.length || me) ? '<div class="row">' +
        (me ? '<button data-name="'+esc(me)+'" class="me" title="'+esc(me)+'">Me</button>' : '') +
        sug.slice(0, 8).map(function(s){
        return '<button data-name="'+esc(s[0])+'" class="'+s[1]+'" dir="auto">'+esc(s[0])+'</button>'; }).join('') + '</div>' : '') +
    '<input id="vname" dir="auto" maxlength="40" placeholder="Name this voice" value="'+esc(cur)+'">' +
    (others.length ? '<div class="h">Same person as</div><div class="row">' + others.map(function(x){
        return '<button data-into="'+esc(x.v)+'" dir="auto">'+esc(x.name)+'</button>'; }).join('') + '</div>' : '') +
    '<div class="acts"><button data-act="clear">Clear</button><button data-act="save" class="pri">Save</button></div>';
  document.body.appendChild(p);
  var r = tag.getBoundingClientRect(), w = 260, h = p.offsetHeight;
  var x = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
  var y = r.bottom + 6; if(y + h > window.innerHeight - 8) y = Math.max(8, r.top - h - 6);
  p.style.left = x + 'px'; p.style.top = y + 'px';
  POP = p;
  var inp = p.querySelector('#vname');
  inp.focus(); inp.select();
  async function save(name){
    if(!API) return;
    try{ await API.set_name(v, name); }catch(e){}
    if(VOICES){
      var old = vo.name, now = name || ('Speaker ' + (vo.n || 0));
      (VOICES.voices || []).forEach(function(x){
        if(x.v === v){ x.name = now; x.src = name ? 'user' : ''; } });
      // until the meeting rewrites the transcript (within a second)
      (VOICES.paras || []).forEach(function(q){
        if(q.v === v && (q.name === old || q.shown)) q.shown = now; });
    }
    closePop(); renderInCall(); renderTranscript(lastText);
  }
  inp.addEventListener('keydown', function(ev){
    if(ev.key === 'Enter'){ ev.preventDefault(); save(inp.value.trim()); }
    else if(ev.key === 'Escape'){ ev.preventDefault(); closePop(); }
  });
  p.addEventListener('click', async function(ev){
    var b = ev.target.closest('button'); if(!b) return;
    if(b.hasAttribute('data-name')){ save(b.getAttribute('data-name')); return; }
    if(b.hasAttribute('data-into')){
      try{ if(API) await API.merge(v, b.getAttribute('data-into')); }catch(e){}
      closePop(); return;
    }
    var a = b.getAttribute('data-act');
    if(a === 'save') save(inp.value.trim());
    else if(a === 'clear') save('');
  });
}
document.addEventListener('mousedown', function(ev){
  if(POP && !POP.contains(ev.target) && !ev.target.closest('.v.edit')) closePop();
});
document.addEventListener('keydown', function(ev){ if(ev.key === 'Escape' && POP) closePop(); });
document.getElementById('wrap').addEventListener('click', function(ev){
  var t = ev.target.closest('.v.edit');
  if(t && editable()){ ev.preventDefault(); openPop(t); }
});

function render(r){
  PHASE = r.phase || '';
  diag('first', 'first render: phase=' + PHASE + ' text=' + ((r.text || '').length) +
       ' voices=' + (r.voices ? 'yes' : 'no') + ' hidden=' + document.hidden);
  if(r.voices !== undefined) VOICES = r.voices;
  if(PHASE !== 'recording' && POP) closePop();
  renderInCall();
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
  renderDraft();
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
    if(PHASE === 'recording' && API.draft){ var d = await API.draft(); renderDraft((d && d.text) || ''); }
    else if(DRAFT) renderDraft('');
  }catch(e){ lastVer = ''; diag('poll', 'refresh failed: ' + (e && e.message ? e.message : e)); }
}

function setFollow(on){
  follow = on;
  var el = document.getElementById('followBtn');
  if(el){ el.classList.toggle('off', !on); el.innerHTML = on ? 'Following' : 'Paused'; }
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
async function saveTxt(btn){
  if(!API) return;
  var r = {ok:false};
  try{ r = await API.save_txt(transcriptText()); }catch(e){}
  if(!btn || r.cancelled) return;
  btn.textContent = r.ok ? 'Saved ✓' : 'Not saved';
  btn.title = r.ok ? r.path : (r.error || '');
  setTimeout(function(){ btn.textContent = 'Save .txt'; }, 2000);
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
  var sv = ev.target.closest('#saveTxtBtn'); if(sv){ saveTxt(sv); return; }
  var o = ev.target.closest('[data-open]');
  if(o && API){ try{ await API.open_artifact(o.getAttribute('data-open')); }catch(e){} }
});

window.addEventListener('pywebviewready', async function(){
  if(!API) API = window.pywebview.api;
  try{
    META = await API.meta();
    document.getElementById('title').textContent = META.title || T('Live transcript');
    document.getElementById('sub').textContent = META.started ? ('Started ' + META.started) : '';
  }catch(e){}
  // the poll starts whatever happens below: a first read that fails must not
  // leave the window frozen (it re-reads on the next change)
  window._pollTimer = setInterval(poll, 500);     // the draft moves within a second of the speech
  try{
    lastVer = await API.version();
    await refresh();
  }catch(e){
    lastVer = '';
    diag('start', 'first read failed: ' + (e && e.message ? e.message : e));
  }
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
        win = webview.create_window(ui_i18n.t(payload.get("title", "Live transcript - Lia")),
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
