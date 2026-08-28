"""Lia shared UI kit - one design system for every pywebview window.

Lia's HTML windows (Settings, Ask-your-meetings, Action items, Chat, Email
search, Compose/Summary editor) each grew their own inline CSS: five accent
colours, three font stacks, dark AND light themes, six independent `:root`
blocks. This module is the single source of truth that replaces them:

  - CSS_TOKENS  : the `:root` design tokens (light; a dark block
                  is defined but only active under `data-theme="dark"`).
  - CSS_BASE    : reset + typography + every component (sidebar, pages, fields,
                  buttons, inputs, checks/radios/switches, lists, chips, status,
                  spinner, toasts, empty state, badges, masked secrets, scrollbar).
  - JS_BASE     : esc(), onReady() (assigns the pywebview bridge INSIDE the
                  `pywebviewready` event - never at parse time), toast(), $().
  - page(...)   : the ONLY place a full <!DOCTYPE html> document is assembled.
  - child_main / read_payload / install_stdout_sink : the shared subprocess
                  boilerplate every window duplicated.
  - atomic_write_json / open_path / copy_text : shared Python helpers.

Design rules baked in here (so windows can't drift):
  * Light theme, navy #22314F primary, a calm, high-contrast office palette.
  * Chrome is English; user CONTENT uses dir="auto" (Hebrew renders RTL, Latin
    LTR) - so the page is `lang="en" dir="ltr"` and only content nodes flip.
  * Logical properties only (margin-inline-*, padding-inline-*, inset-inline-*),
    never left/right, so RTL content lays out correctly.
  * Everything is inline + self-contained: no external fonts, CSS, or scripts
    (WebView2 / CSP safe, works fully offline).

IMPORTANT: importing this module must NOT import `webview` (the app imports it
in-process at startup; webview is heavy and only needed inside a child window).
`webview` is imported lazily inside child_main().
"""
import json
import os
import sys
import tempfile


def _warn(msg):
    """Write a diagnostic without ever raising - sys.stderr can be None under
    pythonw/CREATE_NO_WINDOW, which is the exact condition this module survives."""
    s = sys.stderr
    if s is not None:
        try:
            s.write(msg)
        except Exception:
            pass


# ============================================================
# Design tokens
# ============================================================
# Light is the committed default (the calm office palette). The dark block
# is defined for a future toggle but is inert unless the root carries
# data-theme="dark" - the app never sets it today.
CSS_TOKENS = """
:root{
  /* surfaces */
  --bg:#F4F5F7; --card:#FFFFFF; --card-2:#FBFBFC;
  --line:#E2E5EA; --line-2:#CDD3DC;
  /* ink */
  --ink:#1B2430; --ink-2:#2A3342; --muted:#6B7280; --faint:#9AA0AD;
  /* brand */
  --accent:#22314F; --accent-h:#2C3E63; --accent-soft:#EDEFF3;
  --accent-soft-h:#E1E5EC; --on-accent:#FFFFFF;
  /* semantic */
  --ok:#1a7f37; --warn:#b06a00; --err:#c0263c; --info:#2f5fd0;
  --ok-soft:#e8f5ec; --warn-soft:#fbf1e0; --err-soft:#fbe9ec; --info-soft:#eaf0fd;
  /* radii */
  --r-s:8px; --r-m:12px; --r-l:16px; --r-pill:999px;
  /* spacing scale */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:20px; --sp-6:24px;
  /* type */
  --font:"Segoe UI Variable Text","Segoe UI",Heebo,system-ui,Arial,sans-serif;
  --mono:"Cascadia Code",Consolas,"Courier New",monospace;
  --fs-base:14px; --fs-h1:19px; --fs-section:16px; --fs-label:13px;
  --fs-hint:12px; --fs-small:11.5px;
  /* effects */
  --shadow:0 1px 2px rgba(16,24,40,.06),0 12px 32px rgba(16,24,40,.05);
  --shadow-sm:0 1px 2px rgba(16,24,40,.06);
  --ring:0 0 0 3px rgba(34,49,79,.18);
  --tr:.12s ease;
  /* toast: a dark pill in BOTH themes (deliberately NOT overridden in the dark
     block) so the white text + coloured level dots always stay readable. */
  --toast-bg:#1B2430; --toast-fg:#FFFFFF;
  /* scrollbar */
  --sb-thumb:#d6d9e0; --sb-thumb-h:#c2c6d0;
}
:root[data-theme="dark"]{
  --bg:#0f131a; --card:#161b24; --card-2:#1b212c;
  --line:#262d3a; --line-2:#333c4c;
  --ink:#e7ebf2; --ink-2:#c3cad6; --muted:#9099a8; --faint:#6c7585;
  --accent:#7f9bd4; --accent-h:#93abdd; --accent-soft:#1e2635;
  --accent-soft-h:#26324a; --on-accent:#0e1420;
  --ok:#7fce9b; --warn:#e2b877; --err:#e79aa7; --info:#8fb0ee;
  --ok-soft:#16241b; --warn-soft:#2a2113; --err-soft:#2a1418; --info-soft:#141d2f;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 12px 32px rgba(0,0,0,.45);
  --shadow-sm:0 1px 2px rgba(0,0,0,.4);
  --ring:0 0 0 3px rgba(127,155,212,.28);
  --sb-thumb:#333c4c; --sb-thumb-h:#414c60;
}
"""


