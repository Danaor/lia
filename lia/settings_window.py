"""Lia Settings - a pywebview child window (sidebar + pages) on ui_kit.

Spawned by the running app (LiaApp._open_settings_window) with stdin +
stdout PIPES. It is the single home for everything that used to live in the
deeply-nested tray "Options" submenus.

IPC (see lia.py for the parent half):
  child -> parent (stdout, one JSON line per message, prefixed "@@LIA "):
      {"t":"call","id":N,"method":..,"args":[..]}   - run an allowlisted action
      {"t":"refresh"}                                - please resend full state
      {"t":"closed"}                                 - the window is closing
  parent -> child (stdin, one JSON line each; applied via evaluate_js):
      {"t":"state","state":{..}}   - full state (re-render current page)
      {"t":"result","id":N,"ok":..,"msg":..,"data":..}
      {"t":"tick", ...volatile...} - lightweight status update (no re-render)
      {"t":"toast","level":..,"msg":..}
      {"t":"focus","page":..,"focus":..}
      {"t":"close"}

We must NOT redirect stdout (it is the IPC channel) - only stderr goes to a log.
"""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_kit as uk  # noqa: E402

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Lia")
LOG_PATH = os.path.join(CONFIG_DIR, "settings_window.log")

_WINDOW = None
_READY = False
_OUT_LOCK = threading.Lock()
# The real stdout pipe, captured before anything can reassign it.
_STDOUT = sys.stdout


def _emit(obj):
    """Send one message to the parent on stdout (prefixed, one line, flushed)."""
    try:
        line = "@@LIA " + json.dumps(obj, ensure_ascii=True) + "\n"
    except Exception:
        return
    with _OUT_LOCK:
        try:
            _STDOUT.write(line)
            _STDOUT.flush()
        except Exception:
            pass


class Bridge:
    """JS-facing API. JS calls pywebview.api.emit({...}) to reach the parent."""

    def emit(self, msg):
        try:
            _emit(msg)
        except Exception:
            pass
        return True

    def mark_ready(self):
        global _READY
        _READY = True
        return True


def _push_to_js(msg):
    """Parent->child: hand a message to the page (buffered there until ready)."""
    win = _WINDOW
    if win is None:
        return
    try:
        win.evaluate_js("window.__lia_push(%s)" % json.dumps(msg, ensure_ascii=True))
    except Exception:
        pass


def _reveal_window():
    """Bring the window to the front from ANY state: pre-warmed hidden (show),
    minimized to the taskbar (restore un-minimizes it - show() alone does NOT),
    or just behind another window (show -> Activate). restore() is a no-op when
    the window isn't minimized, so calling both is always safe/idempotent."""
    global _READY
    win = _WINDOW
    if win is None:
        return
    try:
        win.restore()   # WindowState -> Normal: un-minimize if minimized
    except Exception:
        pass
    try:
        win.show()      # Show() + Activate(): visible + foreground
    except Exception:
        pass
    _READY = True  # revealed => JS has certainly run; satisfy the ready watchdog.


def _stdin_reader():
    """Read parent messages line-by-line; EOF or {"t":"close"} => close."""
    try:
        for raw in iter(sys.stdin.readline, ""):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            mt = msg.get("t")
            if mt == "close":
                break
            if mt in ("show", "focus"):
                _reveal_window()
            _push_to_js(msg)
    except Exception:
        pass
    # parent gone (EOF) or asked to close -> tear down the window
    try:
        if _WINDOW is not None:
            _WINDOW.destroy()
    except Exception:
        pass


def _on_closing():
    _emit({"t": "closed"})
    return True  # allow the close


def _js(obj):
    """JSON for embedding inside a <script> (guard the </script> sequence)."""
    return json.dumps(obj, ensure_ascii=True).replace("</", "<\\/")


def build_window(webview, payload):
    global _WINDOW
    init_state = payload.get("state", {})
    page = payload.get("page", "general")
    focus = payload.get("focus", "")
    prewarm = bool(payload.get("prewarm"))
    head = ("<script>window.__LIA_INIT__=%s;window.__LIA_PAGE__=%s;"
            "window.__LIA_FOCUS__=%s;</script>" %
            (_js(init_state), _js(page), _js(focus or "")))
    theme = "dark" if (init_state.get("config", {}).get("ui_theme") == "dark") else None
    html = uk.page("Lia Settings", BODY, extra_css=EXTRA_CSS, extra_js=APP_JS,
                   head_extra=head, theme=theme)
    # "settings2" (2026-08-29): a fresh geometry key so every user gets the
    # new larger centered default ONCE (the old "settings" entry may carry a
    # cramped/off-screen geometry); resizes are remembered from then on.
    # clamp_to_workarea keeps the window fully INSIDE the screen-minus-
    # taskbar area - it must never open touching or behind the taskbar.
    geo = uk.window_geometry("settings2", {"width": 1100, "height": 780})
    kw = uk.clamp_to_workarea(geo.get("width", 1100), geo.get("height", 780),
                              geo.get("x"), geo.get("y"))
    kw["min_size"] = (820, 600)
    if prewarm:
        # Pre-warmed: boot the WebView2 engine + render now, but stay hidden
        # until the parent sends a reveal (t:"focus"/"show"). WebView2 still
        # loads the DOM + runs JS while hidden, so the window is ready-and-warm.
        kw["hidden"] = True
    _WINDOW = webview.create_window("Lia Settings", html=html, js_api=Bridge(), **kw)
    try:
        _WINDOW.events.closing += _on_closing
    except Exception:
        pass
    uk.attach_geometry_memory(_WINDOW, "settings2")
    threading.Thread(target=_stdin_reader, daemon=True).start()
    return _WINDOW


