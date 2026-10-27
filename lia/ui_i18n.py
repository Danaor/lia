"""Lia's interface language - English (the source) or Hebrew.

One mechanism for every window, the tray and the Tk cards:

  - The code keeps writing ENGLISH. Hebrew lives in one dictionary
    (ui_he.STRINGS) keyed by the English text, gettext style. A key with
    {name} placeholders is a template: "Saved {n} terms" also matches
    "Saved 12 terms" and keeps the 12.
  - Python surfaces (tray menu, overlays, the meeting card, window titles)
    call t(text) where the text is shown.
  - HTML windows get I18N_JS (page() / brand_html() inject it): a
    MutationObserver translates every chrome text node and the placeholder /
    title / aria-label attributes as they are rendered, so strings built by
    the window's JS or sent by the parent (toasts, results, model rows) are
    covered without rewriting the renderers. The document is lang="he"
    dir="rtl"; the CSS already uses logical properties only.
  - User CONTENT is never translated: anything inside [dir=auto], .rtl-auto,
    [contenteditable], [data-noi18n], inputs/textareas, code/pre/kbd keeps its
    own text (.i18n opts a small chrome label back in).

The language: config "ui_language" ("en" / "he"). The running app exports it
as LIA_UI_LANG so every child inherits it; a child started without that env
(the de-elevated spawn) falls back to reading config.json.

Importing this module must stay cheap (the app imports it at startup and
every child window imports it through ui_kit).
"""
import json
import os
import re

UI_LANGS = ("en", "he")
ENV_KEY = "LIA_UI_LANG"


def _config_path():
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Lia", "config.json")


def normalize(lang):
    """'he' / 'en' (anything unknown -> 'en')."""
    lang = str(lang or "").strip().lower()[:2]
    return lang if lang in UI_LANGS else "en"


def resolve(cfg=None):
    """The interface language: an explicit config dict wins, then the
    LIA_UI_LANG the app exported, then config.json on disk, then English."""
    if isinstance(cfg, dict) and cfg.get("ui_language"):
        return normalize(cfg.get("ui_language"))
    env = os.environ.get(ENV_KEY)
    if env:
        return normalize(env)
    try:
        with open(_config_path(), "r", encoding="utf-8-sig") as f:
            return normalize(json.load(f).get("ui_language"))
    except Exception:
        return "en"


def set_process_lang(lang):
    """Export the language to this process (and so to every child it spawns)."""
    os.environ[ENV_KEY] = normalize(lang)


def is_rtl(lang=None):
    return (lang or resolve()) == "he"


def windows_default():
    """The first-run default: Hebrew when Windows itself is in Hebrew."""
    try:
        import ctypes
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "he" if (langid & 0x3FF) == 0x0D else "en"
    except Exception:
        return "en"


# ------------------------------------------------------------------
# The dictionary
# ------------------------------------------------------------------
_TABLE = None      # {english: hebrew} (exact keys, no placeholders)
_TEMPLATES = None  # [(regex, hebrew_template, names)]
_TPL_KEYS = {}     # {english template key: hebrew template} (for t(key, **kw))
_PH = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
# {n} {n2} {count} {pct} match a number only ("{n} terms" must not swallow
# "Learned terms"); any other name matches any text.
_NUM_PH = re.compile(r"^(n\d?|count|pct)$")


def _tpl_regex(key):
    out, pos, names = [], 0, []
    for m in _PH.finditer(key):
        out.append(re.escape(key[pos:m.start()]))
        out.append(r"(~?\$?[\d][\d.,]*)" if _NUM_PH.match(m.group(1)) else "(.+?)")
        names.append(m.group(1))
        pos = m.end()
    out.append(re.escape(key[pos:]))
    return re.compile("^" + "".join(out) + "$", re.S), names


def _load():
    global _TABLE, _TEMPLATES
    if _TABLE is not None:
        return
    try:
        import ui_he
        src = ui_he.STRINGS
    except Exception:
        src = {}
    table, tpls = {}, []
    for k, v in src.items():
        if _PH.search(k):
            rx, names = _tpl_regex(k)
            tpls.append((rx, v, names, len(k)))
        else:
            table[k] = v
    # the longest (most specific) template first
    tpls.sort(key=lambda t: -t[3])
    _TABLE = table
    _TPL_KEYS.update({k: v for k, v in src.items() if _PH.search(k)})
    _TEMPLATES = [(rx, v, names) for rx, v, names, _ in tpls]