# ============================================================
# Base stylesheet + components
# ============================================================
CSS_BASE = """
*,*::before,*::after{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{
  background:var(--bg); color:var(--ink);
  font-family:var(--font); font-size:var(--fs-base); line-height:1.55;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
::selection{background:var(--accent); color:var(--on-accent);}
a{color:var(--accent); text-decoration:none;}
a:hover{text-decoration:underline;}

/* --- layout shell: sidebar + content ------------------------------------ */
.shell{display:flex; height:100vh; overflow:hidden;}
.sidebar{
  width:190px; flex:0 0 190px; background:var(--card);
  border-inline-end:1px solid var(--line);
  padding:var(--sp-3) var(--sp-2); overflow-y:auto;
  display:flex; flex-direction:column; gap:2px;
}
.sidebar .brand{
  display:flex; align-items:center; gap:var(--sp-2);
  padding:var(--sp-2) var(--sp-3) var(--sp-4);
  font-size:var(--fs-h1); font-weight:700; color:var(--ink);
}
.sidebar .brand .logo{
  width:28px; height:28px; border-radius:8px; flex:0 0 28px;
  /* the brand orb (lia_logo.png) - transparent corners, own rim/glow, so no
     accent plate behind it. Text fallback ("R") still centers if the img is absent. */
  background:transparent; color:var(--on-accent);
  display:flex; align-items:center; justify-content:center;
  font-size:14px; font-weight:800; object-fit:contain;
}
.sidebar .brand img.logo{display:block;}
.nav-item{
  appearance:none; border:0; width:100%; text-align:start; cursor:pointer;
  padding:11px 16px; margin:2px 0; border-radius:var(--r-s);
  color:var(--ink-2); background:transparent; font-family:inherit;
  font-size:var(--fs-base); font-weight:500; transition:background var(--tr),color var(--tr);
  display:flex; align-items:center; gap:10px;
}
.nav-item:hover{background:var(--accent-soft);}
/* Selected page: a soft tint + accent-coloured bold label (was a solid navy
   fill, too high-contrast). Distinct from hover via the accent text + weight. */
.nav-item.on{background:var(--accent-soft-h); color:var(--accent); font-weight:600;}
.nav-item.on .ico{color:var(--accent);}
.nav-item .ico{font-size:15px; width:18px; text-align:center; flex:0 0 18px;}
.sidebar .spacer{flex:1 1 auto;}

.content{flex:1 1 auto; overflow-y:auto; padding:var(--sp-5) var(--sp-6);}
.content-head{
  display:flex; align-items:center; gap:var(--sp-3);
  margin-bottom:var(--sp-4);
}
.content-head h1{margin:0; font-size:var(--fs-h1); font-weight:700; color:var(--ink);}
.content-head .sub{color:var(--muted); font-size:var(--fs-hint);}

/* --- page cards --------------------------------------------------------- */
.page{
  background:var(--card); border:1px solid var(--line); border-radius:var(--r-m);
  padding:22px 20px; margin-bottom:var(--sp-4);
}
.page.flush{padding:0; overflow:hidden;}
.section-title{
  font-size:var(--fs-section); font-weight:600; color:var(--ink);
  margin:0 0 var(--sp-3);
}
.section-title:not(:first-child){margin-top:var(--sp-5);}
.divider{height:1px; background:var(--line); margin:var(--sp-4) 0;}
.hint{font-size:var(--fs-hint); color:var(--muted); margin:6px 0 0;}

/* --- field: label + control + hint ------------------------------------- */
.field{margin-bottom:var(--sp-5);}
/* Group header: uppercase muted small-caps so it reads as a SECTION LABEL
   above its controls, never as one of the choices below it. */
.field > label,.field-label{
  display:block; font-size:var(--fs-hint); font-weight:700; letter-spacing:.03em;
  text-transform:uppercase; color:var(--muted); margin-bottom:10px;
}
.field .hint{margin-top:6px;}
.row-inline{display:flex; align-items:center; gap:var(--sp-2); flex-wrap:wrap;}

/* --- buttons ------------------------------------------------------------ */
.btn{
  appearance:none; font-family:inherit; font-size:var(--fs-base); font-weight:600;
  border:0; border-radius:var(--r-s); padding:8px 16px; cursor:pointer;
  background:var(--accent-soft); color:var(--accent);
  transition:background var(--tr),color var(--tr),box-shadow var(--tr),transform .05s;
  display:inline-flex; align-items:center; gap:8px; line-height:1.2;
}
.btn:hover{background:var(--accent-soft-h);}
.btn:active{transform:translateY(.5px);}
.btn:focus-visible{outline:none; box-shadow:var(--ring);}
.btn.primary{background:var(--accent); color:var(--on-accent); box-shadow:var(--shadow-sm);}
.btn.primary:hover{background:var(--accent-h);}
.btn.ghost{background:transparent; color:var(--ink-2); box-shadow:inset 0 0 0 1px var(--line-2);}
.btn.ghost:hover{background:var(--accent-soft); color:var(--accent);}
.btn.danger{background:transparent; color:var(--err); box-shadow:inset 0 0 0 1px var(--err);}
.btn.danger:hover{background:var(--err-soft);}
.btn.icon{padding:8px 10px; min-width:36px; justify-content:center;}
.btn:disabled,.btn.disabled{opacity:.5; cursor:default; pointer-events:none;}
.btn.sm{padding:6px 12px; font-size:var(--fs-hint);}

/* --- inputs ------------------------------------------------------------- */
input[type=text],input[type=password],input[type=search],select,textarea,.input{
  width:100%; font-family:inherit; font-size:var(--fs-base); color:var(--ink);
  background:var(--card); border:1px solid var(--line-2); border-radius:var(--r-s);
  padding:8px 11px; transition:border-color var(--tr),box-shadow var(--tr);
}
input.mono,textarea.mono,.mono{font-family:var(--mono);}
input::placeholder,textarea::placeholder{color:var(--faint);}
input:focus,select:focus,textarea:focus,.input:focus{
  outline:none; border-color:var(--accent); box-shadow:var(--ring);
}
textarea{resize:vertical; min-height:96px; line-height:1.6;}
select{cursor:pointer; padding-inline-end:28px;}
input:disabled,select:disabled,textarea:disabled{opacity:.55; cursor:default;}

/* --- checkbox (custom) ------------------------------------------------- */
.check{
  display:flex; align-items:flex-start; gap:10px; cursor:pointer;
  padding:7px 0; color:var(--ink); user-select:none;
}
.check input{position:absolute; opacity:0; width:0; height:0;}
.check .box{
  flex:0 0 18px; width:18px; height:18px; margin-top:1px; border-radius:5px;
  border:1px solid var(--line-2); background:var(--card); position:relative;
  transition:background var(--tr),border-color var(--tr),box-shadow var(--tr);
}
.check:hover .box{border-color:var(--accent);}
.check input:focus-visible ~ .box{box-shadow:var(--ring);}
.check input:checked ~ .box{background:var(--accent); border-color:var(--accent);}
.check input:checked ~ .box::after{
  content:""; position:absolute; inset-inline-start:5px; top:1px;
  width:5px; height:10px; border:solid var(--on-accent);
  border-width:0 2px 2px 0; transform:rotate(45deg);
}
.check.disabled{opacity:.5; pointer-events:none;}
.check .txt{flex:1 1 auto;}
.check .txt small{display:block; color:var(--muted); font-size:var(--fs-hint);}

/* --- radio: rendered as a selectable option ROW ------------------------- */
/* Each choice is a bordered row; the SELECTED row gets a tinted fill, an
   accent border, bold ink, and a filled accent dot - so the current pick is
   unmistakable and the group never blurs into its header. */
.radio{
  display:flex; align-items:center; gap:12px; cursor:pointer; user-select:none;
  padding:11px 14px; margin-bottom:8px; color:var(--ink-2);
  border:1.5px solid var(--line); border-radius:var(--r-s); background:var(--card);
  transition:background var(--tr),border-color var(--tr),color var(--tr);
}
.radio:last-child{margin-bottom:0;}
.radio input{position:absolute; opacity:0; width:0; height:0;}
.radio .box{
  flex:0 0 20px; width:20px; height:20px; border-radius:50%;
  border:2px solid var(--line-2); background:var(--card); position:relative;
  transition:border-color var(--tr);
}
.radio input:checked ~ .box{border-color:var(--accent);}
.radio input:checked ~ .box::after{
  content:""; position:absolute; inset:4px; border-radius:50%; background:var(--accent);
}
.radio:hover{background:var(--accent-soft); border-color:var(--line-2);}
.radio input:focus-visible ~ .box{box-shadow:var(--ring);}
/* selected: JS adds .on (survives re-render); :has() gives instant feedback on
   click. Kept as SEPARATE rules so an engine without :has() still honours .on. */
.radio.on{background:var(--accent-soft); border-color:var(--accent);
  color:var(--ink); font-weight:600;}
.radio:has(input:checked){background:var(--accent-soft); border-color:var(--accent);
  color:var(--ink); font-weight:600;}
.radio.disabled{opacity:.5; pointer-events:none;}
.radio .txt{flex:1 1 auto;}
.radio .txt small{display:block; color:var(--muted); font-size:var(--fs-hint); font-weight:400;}

/* --- switch ------------------------------------------------------------- */
.switch{display:inline-flex; align-items:center; gap:10px; cursor:pointer; user-select:none;}
.switch input{position:absolute; opacity:0; width:0; height:0;}
.switch .track{
  width:38px; height:22px; border-radius:var(--r-pill); background:var(--line-2);
  position:relative; transition:background var(--tr); flex:0 0 38px;
}
.switch .track::after{
  content:""; position:absolute; top:2px; inset-inline-start:2px;
  width:18px; height:18px; border-radius:50%; background:#fff;
  box-shadow:0 1px 2px rgba(0,0,0,.25); transition:transform var(--tr);
}
.switch input:checked ~ .track{background:var(--accent);}
.switch input:checked ~ .track::after{transform:translateX(16px);}
.switch input:focus-visible ~ .track{box-shadow:var(--ring);}
[dir=rtl] .switch input:checked ~ .track::after{transform:translateX(-16px);}
.switch.disabled{opacity:.5; pointer-events:none;}

/* --- rows / lists ------------------------------------------------------- */
.list{display:flex; flex-direction:column;}
.list .row{
  display:flex; align-items:center; gap:var(--sp-3);
  padding:11px 14px; border-bottom:1px solid var(--line);
  transition:background var(--tr); position:relative; cursor:default;
}
.list .row:last-child{border-bottom:0;}
.list .row.click{cursor:pointer;}
.list .row.click:hover{background:var(--accent-soft);}
.list .row.sel{background:var(--accent-soft);}
.list .row.sel::before{
  content:""; position:absolute; inset-inline-start:0; top:0; bottom:0;
  width:3px; background:var(--accent);
}
.list .row .grow{flex:1 1 auto; min-width:0;}
.list .row .muted{color:var(--muted); font-size:var(--fs-hint);}

/* --- chip / badge ------------------------------------------------------- */
.chip{
  display:inline-flex; align-items:center; gap:6px;
  background:var(--accent-soft); color:var(--accent);
  border-radius:var(--r-pill); padding:3px 11px; font-size:var(--fs-hint); font-weight:600;
}
.badge{
  display:inline-flex; align-items:center; justify-content:center;
  min-width:20px; height:20px; padding:0 6px; border-radius:var(--r-pill);
  background:var(--accent); color:var(--on-accent);
  font-size:var(--fs-small); font-weight:700;
}
.badge.soft{background:var(--accent-soft); color:var(--accent);}
.kbd{
  display:inline-block; font-family:var(--mono); font-size:12px;
  background:var(--card-2); color:var(--ink-2);
  border:1px solid var(--line-2); border-bottom-width:2px; border-radius:6px;
  padding:1px 7px;
}

/* --- status line -------------------------------------------------------- */
.status{display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:var(--fs-hint);}
.status .dot{width:8px; height:8px; border-radius:50%; background:var(--ok); flex:0 0 8px;}
.status.warn .dot{background:var(--warn);} .status.warn{color:var(--warn);}
.status.err .dot{background:var(--err);} .status.err{color:var(--err);}
.status.busy .dot{background:var(--accent); animation:pulse 1s infinite;}
.status.ok .dot{background:var(--ok);}
@keyframes pulse{50%{opacity:.35;}}

/* --- spinner ------------------------------------------------------------ */
.spinner{
  display:inline-block; width:14px; height:14px; vertical-align:-2px;
  border:2px solid var(--accent-soft); border-top-color:var(--accent);
  border-radius:50%; animation:spin .7s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg);}}

/* --- masked secret ------------------------------------------------------ */
.masked{font-family:var(--mono); font-size:13px; color:var(--ok);}
.masked.empty{color:var(--faint);}

/* --- empty state -------------------------------------------------------- */
.empty{
  display:flex; flex-direction:column; align-items:center; gap:10px;
  padding:54px 24px; text-align:center; color:var(--muted);
}
.empty .big{font-size:34px; opacity:.55;}
.empty .sub{font-size:var(--fs-hint); color:var(--faint); max-width:300px;}

/* --- toast -------------------------------------------------------------- */
#toast-host{
  position:fixed; inset-inline:0; bottom:18px; display:flex;
  flex-direction:column; align-items:center; gap:8px; pointer-events:none; z-index:999;
}
.toast{
  pointer-events:auto; background:var(--toast-bg); color:var(--toast-fg);
  border-radius:var(--r-s); padding:9px 16px; font-size:var(--fs-hint); font-weight:600;
  box-shadow:var(--shadow); opacity:0; transform:translateY(8px);
  transition:opacity .16s,transform .16s; max-width:80vw;
  display:flex; align-items:center; gap:8px;
}
.toast.in{opacity:1; transform:none;}
.toast .tdot{width:8px; height:8px; border-radius:50%; background:var(--toast-fg); flex:0 0 8px;}
.toast.ok .tdot{background:#5fd08a;} .toast.warn .tdot{background:#f0c274;}
.toast.err .tdot{background:#ef8ba0;} .toast.info .tdot{background:#8fb0ee;}

/* --- content that may be Hebrew: let the browser pick direction --------- */
.rtl-auto,[dir=auto]{unicode-bidi:plaintext;}

/* --- scrollbar ---------------------------------------------------------- */
::-webkit-scrollbar{width:11px; height:11px;}
::-webkit-scrollbar-thumb{background:var(--sb-thumb); border-radius:8px; border:3px solid var(--bg);}
::-webkit-scrollbar-thumb:hover{background:var(--sb-thumb-h);}
::-webkit-scrollbar-track{background:transparent;}
"""