def _demo_state():
    """A canned state that exercises every page (for --demo / --html QA)."""
    return {
        "config": {
            "hotkey": "ctrl+space", "recording_mode": "hold",
            "paste_mode": "auto_paste", "clipboard_auto_restore": True,
            "press_enter_after_paste": False, "silent_mode": True,
            "beep_device_index": "off", "recording_source": "both",
            "input_device_index": 1, "loopback_device_index": None,
            "cleanup_style": "spoken", "auto_detect_meetings": True,
            "custom_vocabulary": "git, push, React, GuardDuty, Landing Zone",
            "vocab_autolearn": True, "meeting_model": "local_hebrew_turbo",
            "file_transcribe_model": "", "summary_model": "gpt-5.6-sol",
            "whisper_device": "auto",
        },
        "secrets": {"openai_api_key": "sk-abc…7xQ", "groq_api_key": "",
                    "gemini_api_key": "AQ.xyz…9kk", "assemblyai_api_key": "",
                    "hf_token": "", "remote_server_token": ""},
        "has": {"openai_api_key": True, "groq_api_key": False,
                "gemini_api_key": True, "assemblyai_api_key": False,
                "hf_token": False, "remote_server_token": False},
        "status_line": "Ready", "model_loaded": True, "recording": False,
        "meeting_active": False, "live_transcript_available": False,
        "loopback_available": True, "whisper_device_label": "Auto",
        "cleanup_model_label": "gpt-5.6-luna", "cleanup_provider": "openai",
        "vocab_pending": 4, "auto_start": True,
        "serve": {"enabled": True, "autostart": False, "running": True,
                  "port": 9090, "has_token": False,
                  "tailscale_ip": "100.70.229.87",
                  "ws_url": "ws://100.70.229.87:9090",
                  "role": "server",
                  "gpu": {"has_cuda": True, "name": "NVIDIA GeForce RTX 3090",
                          "vram_gb": 24.0, "verdict": "good",
                          "note": "NVIDIA GeForce RTX 3090 · 24 GB VRAM - good "
                                  "for hosting a transcription server."}},
        "hotkeys": {"main": "ctrl+space", "undo": "ctrl+alt+z", "cancel": "esc",
                    "ask": "ctrl+alt+m", "actions": "ctrl+alt+t",
                    "email": "ctrl+alt+f", "chat": "ctrl+alt+c"},
        "paths": {"config": r"C:\Users\you\AppData\Roaming\Lia",
                  "log": r"C:\Users\you\AppData\Roaming\Lia\lia.log",
                  "meetings": r"C:\Users\you\AppData\Roaming\Lia\meetings"},
        "mics": [{"idx": 1, "name": "USB Microphone"},
                 {"idx": 2, "name": "Logitech C920"}],
        "loopbacks": [{"idx": 5, "name": "Speakers (Realtek)"}],
        "outputs": [{"idx": 3, "name": "Speakers (Realtek)"},
                    {"idx": 4, "name": "Focusrite USB"}],
        "tables": {
            "dictation": [{"idx": 0, "label": "Hebrew Turbo Local ⭐ (best local Hebrew)", "checked": True, "where": "local", "wnote": "GPU (4 GB+) recommended · slower on CPU"},
                          {"idx": 1, "label": "English Parakeet Local ⭐ (best English)", "checked": False, "where": "local", "wnote": "fast on a plain CPU · no GPU needed"},
                          {"idx": 4, "label": "OpenAI GPT transcribe", "checked": False, "where": "cloud", "wnote": "API key set · ~$0.4 per audio hour"},
                          {"idx": 6, "label": "Groq Whisper Large v3 Turbo", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · free tier available"},
                          {"idx": 7, "label": "Gemini 3.5 transcribe", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · free tier available · ~3.6s, slower than Groq · trains on your audio"},
                          {"idx": 8, "label": "Hebrew Turbo Remote", "checked": False, "where": "remote"}],
            "meeting": [{"key": "local_hebrew_turbo", "label": "Hebrew Turbo Local only", "checked": True, "enabled": True, "note": ""},
                        {"key": "gemini_transcribe", "label": "Gemini 3.5 transcribe", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · free tier available · trains on your audio", "note": "needs gemini_api_key"},
                        {"key": "openai_gpt_transcribe", "label": "OpenAI GPT transcribe", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · ~$0.4 per meeting hour", "note": "needs openai_api_key"}],
            "file": [{"key": "", "label": "Same as meeting model", "checked": True, "enabled": True, "note": ""}],
            "summary": [{"model": "off", "label": "Off - transcript only (no AI summary)", "checked": True, "enabled": True, "note": ""},
                        {"model": "gpt-5.6-sol", "label": "OpenAI ChatGPT 5.6 Sol", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · ~$0.10 per meeting summary", "note": ""},
                        {"model": "gemini-3.7-flash", "label": "Gemini 3.7 Flash", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · free tier available", "note": "set Gemini key"},
                        {"model": "gemma4:31b-it-qat", "label": "Gemma 4 31B QAT (local · best quality · needs a 24 GB GPU)", "checked": False, "enabled": False, "where": "local", "note": "start Ollama"}],
            "cleanup_styles": [{"style": "off", "label": "Off - raw transcription", "checked": False},
                               {"style": "spoken", "label": "Spoken - remove fillers + self-corrections", "checked": True},
                               {"style": "proofread", "label": "Proofread - full polish", "checked": False}],
            "cleanup_models": [{"provider": "gemini", "model": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite (free, fastest)", "checked": False},
                               {"provider": "openai", "model": "gpt-5.6-luna", "label": "OpenAI ChatGPT 5.6 Luna - light, fast", "checked": True}],
            "device": [{"key": "auto", "label": "Auto (GPU if available)", "checked": True},
                       {"key": "cuda", "label": "GPU (CUDA)", "checked": False},
                       {"key": "cpu", "label": "CPU (no GPU needed)", "checked": False}],
            "cleanup_any_key": True,
        },
    }


def _demo_html(page="general"):
    head = ("<script>window.__LIA_INIT__=%s;window.__LIA_PAGE__=%s;"
            "window.__LIA_FOCUS__='';window.__LIA_DEMO__=true;</script>" %
            (_js(_demo_state()), _js(page)))
    return uk.page("Lia Settings", BODY, extra_css=EXTRA_CSS, extra_js=APP_JS,
                   head_extra=head)


def main():
    if "--html" in sys.argv:
        out = sys.argv[sys.argv.index("--html") + 1]
        with open(out, "w", encoding="utf-8") as f:
            f.write(_demo_html())
        sys.stdout.write("wrote %s\n" % out)
        return
    if "--demo" in sys.argv:
        try:
            import webview
        except Exception as e:
            sys.stderr.write("pywebview unavailable: %r\n" % (e,))
            sys.exit(2)
        webview.create_window("Lia Settings (demo)", html=_demo_html(),
                              width=960, height=680, min_size=(760, 540))
        uk.webview_start(webview)
        return
    # stderr -> log; stdout stays the IPC channel (never redirect it).
    uk.install_stderr_sink(LOG_PATH)
    # A pre-warmed window stays hidden until the user opens it, so it may not be
    # "ready" for a while (it flips ready on reveal). Give its watchdog lots of
    # slack; a normal visible open keeps the tight 30s hang-guard.
    prewarm = False
    try:
        prewarm = bool(uk.read_payload().get("prewarm"))
    except Exception:
        prewarm = False
    uk.child_main(build_window, ready_check=lambda: _READY,
                  watchdog_s=(3600 if prewarm else 30))


# ============================================================
# Front-end
# ============================================================
BODY = """
<div class="shell">
  <nav class="sidebar">
    <div class="brand"><img class="logo" src="__LOGO_SRC__" alt=""><span class="brand-txt"><span class="brand-name">Lia</span><span class="brand-sub">Local Inference Assistant</span></span></div>
    <button class="nav-item" data-page="general"><span class="ico">&#9881;</span>General</button>
    <button class="nav-item" data-page="audio"><span class="ico">&#127911;</span>Audio</button>
    <button class="nav-item" data-page="models"><span class="ico">&#129504;</span>Models</button>
    <button class="nav-item" data-page="cleanup"><span class="ico">&#10024;</span>AI Cleanup</button>
    <button class="nav-item" data-page="keys"><span class="ico">&#128273;</span>API Keys</button>
    <button class="nav-item" data-page="meetings"><span class="ico">&#128220;</span>Meetings</button>
    <button class="nav-item" data-page="vocab"><span class="ico">&#128218;</span>Vocabulary</button>
    <button class="nav-item" data-page="snippets"><span class="ico">&#9998;</span>Snippets</button>
    <button class="nav-item" data-page="server"><span class="ico">&#128225;</span>Transcription server</button>
    <div class="spacer"></div>
    <button class="nav-item" data-page="advanced"><span class="ico">&#8505;</span>Advanced</button>
  </nav>
  <main class="content" id="content"></main>
</div>
"""
# Embed the brand orb inline (CSP-safe data: URI; "" if the asset is missing).
BODY = BODY.replace("__LOGO_SRC__", uk.logo_data_uri())

EXTRA_CSS = """
.content{max-width:900px;}
.kv{display:flex; gap:8px; flex-wrap:wrap; font-size:var(--fs-hint); color:var(--muted);}
.kv .k{color:var(--ink-2); font-weight:600;}
/* Keys & Server page: each service is a standalone card on the page ground
   (the wrapping .page frame is dropped via .keys-page) with subtle depth. */
.page.keys-page{background:transparent; border:none; box-shadow:none; padding:0;}
.credcard{background:var(--card); border:1px solid var(--line); border-radius:var(--r-l);
  padding:18px 20px; margin-bottom:16px; box-shadow:0 1px 2px rgba(16,24,40,.05);
  transition:box-shadow .18s ease, border-color .18s ease;}
.credcard:hover{border-color:var(--line-2);
  box-shadow:0 1px 2px rgba(16,24,40,.06), 0 10px 24px rgba(16,24,40,.07);}
.credcard .head{display:flex; align-items:center; gap:12px; margin-bottom:14px;}
.credcard .bdg{width:38px;height:38px;border-radius:11px;display:flex;align-items:center;
  justify-content:center;font-weight:800;font-size:16px;color:var(--on-accent);flex:0 0 38px;
  box-shadow:0 2px 6px rgba(16,24,40,.16), inset 0 1px 0 rgba(255,255,255,.28);}
.credcard h3{margin:0;font-size:15.5px;font-weight:700;letter-spacing:-.01em;}
.credcard .sub{color:var(--muted);font-size:var(--fs-hint);margin-top:2px;line-height:1.45;}
/* current-key value shown as a soft pill (dashed + faint when unset) */
.credcard .keyline{display:flex; align-items:center; gap:8px; margin:0 0 12px;
  font-size:var(--fs-hint); color:var(--muted);}
.credcard .keyline .masked{font-size:12px; background:var(--accent-soft);
  border:1px solid var(--line); padding:3px 10px; border-radius:var(--r-pill); letter-spacing:.02em;}
.credcard .keyline .masked.empty{background:transparent; border-style:dashed;}
.credcard .row-inline{margin-top:2px;}
.btnrow{display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:12px;}
.btnrow .grow{flex:1 1 auto;}
.tbl{width:100%; border-collapse:collapse;}
.tbl td{padding:7px 6px; border-bottom:1px solid var(--line); vertical-align:middle;}
.tbl td.act{width:36px; text-align:end;}
.muted{color:var(--muted);}
.tbl tr.corr-unused td{opacity:.5;}
.tbl tr.corr-unused .muted{color:var(--warn);}
.note{color:var(--warn); font-size:var(--fs-hint); margin-inline-start:8px;}
.rowbtn{display:flex; align-items:center; gap:10px; padding:9px 12px; border:1px solid var(--line);
  border-radius:var(--r-s); margin-bottom:8px; cursor:pointer; transition:background var(--tr),border-color var(--tr);}
.rowbtn:hover{background:var(--accent-soft); border-color:var(--accent);}
.rowbtn .ic{font-size:16px;}
.rowbtn .hk{margin-inline-start:auto; color:var(--muted); font-size:var(--fs-hint);}
/* A badge (e.g. BETA) takes over the right-push; the hotkey trails it. */
.rowbtn .wb{margin-inline-start:auto;}
.rowbtn .wb ~ .hk{margin-inline-start:0;}
.disabledrow{opacity:.5;}
"""

APP_JS = r"""
(function(){
  var S = window.__LIA_INIT__ || {};
  var PAGE = window.__LIA_PAGE__ || "models";
  var FOCUS = window.__LIA_FOCUS__ || "";
  var DRAFT = {};            // text fields that must survive re-render, by id
  var pending = {};          // call id -> {resolve}
  var nextId = 1;
  var $ = RK.$;

  function esc(s){ return RK.esc(s); }
  function cfg(k, d){ var c = S.config || {}; return (k in c) ? c[k] : d; }
  function draftOr(id, val){ return (id in DRAFT) ? DRAFT[id] : val; }

  function call(method, args, slow){
    var id = nextId++;
    RK.api && RK.api.emit({t:"call", id:id, method:method, args:args||[]});
    return new Promise(function(res){ pending[id] = {res:res, slow:!!slow}; });
  }

  // ---------- generic control builders ----------
  function field(label, control, hint){
    return '<div class="field"><label>'+esc(label)+'</label>'+control+
      (hint?'<div class="hint">'+esc(hint)+'</div>':'')+'</div>';
  }
  function sw(label, checked, method, disabled){
    return '<label class="switch'+(disabled?' disabled':'')+'">'+
      '<input type="checkbox" data-toggle="'+esc(method)+'"'+(checked?' checked':'')+
      (disabled?' disabled':'')+'><span class="track"></span><span>'+esc(label)+'</span></label>';
  }
  var WB_TEXT = {local:'🖥️ LOCAL', cloud:'CLOUD',
                 remote:'REMOTE LOCAL GPU'};
  var WB_SUB = {remote:'Use a GPU on another PC in your local network'};
  function radio(name, method, arg, argtype, label, checked, enabled, note, where, wnote){
    var dis = enabled===false;
    var wsub = wnote || (where ? (WB_SUB[where]||'') : '');
    var badge = '';
    if(where){
      var pill = '<span class="wb '+esc(where)+'">'+(WB_TEXT[where]||esc(where))+'</span>';
      if(wsub){
        badge = '<span class="wb-group">'+pill+'<span class="wb-sub">'+esc(wsub)+'</span></span>';
      } else {
        badge = pill;
      }
    }
    return '<label class="radio'+(checked?' on':'')+(dis?' disabled':'')+'">'+
      '<input type="radio" name="'+esc(name)+'" data-radio="'+esc(method)+'" '+
      'data-arg="'+esc(arg)+'" data-argtype="'+(argtype||'str')+'"'+
      (checked?' checked':'')+(dis?' disabled':'')+'>'+
      '<span class="box"></span><span class="txt">'+esc(label)+
      (note?'<small>'+esc(note)+'</small>':'')+'</span>'+badge+'</label>';
  }
  function btn(label, method, args, kind, slow){
    return '<button class="btn '+(kind||'')+'" data-call="'+esc(method)+'" '+
      'data-args=\''+esc(JSON.stringify(args||[]))+'\''+(slow?' data-slow="1"':'')+
      '>'+esc(label)+'</button>';
  }

  // ---------- page renderers ----------
  var PAGES = {};

  PAGES.general = function(){
    var hk = (S.hotkeys||{});
    var beep = cfg("beep_device_index", "off");
    var beepRows = [
      radio("beep","set_beep_device","off","str","None (no beep)", beep==="off"),
      radio("beep","set_beep_device","__null__","null","System Default", beep===null)
    ];
    (S.outputs||[]).forEach(function(d){
      beepRows.push(radio("beep","set_beep_device",String(d.idx),"int",d.name, beep===d.idx));
    });
    var shortcuts = '<div class="list">'+
      Object.keys(hk).map(function(k){
        return '<div class="row"><span class="grow">'+esc(k)+'</span><span class="kbd">'+esc(hk[k])+'</span></div>';
      }).join('')+'</div>';
    // Each concern gets its OWN card (2026-08-29): one long undivided page
    // made 'Recording mode' / 'Paste' / the toggles read as one blur.
    return '<div class="content-head"><h1>General</h1></div>'+
      '<div class="page"><div class="section-title">Startup</div>'+
        sw("Start Lia automatically when Windows starts", !!S.auto_start, "toggle_auto_start")+
        '<div class="hint">Lia launches to the system tray on every boot, ready to dictate.</div>'+
      '</div>'+
      '<div class="page"><div class="section-title">Press-to-talk hotkey</div>'+
        '<div class="row-inline"><input type="text" id="hotkeyInput" style="max-width:220px" value="'+
          esc(draftOr("hotkeyInput", hk.main||"ctrl+space"))+'">'+
        '<button class="btn" id="btnCapture">Capture&#8230;</button>'+
        '<button class="btn primary" data-save-hotkey="1">Save</button>'+
        '<button class="btn ghost" data-reset-hotkey="1">Reset</button></div>'+
        '<div class="hint">Click Capture, then press the combination. Needs a modifier (Ctrl/Alt/Shift).</div>'+
      '</div>'+
      '<div class="page"><div class="section-title">Recording mode</div>'+
        radio("rm","set_recording_mode","hold","str","Hold to record", cfg("recording_mode","hold")==="hold")+
        radio("rm","set_recording_mode","toggle","str","Toggle (press start / press stop)", cfg("recording_mode")==="toggle")+
      '</div>'+
      '<div class="page"><div class="section-title">Paste</div>'+
        radio("pm","set_paste_mode","auto_paste","str","Auto-paste (Ctrl+V)", cfg("paste_mode")==="auto_paste")+
        radio("pm","set_paste_mode","clipboard_only","str","Clipboard only", cfg("paste_mode")==="clipboard_only")+
      '</div>'+
      '<div class="page"><div class="section-title">Primary language</div>'+
        radio("plang","set_primary_language","he","str","Hebrew (עברית)", cfg("primary_language","he")==="he")+
        radio("plang","set_primary_language","en","str","English", cfg("primary_language","he")==="en")+
        '<div class="hint">The language you mainly dictate and hold meetings in. Sets the summary language and which models the app prefers.</div>'+
      '</div>'+
      '<div class="page"><div class="section-title">Behavior</div>'+
        sw("Restore clipboard after paste", !!cfg("clipboard_auto_restore", true), "toggle_clipboard_auto_restore")+'<br>'+
        sw("Press Enter after paste", !!cfg("press_enter_after_paste", false), "toggle_press_enter_after_paste")+'<br>'+
        sw("Invisible mode (no overlay / waveform)", !!cfg("silent_mode", false), "toggle_silent_mode")+
      '</div>'+
      '<div class="page"><div class="section-title">Beep output</div>'+beepRows.join('')+'</div>'+
      '<div class="page"><div class="section-title">Keyboard shortcuts</div>'+shortcuts+'</div>'+
      '<div class="page"><div class="section-title">Local Whisper device ('+esc(S.whisper_device_label||"Auto")+')</div>'+
        '<div class="radio-row">'+((S.tables||{}).device||[]).map(function(r){
          return radio("dev","set_whisper_device",r.key,"str",r.label,r.checked,true,"");
        }).join('')+'</div></div>';
  };

  PAGES.audio = function(){
    var src = cfg("recording_source","microphone");
    var micOn = (src==="microphone"||src==="both");
    var sysOn = (src==="stereo_mix"||src==="both");
    var mid = cfg("input_device_index", null);
    var lid = cfg("loopback_device_index", null);
    function devRow(method, idx, name, checked){
      return '<div class="rowbtn'+(checked?' sel':'')+'" data-call="'+method+'" data-args=\''+
        JSON.stringify([idx])+'\''+(method==="toggle_loopback_device"?' data-slow="1"':'')+'>'+
        '<span class="ic">'+(checked?'&#9679;':'&#9675;')+'</span><span class="grow rtl-auto" dir="auto">'+esc(name)+'</span></div>';
    }
    var mics = [devRow("toggle_mic_device", null, "System Default", micOn && mid===null)];
    (S.mics||[]).forEach(function(d){ mics.push(devRow("toggle_mic_device", d.idx, d.name, micOn && mid===d.idx)); });
    // Dedicated meeting mic (radio semantics; null = follow the dictation mic).
    // Lets a headset own the meeting while a desk mic stays free for dictating
    // mid-meeting. Falls back to the dictation mic if the device is unplugged.
    var mmid = cfg("meeting_input_device_index", null);
    var mmics = [devRow("set_meeting_mic_device", null, "Same as dictation mic", mmid===null)];
    (S.mics||[]).forEach(function(d){ mmics.push(devRow("set_meeting_mic_device", d.idx, d.name, mmid===d.idx)); });
    var loops = "";
    if(S.loopback_available){
      var L = [devRow("toggle_loopback_device", null, "System Default", sysOn && lid===null)];
      (S.loopbacks||[]).forEach(function(d){ L.push(devRow("toggle_loopback_device", d.idx, d.name, sysOn && lid===d.idx)); });
      loops = '<div class="page"><div class="section-title">System audio</div>'+L.join('')+'</div>';
    }
    return '<div class="content-head"><h1>Audio</h1></div>'+
      '<div class="page"><div class="section-title">Recording sources</div>'+
        sw("Record my microphone", micOn, "toggle_record_mic")+'<br>'+
        sw("Record system audio", sysOn, "toggle_record_system", !S.loopback_available)+
        '<div class="hint">Whether each source is captured at all. Which devices are used is set below (Dictation / Meeting microphone).</div>'+
        '<div class="btnrow">'+btn("Refresh devices","refresh",[],"ghost",true)+'</div>'+
      '</div>'+
      '<div class="page"><div class="section-title">Dictation microphone</div>'+mics.join('')+
        '<div class="hint">The mic used for press-to-talk dictation.</div>'+
      '</div>'+
      '<div class="page"><div class="section-title">Meeting microphone</div>'+mmics.join('')+
        '<div class="hint">The mic that records YOUR side of a meeting (needs "Record my microphone" on above). Pick your call headset here for its close-up quality in meeting transcripts, while the Dictation microphone stays free for dictating mid-meeting. If this device is unplugged, meetings fall back to the dictation mic.</div>'+
      '</div>'+
      loops;
  };

  PAGES.models = function(){
    var t = (S.tables||{});
    function group(title, rows, method, argKey, argtype, slow){
      var html = '<div class="page"><div class="section-title">'+esc(title)+'</div>';
      (rows||[]).forEach(function(r){
        html += radio(method, method, String(r[argKey]), argtype, r.label, r.checked, r.enabled!==false, r.note||"", r.where||"", r.wnote||"");
      });
      html += '</div>';
      return html;
    }
    return '<div class="content-head"><h1>Models</h1></div>'+
      group("Dictation model", t.dictation, "set_dictation_model", "idx", "int")+
      group("Meeting transcription model", t.meeting, "set_meeting_model", "key", "str")+
      group("Summary model", t.summary, "set_summary_model", "model", "str")+
      '<div class="page">'+
        sw("Local summaries: add a dedicated tasks pass (more complete task list, +15-45s)", !!cfg("summary_local_tasks_pass",false), "toggle_summary_local_tasks_pass")+
        '<div class="hint">Local (Ollama) meeting summaries only. Runs a narrow second pass that extracts every commitment and replaces the task list; strips speaker-label owners.</div>'+
        sw("Local summaries: merge twice-discussed topics (consolidate pass)", !!cfg("summary_consolidate_pass",true), "toggle_summary_consolidate_pass")+
        sw("Local summaries: mark tasks completed during the meeting as [x]", !!cfg("summary_task_done_pass",true), "toggle_summary_task_done_pass")+
        sw("Cloud summaries: parity rules + code cleanups (dedup, tone, owners)", !!cfg("summary_cloud_parity",true), "toggle_summary_cloud_parity")+
        '<div class="hint">Quality passes ported from a private upstream project. The two local passes each add a narrow Ollama call; the cloud row only extends the prompt and runs free code cleanups.</div>'+
      '</div>'+
      '<div class="page"><div class="section-title">Summary language</div>'+
        '<div class="radio-row">'+
        radio("slang","set_summary_language","primary","str","Follow primary language", cfg("summary_language","primary")==="primary")+
        radio("slang","set_summary_language","auto","str","Follow the transcript", cfg("summary_language")==="auto")+
        radio("slang","set_summary_language","he","str","Always Hebrew", cfg("summary_language")==="he")+
        radio("slang","set_summary_language","en","str","Always English", cfg("summary_language")==="en")+
        '</div>'+
        '<div class="hint">The language meeting and text summaries are written in, regardless of the language spoken.</div>'+
      '</div>'+
      group("Transcribe-file model", t.file, "set_file_model", "key", "str");
  };

  PAGES.cleanup = function(){
    var t = (S.tables||{});
    var styles = (t.cleanup_styles||[]).map(function(r){
      return radio("cs","set_cleanup_style",r.style,"str",r.label,r.checked,true,"");
    }).join('');
    var models = (t.cleanup_models||[]);
    var mhtml;
    if(!models.length){
      mhtml = '<div class="empty"><div class="big">&#128273;</div><div>No cleanup provider key set</div>'+
        '<div class="sub">Add an OpenAI, Groq, or Gemini key on the Keys page to enable AI cleanup.</div>'+
        '<button class="btn" data-page-link="keys">Go to Keys</button></div>';
    } else {
      mhtml = models.map(function(r){
        return radio("cm","set_cleanup_provider_model",JSON.stringify([r.provider,r.model]),"json",r.label,r.checked,true,"");
      }).join('');
    }
    return '<div class="content-head"><h1>AI Cleanup</h1>'+
      '<span class="status"><span class="dot"></span>'+esc(S.cleanup_model_label||"")+'</span></div>'+
      '<div class="page"><div class="section-title">Style</div>'+styles+'</div>'+
      '<div class="page"><div class="section-title">Provider &amp; model</div>'+mhtml+'</div>';
  };

  var KEYCARDS = [
    {svc:"openai", key:"openai_api_key", name:"OpenAI", badge:"O", color:"#0aa37f",
     sub:"Paid - best dictation, meeting transcription & summaries.",
     save:"Save & Verify", url:"https://platform.openai.com/api-keys", slow:true},
    {svc:"gemini", key:"gemini_api_key", name:"Gemini", badge:"G", color:"#3b6fd4",
     sub:"Free meeting summaries + free AI cleanup (Google AI Studio).",
     save:"Save", url:"https://aistudio.google.com/apikey", slow:true},
    {svc:"hf", key:"hf_token", name:"Local Diarization", badge:"H", color:"#b08900",
     sub:"100% local speaker labels on your GPU (pyannote + ivrit.ai).",
     save:"Save & Download", url:"https://huggingface.co/settings/tokens", slow:true, noun:"token"},
    {svc:"groq", key:"groq_api_key", name:"Groq", badge:"G", color:"#d97706",
     sub:"Free, fast cloud dictation (Whisper v3 Turbo).",
     save:"Save & Verify", url:"https://console.groq.com/keys", slow:true}
    // AssemblyAI (paid cloud diarization) card removed 2026-09-01 - local
    // pyannote + Gemini diarize cover meetings; the key/code path still exist.
  ];

  PAGES.keys = function(){
    var secrets = S.secrets||{}, has = S.has||{};
    var cards = KEYCARDS.map(function(c){
      var noun = c.noun||"key";
      var cur = has[c.key] ? '<span class="masked">'+esc(secrets[c.key]||"set")+'</span>'
                           : '<span class="masked empty">not set</span>';
      var iid = "in_"+c.svc;
      return '<div class="credcard">'+
        '<div class="head"><span class="bdg" style="background:'+c.color+'">'+esc(c.badge)+'</span>'+
        '<div><h3>'+esc(c.name)+'</h3><div class="sub">'+esc(c.sub)+'</div></div></div>'+
        '<div class="keyline">Current '+esc(noun)+': '+cur+'</div>'+
        '<div class="row-inline" style="margin-top:8px">'+
          '<input type="password" id="'+iid+'" class="mono" placeholder="Enter '+esc(noun)+'…" style="flex:1" value="'+esc(draftOr(iid,""))+'">'+
          '<button class="btn ghost" data-show="'+iid+'">Show</button></div>'+
        '<div class="btnrow">'+
          '<button class="btn primary" data-apply-key="'+c.svc+'" data-input="'+iid+'"'+(c.slow?' data-slow="1"':'')+'>'+esc(c.save)+'</button>'+
          '<button class="btn danger" data-call="clear_key" data-args=\''+JSON.stringify([c.svc])+'\'>Clear</button>'+
          '<span class="grow"></span>'+
          '<a href="#" data-openurl="'+esc(c.url)+'">Get a '+esc(noun)+' ↗</a>'+
        '</div>'+
        '<div class="hint" id="st_'+c.svc+'"></div>'+
      '</div>';
    }).join('');
    var privacy = '<div class="privacy-note">'+
      '<span class="pn-ico">&#9888;&#65039;</span>'+
      '<span>Please note: on <b>free cloud tiers</b>, providers often use the audio and text you send to <b>train their models</b>. Be mindful of the sensitivity of what you dictate or share - avoid confidential information on free plans. Local models (on your own GPU) never leave this machine.</span>'+
    '</div>';
    return '<div class="content-head"><h1>API Keys</h1></div><div class="page keys-page">'+privacy+cards+'</div>';
  };

  // ---- Transcription server: choose CLIENT (use a home server) or SERVER
  //      (host one on this GPU). Moved out of Keys into its own page. ----
  PAGES.server = function(){
    var has = S.has || {};
    var sv = S.serve || {};
    var gpu = sv.gpu || {};
    var svPort = sv.port || 9090;
    var role = sv.role || "";
    if(!role){ role = sv.enabled ? "server" : (cfg("remote_server_url","") ? "client" : ""); }

    function modeCard(r, icon, title, desc){
      return '<button class="mode-card'+(role===r?' sel':'')+'" data-set-role="'+r+'">'+
        '<span class="mode-ico">'+icon+'</span>'+
        '<span class="mode-t">'+esc(title)+'</span>'+
        '<span class="mode-d">'+esc(desc)+'</span></button>';
    }
    var chooser = '<div class="mode-row">'+
      modeCard("client","&#128421;","Client mode","Use a server: send audio to a transcription server running on another machine (your home GPU).")+
      modeCard("server","&#128225;","Server mode","Host a server: run one on THIS machine’s GPU so your other devices transcribe against it.")+
    '</div>';

    // Prominent prerequisite: both machines need Tailscale.
    var tsBanner = '<div class="ts-banner">'+
      '<div class="ts-left">'+
        '<span class="ts-ico">&#128279;</span>'+
        '<div class="ts-body"><div class="ts-title">First, install Tailscale on both machines</div>'+
        '<div class="ts-sub">It’s a free app that puts your PCs on one private network so they can reach each other - <b>no router setup, no open ports</b>. Sign in with the <b>same account</b> on the server and every device that connects.</div></div>'+
      '</div>'+
      '<button class="btn primary ts-btn" data-call="open_tailscale" data-args="[]">&#11015;&#65039;&nbsp; Download Tailscale</button>'+
    '</div>';

    // --- CLIENT panel (was Home Server) ---
    var url = draftOr("in_remote_url", cfg("remote_server_url",""));
    var clientPanel =
      '<div class="srv-panel">'+
        '<div class="srv-h"><span class="srv-ico" style="background:#0aa37f">&#128421;</span>'+
          '<div><h3>Connect to a transcription server</h3><div class="sub">Enter the address of the server you run at home (over Tailscale).</div></div></div>'+
        field("Server URL",'<input type="text" id="in_remote_url" class="mono" placeholder="ws://host:9090" value="'+esc(url)+'">',
          "host:9090 · ws://host:9090 · wss://your-domain")+
        field("Access token (optional)",
          '<input type="password" id="in_remote_tok" class="mono" placeholder="'+
          (has.remote_server_token?"(saved - leave blank to keep)":"none")+'" value="'+esc(draftOr("in_remote_tok",""))+'">')+
        '<div class="btnrow">'+
          '<button class="btn primary" data-apply-remote="1" data-slow="1">Save</button>'+
          '<button class="btn" data-test-remote="1" data-slow="1">Test</button>'+
          '<button class="btn danger" data-call="clear_key" data-args=\'["remote"]\'>Clear</button>'+
          '<span class="grow"></span></div>'+
        '<div class="hint" id="st_remote"></div>'+
        '<details class="setup-help"><summary>How to set up (Tailscale)</summary><ol>'+
          '<li>Install <b>Tailscale</b> on both machines from <span class="mono">tailscale.com/download</span> and sign in with the <b>same account</b>.</li>'+
          '<li>On the server machine, switch to <b>Server mode</b> (the other tab here), turn it on, and note its <span class="mono">ws://…</span> address.</li>'+
          '<li>Paste that address above, click <b>Test</b>, then <b>Save</b>.</li>'+
          '<li>On the <b>Models</b> page, pick a <b>Home Server</b> model.</li>'+
        '</ol><div class="hint">Work PC that blocks Tailscale? Use a Cloudflare tunnel with a <span class="mono">wss://your-domain</span> URL + token.</div></details>'+
      '</div>';

    // --- SERVER panel (host) with a GPU check + a graphical run control ---
    var verdict = gpu.verdict || "none";
    var gpuTitle = gpu.has_cuda
      ? (esc(gpu.name || "CUDA GPU") + (gpu.vram_gb ? (' · ' + gpu.vram_gb + ' GB VRAM') : ''))
      : "No dedicated GPU detected";
    var gpuCard = '<div class="gpu-card '+verdict+'">'+
      '<div class="gpu-h"><span class="gpu-dot"></span><span class="gpu-name">'+gpuTitle+'</span>'+
        '<span class="gpu-tag '+verdict+'">'+({good:"Ready",marginal:"Marginal",none:"Not suitable"}[verdict])+'</span></div>'+
      '<div class="gpu-note">'+esc(gpu.note||"")+'</div></div>';
    var canServe = (verdict !== "none");
    var runTitle = sv.running ? "Server is running" : (sv.enabled ? "Starting…" : "Server is off");
    var runSub = sv.running ? ("Listening on port " + svPort)
                            : (canServe ? "Turn on to host on this GPU" : "Needs a dedicated GPU");
    var runState = sv.running ? "on" : (sv.enabled ? "starting" : "off");
    var runControl = '<div class="run-box '+runState+(canServe?'':' locked')+'">'+
      '<span class="run-led"></span>'+
      '<div class="run-txt"><div class="run-title">'+runTitle+'</div><div class="run-sub">'+runSub+'</div></div>'+
      '<label class="big-switch'+(canServe?'':' disabled')+'" title="'+(canServe?'':'No suitable GPU')+'">'+
        '<input type="checkbox" data-toggle="toggle_serve"'+(sv.enabled?' checked':'')+(canServe?'':' disabled')+'>'+
        '<span class="bs-track"></span><span class="bs-knob"></span></label>'+
    '</div>';
    var urlLine = sv.ws_url
      ? ('Other devices point their <b>Server URL</b> (the Use-a-server tab) here:<br><span class="mono copyable">' + esc(sv.ws_url) + '</span>')
      : ('Install <a href="https://tailscale.com/download" target="_blank" rel="noopener">Tailscale</a> and sign in (same account) on both machines to get a private address, then use <span class="mono">ws://&lt;this-ip&gt;:' + svPort + '</span>.');
    var serverPanel =
      '<div class="srv-panel">'+
        '<div class="srv-h"><span class="srv-ico" style="background:#7c5cff">&#128225;</span>'+
          '<div><h3>Host a transcription server</h3><div class="sub">Runs Lia’s Hebrew model on this GPU. No Docker, no extra install.</div></div></div>'+
        gpuCard + runControl +
        field("Port",'<input type="number" id="in_serve_port" class="mono" value="'+svPort+'" style="max-width:120px"'+(canServe?'':' disabled')+'>',"default 9090")+
        field("Access token (optional)",
          '<input type="password" id="in_serve_tok" class="mono" placeholder="'+
          (sv.has_token?"(saved - leave blank to keep)":"none")+'"'+(canServe?'':' disabled')+'>')+
        '<div class="btnrow"><button class="btn" data-apply-serve="1" data-slow="1"'+(canServe?'':' disabled')+'>Save port / token</button><span class="grow"></span></div>'+
        sw("Keep running after reboot (start at Windows logon)", !!sv.autostart, "toggle_serve_autostart", !canServe)+
        '<div class="hint">'+urlLine+'</div>'+
        '<details class="setup-help"><summary>How another device connects</summary><ol>'+
          '<li>Turn the server on above - it loads a local Hebrew model on this GPU.</li>'+
          '<li>Install <b>Tailscale</b> on both machines, same account.</li>'+
          '<li>On the other device: <b>Transcription server → Client mode</b>, paste the URL above, <b>Test</b>, <b>Save</b>, then pick a <b>Home Server</b> model.</li>'+
        '</ol><div class="hint">Hebrew only (one model); English falls back to the client’s own cloud/local.</div></details>'+
        '<div class="hint" id="st_serve"></div>'+
      '</div>';

    var body = chooser + tsBanner;
    if(role==="client") body += clientPanel;
    else if(role==="server") body += serverPanel;
    else body += '<div class="empty" style="padding:26px 8px"><div class="sub">Pick whether this device <b>uses</b> a server or <b>hosts</b> one.</div></div>';

    return '<div class="content-head"><h1>Transcription server</h1>'+
      '<div class="sub">Run Lia’s Hebrew transcription on one machine’s GPU and reach it from your others - no Docker.</div></div>'+
      '<div class="page">'+body+'</div>';
  };

  PAGES.meetings = function(){
    var hk = S.hotkeys||{};
    function actrow(icon, label, method, hkstr, beta){
      return '<div class="rowbtn" data-call="'+method+'" data-args="[]">'+
        '<span class="ic">'+icon+'</span><span class="grow">'+esc(label)+'</span>'+
        (beta?'<span class="wb beta">BETA</span>':'')+
        (hkstr?'<span class="hk">'+esc(hkstr)+'</span>':'')+'</div>';
    }
    var live = S.live_transcript_available
      ? actrow('&#128196;','Open live transcript','open_live_transcript','')
      : '<div class="rowbtn disabledrow"><span class="ic">&#128196;</span><span class="grow">Live transcript (none active)</span></div>';
    return '<div class="content-head"><h1>Meetings</h1></div>'+
      '<div class="page">'+
        sw("Auto-detect Zoom / Teams / Meet calls", !!cfg("auto_detect_meetings",false), "toggle_auto_detect_meetings")+
      '</div>'+
      '<div class="page"><div class="section-title">Tools</div>'+
        actrow('&#128269;','Ask your meetings…','open_meetings_ask',hk.ask,true)+
        actrow('&#127908;','Voice ask (speak a question, press again to answer)','voice_ask_now',hk.voice_ask,true)+
        field("Voice ask answer goes to",
          radio("vao","set_voice_ask_output","card","str","Answer card (always visible)", cfg("voice_ask_output","card")==="card")+
          radio("vao","set_voice_ask_output","paste","str","Paste at cursor", cfg("voice_ask_output")==="paste")+
          radio("vao","set_voice_ask_output","both","str","Both", cfg("voice_ask_output")==="both"))+
        actrow('&#128203;','Action items…','open_action_items',hk.actions)+
        actrow('&#128193;','Open meeting folder','open_meetings_folder','')+
        actrow('&#9998;','Edit a meeting summary…','edit_meeting_summary','')+
        actrow('&#128101;','Rename speakers in a meeting…','rename_speakers_old','')+
        actrow('&#127908;','Transcribe a file…','transcribe_file','')+
        actrow('&#128221;','Summarize text / file…','summarize_text_dialog','')+
        live+
      '</div>';
  };

  PAGES.vocab = function(){
    return '<div class="content-head"><h1>Vocabulary</h1></div>'+
      '<div class="page"><div class="section-title">Manual terms</div>'+
        '<div class="hint">Comma- or newline-separated terms the transcriber should prefer.</div>'+
        '<textarea id="vocabText" class="rtl-auto" dir="auto" style="min-height:120px">'+esc(draftOr("vocabText", cfg("custom_vocabulary","")))+'</textarea>'+
        '<div class="btnrow"><button class="btn primary" data-save-vocab="1">Save terms</button>'+
        '<button class="btn ghost" data-reset-vocab="1">Reset to default terms</button>'+
        sw("Auto-learn new terms from meetings", !!cfg("vocab_autolearn",true), "toggle_vocab_autolearn")+'</div>'+
      '</div>'+
      '<div class="page"><div class="section-title">Suggestions <span class="badge soft">'+(S.vocab_pending||0)+'</span></div>'+
        '<div class="btnrow">'+btn("Load suggestions","__load_pending",[],"")+
        '<button class="btn ghost" data-vocab-apply="1">Apply checked / reject rest</button></div>'+
        '<div id="pendingList"></div></div>'+
      '<div class="page"><div class="section-title">Learned terms</div>'+
        '<div class="btnrow">'+btn("Load learned","__load_learned",[],"")+
        '<button class="btn danger" data-vocab-remove="1">Remove checked</button></div>'+
        '<div id="learnedList"></div></div>'+
      '<div class="page"><div class="section-title">Corrections (wrong &#8594; right)</div>'+
        '<div class="hint">&#215;N = times applied in live dictation/meetings. hits = matches across your saved archive (press Scan). "unused" = zero of both &#8594; safe to prune.</div>'+
        '<div class="btnrow">'+btn("Load corrections","__load_corr",[],"")+
          btn("Scan archive","__scan_corr",[],"ghost",true)+
          '<button class="btn" data-corr-unused="1">Select unused</button>'+
          '<button class="btn danger" data-corr-remove="1">Remove checked</button></div>'+
        '<div class="btnrow">'+btn("Harvest new meetings now","harvest_corrections_now",[],"ghost",true)+
          sw("Auto-harvest corrections from new meetings (local)", !!cfg("corrections_autoharvest",true), "toggle_corrections_autoharvest")+'</div>'+
        '<div id="corrList"></div>'+
        '<div class="row-inline" style="margin-top:8px">'+
          '<input type="text" id="corrWrong" class="mono" placeholder="Wrong" style="max-width:180px">'+
          '<span>&#8594;</span>'+
          '<input type="text" id="corrRight" class="mono" placeholder="Right" style="max-width:180px">'+
          '<button class="btn" data-add-corr="1">Add</button></div>'+
      '</div>'+
      '<div class="page"><div class="btnrow">'+btn("Scan meetings for new terms","vocab_rebuild",[],"ghost",true)+'</div></div>';
  };

  PAGES.snippets = function(){
    return '<div class="content-head"><h1>Snippets</h1></div>'+
      '<div class="page"><div class="hint">Say a cue to paste its expansion. Use \\n for line breaks.</div>'+
        '<div class="btnrow">'+btn("Load snippets","__load_snips",[],"")+
        '<button class="btn" data-add-snip="1">Add row</button>'+
        '<button class="btn primary" data-save-snips="1">Save</button></div>'+
        '<div id="snipList"></div></div>';
  };

  PAGES.advanced = function(){
    var p = S.paths||{};
    return '<div class="content-head"><h1>Advanced</h1></div>'+
      '<div class="page"><div class="btnrow">'+
        btn("Restart Lia","restart_app",[],"")+
        btn("Open log","open_log",[],"ghost")+
        btn("Open config folder","open_config_dir",[],"ghost")+
        btn("Report a problem…","report_problem",[],"ghost",true)+
        '<button class="btn danger" data-quit="1">Quit Lia</button>'+
      '</div>'+
      '<div class="hint">Report a problem builds a diagnostic zip (log + sanitized settings - API keys and personal lists removed), shows it in Explorer, and opens the GitHub issue page. Nothing is sent automatically - you attach the file yourself.</div>'+
      '</div>'+
      '<div class="page"><div class="section-title">About</div>'+
        '<div class="kv"><span class="k">Config</span><span class="rtl-auto" dir="auto">'+esc(p.config||"")+'</span></div>'+
        '<div class="kv"><span class="k">Log</span><span class="rtl-auto" dir="auto">'+esc(p.log||"")+'</span></div>'+
        '<div class="kv"><span class="k">Meetings</span><span class="rtl-auto" dir="auto">'+esc(p.meetings||"")+'</span></div>'+
      '</div>'+
      '<div class="page"><div class="section-title">Privacy &amp; data</div>'+
        '<div class="hint">Everything Lia stores lives in the config folder above: recordings '+
        '(WAV kept ~30 days, Opus ~2 years), transcripts, summaries, history, indexes and settings. '+
        'Transcripts are kept until you delete them.</div>'+
        '<div class="btnrow"><button class="btn danger" data-wipe="1">Delete all my data…</button></div>'+
      '</div>';
  };

  // ---------- render ----------
  function setStatus(){
    var el = $("hdrStatus");
    if(!el) return;
    var busy = S.recording || S.meeting_active;
    el.className = "status " + (busy ? "busy" : "ok");
    el.innerHTML = '<span class="dot"></span>'+esc(S.status_line||"Ready");
  }

  function render(){
    var content = $("content");
    if(!content) return;
    var sc = content.scrollTop;
    // preserve focused text input value + caret across re-render
    var ae = document.activeElement, aid = ae && ae.id, asel = null;
    if(ae && (ae.tagName==="INPUT"||ae.tagName==="TEXTAREA")){
      try{ asel = [ae.selectionStart, ae.selectionEnd]; }catch(e){}
    }
    var fn = PAGES[PAGE] || PAGES.general;
    content.innerHTML =
      '<div style="position:sticky;top:-20px;background:var(--bg);padding:2px 0 8px;z-index:4;margin:-4px 0 4px">'+
      '<span class="status ok" id="hdrStatus"><span class="dot"></span>Ready</span></div>'+ fn();
    setStatus();
    content.scrollTop = sc;
    document.querySelectorAll('.nav-item').forEach(function(b){
      b.classList.toggle('on', b.getAttribute('data-page')===PAGE);
    });
    if(aid){ var re=document.getElementById(aid); if(re){ try{ re.focus();
      if(asel && re.setSelectionRange) re.setSelectionRange(asel[0],asel[1]); }catch(e){} } }
  }

  function go(page){ PAGE = page; render(); }

  // ---------- push handling ----------
  RK.onPush(function(msg){
    var t = msg.t;
    if(t==="tick"){
      var liveChanged = (S.live_transcript_available !== msg.live_transcript_available);
      S.status_line = msg.status_line; S.recording = msg.recording;
      S.meeting_active = msg.meeting_active; S.live_transcript_available = msg.live_transcript_available;
      setStatus();
      // the Meetings page shows a live-transcript affordance driven by this flag
      if(liveChanged && PAGE==="meetings") render();
      return;
    }
    if(t==="state"){ S = msg.state || S; render(); return; }
    if(t==="result"){
      var p = pending[msg.id]; if(p){ delete pending[msg.id]; p.res(msg); }
      if(msg.msg) RK.toast(msg.msg, msg.ok?"ok":"err");
      return;
    }
    if(t==="toast"){ RK.toast(msg.msg, msg.level); return; }
    if(t==="focus"){ if(msg.page){ go(msg.page); } return; }
  });

  // ---------- event wiring (delegated, attached once) ----------
  function argOf(elm){
    var v = elm.getAttribute('data-arg'), ty = elm.getAttribute('data-argtype');
    if(ty==="int") return parseInt(v,10);
    if(ty==="null") return null;
    if(ty==="json"){ try{ return JSON.parse(v); }catch(e){ return v; } }
    return v;
  }

  document.addEventListener('click', function(e){
    var el = e.target.closest('[data-page]');
    if(el){ go(el.getAttribute('data-page')); return; }
    var pl = e.target.closest('[data-page-link]');
    if(pl){ go(pl.getAttribute('data-page-link')); return; }
    var url = e.target.closest('[data-openurl]');
    if(url){ e.preventDefault(); try{ RK.api.emit({t:"openurl", url:url.getAttribute('data-openurl')}); }catch(_){}
             window.open(url.getAttribute('data-openurl'),'_blank'); return; }
    var show = e.target.closest('[data-show]');
    if(show){ var inp=document.getElementById(show.getAttribute('data-show'));
      if(inp){ inp.type = inp.type==="password"?"text":"password"; show.textContent = inp.type==="password"?"Show":"Hide"; } return; }
    // generic call button
    var cb = e.target.closest('[data-call]');
    if(cb){
      var m = cb.getAttribute('data-call');
      var args = JSON.parse(cb.getAttribute('data-args')||"[]");
      var slow = cb.getAttribute('data-slow')==="1";
      // internal loaders (client-only)
      if(m==="__load_pending"){ loadList("vocab_pending_list","pendingList","pending"); return; }
      if(m==="__load_learned"){ loadList("vocab_learned_list","learnedList","learned"); return; }
      if(m==="__load_corr"){ loadCorr(); return; }
      if(m==="__scan_corr"){ busy(cb,true); call("vocab_corrections_scan",[],true).then(function(r){ unbusy(cb); renderCorr(r.data||[]); }); return; }
      if(m==="harvest_corrections_now"){ busy(cb,true); call(m,args,true).then(function(){ unbusy(cb); loadCorr(); }); return; }
      if(m==="__load_snips"){ loadSnips(); return; }
      busy(cb, slow);
      call(m, args, slow).then(function(){ unbusy(cb); });
      return;
    }
    // Hotkey capture: read the combo IN THIS WINDOW via keydown, NOT the
    // parent's global keyboard.read_hotkey - that fought the app's own keyboard
    // hooks + the dead-hook watchdog and could crash/hang the app. The parent
    // still validates (needs a modifier) when Save is clicked.
    var cap = e.target.closest('#btnCapture');
    if(cap){
      var input=document.getElementById("hotkeyInput");
      var orig=cap.textContent; cap.textContent="Press keys…"; cap.classList.add("primary");
      function keyName(ev){
        var c=ev.code||"";
        if(/^Key[A-Z]$/.test(c)) return c.slice(3).toLowerCase();
        if(/^Digit[0-9]$/.test(c)) return c.slice(5);
        if(/^Numpad[0-9]$/.test(c)) return c.slice(6);
        if(/^F([1-9]|1[0-9]|2[0-4])$/.test(c)) return c.toLowerCase();
        var M={Space:"space",Enter:"enter",Tab:"tab",Backspace:"backspace",
          Delete:"delete",Escape:"esc",Home:"home",End:"end",Insert:"insert",
          PageUp:"page up",PageDown:"page down",ArrowUp:"up",ArrowDown:"down",
          ArrowLeft:"left",ArrowRight:"right",Minus:"-",Equal:"=",Semicolon:";",
          Quote:"'",Comma:",",Period:".",Slash:"/",Backquote:"`",
          BracketLeft:"[",BracketRight:"]"};
        if(M[c]) return M[c];
        if(/^(Control|Alt|Shift|Meta|OS)/.test(c)) return null; // pure modifier
        if(ev.key && ev.key.length===1) return ev.key.toLowerCase();
        return null;
      }
      function stop(){ document.removeEventListener("keydown",onKey,true);
        cap.textContent=orig; cap.classList.remove("primary"); }
      function onKey(ev){
        ev.preventDefault(); ev.stopPropagation();
        var bare=!ev.ctrlKey&&!ev.altKey&&!ev.shiftKey&&!ev.metaKey;
        if(ev.key==="Escape"&&bare){ stop(); return; }   // cancel capture
        var k=keyName(ev); if(k===null) return;           // wait for a real key
        var mods=[]; if(ev.ctrlKey)mods.push("ctrl"); if(ev.altKey)mods.push("alt");
        if(ev.shiftKey)mods.push("shift"); if(ev.metaKey)mods.push("windows");
        var combo=mods.concat([k]).join("+");
        DRAFT["hotkeyInput"]=combo; if(input) input.value=combo;
        stop();
      }
      document.addEventListener("keydown",onKey,true);
      return;
    }
    var sh = e.target.closest('[data-save-hotkey]');
    if(sh){ var v=(document.getElementById("hotkeyInput")||{}).value||"";
      call("set_hotkey",[v]).then(function(r){ if(r.ok) delete DRAFT["hotkeyInput"]; }); return; }
    var rh = e.target.closest('[data-reset-hotkey]');
    if(rh){ DRAFT["hotkeyInput"]="ctrl+space"; call("set_hotkey",["ctrl+space"]); return; }
    var ak = e.target.closest('[data-apply-key]');
    if(ak){ var svc=ak.getAttribute('data-apply-key'); var iid=ak.getAttribute('data-input');
      var val=(document.getElementById(iid)||{}).value||"";
      busy(ak,true); call("apply_key",[svc,val],true).then(function(r){ unbusy(ak);
        if(r.ok) delete DRAFT[iid]; var st=document.getElementById("st_"+svc); if(st){st.textContent=r.msg||"";} }); return; }
    var ar = e.target.closest('[data-apply-remote]');
    if(ar){ var u=(document.getElementById("in_remote_url")||{}).value||"";
      var tk=(document.getElementById("in_remote_tok")||{}).value||"";
      busy(ar,true); call("apply_remote",[u,tk],true).then(function(r){ unbusy(ar);
        if(r.ok){ delete DRAFT["in_remote_url"]; delete DRAFT["in_remote_tok"]; }
        var st=document.getElementById("st_remote"); if(st) st.textContent=r.msg||""; }); return; }
    var tr = e.target.closest('[data-test-remote]');
    if(tr){ var u2=(document.getElementById("in_remote_url")||{}).value||"";
      var tk2=(document.getElementById("in_remote_tok")||{}).value||"";
      busy(tr,true); call("test_remote",[u2,tk2],true).then(function(r){ unbusy(tr);
        var st=document.getElementById("st_remote"); if(st) st.textContent=r.msg||""; }); return; }
    var asv = e.target.closest('[data-apply-serve]');
    if(asv){ var sp=(document.getElementById("in_serve_port")||{}).value||"";
      var stk=(document.getElementById("in_serve_tok")||{}).value||"";
      busy(asv,true); call("apply_serve",[sp,stk],true).then(function(r){ unbusy(asv);
        if(r.ok){ delete DRAFT["in_serve_port"]; delete DRAFT["in_serve_tok"]; }
        var st=document.getElementById("st_serve"); if(st) st.textContent=r.msg||""; }); return; }
    var mc = e.target.closest('[data-set-role]');
    if(mc){ call("set_transcription_role",[mc.getAttribute('data-set-role')]); return; }
    var sv = e.target.closest('[data-save-vocab]');
    if(sv){ var vt=(document.getElementById("vocabText")||{}).value||"";
      call("save_vocabulary",[vt]).then(function(r){ if(r.ok) delete DRAFT["vocabText"]; }); return; }
    var rv = e.target.closest('[data-reset-vocab]');
    if(rv){ if(!confirm("Replace the terms above with the shipped default set?")) return;
      call("reset_vocab_default",[]).then(function(r){ if(r.ok) delete DRAFT["vocabText"]; }); return; }
    var va = e.target.closest('[data-vocab-apply]');
    if(va){ applyPending(); return; }
    var vr = e.target.closest('[data-vocab-remove]');
    if(vr){ removeLearned(); return; }
    var ac = e.target.closest('[data-add-corr]');
    if(ac){ var w=(document.getElementById("corrWrong")||{}).value||"";
      var ri=(document.getElementById("corrRight")||{}).value||"";
      call("vocab_add_correction",[w,ri]).then(function(r){ if(r.ok){ loadCorr();
        var a=document.getElementById("corrWrong"); var b=document.getElementById("corrRight"); if(a)a.value=""; if(b)b.value=""; } }); return; }
    var dc = e.target.closest('[data-del-corr]');
    if(dc){ call("vocab_remove_correction",[dc.getAttribute('data-del-corr')]).then(loadCorr); return; }
    var cun = e.target.closest('[data-corr-unused]');
    if(cun){ Array.prototype.slice.call(document.querySelectorAll('#corrList tr.corr-unused input[data-ck]'))
      .forEach(function(x){ x.checked=true; }); return; }
    var crm = e.target.closest('[data-corr-remove]');
    if(crm){ var keys=Array.prototype.slice.call(document.querySelectorAll('#corrList input[data-ck]'))
      .filter(function(x){return x.checked;}).map(function(x){return x.getAttribute('data-ck');});
      if(!keys.length) return;
      if(!confirm("Remove "+keys.length+" correction(s)?")) return;
      call("vocab_remove_corrections",[keys]).then(loadCorr); return; }
    var asn = e.target.closest('[data-add-snip]');
    if(asn){ addSnipRow("",""); return; }
    var dsn = e.target.closest('[data-del-snip]');
    if(dsn){ dsn.closest('tr').remove(); return; }
    var ss = e.target.closest('[data-save-snips]');
    if(ss){ saveSnips(); return; }
    var q = e.target.closest('[data-quit]');
    if(q){ if(confirm("Quit Lia? Dictation and meetings will stop.")) call("quit_app",[]); return; }
    var dw = e.target.closest('[data-wipe]');
    if(dw){
      var msg = "Delete ALL Lia data?\n\nThis permanently removes:\n"+
        "- meeting recordings, transcripts and summaries\n"+
        "- transcription history and chat history\n"+
        "- the email/meeting indexes and voiceprints\n"+
        "- vocabulary, settings, API keys and the log\n\n"+
        "Close any other open Lia windows first.\n"+
        "Lia will quit when done. This cannot be undone.";
      if(!confirm(msg)) return;
      var typed = prompt("Type DELETE to confirm:");
      if(((typed||"").trim().toUpperCase())!=="DELETE") return;
      call("delete_all_data",[],true); return;
    }
  });

  document.addEventListener('change', function(e){
    var t = e.target;
    if(t.matches('[data-toggle]')){ call(t.getAttribute('data-toggle'),[]); return; }
    if(t.matches('[data-radio]')){ if(t.checked){ var m=t.getAttribute('data-radio'), a=argOf(t);
      call(m, m==="set_cleanup_provider_model"?a:[a], t.getAttribute('data-slow')==="1"); } return; }
  });

  document.addEventListener('input', function(e){
    var t = e.target;
    if(t.id && (t.tagName==="INPUT"||t.tagName==="TEXTAREA")){ DRAFT[t.id]=t.value; }
  });

  function busy(el, slow){ if(el && slow){ el.classList.add('disabled'); el.dataset._t=el.textContent;
    el.innerHTML='<span class="spinner"></span>'; } }
  function unbusy(el){ if(el && el.dataset._t!=null){ el.classList.remove('disabled'); el.textContent=el.dataset._t; delete el.dataset._t; } }

  // ---------- vocab / snippets list loaders ----------
  function loadList(method, targetId, kind){
    call(method,[]).then(function(r){
      var data = r.data||[]; var el=document.getElementById(targetId); if(!el) return;
      if(!data.length){ el.innerHTML='<div class="hint">None.</div>'; return; }
      el.setAttribute("data-kind", kind);
      el.innerHTML = data.map(function(it){
        var k = it.term!=null ? it.term : (it.key||"");
        var meta = kind==="pending" ? ("×"+(it.count_corpus||0)) : ("used "+(it.count_used||0));
        return '<label class="check"><input type="checkbox" data-vk="'+esc(k)+'"><span class="box"></span>'+
          '<span class="txt rtl-auto" dir="auto">'+esc(k)+' <small>'+esc(meta)+'</small></span></label>';
      }).join('');
    });
  }
  function checkedKeys(id){ return Array.prototype.slice.call(
    document.querySelectorAll('#'+id+' input[data-vk]')).filter(function(x){return x.checked;})
    .map(function(x){return x.getAttribute('data-vk');}); }
  function allKeys(id){ return Array.prototype.slice.call(
    document.querySelectorAll('#'+id+' input[data-vk]')).map(function(x){return x.getAttribute('data-vk');}); }
  function applyPending(){
    var approve = checkedKeys("pendingList");
    var all = allKeys("pendingList");
    var reject = all.filter(function(k){ return approve.indexOf(k)<0; });
    call("vocab_resolve",[approve,reject]).then(function(){ loadList("vocab_pending_list","pendingList","pending"); });
  }
  function removeLearned(){
    var keys = checkedKeys("learnedList");
    if(!keys.length) return;
    call("vocab_remove_learned",[keys]).then(function(){ loadList("vocab_learned_list","learnedList","learned"); });
  }
  function corrUnused(c){
    // "unused" only makes sense once an archive scan has stamped corpus_hits.
    return c.corpus_hits!=null && (c.count_applied||0)===0 && (c.corpus_hits||0)===0;
  }
  function corrMeta(c){
    var parts = ['&#215;'+(c.count_applied||0)];
    if(c.corpus_hits!=null) parts.push((c.corpus_hits||0)+' hits');
    if(c.source && c.source!=="manual") parts.push(esc(c.source));
    if(corrUnused(c)) parts.push('unused');
    return parts.join(' &middot; ');
  }
  function renderCorr(data){
    var el=document.getElementById("corrList"); if(!el) return;
    if(!data.length){ el.innerHTML='<div class="hint">No corrections yet.</div>'; return; }
    data.sort(function(a,b){ return (b.count_applied||0)-(a.count_applied||0)
      || (b.corpus_hits||0)-(a.corpus_hits||0)
      || String(a.wrong||"").localeCompare(String(b.wrong||"")); });
    el.innerHTML='<table class="tbl">'+data.map(function(c){
      var un=corrUnused(c);
      return '<tr'+(un?' class="corr-unused"':'')+'>'+
        '<td class="act"><input type="checkbox" data-ck="'+esc(c.wrong)+'"></td>'+
        '<td class="rtl-auto" dir="auto">'+esc(c.wrong)+' &#8594; '+esc(c.right)+
          ' <small class="muted">'+corrMeta(c)+'</small></td>'+
        '<td class="act"><button class="btn ghost sm" data-del-corr="'+esc(c.wrong)+'">&#128465;</button></td></tr>';
    }).join('')+'</table>';
  }
  function loadCorr(){ call("vocab_corrections_list",[]).then(function(r){ renderCorr(r.data||[]); }); }
  function loadSnips(){
    call("snippets_get",[]).then(function(r){
      var data=r.data||[]; var el=document.getElementById("snipList"); if(!el) return;
      el.innerHTML='<table class="tbl" id="snipTbl"></table>';
      if(!data.length){ addSnipRow("",""); } else { data.forEach(function(s){ addSnipRow(s.cue||"", s.text||""); }); }
    });
  }
  function addSnipRow(cue, text){
    var tb = document.getElementById("snipTbl");
    if(!tb){ var el=document.getElementById("snipList"); if(el){ el.innerHTML='<table class="tbl" id="snipTbl"></table>'; tb=document.getElementById("snipTbl"); } }
    if(!tb) return;
    var tr=document.createElement("tr");
    tr.innerHTML='<td style="width:35%"><input type="text" class="snip-cue" value="'+esc(cue)+'" placeholder="cue"></td>'+
      '<td><input type="text" class="snip-text rtl-auto" dir="auto" value="'+esc(text)+'" placeholder="expansion"></td>'+
      '<td class="act"><button class="btn ghost sm" data-del-snip="1">&#128465;</button></td>';
    tb.appendChild(tr);
  }
  function saveSnips(){
    var rows = Array.prototype.slice.call(document.querySelectorAll('#snipTbl tr'));
    var items = rows.map(function(tr){
      return { cue:(tr.querySelector('.snip-cue')||{}).value||"", text:(tr.querySelector('.snip-text')||{}).value||"" };
    });
    // re-load from the store after saving so the table reflects what's persisted
    // (and a later Save can't overwrite with a stale/empty table).
    call("snippets_set",[items]).then(function(){ loadSnips(); });
  }

  // ---------- boot ----------
  if(window.__LIA_DEMO__){
    // Static QA render (browser / no pywebview): render immediately.
    go(PAGE);
  } else {
    RK.ready(function(){
      try{ RK.api.mark_ready(); }catch(e){}
      if(FOCUS){ /* reserved: focus a specific control */ }
      go(PAGE);
    });
  }
})();
"""


if __name__ == "__main__":
    main()
