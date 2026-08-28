"""Dev tool: render every ui_kit component to one page for visual review.

Usage:
  python -X utf8 ui_kit_gallery.py            # open in a pywebview window
  python -X utf8 ui_kit_gallery.py --html P   # write the HTML to path P and exit
  python -X utf8 ui_kit_gallery.py --dark ...  # force the dark theme

Not shipped in any menu - a design QA harness only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_kit as uk


def _body():
    # A mixed Hebrew+Latin line to prove dir="auto" inside the LTR chrome.
    rtl_sample = ('דיברנו על ה-Landing Zone ותקציב של 30% ל-Q4 עם GuardDuty '
                  'ואיזור Entra ID.')
    return ("""
<div class="shell">
  <nav class="sidebar">
    <div class="brand"><img class="logo" src="__LOGO_SRC__" alt=""><span>Lia</span></div>
    <button class="nav-item on"><span class="ico">&#9881;</span>General</button>
    <button class="nav-item"><span class="ico">&#127911;</span>Audio</button>
    <button class="nav-item"><span class="ico">&#129504;</span>Models</button>
    <button class="nav-item"><span class="ico">&#10024;</span>AI Cleanup</button>
    <button class="nav-item"><span class="ico">&#128273;</span>Keys &amp; Server</button>
    <button class="nav-item"><span class="ico">&#128220;</span>Meetings</button>
    <button class="nav-item"><span class="ico">&#128218;</span>Vocabulary</button>
    <div class="spacer"></div>
    <button class="nav-item"><span class="ico">&#8505;</span>Advanced</button>
  </nav>
  <main class="content">
    <div class="content-head">
      <h1>Component gallery</h1>
      <span class="status ok"><span class="dot"></span>Ready</span>
    </div>

    <div class="page">
      <div class="section-title">Buttons</div>
      <div class="row-inline">
        <button class="btn primary">Save &amp; Verify</button>
        <button class="btn">Secondary</button>
        <button class="btn ghost">Ghost</button>
        <button class="btn danger">Clear</button>
        <button class="btn icon">&#128269;</button>
        <button class="btn primary" disabled>Disabled</button>
        <button class="btn primary"><span class="spinner"></span> Working</button>
      </div>
    </div>

    <div class="page">
      <div class="section-title">Inputs</div>
      <div class="field">
        <label>Hotkey</label>
        <input type="text" value="ctrl+space" placeholder="ctrl+space">
        <div class="hint">Global press-to-talk shortcut.</div>
      </div>
      <div class="field">
        <label>OpenAI API key</label>
        <div class="row-inline">
          <input type="password" class="mono" value="sk-abcdef" style="flex:1">
          <button class="btn ghost sm">Show</button>
        </div>
        <div class="hint">Current: <span class="masked">sk-abc&#8230;7xQ</span></div>
      </div>
      <div class="field">
        <label>Summary model</label>
        <select><option>ChatGPT 5.6-SOL (cloud)</option><option>Gemini 3.7 Flash (free)</option></select>
      </div>
      <div class="field">
        <label>Manual vocabulary</label>
        <textarea class="rtl-auto" dir="auto">git, push, React, Kubernetes, GuardDuty</textarea>
      </div>
    </div>

    <div class="page">
      <div class="section-title">Choices</div>
      <label class="check"><input type="checkbox" checked><span class="box"></span>
        <span class="txt">Restore clipboard after paste<small>Put your previous clipboard back ~2s after a paste.</small></span></label>
      <label class="check"><input type="checkbox"><span class="box"></span>
        <span class="txt">Press Enter after paste</span></label>
      <div class="divider"></div>
      <label class="radio"><input type="radio" name="rm" checked><span class="box"></span><span class="txt">Hold to record</span></label>
      <label class="radio"><input type="radio" name="rm"><span class="box"></span><span class="txt">Toggle (press start/stop)</span></label>
      <div class="divider"></div>
      <label class="switch"><input type="checkbox" checked><span class="track"></span><span>Invisible mode</span></label>
      &nbsp;&nbsp;
      <label class="switch"><input type="checkbox"><span class="track"></span><span>Start with Windows</span></label>
    </div>

    <div class="page flush">
      <div class="list">
        <div class="row click sel"><span class="grow">System Default</span><span class="chip">active</span></div>
        <div class="row click"><span class="grow">USB Microphone</span></div>
        <div class="row click"><span class="grow">Logitech C920</span><span class="muted">webcam</span></div>
      </div>
    </div>

    <div class="page">
      <div class="section-title">Content direction (dir=auto)</div>
      <div class="list">
        <div class="row"><span class="grow rtl-auto" dir="auto">""" + uk._h(rtl_sample) + """</span></div>
        <div class="row"><span class="grow rtl-auto" dir="auto">Meeting with the DevOps team about EKS and CloudTrail.</span></div>
      </div>
    </div>

    <div class="page">
      <div class="section-title">Status &amp; misc</div>
      <div class="row-inline" style="gap:18px">
        <span class="status ok"><span class="dot"></span>Ready</span>
        <span class="status busy"><span class="dot"></span>Transcribing</span>
        <span class="status warn"><span class="dot"></span>Model missing</span>
        <span class="status err"><span class="dot"></span>Error</span>
        <span class="chip">&#8984; free</span>
        <span class="badge">3</span>
        <span class="badge soft">12</span>
        <span class="kbd">Ctrl+Alt+M</span>
      </div>
      <div class="divider"></div>
      <div class="row-inline">
        <button class="btn" onclick="RK.toast('Saved','ok')">Toast OK</button>
        <button class="btn" onclick="RK.toast('Model missing','warn')">Toast warn</button>
        <button class="btn" onclick="RK.toast('Something failed','err')">Toast err</button>
      </div>
    </div>

    <div class="page">
      <div class="section-title">Empty state</div>
      <div class="empty">
        <div class="big">&#128220;</div>
        <div>No meetings indexed yet</div>
        <div class="sub">Record a meeting and it will appear here for search.</div>
      </div>
    </div>
  </main>
</div>
""").replace("__LOGO_SRC__", uk.logo_data_uri())


def build_html(dark=False):
    body = _body()
    html = uk.page("Lia UI Gallery", body)
    if dark:
        html = html.replace('<html lang="en" dir="ltr">',
                            '<html lang="en" dir="ltr" data-theme="dark">')
    # sidebar nav demo: clicking swaps the .on class
    demo_js = """
document.querySelectorAll('.nav-item').forEach(function(b){
  b.addEventListener('click', function(){
    document.querySelectorAll('.nav-item').forEach(function(x){x.classList.remove('on');});
    b.classList.add('on');
  });
});
"""
    return html.replace("</script>\n</body>", demo_js + "\n</script>\n</body>")


def main():
    dark = "--dark" in sys.argv
    if "--html" in sys.argv:
        i = sys.argv.index("--html")
        out = sys.argv[i + 1]
        with open(out, "w", encoding="utf-8") as f:
            f.write(build_html(dark))
        sys.stdout.write("wrote %s\n" % out)
        return
    try:
        import webview
    except Exception as e:
        sys.stderr.write("pywebview unavailable: %r\n" % (e,))
        sys.exit(2)
    webview.create_window("Lia UI Gallery", html=build_html(dark),
                          width=1040, height=820, min_size=(760, 560))
    webview.start(gui="edgechromium")


if __name__ == "__main__":
    main()