def lookup(text, _depth=0):
    """The Hebrew for one English string, or None. Surrounding whitespace is
    kept; inner text must match a key exactly or a template."""
    _load()
    if not text:
        return None
    s = text.strip()
    if not s:
        return None
    he = _TABLE.get(s)
    if he is None:
        for rx, tpl, names in _TEMPLATES:
            m = rx.match(s)
            if m:
                vals = dict(zip(names, m.groups()))
                # a captured value that is itself a known phrase is translated too
                # a captured value that is itself a known phrase is translated too
                he = _PH.sub(lambda mm: (lookup(vals.get(mm.group(1), ""), _depth + 1)
                                         if _depth < 2 else None)
                             or vals.get(mm.group(1), ""), tpl)
                break
    if he is None:
        return None
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return lead + he + trail


def t(text, lang=None, **kw):
    """`text` in the interface language. With kwargs, `text` is a template
    ("Saved {n} terms", n=3) that is filled after translation."""
    if text is None:
        return text
    lang = lang or resolve()
    out = text
    if lang == "he":
        # a template key passed as-is ("Saved {n} terms", n=3) is its own entry
        he = _TPL_KEYS.get(text) if kw else None
        if he is None:
            he = lookup(text)
        if he is not None:
            out = he
    if kw:
        try:
            out = out.format(**kw)
        except Exception:
            pass
    return out


def payload_json():
    """The dictionary as JSON for the page runtime (exact table + templates)."""
    try:
        import ui_he
        src = ui_he.STRINGS
    except Exception:
        src = {}
    return json.dumps(src, ensure_ascii=True)