# ============================================================
# Shared JS
# ============================================================
# NOTE: the bridge (window.pywebview.api) is read ONLY inside the pywebviewready
# handler in RK.ready() - never at parse time (a parse-time read is undefined and
# throws, aborting the whole <script>: the dead-window bug). Every line that
# touches `window.pywebview.api` is indented (never column 0), which the
# regression test asserts.
JS_BASE = r"""
window.RK = (function(){
  var api = null;
  var _readyFns = [];
  var _pushBuf = [];
  function esc(s){
    return String(s == null ? "" : s)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
  }
  function $(id){ return document.getElementById(id); }
  function el(tag, attrs, html){
    var e = document.createElement(tag);
    if(attrs){ for(var k in attrs){ if(k==="class") e.className=attrs[k];
      else if(k==="text") e.textContent=attrs[k]; else e.setAttribute(k,attrs[k]); } }
    if(html!=null) e.innerHTML=html;
    return e;
  }
  function toastHost(){
    var h = $("toast-host");
    if(!h){ h = el("div",{id:"toast-host"}); document.body.appendChild(h); }
    return h;
  }
  function toast(msg, level, ms){
    var t = el("div",{"class":"toast "+(level||"")});
    t.appendChild(el("span",{"class":"tdot"}));
    t.appendChild(el("span",{text:String(msg==null?"":msg)}));
    toastHost().appendChild(t);
    requestAnimationFrame(function(){ t.classList.add("in"); });
    setTimeout(function(){
      t.classList.remove("in");
      setTimeout(function(){ if(t.parentNode) t.parentNode.removeChild(t); }, 200);
    }, ms || 2600);
  }
  // Register work to run once the bridge is live. If already ready, runs now.
  function ready(fn){ if(api) fn(api); else _readyFns.push(fn); }
  // Parent->child push hook. Windows override RK.onPush; buffered until wired.
  var _onPush = null;
  function onPush(fn){
    _onPush = fn;
    if(_pushBuf.length){ var b=_pushBuf; _pushBuf=[]; b.forEach(function(m){ try{ fn(m); }catch(e){} }); }
  }
  // Called from Python via evaluate_js("window.__lia_push(<json>)").
  window.__lia_push = function(msg){
    if(_onPush){ try{ _onPush(msg); }catch(e){} } else { _pushBuf.push(msg); }
  };
  window.addEventListener("pywebviewready", function(){
    api = window.pywebview.api;   /* deferred bridge read - the ONLY one */
    var fns = _readyFns; _readyFns = [];
    fns.forEach(function(fn){ try{ fn(api); }catch(e){ console.error(e); } });
  });
  return { esc:esc, $:$, el:el, toast:toast, ready:ready, onPush:onPush,
           get api(){ return api; } };
})();
"""


