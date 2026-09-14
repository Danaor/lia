"""Mechanically-checkable meeting-summary rules - the prompt rules a machine can
verify with NO model and NO network. Ported/adapted from WhisperMeet's
pipeline/summary_check.py for Lia's summary contract (he + en).

WHY: summary quality lives in a prompt text file; editing it silently changes
every future summary, and regressions have only ever been caught by reading a
real meeting's output afterwards. Several rules ("no bold", "the notetaker is
never named", "these headers in this order", "tasks are '- [ ]'") are not
judgement calls - this module decides them, so a prompt edit (or a model
regression) can be checked in milliseconds instead of found in production.

It CANNOT tell you a summary is faithful - only that it does not break a rule
the prompt states outright. Invented owners / dropped decisions need a model.

    python -m summary_check --file x.md|x.html|x.txt   # one summary
    python -m summary_check --meetings                 # every %APPDATA%\\Lia meeting summary
    python -m summary_check --json                     # machine-readable

Lia adaptation vs WhisperMeet:
 - Six delivery headers, he OR en (parsing is language-union; there is no
   code-spliced "## משתתפים" here). Optional = status / done-in-meeting.
 - R2 is STRICTER (Naor's rule 2026-09-14): the notetaker name must appear
   NOWHERE in a summary, not only after "אחראי:".
 - Dropped R10 (Teams "(Unverified)") and R13 (mailbox aliases): those inputs
   are WhisperMeet-only.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

# --- the delivery contract, per language (verbatim from the summary prompts'
# OUTPUT FORMAT section: _SUMMARY_PROMPT_MEETING / SUMMARY_PROMPT_MEETING_EN). ---
HEADERS_HE = ("## כותרת הדיון", "## תקציר", "## דגשים מרכזיים",
              "## סטטוס פרויקטים", "## בוצע בפגישה", "## משימות")
HEADERS_EN = ("## Discussion Title", "## Summary", "## Key Highlights",
              "## Project Status", "## Done in This Meeting", "## Tasks")
OPTIONAL_HE = ("## סטטוס פרויקטים", "## בוצע בפגישה")
OPTIONAL_EN = ("## Project Status", "## Done in This Meeting")
HIGHLIGHTS_HE, HIGHLIGHTS_EN = "## דגשים מרכזיים", "## Key Highlights"
TASKS_HE, TASKS_EN = "## משימות", "## Tasks"
DONE_HE, DONE_EN = "## בוצע בפגישה", "## Done in This Meeting"
TASK_FIELDS_HE = ("אחראי", "יעד", "סטטוס")
TASK_FIELDS_EN = ("Owner", "Due", "Status")
OWNER_WORD_HE, OWNER_WORD_EN = "אחראי", "Owner"
PLACEHOLDERS = ("לא צוין", "not specified")
DONE_MARK_HE = "בוצע במהלך הפגישה"
DONE_MARK_EN = "done during the meeting"

# Default notetaker identity (config `notetaker_names`); callers pass their own.
DEFAULT_BOT_NAMES = ("Lia", "ליה", "לייה")

_BOLD_RE = re.compile(r"\*\*[^*\n]+\*\*")
# 'Speaker B' / 'דובר 2' / 'S1-spk:0' - a diarization label is never a name (R14; the
# depth pass shipped "Speaker B יבצע..." into prose on its first live run, 2026-09-14).
_SPEAKER_LABEL_RE = re.compile(r"(?i)\bSpeaker\s+[A-Z](?:\d+)?(?:-spk:?\d*)?\b|(?<![א-ת])דובר\s+\d+")
_HEADER_RE = re.compile(r"^##\s+\S")
_BULLET_RE = re.compile(r"^\s*-\s+(?!\[)")           # a bullet that is not a checklist item
_TASK_RE = re.compile(r"^\s*-\s+\[[ xX]\]\s*\S")
_DONE_TASK_RE = re.compile(r"^\s*-\s+\[[xX]\]\s*")
_LABEL_PREFIX_RE = re.compile(r"^\s*-\s+(?:\[[ xX]\]\s*)?([^\s:]{2,14}):\s")
_OWNER_FIELD_RE = re.compile(r"\s*[-–]\s*(?:אחראי|יעד|סטטוס|Owner|Due|Status)\s*:.*$")
# an internal KIND label leaked as a trailing field
_KIND_SUFFIX_RE = re.compile(
    r"\s[-–]\s*\(?(?:DO|DECIDE|CONFIRM|VERIFY|BLOCKER|DEPENDENCY)"
    r"(?:\s*/\s*(?:DO|DECIDE|CONFIRM|VERIFY|BLOCKER|DEPENDENCY))*\)?\s*[.:]?\s*$",
    re.IGNORECASE)
# a status line that is a bare verdict (a decision/blocker dressed as a project)
_STATUS_BARE_RE = re.compile(
    r"^\s*-\s+[^:\n]{2,60}:\s*(?:הוחלט|תקוע|נדחה|סגור|בוצע|פתוח|"
    r"decided|blocked|rejected|closed|done|open)\.?\s*$", re.IGNORECASE)


class Finding:
    __slots__ = ("rule", "severity", "line", "detail")

    def __init__(self, rule, severity, line, detail):
        self.rule, self.severity, self.line, self.detail = rule, severity, line, detail

    def as_dict(self):
        return {"rule": self.rule, "severity": self.severity,
                "line": self.line, "detail": self.detail}

    def __repr__(self):
        return "<%s %s:%d %s>" % (self.severity, self.rule, self.line, self.detail)


def detect_lang(md):
    """'he', 'en', or 'unknown' from which header set the summary uses."""
    headers = [ln.strip() for ln in (md or "").splitlines()
               if _HEADER_RE.match(ln.strip())]
    if not headers:
        return "unknown"
    he = sum(1 for h in headers if h in HEADERS_HE)
    en = sum(1 for h in headers if h in HEADERS_EN)
    if he == 0 and en == 0:
        return "unknown"
    return "he" if he >= en else "en"


def _owner_twice_re(owner_word):
    return re.compile(re.escape(owner_word) + r"\s*:.*" + re.escape(owner_word) + r"\s*:")


def check_summary(md, *, bot_names=DEFAULT_BOT_NAMES):
    """Every mechanically-decidable rule violation in one summary, in document
    order. `bot_names` = the notetaker's name + ASR variants (config
    notetaker_names) - flagged anywhere (R2, Naor's strict rule)."""
    out = []
    if not (md or "").strip():
        return [Finding("empty", "error", 0, "the summary is empty")]
    lines = md.splitlines()
    lang = detect_lang(md)
    if lang == "en":
        expected, optional, highlights_h, tasks_h, done_h, fields, owner_w = (
            HEADERS_EN, OPTIONAL_EN, HIGHLIGHTS_EN, TASKS_EN, DONE_EN,
            TASK_FIELDS_EN, OWNER_WORD_EN)
    else:
        expected, optional, highlights_h, tasks_h, done_h, fields, owner_w = (
            HEADERS_HE, OPTIONAL_HE, HIGHLIGHTS_HE, TASKS_HE, DONE_HE,
            TASK_FIELDS_HE, OWNER_WORD_HE)
    owner_twice_re = _owner_twice_re(owner_w)

    # R1 no bold (renders as literal asterisks in an email/doc)
    for i, ln in enumerate(lines, 1):
        m = _BOLD_RE.search(ln)
        if m:
            out.append(Finding("bold", "error", i, "bold markup: %s" % m.group(0)))

    # R2 the notetaker name appears NOWHERE (Naor's rule 2026-09-14) - stricter
    # than WhisperMeet's "never as אחראי". Word-ish match, case-insensitive.
    for i, ln in enumerate(lines, 1):
        for name in bot_names:
            if name and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", ln, re.I):
                out.append(Finding("bot-name", "error", i,
                                   "the notetaker name %r must not appear in a summary" % name))
                break

    # R3 the placeholder the prompts forbid outright
    for i, ln in enumerate(lines, 1):
        for ph in PLACEHOLDERS:
            if ph in ln:
                out.append(Finding("placeholder", "error", i, "%r - the prompts forbid it" % ph))

    # R14 a diarization speaker label used as a name / owner, anywhere
    for i, ln in enumerate(lines, 1):
        m = _SPEAKER_LABEL_RE.search(ln)
        if m:
            out.append(Finding("speaker-label", "error", i,
                               "speaker label %r used as a name" % m.group(0)))

    # R4 header contract
    seen = [ln.strip() for ln in lines if _HEADER_RE.match(ln.strip())]
    first_content = next((i for i, ln in enumerate(lines, 1) if ln.strip()), 0)
    if first_content and not _HEADER_RE.match(lines[first_content - 1].strip()):
        out.append(Finding("preamble", "error", first_content,
                           "text before the first header: %r" % lines[first_content - 1][:60]))
    if lang == "unknown":
        out.append(Finding("shape", "error", 0,
                           "no recognised header shape (headers: %s)" % (seen[:4] or "none")))
    else:
        unknown = [h for h in seen if h not in expected]
        if unknown:
            out.append(Finding("header-unknown", "error", 0,
                               "headers outside the %s contract: %s" % (lang, unknown)))
        if seen and seen[0] != expected[0]:
            out.append(Finding("header-first", "error", 0,
                               "starts with %s, not %s" % (seen[0], expected[0])))
        order = [expected.index(h) for h in seen if h in expected]
        if order != sorted(order):
            out.append(Finding("header-order", "error", 0, "headers out of contract order: %s" % seen))
        for h in expected:
            if h not in seen and h not in optional:
                out.append(Finding("header-missing", "warn", 0, "missing section %s" % h))

    # R5-R12 per-section
    section, highlights = "", 0
    done_bullets, done_tasks = [], []
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if _HEADER_RE.match(s):
            if section == highlights_h and not 3 <= highlights <= 6:
                out.append(Finding("highlight-count", "warn", i,
                                   "%s has %d bullets (the prompt says 3-6)" % (highlights_h, highlights)))
            section, highlights = s, 0
            continue
        if not s:
            continue
        if section == highlights_h and _BULLET_RE.match(ln):
            highlights += 1
        if section == tasks_h and s.startswith("-") and not _TASK_RE.match(ln):
            out.append(Finding("task-format", "error", i, "task is not '- [ ] …': %r" % s[:60]))
        if section == tasks_h and _TASK_RE.match(ln):
            if owner_twice_re.search(s):
                out.append(Finding("owner-doubled", "error", i, "two owner fields: %r" % s[:70]))
            if _KIND_SUFFIX_RE.search(s):
                out.append(Finding("kind-suffix", "error", i, "internal KIND label leaked: %r" % s[:70]))
            if _DONE_TASK_RE.match(ln):
                text = _OWNER_FIELD_RE.sub("", _DONE_TASK_RE.sub("", ln, count=1)).strip()
                done_tasks.append((i, text))
        if section == done_h and s.startswith("-"):
            done_bullets.append(s.lstrip("- ").strip())
        if section == expected[3] and _STATUS_BARE_RE.match(ln):   # project-status section
            out.append(Finding("status-bare", "warn", i, "bare verdict as a project status: %r" % s[:60]))
        m = _LABEL_PREFIX_RE.match(ln)
        if m and m.group(1) not in fields and section != expected[3]:
            out.append(Finding("label-prefix", "warn", i,
                               "in-bullet label prefix %r - the prompts forbid it" % m.group(1)))
    if section == highlights_h and not 3 <= highlights <= 6:
        out.append(Finding("highlight-count", "warn", len(lines),
                           "%s has %d bullets (the prompt says 3-6)" % (highlights_h, highlights)))

    # R11 a '[x]' task that repeats a done-in-meeting bullet (forbidden twice over)
    if done_tasks and done_bullets:
        try:
            import summary_dedup
            near = summary_dedup.is_near_duplicate
        except Exception:
            near = None
        for i, text in done_tasks:
            text = re.sub(r"\s*[-–]\s*(?:%s|%s)\s*$" % (re.escape(DONE_MARK_HE),
                                                        re.escape(DONE_MARK_EN)), "", text)
            hit = any((near(text, b) if near else text.strip() == b.strip())
                      for b in done_bullets)
            if hit:
                out.append(Finding("done-duplicated", "warn", i,
                                   "'[x]' task repeats a done-in-meeting bullet: %r" % text[:60]))
    return out


# --------------------------------------------------------------------- sources
def _html_to_text(html):
    """Best-effort: the summary text out of a Lia *_meeting_summary.html card."""
    # Strip the app footer chrome first (older summaries carried a
    # "Generated by Lia" footer div OUTSIDE the card) so it can't leak into the
    # extracted summary text.
    html = re.sub(r'(?is)<div class="footer">.*?</div>', "", html)
    m = re.search(r'<div class="card">(.*?)</div>\s*(?:</div>\s*)?</body>', html, re.S)
    body = m.group(1) if m else html
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", body)
    # Drop chrome that is not part of the summary the model produced: the
    # "Meeting summary" eyebrow kicker, the "## ...meta..." date line, and the
    # <h1> meeting-title (the summary's own title is its "## כותרת הדיון" section).
    body = re.sub(r'(?is)<div class="eyebrow">.*?</div>', "", body)
    body = re.sub(r'(?is)<div class="meta">.*?</div>', "", body)
    body = re.sub(r"(?is)<h1[^>]*>.*?</h1>", "", body)
    body = re.sub(r"(?i)<h2[^>]*>", "\n## ", body)     # section headers -> markdown
    body = re.sub(r"(?i)<li[^>]*class=\"[^\"]*task[^\"]*\"[^>]*>", "\n- [ ] ", body)
    body = re.sub(r"(?i)<li[^>]*>", "\n- ", body)
    body = re.sub(r"(?i)<(br|p|div|/h1|/h2|/li|/ul)[^>]*>", "\n", body)
    body = re.sub(r"(?s)<[^>]+>", "", body)
    import html as _h
    body = _h.unescape(body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def _summary_from_meeting_txt(text):
    """The summary block of a Lia meeting .txt (between the summary banner and the
    Transcript banner), or the whole thing if no banners. Strips RLM/BOM."""
    t = (text or "").replace("‏", "").replace("﻿", "")
    lines = t.splitlines()
    # first delivery header (he or en) marks the start of the summary
    start = next((i for i, l in enumerate(lines)
                  if l.strip() in HEADERS_HE or l.strip() in HEADERS_EN), -1)
    if start == -1:
        return ""
    end = next((i for i, l in enumerate(lines) if i > start
                and l.strip() in ("Transcript", "תמלול")), len(lines))
    body = [l for l in lines[start:end]
            if not all("─" <= c <= "╿" for c in l.strip() or " ")]
    return "\n".join(body).strip()


def load_summary(path):
    """Read a summary from a .md/.txt/.html file into markdown for checking."""
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    low = path.lower()
    if low.endswith(".html") or low.endswith(".htm"):
        return _html_to_text(raw)
    if low.endswith("_meeting.txt") or "_meeting" in os.path.basename(low):
        block = _summary_from_meeting_txt(raw)
        if block:
            return block
    return raw.replace("‏", "").replace("﻿", "").strip()


def _meetings_dir():
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Lia", "meetings")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check Lia meeting summaries against the prompt's mechanical rules.")
    ap.add_argument("--file", help="one .md/.txt/.html summary")
    ap.add_argument("--meetings", action="store_true",
                    help="every *_meeting_summary.html in %%APPDATA%%\\Lia\\meetings")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    targets = []
    if args.file:
        targets = [args.file]
    elif args.meetings:
        d = _meetings_dir()
        targets = sorted(glob.glob(os.path.join(d, "*_meeting_summary.html")))
    else:
        ap.error("pass --file or --meetings")

    results, total = [], 0
    for path in targets:
        try:
            md = load_summary(path)
            findings = check_summary(md)
        except Exception as e:
            findings = [Finding("read-error", "error", 0, str(e))]
        total += sum(1 for f in findings if f.severity == "error")
        results.append((path, findings))

    if args.json:
        print(json.dumps([{"file": p, "findings": [f.as_dict() for f in fs]}
                          for p, fs in results], ensure_ascii=False, indent=2))
    else:
        for p, fs in results:
            if fs:
                print("%s: %d finding(s)" % (os.path.basename(p), len(fs)))
                for f in fs:
                    print("  [%s] %s (line %d): %s" % (f.severity, f.rule, f.line, f.detail))
            else:
                print("%s: OK" % os.path.basename(p))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
