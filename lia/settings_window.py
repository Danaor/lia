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
        # Advanced > cloud summary-prompt addendum editor (placeholder text: the
        # real default lives in lia.py, which this child module must not import)
        "summary_short_prompt": False,
        "summary_addendum_default": ("כללים נוספים מחייבים:\n- (demo) כל עובדה קונקרטית "
                                     "נכנסת לתוך הבולט הרלוונטי.\n- (demo) 'דגשים מרכזיים' "
                                     "נשאר 3-6 בולטים."),
        "summary_base_prompt": ("You are an experienced project manager writing the final "
                                "Hebrew meeting summary. (demo placeholder for the full base "
                                "prompt - the real one is ~150 lines and lives in lia.py.)\n\n"
                                "## כותרת הדיון\n## תקציר\n## דגשים מרכזיים\n## משימות"),
        "summary_templates": [
            {"id": "technical", "name_en": "Technical / Project",
             "desc_en": "Project-manager notes: decisions, project status, work done, tasks with owners."},
            {"id": "general", "name_en": "General meeting",
             "desc_en": "A clean everyday recap: summary, key points, decisions, tasks, open questions."},
            {"id": "minutes", "name_en": "Detailed minutes",
             "desc_en": "Formal minutes: participants, topics, decisions with rationale, action items, next steps."},
        ],
        "config": {
            "speaker_self_name": "Dana",
            "hotkey": "ctrl+space", "recording_mode": "toggle",
            "paste_mode": "auto_paste", "clipboard_auto_restore": True,
            "press_enter_after_paste": False, "silent_mode": True,
            "beep_device_index": "off", "recording_source": "both",
            "input_device_index": 1, "loopback_device_index": None,
            "cleanup_style": "spoken", "auto_detect_meetings": True,
            "custom_vocabulary": "git, push, React, GuardDuty, Landing Zone",
            "vocab_autolearn": True, "meeting_model": "local_hebrew_turbo",
            "file_transcribe_model": "", "summary_model": "gpt-6.1-sol",
            "summary_template": "general", "whisper_device": "auto",
        },
        "secrets": {"openai_api_key": "sk-abc…7xQ", "groq_api_key": "",
                    "gemini_api_key": "AQ.xyz…9kk", "assemblyai_api_key": "",
                    "hf_token": "", "remote_server_token": ""},
        "has": {"openai_api_key": True, "groq_api_key": False,
                "gemini_api_key": True, "assemblyai_api_key": False,
                "hf_token": False, "remote_server_token": False},
        # the demo shows the red "rejected" state on the Gemini card
        "rejected": {"openai_api_key": False, "groq_api_key": False,
                     "gemini_api_key": True},
        # ... and a newer release (the Home + Advanced update bar)
        "update": {"available": True, "version": "1.6.6", "current": "1.6.5",
                   "kind": "installer", "url": "https://github.com/Danaor/lia/releases/latest",
                   "status": "idle", "progress": 0, "error": "", "auto": True},
        "status_line": "Ready", "model_loaded": True, "recording": False,
        "meeting_active": False, "live_transcript_available": False,
        "home": {"recording_source": "both", "mic_name": "JOUNIVO Mic",
                 "meeting_mic_name": "Jabra Headset", "action_items_open": 3,
                 "speaker_profiles": [
                     {"name": "דנה", "count": 3, "updated": "2026-09-14"},
                     {"name": "Avi Bar-Yuda", "count": 1, "updated": "2026-09-17"}],
                 "recent_meetings": [
                     {"title": "Meeting %d" % i,
                      "date": "2026-09-%02d %02d:%02d" % (16 - i // 3, 9 + i % 8, (i * 7) % 60),
                      "path": "m%d" % i, "has_summary": (i % 3 == 0)}
                     for i in range(24)]},
        "loopback_available": True, "whisper_device_label": "Auto",
        "cleanup_model_label": "gpt-6-luna", "cleanup_provider": "openai",
        "vocab_pending": 4, "auto_start": True,
        # the Languages controls (General) + summary-language choices (Meetings)
        "languages": {"primary": "he", "locked": False, "profile": "hebrew",
                      "model_lang_count": 99, "model_kind": "whisper",
                      "rows": [{"id": c, "name_en": e, "native": n, "rtl": c in ("he", "ar"),
                                "enabled": c in ("he", "en"), "summary_ok": c in ("he", "en", "es"),
                                "primary": c == "he", "supported": True}
                               for c, e, n in [("he", "Hebrew", "עברית"),
                                               ("en", "English", "English"),
                                               ("ar", "Arabic", "العربية"),
                                               ("de", "German", "Deutsch"),
                                               ("es", "Spanish", "Español"),
                                               ("fr", "French", "Français")]]},
        "vocab_learned_count": 112, "corrections_count": 37,
        "serve": {"enabled": True, "autostart": False, "running": True,
                  "port": 9090, "has_token": False,
                  "tailscale_ip": "100.100.100.100",
                  "ws_url": "ws://100.100.100.100:9090",
                  "host": "auto", "host_label": "Tailscale 100.100.100.100",
                  "insecure": False,
                  "role": "server", "model": "",
                  "gpu": {"has_cuda": True, "name": "NVIDIA GeForce RTX 3090",
                          "vram_gb": 24.0, "verdict": "good",
                          "note": "NVIDIA GeForce RTX 3090 · 24 GB VRAM - good "
                                  "for hosting a transcription server."}},
        "lexicon": {"installed": True, "loaded": True, "enabled": True,
                    "suggest": True, "size_mb": 5.7, "words": 341806},
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
            "dictation": [{"idx": 0, "label": "Whisper Hebrew Local", "checked": True, "where": "local", "wnote": "GPU (4 GB+) recommended · slower on CPU"},
                          {"idx": 1, "label": "Parakeet Multi-Language Local (25 languages)", "checked": False, "where": "local", "wnote": "fast on a plain CPU · no GPU needed"},
                          {"idx": 4, "label": "OpenAI GPT transcribe", "checked": False, "where": "cloud", "wnote": "API key set · ~$0.27 per audio hour"},
                          {"idx": 6, "label": "Groq Multi-Language", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · Free Tier Available"},
                          {"idx": 7, "label": "Gemini 3.5 transcribe Multi-Language", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · Free Tier Available · ~3.6s, slower than Groq"},
                          {"idx": 8, "label": "Remote Transcription server", "checked": False, "where": "remote"}],
            "meeting": [{"key": "local_hebrew_turbo", "label": "Whisper Hebrew Local", "checked": True, "enabled": True, "where": "local", "wnote": "GPU (4 GB+) recommended · CPU: ready ~5 min after a 1h meeting", "note": ""},
                        {"key": "local_pyannote_hebrew", "label": "Whisper Hebrew Local + Pyannote Diarization", "checked": False, "enabled": True, "where": "local", "wnote": "GPU (6 GB+) strongly recommended · CPU: ~90 min per meeting hour", "note": ""},
                        {"key": "local_parakeet_english", "label": "Parakeet Multi-Language Local (25 languages)", "checked": False, "enabled": True, "where": "local", "wnote": "fast on a plain CPU · no GPU needed", "note": ""},
                        {"key": "local_pyannote_parakeet", "label": "Parakeet Multi-Language Local + Pyannote Diarization", "checked": False, "enabled": True, "where": "local", "wnote": "GPU (6 GB+) strongly recommended · CPU: ~90 min per meeting hour", "note": ""},
                        {"key": "local_multilang_turbo", "label": "Whisper Multi-Language Local (99 languages)", "checked": False, "enabled": True, "where": "local", "wnote": "GPU (4 GB+) recommended · CPU: ready ~5 min after a 1h meeting", "note": ""},
                        {"key": "local_pyannote_multilang", "label": "Whisper Multi-Language Local + Pyannote Diarization", "checked": False, "enabled": True, "where": "local", "wnote": "GPU (6 GB+) strongly recommended · CPU: ~90 min per meeting hour", "note": ""},
                        {"key": "gemini_transcribe", "label": "Gemini 3.5 transcribe Multi-Language", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · Free Tier Available", "note": "needs gemini_api_key"},
                        {"key": "openai_gpt_transcribe", "label": "OpenAI GPT transcribe", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · ~$0.27 per meeting hour", "note": "needs openai_api_key"}],
            "file": [{"key": "", "label": "Same as meeting model", "checked": True, "enabled": True, "note": ""}],
            "summary": [{"model": "off", "label": "Off - transcript only (no AI summary)", "checked": True, "enabled": True, "note": ""},
                        {"model": "gpt-6.1-sol", "label": "OpenAI ChatGPT 6.1 Sol", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · ~$0.05 per meeting summary", "note": ""},
                        {"model": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "checked": False, "enabled": False, "where": "cloud", "wnote": "Requires API key · Free Tier Available", "note": "set Gemini key"},
                        {"model": "gemma4:31b-it-qat", "label": "Gemma 4 31B QAT (best quality · needs a 24 GB GPU)", "checked": False, "enabled": False, "where": "local", "note": "start Ollama"}],
            "cleanup_styles": [{"style": "off", "label": "Off - raw transcription", "checked": False},
                               {"style": "spoken", "label": "Spoken - remove fillers + self-corrections", "checked": True},
                               {"style": "proofread", "label": "Proofread - full polish", "checked": False}],
            "cleanup_models": [{"provider": "gemini", "model": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite (free, fastest)", "checked": False},
                               {"provider": "openai", "model": "gpt-6-luna", "label": "OpenAI ChatGPT 6 Luna - light, fast", "checked": True}],
            "device": [{"key": "auto", "label": "Auto (GPU if available)", "checked": True},
                       {"key": "cuda", "label": "GPU (CUDA)", "checked": False},
                       {"key": "cpu", "label": "CPU (no GPU needed)", "checked": False}],
            "gpu_busy": [{"key": k, "label": l, "checked": k == "openai", "enabled": k != "groq"}
                         for k, l in (("openai", "OpenAI"), ("gemini", "Gemini"), ("groq", "Groq"),
                                      ("off", "Off (wait for the GPU)"))],
            "summary_gpu_busy": [{"key": k, "label": l, "checked": k == "off", "enabled": k != "groq"}
                                 for k, l in (("openai", "OpenAI"), ("gemini", "Gemini"), ("groq", "Groq"),
                                              ("off", "Off (wait for the GPU)"))],
            "cleanup_any_key": True,
        },
    }


def _demo_html(page="general"):
    st = _demo_state()
    st.setdefault("config", {})["ui_language"] = uk.ui_i18n.resolve()
    head = ("<script>window.__LIA_INIT__=%s;window.__LIA_PAGE__=%s;"
            "window.__LIA_FOCUS__='';window.__LIA_DEMO__=true;</script>" %
            (_js(st), _js(page)))
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
# Sidebar nav, grouped by task (page ids unchanged so deep links keep working).
# Each item is (page id, ui_kit icon name, label). Local SVG icons, not emoji.
_NAV_GROUPS = [
    ("Daily use", [
        ("general", "general", "General"),
        ("audio", "audio", "Audio"),
        ("meetings", "meetings", "Meetings"),
    ]),
    ("Transcription & text", [
        ("models", "models", "Models"),
        ("cleanup", "cleanup", "AI Cleanup"),
        ("vocab", "vocabulary", "Vocabulary"),
        ("snippets", "snippets", "Snippets"),
    ]),
    ("Connections & advanced", [
        ("keys", "keys", "API Keys"),
        ("server", "server", "Transcription server"),
        ("advanced", "advanced", "Advanced"),
    ]),
]


def _nav_button(page_id, icon_name, label):
    return ('<button class="nav-item" data-page="%s"><span class="ico">%s</span>%s</button>'
            % (page_id, uk.icon(icon_name, size=20), label))


def _nav_html():
    parts = [_nav_button("home", "home", "Home")]
    for group_label, items in _NAV_GROUPS:
        parts.append('<div class="nav-group">%s</div>' % uk._h(group_label))
        parts.extend(_nav_button(*it) for it in items)
    return "\n    ".join(parts)


BODY = """
<div class="shell">
  <nav class="sidebar">
    <div class="brand">
      <div class="brand-live" title="Lia" dir="ltr">__ORB__<img class="wm" src="__WORDMARK_SRC__" alt="Lia"></div>
      <div class="brand-state" aria-live="polite"><span class="rdot" id="brandRec" hidden></span><span class="bs-t" id="brandState">Ready</span></div>
    </div>
    <div class="nav-search">
      <span class="nav-search-ic">__SEARCH_ICON__</span>
      <input type="text" id="settingsSearch" placeholder="Search…" autocomplete="off" spellcheck="false" aria-label="Search settings">
      <span class="nav-search-k" aria-hidden="true">Ctrl K</span>
      <div id="searchResults" class="search-results" role="listbox" hidden></div>
    </div>
    __NAV__
  </nav>
  <main class="content" id="content"></main>
</div>
<div id="pal"></div>
"""
BODY = BODY.replace("__NAV__", _nav_html())
BODY = BODY.replace("__SEARCH_ICON__", uk.icon("search", size=15))
# The brand: Lia's live orb (ui_kit.orb_svg - it breathes, turns while she
# works, gathers from three petals when the window is first seen) beside the
# 'lia' wordmark (CSP-safe data: URI; "" if the asset is missing).
BODY = BODY.replace("__ORB__", uk.orb_svg("", id_="brandOrb"))
BODY = BODY.replace("__WORDMARK_SRC__", uk.wordmark_data_uri())

EXTRA_CSS = """
/* Maximized / wide window: center the WHOLE app (sidebar + content) as one
   capped block. SETTINGS pages keep a readable form width, centered in the work
   area; the HOME page (data-dense: meetings list + preview) uses the full width
   so a maximized window is put to work instead of stretched into empty gutters.
   The sidebar stays edge-anchored (fixed width, first in the flex row). */
/* 928 = rows of ~880 px + padding (Naor 2026-10-05: on a wide window keep a
   setting's label close to its control). */
.content:not(.home-mode){max-width:928px; margin-inline:auto;}
/* the status pill floats at the right end of the title row (it had its own line) */
/* API Keys rows: a small provider badge before the name */
.kname{display:inline-flex; align-items:center; gap:10px;}
.kbdg{width:26px; height:26px; border-radius:8px; display:inline-flex; align-items:center;
  justify-content:center; font-weight:700; font-size:12.5px; flex:0 0 26px;
  background:var(--sunk); color:var(--ink-2);}
.kbdg.img{background:#fff; padding:4px; box-shadow:inset 0 0 0 1px rgba(23,23,40,.10),0 1px 2px rgba(23,23,40,.05);}
.kbdg.img img{width:100%; height:100%; object-fit:contain; display:block;}
.srow-c .masked{font-size:12px;}
.keybad{margin:0 0 8px; font-size:var(--fs-hint); color:var(--warn); font-weight:600;}
/* segmented control made of buttons (Transcription server: Client / Server) */
.seg button.radio{font:inherit; cursor:pointer; color:var(--ink-2);}
/* server status LED inside a row title */
.run-led-sm{display:inline-block; width:9px; height:9px; border-radius:50%; margin-inline-end:8px;
  background:var(--faint); vertical-align:middle;}
.run-led-sm.on{background:var(--ok); box-shadow:0 0 0 3px var(--ok-soft);}
.run-led-sm.starting{background:var(--warn); box-shadow:0 0 0 3px var(--warn-soft);}
.srow-c .gpu-tag{margin-inline-start:0;}
/* The status lives in the sidebar now (under Lia's orb). The pill by the
   settings only appears for a moment to confirm a save ("in place"). */
.hdr-pill{position:sticky; top:-20px; height:0; z-index:4; text-align:end; pointer-events:none;}
.hdr-pill .status{pointer-events:auto; position:relative; top:8px; background:var(--card);
  padding:4px 12px; border-radius:var(--r-pill); box-shadow:var(--float);
  opacity:0; transform:translateY(-4px); transition:opacity .22s var(--ease), transform .22s var(--ease);}
.hdr-pill .status.flash{opacity:1; transform:none;}
.content.home-mode{max-width:none; margin-inline:0;
  padding:16px 22px; display:flex; flex-direction:column; gap:12px; overflow:hidden;}

/* --- Home: the greeting row (greeting + overflow menu) --- */
.home-head{display:flex; align-items:center; gap:12px; flex-wrap:wrap; min-height:40px;}
.home-head h1{margin:0; font-size:var(--fs-title); font-weight:750; letter-spacing:-.6px;
  line-height:1.15; color:var(--ink);}
.home-head h1 .nm{background:linear-gradient(120deg,#6A5BE2,#4C8DF0); -webkit-background-clip:text;
  background-clip:text; color:transparent; unicode-bidi:isolate;}
.home-head .hh-actions{margin-inline-start:auto; display:flex; gap:8px;
  align-items:center; flex-wrap:wrap;}
.home-menu{position:relative;}
.home-menu-pop{position:absolute; inset-inline-end:0; top:calc(100% + 6px); z-index:20;
  background:var(--card); border:1px solid var(--line); border-radius:var(--r-m);
  box-shadow:var(--float); min-width:230px; padding:6px; display:none;}
.home-menu.open .home-menu-pop{display:block; animation:pop .2s var(--ease);}
.home-menu-pop button{display:flex; align-items:center; gap:9px; width:100%;
  text-align:start; background:none; border:0; color:var(--ink); cursor:pointer;
  font:inherit; padding:8px 10px; border-radius:var(--r-s);}
.home-menu-pop button:hover{background:var(--card-2);}
.home-menu-pop button .ico-svg{color:var(--muted);}
/* one quiet line of facts (no boxes): mic, meeting mic, source, hotkey */
.home-info{display:flex; flex-wrap:wrap; gap:6px 18px; align-items:center; color:var(--muted);
  font-size:13px;}
.home-actions{display:flex; flex-wrap:wrap; gap:10px; align-items:center;}
/* a meeting is recording: a calm card with the red dot and Lia listening */
.home-live{display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  padding:12px 14px 12px 16px; border:1px solid rgba(214,53,67,.18); border-radius:var(--r-l);
  background:var(--card); box-shadow:var(--shadow);}
.home-live .lv-t{display:flex; align-items:center; gap:10px; font-weight:700;}
.home-live .lv-t .rdot{width:9px; height:9px; border-radius:50%; background:var(--rec);
  animation:recPing 1.6667s steps(16) infinite; animation-delay:var(--d,0s);}
/* a newer Lia release is available (Home + Advanced > About): a slim, quiet bar */
.upd-bar{display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:0 0 12px;
  padding:7px 8px 7px 14px; border:1px solid var(--lia-line); border-radius:var(--r-pill);
  background:var(--lia-soft); color:var(--ink-2); font-size:13px;}
.upd-bar > .ico-svg{color:var(--lia-ink);}
.upd-bar .grow{flex:1 1 auto;}
.upd-bar .btn{padding:5px 14px; font-size:12.5px; border-radius:var(--r-pill);}
.upd-bar a{font-size:12.5px; padding-inline-end:6px;}
.upd-bar .upd-err{color:var(--warn); font-weight:600;}
.gbody > .upd-bar:only-child,.gbody > .upd-bar:last-child{margin-bottom:0;}
.hchip{display:inline-flex; align-items:center; gap:6px; white-space:nowrap;}
.hchip .ico-svg{color:var(--faint);}
.hchip b{font-weight:500; color:var(--ink-2);}

/* --- Home: meetings area fills the remaining height --- */
.home-body{flex:1 1 auto; min-height:0; display:flex; gap:16px;}
.mtg-list{display:flex; flex-direction:column; min-height:0; flex:1 1 44%;
  border:1px solid var(--line); border-radius:var(--r-l); background:var(--card);
  overflow:hidden; box-shadow:var(--shadow);}
.mtg-listhead{display:flex; align-items:center; gap:10px; padding:10px 12px;
  border-bottom:1px solid var(--line);}
.mtg-search{flex:1 1 auto; position:relative; display:flex; align-items:center;}
.mtg-search .ic,.mtg-search > .ico-svg{position:absolute; inset-inline-start:10px; color:var(--faint);
  display:inline-flex; pointer-events:none;}
.mtg-search input{width:100%; padding:7px 10px 7px 30px; border:1px solid transparent;
  border-radius:var(--r-s); background:var(--sunk); color:var(--ink); font:inherit;}
.mtg-search input:focus{outline:none; border-color:rgba(106,91,226,.55); box-shadow:var(--ring); background:#fff;}
.mtg-count{color:var(--faint); font-size:12px; flex:0 0 auto; font-variant-numeric:tabular-nums;
  padding-inline-end:4px;}
.mtg-rows{flex:1 1 auto; overflow-y:auto; min-height:0; padding:6px;}
/* Vocabulary: learned-term grid + tools, suggestion rows (2026-10-05) */
.vtools{display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:10px 0 6px;}
.vtools input[type=text]{flex:1 1 180px; min-width:140px; width:auto;}
.vtools select{flex:0 0 auto; width:auto;}
.vgrid{display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:0 14px;}
.vgrid .check{padding:3px 0;}
.sugg{padding:8px 0; border-bottom:1px solid var(--line);}
.sugg:last-child{border-bottom:0;}
.sugg-sample{color:var(--muted); font-size:var(--fs-hint); margin:2px 0 0 30px;}
.sugg-fix{display:block; width:100%; max-width:380px; margin:6px 0 0 30px; box-sizing:border-box;}
.drop-row{display:flex; gap:10px; align-items:center; padding:4px 0;}
.drop-row .txt{flex:1;}
/* a meeting row: a date tile, the name (+ Lia's dot when it has a summary),
   when it was (in words; the full date on hover), and an arrow on hover */
.mtg-row{display:grid; grid-template-columns:auto 1fr auto; grid-template-rows:auto auto;
  gap:1px 12px; align-items:center; width:100%; text-align:start; cursor:pointer; font:inherit;
  color:var(--ink); background:none; border:0; border-radius:12px; position:relative;
  padding:8px 10px; margin-bottom:2px; transition:background var(--tr);}
.mtg-row:hover{background:var(--card-2);}
.mtg-row.sel{background:var(--lia-soft);}
.mtg-row .rtile{grid-row:1 / 3; width:38px; height:38px; border-radius:11px; background:var(--sunk);
  display:flex; flex-direction:column; align-items:center; justify-content:center; line-height:1;
  font-variant-numeric:tabular-nums; color:var(--ink-2);}
.mtg-row.sel .rtile{background:#fff; box-shadow:0 1px 2px rgba(23,23,40,.06);}
.mtg-row .rtile .d{font-size:15px; font-weight:700;}
.mtg-row .rtile .m{font-size:9.5px; font-weight:700; letter-spacing:.06em; color:var(--faint);
  text-transform:uppercase; margin-top:2px;}
.mtg-row .rn{font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0;}
.mtg-row .rgo{grid-row:1 / 3; grid-column:3; color:var(--faint); opacity:0; transform:translateX(-4px);
  transition:opacity var(--tr), transform var(--tr); display:inline-flex;}
.mtg-row:hover .rgo,.mtg-row.sel .rgo{opacity:1; transform:none;}
.mtg-row.sel .rgo{color:var(--lia-ink);}
.mtg-row .rmeta{grid-column:2; display:flex; align-items:center; gap:8px; min-width:0;
  color:var(--muted); font-size:12px;}
.mtg-row .rtag{width:7px; height:7px; border-radius:50%; background:var(--lia-grad); flex:0 0 7px;
  display:inline-block; vertical-align:middle; margin-inline-start:7px;}
.mtg-row .rdate{font-variant-numeric:tabular-nums;}
.mtg-row.born{border-radius:12px;}
.mtg-empty{color:var(--muted); padding:18px 12px; font-size:var(--fs-hint);}
/* first run: no meetings at all - a calm centred welcome, Lia's orb resting */
.mtg-empty.first{display:flex; flex-direction:column; align-items:center; justify-content:center;
  gap:6px; text-align:center; padding:56px 24px; min-height:240px;}
.mtg-empty.first .orb{width:44px; height:44px; margin-bottom:8px; opacity:.9;}
.mtg-empty.first b{color:var(--ink); font-size:var(--fs-body); font-weight:600;}

/* --- Home: preview pane (selected meeting) --- */
.mtg-preview{flex:1 1 56%; min-height:0; overflow-y:auto;
  border:1px solid var(--line); border-radius:var(--r-l); background:var(--card);
  padding:20px 22px; box-shadow:var(--shadow);}
.mtg-preview .pv-title{font-size:20px; font-weight:750; letter-spacing:-.3px; margin:0 0 2px;
  line-height:1.25;}
.mtg-preview .pv-sub{color:var(--muted); font-size:12.5px; margin-bottom:14px;
  font-variant-numeric:tabular-nums;}
.mtg-preview .pv-actions{display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px;
  padding-bottom:16px; border-bottom:1px solid var(--line);}
.mtg-preview .pv-actions .btn{padding:6px 12px; font-size:12.5px;}
.mtg-preview .pv-body{white-space:pre-wrap; line-height:1.65; color:var(--ink-2);
  font-size:var(--fs-base);}
.mtg-preview .pv-none{color:var(--muted);}
/* the summary as a document */
.pv-doc{white-space:normal !important;}
.pv-doc h4{margin:16px 0 6px; font-size:13px; font-weight:700; color:var(--ink); letter-spacing:.1px;
  unicode-bidi:plaintext; text-align:start;}
.pv-doc h4:first-child{margin-top:0;}
.pv-doc p{margin:0 0 8px; unicode-bidi:plaintext; text-align:start;}
.pv-doc ul{margin:0 0 8px; padding:0; list-style:none;}
.pv-doc li{position:relative; padding-inline-start:16px; margin:0 0 5px; unicode-bidi:plaintext; text-align:start;}
.pv-doc li::before{content:""; position:absolute; inset-inline-start:3px; top:.62em; width:5px; height:5px;
  border-radius:50%; background:var(--faint);}
.pv-doc li.task{padding-inline-start:24px;}
.pv-doc li.task::before{display:none;}
.pv-doc li.task .tb{position:absolute; inset-inline-start:0; top:.25em; width:15px; height:15px; border-radius:5px;
  border:1.5px solid var(--line-2); background:var(--card);}
.pv-doc li.task.done{color:var(--muted); text-decoration:line-through; text-decoration-color:var(--faint);}
.pv-doc li.task.done .tb{background:var(--ok); border-color:var(--ok);}
.mtg-preview.empty{display:flex; align-items:center; justify-content:center;
  color:var(--faint); text-align:center;}
/* Normal / narrow window: one column, the preview drops (selecting opens the
   editor instead). Wide window: list + preview side by side. */
@media (max-width: 980px){
  .home-body{flex-direction:column;}
  .mtg-preview,.mtg-preview.empty{display:none;}
  .mtg-list{flex:1 1 auto;}
}
.kv{display:flex; gap:8px; flex-wrap:wrap; font-size:var(--fs-hint); color:var(--muted);}
.kv .k{color:var(--ink-2); font-weight:600;}
/* Keys & Server page: each service is a standalone card on the page ground
   (the wrapping .page frame is dropped via .keys-page) with subtle depth. */
.page.keys-page{background:transparent; border:none; box-shadow:none; padding:0;}
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
.rowbtn:hover{background:var(--card-2); border-color:var(--line-2);}
.rowbtn .ic{display:inline-flex; align-items:center; justify-content:center; width:20px; flex:0 0 20px; color:var(--muted);}
.rowbtn:hover .ic{color:var(--lia-ink);}

/* --- settings search (sidebar) ----------------------------------------- */
.nav-search{position:relative; margin:2px 6px 10px; display:flex; align-items:center;}
.nav-search-ic{position:absolute; inset-inline-start:10px; display:inline-flex; color:var(--faint); pointer-events:none;}
.nav-search input{width:100%; padding:8px 52px 8px 31px; border:1px solid var(--line);
  border-radius:var(--r-s); background:var(--card); color:var(--ink); font-size:var(--fs-hint);
  box-shadow:0 1px 2px rgba(23,23,40,.04);}
.nav-search input:focus{outline:none; border-color:rgba(106,91,226,.55); box-shadow:var(--ring);}
.nav-search-k{position:absolute; inset-inline-end:7px; pointer-events:none; font-family:var(--mono);
  font-size:10.5px; color:var(--faint); border:1px solid var(--line-2); border-bottom-width:2px;
  border-radius:5px; padding:0 5px; background:var(--card-2);}
.nav-search input:focus ~ .nav-search-k{opacity:0;}
.search-results{position:absolute; top:calc(100% + 6px); inset-inline:0; z-index:20;
  background:var(--card); border:1px solid var(--line); border-radius:var(--r-m);
  box-shadow:var(--float); max-height:320px; overflow:auto; padding:4px; width:300px;}
.search-results .sr{display:block; width:100%; text-align:start; border:0; background:transparent;
  cursor:pointer; padding:7px 9px; border-radius:var(--r-s); color:var(--ink); font-family:inherit; font-size:var(--fs-hint);}
.search-results .sr:hover,.search-results .sr.hi{background:var(--lia-soft);}
.search-results .sr .sr-page{display:block; font-size:var(--fs-small); color:var(--muted); margin-top:1px;}
.search-results .sr-none{padding:10px 9px; color:var(--muted); font-size:var(--fs-hint); text-align:center;}
/* flash a jumped-to setting so the eye lands on it */
@keyframes settingFlash{0%{background:var(--lia-soft);}100%{background:transparent;}}
.setting-flash{animation:settingFlash 1.3s ease-out;}


/* --- Home landing page -------------------------------------------------- */
.hchips{display:flex; flex-wrap:wrap; gap:8px;}
.home-hero{display:flex; flex-direction:column; align-items:flex-start; gap:10px;}
.home-hero.live{border-color:var(--lia-line);}
.home-hero .hero-title{font-size:var(--fs-section); font-weight:700; display:flex; align-items:center; gap:8px;}
.btn.big{font-size:15px; padding:11px 20px; display:inline-flex; align-items:center; gap:9px;}
.home-acts{display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px;}
.home-act{appearance:none; cursor:pointer; font-family:inherit; text-align:start;
  display:flex; align-items:center; gap:10px; padding:12px 14px; border:1px solid var(--line);
  border-radius:var(--r-m); background:var(--card); color:var(--ink); font-size:var(--fs-base); font-weight:500;
  transition:border-color var(--tr),background var(--tr);}
.home-act:hover{border-color:var(--line-2); background:var(--card-2);}
.home-act:focus-visible{outline:none; box-shadow:var(--ring);}
.home-act .ha-ic{color:var(--muted); display:inline-flex;}
.home-act:hover .ha-ic{color:var(--lia-ink);}
.recent-row{appearance:none; cursor:pointer; font-family:inherit; width:100%; text-align:start;
  display:flex; align-items:center; gap:10px; padding:9px 12px; border:1px solid var(--line);
  border-radius:var(--r-s); background:var(--card); color:var(--ink); margin-top:8px; font-size:var(--fs-base);
  transition:border-color var(--tr),background var(--tr);}
.recent-row:hover{border-color:var(--line-2); background:var(--card-2);}
.recent-row:focus-visible{outline:none; box-shadow:var(--ring);}
.recent-row .rr-ic{color:var(--muted); display:inline-flex; flex:0 0 auto;}
.recent-row .rr-title{flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.recent-row .rr-tag{font-size:var(--fs-small); font-weight:700; color:var(--lia-ink); background:var(--lia-soft);
  border-radius:var(--r-pill); padding:1px 8px; flex:0 0 auto;}
.recent-row .rr-date{font-size:var(--fs-small); color:var(--muted); font-variant-numeric:tabular-nums; flex:0 0 auto;}
/* Recent meetings: a scroll area so the last ~30 are reachable without the Home
   page growing unbounded. Sized to ~7 rows; the rest scroll. A little end
   padding keeps the scrollbar off the row borders. */
.recent-list{max-height:360px; overflow-y:auto; padding-inline-end:4px;}
.recent-list .recent-row:first-child{margin-top:0;}
.rowbtn .hk{margin-inline-start:auto; color:var(--muted); font-size:var(--fs-hint);}
/* A badge (e.g. BETA) takes over the right-push; the hotkey trails it. */
.rowbtn .wb{margin-inline-start:auto;}
.rowbtn .wb ~ .hk{margin-inline-start:0;}
.disabledrow{opacity:.5;}
.subhead{font-size:13px; font-weight:600; margin:14px 0 2px;}
/* Small provider logo before a model label (OpenAI / Gemini rows). */
.radio .txt .prov-ico{width:15px;height:15px;object-fit:contain;
  vertical-align:-3px;margin-inline-end:7px;border-radius:3px;}
"""

APP_JS = r"""
(function(){
  var S = window.__LIA_INIT__ || {};
  var PAGE = window.__LIA_PAGE__ || "home";
  var FOCUS = window.__LIA_FOCUS__ || "";
  // Provider/model logos (inlined data URIs; "" -> the placeholder stays, guarded below).
  var PROV_LOGO = {openai:"__OPENAI_LOGO__", gemini:"__GEMINI_LOGO__",
                   groq:"__GROQ_LOGO__", parakeet:"__PARAKEET_LOGO__",
                   whisper:"__WHISPER_LOGO__"};
  function provIcon(label){
    var l = (label||"").toLowerCase();
    var key = l.indexOf("gemini")>=0 ? "gemini"
            : l.indexOf("openai")>=0 ? "openai"
            : l.indexOf("parakeet")>=0 ? "parakeet"
            : l.indexOf("groq")>=0 ? "groq"
            : (l.indexOf("whisper")>=0 ? "whisper" : "");
    var src = key && PROV_LOGO[key];
    if(!src || src.indexOf("data:")!==0) return "";
    return '<img class="prov-ico" src="'+src+'" alt="">';
  }
  var DRAFT = {};            // text fields that must survive re-render, by id
  var pending = {};          // call id -> {resolve}
  var nextId = 1;
  var homeSearch = "";       // Home: meetings search query (survives re-render)
  var homeSel = null;        // Home: selected meeting path (drives the preview)
  var $ = RK.$;

  function esc(s){ return RK.esc(s); }
  function cfg(k, d){ var c = S.config || {}; return (k in c) ? c[k] : d; }
  function draftOr(id, val){ return (id in DRAFT) ? DRAFT[id] : val; }

  function call(method, args, slow){
    if(window.__LIA_DEMO__) return demoCall(method, args||[]);
    var id = nextId++;
    RK.api && RK.api.emit({t:"call", id:id, method:method, args:args||[]});
    return new Promise(function(res){ pending[id] = {res:res, slow:!!slow}; });
  }
  // Offline demo (ui_kit_gallery / _demo_html): resolve a few reads with fakes so
  // the layout is previewable without the parent process.
  function demoCall(method, args){
    // demo/QA only: a log of every call, so a click test can check the wiring
    (window.__calls = window.__calls || []).push([method, args]);
    return new Promise(function(res){ setTimeout(function(){
      if(method==="get_meeting_details"){
        res({ok:true, data:{has_summary:true, diarized:true, date:"2026-09-14 16:07",
          summary_text:"## תקציר\nפגישת דמו על מדיניות ענן.\n\n## דגשים\n- נקודה ראשונה לדוגמה\n- נקודה שנייה\n\n## משימות\n- [ ] לסיים את המסמך"}});
        return; }
      if(method==="vocab_pending_list"){
        res({ok:true, data:[{term:"Ristructure", count_corpus:3, sample:"שעשינו Domain Ristructure בחודש"},
          {term:"Terraform", count_corpus:5, sample:"הגדרנו את זה ב-Terraform"}]}); return; }
      if(method==="vocab_learned_list"){
        res({ok:true, data:[{term:"API",count_used:100},{term:"Cloud",count_used:60},
          {term:"account",count_used:40},{term:"Entra ID",count_used:15},{term:"Security",count_used:22},
          {term:"Walkload",count_used:0},{term:"GitLab",count_used:14},{term:"monthly",count_used:0}]}); return; }
      if(method==="vocab_dropped_list"){
        res({ok:true, data:[{term:"Ristructure", count_corpus:3, fixed_to:"Restructure"},
          {term:"Lendding", count_corpus:4}]}); return; }
      res({ok:true, msg:""});
    }, 60); });
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
  var WB_TEXT = {local:'LOCAL', cloud:'CLOUD',
                 remote:'REMOTE LOCAL GPU'};
  var WB_SUB = {remote:'Use a GPU on another PC in your local network'};
  function radio(name, method, arg, argtype, label, checked, enabled, note, where, wnote, noicon){
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
      '<span class="box"></span><span class="txt">'+(noicon?'':provIcon(label))+esc(label)+
      (note?'<small>'+esc(note)+'</small>':'')+'</span>'+badge+'</label>';
  }
  function btn(label, method, args, kind, slow){
    return '<button class="btn '+(kind||'')+'" data-call="'+esc(method)+'" '+
      'data-args=\''+esc(JSON.stringify(args||[]))+'\''+(slow?' data-slow="1"':'')+
      '>'+esc(label)+'</button>';
  }

  // ---------- compact rows (2026-10-05 redesign) ----------
  // One card per TOPIC (grp), one ROW per setting (srow): title + a one-line
  // description on the left, the control on the right. Short enums are a
  // segmented control (seg, built from radio() so the data-radio wiring is the
  // same); long lists collapse into more() whose summary shows the current
  // value. OPENX keeps a picker open across re-renders (a state push redraws
  // the page). ONOPEN[k] fills picker k's list when it opens and again after a
  // re-render while it is open; ONPAGE[page] does the same for a page. Each
  // hook loads only when its list is still empty (render() carries a loaded
  // list over, with whatever the user typed or ticked in it).
  var OPENX = {};
  var ONOPEN = {};
  var ONPAGE = {};
  function isEmpty(id){ var el = $(id); return !!el && !el.childNodes.length; }
  document.addEventListener('toggle', function(e){
    var d = e.target;
    if(!(d && d.matches && d.matches('details[data-k]'))) return;
    var k = d.getAttribute('data-k');
    OPENX[k] = d.open;
    // render() already ran the hook for a picker it drew open; Chromium then
    // fires a toggle for it too, which would load the list a second time
    if(d.open && ONOPEN[k] && !d.__hooked){ d.__hooked = true; try{ ONOPEN[k](); }catch(_){} }
  }, true);
  function opn(k, dflt){ return ((k in OPENX) ? OPENX[k] : !!dflt) ? ' open' : ''; }
  function grp(title, rows, opts){
    opts = opts || {};
    return '<div class="grp-h">'+title+
        (opts.sub?' <span class="grp-sub">'+opts.sub+'</span>':'')+
        (opts.action?'<span class="grp-act">'+opts.action+'</span>':'')+'</div>'+
      '<div class="grp'+(opts.tone?' '+opts.tone:'')+'"'+(opts.id?' id="'+opts.id+'"':'')+'>'+rows+'</div>';
  }
  function srow(title, desc, control, cls){
    return '<div class="srow'+(cls?' '+cls:'')+'"><div class="srow-l"><div class="srow-t">'+title+'</div>'+
      (desc?'<div class="srow-d">'+desc+'</div>':'')+'</div>'+
      '<div class="srow-c">'+(control||'')+'</div></div>';
  }
  function swb(checked, method, disabled){
    return '<label class="switch bare'+(disabled?' disabled':'')+'"><input type="checkbox" data-toggle="'+esc(method)+'"'+
      (checked?' checked':'')+(disabled?' disabled':'')+'><span class="track"></span></label>';
  }
  // opts: [[arg, label, checked, enabled], ...]
  function seg(name, method, argtype, opts){
    return '<div class="seg">'+opts.map(function(o){
      return radio(name, method, o[0], argtype, o[1], !!o[2], o[3]!==false, "", "", "", true);
    }).join('')+'</div>';
  }
  function more(k, title, desc, cur, body, dfltOpen){
    return '<details class="more" data-k="'+esc(k)+'"'+opn(k, dfltOpen)+'>'+
      '<summary class="srow"><div class="srow-l"><div class="srow-t">'+title+'</div>'+
      (desc?'<div class="srow-d">'+desc+'</div>':'')+'</div>'+
      '<div class="srow-c">'+(cur||'')+'<span class="chev">&#8250;</span></div></summary>'+
      '<div class="more-body">'+body+'</div></details>';
  }
  function arow(iconName, label, method, hkstr, beta, cls){
    return '<button class="arow'+(cls?' '+cls:'')+'" data-call="'+esc(method)+'" data-args="[]">'+
      '<span class="ic">'+RK.icon(iconName,{size:18})+'</span><span class="grow">'+esc(label)+'</span>'+
      (beta?'<span class="wb beta">BETA</span>':'')+
      (hkstr?'<span class="hk">'+esc(hkstr)+'</span>':'')+'</button>';
  }
  // a current-value list of names (mixed Hebrew/English) that keeps the list
  // order left-to-right while each name picks its own direction
  function curList(names, cls){
    return '<span class="cur'+(cls?' '+cls:'')+'" dir="ltr">'+names.map(function(n){
      return '<span dir="auto">'+esc(n)+'</span>';
    }).join(' &#183; ')+'</span>';
  }

  // A newer Lia release (S.update, from the parent's GitHub check): one bar,
  // shown on Home and on Advanced > About. Installed copy -> "Update now"
  // (download + verify + silent install + restart); portable -> "Download".
  function updateBar(){
    var u = S.update || {};
    if(!u.available) return '';
    var txt, acts = '';
    if(u.status === 'downloading'){
      txt = 'Downloading Lia '+esc(u.version)+'… '+(u.progress||0)+'%';
    } else if(u.status === 'installing'){
      txt = 'Installing Lia '+esc(u.version)+' - Lia restarts in a moment';
    } else {
      txt = T('<b>Lia '+esc(u.version)+' is available</b> (you have '+esc(u.current)+')');
      acts = btn(u.self_update ? 'Update now' : 'Download', 'update_now', [], 'primary', true);
    }
    return '<div class="upd-bar">'+RK.icon('download',{size:16})+'<span class="grow">'+txt+
      (u.error ? ' <span class="upd-err">'+esc(u.error)+'</span>' : '')+'</span>'+acts+
      (u.url ? '<a href="#" data-openurl="'+esc(u.url)+'">What’s new ↗</a>' : '')+'</div>';
  }

  // ---------- page renderers ----------
  var PAGES = {};

  // Home page state: the meetings search query + the selected meeting path
  // (survive re-renders; the preview loads that meeting's summary on the side).
  function homeMeetings(){
    var h = S.home||{};
    if(h.select){ homeSel = h.select; h.select = null; }   // a meeting just renamed
    return h.recent_meetings||[];
  }
  function homeFiltered(){
    var q = (homeSearch||'').toLowerCase();
    return homeMeetings().filter(function(m){
      return !q || (m.title||'').toLowerCase().indexOf(q)>=0; });
  }
  function homeIsNarrow(){ return window.matchMedia('(max-width: 980px)').matches; }

  // The greeting: by the hour, with the name from Settings > Meetings > Your name.
  function greeting(){
    var hr = new Date().getHours();
    var g = hr < 5 ? 'Good evening' : hr < 12 ? 'Good morning' : hr < 18 ? 'Good afternoon' : 'Good evening';
    var nm = (cfg("speaker_self_name","")||"").trim();
    return esc(g) + (nm ? ', <span class="nm" dir="auto">'+esc(nm)+'</span>' : '');
  }
  // "2026-09-16 09:00" -> a Date (local), or null
  function parseWhen(s){
    var m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/.exec(s||"");
    return m ? new Date(+m[1], +m[2]-1, +m[3], +(m[4]||0), +(m[5]||0)) : null;
  }
  var MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  var WDAY = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
  // when it was, in words: "Today, 09:32" / "Yesterday, 14:01" / "Monday, 10:00" / "16 Sep" / "16 Sep 2025"
  function whenWords(s){
    var d = parseWhen(s); if(!d) return esc(s||"");
    var now = new Date(), day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var dd = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    var days = Math.round((day0 - dd) / 86400000);
    var hm = ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);
    if(days === 0) return 'Today, '+hm;
    if(days === 1) return 'Yesterday, '+hm;
    if(days > 1 && days < 7) return WDAY[d.getDay()]+', '+hm;
    return WDAY[d.getDay()].slice(0,3)+', '+hm+(d.getFullYear()!==now.getFullYear() ? ' · '+d.getFullYear() : '');
  }
  // the full form for the preview: "Wednesday, 16 Sep 2026 · 09:00"
  function whenFull(s){
    var d = parseWhen(s); if(!d) return esc(s||"");
    if(window.LIA_I18N && LIA_I18N.rtl){
      try{ return esc(d.toLocaleDateString('he-IL', {weekday:'long', day:'numeric', month:'long', year:'numeric'})) +
        ' · ' + ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2); }catch(_){}
    }
    return WDAY[d.getDay()]+', '+d.getDate()+' '+MON[d.getMonth()]+' '+d.getFullYear()+
      ' · '+('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);
  }
  // A meeting that appears while this window is open is "born" (once): the
  // first list this window sees only marks what is there.
  var MTG_SEEN = null, MTG_BORN = {};
  function markMeetingsSeen(list){
    if(MTG_SEEN === null){ MTG_SEEN = {}; (list||[]).forEach(function(m){ MTG_SEEN[m.path] = 1; }); return; }
    (list||[]).forEach(function(m){ if(RK.once(MTG_SEEN, m.path) && !RK.reduce) MTG_BORN[m.path] = Date.now(); });
  }
  var MTG_ROSE = false;
  function mtgRow(m, i, noRise){
    var d = parseWhen(m.date), cls = 'mtg-row'+(m.path===homeSel?' sel':''), st = '';
    var bornAt = MTG_BORN[m.path] ? Date.now() - MTG_BORN[m.path] : -1;
    if(bornAt >= 0 && bornAt < 2600){ cls += ' born'; st = ' style="--bd:-'+bornAt+'ms"'; }
    else if(!MTG_ROSE && !noRise && !RK.reduce && i < 14){ cls += ' rise'; st = ' style="--rd:'+(i*30)+'ms"'; }
    var tile = d ? '<span class="rtile" aria-hidden="true"><span class="d">'+d.getDate()+'</span><span class="m">'+MON[d.getMonth()]+'</span></span>'
                 : '<span class="rtile" aria-hidden="true">'+RK.icon('meetings',{size:16})+'</span>';
    return '<button class="'+cls+'"'+st+' data-mtg="'+esc(m.path)+'">'+tile+
      '<span class="rn rtl-auto" dir="auto">'+esc(m.title)+
        (m.has_summary?'<span class="rtag" title="Has a summary"></span>':'')+'</span>'+
      '<span class="rgo" aria-hidden="true">'+RK.icon('chevron-right',{size:16})+'</span>'+
      '<span class="rmeta"><span class="rdate" title="'+esc(m.date)+'">'+whenWords(m.date)+'</span>'+
        (m.diarized?'<span>· speakers</span>':'')+'</span>'+
    '</button>';
  }

  PAGES.home = function(){
    var h = S.home || {}, hk = S.hotkeys || {};
    var active = !!S.meeting_active;

    // --- compact header: title + status + overflow menu (primary actions sit
    //     UNDER the info line, per Naor 2026-09-16) ---
    var menu = '<div class="home-menu" id="homeMenu">'+
      '<button class="btn icon ghost" id="homeMenuBtn" aria-label="More actions" title="More">'+RK.icon('more',{size:18})+'</button>'+
      '<div class="home-menu-pop">'+
        '<button data-call="show_history" data-args="[]">'+RK.icon('history',{size:16})+' History</button>'+
        '<button data-call="open_meetings_folder" data-args="[]">'+RK.icon('folder',{size:16})+' Open meetings folder</button>'+
        '<button data-call="summarize_text_dialog" data-args="[]">'+RK.icon('summarize',{size:16})+' Summarize a meeting file</button>'+
      '</div></div>';
    var head = '<div class="home-head"><h1 class="greet">'+greeting()+'</h1>'+
      '<div class="hh-actions">'+menu+'</div></div>';

    // --- info line + primary actions, OR the live bar while a meeting runs ---
    var info;
    if(active){
      info = '<div class="home-live">'+RK.orb('sm busy')+
        '<span class="grow lv-t"><span class="rdot" style="--d:'+RK.ad(1.6667)+'"></span>Meeting in progress</span>'+
        (S.live_transcript_available ? '<button class="btn" data-call="open_live_transcript" data-args="[]">Open live transcript</button>':'')+
        '<button class="btn lia" style="--d:'+RK.ad(4.8)+'" data-call="home_stop_meeting" data-args="[true]">End &amp; summarize</button>'+
        '<button class="btn" data-call="home_stop_meeting" data-args="[false]">End</button>'+
        '<button class="btn danger" data-call="cancel_meeting" data-args="[]">Discard</button>'+
        '</div>';
    } else {
      var src = h.recording_source;
      var srcTxt = src==='both'?'Mic + system audio':(src==='stereo_mix'?'System audio':'Microphone');
      function chip(ic, k, v){ return '<span class="hchip" title="'+esc(T(k))+'">'+RK.icon(ic,{size:15})+
        '<b dir="auto">'+esc(T.quiet(v))+'</b></span>'; }
      info = '<div class="home-actions">'+
          '<button class="btn rec" data-call="home_start_meeting" data-args="[]">'+
            '<span class="rdot" style="--d:'+RK.ad(2.2)+'"></span>Start meeting</button>'+
          '<button class="btn" data-call="transcribe_file" data-args="[]">'+
            RK.icon('file',{size:16})+' Transcribe file</button>'+
        '</div>'+
        '<div class="home-info">'+
        chip('mic','Dictation microphone', h.mic_name||'Default mic')+
        (h.meeting_mic_name?chip('audio','Meeting microphone', h.meeting_mic_name):'')+
        chip('meetings','Recording source', srcTxt)+
        chip('keyboard','Press-to-talk hotkey', hk.main||'ctrl+space')+'</div>';
    }

    // --- meetings list (the main content) ---
    var all = homeMeetings(), rows = homeFiltered();
    if(rows.length && (homeSel==null || !rows.some(function(m){return m.path===homeSel;})))
      homeSel = rows[0].path;
    markMeetingsSeen(all);
    var rowsHtml = rows.length ? rows.map(function(m, i){ return mtgRow(m, i); }).join('') : mtgEmptyHtml(all);
    var list = '<div class="mtg-list">'+
      '<div class="mtg-listhead">'+
        '<div class="mtg-search">'+RK.icon('search',{size:14})+
          '<input type="text" id="mtgSearch" placeholder="Search meetings…" autocomplete="off" spellcheck="false" value="'+esc(homeSearch||'')+'">'+
        '</div><span class="mtg-count" id="mtgCount">'+rows.length+'</span>'+
      '</div>'+
      '<div class="mtg-rows" id="mtgRows">'+rowsHtml+'</div>'+
    '</div>';

    var preview = '<div class="mtg-preview empty" id="mtgPreview">'+
      RK.icon('meetings',{size:22})+'<div style="margin-top:8px">Select a meeting to preview its summary.</div></div>';

    return head + updateBar() + info + '<div class="home-body">'+list+preview+'</div>';
  };

  PAGES.general = function(){
    var hk = (S.hotkeys||{});
    var histWeeks = parseInt(cfg("history_retention_weeks", 2), 10);
    if(isNaN(histWeeks) || histWeeks < 0) histWeeks = 2;
    if(histWeeks > 12) histWeeks = 12;
    // Languages (multi-language, 2026-09-15): a primary-language dropdown, a
    // "lock to one language" switch, and an enabled-languages checklist (the
    // anti-roaming control - the app never picks a language outside this set).
    var LANGS = (S.languages && S.languages.rows) || [];
    var LPRIM = (S.languages && S.languages.primary) || cfg("primary_language","he");
    var LLOCK = !!(S.languages && S.languages.locked);
    var LCOUNT = (S.languages && S.languages.model_lang_count) || LANGS.length;
    var langOpts = LANGS.map(function(r){
      return '<option value="'+esc(r.id)+'"'+(r.id===LPRIM?' selected':'')+'>'+
             esc(r.native)+(r.native!==T(r.name_en)?' ('+esc(T(r.name_en))+')':'')+'</option>';
    }).join('');
    var langChecks = LANGS.map(function(r){
      var nm = ((r.native||'')+' '+(r.name_en||'')).toLowerCase();
      return '<label class="switch lang-item" data-lang-name="'+esc(nm)+'" '+
        'style="display:flex;width:100%;margin:1px 0"'+(r.supported===false?' title="Not supported by the current model"':'')+'>'+
        '<input type="checkbox" data-lang-enable data-lang-code="'+esc(r.id)+'"'+
        (r.enabled?' checked':'')+'><span class="track"></span>'+
        '<span>'+esc(r.native)+(r.native!==T(r.name_en)?' ('+esc(T(r.name_en))+')':'')+
        (r.supported===false?' <small style="opacity:.6">not in this model</small>':'')+'</span></label>';
    }).join('');
    var enabledNames = LANGS.filter(function(r){ return r.enabled; }).map(function(r){ return r.native; });
    var histSel = '<select id="sel_hist_weeks" data-select="set_history_retention_weeks" style="max-width:170px">'+
      [1,2,3,4,5,6,7,8,9,10,11,12].map(function(w){
        return '<option value="'+w+'"'+(histWeeks===w?' selected':'')+'>'+w+' week'+(w===1?'':'s')+(w===2?' (default)':'')+'</option>';
      }).join('')+
      '<option value="0"'+(histWeeks===0?' selected':'')+'>Keep everything</option></select>';
    var shortcuts = '<div class="list">'+Object.keys(hk).map(function(k){
      return '<div class="row"><span class="grow">'+esc(k)+'</span><span class="kbd">'+esc(hk[k])+'</span></div>';
    }).join('')+'</div>';
    var rm = cfg("recording_mode","hold");
    var pm = cfg("paste_mode","auto_paste");
    var UIL = cfg("ui_language","en");
    return '<div class="content-head"><h1>General</h1></div>'+
      // Dictation first: the daily-use controls lead the page.
      grp('Dictation',
        srow('Press-to-talk hotkey', 'Click Capture, then press the keys. Needs Ctrl, Alt or Shift.',
          '<input type="text" id="hotkeyInput" style="width:140px" value="'+
            esc(draftOr("hotkeyInput", hk.main||"ctrl+space"))+'">'+
          '<button class="btn" id="btnCapture">Capture&#8230;</button>'+
          '<button class="btn primary" data-save-hotkey="1">Save</button>'+
          '<button class="btn ghost" data-reset-hotkey="1">Reset</button>', 'wide')+
        srow('Recording mode', rm==="toggle" ? 'Press once to start, press again to stop.'
                                             : 'Records while you hold the hotkey down.',
          seg("rm","set_recording_mode","str",[["hold","Hold",rm==="hold"],["toggle","Toggle",rm==="toggle"]]))+
        srow('After transcription', pm==="clipboard_only" ? 'The text is copied; you paste it yourself.'
                                                         : 'The text is pasted where your cursor is.',
          seg("pm","set_paste_mode","str",[["auto_paste","Paste (Ctrl+V)",pm==="auto_paste"],
                                            ["clipboard_only","Clipboard only",pm==="clipboard_only"]]))+
        srow('Restore clipboard after paste', 'Puts back what you had copied, about 2 seconds later.',
          swb(!!cfg("clipboard_auto_restore", true), "toggle_clipboard_auto_restore"))+
        srow('Press Enter after paste', 'Sends the message right away in chat apps.',
          swb(!!cfg("press_enter_after_paste", false), "toggle_press_enter_after_paste"))+
        srow('Invisible mode', 'No overlay or waveform while recording; the tray icon still shows the state.',
          swb(!!cfg("silent_mode", false), "toggle_silent_mode"))
      )+
      grp('Languages',
        // each option in its own language, never translated (an English
        // reader who landed on the Hebrew interface still finds "English")
        srow('Interface language', 'Lia’s windows, menus and cards. Speech and summary languages are set below and on Meetings.',
          seg("uil","set_ui_language","str",[["en","English",UIL!=="he"],["he","עברית",UIL==="he"]]).replace('<div class="seg"', '<div class="seg no-i18n"'))+
        srow('Primary language', 'What you mainly speak, and the default language of summaries (Meetings page to change it).',
          '<select data-select-str="set_primary_language" style="max-width:220px">'+langOpts+'</select>')+
        srow('Lock to one language', 'Always transcribe the primary language, never detect another.',
          '<label class="switch bare"><input type="checkbox" data-lang-lock data-lang-primary="'+esc(LPRIM)+'"'+
            (LLOCK?' checked':'')+'><span class="track"></span></label>')+
        more('langs', 'Languages I use', 'Lia only detects among these ('+LCOUNT+' in this model).',
          curList(enabledNames.length ? enabledNames : ['None']),
          '<input type="text" id="langFilter" placeholder="Search '+LCOUNT+' languages…" style="width:100%;box-sizing:border-box;margin:0 0 6px">'+
          '<div id="langList" style="max-height:220px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:6px;background:var(--card)">'+langChecks+'</div>'+
          '<div class="hint">Tick exactly one (or use the lock) to force it. Whisper models auto-detect among your ticked set; Gemini pins to it; Parakeet always auto-detects among its own languages.</div>')
      )+
      grp('Startup and history',
        srow('Start with Windows', 'Lia starts in the system tray when you sign in, ready to dictate.',
          swb(!!S.auto_start, "toggle_auto_start"))+
        srow('Keep dictation history for', 'Older entries are removed at startup and after each dictation.',
          histSel+'<button class="btn danger" data-clear-history="1">Delete all history&#8230;</button>')
      )+
      grp('Keyboard shortcuts',
        more('shortcuts', 'All shortcuts', Object.keys(hk).length+' shortcuts',
          '<span class="kbd">'+esc(hk.main||"ctrl+space")+'</span>', shortcuts)
      );
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
    // Selection is matched by NAME when one is stored (indices shift on USB
    // hot-plug - a headset's slot once became the Cam Link, and this radio
    // dutifully highlighted the wrong device). Falls back to the index for a
    // legacy config that has no name yet.
    var mname = cfg("input_device_name", "") || "";
    function isSel(storedIdx, storedName, d){ return storedName ? (d.name===storedName) : (storedIdx===d.idx); }
    function gone(storedName){ return !!storedName && !(S.mics||[]).some(function(d){ return d.name===storedName; }); }
    function goneRow(storedName){
      return '<div class="rowbtn disabledrow"><span class="ic">&#9679;</span><span class="grow rtl-auto" dir="auto">'+esc(storedName)+
        '</span><span class="hk i18n">disconnected</span></div>';
    }
    // the current value shown on a collapsed device row
    function curDev(label, missing){
      return '<span class="cur rtl-auto'+(missing?' warn':'')+'" dir="auto">'+esc(T.quiet(label))+(missing?' '+T('(disconnected)'):'')+'</span>';
    }
    function nameOf(list, storedIdx, storedName, dflt){
      if(storedName) return storedName;
      var hit = (list||[]).filter(function(d){ return d.idx===storedIdx; })[0];
      return hit ? hit.name : dflt;
    }
    var mics = [devRow("toggle_mic_device", null, "System Default", micOn && mid===null && !mname)];
    (S.mics||[]).forEach(function(d){ mics.push(devRow("toggle_mic_device", d.idx, d.name, micOn && isSel(mid, mname, d))); });
    if(gone(mname)) mics.push(goneRow(mname));
    // Dedicated meeting mic (radio semantics; null = follow the dictation mic).
    // Lets a headset own the meeting while a desk mic stays free for dictating
    // mid-meeting. Falls back to the dictation mic if the device is unplugged.
    var mmid = cfg("meeting_input_device_index", null);
    var mmname = cfg("meeting_input_device_name", "") || "";
    var mmics = [devRow("set_meeting_mic_device", null, "Same as dictation mic", mmid===null && !mmname)];
    (S.mics||[]).forEach(function(d){ mmics.push(devRow("set_meeting_mic_device", d.idx, d.name, isSel(mmid, mmname, d))); });
    if(gone(mmname)) mmics.push(goneRow(mmname));
    var loops = "";
    if(S.loopback_available){
      var L = [devRow("toggle_loopback_device", null, "System Default", sysOn && lid===null)];
      (S.loopbacks||[]).forEach(function(d){ L.push(devRow("toggle_loopback_device", d.idx, d.name, sysOn && lid===d.idx)); });
      loops = more('dev_sys', 'System audio device', 'The output whose sound is recorded (meetings, videos).',
        sysOn ? curDev(nameOf(S.loopbacks, lid, "", "System Default")) : '<span class="cur muted">Off</span>',
        L.join(''));
    }
    // Sounds (moved here from General 2026-09-15): the done-beep output device.
    var beep = cfg("beep_device_index", "off");
    var beepRows = [
      radio("beep","set_beep_device","off","str","None (no beep)", beep==="off"),
      radio("beep","set_beep_device","__null__","null","System Default", beep===null)
    ];
    (S.outputs||[]).forEach(function(d){
      beepRows.push(radio("beep","set_beep_device",String(d.idx),"int",d.name, beep===d.idx)
        .replace('<label class="radio', '<label class="radio no-i18n"'));
    });
    var beepCur = beep==="off" ? 'None' : (beep===null ? 'System Default' : nameOf(S.outputs, beep, "", "Device "+beep));
    return '<div class="content-head"><h1>Audio</h1></div>'+
      grp('Recording sources',
        srow('Record my microphone', 'Your voice: dictation and your side of a meeting.', swb(micOn, "toggle_record_mic"))+
        srow('Record system audio', S.loopback_available ? 'What the PC plays: the other side of a meeting.'
                                                         : 'Not available on this PC (no loopback device).',
          swb(sysOn, "toggle_record_system", !S.loopback_available)),
        {action: btn("Refresh devices","refresh",[],"ghost",true)})+
      grp('Devices',
        more('dev_mic', 'Dictation microphone', 'The mic used for press-to-talk dictation.',
          micOn ? curDev(mname || nameOf(S.mics, mid, "", "System Default"), gone(mname)) : '<span class="cur muted">Off</span>',
          mics.join(''))+
        more('dev_mtg', 'Meeting microphone', 'Your side of a meeting. Pick your call headset; unplugged = the dictation mic.',
          curDev(mmname || (mmid===null ? "Same as dictation mic" : nameOf(S.mics, mmid, "", "Device "+mmid)), gone(mmname)),
          mmics.join('')+
          '<div class="hint">Needs "Record my microphone" on. The Dictation microphone stays free for dictating mid-meeting.</div>')+
        loops
      )+
      grp('Sounds',
        more('beep', 'Done beep', 'A short beep after a transcription is pasted.',
          '<span class="cur rtl-auto" dir="auto">'+esc(T.quiet(beepCur))+'</span>', beepRows.join(''))
      );
  };

  PAGES.models = function(){
    var t = (S.tables||{});
    // Each role is ONE row showing the selected model, its provider logo and
    // where it runs (this replaced the separate engine-summary card,
    // 2026-10-05); the full list opens in place. A selection that is not
    // usable (no key / not installed) shows its note in the warning colour -
    // derived from the row's own enabled/note, never a made-up status.
    function pick(k, title, desc, rows, method, argKey, argtype){
      rows = rows || [];
      var sel = rows.filter(function(r){ return r.checked; })[0];
      var cur;
      if(!sel){
        cur = '<span class="cur warn">Not set</span>';
      } else {
        var ready = sel.enabled!==false;
        cur = '<span class="cur rtl-auto'+(ready?'':' warn')+'" dir="auto" title="'+esc(T.quiet(ready?sel.label:(sel.note||"Needs setup")))+'">'+
            provIcon(sel.label)+esc(T.quiet(sel.label))+'</span>'+
          (sel.where ? '<span class="wb '+esc(sel.where)+'">'+(WB_TEXT[sel.where]||esc(sel.where))+'</span>' : '');
      }
      var body = rows.map(function(r){
        return radio(method, method, String(r[argKey]), argtype, r.label, r.checked, r.enabled!==false,
                     r.note||"", r.where||"", r.wnote||"");
      }).join('');
      return more(k, title, desc, cur, body);
    }
    // Short segment labels; the full wording stays in the row description.
    var SHORT = {auto:"Auto", cuda:"GPU", cpu:"CPU", off:"Off (wait)"};
    function segOf(name, method, rows){
      return seg(name, method, "str", (rows||[]).map(function(r){
        return [r.key, SHORT[r.key] || r.label, r.checked, r.enabled!==false];
      }));
    }
    // Local processing options (moved here from General 2026-09-15): where the
    // local Whisper model runs, and what a busy GPU falls back to.
    var local = '';
    if((t.device||[]).length){
      local = grp('Local processing',
        srow('Local Whisper device ('+esc(S.whisper_device_label||"Auto")+')',
          'Auto uses the GPU when there is one, else the CPU.', segOf("dev","set_whisper_device",t.device))+
        ((t.gpu_busy||[]).length ?
          srow('When the GPU is busy, dictate with',
            'If another app holds the GPU, a dictation goes to this service (then the others with a key) and Lia tells you.',
            segOf("gpubusy","set_dictation_gpu_busy_cloud",t.gpu_busy)) : '')+
        ((t.summary_gpu_busy||[]).length ?
          srow('When the GPU is busy, summarize with',
            'A local summary that would wait runs on this service instead; the transcript is sent to it. Off = wait. Transcription and diarization always wait.',
            segOf("sumgpubusy","set_summary_gpu_busy_cloud",t.summary_gpu_busy)) : '')
      );
    }
    return '<div class="content-head"><h1>Models</h1></div>'+
      grp('Engines',
        pick('m_dict', 'Dictation model', 'Press-to-talk dictation.', t.dictation, "set_dictation_model", "idx", "int")+
        pick('m_meet', 'Meeting transcription model', 'Live meetings.', t.meeting, "set_meeting_model", "key", "str")+
        pick('m_sum', 'Meeting summary model', 'Writes the summary after a meeting.', t.summary, "set_summary_model", "model", "str")+
        pick('m_file', 'Transcribe-file model', 'Tray > Transcribe a file.', t.file, "set_file_model", "key", "str")
      )+
      local;
  };

  PAGES.cleanup = function(){
    var t = (S.tables||{});
    // Cleanup Off -> no provider/model is used, so grey out that whole row
    // (and disable its radios) until a style is picked (Naor's ask).
    var isOff = (t.cleanup_styles||[]).some(function(r){ return r.checked && r.style==="off"; });
    // "Spoken - remove fillers ..." -> segment "Spoken", the rest describes it
    var DASH = new RegExp('\\s[-' + String.fromCharCode(8212) + ']\\s');
    function parts(label){
      var m = DASH.exec(label||"");
      return m ? [label.slice(0, m.index), label.slice(m.index + m[0].length)] : [label, ""];
    }
    var styles = t.cleanup_styles||[];
    var cur = styles.filter(function(r){ return r.checked; })[0];
    var curDesc = cur ? parts(cur.label)[1] : "";
    // noicon: AI Cleanup rows carry NO provider logo (Naor 2026-09-16 - the
    // logos live on the Models page; keep Cleanup plain).
    var styleSeg = seg("cs","set_cleanup_style","str", styles.map(function(r){
      return [r.style, parts(r.label)[0], r.checked];
    }));
    var models = (t.cleanup_models||[]);
    var modelRow;
    if(!models.length){
      modelRow = srow('Provider and model', 'Add an OpenAI, Groq or Gemini key to enable AI cleanup.',
        '<span class="cur warn">No key</span><button class="btn" data-page-link="keys">Go to API Keys</button>');
    } else {
      var curModel = models.filter(function(r){ return r.checked; })[0];
      modelRow = more('cl_model', 'Provider and model',
        isOff ? 'Cleanup is Off - pick a style above to choose one.' : 'The model that rewrites each dictation.',
        isOff ? '<span class="cur muted">Off</span>'
              : '<span class="cur">'+esc(curModel ? curModel.label : (S.cleanup_model_label||"Not set"))+'</span>',
        models.map(function(r){
          return radio("cm","set_cleanup_provider_model",JSON.stringify([r.provider,r.model]),"json",r.label,r.checked,!isOff,"","","",true);
        }).join(''));
    }
    // When cleanup is Off no model runs, so the header must not read as active
    // (a green dot + model name looked like the motor was on - Naor 2026-09-17):
    // show a muted "Off" instead of the model badge.
    var head = isOff
      ? '<span class="status off"><span class="dot"></span>Off</span>'
      : '<span class="status ok"><span class="dot"></span>'+esc(S.cleanup_model_label||"")+'</span>';
    return '<div class="content-head"><h1>AI Cleanup</h1>'+head+'</div>'+
      grp('Cleanup after dictation',
        srow('Style', curDesc ? esc(curDesc) : 'How the dictated text is polished before it is pasted.', styleSeg)+
        modelRow
      );
  };

  var KEYCARDS = [
    {svc:"openai", key:"openai_api_key", name:"OpenAI", badge:"O", color:"#0aa37f", img:"__OPENAI_LOGO__",
     sub:"Paid - best dictation, meeting transcription & summaries.",
     save:"Save & Verify", url:"https://platform.openai.com/api-keys", slow:true},
    {svc:"gemini", key:"gemini_api_key", name:"Gemini", badge:"G", color:"#3b6fd4", img:"__GEMINI_LOGO__",
     sub:"Free meeting summaries + free AI cleanup (Google AI Studio).",
     save:"Save", url:"https://aistudio.google.com/apikey", slow:true},
    {svc:"hf", key:"hf_token", name:"Local Diarization", badge:"H", color:"#b08900",
     sub:"100% local speaker labels on your GPU (pyannote + ivrit.ai).",
     save:"Save & Download", url:"https://huggingface.co/settings/tokens", slow:true, noun:"token"},
    {svc:"groq", key:"groq_api_key", name:"Groq", badge:"G", color:"#d97706", img:"__GROQ_LOGO__",
     sub:"Free, fast cloud dictation (Whisper v3 Turbo).",
     save:"Save & Verify", url:"https://console.groq.com/keys", slow:true}
    // AssemblyAI (paid cloud diarization) card removed 2026-09-01 - local
    // pyannote + Gemini diarize cover meetings; the key/code path still exist.
  ];

  PAGES.keys = function(){
    var secrets = S.secrets||{}, has = S.has||{}, rejected = S.rejected||{};
    // One collapsed row per provider: logo + name + what it is for, and the
    // key's state (set / not set / rejected). The editor opens in place; a
    // rejected key opens by default so the fix is one paste away.
    var rows = KEYCARDS.map(function(c){
      var noun = c.noun||"key";
      var bad = has[c.key] && rejected[c.key];
      var cur = bad ? '<span class="cur warn">Rejected</span>'
              : (has[c.key] ? '<span class="cur ok">Set</span> <span class="masked">'+esc(secrets[c.key]||"")+'</span>'
                            : '<span class="cur muted">Not set</span>');
      var iid = "in_"+c.svc;
      var bdg = (c.img && c.img.indexOf("data:")===0)
        ? '<span class="kbdg img"><img src="'+c.img+'" alt=""></span>'
        : '<span class="kbdg">'+esc(c.badge)+'</span>';
      var title = '<span class="kname">'+bdg+'<span>'+esc(c.name)+'</span></span>';
      var body =
        (bad ? '<div class="keybad">'+esc(c.name)+' rejected this '+esc(noun)+
               ' (revoked, deleted or mistyped). Paste a new one below.</div>' : '')+
        '<div class="row-inline">'+
          '<input type="password" id="'+iid+'" class="mono" placeholder="'+(has[c.key]?'Paste a new '+esc(noun)+' to replace it…':'Enter '+esc(noun)+'…')+
            '" style="flex:1" value="'+esc(draftOr(iid,""))+'">'+
          '<button class="btn ghost" data-show="'+iid+'">Show</button>'+
          '<button class="btn primary" data-apply-key="'+c.svc+'" data-input="'+iid+'"'+(c.slow?' data-slow="1"':'')+'>'+esc(c.save)+'</button>'+
          '<button class="btn danger" data-call="clear_key" data-args=\''+JSON.stringify([c.svc])+'\'>Clear</button>'+
        '</div>'+
        '<div class="btnrow" style="margin-top:6px"><span class="hint" id="st_'+c.svc+'" style="margin:0"></span><span class="grow"></span>'+
          '<a href="#" data-openurl="'+esc(c.url)+'">Get a '+esc(noun)+' ↗</a></div>';
      return more('key_'+c.svc, title, esc(c.sub), cur, body, bad);
    }).join('');
    return '<div class="content-head"><h1>API Keys</h1></div>'+
      grp('Providers', rows)+
      '<div class="privacy-note" style="margin-top:12px"><span class="pn-ico">&#9888;&#65039;</span>'+
        '<span>'+T('On <b>free cloud tiers</b> providers often use what you send to <b>train their models</b>. '+
        'Avoid confidential material on free plans. Local models never leave this machine.')+'</span></div>';
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

    // Client / Server as a two-button segmented control (the big mode cards
    // took a third of the screen). Same data-set-role wiring.
    function roleBtn(r, label){
      return '<button class="radio'+(role===r?' on':'')+'" data-set-role="'+r+'">'+
        '<span class="box"></span><span class="txt">'+esc(label)+'</span></button>';
    }
    var modeRows =
      srow('This PC', role==="client" ? 'Uses a transcription server running on another machine (your home GPU).'
                    : (role==="server" ? 'Hosts a server on this GPU so your other devices transcribe against it.'
                                       : 'Pick whether this PC uses a server or hosts one.'),
        '<div class="seg">'+roleBtn("client","Client")+roleBtn("server","Server")+'</div>')+
      srow('Tailscale', 'Free; puts your PCs on one private network. No router setup, no open ports. Same account on every machine.',
        '<button class="btn" data-call="open_tailscale" data-args="[]">Download Tailscale</button>');

    // --- CLIENT (was Home Server) ---
    var url = draftOr("in_remote_url", cfg("remote_server_url",""));
    var clientGrp = grp('Connection',
      srow('Server URL', 'host:9090 · ws://host:9090 · wss://your-domain',
        '<input type="text" id="in_remote_url" class="mono" placeholder="ws://host:9090" style="width:260px" value="'+esc(url)+'">')+
      srow('Access token', 'Optional. The same value as the server’s token.',
        '<input type="password" id="in_remote_tok" class="mono" style="width:260px" placeholder="'+
          (has.remote_server_token?"(saved - leave blank to keep)":"none")+'" value="'+esc(draftOr("in_remote_tok",""))+'">')+
      srow('<span class="hint" id="st_remote" style="margin:0"></span>', '',
        '<button class="btn primary" data-apply-remote="1" data-slow="1">Save</button>'+
        '<button class="btn" data-test-remote="1" data-slow="1">Test</button>'+
        '<button class="btn danger" data-call="clear_key" data-args=\'["remote"]\'>Clear</button>')+
      '<div class="gbody"><details class="setup-help" style="margin:0;border:0;padding:0"><summary>How to set up (Tailscale)</summary><ol>'+
        '<li>'+T('Install <b>Tailscale</b> on both machines from <span class="mono">tailscale.com/download</span> and sign in with the <b>same account</b>.')+'</li>'+
        '<li>'+T('On the server machine, set <b>This PC</b> to <b>Server</b>, turn it on, and note its <span class="mono">ws://…</span> address.')+'</li>'+
        '<li>'+T('Paste that address above, click <b>Test</b>, then <b>Save</b>.')+'</li>'+
        '<li>'+T('On the <b>Models</b> page, pick a <b>Home Server</b> model.')+'</li>'+
      '</ol><div class="hint">'+T('Work PC that blocks Tailscale? Use a Cloudflare tunnel with a <span class="mono">wss://your-domain</span> URL + token.')+'</div></details></div>');

    // --- SERVER (host) ---
    var verdict = gpu.verdict || "none";
    var canServe = (verdict !== "none");
    var gpuTitle = gpu.has_cuda
      ? (esc(gpu.name || "CUDA GPU") + (gpu.vram_gb ? (' · ' + gpu.vram_gb + ' GB VRAM') : ''))
      : "No dedicated GPU detected";
    var runState = sv.running ? "on" : (sv.enabled ? "starting" : "off");
    var runTitle = sv.running ? "Running" : (sv.enabled ? "Starting…" : "Off");
    var runSub = sv.running ? ("Listening on port " + svPort)
                            : (canServe ? "Turn on to host on this GPU" : "Needs a dedicated GPU");
    var svModel = sv.model || "";
    // Which model the server loads. Add entries here to support more languages.
    var SERVE_MODELS = [
      {id:"", label:"Hebrew", note:"ivrit.ai turbo, specialised for Hebrew (default)"},
      {id:"large-v3-turbo", label:"Multilingual", note:"Whisper large-v3-turbo, all languages"}
    ];
    var svSel = SERVE_MODELS.filter(function(m){ return m.id===svModel; })[0] || SERVE_MODELS[0];
    var HOSTS = [
      {id:"auto", short:"Tailscale, else this PC", label:"Tailscale, else this PC (recommended)", note:"Reachable by your other devices over Tailscale; this PC only when Tailscale is off."},
      {id:"loopback", short:"This PC only", label:"This PC only", note:"Nothing on the network can reach it."},
      {id:"all", short:"All networks", label:"All networks (needs a token)", note:"Every interface, including the LAN. Requires an access token."}
    ];
    var hostCur = sv.host || "auto";
    var hostSel = HOSTS.filter(function(h){ return h.id===hostCur; })[0] || HOSTS[0];
    var tokReq = (hostCur==="all" || sv.insecure);
    var urlLine = sv.ws_url
      ? '<span class="mono copyable">' + esc(sv.ws_url) + '</span>'
      : '<span class="hint" style="margin:0">' + T('Install Tailscale on both machines, then use <span class="mono">ws://&lt;this-ip&gt;:' + svPort + '</span>') + '</span>';
    var serverGrp = grp('Server',
      srow('<span class="run-led-sm '+runState+'"></span>'+runTitle, runSub,
        '<label class="switch bare'+(canServe?'':' disabled')+'" title="'+(canServe?'':'No suitable GPU')+'">'+
          '<input type="checkbox" data-toggle="toggle_serve"'+(sv.enabled?' checked':'')+(canServe?'':' disabled')+'><span class="track"></span></label>')+
      srow('GPU', esc(gpu.note||"") || gpuTitle,
        '<span class="gpu-tag '+verdict+'">'+({good:"Ready",marginal:"Marginal",none:"Not suitable"}[verdict])+'</span>')+
      srow('Model', esc(svSel.note)+'. Changing it reloads the server.',
        '<div class="seg">'+SERVE_MODELS.map(function(m){
          return radio("svmodel","set_serve_model",m.id,"str",m.label,svModel===m.id,canServe,"","","",true);
        }).join('')+'</div>')+
      more('svhost', 'Listen on', esc(hostSel.note)+' Now bound to: <b dir="ltr" class="no-i18n">'+esc(sv.host_label||"")+'</b>.',
        '<span class="cur">'+esc(hostSel.short)+'</span>',
        HOSTS.map(function(h){
          return radio("svhost","set_serve_host",h.id,"str",h.label,hostCur===h.id,canServe,h.note);
        }).join('')+
        (sv.insecure ? '<div class="hint" style="color:var(--warn)"><b>Set a token below - the server will refuse to start on a network interface without one.</b></div>' : ''))+
      srow('Port', 'Default 9090.',
        '<input type="number" id="in_serve_port" class="mono" value="'+svPort+'" style="width:110px"'+(canServe?'':' disabled')+'>')+
      srow('Access token'+(tokReq?' (required)':''), 'Type any secret you like - it does not have to be the Generated one. Put the same value on the client.',
        '<input type="password" id="in_serve_tok" class="mono" style="width:150px" placeholder="'+
          (sv.has_token?"(saved - leave blank to keep)":"none")+'"'+(canServe?'':' disabled')+'>'+
        '<button class="btn" data-gen-token="1" data-slow="1"'+(canServe?'':' disabled')+'>Generate</button>'+
        (sv.has_token?'<button class="btn" data-copy-token="1"'+(canServe?'':' disabled')+'>Copy</button>':''))+
      srow('<span class="hint" id="st_serve" style="margin:0"></span>', '',
        '<button class="btn primary" data-apply-serve="1" data-slow="1"'+(canServe?'':' disabled')+'>Save port and token</button>')+
      srow('Start at Windows logon', 'Keeps the server running after a reboot.',
        swb(!!sv.autostart, "toggle_serve_autostart", !canServe))+
      srow('Address for other devices', 'Their Transcription server page > Client > Server URL.', urlLine)+
      '<div class="gbody"><details class="setup-help" style="margin:0;border:0;padding:0"><summary>How another device connects</summary><ol>'+
        '<li>Turn the server on above - it loads a local model on this GPU.</li>'+
        '<li>'+T('Install <b>Tailscale</b> on both machines, same account.')+'</li>'+
        '<li>'+T('On the other device: <b>Transcription server → Client</b>, paste the address above, <b>Test</b>, <b>Save</b>, then pick a <b>Home Server</b> model.')+'</li>'+
      '</ol><div class="hint">One model per server; a language it does not cover falls back to the client’s own cloud / local model.</div></details></div>');

    var body = grp('Mode', modeRows);
    if(role==="client") body += clientGrp;
    else if(role==="server") body += serverGrp;
    return '<div class="content-head"><h1>Transcription server</h1></div>'+
      '<div class="hint" style="margin:-2px 4px 4px">Run transcription on one machine’s GPU and reach it from your others - no Docker.</div>'+
      body;
  };

  PAGES.meetings = function(){
    var hk = S.hotkeys||{};
    var mtgChunk = parseInt(cfg("meeting_chunk_seconds",15),10);
    var mtgOpts = [8,10,15,20,30,45];
    if(mtgOpts.indexOf(mtgChunk)<0 && mtgChunk>=5 && mtgChunk<=120){
      mtgOpts = mtgOpts.concat([mtgChunk]).sort(function(a,b){return a-b;});
    }
    var live = S.live_transcript_available
      ? arow('file','Open live transcript','open_live_transcript','')
      : '<div class="arow disabledrow"><span class="ic">'+RK.icon('file',{size:18})+'</span><span class="grow">Live transcript (no meeting running)</span></div>';
    // Summary template (cloud only): the description follows the chosen template.
    var TPL = S.summary_templates||[];
    var tplCur = cfg("summary_template","technical");
    var tplSel = TPL.filter(function(t){ return t.id===tplCur; })[0];
    var TPL_SHORT = {technical:"Technical", general:"General", minutes:"Minutes"};
    var tplSeg = seg("stpl","set_summary_template","str", TPL.map(function(t){
      return [t.id, TPL_SHORT[t.id] || t.name_en, t.id===tplCur];
    }));
    // the audio safety net: three rarely changed switches, collapsed with a count
    var SAFE = ["keep_meeting_tracks", "meeting_backup_mic", "meeting_mic_auto_fallback"];
    var safeOn = SAFE.filter(function(k){ return !!cfg(k, true); }).length;
    // Summary language: follow / always X (only languages that can be summarized)
    var slang = cfg("summary_language","primary");
    var slangRows = [["primary","Follow primary language"],["auto","Follow the transcript"]].concat(
      ((S.languages&&S.languages.rows)||[]).filter(function(r){ return r.summary_ok; }).map(function(r){
        return [r.id, "Always "+r.name_en];
      }));
    var slangCur = slangRows.filter(function(r){ return r[0]===slang; })[0];
    var profiles = ((S.home||{}).speaker_profiles)||[];
    return '<div class="content-head"><h1>Meetings</h1></div>'+
      grp('Recording',
        srow('Auto-detect Zoom / Teams / Meet calls', 'Offers to record when a call starts.',
          swb(!!cfg("auto_detect_meetings",false), "toggle_auto_detect_meetings"))+
        srow('Hide meeting windows', 'No meeting cards, prompts or notices on screen, and the summary does not open by itself. Run meetings from the tray.',
          swb(!!cfg("meeting_windows_hidden",false), "toggle_meeting_windows_hidden"))+
        srow('Live transcript updates every',
          'Lower = snappier, more transcription calls. A running meeting keeps its interval until it ends.',
          '<select data-select="set_meeting_chunk_seconds" style="max-width:200px">'+
            mtgOpts.map(function(s){
              return '<option value="'+s+'"'+(mtgChunk===s?' selected':'')+'>'+s+' seconds'+(s===15?' (default)':'')+'</option>';
            }).join('')+'</select>')+
        live
      )+
      grp('Audio safety net',
        more('safety', 'Raw tracks, backup mic, auto-switch', 'Extra recordings that rescue a meeting when a mic fails.',
          '<span class="cur'+(safeOn===SAFE.length?'':' warn')+'">'+safeOn+' of '+SAFE.length+' on</span>',
          srow('Keep raw per-source tracks', 'Mic / system / backup next to the mixdown, to re-transcribe one side alone.',
            swb(!!cfg("keep_meeting_tracks",true), "toggle_keep_meeting_tracks"))+
          srow('Backup mic', 'Also record the dictation mic when the meeting mic is a different device.',
            swb(!!cfg("meeting_backup_mic",true), "toggle_meeting_backup_mic"))+
          srow('Auto-switch to the dictation mic', 'When the meeting mic is silent but the backup hears you.',
            swb(!!cfg("meeting_mic_auto_fallback",true), "toggle_meeting_mic_auto_fallback"))+
          '<div class="hint">The mixdown is always kept; the tracks follow the same retention as the meeting audio.</div>')
      )+
      grp('Summaries',
        srow('Template', tplSel ? esc(tplSel.desc_en) : 'How the summary is laid out.', tplSeg)+
        more('sum_lang', 'Summary language', 'The language summaries are written in, whatever was spoken.',
          '<span class="cur">'+esc(slangCur ? slangCur[1] : slang)+'</span>',
          slangRows.map(function(r){
            return radio("slang","set_summary_language",r[0],"str",r[1], slang===r[0]);
          }).join(''))+
        srow('Parity rules + free code cleanups', 'Dedup, tone, owners. Still one cloud call; the cleanups are local code.',
          swb(!!cfg("summary_cloud_parity",true), "toggle_summary_cloud_parity")),
        {sub: 'OpenAI / Gemini only - the local Gemma summary is tuned on Advanced'})+
      grp('Speakers',
        srow('Your name', 'Written for your own turns and biased into transcription. Empty = your Windows name.',
          '<input type="text" id="selfName" dir="auto" style="width:200px" placeholder="e.g. נאור / Naor" value="'+esc(draftOr("selfName", cfg("speaker_self_name","")))+'">'+
          '<button class="btn primary" data-save-selfname="1">Save</button>')+
        (profiles.length ?
          more('voices', 'Known voices', 'Learned when you named speakers (local detection). Forget a wrong one.',
            '<span class="cur">'+profiles.length+' voice'+(profiles.length===1?'':'s')+'</span>',
            profiles.map(function(p){
              return '<div class="row" style="display:flex;align-items:center;gap:10px;padding:6px 0">'+
                '<span dir="auto" style="flex:1">'+esc(p.name)+'</span>'+
                '<span class="hint" style="margin:0">'+esc(String(p.count))+' sample'+(p.count===1?'':'s')+(p.updated?' · '+esc(p.updated):'')+'</span>'+
                '<button class="btn" data-call="delete_speaker_profile" data-args=\''+esc(JSON.stringify([p.name]))+'\'>Forget</button>'+
              '</div>';
            }).join('')) : '')
      )+
      // Tools stay on this page as a compact grid (Naor 2026-10-05).
      '<div class="grp-h">Tools</div><div class="grp agrid">'+
        arow('tasks','Action items (from meetings)…','open_action_items',hk.actions)+
        arow('folder','Open meeting folder','open_meetings_folder','')+
        arow('edit','Edit a meeting summary…','edit_meeting_summary','')+
        arow('meetings','Rename speakers in a meeting…','rename_speakers_old','')+
        arow('mic','Transcribe a file…','transcribe_file','')+
        arow('summarize','Summarize a meeting file…','summarize_text_dialog','')+
      '</div>'+
      grp('Experimental (Beta)',
        '<div class="agrid">'+
          arow('ask','Ask your meetings…','open_meetings_ask',hk.ask,true)+
          arow('mic','Voice ask (speak, press again)','voice_ask_now',hk.voice_ask,true)+
          arow('email','Search your email…','open_email_search',hk.email,true)+
        '</div>'+
        srow('Voice ask answer goes to', '',
          seg("vao","set_voice_ask_output","str",[["card","Answer card",cfg("voice_ask_output","card")==="card"],
                                                   ["paste","Paste at cursor",cfg("voice_ask_output")==="paste"],
                                                   ["both","Both",cfg("voice_ask_output")==="both"]])),
        {sub: 'local models, needs a 24 GB VRAM GPU'});
  };

  PAGES.vocab = function(){
    // Every list here loads when its row opens (ONOPEN below); a closed row
    // shows the count from the state.
    var manual = String(cfg("custom_vocabulary","")||"").split(/[,\n]/).filter(function(x){ return x.trim(); }).length;
    return '<div class="content-head"><h1>Vocabulary</h1></div>'+
      grp('Terms',
        more('v_manual', 'Manual terms', 'Terms the transcriber should prefer, comma- or newline-separated.',
          '<span class="cur">'+manual+' term'+(manual===1?'':'s')+'</span>',
          '<textarea id="vocabText" class="rtl-auto" dir="auto" style="min-height:120px">'+esc(draftOr("vocabText", cfg("custom_vocabulary","")))+'</textarea>'+
          '<div class="btnrow"><button class="btn primary" data-save-vocab="1">Save terms</button>'+
          '<button class="btn ghost" data-reset-vocab="1">Reset to default terms</button></div>')+
        srow('Auto-learn new terms from meetings', 'New terms found in your meetings are suggested below.',
          swb(!!cfg("vocab_autolearn",true), "toggle_vocab_autolearn"))+
        srow('Scan meetings for new terms', 'Runs that search over all your saved meetings now.',
          btn("Scan now","vocab_rebuild",[],"ghost",true))
      )+
      grp('Learned from meetings',
        more('v_pending', 'Suggestions',
          T('<b>Tick</b> a term spelled right, <b>type the right spelling</b> if Lia heard it wrong, or leave it. Untouched ones stay.'),
          '<span class="cur"><span class="badge soft" id="pendingBadge">'+(S.vocab_pending||0)+'</span> waiting</span>',
          '<div class="btnrow" style="margin-top:0"><button class="btn primary" data-vocab-apply="1">Save choices</button>'+
          '<button class="btn ghost" data-vocab-drop-rest="1">Drop the untouched ones</button></div>'+
          '<div id="pendingList"></div>'+
          '<details class="setup-help" data-k="v_dropped"'+opn("v_dropped")+'><summary>Dropped suggestions</summary><div id="droppedList"></div></details>')+
        more('v_learned', 'Learned terms',
          'English terms from your meetings, given to the transcriber as hints. Common words take room from rarer ones.',
          '<span class="cur"><span class="badge soft" id="learnedBadge">'+(S.vocab_learned_count!=null?S.vocab_learned_count:'')+'</span> terms</span>',
          '<div id="learnedTools" class="vtools" hidden>'+
            '<input type="text" id="learnedFilter" placeholder="Filter…" autocomplete="off" spellcheck="false">'+
            '<select id="learnedSort"><option value="used">Most used</option><option value="least">Least used</option><option value="az">A-Z</option></select>'+
            '<button class="btn ghost sm" data-learned-tool="shown">Check all shown</button>'+
            '<button class="btn ghost sm" data-learned-tool="generic">Check common words</button>'+
            '<button class="btn ghost sm" data-learned-tool="clear">Clear checks</button>'+
            '<button class="btn danger sm" data-vocab-remove="1">Remove checked</button>'+
            '<span class="hint" id="learnedCount"></span>'+
          '</div>'+
          '<div class="hint" style="margin:0 0 6px">"used" = times the term showed up in your transcripts since it was learned.</div>'+
          '<div id="learnedList" class="vgrid"></div>')
      )+
      grp('Corrections',
        more('v_corr', 'Corrections (wrong &#8594; right)',
          '&#215;N = applied live. hits = matches in your saved archive (Scan). "unused" = zero of both, safe to prune.',
          '<span class="cur">'+(S.corrections_count!=null?S.corrections_count+' corrections':'')+'</span>',
          '<div class="btnrow" style="margin-top:0">'+btn("Scan archive","__scan_corr",[],"ghost",true)+
            '<button class="btn" data-corr-unused="1">Select unused</button>'+
            '<button class="btn danger" data-corr-remove="1">Remove checked</button></div>'+
          '<div id="corrList"></div>'+
          '<div class="row-inline" style="margin-top:8px">'+
            '<input type="text" id="corrWrong" class="mono" placeholder="Wrong" style="max-width:180px">'+
            '<span>&#8594;</span>'+
            '<input type="text" id="corrRight" class="mono" placeholder="Right" style="max-width:180px">'+
            '<button class="btn" data-add-corr="1">Add</button></div>')+
        srow('Auto-harvest corrections from new meetings', 'Local: takes the fixes a meeting summary proposed.',
          swb(!!cfg("corrections_autoharvest",true), "toggle_corrections_autoharvest"))+
        srow('Harvest new meetings now', '', btn("Harvest now","harvest_corrections_now",[],"ghost",true))
      )+
      lexiconPanel();
  };

  function lexiconPanel(){
    var lx = S.lexicon||{};
    var status = lx.installed
      ? (lx.loaded ? ('Installed and active - '+(lx.words||0).toLocaleString()+' words')
                   : ('Installed ('+(lx.size_mb||0)+' MB) - turn on the fix to load it'))
      : 'Not installed';
    var rows = srow('Hebrew dictionary', status,
        lx.installed ? '<span class="cur ok">Installed</span>'
                     : '<button class="btn primary" data-call="lexicon_download" data-args="[]" data-slow="1">Download (5.7 MB)</button>');
    if(lx.installed){
      rows +=
        // <bdi> keeps "wrong -> right" in reading order inside the English line
        srow(T('Fix the plural <bdi>&#1497;&#1503;</bdi> &#8594; <bdi>&#1497;&#1501;</bdi>'),
          T('e.g. <b><bdi>&#1489;&#1497;&#1496;&#1493;&#1500;&#1497;&#1503;</bdi></b> &#8594; <b><bdi>&#1489;&#1497;&#1496;&#1493;&#1500;&#1497;&#1501;</bdi></b>, the fast-speech plural Whisper mishears. Only this one is fixed automatically.'),
          swb(!!lx.enabled, "toggle_lexicon_fix"))+
        srow('Suggest other unknown Hebrew words', 'They are listed below; nothing changes until you add one.',
          swb(lx.suggest!==false, "toggle_lexicon_suggest"));
    }
    rows += more('v_oov', 'Suggested unknown words',
      'Not in the dictionary, plus fixes a meeting summary proposed (pre-filled). Add = joins the corrections.',
      '', '<div id="oovList"></div>');
    return grp('Hebrew spelling guard', rows+
      '<div class="gnote">'+T('Uses the hspell Hebrew word list (AGPL-3.0), downloaded to your config folder - <b>not</b> bundled with Lia.')+'</div>');
  }
  ONOPEN.v_pending = function(){ if(isEmpty("pendingList")) loadPending(); };
  ONOPEN.v_dropped = function(){ if(isEmpty("droppedList")) loadDropped(); };
  ONOPEN.v_learned = function(){ var t = $("learnedTools"); if(t && t.hidden) loadLearned(); };
  ONOPEN.v_corr = function(){ if(isEmpty("corrList")) loadCorr(); };
  ONOPEN.v_oov = function(){ if(isEmpty("oovList")) loadOov(); };

  PAGES.snippets = function(){
    // the table loads by itself when the page opens (ONPAGE.snippets)
    return '<div class="content-head"><h1>Snippets</h1></div>'+
      grp('Snippets',
        '<div class="gnote">Say a cue to paste its expansion. Use \\n for line breaks.</div>'+
        '<div class="gbody"><div id="snipList"></div></div>',
        {action: '<button class="btn" data-add-snip="1">Add row</button>'+
                 '<button class="btn primary" data-save-snips="1">Save</button>'});
  };

  PAGES.advanced = function(){
    var p = S.paths||{};
    var u = S.update || {};
    var updRow;
    if(u.kind === 'source'){
      updRow = srow('Updates', 'Running from a source checkout - update it with git.', '');
    } else {
      var st = u.status === 'checking' ? 'Checking…'
             : (u.status === 'latest' ? 'Lia '+esc(u.current||'')+' is the latest version.' : '');
      if(!st && u.error && !u.available) st = '<span style="color:var(--err)">'+esc(u.error)+'</span>';
      updRow = srow('Check for updates automatically', st || 'Lia checks GitHub for a newer signed release.',
        swb(u.auto !== false, "toggle_update_check")+btn("Check now","update_check_now",[],"ghost",true));
    }
    var PASSES = [["summary_local_tasks_pass",false],["summary_consolidate_pass",true],["summary_task_done_pass",true],
                  ["summary_coverage_pass",false],["summary_depth_pass",true]];
    var passesOn = PASSES.filter(function(x){ return !!cfg(x[0], x[1]); }).length;
    var baseOverride = !!cfg("summary_base_prompt_override","");
    var addOverride = !!cfg("summary_cloud_addendum","");
    function stateCur(on){ return on ? '<span class="cur warn">Manual override</span>' : '<span class="cur">Default</span>'; }
    return '<div class="content-head"><h1>Advanced</h1></div>'+
      grp('App',
        '<div class="agrid">'+
          arow('refresh','Restart Lia','restart_app','')+
          arow('file','Open log','open_log','')+
          arow('folder','Open config folder','open_config_dir','')+
          '<button class="arow" data-call="report_problem" data-args="[]" data-slow="1"><span class="ic">'+RK.icon('download',{size:18})+'</span><span class="grow">Report a problem…</span></button>'+
          '<button class="arow danger" data-quit="1"><span class="ic">'+RK.icon('close',{size:18})+'</span><span class="grow">Quit Lia</span></button>'+
        '</div>'+
        '<div class="gnote">Report a problem makes a zip of the log and settings (no API keys or personal lists), shows it and opens the GitHub issue page. Nothing is sent automatically.</div>'
      )+
      grp('About and updates',
        (updateBar() ? '<div class="gbody">'+updateBar()+'</div>' : '')+
        srow('Lia - Local Inference Assistant', 'The acronym explains what it does. The name was inspired by a little girl at home.',
          '<span class="cur mono">'+esc(S.version ? 'v'+S.version : '')+'</span>')+
        updRow+
        more('folders', 'Folders', 'Where Lia keeps its settings, log and meetings.',
          '<span class="cur">Config, log, meetings</span>',
          '<div class="kv"><span class="k">Config</span><span class="rtl-auto" dir="auto">'+esc(p.config||"")+'</span></div>'+
          '<div class="kv"><span class="k">Log</span><span class="rtl-auto" dir="auto">'+esc(p.log||"")+'</span></div>'+
          '<div class="kv"><span class="k">Meetings</span><span class="rtl-auto" dir="auto">'+esc(p.meetings||"")+'</span></div>')
      )+
      grp('Local summaries',
        more('passes', 'Local summary passes', 'Extra local Ollama calls. Gemma only, no effect on OpenAI / Gemini. Defaults recommended.',
          '<span class="cur">'+passesOn+' of '+PASSES.length+' on</span>',
          srow('Dedicated tasks pass', 'A more complete task list; strips speaker-label owners. +15-45 s.',
            swb(!!cfg("summary_local_tasks_pass",false), "toggle_summary_local_tasks_pass"))+
          srow('Merge twice-discussed topics', 'A consolidate pass.',
            swb(!!cfg("summary_consolidate_pass",true), "toggle_summary_consolidate_pass"))+
          srow('Mark tasks done during the meeting as [x]', '',
            swb(!!cfg("summary_task_done_pass",true), "toggle_summary_task_done_pass"))+
          srow('Coverage pass', 'Adds topics the summary missed. +30-60 s.',
            swb(!!cfg("summary_coverage_pass",false), "toggle_summary_coverage_pass"))+
          srow('Depth pass', 'Numbers, the why, blockers; guarded. +2 min.',
            swb(!!cfg("summary_depth_pass",true), "toggle_summary_depth_pass")))
      )+
      grp('Cloud summary prompt',
        // collapsed by default (Naor 2026-10-05): long editors, rarely opened
        more('base_prompt', 'Base prompt', 'The full instructions before your rules. Locked; unlock to edit.',
          stateCur(baseOverride),
          '<div class="hint" style="margin:0 0 6px">'+(S.summary_short_prompt
            ? 'The compact prompt for OpenAI models (one prompt, the rules below included). Gemini and the local summary keep the full prompt; '+
              'editing here replaces it for OpenAI / Gemini summaries only.'
            : 'Shared with the local Gemma summary; editing here changes OpenAI / Gemini summaries only. '+
              'The local summary runs a hardcoded pipeline of passes and never reads this field.')+'</div>'+
          '<textarea id="summaryBase" class="rtl-auto" dir="auto" style="min-height:220px" disabled>'+
            esc(draftOr("summaryBase", cfg("summary_base_prompt_override","") || (S.summary_base_prompt||"")))+
          '</textarea>'+
          '<div class="btnrow"><button class="btn" data-unlock-base="1">&#128274; Unlock to edit</button>'+
            '<button class="btn primary" data-save-base="1" style="display:none">Save</button>'+
            '<button class="btn ghost" data-reset-base="1">Reset to default</button>'+
            (baseOverride?'<span class="note">manual override active</span>':'')+
          '</div>')+
        more('addendum', 'Additional rules', 'Appended after the base prompt. Empty or unchanged = the built-in text.',
          stateCur(addOverride),
          '<textarea id="summaryAddendum" class="rtl-auto" dir="auto" style="min-height:240px">'+
            esc(draftOr("summaryAddendum", cfg("summary_cloud_addendum","") || (S.summary_addendum_default||"")))+
          '</textarea>'+
          '<div class="btnrow"><button class="btn primary" data-save-addendum="1">Save</button>'+
            '<button class="btn ghost" data-reset-addendum="1">Reset to default</button>'+
            (addOverride?'<span class="note">manual override active</span>':'')+
          '</div>'),
        {sub: 'OpenAI and Gemini only'})+
      grp('System',
        srow('Share the GPU with another local app',
          T('Both apps take turns for heavy jobs through <span class="mono">C:\\ProgramData\\GpuLease</span>. Off if Lia is the only GPU app.'),
          swb(!!cfg("gpu_lease_enabled", false), "toggle_gpu_lease"))+
        srow('Keep dictation clips for tuning',
          T('Your last '+esc(cfg("debug_clips_ring", 100))+' clips (audio + text) in <span class="mono">debug_clips</span>; stays on this PC.'),
          swb(!!cfg("debug_keep_dictation_clips", false), "toggle_debug_capture")+
          btn("Open clips folder","open_debug_clips_dir",[],"ghost"))
      )+
      grp('Privacy and data',
        srow('Delete all my data',
          'Recordings (WAV ~30 days, Opus ~2 years), transcripts, summaries, history, indexes and settings in the config folder.',
          '<button class="btn danger" data-wipe="1">Delete all my data…</button>'),
        {tone: 'danger'});
  };

  // ---------- render ----------
  function setStatus(){
    var rec = !!(S.recording || S.meeting_active), busy = !!S.busy;
    var bs = $("brandState"); if(bs) bs.textContent = S.status_line || "Ready";
    var rd = $("brandRec");
    if(rd){ rd.hidden = !rec; if(rec && !rd.style.getPropertyValue("--d")) rd.style.setProperty("--d", RK.ad(1.6667)); }
    var orb = $("brandOrb");
    if(orb && orb.classList.contains("busy") !== busy){
      if(busy) orb.style.setProperty("--d", RK.ad(14));
      orb.classList.toggle("busy", busy);
    }
    var el = $("hdrStatus");
    if(!el || el.classList.contains("flash")) return;
    el.className = "status " + (rec || busy ? "busy" : "ok");
    el.innerHTML = '<span class="dot"></span>'+esc(S.status_line||"Ready");
  }

  // In-place save/result confirmation: after a real action result, the header
  // briefly shows the ACTUAL message (a check for success, a warning for a
  // failure) right by the settings, then reverts. The corner toast stays as the
  // secondary channel. Most settings apply on change, so this is their "saved".
  var _flashTimer = null;
  function flashStatus(msg, ok){
    var el = $("hdrStatus"); if(!el || !msg) return;
    el.className = "status flash " + (ok ? "ok" : "warn");
    el.innerHTML = '<span class="dot"></span>' + (ok ? '✓ ' : '') + esc(msg);
    if(_flashTimer) clearTimeout(_flashTimer);
    _flashTimer = setTimeout(function(){ el.classList.remove("flash"); setStatus(); }, 2200);
  }

  var KEEP_LISTS = ["snipList", "pendingList", "droppedList", "learnedTools", "learnedList",
                    "corrList", "oovList"];
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
    var home = (PAGE==="home");
    content.classList.toggle("home-mode", home);
    // The Home page owns its own header/status; other pages get the sticky pill.
    var pill = home ? '' :
      '<div class="hdr-pill"><span class="status ok" id="hdrStatus"><span class="dot"></span>Ready</span></div>';
    // Lists filled on demand (snippets, vocabulary) live outside the state, so
    // a re-render would empty them: carry the loaded nodes over as they are.
    var kept = {};
    KEEP_LISTS.forEach(function(id){
      var el = $(id); if(el && el.childNodes.length && !el.hidden) kept[id] = el;
    });
    content.innerHTML = pill + fn();
    Object.keys(kept).forEach(function(id){ var ph = $(id); if(ph) ph.replaceWith(kept[id]); });
    content.querySelectorAll('details[open][data-k]').forEach(function(d){
      var f = ONOPEN[d.getAttribute('data-k')]; if(f){ d.__hooked = true; try{ f(); }catch(_){} }
    });
    if(ONPAGE[PAGE]){ try{ ONPAGE[PAGE](); }catch(_){} }
    setStatus();
    RK.segThumbs(content);
    content.scrollTop = sc;
    document.querySelectorAll('.nav-item').forEach(function(b){
      b.classList.toggle('on', b.getAttribute('data-page')===PAGE);
    });
    if(aid){ var re=document.getElementById(aid); if(re){ try{ re.focus();
      if(asel && re.setSelectionRange) re.setSelectionRange(asel[0],asel[1]); }catch(e){} } }
    // Home: load the selected meeting's summary into the side preview (wide only).
    if(home && !S.meeting_active && homeSel && !homeIsNarrow()) loadHomePreview(homeSel);
    if(home) MTG_ROSE = true;
  }

  // A meeting summary (markdown) as safe HTML for the preview: every line is
  // escaped FIRST; "## x" -> a heading, "- [ ] x" / "- [x] x" -> a task,
  // "- x" -> a bullet, **x** -> bold. Nothing else is interpreted.
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
        out.push('<li class="task'+(m[1]===' '?'':' done')+'" dir="auto"><span class="tb" aria-hidden="true"></span>'+inl(m[2])+'</li>'); return; }
      if((m = /^[-*•]\s+(.*)$/.exec(l))){
        if(!list){ out.push('<ul>'); list = true; }
        out.push('<li dir="auto">'+inl(m[1])+'</li>'); return; }
      close(); out.push('<p dir="auto">'+inl(l)+'</p>');
    });
    close();
    return out.join('');
  }

  // ---- Home: meetings preview (wide window) ----
  function loadHomePreview(path){
    var pv = $("mtgPreview"); if(!pv) return;
    var m = homeMeetings().filter(function(x){ return x.path===path; })[0];
    var title = m ? m.title : "Meeting";
    pv.classList.remove("empty");
    pv.innerHTML = '<div class="pv-title rtl-auto" dir="auto">'+esc(title)+'</div>'+
      '<div class="pv-sub">Loading…</div>';
    call("get_meeting_details", [path]).then(function(r){
      if($("mtgPreview")!==pv) return;              // selection changed meanwhile
      if(homeSel!==path) return;
      var d = (r && r.data) || {};
      var pj = JSON.stringify([path]);
      var acts = '<div class="pv-actions">';
      if(d.has_summary){
        acts += '<button class="btn primary" data-call="open_meeting_path" data-args=\''+esc(pj)+'\'>'+RK.icon('edit',{size:15})+' Edit summary</button>'+
                '<button class="btn" data-call="copy_meeting_summary" data-args=\''+esc(pj)+'\'>'+RK.icon('copy',{size:15})+' Copy summary</button>';
      }
      acts += '<button class="btn" data-call="open_meeting_transcript" data-args=\''+esc(pj)+'\'>'+RK.icon('file',{size:15})+' Open transcript</button>';
      acts += '<button class="btn" data-call="rename_meeting_for" data-args=\''+esc(pj)+'\'>'+RK.icon('edit',{size:15})+' Rename</button>';
      if(d.diarized)
        acts += '<button class="btn" data-call="rename_speakers_for" data-args=\''+esc(pj)+'\'>'+RK.icon('meetings',{size:15})+' Rename speakers</button>';
      acts += '</div>';
      var bodyHtml = d.has_summary
        ? '<div class="pv-body pv-doc">'+summaryHtml(d.summary_text||"")+'</div>'
        : '<div class="pv-body pv-none">No summary for this meeting - transcript only. Use Open transcript to read it.</div>';
      pv.innerHTML = '<div class="pv-title rtl-auto" dir="auto">'+esc(title)+'</div>'+
        '<div class="pv-sub"><span>'+whenFull(d.date||(m&&m.date)||"")+'</span>'+(d.diarized?' · <span>speakers</span>':'')+'</div>'+
        acts + bodyHtml;
    });
  }

  // ---- Home: filter the meetings rows in place (no full re-render on keystroke) ----
  function renderHomeRows(){
    var box = $("mtgRows"); if(!box) return;
    var rows = homeFiltered(), all = homeMeetings();
    if(rows.length && (homeSel==null || !rows.some(function(m){return m.path===homeSel;})))
      homeSel = rows[0].path;
    var cnt = $("mtgCount"); if(cnt) cnt.textContent = rows.length;
    box.innerHTML = rows.length ? rows.map(function(m, i){ return mtgRow(m, i, true); }).join('') : mtgEmptyHtml(all);
    if(homeSel && !homeIsNarrow()) loadHomePreview(homeSel);
  }

  // ---- Home: no rows - nothing matched the search, or a first run ----
  function mtgEmptyHtml(all){
    if(all.length) return '<div class="mtg-empty">No meetings match “'+esc(homeSearch)+'”.</div>';
    return '<div class="mtg-empty first">'+RK.orb('')+'<b>No meetings yet</b>'+
      '<span>Start one above - it appears here once saved.</span></div>';
  }

  function go(page){ PAGE = page; render(); }

  // ---------- settings search (local, offline; no field values indexed) -----
  var NAV_ICON = {general:'general', audio:'audio', meetings:'meetings', models:'models',
    cleanup:'cleanup', vocab:'vocabulary', snippets:'snippets', keys:'keys', server:'server',
    advanced:'advanced', home:'home'};
  var PAGE_LABEL = {general:'General', audio:'Audio', meetings:'Meetings',
    models:'Models', cleanup:'AI Cleanup', vocab:'Vocabulary', snippets:'Snippets',
    keys:'API Keys', server:'Transcription server', advanced:'Advanced'};
  // Each entry: t=title, k=keywords (he+en aliases), p=page id, f=text to locate
  // on that page (a section title / label; "" = just open the page). No secrets,
  // no field values - only static labels.
  var SEARCH_INDEX = [
    {t:"Press-to-talk hotkey", k:"hotkey shortcut key dictation קיצור מקש דיבור הכתבה", p:"general", f:"Press-to-talk hotkey"},
    {t:"Recording mode", k:"recording hold toggle מצב הקלטה", p:"general", f:"Recording mode"},
    {t:"Paste mode", k:"paste clipboard enter הדבקה לוח", p:"general", f:"After transcription"},
    {t:"Start with Windows", k:"startup autostart boot launch הפעלה אתחול", p:"general", f:"Start with Windows"},
    {t:"Interface language", k:"interface ui language hebrew english rtl ממשק שפת ממשק עברית אנגלית", p:"general", f:"Interface language"},
    {t:"Primary & speech languages", k:"language primary speech hebrew english שפה ראשית שפות דיבור עברית אנגלית", p:"general", f:"Primary language"},
    {t:"History retention", k:"history keep clear delete היסטוריה שמירה מחיקה", p:"general", f:"Keep dictation history"},
    {t:"Invisible / silent mode", k:"invisible silent overlay waveform שקט מצב בלתי נראה", p:"general", f:"Invisible mode"},
    {t:"Microphone device", k:"microphone mic input device מיקרופון התקן קלט", p:"audio", f:"Dictation microphone"},
    {t:"System audio", k:"system audio loopback שמע מערכת", p:"audio", f:"System audio device"},
    {t:"Beep / sounds", k:"beep sound chime צליל ביפ", p:"audio", f:"Done beep"},
    {t:"Dictation model", k:"dictation model engine הכתבה מודל מנוע", p:"models", f:"Dictation model"},
    {t:"Meeting model", k:"meeting model engine פגישות מודל", p:"models", f:"Meeting transcription model"},
    {t:"Summary model", k:"summary model סיכום מודל", p:"models", f:"Meeting summary model"},
    {t:"Restore clipboard after paste", k:"clipboard restore paste שחזור לוח הדבקה", p:"general", f:"Restore clipboard"},
    {t:"Press Enter after paste", k:"enter send paste אנטר שליחה", p:"general", f:"Press Enter after paste"},
    {t:"Languages I use", k:"languages detect enabled שפות זיהוי", p:"general", f:"Languages I use"},
    {t:"Lock to one language", k:"lock language force נעילת שפה", p:"general", f:"Lock to one language"},
    {t:"Keyboard shortcuts", k:"shortcuts hotkeys keys קיצורי מקלדת", p:"general", f:"All shortcuts"},
    {t:"Transcribe-file model", k:"file transcribe model קובץ תמלול מודל", p:"models", f:"Transcribe-file model"},
    {t:"When the GPU is busy", k:"gpu busy fallback cloud כרטיס מסך תפוס ענן", p:"models", f:"When the GPU is busy"},
    {t:"Local Whisper device (GPU / CPU)", k:"gpu cpu device whisper cuda מעבד כרטיס מסך", p:"models", f:"Local Whisper device"},
    {t:"Meeting microphone", k:"meeting mic headset microphone מיקרופון פגישה אוזניות", p:"audio", f:"Meeting microphone"},
    {t:"Record my microphone / system audio", k:"record source mic system מקור הקלטה", p:"audio", f:"Record my microphone"},
    {t:"AI cleanup provider and model", k:"cleanup model provider ניקוי מודל ספק", p:"cleanup", f:"Provider and model"},
    {t:"AI cleanup style", k:"cleanup polish style proofread ניקוי סגנון עריכה הגהה", p:"cleanup", f:"Style"},
    {t:"OpenAI / Groq / Gemini keys", k:"api key openai groq gemini token מפתח", p:"keys", f:"OpenAI"},
    {t:"Home server (client)", k:"home server serve remote client שרת בית מרוחק לקוח", p:"server", f:"This PC"},
    {t:"Auto-detect meetings", k:"auto detect meetings zoom teams meet זיהוי פגישות", p:"meetings", f:"Auto-detect"},
    {t:"Live transcript interval", k:"live transcript interval chunk realtime תמלול חי מקצב מרווח", p:"meetings", f:"Live transcript updates"},
    {t:"Summary template", k:"summary template technical minutes general תבנית סיכום", p:"meetings", f:"Template"},
    {t:"Summary language", k:"summary language שפת סיכום", p:"meetings", f:"Summary language"},
    {t:"Vocabulary terms", k:"vocabulary terms glossary מילון מונחים אוצר מילים", p:"vocab", f:"Manual terms"},
    {t:"Corrections", k:"corrections fix replace תיקונים", p:"vocab", f:"Corrections"},
    {t:"Vocabulary suggestions", k:"suggestions learn terms הצעות מונחים", p:"vocab", f:"Suggestions"},
    {t:"Learned terms", k:"learned terms hints נלמדו מונחים", p:"vocab", f:"Learned terms"},
    {t:"Hebrew spelling guard", k:"hebrew spelling lexicon hspell plural איות עברית רבים", p:"vocab", f:"Hebrew dictionary"},
    {t:"Snippets", k:"snippets expansion abbreviation קיצורים הרחבה", p:"snippets", f:""},
    {t:"Groq key", k:"groq key api מפתח", p:"keys", f:"Groq"},
    {t:"Gemini key", k:"gemini google key api מפתח", p:"keys", f:"Gemini"},
    {t:"Hugging Face token (local diarization)", k:"hugging face hf token pyannote diarization דוברים", p:"keys", f:"Local Diarization"},
    {t:"Host a server on this GPU", k:"host serve server gpu port token אירוח שרת", p:"server", f:"This PC"},
    {t:"Server listen address / port / token", k:"listen port token bind tailscale פורט טוקן", p:"server", f:"Listen on"},
    {t:"Transcription server (client / host)", k:"transcription server serve client host שרת תמלול", p:"server", f:""},
    {t:"Restart / open log", k:"restart log logs quit about אתחול יומן לוג", p:"advanced", f:"Restart Lia"},
    {t:"Keep raw meeting tracks", k:"raw tracks mixdown backup safety רצועות גיבוי", p:"meetings", f:"Keep raw per-source tracks"},
    {t:"Backup mic / auto-switch mic", k:"backup mic fallback switch מיקרופון גיבוי", p:"meetings", f:"Backup mic"},
    {t:"Your name (speaker)", k:"name speaker self שם דובר", p:"meetings", f:"Your name"},
    {t:"Known voices", k:"voices speakers forget קולות דוברים", p:"meetings", f:"Known voices"},
    {t:"Voice ask answer output", k:"voice ask answer paste card שאלה קולית תשובה", p:"meetings", f:"Voice ask answer"},
    {t:"Meeting tools (action items, rename, transcribe)", k:"tools action items rename speakers folder כלים משימות", p:"meetings", f:"Action items"},
    {t:"Local summary passes (tasks, depth, coverage)", k:"gemma ollama depth coverage tasks pass סיכום מקומי", p:"advanced", f:"Local summary passes"},
    {t:"Cloud summary prompt", k:"prompt base rules addendum פרומפט הנחיות", p:"advanced", f:"Base prompt"},
    {t:"Check for updates", k:"update version upgrade עדכון גרסה", p:"advanced", f:"Check for updates"},
    {t:"GPU sharing", k:"gpu lease share שיתוף כרטיס מסך", p:"advanced", f:"Share the GPU"},
    {t:"Keep dictation clips", k:"debug clips diagnostics קליפים אבחון", p:"advanced", f:"Keep dictation clips"},
    {t:"Delete all my data", k:"delete wipe data privacy מחיקה נתונים פרטיות", p:"advanced", f:"Delete all my data"},
    {t:"Report a problem", k:"report bug problem issue דיווח תקלה", p:"advanced", f:"Report a problem"},
  ];

  // Open a page's control by a short name: an API Keys provider ("groq",
  // "openai", "gemini", "hf") opens that provider's row and focuses its key
  // input; "remote" focuses the client Server URL.
  function focusControl(f){
    var el = null;
    if(PAGE==="keys"){
      OPENX["key_"+f] = true; render();
      el = $("in_"+f);
    } else if(PAGE==="server" && f==="remote"){
      el = $("in_remote_url");
    }
    if(!el) return;
    try{ el.scrollIntoView({block:'center'}); }catch(_){}
    try{ el.focus({preventScroll:true}); }catch(_){ try{ el.focus(); }catch(__){} }
  }

  function searchHide(){ var b=$("searchResults"); if(b){ b.hidden=true; b.innerHTML=''; } }
  function searchSetHi(items, idx){
    items.forEach(function(it,i){ it.classList.toggle('hi', i===idx); });
    if(items[idx]) items[idx].scrollIntoView({block:'nearest'});
  }
  function searchRun(q){
    var box = $("searchResults"); if(!box) return;
    q = (q||'').trim().toLowerCase();
    if(!q){ searchHide(); return; }
    var toks = q.split(/\s+/);
    var hits = SEARCH_INDEX.filter(function(e){
      var hay = (e.t+' '+T(e.t)+' '+e.k+' '+(PAGE_LABEL[e.p]||'')+' '+T(PAGE_LABEL[e.p]||'')).toLowerCase();
      return toks.every(function(t){ return hay.indexOf(t)>=0; });
    }).slice(0,12);
    if(!hits.length){ box.innerHTML='<div class="sr-none">No settings found</div>'; box.hidden=false; return; }
    box.innerHTML = hits.map(function(e,i){
      return '<button class="sr'+(i===0?' hi':'')+'" data-sr-page="'+RK.esc(e.p)+
        '" data-sr-find="'+RK.esc(e.f||'')+'">'+RK.esc(e.t)+
        '<span class="sr-page">'+RK.esc(PAGE_LABEL[e.p]||e.p)+'</span></button>';
    }).join('');
    box.hidden = false;
  }
  function jumpTo(page, find){
    go(page);   // render() populates #content synchronously, so the DOM is ready
    var content = $("content"); if(!content) return;
    var target = null;
    if(find){
      // the page may still be English for a moment (the interface translator
      // runs right after this render) or already Hebrew: match either form
      var needle = find.toLowerCase(), needle2 = T(find).toLowerCase();
      // row titles first (compact layout), then group labels, then the old
      // card-era selectors for pages not yet moved to rows
      var sels = ['.srow-t', '.grp-h', '.arow',
                  '.section-title,.field-label,.field>label,.subhead,.switch,.row-inline,.rowbtn'];
      for(var si=0; si<sels.length && !target; si++){
        var nodes = content.querySelectorAll(sels[si]);
        for(var i=0;i<nodes.length;i++){
          var tx = (nodes[i].textContent||'').toLowerCase();
          if(tx.indexOf(needle)>=0 || tx.indexOf(needle2)>=0){ target = nodes[i]; break; }
        }
      }
      // a row inside a closed picker: open it (and remember that) first
      var dd = target && target.closest('details');
      while(dd){
        if(!dd.open){ dd.open = true; var dk = dd.getAttribute('data-k'); if(dk) OPENX[dk] = true; }
        dd = dd.parentElement && dd.parentElement.closest('details');
      }
    }
    var card = target ? (target.closest('.srow, .arow, .grp, .page') || target)
                      : (content.querySelector('.content-head') || content.firstElementChild);
    if(card && card.classList.contains('grp-h') && card.nextElementSibling) card = card.nextElementSibling;
    if(!card) return;
    try{ card.scrollIntoView({block:'center', behavior:'smooth'}); }catch(e){ try{ card.scrollIntoView(); }catch(_){} }
    card.classList.remove('setting-flash'); void card.offsetWidth; card.classList.add('setting-flash');
    var focusable = card.querySelector('input:not([type=hidden]),select,textarea,button');
    if(focusable){ try{ focusable.focus({preventScroll:true}); }catch(e){ try{ focusable.focus(); }catch(_){} } }
  }

  // ---------- Ctrl+K command palette ----------
  // Groups: Actions (existing data-call methods only), Pages (the sidebar),
  // Settings (SEARCH_INDEX + jumpTo), Meetings (the Home list). Max 12 results.
  var PAL = null;   // {q, sel}
  var PAL_ACTIONS = [
    {ic:'meetings', t:'Start meeting', m:'home_start_meeting', when:function(){ return !S.meeting_active; }},
    {ic:'file', t:'Transcribe a file', m:'transcribe_file'},
    {ic:'summarize', t:'Summarize a meeting file', m:'summarize_text_dialog'},
    {ic:'history', t:'Dictation history', m:'show_history'},
    {ic:'folder', t:'Open meetings folder', m:'open_meetings_folder'}
  ];
  function palMatch(hay, toks){ hay = (hay||'').toLowerCase();
    return toks.every(function(t){ return hay.indexOf(t) >= 0; }); }
  function palItems(){
    var q = ((PAL && PAL.q) || '').trim().toLowerCase(), toks = q ? q.split(/\s+/) : [], out = [];
    PAL_ACTIONS.forEach(function(a){
      if((!a.when || a.when()) && palMatch(a.t+' '+T(a.t), toks))
        out.push({g:'Actions', ic:a.ic, t:a.t, go:function(){ call(a.m, []); }});
    });
    var pages = [['home','home','Home']].concat(Object.keys(PAGE_LABEL).map(function(k){
      return [k, NAV_ICON[k] || 'settings', PAGE_LABEL[k]]; }));
    pages.forEach(function(pg){
      if(toks.length && palMatch(pg[2]+' '+T(pg[2]), toks))
        out.push({g:'Pages', ic:pg[1], t:pg[2], go:function(){ go(pg[0]); }});
    });
    if(toks.length){
      SEARCH_INDEX.forEach(function(e){
        if(palMatch(e.t+' '+T(e.t)+' '+e.k+' '+(PAGE_LABEL[e.p]||'')+' '+T(PAGE_LABEL[e.p]||''), toks))
          out.push({g:'Settings', ic:NAV_ICON[e.p] || 'settings', t:e.t, sub:PAGE_LABEL[e.p]||e.p,
                    go:function(){ jumpTo(e.p, e.f); }});
      });
      homeMeetings().forEach(function(m){
        if(palMatch(m.title, toks))
          out.push({g:'Meetings', ic:'meetings', t:m.title, sub:whenWords(m.date), rtl:true,
                    go:function(){ homeSel = m.path; homeSearch = ''; go('home'); }});
      });
    }
    return out.slice(0, 12);
  }
  function palRender(keepInput){
    var host = $("pal"); if(!host) return;
    if(!PAL){ host.innerHTML = ''; return; }
    var items = palItems(), last = '';
    PAL.sel = Math.max(0, Math.min(PAL.sel, items.length-1));
    var list = items.map(function(it, i){
      var head = it.g !== last ? '<li class="pal-group" role="presentation">'+esc(it.g)+'</li>' : '';
      last = it.g;
      return head + '<li class="pi'+(i===PAL.sel?' on':'')+'" role="option" data-pi="'+i+'">'+
        '<span class="pic">'+RK.icon(it.ic,{size:16})+'</span>'+
        '<span class="pt"'+(it.rtl?' dir="auto"':'')+'>'+esc(it.t)+'</span>'+
        (it.sub ? '<span class="ps">'+esc(it.sub)+'</span>' : '')+'</li>';
    }).join('');
    if(!items.length) list = '<li class="pal-empty">Nothing found</li>';
    if(keepInput && host.querySelector('.pal-list')){ host.querySelector('.pal-list').innerHTML = list; return; }
    host.innerHTML = '<div class="pal-bg"><div class="pal" role="dialog" aria-label="Go to">'+
      '<div class="pal-in">'+RK.icon('search',{size:18})+
      '<input type="text" id="palIn" dir="auto" autocomplete="off" spellcheck="false" '+
      'placeholder="Search actions, settings and meetings" value="'+esc(PAL.q||'')+'">'+
      '<span class="kbd">Esc</span></div><ul class="pal-list" role="listbox">'+list+'</ul></div></div>';
    var inp = $("palIn"); if(inp){ inp.focus(); try{ inp.setSelectionRange(inp.value.length, inp.value.length); }catch(_){} }
  }
  function palOpen(){ searchHide(); PAL = {q:'', sel:0}; palRender(); }
  function palClose(){ PAL = null; palRender(); }
  function palGo(i){ var it = palItems()[i]; palClose(); if(it) it.go(); }
  document.addEventListener('keydown', function(e){
    if((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.code==='KeyK' || e.key==='k' || e.key==='K')){
      e.preventDefault(); if(PAL) palClose(); else palOpen(); return; }
    if(!PAL) return;
    if(e.key==='Escape'){ e.preventDefault(); e.stopPropagation(); palClose(); return; }
    if(e.key==='ArrowDown' || e.key==='ArrowUp'){ e.preventDefault(); e.stopPropagation();
      PAL.sel += (e.key==='ArrowDown' ? 1 : -1); palRender(true);
      var on = document.querySelector('.pal-list li.on'); if(on) on.scrollIntoView({block:'nearest'}); return; }
    if(e.key==='Enter'){ e.preventDefault(); e.stopPropagation(); palGo(PAL.sel); }
  }, true);
  document.addEventListener('input', function(e){
    if(PAL && e.target.id==='palIn'){ PAL.q = e.target.value; PAL.sel = 0; palRender(true); } }, true);
  document.addEventListener('mousedown', function(e){
    if(!PAL) return;
    var li = e.target.closest('.pal-list li.pi'); if(li){ e.preventDefault(); palGo(+li.getAttribute('data-pi')); return; }
    if(!e.target.closest('.pal')) palClose();
  }, true);

  // ---------- Lia arrives: the orb gathers from three petals the first time
  //            this window is actually SEEN (a pre-warmed window is hidden) ----
  var ARRIVED = false;
  function arrive(){
    if(ARRIVED || document.hidden) return;
    ARRIVED = true;
    var o = $("brandOrb"); if(!o || RK.reduce) return;
    o.classList.add("arrive");
    setTimeout(function(){ o.classList.remove("arrive"); }, 1400);
  }
  document.addEventListener("visibilitychange", arrive);
  // not the active window -> the orb and the recording dot rest (ui_kit:
  // html.blurred); a Settings window left open behind other apps costs ~0
  function syncBlurred(){ document.documentElement.classList.toggle("blurred", !document.hasFocus()); }
  window.addEventListener("focus", syncBlurred); window.addEventListener("blur", syncBlurred);
  syncBlurred();

  // ---------- push handling ----------
  RK.onPush(function(msg){
    var t = msg.t;
    if(t==="tick"){
      var liveChanged = (S.live_transcript_available !== msg.live_transcript_available);
      var meetingChanged = (S.meeting_active !== msg.meeting_active);
      S.status_line = msg.status_line; S.recording = msg.recording;
      S.meeting_active = msg.meeting_active; S.live_transcript_available = msg.live_transcript_available;
      if("busy" in msg) S.busy = msg.busy;
      setStatus();
      // the Meetings page shows a live-transcript affordance; the Home page shows
      // the whole meeting-active hero + actions - re-render either when relevant.
      if((liveChanged || meetingChanged) && PAGE==="meetings") render();
      if(meetingChanged && PAGE==="home") render();
      return;
    }
    if(t==="state"){ S = msg.state || S; render(); return; }
    if(t==="result"){
      var p = pending[msg.id]; if(p){ delete pending[msg.id]; p.res(msg); }
      if(msg.msg){ RK.toast(msg.msg, msg.ok?"ok":"err"); flashStatus(msg.msg, msg.ok); }
      return;
    }
    if(t==="toast"){ RK.toast(msg.msg, msg.level); return; }
    if(t==="focus"){ if(msg.page){ go(msg.page); } if(msg.focus){ focusControl(msg.focus); } return; }
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
    // a click anywhere outside the search box dismisses the results
    if(!e.target.closest('.nav-search')) searchHide();
    // Home overflow menu: toggle on its button, close on an item or outside click
    if(e.target.closest('#homeMenuBtn')){
      var hm=$("homeMenu"); if(hm) hm.classList.toggle('open'); return; }
    if(e.target.closest('.home-menu-pop button')){
      var hmo=$("homeMenu"); if(hmo) hmo.classList.remove('open'); }  // then fall through to data-call
    else if(!e.target.closest('#homeMenu')){
      var hmc=$("homeMenu"); if(hmc) hmc.classList.remove('open'); }
    // Home meeting row: WIDE -> select + side preview; NARROW -> open the editor
    var mr = e.target.closest('.mtg-row');
    if(mr){
      var mp = mr.getAttribute('data-mtg'); homeSel = mp;
      if(homeIsNarrow()){ call('open_meeting_path',[mp]); return; }
      var box=$("mtgRows");
      if(box) box.querySelectorAll('.mtg-row').forEach(function(b){ b.classList.toggle('sel', b===mr); });
      loadHomePreview(mp); return;
    }
    var sr = e.target.closest('[data-sr-page]');
    if(sr){ var si=$("settingsSearch"); if(si) si.value=''; searchHide();
      jumpTo(sr.getAttribute('data-sr-page'), sr.getAttribute('data-sr-find')); return; }
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
      if(m==="__load_pending"){ loadPending(); return; }
      if(m==="__load_learned"){ loadLearned(); return; }
      if(m==="__load_dropped"){ loadDropped(); return; }
      if(m==="__load_corr"){ loadCorr(); return; }
      if(m==="__scan_corr"){ busy(cb,true); call("vocab_corrections_scan",[],true).then(function(r){ unbusy(cb); renderCorr(r.data||[]); }); return; }
      if(m==="harvest_corrections_now"){ busy(cb,true); call(m,args,true).then(function(){ unbusy(cb); loadCorr(); }); return; }
      if(m==="__load_snips"){ loadSnips(); return; }
      if(m==="__load_oov"){ loadOov(); return; }
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
    var gtk = e.target.closest('[data-gen-token]');
    if(gtk){ busy(gtk,true); call("gen_serve_token",[],true).then(function(r){ unbusy(gtk);
        var st=document.getElementById("st_serve");
        if(r.ok){ var inp=document.getElementById("in_serve_tok"); if(inp){ inp.type="text"; inp.value=r.msg; }
          if(st) st.textContent=T("New token generated and copied to the clipboard - paste it into the client's Access token on your other device."); }
        else if(st){ st.textContent=r.msg||""; } }); return; }
    var ctk = e.target.closest('[data-copy-token]');
    if(ctk){ busy(ctk,true); call("copy_serve_token",[],true).then(function(r){ unbusy(ctk);
        var st=document.getElementById("st_serve"); if(st) st.textContent=r.msg||""; }); return; }
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
    var ssn = e.target.closest('[data-save-selfname]');
    if(ssn){ var nv=(document.getElementById("selfName")||{}).value||"";
      call("set_speaker_self_name",[nv]).then(function(r){ if(r.ok) delete DRAFT["selfName"]; }); return; }
    var rv = e.target.closest('[data-reset-vocab]');
    if(rv){ if(!confirm("Replace the terms above with the shipped default set?")) return;
      call("reset_vocab_default",[]).then(function(r){ if(r.ok) delete DRAFT["vocabText"]; }); return; }
    var sa = e.target.closest('[data-save-addendum]');
    if(sa){ var at=(document.getElementById("summaryAddendum")||{}).value||"";
      call("save_summary_addendum",[at]).then(function(r){ if(r.ok) delete DRAFT["summaryAddendum"]; }); return; }
    var ra = e.target.closest('[data-reset-addendum]');
    if(ra){ if(!confirm("Replace the text above with the built-in default?")) return;
      call("reset_summary_addendum",[]).then(function(r){ if(r.ok) delete DRAFT["summaryAddendum"]; }); return; }
    var ulb = e.target.closest('[data-unlock-base]');
    if(ulb){ if(ulb.dataset.on==="1") return;
      if(!confirm("Editing the base prompt can cause unexpected changes to your OpenAI / Gemini "+
        "summaries - wrong headers, a broken format, or lost faithfulness. It does NOT affect the "+
        "local Gemma summary. Continue?")) return;
      var bt=document.getElementById("summaryBase"); if(bt){ bt.disabled=false; bt.focus(); }
      ulb.dataset.on="1"; ulb.textContent="🔓 Editing enabled";
      var sbb=document.querySelector('[data-save-base]'); if(sbb) sbb.style.display=""; return; }
    var sb2 = e.target.closest('[data-save-base]');
    if(sb2){ var bv=(document.getElementById("summaryBase")||{}).value||"";
      call("save_summary_base_prompt",[bv]).then(function(r){ if(r.ok) delete DRAFT["summaryBase"]; }); return; }
    var rb2 = e.target.closest('[data-reset-base]');
    if(rb2){ if(!confirm("Replace the base prompt with the built-in default?")) return;
      call("reset_summary_base_prompt",[]).then(function(r){ if(r.ok) delete DRAFT["summaryBase"]; }); return; }
    var va = e.target.closest('[data-vocab-apply]');
    if(va){ applyPending(); return; }
    var vdr = e.target.closest('[data-vocab-drop-rest]');
    if(vdr){ dropUntouched(); return; }
    var vro = e.target.closest('[data-reopen]');
    if(vro){ call("vocab_reopen",[[vro.getAttribute('data-reopen')]]).then(function(){ loadDropped(); loadPending(); }); return; }
    var vlt = e.target.closest('[data-learned-tool]');
    if(vlt){ learnedTool(vlt.getAttribute('data-learned-tool')); return; }
    var vr = e.target.closest('[data-vocab-remove]');
    if(vr){ removeLearned(); return; }
    var ac = e.target.closest('[data-add-corr]');
    if(ac){ var w=(document.getElementById("corrWrong")||{}).value||"";
      var ri=(document.getElementById("corrRight")||{}).value||"";
      call("vocab_add_correction",[w,ri]).then(function(r){ if(r.ok){ loadCorr();
        var a=document.getElementById("corrWrong"); var b=document.getElementById("corrRight"); if(a)a.value=""; if(b)b.value=""; } }); return; }
    var dc = e.target.closest('[data-del-corr]');
    if(dc){ call("vocab_remove_correction",[dc.getAttribute('data-del-corr')]).then(loadCorr); return; }
    var oa = e.target.closest('[data-oov-add]');
    if(oa){ var ow=oa.getAttribute('data-oov-add');
      var inp=document.querySelector('[data-oov-right="'+ow+'"]'); var ri=(inp&&inp.value||"").trim();
      if(!ri){ RK.toast("Type the correct spelling first","err"); return; }
      call("vocab_add_correction",[ow,ri]).then(function(r){ if(r.ok){ call("lexicon_oov_dismiss",[[ow]]).then(loadOov); } }); return; }
    var oaa = e.target.closest('[data-oov-accept-all]');
    if(oaa){ var lab=oaa.getAttribute('data-oov-accept-all');
      if(!confirm("Add all "+oaa.getAttribute('data-n')+" fixes learned from meetings to the corrections table? "+
                  "They will be applied to your dictation and meetings from now on.")) return;
      call("vocab_accept_suggestions",[lab]).then(function(r){
        RK.toast(r.msg||"", r.ok?"ok":"err"); loadOov(); loadCorr(); }); return; }
    var od = e.target.closest('[data-oov-dismiss]');
    if(od){ call("lexicon_oov_dismiss",[[od.getAttribute('data-oov-dismiss')]]).then(loadOov); return; }
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
    var ch = e.target.closest('[data-clear-history]');
    if(ch){ if(!confirm("Delete ALL transcription history?\n\nThis cannot be undone.")) return;
      call("clear_history",[]); return; }
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
      var typed = prompt(T("Type DELETE to confirm:"));
      if(((typed||"").trim().toUpperCase())!=="DELETE") return;
      call("delete_all_data",[],true); return;
    }
  });

  document.addEventListener('change', function(e){
    var t = e.target;
    if(t.matches('[data-lk]')){ LCHECK[t.getAttribute('data-lk')] = t.checked; learnedCount(); return; }
    if(t.id==="learnedSort"){ renderLearned(); return; }
    if(t.matches('[data-toggle]')){ call(t.getAttribute('data-toggle'),[]); return; }
    if(t.matches('[data-select]')){ call(t.getAttribute('data-select'),[parseInt(t.value,10)]); return; }
    if(t.matches('[data-select-str]')){ call(t.getAttribute('data-select-str'),[t.value]); return; }
    if(t.matches('[data-lang-enable]')){
      var codes=[]; document.querySelectorAll('[data-lang-enable]').forEach(function(x){
        if(x.checked) codes.push(x.getAttribute('data-lang-code')); });
      call('set_enabled_languages',[codes]); return;
    }
    if(t.matches('[data-lang-lock]')){
      var p=t.getAttribute('data-lang-primary')||'he';
      call('set_enabled_languages', t.checked ? [[p]] : [[p, p==='en'?'he':'en']]); return;
    }
    if(t.matches('[data-radio]')){ if(t.checked){ var m=t.getAttribute('data-radio'), a=argOf(t);
      call(m, m==="set_cleanup_provider_model"?a:[a], t.getAttribute('data-slow')==="1"); } return; }
  });

  document.addEventListener('input', function(e){
    var t = e.target;
    if(t.id==="mtgSearch"){ homeSearch = t.value; renderHomeRows(); return; }
    if(t.id==="learnedFilter"){ renderLearned(); return; }
    if(t.id==="langFilter"){
      var q=(t.value||"").toLowerCase();
      document.querySelectorAll('#langList .lang-item').forEach(function(el){
        var nm=el.getAttribute('data-lang-name')||"";
        el.style.display=(!q || nm.indexOf(q)>=0) ? '' : 'none';
      });
      return;
    }
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
          '<span class="txt rtl-auto" dir="auto">'+esc(k)+' <small class="i18n">'+esc(meta)+'</small></span></label>';
      }).join('');
    });
  }
  function checkedKeys(id){ return Array.prototype.slice.call(
    document.querySelectorAll('#'+id+' input[data-vk]')).filter(function(x){return x.checked;})
    .map(function(x){return x.getAttribute('data-vk');}); }
  function allKeys(id){ return Array.prototype.slice.call(
    document.querySelectorAll('#'+id+' input[data-vk]')).map(function(x){return x.getAttribute('data-vk');}); }
  // ---- Suggestions: tick = spelled right (add), typed spelling = heard wrong
  //      (fix), untouched = stays (2026-10-05: "Apply checked / reject rest"
  //      dropped every untouched term, also the misheard ones Naor meant to fix)
  function loadPending(){
    call("vocab_pending_list",[]).then(function(r){
      var data = r.data||[], el = document.getElementById("pendingList"); if(!el) return;
      var b = document.getElementById("pendingBadge"); if(b) b.textContent = data.length;
      if(!data.length){ el.innerHTML='<div class="hint">None.</div>'; return; }
      el.innerHTML = data.map(function(it){
        var k = it.term||"";
        return '<div class="sugg"><label class="check"><input type="checkbox" data-vk="'+esc(k)+'"><span class="box"></span>'+
          '<span class="txt rtl-auto" dir="auto">'+esc(k)+' <small>&#215;'+(it.count_corpus||0)+'</small></span></label>'+
          (it.sample ? '<div class="sugg-sample rtl-auto" dir="auto">&#8230;'+esc(it.sample)+'&#8230;</div>' : '')+
          '<input type="text" class="sugg-fix" data-fix="'+esc(k)+'" placeholder="Heard wrong? Type the right spelling" autocomplete="off" spellcheck="false"></div>';
      }).join('');
    });
  }
  function pendingChoices(){
    var approve = [], fixes = [], untouched = [];
    document.querySelectorAll('#pendingList .sugg').forEach(function(row){
      var cb = row.querySelector('input[data-vk]'), fx = row.querySelector('input[data-fix]');
      var k = cb.getAttribute('data-vk'), f = (fx && fx.value || "").trim();
      if(f) fixes.push([k, f]); else if(cb.checked) approve.push(k); else untouched.push(k);
    });
    return {approve:approve, fixes:fixes, untouched:untouched};
  }
  function applyPending(){
    var c = pendingChoices();
    if(!c.approve.length && !c.fixes.length){
      RK.toast("Tick a term, or type the right spelling for one Lia heard wrong", "err"); return; }
    var p = c.approve.length ? call("vocab_resolve",[c.approve,[]]) : Promise.resolve();
    p.then(function(){ return c.fixes.length ? call("vocab_fix_suggestions",[c.fixes]) : null; })
     .then(function(){ loadPending(); });
  }
  function dropUntouched(){
    var c = pendingChoices();
    if(!c.untouched.length){ RK.toast("Nothing untouched to drop", "ok"); return; }
    if(!confirm("Drop "+c.untouched.length+" untouched suggestion(s)? You can bring any back from Show dropped suggestions.")) return;
    call("vocab_resolve",[[],c.untouched]).then(function(){ loadPending(); });
  }
  function loadDropped(){
    call("vocab_dropped_list",[]).then(function(r){
      var data = r.data||[], el = document.getElementById("droppedList"); if(!el) return;
      if(!data.length){ el.innerHTML='<div class="hint">No dropped suggestions.</div>'; return; }
      el.innerHTML = '<div class="hint">Most recently dropped first.</div>'+data.map(function(it){
        var k = it.term||"";
        return '<div class="drop-row"><span class="txt rtl-auto" dir="auto">'+esc(k)+
          (it.fixed_to ? ' <small class="i18n">'+T('fixed to '+esc(it.fixed_to))+'</small>' : ' <small>&#215;'+(it.count_corpus||0)+'</small>')+'</span>'+
          '<button class="btn ghost sm" data-reopen="'+esc(k)+'">Back to suggestions</button></div>';
      }).join('');
    });
  }
  // ---- Learned terms: filter / sort / bulk checks (2026-10-05: 230 one-by-one
  //      boxes were unworkable). Checks survive filtering and re-sorting.
  var LEARNED = [], LCHECK = {};
  var GENERIC = {};
  ("cloud security network server production enterprise organization policy deployment agent "+
   "origin infrastructure governance inference distribution template local build front control "+
   "storage identity availability support web audit architecture central secret inbound outbound "+
   "coding studio workspace workspaces instance token compliance visual slash link links wait billing "+
   "findings router stack repository staging proxy incident report administrator execute target header "+
   "internal version app basic monthly bye limit monday review scale planning level artifacts core "+
   "capacity bucket dev pipeline workload inspection assessment teaching account accounts region regions "+
   "data service services platform portal access user users management environment system systems "+
   "solution project process team customer client cost costs session file files folder log logs key keys "+
   "group groups role roles rule rules zone zones domain domains endpoint endpoints backup monitoring "+
   "alert alerts dashboard migration integration testing test demo meeting update upgrade release feature "+
   "features issue issues ticket task tasks design model models engine connection connector tenant "+
   "subscription license partner vendor provider private public shared global standard premium free open "+
   "active default custom manual automatic auto online offline mobile desktop remote hybrid primary "+
   "secondary main center hub edge cluster node container image function layer object table query index "+
   "schema event events message queue topic notification email phone chat video voice audio text document "+
   "page site prod on-prem on-premise read-only real-time vibe firewall bot"
  ).split(" ").forEach(function(w){ if(w) GENERIC[w]=1; });
  function isGeneric(t){
    if(!t || /\s/.test(t) || /\d/.test(t)) return false;            // phrases / F5 / D9: specific
    if(t.length > 1 && t === t.toUpperCase()) return false;          // acronyms: API, DNS, KMS
    return t === t.toLowerCase() || !!GENERIC[t.toLowerCase()];
  }
  function loadLearned(){
    call("vocab_learned_list",[]).then(function(r){
      LEARNED = r.data||[]; LCHECK = {};
      var tools = document.getElementById("learnedTools"); if(tools) tools.hidden = false;
      var b = document.getElementById("learnedBadge"); if(b) b.textContent = LEARNED.length;
      renderLearned();
    });
  }
  function learnedShown(){
    var q = ((document.getElementById("learnedFilter")||{}).value||"").trim().toLowerCase();
    var sort = (document.getElementById("learnedSort")||{}).value||"used";
    var rows = LEARNED.filter(function(t){ return !q || (t.term||"").toLowerCase().indexOf(q)>=0; });
    rows.sort(function(a,b){
      if(sort==="az") return (a.term||"").localeCompare(b.term||"");
      var d = (a.count_used||0)-(b.count_used||0);
      return sort==="least" ? d : -d;
    });
    return rows;
  }
  function renderLearned(){
    var el = document.getElementById("learnedList"); if(!el) return;
    if(!LEARNED.length){ el.innerHTML='<div class="hint">None.</div>'; return; }
    var rows = learnedShown();
    el.innerHTML = rows.map(function(t){
      var k = t.term||"";
      return '<label class="check"><input type="checkbox" data-lk="'+esc(k)+'"'+(LCHECK[k]?' checked':'')+'><span class="box"></span>'+
        '<span class="txt rtl-auto" dir="auto">'+esc(k)+' <small class="i18n">used '+(t.count_used||0)+'</small></span></label>';
    }).join('') || '<div class="hint">No term matches the filter.</div>';
    learnedCount(rows.length);
  }
  function learnedCount(shown){
    var n = Object.keys(LCHECK).filter(function(k){ return LCHECK[k]; }).length;
    var c = document.getElementById("learnedCount");
    if(c) c.textContent = (shown!=null ? shown : learnedShown().length)+" shown · "+n+" checked";
  }
  function learnedTool(kind){
    if(kind==="clear"){ LCHECK = {}; }
    else if(kind==="shown"){ learnedShown().forEach(function(t){ LCHECK[t.term] = true; }); }
    else if(kind==="generic"){
      var n = 0; LEARNED.forEach(function(t){ if(isGeneric(t.term)){ LCHECK[t.term] = true; n++; } });
      RK.toast(n+" common word(s) checked - review them, then Remove checked", "ok");
    }
    renderLearned();
  }
  function removeLearned(){
    var keys = Object.keys(LCHECK).filter(function(k){ return LCHECK[k]; });
    if(!keys.length){ RK.toast("Nothing checked", "err"); return; }
    if(keys.length > 10 && !confirm("Remove "+keys.length+" learned term(s)? They stop being hints and the miner will not suggest them again.")) return;
    call("vocab_remove_learned",[keys]).then(function(){ loadLearned(); });
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
    return parts.map(function(x){ return '<span>'+x+'</span>'; }).join(' &middot; ');
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
        '<td class="rtl-auto" dir="auto">'+esc(c.wrong)+(/[\u0590-\u05FF]/.test(c.wrong) ? ' &#8592; ' : ' &#8594; ')+esc(c.right)+
          ' <small class="muted i18n">'+corrMeta(c)+'</small></td>'+
        '<td class="act"><button class="btn ghost sm" data-del-corr="'+esc(c.wrong)+'">&#128465;</button></td></tr>';
    }).join('')+'</table>';
  }
  function loadCorr(){ call("vocab_corrections_list",[]).then(function(r){ renderCorr(r.data||[]); }); }
  function renderOov(data){
    var el=document.getElementById("oovList"); if(!el) return;
    if(!data.length){ el.innerHTML='<div class="hint">No unknown words recorded yet.</div>'; return; }
    var nHarv=data.filter(function(o){ return o.label==="auto-harvest" && o.proposed; }).length;
    el.innerHTML=(nHarv ? '<div class="btnrow"><button class="btn sm" data-oov-accept-all="auto-harvest" data-n="'+nHarv+'">'+
        'Add all '+nHarv+' fixes learned from meetings</button></div>' : '')+
      '<table class="tbl">'+data.map(function(o){
      var w=esc(o.word);
      return '<tr>'+
        '<td class="rtl-auto" dir="auto">'+w+' <small class="muted i18n">&#215;'+(o.count||0)+
          (o.proposed ? ' &#183; '+T('proposed by the '+esc(o.label||"summary")) : '')+'</small></td>'+
        '<td><input type="text" class="mono" data-oov-right="'+w+'" value="'+esc(o.proposed||"")+'" placeholder="Fix to&#8230;" style="max-width:150px"></td>'+
        '<td class="act"><button class="btn sm" data-oov-add="'+w+'">Add</button> '+
          '<button class="btn ghost sm" data-oov-dismiss="'+w+'">Dismiss</button></td></tr>';
    }).join('')+'</table>';
  }
  function loadOov(){ call("lexicon_oov_list",[]).then(function(r){ renderOov(r.data||[]); }); }
  function loadSnips(){
    call("snippets_get",[]).then(function(r){
      var data=r.data||[]; var el=document.getElementById("snipList"); if(!el) return;
      el.innerHTML='<table class="tbl" id="snipTbl"></table>';
      if(!data.length){ addSnipRow("",""); } else { data.forEach(function(s){ addSnipRow(s.cue||"", s.text||""); }); }
    });
  }
  ONPAGE.snippets = function(){ if(isEmpty("snipList")) loadSnips(); };
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

  // wire the settings-search input (a static element in BODY)
  (function(){
    var si = $("settingsSearch"); if(!si) return;
    si.addEventListener('input', function(){ searchRun(si.value); });
    si.addEventListener('focus', function(){ if(si.value) searchRun(si.value); });
    si.addEventListener('keydown', function(e){
      var box = $("searchResults");
      var items = (box && !box.hidden) ? [].slice.call(box.querySelectorAll('.sr')) : [];
      var hi = box ? box.querySelector('.sr.hi') : null;
      var idx = items.indexOf(hi);
      if(e.key==='ArrowDown'){ e.preventDefault(); if(items.length) searchSetHi(items, Math.min(items.length-1, idx+1)); }
      else if(e.key==='ArrowUp'){ e.preventDefault(); if(items.length) searchSetHi(items, Math.max(0, idx-1)); }
      else if(e.key==='Enter'){ e.preventDefault(); var pick = hi || items[0]; if(pick) pick.click(); }
      else if(e.key==='Escape'){ si.value=''; searchHide(); si.blur(); }
    });
  })();

  // ---------- boot ----------
  if(window.__LIA_DEMO__){
    // Static QA render (browser / no pywebview): render immediately.
    go(PAGE); arrive();
  } else {
    RK.ready(function(){
      try{ RK.api.mark_ready(); }catch(e){}
      go(PAGE); arrive();
      if(FOCUS){ focusControl(FOCUS); }
    });
  }
})();
"""
# Embed provider logos inline (CSP-safe data: URIs; left as the "__..._LOGO__"
# placeholder if the asset is missing, so the render falls back to the letter badge).
APP_JS = (APP_JS
          .replace("__OPENAI_LOGO__", uk.asset_data_uri("provider_openai.png"))
          .replace("__GEMINI_LOGO__", uk.asset_data_uri("provider_gemini.png"))
          .replace("__GROQ_LOGO__", uk.asset_data_uri("provider_groq.png"))
          .replace("__PARAKEET_LOGO__", uk.asset_data_uri("provider_parakeet.png"))
          .replace("__WHISPER_LOGO__", uk.asset_data_uri("provider_whisper.png")))


if __name__ == "__main__":
    main()