# ============================================================
# Page assembly - the ONLY place a full HTML document is built
# ============================================================
def page(title, body_html, *, extra_css="", extra_js="", lang="en", direction="ltr",
         head_extra="", theme=None):
    """Return a complete, self-contained HTML document string.

    title      - <title> text (also usable as a header by the caller's body).
    body_html  - the page body (no <html>/<head>/<body> wrappers).
    extra_css  - window-specific CSS appended after the shared base.
    extra_js   - window-specific JS appended after RK (RK is available).
    lang/dir   - document language/direction (chrome is en/ltr; content nodes
                 use dir="auto"). Do not set dir="rtl" on the doc - it flips the
                 whole chrome; use dir="auto" on individual content nodes.
    head_extra - raw extra <head> markup (rare).
    theme      - "dark" stamps data-theme="dark" on <html> (CSS_TOKENS carries the
                 dark palette); anything else = the default light. Only windows
                 built purely on ui_kit tokens (Settings, History) are dark-ready.
    """
    theme_attr = ' data-theme="dark"' if theme == "dark" else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="' + lang + '" dir="' + direction + '"' + theme_attr + '>\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>" + _h(title) + "</title>\n"
        + head_extra +
        "<style>\n" + CSS_TOKENS + CSS_BASE + "\n" + extra_css + "\n</style>\n"
        "</head>\n"
        "<body>\n"
        + body_html + "\n"
        "<script>\n" + JS_BASE + "\n" + extra_js + "\n</script>\n"
        "</body>\n</html>\n"
    )