# ------------------------------------------------------------------
# The page runtime (HTML windows)
# ------------------------------------------------------------------
# Runs before any window script: patches confirm/alert/prompt, translates the
# static body on DOMContentLoaded and every later mutation (a microtask, so
# the English never paints). __LIA_I18N_MISS__ collects English chrome text
# that had no entry (the QA tools read it; nothing is sent anywhere).
_I18N_JS = r"""
window.LIA_I18N = (function(){
  var LANG = "__LANG__";
  var D = __DICT__;
  var EX = {}, TPL = [];
  var PH = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g;
  function reEsc(s){ return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
  Object.keys(D).forEach(function(k){
    if(/\{[a-zA-Z_][a-zA-Z0-9_]*\}/.test(k)){
      var names = [], src = "", pos = 0, m;
      PH.lastIndex = 0;
      while((m = PH.exec(k))){
        src += reEsc(k.slice(pos, m.index)) + (/^(n\d?|count|pct)$/.test(m[1]) ? "(~?\\$?\\d[\\d.,]*)" : "([\\s\\S]+?)");
        names.push(m[1]); pos = m.index + m[0].length; }
      src += reEsc(k.slice(pos));
      TPL.push({rx: new RegExp("^" + src + "$"), out: D[k], names: names, n: k.length});
    } else EX[k] = D[k];
  });
  TPL.sort(function(a, b){ return b.n - a.n; });
  var miss = window.__LIA_I18N_MISS__ = {};
  // a captured value that is itself a known phrase (or template) is translated
  // too: "{base} · nothing new ✓" where the base is "{n} emails · ..."
  function look(s, depth){
    var t = s.trim(); if(!t) return null;
    if(Object.prototype.hasOwnProperty.call(EX, t)) return EX[t];
    for(var i = 0; i < TPL.length; i++){
      var m = TPL[i].rx.exec(t);
      if(m){ var vals = {}; TPL[i].names.forEach(function(n, j){ vals[n] = m[j + 1]; });
        return TPL[i].out.replace(PH, function(_, n){ var v = vals[n] == null ? "" : vals[n];
          var h = (depth || 0) < 2 ? look(v, (depth || 0) + 1) : null;
          return h == null ? v : h; }); }
    }
    return null;
  }
  function tr(s, quiet){
    if(LANG !== "he" || s == null) return s;
    s = String(s);
    var h = look(s);
    if(h == null){ if(!quiet && /[A-Za-z]{2}/.test(s) && !/[֐-׿]/.test(s)) miss[s.trim()] = 1; return s; }
    var lead = s.match(/^\s*/)[0], trail = s.match(/\s*$/)[0];
    return lead + h + trail;
  }
  // user content, key names, paths and ids keep their own text
  // an editable document (the summary editor's card) is the user's text: a
  // translated heading there would be SAVED into the summary
  var SKIP = "script,style,textarea,input,code,pre,kbd,.kbd,.hk,.mono,.masked,[contenteditable],[dir=auto],.rtl-auto,[data-noi18n],.no-i18n";
  // .i18n opts a chrome bit back in inside content ("used 5" next to a term)
  function skipped(el){
    if(!el || !el.closest) return !el;
    var s = el.closest(SKIP); if(!s) return false;
    var i = el.closest(".i18n");
    return !(i && i !== s && s.contains(i));
  }
  var ATTRS = ["placeholder", "title", "aria-label", "data-tip"];
  // a text field's own value is user content, but its placeholder / title /
  // aria-label are chrome
  function field(el){ return el.tagName === "INPUT" || el.tagName === "TEXTAREA"; }
  function trEl(el){
    if(field(el) ? (!el.parentElement || skipped(el.parentElement)) : skipped(el)) return;
    for(var i = 0; i < ATTRS.length; i++){
      var a = el.getAttribute && el.getAttribute(ATTRS[i]);
      if(a){ var h = tr(a); if(h !== a) el.setAttribute(ATTRS[i], h); }
    }
    if(el.tagName === "INPUT" && /^(button|submit)$/i.test(el.type || "") && el.value){
      var v = tr(el.value); if(v !== el.value) el.value = v;
    }
  }
  function trText(n){
    var p = n.parentNode;
    if(!p || skipped(p)) return;
    var v = n.nodeValue, h = tr(v);
    if(h !== v){ n.nodeValue = h; reflowed(); }
  }
  // a segment's sliding tile was measured on the English label: re-seat the
  // tiles (no slide) once the Hebrew is in, one frame later, batched
  var rf = 0;
  function reflowed(){
    if(rf || !window.RK || !RK.segThumbs) return;
    rf = requestAnimationFrame(function(){ rf = 0; try{ RK.segThumbs(document, true); }catch(_){} });
  }
  function walk(root){
    if(LANG !== "he" || !root) return;
    if(root.nodeType === 3){ trText(root); return; }
    if(root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    if(root.nodeType === 1){ if(field(root)){ trEl(root); return; } if(skipped(root)) return; trEl(root); }
    var w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode: function(n){
        if(n.nodeType === 1){
          if(field(n)){ trEl(n); return NodeFilter.FILTER_REJECT; }
          return skipped(n) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT; }
        return NodeFilter.FILTER_ACCEPT; }});
    var n, texts = [];
    while((n = w.nextNode())){ if(n.nodeType === 1) trEl(n); else texts.push(n); }
    texts.forEach(trText);
  }
  if(LANG === "he"){
    var _c = window.confirm, _a = window.alert, _p = window.prompt;
    window.confirm = function(m){ return _c.call(window, tr(m)); };
    window.alert = function(m){ return _a.call(window, tr(m)); };
    window.prompt = function(m, d){ return _p.call(window, tr(m), d); };
    var start = function(){
      if(document.title) document.title = tr(document.title);
      walk(document.body);
      new MutationObserver(function(muts){
        muts.forEach(function(m){
          if(m.type === "characterData") trText(m.target);
          else if(m.type === "attributes") trEl(m.target);
          else m.addedNodes.forEach(walk);
        });
      }).observe(document.body, {childList: true, subtree: true, characterData: true,
                                 attributes: true, attributeFilter: ATTRS});
    };
    if(document.body) start(); else document.addEventListener("DOMContentLoaded", start);
  }
  return {lang: LANG, rtl: LANG === "he", t: tr, walk: walk};
})();
window.T = window.LIA_I18N.t;
// T.quiet: a value that MAY be a built-in label ("Default mic") or user data
// (a device name) - translated when known, never reported as a miss
window.T.quiet = function(s){ return window.LIA_I18N.t(s, true); };
"""


def page_js(lang=None):
    """The <script> body for an HTML window (always defines window.T)."""
    lang = lang or resolve()
    d = payload_json() if lang == "he" else "{}"
    return _I18N_JS.replace("__LANG__", lang).replace("__DICT__", d)


def html_attrs(lang=None):
    """(lang, dir) for the <html> element."""
    lang = lang or resolve()
    return (lang, "rtl" if lang == "he" else "ltr")
