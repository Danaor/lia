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
  * Light theme, warm neutral surfaces, a deep-ink primary (#272A4A); Lia's
    own colour (--lia, the gradient) only on Lia: her orb, her one button.
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
# "The room is calm, Lia is the light" (plans/UI_REDESIGN_PLAN_2026-10-13.md,
# the same tokens as Lia Lite 0.3.0 so the two apps read as one family):
# neutral warm surfaces; colour and motion belong to Lia (--lia*), red to
# recording only, failures are a calm amber. Light is the committed default;
# the dark block is inert unless the root carries data-theme="dark".
CSS_TOKENS = """
:root{
  /* surfaces: warm porcelain, never system grey */
  --bg:#F6F6F3; --card:#FFFFFF; --card-2:#FAFAF8; --sunk:#F0F0EC;
  --line:#EAEAE5; --line-2:#DADAD3;
  /* ink */
  --ink:#17171E; --ink-2:#3A3C46; --muted:#6D6F78; --faint:#A3A5AC;
  /* the primary button: a deep indigo ink (not a brand colour) */
  --deep:#272A4A; --deep-h:#33375E;
  --accent:#272A4A; --accent-h:#33375E; --accent-soft:#F0F0EC;
  --accent-soft-h:#E8E8E3; --on-accent:#FFFFFF;
  /* Lia's own colour - ONLY on Lia: the orb, her one button, her progress */
  --lia:#6A5BE2; --lia-2:#4C8DF0; --lia-ink:#5546C9; --lia-soft:#F2F0FD;
  --lia-line:rgba(106,91,226,.30);
  --lia-grad:linear-gradient(135deg,#8E70F2 0%,#6A5BE2 48%,#4C8DF0 100%);
  /* red is for recording only */
  --rec:#D63543; --rec-soft:#FCEEEF;
  /* semantic: failures and warnings are a calm amber, never an alarm */
  --ok:#23845A; --warn:#A8701C; --err:#B4424E; --info:#5546C9; --calm:#B07A22;
  --ok-soft:#EBF5EF; --warn-soft:#FBF5E9; --err-soft:#FBF0F1; --info-soft:#F2F0FD;
  /* radii */
  --r-s:10px; --r-m:14px; --r-l:18px; --r-pill:999px;
  /* spacing scale */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:20px; --sp-6:24px;
  /* type: Heebo (embedded, ui_kit.font_css), Segoe UI as the fallback */
  --font:"Heebo","Segoe UI Variable Text","Segoe UI",system-ui,Arial,sans-serif;
  --mono:"Cascadia Code","Cascadia Mono",Consolas,"Courier New",monospace;
  --fs-base:14px; --fs-title:28px; --fs-h1:20px; --fs-section:16px; --fs-label:13px;
  --fs-hint:12.5px; --fs-small:11.5px;
  /* depth: two shadows only */
  --shadow:0 1px 2px rgba(23,23,40,.04),0 10px 28px -14px rgba(23,23,40,.12);
  --shadow-sm:0 1px 2px rgba(23,23,40,.06);
  --float:0 2px 6px rgba(23,23,40,.05),0 22px 48px -18px rgba(38,30,110,.24);
  --ring:0 0 0 3px rgba(106,91,226,.20);
  /* motion: one curve */
  --ease:cubic-bezier(.2,.8,.2,1);
  --tr:.16s cubic-bezier(.2,.8,.2,1);
  /* toast: a dark pill in BOTH themes so its text + level dots stay readable */
  --toast-bg:#1F2135; --toast-fg:#FFFFFF;
  /* scrollbar - visible against the warm bg */
  --sb-thumb:#D0D0C9; --sb-thumb-h:#B8B8B0;
}
:root[data-theme="dark"]{
  --bg:#0f1016; --card:#17181f; --card-2:#1c1d25; --sunk:#14151b;
  --line:#272833; --line-2:#333544;
  --ink:#ececf2; --ink-2:#c6c7d2; --muted:#9293a3; --faint:#6b6c7d;
  --deep:#b9b3f5; --deep-h:#cbc6f8;
  --accent:#b9b3f5; --accent-h:#cbc6f8; --accent-soft:#1e1f2a;
  --accent-soft-h:#272838; --on-accent:#14141c;
  --lia-soft:#1d1a35;
  --ok:#7fce9b; --warn:#e2b877; --err:#e79aa7; --info:#a99ef2;
  --ok-soft:#16241b; --warn-soft:#2a2113; --err-soft:#2a1418; --info-soft:#1d1a35;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 12px 32px rgba(0,0,0,.45);
  --shadow-sm:0 1px 2px rgba(0,0,0,.4);
  --ring:0 0 0 3px rgba(169,158,242,.28);
  --sb-thumb:#333544; --sb-thumb-h:#414457;
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
::selection{background:rgba(106,91,226,.22); color:inherit;}
a{color:var(--lia-ink); text-decoration:none;}
a:hover{text-decoration:underline;}
/* digits that never jump (counters, clocks, versions, times) */
.num,.tnum{font-variant-numeric:tabular-nums;}

/* --- layout shell: sidebar + content ------------------------------------ */
.shell{display:flex; height:100vh; overflow:hidden;}
.sidebar{
  width:196px; flex:0 0 196px; background:var(--sunk);
  border-inline-end:1px solid var(--line);
  padding:var(--sp-3) var(--sp-2); overflow-y:auto;
  display:flex; flex-direction:column; gap:1px;
}
.sidebar .brand{
  display:flex; flex-direction:column; align-items:center; gap:6px;
  padding:6px var(--sp-3) 10px; color:var(--ink);
}
/* the live brand: Lia's orb (SVG, ui_kit.orb_svg) beside the wordmark */
.sidebar .brand .brand-live{display:flex; align-items:center; gap:9px; user-select:none;}
.sidebar .brand .brand-live .orb{width:34px; height:34px;}
.sidebar .brand .brand-live .wm{height:19px; width:auto; display:block; margin-top:2px;}
/* one quiet line under the brand: what Lia is doing right now */
.sidebar .brand .brand-state{display:flex; align-items:center; gap:6px; font-size:12px;
  color:var(--muted); min-height:18px; max-width:100%;}
.sidebar .brand .brand-state .bs-t{overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.sidebar .brand .brand-state .rdot{width:7px; height:7px; border-radius:50%; background:var(--rec);
  flex:0 0 7px; animation:recPing 1.6667s steps(16) infinite; animation-delay:var(--d,0s);}
.sidebar .brand .brand-state .rdot[hidden]{display:none;}
.sidebar .brand .brand-full{height:42px; width:auto; display:block;}
.sidebar .brand .brand-txt{display:flex; flex-direction:column; line-height:1.15; min-width:0;}
.sidebar .brand .brand-name{font-size:var(--fs-h1); font-weight:700;}
/* the 'lia' wordmark image (dev gallery + Tk fallback) */
.sidebar .brand .brand-wordmark{height:22px; width:auto; align-self:flex-start; display:block;}
.sidebar .brand .logo{
  width:28px; height:28px; border-radius:8px; flex:0 0 28px;
  background:transparent; color:var(--on-accent);
  display:flex; align-items:center; justify-content:center;
  font-size:14px; font-weight:800; object-fit:contain;
}
.sidebar .brand img.logo{display:block;}
.nav-item{
  appearance:none; border:0; width:100%; text-align:start; cursor:pointer; position:relative;
  padding:7px 12px; margin:1px 0; border-radius:var(--r-s);
  color:var(--ink-2); background:transparent; font-family:inherit;
  font-size:var(--fs-base); font-weight:500; transition:background var(--tr),color var(--tr);
  display:flex; align-items:center; gap:10px;
}
.nav-item:hover{background:rgba(23,23,40,.045); color:var(--ink);}
.nav-item:focus-visible{outline:none; box-shadow:var(--ring);}
/* the selected page: a white tile that lifts off the sunken rail, a thin bar
   of Lia's colour at its start, and ink text */
.nav-item.on{background:var(--card); color:var(--ink); font-weight:600;
  box-shadow:0 1px 2px rgba(23,23,40,.06),0 4px 12px -6px rgba(23,23,40,.10);}
.nav-item.on::before{content:""; position:absolute; inset-inline-start:0; top:8px; bottom:8px;
  width:3px; border-radius:3px; background:var(--lia-grad);}
.nav-item .ico{display:inline-flex; align-items:center; justify-content:center;
  font-size:15px; width:20px; height:20px; flex:0 0 20px; color:var(--muted);
  transition:color var(--tr);}
.nav-item:hover .ico{color:var(--ink-2);}
.nav-item.on .ico{color:var(--lia-ink);}
.nav-group{font-size:var(--fs-small); font-weight:600; color:var(--faint);
  padding:12px 12px 3px; letter-spacing:.2px;}
.nav-group:first-of-type{padding-top:2px;}
.sidebar .spacer{flex:1 1 auto;}

.content{flex:1 1 auto; overflow-y:auto; padding:var(--sp-5) var(--sp-6);}
.content-head{
  display:flex; align-items:center; gap:var(--sp-3);
  margin-bottom:6px;
}
.content-head h1{margin:0; font-size:26px; font-weight:750; letter-spacing:-.4px;
  line-height:1.2; color:var(--ink);}
.content-head .sub{color:var(--muted); font-size:var(--fs-hint);}

/* --- page cards --------------------------------------------------------- */
.page{
  background:var(--card); border:1px solid var(--line); border-radius:var(--r-m);
  padding:22px 20px; margin-bottom:var(--sp-4); box-shadow:var(--shadow);
}
.page.flush{padding:0; overflow:hidden;}
.section-title{
  font-size:var(--fs-section); font-weight:700; color:var(--ink);
  margin:0 0 var(--sp-3);
}
.section-title:not(:first-child){margin-top:var(--sp-5);}
.divider{height:1px; background:var(--line); margin:var(--sp-4) 0;}
.hint{font-size:var(--fs-hint); color:var(--muted); margin:6px 0 0;}

/* --- compact settings rows (2026-10-05 redesign, restyled 2026-10-13) ----
   One card per TOPIC (.grp, label above it in .grp-h), one ROW per setting
   (.srow: title + one-line description on the left, the control on the
   right). Short enums are a segmented control (.seg); long lists collapse
   into a <details class="more"> whose summary row shows the value (.cur). */
.grp-h{display:flex; align-items:center; gap:8px; font-size:12px; font-weight:700;
  color:var(--faint); margin:15px 6px 6px; letter-spacing:.2px;}
.grp-h .grp-act{margin-inline-start:auto; display:flex; gap:6px; font-weight:400;}
.grp-h .grp-sub{font-weight:400; color:var(--faint);}
.grp-h .btn{padding:3px 10px; font-size:var(--fs-label);}
.grp{background:var(--card); border:1px solid var(--line); border-radius:var(--r-m); overflow:hidden;
  box-shadow:var(--shadow);}
.grp > * + *{border-top:1px solid var(--line);}
.grp.danger{border-color:var(--line);}
.srow{display:flex; align-items:center; gap:16px; padding:9px 18px; min-height:47px;}
/* the description gives way first; the control keeps its natural width */
.srow-l{flex:1 1 0; min-width:0;}
.srow-t{font-size:var(--fs-base); color:var(--ink); font-weight:600; line-height:1.3;}
.srow-d{font-size:var(--fs-hint); color:var(--muted); margin-top:1px; line-height:1.38;}
.srow-c{flex:0 0 auto; display:flex; align-items:center; gap:8px; flex-wrap:nowrap;
  justify-content:flex-end; min-width:0;}
.srow-c select{max-width:240px; width:auto;}
.srow-c input[type=text],.srow-c input[type=password],.srow-c input[type=number]{width:220px;}
.srow .btn, .srow select, .srow input{padding-top:6px; padding-bottom:6px;}
.srow .btn{white-space:nowrap;}
.srow.dim{opacity:.55;}
/* a full-width body line inside a group (a list, a textarea, a note) */
.gbody{padding:12px 18px 14px;}
.gnote{padding:9px 18px; font-size:var(--fs-hint); color:var(--muted); background:var(--card-2);}
/* segmented control: a sunken rail; the chosen option is a white tile. When
   RK.segThumbs() has run (.thumbed) one shared tile SLIDES between options. */
.seg{display:inline-flex; position:relative; border-radius:12px; padding:3px; gap:2px;
  background:var(--sunk); flex-wrap:nowrap; isolation:isolate;}
.seg .radio{position:relative; z-index:1; margin:0; border:0; padding:5px 13px; border-radius:9px;
  background:transparent; font-weight:500; white-space:nowrap; min-width:0; flex:0 0 auto;
  color:var(--muted); transition:color var(--tr);}
.seg .radio .box{display:none;}
.seg .radio:hover{background:transparent; color:var(--ink);}
.seg .radio.on, .seg .radio:has(input:checked){background:var(--card); box-shadow:var(--shadow-sm);
  color:var(--ink); font-weight:600; border:0;}
.seg.thumbed .radio.on, .seg.thumbed .radio:has(input:checked){background:transparent; box-shadow:none;}
.seg .seg-ind{position:absolute; z-index:0; top:3px; bottom:3px; left:0; width:0; border-radius:9px;
  background:var(--card); box-shadow:0 1px 2px rgba(23,23,40,.08),0 4px 10px -4px rgba(23,23,40,.14);
  transition:left .26s var(--ease), width .26s var(--ease); pointer-events:none;}
.seg .seg-ind.still{transition:none;}
.seg .radio:has(input:focus-visible){box-shadow:var(--ring);}
.switch.bare{gap:0;}
/* collapsed picker: the chevron turns and the body opens softly */
details.more > summary{list-style:none; cursor:pointer; transition:background var(--tr);}
details.more > summary::-webkit-details-marker{display:none;}
details.more > summary:hover{background:var(--card-2);}
details.more > summary:focus-visible{outline:none; box-shadow:inset var(--ring);}
details.more{interpolate-size:allow-keywords;}
details.more::details-content{height:0; overflow:clip;
  transition:height .24s var(--ease), content-visibility .24s allow-discrete;}
details.more[open]::details-content{height:auto;}
.chev{display:inline-block; transition:transform .24s var(--ease); color:var(--faint); font-size:16px;
  width:12px; text-align:center; flex:0 0 12px;}
details.more[open] > summary .chev{transform:rotate(90deg); color:var(--ink-2);}
.more-body{padding:12px 18px 14px; border-top:1px solid var(--line); background:var(--card-2);}
.more-body .radio{margin-bottom:5px;}
.more-body .radio:last-child{margin-bottom:0;}
/* setting rows nested in a collapsed group (rarely changed switches) */
.more-body > .srow{padding-inline:0; min-height:44px;}
.more-body > .srow + .srow{border-top:1px solid var(--line);}
.cur{display:inline-flex; align-items:center; gap:7px; font-size:13px; color:var(--ink-2);
  font-weight:500; max-width:360px; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.cur .prov-ico{width:16px; height:16px; object-fit:contain; flex:0 0 16px;}
.cur.warn{color:var(--warn);} .cur.ok{color:var(--ok);} .cur.err{color:var(--err);}
.cur.muted{color:var(--faint);}
.srow-c .wb{margin-inline-start:0;}
/* action rows / grid (tools, app commands) */
.agrid{display:grid; grid-template-columns:1fr 1fr;}
.agrid > .arow{border-top:1px solid var(--line);}
.agrid > .arow:nth-child(-n+2){border-top:0;}
.agrid > .arow:nth-child(2n){border-inline-start:1px solid var(--line);}
.arow{display:flex; align-items:center; gap:10px; padding:10px 18px; min-height:44px; cursor:pointer;
  background:none; border:0; width:100%; text-align:start; font:inherit; color:var(--ink);
  transition:background var(--tr);}
.arow:hover{background:var(--card-2);}
.arow:focus-visible{outline:none; box-shadow:inset var(--ring);}
.arow .ic{display:inline-flex; color:var(--muted); flex:0 0 18px; transition:color var(--tr);}
.arow:hover .ic{color:var(--lia-ink);}
.arow .grow{flex:1 1 auto; min-width:0;}
.arow .hk{font-size:11px; color:var(--faint); font-family:var(--mono);}
.arow.danger{color:var(--err);} .arow.danger .ic{color:var(--err);}
.arow.disabledrow{opacity:.5; cursor:default; pointer-events:none;}
/* a row with a wide control (hotkey: input + 3 buttons) drops the control
   under its label sooner, before the label gets squeezed */
@media (max-width:900px){
  .srow.wide{flex-wrap:wrap;}
  .srow.wide .srow-c{flex-basis:100%; justify-content:flex-start;}
}
@media (max-width:760px){
  .srow{flex-wrap:wrap;}
  .srow-c{flex-wrap:wrap; justify-content:flex-start; flex-shrink:1;}
  .seg{flex-wrap:wrap;}
  .agrid{grid-template-columns:1fr;}
  .agrid > .arow:nth-child(2){border-top:1px solid var(--line);}
  .agrid > .arow:nth-child(2n){border-inline-start:0;}
}

/* --- setup-help: a compact collapsible how-to inside a card ------------- */
.setup-help{margin-top:var(--sp-3); border-top:1px solid var(--line); padding-top:var(--sp-3);}
.setup-help > summary{
  cursor:pointer; font-size:var(--fs-hint); font-weight:600; color:var(--lia-ink);
  list-style:none; user-select:none;
}
.setup-help > summary::-webkit-details-marker{display:none;}
.setup-help > summary::before{content:"▸ "; color:var(--muted);}
.setup-help[open] > summary::before{content:"▾ ";}
.setup-help ol{margin:var(--sp-3) 0 0; padding-inline-start:1.4em; font-size:var(--fs-hint); color:var(--ink-2);}
.setup-help ol li{margin:0 0 6px;}
.setup-help .hint{margin-top:var(--sp-3);}

/* --- Transcription-server page: GPU verdict tag + copyable address ----- */
.gpu-tag{margin-inline-start:auto; font-size:var(--fs-small); font-weight:700; text-transform:uppercase;
  letter-spacing:.03em; padding:2px 9px; border-radius:var(--r-pill);}
.gpu-tag.good{background:var(--ok-soft); color:var(--ok);}
.gpu-tag.marginal{background:var(--warn-soft); color:var(--warn);}
.gpu-tag.none{background:var(--sunk); color:var(--muted);}

.mono.copyable{display:inline-block; margin-top:4px; padding:3px 8px; border-radius:var(--r-s);
  background:var(--card-2); border:1px solid var(--line); user-select:all;}

/* --- privacy note: a calm callout, never an alarm ---------------------- */
.privacy-note{display:flex; gap:10px; align-items:flex-start;
  padding:var(--sp-3) var(--sp-4); margin-bottom:var(--sp-4);
  border:1px solid rgba(176,122,34,.28); border-radius:var(--r-m); background:var(--warn-soft);
  font-size:var(--fs-hint); color:var(--ink-2); line-height:1.5;}
.privacy-note .pn-ico{flex:none; font-size:15px; line-height:1.4;}

/* --- field: label + control + hint ------------------------------------- */
.field{margin-bottom:var(--sp-5);}
/* Group header: a normal sentence-case label above its controls (never ALL
   CAPS - plain labels read easier and are the house style). */
.field > label,.field-label{
  display:block; font-size:var(--fs-label); font-weight:600;
  color:var(--ink-2); margin-bottom:8px;
}
.field .hint{margin-top:6px;}
.row-inline{display:flex; align-items:center; gap:var(--sp-2); flex-wrap:wrap;}

/* --- buttons ------------------------------------------------------------ */
.btn{
  appearance:none; font-family:inherit; font-size:var(--fs-base); font-weight:600;
  border:0; border-radius:var(--r-s); padding:8px 16px; cursor:pointer;
  background:var(--card); color:var(--ink);
  box-shadow:inset 0 0 0 1px var(--line-2),0 1px 2px rgba(23,23,40,.04);
  transition:background var(--tr),color var(--tr),box-shadow var(--tr),transform var(--tr);
  display:inline-flex; align-items:center; gap:8px; line-height:1.25; position:relative;
}
.btn:hover{background:var(--card-2); box-shadow:inset 0 0 0 1px #C9C9C1,0 2px 6px -2px rgba(23,23,40,.10);}
.btn:active{transform:scale(.98);}
.btn:focus-visible{outline:none; box-shadow:inset 0 0 0 1px var(--lia-line),var(--ring);}
.btn.primary{background:var(--deep); color:#fff;
  box-shadow:0 1px 2px rgba(23,23,40,.18),0 8px 18px -10px rgba(39,42,74,.55);}
.btn.primary:hover{background:var(--deep-h); transform:translateY(-1px);
  box-shadow:0 2px 4px rgba(23,23,40,.16),0 12px 22px -10px rgba(39,42,74,.55);}
.btn.primary:active{transform:translateY(0) scale(.98);}
/* the ONE button per window that asks Lia to work: her colour, a slow sheen */
.btn.lia{background:var(--lia-grad); color:#fff; overflow:hidden; isolation:isolate;
  box-shadow:0 1px 2px rgba(60,40,160,.20),0 10px 22px -10px rgba(106,91,226,.75);}
.btn.lia:hover{background:var(--lia-grad); transform:translateY(-1px);
  box-shadow:0 2px 4px rgba(60,40,160,.18),0 14px 26px -10px rgba(106,91,226,.8);}
.btn.lia::after{content:""; position:absolute; inset:0; z-index:-1; pointer-events:none;
  background:linear-gradient(100deg,transparent 35%,rgba(255,255,255,.38) 50%,transparent 65%);
  transform:translateX(-130%); animation:liaSheen 4.8s var(--ease) 3; animation-delay:var(--d,0s);}
.btn.lia:hover::after{animation:liaSheen 1.8s var(--ease) 1; animation-delay:0s;}
@keyframes liaSheen{0%{transform:translateX(-130%);} 55%,100%{transform:translateX(130%);}}
/* the record button: white, red text, a red dot whose ring wakes on hover (a
   constant pulse at rest would read as "recording" and costs frames) */
.btn.rec{background:var(--card); color:#C42F3D; font-weight:700; gap:9px; border-radius:var(--r-pill);
  padding:8px 18px 8px 15px;
  box-shadow:inset 0 0 0 1px rgba(214,53,67,.22),0 1px 2px rgba(214,53,67,.10),0 10px 22px -14px rgba(214,53,67,.55);}
.btn.rec:hover{background:#fff; transform:translateY(-1px);
  box-shadow:inset 0 0 0 1px rgba(214,53,67,.32),0 2px 4px rgba(214,53,67,.10),0 14px 26px -14px rgba(214,53,67,.6);}
.btn.rec .rdot{position:relative; width:9px; height:9px; border-radius:50%; background:var(--rec); flex:0 0 auto;}
.btn.rec .rdot::after{content:""; position:absolute; inset:-5px; border-radius:50%;
  border:1.5px solid rgba(214,53,67,.55); opacity:0;}
.btn.rec:hover .rdot::after,.btn.rec:focus-visible .rdot::after{animation:ring 2.2s ease-out infinite;}
@keyframes ring{0%{transform:scale(.5); opacity:1;} 100%{transform:scale(1.5); opacity:0;}}
.btn.ghost{background:transparent; color:var(--ink-2); box-shadow:inset 0 0 0 1px var(--line-2);}
.btn.ghost:hover{background:var(--card); color:var(--ink);}
.btn.danger{background:transparent; color:var(--err); box-shadow:inset 0 0 0 1px rgba(180,66,78,.35);}
.btn.danger:hover{background:var(--err-soft);}
.btn.icon{padding:7px; min-width:34px; justify-content:center;}
.btn:disabled,.btn.disabled{opacity:.5; cursor:default; pointer-events:none;}
.btn.sm{padding:5px 12px; font-size:12.5px; border-radius:9px; gap:6px;}
.btn.sm.icon{padding:5px; min-width:30px;}
.btn .ico-svg{flex:0 0 auto;}

/* --- inputs ------------------------------------------------------------- */
input[type=text],input[type=password],input[type=search],input[type=number],select,textarea,.input{
  width:100%; font-family:inherit; font-size:var(--fs-base); color:var(--ink);
  background:var(--card); border:1px solid var(--line-2); border-radius:var(--r-s);
  padding:8px 12px; transition:border-color var(--tr),box-shadow var(--tr),background var(--tr);
}
input.mono,textarea.mono,.mono{font-family:var(--mono);}
input::placeholder,textarea::placeholder{color:var(--faint);}
input:hover,select:hover,textarea:hover{border-color:#CBCBC3;}
input:focus,select:focus,textarea:focus,.input:focus{
  outline:none; border-color:rgba(106,91,226,.55); box-shadow:var(--ring); background:#fff;
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
  flex:0 0 18px; width:18px; height:18px; margin-top:1px; border-radius:6px;
  border:1px solid var(--line-2); background:var(--card); position:relative;
  transition:background var(--tr),border-color var(--tr),box-shadow var(--tr);
}
.check:hover .box{border-color:var(--deep);}
.check input:focus-visible ~ .box{box-shadow:var(--ring);}
.check input:checked ~ .box{background:var(--deep); border-color:var(--deep);}
.check input:checked ~ .box::after{
  content:""; position:absolute; inset-inline-start:5px; top:1px;
  width:5px; height:10px; border:solid #fff;
  border-width:0 2px 2px 0; transform:rotate(45deg);
}
.check.disabled{opacity:.5; pointer-events:none;}
.check .txt{flex:1 1 auto;}
.check .txt small{display:block; color:var(--muted); font-size:var(--fs-hint);}

/* --- radio: rendered as a selectable option ROW ------------------------- */
/* The SELECTED row gets Lia's soft tint, a fine Lia border, ink text and a
   filled dot - the current pick is unmistakable without shouting. */
.radio{
  display:flex; align-items:center; gap:10px; cursor:pointer; user-select:none;
  padding:8px 12px; margin-bottom:5px; color:var(--ink-2); line-height:1.25;
  border:1px solid var(--line); border-radius:var(--r-s); background:var(--card);
  transition:background var(--tr),border-color var(--tr),color var(--tr);
}
.radio:last-child{margin-bottom:0;}
/* Where-badges: LOCAL / CLOUD / HOME GPU / BETA on option rows (Models page).
   Quiet pills: a small coloured dot carries the meaning, the pill stays neutral,
   so the model NAME stays the thing the eye reads first. */
.wb{margin-inline-start:auto; flex:0 0 auto; font-size:10px; font-weight:700;
    letter-spacing:.08em; padding:2px 9px 2px 8px; border-radius:999px;
    white-space:nowrap; background:var(--sunk); color:var(--ink-2); border:0;
    display:inline-flex; align-items:center; gap:5px;}
.wb::before{content:""; width:6px; height:6px; border-radius:50%; background:var(--faint); flex:0 0 6px;}
.wb.local::before{background:var(--ok);}
.wb.cloud::before{background:var(--lia-2);}
.wb.remote::before{background:var(--lia);}
.wb.beta{color:var(--warn); background:var(--warn-soft);}
.wb.beta::before{background:var(--calm);}
/* badge + hint on ONE line (badge pinned far right for column scanning, hint to
   its left) so an option row stays a single line and the list reads tight. */
.wb-group{margin-inline-start:auto; display:flex; flex-direction:row-reverse;
          align-items:center; gap:10px; min-width:0;}
.wb-group .wb{margin-inline-start:0;}
.wb-sub{font-size:11px; color:var(--muted); white-space:nowrap; text-align:end;
        letter-spacing:normal; font-weight:400; overflow:hidden; text-overflow:ellipsis;}
.radio input{position:absolute; opacity:0; width:0; height:0;}
.radio .box{
  flex:0 0 18px; width:18px; height:18px; border-radius:50%;
  border:1.5px solid var(--line-2); background:var(--card); position:relative;
  transition:border-color var(--tr);
}
.radio input:checked ~ .box{border-color:var(--lia);}
.radio input:checked ~ .box::after{
  content:""; position:absolute; inset:3.5px; border-radius:50%; background:var(--lia);
}
.radio:hover{background:var(--card-2); border-color:var(--line-2);}
.radio input:focus-visible ~ .box{box-shadow:var(--ring);}
/* selected: JS adds .on (survives re-render); :has() gives instant feedback on
   click. Kept as SEPARATE rules so an engine without :has() still honours .on. */
.radio.on{background:var(--lia-soft); border-color:var(--lia-line);
  color:var(--ink); font-weight:600;}
.radio:has(input:checked){background:var(--lia-soft); border-color:var(--lia-line);
  color:var(--ink); font-weight:600;}
.radio.disabled{opacity:.5; pointer-events:none;}
.radio .txt{flex:1 1 auto;}
.radio .txt small{display:block; color:var(--muted); font-size:var(--fs-hint); font-weight:400;}
/* horizontal radio group: a short enum on ONE line */
.radio-row{display:flex; flex-wrap:wrap; gap:8px;}
.radio-row .radio{margin-bottom:0; flex:1 1 0; min-width:150px;}

/* --- switch: slim, ink when on, a small spring in the knob -------------- */
.switch{display:inline-flex; align-items:center; gap:10px; cursor:pointer; user-select:none;}
.switch input{position:absolute; opacity:0; width:0; height:0;}
.switch .track{
  width:36px; height:21px; border-radius:var(--r-pill); background:#D9D9D2;
  position:relative; transition:background .22s var(--ease); flex:0 0 36px;
}
.switch .track::after{
  content:""; position:absolute; top:2.5px; inset-inline-start:2.5px;
  width:16px; height:16px; border-radius:50%; background:#fff;
  box-shadow:0 1px 3px rgba(0,0,0,.22); transition:transform .26s cubic-bezier(.3,1.4,.5,1);
}
.switch:hover .track{background:#CDCDC6;}
.switch input:checked ~ .track{background:var(--deep);}
.switch input:checked ~ .track::after{transform:translateX(15px);}
.switch input:focus-visible ~ .track{box-shadow:var(--ring);}
[dir=rtl] .switch input:checked ~ .track::after{transform:translateX(-15px);}
.switch.disabled{opacity:.5; pointer-events:none;}

/* --- rows / lists ------------------------------------------------------- */
.list{display:flex; flex-direction:column;}
.list .row{
  display:flex; align-items:center; gap:var(--sp-3);
  padding:11px 16px; border-bottom:1px solid var(--line);
  transition:background var(--tr); position:relative; cursor:default;
}
.list .row:last-child{border-bottom:0;}
.list .row.click{cursor:pointer;}
.list .row.click:hover{background:var(--card-2);}
.list .row.sel{background:var(--lia-soft);}
.list .row.sel::before{
  content:""; position:absolute; inset-inline-start:0; top:0; bottom:0;
  width:3px; background:var(--lia-grad);
}
.list .row .grow{flex:1 1 auto; min-width:0;}
.list .row .muted{color:var(--muted); font-size:var(--fs-hint);}

/* --- chip / badge ------------------------------------------------------- */
.chip{
  display:inline-flex; align-items:center; gap:6px;
  background:var(--sunk); color:var(--ink-2);
  border-radius:var(--r-pill); padding:3px 11px; font-size:12px; font-weight:600;
}
.badge{
  display:inline-flex; align-items:center; justify-content:center;
  min-width:20px; height:20px; padding:0 6px; border-radius:var(--r-pill);
  background:var(--deep); color:#fff;
  font-size:var(--fs-small); font-weight:700; font-variant-numeric:tabular-nums;
}
.badge.soft{background:var(--sunk); color:var(--ink-2);}
.kbd{
  display:inline-block; font-family:var(--mono); font-size:11.5px;
  background:var(--card); color:var(--ink-2);
  border:1px solid var(--line-2); border-bottom-width:2px; border-radius:6px;
  padding:1px 7px;
}

/* --- status line -------------------------------------------------------- */
.status{display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:var(--fs-hint);}
.status .dot{width:7px; height:7px; border-radius:50%; background:var(--ok); flex:0 0 7px;}
.status.warn .dot{background:var(--calm);} .status.warn{color:var(--warn);}
.status.err .dot{background:var(--err);} .status.err{color:var(--err);}
.status.busy .dot{background:var(--lia); animation:pulse 1.1s infinite;}
.status.ok .dot{background:var(--ok);}
.status.off .dot{background:var(--faint);}
@keyframes pulse{50%{opacity:.35;}}

/* --- spinner (Lia's colour: a spinner means she is working) ------------ */
.spinner{
  display:inline-block; width:14px; height:14px; vertical-align:-2px;
  border:2px solid var(--lia-soft); border-top-color:var(--lia);
  border-radius:50%; animation:spin .7s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg);}}

/* --- masked secret ------------------------------------------------------ */
.masked{font-family:var(--mono); font-size:12.5px; color:var(--ink-2); letter-spacing:.02em;}
.masked.empty{color:var(--faint);}

/* --- empty state -------------------------------------------------------- */
.empty{
  display:flex; flex-direction:column; align-items:center; gap:10px;
  padding:54px 24px; text-align:center; color:var(--muted);
}
.empty .big{font-size:34px; opacity:.55;}
.empty .sub{font-size:var(--fs-hint); color:var(--faint); max-width:300px;}

/* --- toast: a dark pill at the bottom ---------------------------------- */
#toast-host{
  position:fixed; inset-inline:0; bottom:22px; display:flex;
  flex-direction:column; align-items:center; gap:8px; pointer-events:none; z-index:999;
}
.toast{
  pointer-events:auto; background:var(--toast-bg); color:var(--toast-fg);
  border-radius:var(--r-pill); padding:10px 18px 10px 16px; font-size:13px; font-weight:600;
  box-shadow:0 2px 6px rgba(0,0,0,.12),0 18px 40px -14px rgba(20,18,60,.45);
  opacity:0; transform:translateY(14px) scale(.97);
  transition:opacity .28s var(--ease),transform .38s var(--ease); max-width:80vw;
  display:flex; align-items:center; gap:10px;
}
.toast.in{opacity:1; transform:none;}
.toast .tdot{width:8px; height:8px; border-radius:50%; background:var(--toast-fg); flex:0 0 8px;}
.toast.ok .tdot{background:#62d39a; box-shadow:0 0 0 3px rgba(98,211,154,.18);}
.toast.warn .tdot{background:#f0c274; box-shadow:0 0 0 3px rgba(240,194,116,.18);}
.toast.err .tdot{background:#f0a8b3; box-shadow:0 0 0 3px rgba(240,168,179,.18);}
.toast.info .tdot{background:#a99ef2; box-shadow:0 0 0 3px rgba(169,158,242,.2);}

/* --- content that may be Hebrew: let the browser pick direction --------- */
.rtl-auto,[dir=auto]{unicode-bidi:plaintext;}

/* --- scrollbar ---------------------------------------------------------- */
::-webkit-scrollbar{width:12px; height:12px;}
::-webkit-scrollbar-thumb{background:var(--sb-thumb); border-radius:8px; border:4px solid var(--bg); min-height:48px;}
::-webkit-scrollbar-thumb:hover{background:var(--sb-thumb-h);}
::-webkit-scrollbar-track{background:transparent;}
.sidebar::-webkit-scrollbar-thumb{border-color:var(--sunk);}

/* --- local SVG icons (inline, currentColor, uniform stroke) -------------- */
.ico-svg{display:inline-block; vertical-align:middle; flex:0 0 auto; color:inherit;}
/* icon-only button: MUST carry an accessible name (aria-label or title) */
.iconbtn{appearance:none; border:0; background:transparent; color:var(--muted);
  cursor:pointer; padding:6px; border-radius:var(--r-s); display:inline-flex;
  align-items:center; justify-content:center; transition:background var(--tr),color var(--tr);}
.iconbtn:hover{background:var(--sunk); color:var(--ink);}
.iconbtn:focus-visible{outline:none; box-shadow:var(--ring);}

/* --- Lia's orb: the three petals of the logo as a live SVG -------------
   It breathes while Lia is free, turns while she works (.busy), and gathers
   from three petals the first time a window is seen (.arrive). Continuous
   animations take --d (RK.ad) so a re-render keeps their phase. Ambient motion
   is STEPPED at 12 fps (eased keyframe samples), every duration and delay a
   whole number of 1/12 s ticks so one orb's parts step together. In WebView2
   an every-frame animation costs ~15-25% of a core while shown; stepped, the
   busy orb costs ~12% and the resting one ~8% (measured, hidden_cpu.py). At
   these sizes a step is under a pixel, so it reads the same. */
.orb{position:relative; display:inline-block; width:34px; height:34px; flex:0 0 auto;}
.orb svg{position:absolute; inset:0; width:100%; height:100%; overflow:visible;}
.orb .petals{transform-origin:38px 38px; animation:lwSpin 14s steps(168) infinite;
  animation-delay:var(--d,0s); animation-play-state:paused;}
.orb.busy .petals{animation-play-state:running;}
.orb .breath{transform-origin:38px 38px; animation:orbBreath 5.3333s steps(8) infinite;}
.orb .petal{transform-box:fill-box; transform-origin:center;}
.orb.busy .petal{animation:lwBreathe 2.5s steps(5) infinite;}
.orb.busy .petal.b{animation-delay:-.8333s;} .orb.busy .petal.c{animation-delay:-1.6667s;}
.orb.arrive .pa{animation:petalIn .9s var(--ease) both;}
.orb.arrive .pb{animation:petalIn .9s var(--ease) .08s both;}
.orb.arrive .pc{animation:petalIn .9s var(--ease) .16s both;}
.orb .halo{position:absolute; inset:-30%; border-radius:50%;
  background:radial-gradient(closest-side, rgba(106,91,226,.22), transparent);
  opacity:0; transition:opacity .6s var(--ease); pointer-events:none;}
.orb.busy .halo{opacity:1; animation:haloPulse 2.5s steps(5) infinite;}
.orb.sm{width:18px; height:18px;} .orb.md{width:44px; height:44px;}
.orb.lg{width:72px; height:72px;} .orb.xl{width:112px; height:112px;}
@keyframes lwSpin{to{transform:rotate(360deg);}}
@keyframes lwBreathe{0%{transform:scale(1.0000);} 16.667%{transform:scale(0.9650);} 33.333%{transform:scale(0.8950);} 50%{transform:scale(0.8600);} 66.667%{transform:scale(0.8950);} 83.333%{transform:scale(0.9650);} 100%{transform:scale(1.0000);}}
@keyframes petalIn{from{opacity:0; transform:translate(0,-14px) scale(.6);} to{opacity:1; transform:none;}}
@keyframes orbBreath{0%{transform:scale(1.0000);} 12.5%{transform:scale(1.0066);} 25%{transform:scale(1.0225);} 37.5%{transform:scale(1.0384);} 50%{transform:scale(1.0450);} 62.5%{transform:scale(1.0384);} 75%{transform:scale(1.0225);} 87.5%{transform:scale(1.0066);} 100%{transform:scale(1.0000);}}
@keyframes haloPulse{0%{transform:scale(1.0000); opacity:1.000;} 16.667%{transform:scale(1.0375); opacity:0.888;} 33.333%{transform:scale(1.1125); opacity:0.663;} 50%{transform:scale(1.1500); opacity:0.550;} 66.667%{transform:scale(1.1125); opacity:0.662;} 83.333%{transform:scale(1.0375); opacity:0.888;} 100%{transform:scale(1.0000); opacity:1.000;}}
@keyframes recPing{0%{box-shadow:0 0 0 0 rgba(214,53,67,.45);} 80%,100%{box-shadow:0 0 0 7px rgba(214,53,67,0);}}

/* --- entrances (played ONCE: RK.once decides) --------------------------- */
@keyframes rise{from{opacity:0; transform:translateY(8px);} to{opacity:1; transform:none;}}
@keyframes viewIn{from{opacity:0; transform:translateY(6px);} to{opacity:1; transform:none;}}
@keyframes fadeIn{from{opacity:0;}}
@keyframes pop{from{opacity:0; transform:scale(.96) translateY(6px);}}
@keyframes born{0%{opacity:0; transform:scale(.94) translateY(14px); filter:blur(6px);}
  60%{opacity:1; filter:blur(0);} 100%{transform:none;}}
@keyframes glowOut{from{box-shadow:0 0 0 2px rgba(106,91,226,.65),0 0 30px rgba(106,91,226,.38);}
  to{box-shadow:0 0 0 2px rgba(106,91,226,0),0 0 0 rgba(106,91,226,0);}}
.rise{animation:rise .42s var(--ease) both; animation-delay:var(--rd,0s);}
.vin{animation:viewIn .42s var(--ease);}
.born{position:relative; animation:born 1.1s var(--ease) both; animation-delay:var(--bd,0s);}
.born::after{content:""; position:absolute; inset:0; border-radius:inherit; pointer-events:none;
  animation:glowOut 2.6s ease-out forwards; animation-delay:var(--bd,0s);}

/* --- modal sheet (confirmations) --------------------------------------- */
.overlay{position:fixed; inset:0; background:rgba(28,28,40,.32); backdrop-filter:blur(6px);
  display:flex; align-items:center; justify-content:center; z-index:50; padding:20px;
  animation:fadeIn .2s var(--ease);}
.overlay .box{background:var(--card); border-radius:20px; padding:24px 26px 20px; max-width:440px;
  width:100%; box-shadow:0 2px 6px rgba(0,0,0,.06),0 40px 80px -30px rgba(30,24,80,.45);
  animation:pop .32s var(--ease);}
.overlay h2{margin:0 0 8px; font-size:18px; font-weight:700;}
.overlay p{margin:0 0 6px; overflow-wrap:anywhere; color:var(--ink-2);}
.overlay .acts{display:flex; gap:8px; justify-content:flex-end; margin-top:18px; flex-wrap:wrap;}

/* --- Ctrl+K command palette -------------------------------------------- */
.pal-bg{position:fixed; inset:0; z-index:60; background:rgba(28,28,40,.22); backdrop-filter:blur(5px);
  display:flex; align-items:flex-start; justify-content:center; padding:12vh 20px 20px;
  animation:fadeIn .16s var(--ease);}
.pal{width:min(620px,100%); background:rgba(255,255,255,.95); backdrop-filter:blur(20px) saturate(160%);
  border-radius:20px; padding:6px;
  box-shadow:0 0 0 1px rgba(23,23,40,.06),0 40px 90px -30px rgba(30,24,80,.5); animation:pop .26s var(--ease);}
.pal-in{display:flex; align-items:center; gap:10px; padding:4px 14px; color:var(--faint);}
.pal-in input{border:0; background:transparent; box-shadow:none; font-size:17px; padding:12px 0; color:var(--ink);}
.pal-in input:focus{box-shadow:none; background:transparent; border:0;}
.pal-list{list-style:none; margin:0; padding:6px; border-top:1px solid var(--line);
  max-height:min(440px,60vh); overflow:auto;}
.pal-list:empty{display:none;}
.pal-group{font-size:11.5px; font-weight:700; color:var(--faint); padding:8px 12px 4px; letter-spacing:.2px;}
.pal-list li.pi{display:flex; align-items:center; gap:12px; padding:8px 12px; border-radius:12px;
  cursor:pointer; font-weight:500; color:var(--ink);}
.pal-list li.pi.on{background:var(--lia-soft);}
.pal-list li .pic{width:30px; height:30px; border-radius:9px; display:grid; place-items:center;
  background:var(--sunk); color:var(--ink-2); flex:0 0 30px;}
.pal-list li.on .pic{background:#E9E5FC; color:var(--lia-ink);}
.pal-list li .pt{flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.pal-list li .ps{font-size:12px; color:var(--faint); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  max-width:40%;}
.pal-empty{padding:18px 16px 20px; color:var(--muted); font-size:13px;}

/* --- a window that is hidden or minimised runs no animation at all ------ */
html.paused *,html.paused *::before,html.paused *::after{animation-play-state:paused !important;}
/* --- a window that marks itself html.blurred (not the active window) rests
   its ambient motion; entrances and the sheen finish on their own ------- */
html.blurred .orb .petals,html.blurred .orb .breath,html.blurred .orb .petal,html.blurred .orb .halo,
html.blurred .rdot{animation-play-state:paused !important;}

/* --- honour the OS 'reduce motion' setting ------------------------------ */
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:.001ms !important; animation-iteration-count:1 !important;
    transition-duration:.001ms !important; scroll-behavior:auto !important;}
  .orb .petals,.orb .breath,.orb .petal,.orb .halo,.btn.lia::after,.btn.rec .rdot::after,
  .sidebar .brand .brand-state .rdot{animation:none !important;}
}
"""


# ============================================================
# Local SVG icon set (offline, MIT-original geometry, currentColor stroke)
# ============================================================
# feather/lucide-style: 24x24 viewBox, no fill, 2px round stroke. Authored here
# (not copied from a licensed set) so the app ships no web font / external icon
# dependency. Rendered by icon() in Python and RK.icon() in JS from the SAME map.
ICONS = {
    "models":     '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
    "audio":      '<path d="M4 13v-1a8 8 0 0 1 16 0v1"/><rect x="2.5" y="13" width="4.5" height="7" rx="1.6"/><rect x="17" y="13" width="4.5" height="7" rx="1.6"/>',
    "general":    '<line x1="4" y1="8" x2="20" y2="8"/><line x1="4" y1="16" x2="20" y2="16"/><circle cx="15" cy="8" r="2.4"/><circle cx="9" cy="16" r="2.4"/>',
    "cleanup":    '<path d="M5 19L15 9"/><path d="M13 7l4 4"/><path d="M18 3l.7 1.8L20.5 5.5l-1.8.7L18 8l-.7-1.8L15.5 5.5l1.8-.7z"/>',
    "keys":       '<circle cx="8" cy="8" r="4"/><path d="M11 11l8 8"/><path d="M16 16l2-2"/>',
    "meetings":   '<circle cx="9" cy="8" r="3.1"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M15.5 5.6a3 3 0 0 1 0 5.6"/><path d="M17.5 19a5.4 5.4 0 0 0-2.4-4.4"/>',
    "vocabulary": '<path d="M5 4h11a2 2 0 0 1 2 2v14H7a2 2 0 0 1-2-2z"/><path d="M5 18a2 2 0 0 1 2-2h11"/>',
    "snippets":   '<path d="M9 8l-4 4 4 4"/><path d="M15 8l4 4-4 4"/>',
    "server":     '<rect x="4" y="4" width="16" height="6" rx="1.6"/><rect x="4" y="14" width="16" height="6" rx="1.6"/><line x1="7" y1="7" x2="7.01" y2="7"/><line x1="7" y1="17" x2="7.01" y2="17"/>',
    "advanced":   '<circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16.5"/><line x1="12" y1="7.6" x2="12.01" y2="7.6"/>',
    "home":       '<path d="M4 11l8-7 8 7"/><path d="M6 10v9h12v-9"/>',
    "history":    '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2"/>',
    "tasks":      '<path d="M9.5 6h10M9.5 12h10M9.5 18h10"/><path d="M4 5.6l1.3 1.3L7.4 4.7"/><path d="M4 11.6l1.3 1.3L7.4 10.7"/><path d="M4.2 17.4h.01"/>',
    "search":     '<circle cx="10.5" cy="10.5" r="6"/><line x1="15" y1="15" x2="20" y2="20"/>',
    "folder":     '<path d="M4 7a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/>',
    "file":       '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/><line x1="9.5" y1="12.5" x2="15" y2="12.5"/><line x1="9.5" y1="16" x2="15" y2="16"/>',
    "edit":       '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M14 6l4 4"/>',
    "mic":        '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0"/><line x1="12" y1="17" x2="12" y2="21"/><line x1="8.5" y1="21" x2="15.5" y2="21"/>',
    "ask":        '<circle cx="12" cy="12" r="9"/><path d="M9.4 9.4a2.6 2.6 0 1 1 3.6 2.4c-.9.4-1 .9-1 1.7"/><line x1="12" y1="16.6" x2="12.01" y2="16.6"/>',
    "email":      '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M4 7l8 6 8-6"/>',
    "chat":       '<path d="M4 5h16v11H9l-4 3v-3H4z"/>',
    "copy":       '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>',
    "check":      '<path d="M5 12l4 4 10-10"/>',
    "close":      '<path d="M6 6l12 12M18 6L6 18"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "chevron-down":  '<path d="M6 9l6 6 6-6"/>',
    "download":   '<path d="M12 4v10"/><path d="M8 11l4 4 4-4"/><path d="M5 19h14"/>',
    "refresh":    '<path d="M20 11a8 8 0 1 0-1.6 4.5"/><path d="M20 5v6h-6"/>',
    "external":   '<path d="M14 4h6v6"/><path d="M20 4l-8 8"/><path d="M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"/>',
    "alert":      '<path d="M12 4l9 16H3z"/><line x1="12" y1="10" x2="12" y2="15"/><line x1="12" y1="17.5" x2="12.01" y2="17.5"/>',
    "play":       '<path d="M7 5l12 7-12 7z"/>',
    "stop":       '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "summarize":  '<line x1="5" y1="7" x2="19" y2="7"/><line x1="5" y1="12" x2="19" y2="12"/><line x1="5" y1="17" x2="14" y2="17"/>',
    "settings":   '<circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="6.6"/><path d="M12 2.6v2.8M12 18.6v2.8M2.6 12h2.8M18.6 12h2.8M5.4 5.4l2 2M16.6 16.6l2 2M5.4 18.6l2-2M16.6 7.4l2-2"/>',
    "spark":      '<path d="M12 3.5l1.9 5 5 1.9-5 1.9-1.9 5-1.9-5-5-1.9 5-1.9z"/><path d="M18.5 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
    "bolt":       '<path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/>',
    "more":       '<circle cx="5.5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="18.5" cy="12" r="1.2"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="M13 6l6 6-6 6"/>',
    "keyboard":   '<rect x="2.5" y="6" width="19" height="12" rx="2.2"/><path d="M6.5 10h.01M10 10h.01M13.5 10h.01M17 10h.01M8 14h8"/>',
}


def icon(name, *, size=18, cls="", title=None, stroke=1.75):
    """Inline SVG for `name` (currentColor). Pass `title` for an accessible name
    on a standalone/meaningful icon; without it the icon is aria-hidden (decorative,
    the adjacent text label carries the meaning). Unknown name -> "" (caller may
    fall back). Never fetches anything."""
    body = ICONS.get(name)
    if not body:
        return ""
    cls_attr = (' ' + _h(cls)) if cls else ''
    if title:
        a11y = 'role="img" aria-label="%s"' % _h(title)
        inner = '<title>%s</title>%s' % (_h(title), body)
    else:
        a11y = 'aria-hidden="true"'
        inner = body
    return ('<svg class="ico-svg%s" width="%d" height="%d" viewBox="0 0 24 24" '
            'fill="none" stroke="currentColor" stroke-width="%s" '
            'stroke-linecap="round" stroke-linejoin="round" %s>%s</svg>'
            % (cls_attr, size, size, stroke, a11y, inner))


# ============================================================
# The typeface: Heebo (variable, SIL OFL 1.1 - fonts/HEEBO_OFL.txt), embedded
# as data URIs (the CSP allows font-src data: only). Hebrew + Latin subsets,
# ~42 KB together, the same files as Lia Lite. Missing files -> "" and the
# page falls back to Segoe UI.
# ============================================================
_FONT_FILES = (
    ("heebo-hebrew.woff2", "U+0307-0308,U+0590-05FF,U+200C-2010,U+20AA,U+25CC,U+FB1D-FB4F"),
    ("heebo-latin.woff2", "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,"
                          "U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD"),
)
_FONT_CSS = None


def font_css():
    """The @font-face rules of the embedded typeface ("" when the files are
    missing). Built once per process."""
    global _FONT_CSS
    if _FONT_CSS is not None:
        return _FONT_CSS
    import base64
    out = []
    here = os.path.dirname(os.path.abspath(__file__))
    for name, rng in _FONT_FILES:
        try:
            with open(os.path.join(here, "fonts", name), "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            _warn("font_css(%s): %s\n" % (name, e))
            continue
        out.append("@font-face{font-family:'Heebo';font-style:normal;font-weight:100 900;"
                   "font-display:block;src:url(data:font/woff2;base64,%s) format('woff2');"
                   "unicode-range:%s;}" % (b64, rng))
    _FONT_CSS = "\n".join(out) + ("\n" if out else "")
    return _FONT_CSS


# ============================================================
# Lia's orb: the three petals of the logo as a live SVG (the same geometry as
# Lia Lite). One egg path at 0/120/240 degrees on a 76x76 box; the gradients
# live ONCE per page in ORB_DEFS (page() appends them), so every orb shares them.
# ============================================================
ORB_EGG = ("M37 3.5C48.5 2.5 57 7 56.5 16C56 24 47 30.5 41.5 33.5C39.5 34.6 37.5 34.7 "
           "35.5 33.8C30 31.2 20 25.5 19.8 17C19.6 8.5 26.5 4.4 37 3.5Z")
ORB_INNER = ('<span class="halo"></span><svg viewBox="0 0 76 76" aria-hidden="true">'
             '<g class="breath"><g class="petals">'
             '<g class="pa"><path class="petal a" fill="url(#liaOrbA)" d="%s"/></g>'
             '<g transform="rotate(120 38 38)"><g class="pc"><path class="petal b" fill="url(#liaOrbC)" d="%s"/></g></g>'
             '<g transform="rotate(240 38 38)"><g class="pb"><path class="petal c" fill="url(#liaOrbB)" d="%s"/></g></g>'
             '</g></g></svg>' % (ORB_EGG, ORB_EGG, ORB_EGG))
ORB_DEFS = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false"><defs>'
            '<linearGradient id="liaOrbA" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#a38af3"/>'
            '<stop offset="1" stop-color="#7656e3"/></linearGradient>'
            '<linearGradient id="liaOrbB" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6a78ee"/>'
            '<stop offset="1" stop-color="#4c58d8"/></linearGradient>'
            '<linearGradient id="liaOrbC" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6aa6f4"/>'
            '<stop offset="1" stop-color="#4380ea"/></linearGradient>'
            '<linearGradient id="liaGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8E70F2"/>'
            '<stop offset="1" stop-color="#4C8DF0"/></linearGradient>'
            '</defs></svg>')


def orb_svg(cls="", id_=None):
    """Lia's orb as inline markup (`cls`: "busy", "sm", "lg", "arrive" ...).
    Decorative (aria-hidden); the adjacent text carries the meaning."""
    return '<span class="orb%s"%s>%s</span>' % (
        (" " + _h(cls)) if cls else "", (' id="%s"' % _h(id_)) if id_ else "", ORB_INNER)


# ============================================================
# brand_html: the new look for a window that builds its OWN page (Ask your
# meetings, Action items, Chat, Email search, Live transcript, the summary
# editor). They keep their structure and their own CSS; this adds what they
# share with ui_kit: the typeface, Lia's colour tokens, her orb (markup via the
# __LIA_ORB__ placeholder, gradients once), the entrance keyframes, and the
# motion rules (paused while hidden, nothing for reduced motion).
# ============================================================
def _css_between(start, end):
    i = CSS_BASE.index(start)
    return CSS_BASE[i:CSS_BASE.index(end, i)]


LIA_MOTION_CSS = (
    _css_between("/* --- Lia's orb", "/* --- modal sheet")
    + "html.paused *,html.paused *::before,html.paused *::after{animation-play-state:paused !important;}\n"
    "@media (prefers-reduced-motion: reduce){\n"
    "  *,*::before,*::after{animation-duration:.001ms !important; animation-iteration-count:1 !important;\n"
    "    transition-duration:.001ms !important;}\n"
    "  .orb .petals,.orb .breath,.orb .petal,.orb .halo,.btn.lia::after,.lia-btn::after{animation:none !important;}\n"
    "}\n")
BRAND_VARS = (":root{--lia:#6A5BE2; --lia-2:#4C8DF0; --lia-ink:#5546C9; --lia-soft:#F2F0FD;"
              " --lia-line:rgba(106,91,226,.30);"
              " --lia-grad:linear-gradient(135deg,#8E70F2 0%,#6A5BE2 48%,#4C8DF0 100%);"
              " --rec:#D63543; --ease:cubic-bezier(.2,.8,.2,1); --calm:#B07A22;"
              " --sunk:#F0F0EC; --ring:0 0 0 3px rgba(106,91,226,.20);"
              " --shadow:0 1px 2px rgba(23,23,40,.04),0 10px 28px -14px rgba(23,23,40,.12);"
              " --float:0 2px 6px rgba(23,23,40,.05),0 22px 48px -18px rgba(38,30,110,.24);}\n")
# the one button that asks Lia to work, for windows that do not use .btn
LIA_BTN_CSS = (".lia-btn{position:relative; overflow:hidden; isolation:isolate; background:var(--lia-grad) !important;"
               " color:#fff !important; box-shadow:0 1px 2px rgba(60,40,160,.20),0 10px 22px -10px rgba(106,91,226,.75);"
               " transition:transform .16s var(--ease), box-shadow .16s var(--ease);}\n"
               ".lia-btn:hover:not(:disabled){transform:translateY(-1px);"
               " box-shadow:0 2px 4px rgba(60,40,160,.18),0 14px 26px -10px rgba(106,91,226,.8);}\n"
               ".lia-btn::after{content:\"\"; position:absolute; inset:0; z-index:-1; pointer-events:none;"
               " background:linear-gradient(100deg,transparent 35%,rgba(255,255,255,.38) 50%,transparent 65%);"
               " transform:translateX(-130%); animation:liaSheen 4.8s var(--ease) 3;}\n"
               ".lia-btn:hover:not(:disabled)::after{animation:liaSheen 1.8s var(--ease) 1;}\n"
               "@keyframes liaSheen{0%{transform:translateX(-130%);} 55%,100%{transform:translateX(130%);}}\n")
PAUSED_JS = ('<script>(function(){function s(){document.documentElement.classList.toggle("paused",'
             '!!document.hidden);}document.addEventListener("visibilitychange",s);s();})();</script>')


def brand_html(html):
    """`html` (a window's own full page) with the CSP, the typeface, Lia's
    tokens + orb + motion rules, and __LIA_ORB__ replaced by the orb's inner
    markup (safe inside a single-quoted JS string: it has no single quote).
    Idempotent."""
    html = with_csp(html)
    if 'id="lia-brand"' in html:
        return html
    style = ('<style id="lia-brand">\n' + font_css() + BRAND_VARS + LIA_MOTION_CSS + LIA_BTN_CSS
             + "</style>\n")
    i = html.find("Content-Security-Policy")
    j = html.find(">", i) + 1 if i >= 0 else -1
    if j > 0:
        html = html[:j] + "\n" + style + html[j:]
    else:
        html = style + html
    html = html.replace("__LIA_ORB__", ORB_INNER)
    k = html.lower().rfind("</body>")
    tail = ORB_DEFS + "\n" + PAUSED_JS + "\n"
    return (html[:k] + tail + html[k:]) if k >= 0 else (html + tail)


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
  // ---- motion helpers (plans/UI_REDESIGN_PLAN_2026-10-13.md, 8.2/8.3) ----
  var reduce = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  // A continuous animation keeps its phase through a re-render: style="--d:"+ad(P)
  // (snapped to 1/12 s, the tick every ambient step runs on)
  function ad(period){ return "-" + (Math.round(((Date.now()/1000) % period) * 12) / 12).toFixed(4) + "s"; }
  // true the FIRST time `key` is seen in `map` (an entrance plays once)
  function once(map, key){ if(map[key]) return false; map[key] = Date.now(); return true; }
  // re-render without losing the caret: the focused field carries data-keep
  function keepFocus(fn){
    var a = document.activeElement, key = a && a.getAttribute ? a.getAttribute("data-keep") : null;
    var s = 0, e = 0;
    if(key){ try{ s = a.selectionStart; e = a.selectionEnd; }catch(x){} }
    fn();
    if(key){ var n = document.querySelector('[data-keep="' + key + '"]');
      if(n){ n.focus(); try{ n.setSelectionRange(s, e); }catch(x){} } }
  }
  // one white tile that SLIDES between the options of every .seg under root;
  // it starts from where the same control's tile was before the re-render
  var SEGPOS = {};
  function segThumbs(root, instant){
    (root || document).querySelectorAll(".seg").forEach(function(seg){
      var on = seg.querySelector(".radio.on") || seg.querySelector(".radio:has(input:checked)");
      var inp = seg.querySelector("input"), key = seg.getAttribute("data-seg") || (inp && inp.name) || "";
      var ind = seg.querySelector(".seg-ind");
      if(!on){ if(ind) ind.style.opacity = "0"; return; }
      if(!ind){ ind = el("span", {"class":"seg-ind", "aria-hidden":"true"}); seg.insertBefore(ind, seg.firstChild); }
      seg.classList.add("thumbed");
      var to = {l: on.offsetLeft, w: on.offsetWidth}, from = key && SEGPOS[key];
      if(from && !reduce && !instant && (from.l !== to.l || from.w !== to.w)){
        ind.classList.add("still"); ind.style.left = from.l + "px"; ind.style.width = from.w + "px";
        void ind.offsetWidth; ind.classList.remove("still");
      } else { ind.classList.add("still"); void ind.offsetWidth; requestAnimationFrame(function(){ ind.classList.remove("still"); }); }
      ind.style.opacity = "1"; ind.style.left = to.l + "px"; ind.style.width = to.w + "px";
      if(key) SEGPOS[key] = to;
    });
  }
  // the tile follows its option when the layout moves under it (no slide):
  // Heebo arriving after the first paint (the options widen/narrow - a tile
  // measured on the fallback font stuck out of its track) or a resize
  function segRefresh(){ segThumbs(document, true); }
  if(document.fonts){
    try{ document.fonts.addEventListener("loadingdone", segRefresh); }catch(_){}
    if(document.fonts.ready) document.fonts.ready.then(segRefresh);
  }
  var segRT = 0;
  window.addEventListener("resize", function(){ clearTimeout(segRT); segRT = setTimeout(segRefresh, 120); });
  // a click on a segment moves the tile at once (the state push re-renders later)
  document.addEventListener("change", function(e){
    var sg = e.target && e.target.closest ? e.target.closest(".seg") : null;
    if(sg && sg.parentNode){
      sg.querySelectorAll(".radio").forEach(function(r){
        var i = r.querySelector("input"); if(i) r.classList.toggle("on", i.checked); });
      segThumbs(sg.parentNode);
    }
  }, true);
  // nothing animates while the window is hidden (pre-warmed) or minimised
  function syncPaused(){ document.documentElement.classList.toggle("paused", !!document.hidden); }
  document.addEventListener("visibilitychange", syncPaused);
  syncPaused();
  return { esc:esc, $:$, el:el, toast:toast, ready:ready, onPush:onPush,
           ad:ad, once:once, keepFocus:keepFocus, segThumbs:segThumbs, reduce:reduce,
           get api(){ return api; } };
})();
"""

# Expose the SAME icon set + Lia's orb to JS-built HTML (RK.icon, RK.orb), so
# Python and JS render identical markup from one source of truth. Appended
# after JS_BASE so RK exists.
JS_BASE = JS_BASE + (
    "\nRK.ICONS = " + json.dumps(ICONS, ensure_ascii=True) + ";\n"
    "RK.icon = function(name, opts){ opts = opts || {};\n"
    "  var body = RK.ICONS[name]; if(!body) return '';\n"
    "  var size = opts.size || 18, stroke = opts.stroke || 1.75;\n"
    "  var cls = opts.cls ? (' ' + RK.esc(opts.cls)) : '';\n"
    "  var a11y, inner;\n"
    "  if(opts.title){ a11y = 'role=\"img\" aria-label=\"' + RK.esc(opts.title) + '\"';\n"
    "    inner = '<title>' + RK.esc(opts.title) + '</title>' + body; }\n"
    "  else { a11y = 'aria-hidden=\"true\"'; inner = body; }\n"
    "  return '<svg class=\"ico-svg' + cls + '\" width=\"' + size + '\" height=\"' + size +\n"
    "    '\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"' + stroke +\n"
    "    '\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ' + a11y + '>' + inner + '</svg>';\n"
    "};\n"
    "RK.ORB = " + json.dumps(ORB_INNER, ensure_ascii=True) + ";\n"
    "RK.orb = function(cls, id){ return '<span class=\"orb' + (cls ? ' ' + RK.esc(cls) : '') + '\"' +\n"
    "  (/busy/.test(cls || '') ? ' style=\"--d:' + RK.ad(14) + '\"' : '') +\n"
    "  (id ? ' id=\"' + RK.esc(id) + '\"' : '') + '>' + RK.ORB + '</span>'; };\n"
)


# ============================================================
# Page assembly - the ONLY place a full HTML document is built
# ============================================================
# Content-Security-Policy for every window (security audit 2026-09-26 F13):
# nothing is ever fetched from the network (no remote script, style, image,
# font, frame or connection) - even if an escaping bug ever let a meeting
# participant's or an e-mail's markup through, it could not beacon out or load
# code. Inline script/style stay allowed (every window is one inline page, and
# pywebview's bridge is injected by the host, not by a page script).
CSP = ("default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; "
       "style-src 'unsafe-inline'; img-src data:; font-src data:; "
       "base-uri 'none'; form-action 'none'")
CSP_META = '<meta http-equiv="Content-Security-Policy" content="%s">\n' % CSP


def with_csp(html):
    """`html` with CSP_META as the first element of its <head> (idempotent)."""
    if "Content-Security-Policy" in html:
        return html
    i = html.lower().find("<head>")
    if i == -1:
        return CSP_META + html
    i += len("<head>")
    return html[:i] + "\n" + CSP_META + html[i:]


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
        + CSP_META +
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>" + _h(title) + "</title>\n"
        + head_extra +
        "<style>\n" + font_css() + CSS_TOKENS + CSS_BASE + "\n" + extra_css + "\n</style>\n"
        "</head>\n"
        "<body>\n"
        + body_html + "\n" + ORB_DEFS + "\n"
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


_WORDMARK_URI_CACHE = None


def wordmark_data_uri():
    """Return the Lia WORDMARK (lia_text.png - the 'lia' lettering) as a base64
    `data:` URI, so the brand header shows the real wordmark image instead of a
    typeset "Lia". Cached; returns "" if missing so callers fall back to text."""
    global _WORDMARK_URI_CACHE
    if _WORDMARK_URI_CACHE is not None:
        return _WORDMARK_URI_CACHE
    try:
        import base64
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lia_text.png")
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        _WORDMARK_URI_CACHE = "data:image/png;base64," + b64
    except Exception as e:
        _warn("wordmark_data_uri: " + str(e))
        _WORDMARK_URI_CACHE = ""
    return _WORDMARK_URI_CACHE


_FULLLOGO_URI_CACHE = None


def full_logo_data_uri():
    """Return the FULL Lia logo (lia.png - the mark ABOVE the 'lia' wordmark)
    as a base64 `data:` URI, for the Settings brand header. Cached; "" if
    missing so the header degrades gracefully."""
    global _FULLLOGO_URI_CACHE
    if _FULLLOGO_URI_CACHE is not None:
        return _FULLLOGO_URI_CACHE
    try:
        import base64
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lia.png")
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        _FULLLOGO_URI_CACHE = "data:image/png;base64," + b64
    except Exception as e:
        _warn("full_logo_data_uri: " + str(e))
        _FULLLOGO_URI_CACHE = ""
    return _FULLLOGO_URI_CACHE


_ASSET_URI_CACHE = {}


def asset_data_uri(filename):
    """Return a PNG that sits next to this module as a base64 `data:` URI for
    inline <img>/CSS use - CSP-safe, no external asset. Cached per filename;
    returns "" if the file is missing so callers degrade gracefully."""
    if filename in _ASSET_URI_CACHE:
        return _ASSET_URI_CACHE[filename]
    uri = ""
    try:
        import base64
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        with open(p, "rb") as f:
            uri = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    except Exception as e:
        _warn("asset_data_uri(%s): %s" % (filename, e))
    _ASSET_URI_CACHE[filename] = uri
    return uri


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


def clamp_to_workarea(width, height, x=None, y=None, margin=16):
    """Clamp a window's size/position to the PRIMARY monitor's work area
    (the screen minus the taskbar), so windows never open touching or
    sliding behind the taskbar (2026-08-29 field complaint: the Settings
    window's bottom edge hid behind it).

    Returns {width, height, x, y}. With no x/y given the window is centered
    in the work area; a given position is clamped fully inside it. On any
    failure (non-Windows, API error) returns the inputs unchanged."""
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        if not ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            raise OSError("SPI_GETWORKAREA failed")
        wa_w = rect.right - rect.left
        wa_h = rect.bottom - rect.top
        width = max(360, min(int(width), wa_w - 2 * margin))
        height = max(320, min(int(height), wa_h - 2 * margin))
        if x is None or y is None:
            x = rect.left + (wa_w - width) // 2
            y = rect.top + (wa_h - height) // 2
        else:
            x = max(rect.left + margin,
                    min(int(x), rect.right - width - margin))
            y = max(rect.top + margin,
                    min(int(y), rect.bottom - height - margin))
        return {"width": width, "height": height, "x": x, "y": y}
    except Exception:
        out = {"width": width, "height": height}
        if x is not None and y is not None:
            out["x"] = x
            out["y"] = y
        return out


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


_DOTNET_PACKAGES = ("pythonnet", "clr_loader", "webview")


def unblock_dotnet_assemblies(roots=None):
    """Strip the Windows "Mark of the Web" (the Zone.Identifier alternate data
    stream) from the .NET / WebView2 DLLs pywebview loads. Returns how many
    streams were removed.

    WHY (2026-09-05 field bug): a Portable.zip downloaded by a browser carries
    the mark, and Explorer's extract propagates it to EVERY file. The .NET
    Framework refuses to load a managed assembly tagged like that (CAS policy:
    loadFromRemoteSources), so pythonnet fails with the opaque
    "Failed to resolve Python.Runtime.Loader.Initialize from ...
    Python.Runtime.dll" and NO pywebview window (Settings, History, ...) can
    open - in every launch shape, admin or not. Reproduced 1:1 by tagging the
    DLL; removing the stream fixes it. The mark only exists on NTFS; deleting
    the stream is a plain DeleteFile on "<path>:Zone.Identifier". Best-effort
    and silent: a read-only location (Program Files) simply keeps whatever it
    has (the installer never marks files anyway). Runs in a few ms.

    roots: explicit directories to sweep (tests); default = the installed
    pythonnet / clr_loader / webview packages, located WITHOUT importing them
    (importing webview in the parent is forbidden - see the lazy-import rule).
    """
    if os.name != "nt":
        return 0
    if roots is None:
        import importlib.util
        roots = []
        for pkg in _DOTNET_PACKAGES:
            try:
                spec = importlib.util.find_spec(pkg)
            except Exception:
                spec = None
            if spec is not None and spec.submodule_search_locations:
                roots.extend(list(spec.submodule_search_locations))
    removed = 0
    for root in roots:
        for d, _dirs, files in os.walk(root):
            for f in files:
                if not f.lower().endswith((".dll", ".exe")):
                    continue
                try:
                    os.remove(os.path.join(d, f) + ":Zone.Identifier")
                    removed += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
    return removed


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
    # A downloaded+extracted portable carries the Mark of the Web on every
    # DLL, and the .NET Framework then refuses to load Python.Runtime.dll
    # (see unblock_dotnet_assemblies). The parent sweeps at startup; this is
    # the belt for a child launched any other way.
    try:
        n = unblock_dotnet_assemblies()
        if n:
            _warn("ui_kit.child_main: unblocked %d marked .NET/WebView2 DLLs\n" % n)
    except Exception as e:
        _warn("ui_kit.child_main: unblock sweep failed: %r\n" % (e,))
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