def _h(s):
    """Minimal HTML-escape for values embedded in the document by page()."""
    return (str("" if s is None else s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ============================================================
# Shared Python helpers (dedup across windows)
# ============================================================
def atomic_write_json(path, obj):
    """Write JSON atomically (tmp + os.replace) so a crash never truncates it."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d or None, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def open_path(path, *, base_dir=None):
    """os.startfile a path, guarding against path-escape when the caller passes
    an id that should stay inside base_dir. Returns True on success."""
    try:
        if base_dir is not None:
            # only allow a plain filename directly inside base_dir (no ../ escape).
            # Reject degenerate names ("", ".", "..") - basename("a/..") == ".."
            # which would otherwise resolve to the PARENT of base_dir.
            name = os.path.basename(path)
            if name in ("", os.curdir, os.pardir):
                return False
            full = os.path.join(base_dir, name)
            # abspath on `full` (not on dirname) collapses any trailing "..".
            if os.path.dirname(os.path.abspath(full)) != os.path.abspath(base_dir):
                return False
            path = full
        if not os.path.exists(path):
            return False
        os.startfile(path)  # noqa: pylint - Windows
        return True
    except Exception:
        return False


def copy_text(text):
    """Copy text to the clipboard via pyperclip. Returns True on success."""
    try:
        import pyperclip
        pyperclip.copy(str("" if text is None else text))
        return True
    except Exception:
        return False


_LOGO_URI_CACHE = None


def logo_data_uri():
    """Return the Lia brand logo (lia_logo.png, next to this module) as a
    base64 `data:` URI for inline <img>/CSS use - CSP-safe, no external asset.
    Cached after first read; returns "" if the file is missing so callers
    degrade gracefully (no broken-image icon)."""
    global _LOGO_URI_CACHE
    if _LOGO_URI_CACHE is not None:
        return _LOGO_URI_CACHE
    try:
        import base64
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lia_logo.png")
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        _LOGO_URI_CACHE = "data:image/png;base64," + b64
    except Exception as e:
        _warn("logo_data_uri: " + str(e))
        _LOGO_URI_CACHE = ""
    return _LOGO_URI_CACHE


# --- per-window UI prefs (size/position memory) ---------------------------
_CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Lia")
UI_PREFS_FILE = os.path.join(_CONFIG_DIR, "ui_prefs.json")


def load_ui_prefs():
    try:
        with open(UI_PREFS_FILE, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_ui_pref(key, value):
    """Merge one key into ui_prefs.json (atomic). Never raises."""
    try:
        prefs = load_ui_prefs()
        prefs[key] = value
        atomic_write_json(UI_PREFS_FILE, prefs)
    except Exception:
        pass


def window_geometry(name, default=None):
    """Saved {x,y,width,height} for a window, or `default`. Values are sanity-
    clamped so a stale/off-screen or absurd size never traps a window."""
    g = load_ui_prefs().get("win_" + name)
    if not isinstance(g, dict):
        return default
    try:
        w = int(g.get("width", 0))
        h = int(g.get("height", 0))
        if w < 360 or h < 320 or w > 6000 or h > 4000:
            return default
        out = {"width": w, "height": h}
        if isinstance(g.get("x"), int) and isinstance(g.get("y"), int):
            if -50 <= g["x"] <= 12000 and -50 <= g["y"] <= 8000:
                out["x"] = g["x"]
                out["y"] = g["y"]
        return out
    except Exception:
        return default


def attach_geometry_memory(window, name):
    """Persist a pywebview window's size + position across sessions. Wires the
    resized/moved events (best-effort; a pywebview build without them is a no-op).
    Restore is done by the caller passing window_geometry(name) into
    create_window(x=,y=,width=,height=)."""
    state = {"w": None, "h": None, "x": None, "y": None}

    def _flush():
        g = {}
        if state["w"]:
            g["width"] = state["w"]
            g["height"] = state["h"]
        if state["x"] is not None:
            g["x"] = state["x"]
            g["y"] = state["y"]
        if g:
            save_ui_pref("win_" + name, g)

    def _on_resized(w, h):
        try:
            state["w"], state["h"] = int(w), int(h)
            _flush()
        except Exception:
            pass

    def _on_moved(x, y):
        try:
            state["x"], state["y"] = int(x), int(y)
            _flush()
        except Exception:
            pass
    try:
        window.events.resized += _on_resized
    except Exception:
        pass
    try:
        window.events.moved += _on_moved
    except Exception:
        pass


def read_payload(argv=None):
    """Read the JSON payload passed as argv[1] (the window spawn contract).
    Returns {} if absent/unreadable so a window can still open with defaults."""
    argv = sys.argv if argv is None else argv
    if len(argv) >= 2 and argv[1]:
        try:
            with open(argv[1], encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            _warn("ui_kit: bad payload %r: %r\n" % (argv[1], e))
    return {}


def install_stdout_sink(log_path):
    """When launched with CREATE_NO_WINDOW under pythonw, sys.stdout/stderr can
    be None; any print then raises. Redirect them to a log file (or os.devnull)
    so window code never dies on a stray write. Call BEFORE anything prints.

    NOTE: windows that speak an stdout IPC protocol (e.g. Settings) must NOT
    redirect stdout - only stderr. Pass log_path and call with only_stderr=... via
    the sibling helper if needed; this default redirects both."""
    _redirect(log_path, stdout=True, stderr=True)


def install_stderr_sink(log_path):
    """Like install_stdout_sink but leaves stdout ALONE - for windows that use
    stdout as an IPC channel to the parent."""
    _redirect(log_path, stdout=False, stderr=True)


def _redirect(log_path, *, stdout, stderr):
    need = (stdout and sys.stdout is None) or (stderr and sys.stderr is None)
    if not need:
        return
    sink = None
    try:
        d = os.path.dirname(log_path)
        if d:
            os.makedirs(d, exist_ok=True)
        sink = open(log_path, "a", encoding="utf-8")
    except Exception:
        try:
            sink = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            return
    if stdout and sys.stdout is None:
        sys.stdout = sink
    if stderr and sys.stderr is None:
        sys.stderr = sink


def child_main(build_window, *, log_path=None, ready_check=None, watchdog_s=30):
    """Shared subprocess entry point for a pywebview window.

    build_window(webview, payload) MUST create the window (webview.create_window)
    and may start helper threads (e.g. an stdin reader). It returns the window
    object (or None). child_main then starts the edgechromium GUI loop.

    - Imports webview lazily (so `import ui_kit` in the parent stays light).
      Missing webview -> exit 2.
    - Optional ready_check(): a callable returning True once the window has
      initialised; if it stays False for watchdog_s, force-close + exit 3 (a
      broken/missing WebView2 runtime can HANG instead of raising).
    - Any build/start exception -> exit 3.
    """
    if log_path:
        # default: redirect both (override by redirecting yourself before calling)
        install_stdout_sink(log_path)
    try:
        import webview
    except Exception as e:  # pragma: no cover - env without pywebview
        _warn("ui_kit.child_main: pywebview unavailable: %r\n" % (e,))
        sys.exit(2)

    payload = read_payload()
    try:
        window = build_window(webview, payload)
    except Exception as e:
        _warn("ui_kit.child_main: build_window failed: %r\n" % (e,))
        sys.exit(3)

    if ready_check is not None:
        import threading
        import time

        def _watch():
            t0 = time.monotonic()
            while time.monotonic() - t0 < watchdog_s:
                time.sleep(0.5)
                try:
                    if ready_check():
                        return
                except Exception:
                    pass
            _warn("ui_kit.child_main: window never became ready; aborting\n")
            try:
                if window is not None:
                    window.destroy()
            except Exception:
                pass
            os._exit(3)

        threading.Thread(target=_watch, daemon=True).start()

    try:
        webview_start(webview)
    except Exception as e:
        _warn("ui_kit.child_main: webview.start failed: %r\n" % (e,))
        sys.exit(3)


def webview_start(webview, *, gui="edgechromium"):
    """Start the pywebview GUI loop, tagging the window with Lia's icon
    (lia.ico next to this module) so its title bar + taskbar entry show the
    app logo instead of the Python interpreter's icon. Falls back to a plain
    start if the pywebview build rejects icon= or the file is unusable - a
    window must never fail to open over its icon."""
    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lia.ico")
    if os.path.isfile(icon):
        try:
            webview.start(gui=gui, icon=icon)
            return
        except Exception as e:
            _warn("webview_start: icon start failed (%r); retrying plain\n" % (e,))
    webview.start(gui=gui)
