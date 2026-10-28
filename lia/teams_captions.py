"""Speaker names from Microsoft Teams live captions (2026-10-27).

Teams writes the speaker's name next to every live-caption line, in the
user's own Teams window. Lia reads that window through Windows UI Automation
(the accessibility interface; no Teams API, no bot, nothing joins the call,
nothing leaves this PC) and keeps WHO spoke WHEN. Lia's own transcription is
untouched: the names are matched to its chunks by time.

Captions must be on in the Teams meeting. Teams can switch them on for every
meeting: Settings > Accessibility (or Captions and transcripts) > Always show
captions.

Layers:
  * pure: parse a UIA tree (nested dicts) into caption entries, track entries
    into speaker turns, label a time window. Tested without Teams.
  * TeamsCaptionWatcher: a daemon thread polling the Teams windows (comtypes).

A wrong name is worse than no name: a window is labelled only when one
speaker clearly dominates it (or two clearly share it).
"""
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("Lia")

# Window classes that host Teams (the new Teams client); tests pass their own.
TEAMS_WINDOW_CLASSES = ("TeamsWebView",)
# The captions region, found by its accessible name / id / class.
CAPTION_ROOT_RE = re.compile(r"caption|subtitle|כתוביות", re.I)
# Captions show up 1-1.5 s after the words are spoken (measured by a Teams
# caption reader in a live meeting).
DEFAULT_LAG_S = 1.2
# Words per second of normal speech: the length of a caption seen only once.
_WORDS_PER_S = 2.5


# ---------------------------------------------------------------- pure parts
# Caption-author cleanup: shapes Teams really renders a caption author in.
_BIDI_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_TEAMS_ID_RE = re.compile(r"^teams-(.+)$")          # 'teams-Dana_Levi' (Teams web, 2026-08)
_UNVERIFIED_RE = re.compile(r"\s*\((?:unverified|לא מאומת)\)\s*$", re.I)   # guests
_ALL_PAREN_RE = re.compile(r"\(\s*([^()]+?)\s*\)")
_REPEAT_PAREN_RE = re.compile(r"([^()]+?)\s*\(\s*([^()]+?)\s*\)")
_LAST_FIRST_RE = re.compile(r"([^,()]+),\s*([^,()]+)")


def clean_name(name):
    """A caption author as a display name: invisible direction marks gone, an
    exact self-concatenation halved ('Dana LeviDana Levi'), 'teams-First_Last'
    unwrapped, the guest '(Unverified)' tag dropped, a parenthesis that is the
    whole name or only repeats it removed, 'Last, First' flipped."""
    n = _BIDI_RE.sub("", str(name or "")).strip()
    h = len(n) // 2
    if len(n) >= 16 and len(n) % 2 == 0 and n[:h] == n[h:]:
        n = n[:h].strip()
    m = _TEAMS_ID_RE.match(n)
    if m:
        n = m.group(1).replace("_", " ").strip()
    n = _UNVERIFIED_RE.sub("", n).strip()
    m = _ALL_PAREN_RE.fullmatch(n)
    if m:
        n = m.group(1).strip()
    m = _REPEAT_PAREN_RE.fullmatch(n)
    if m and m.group(1).strip().casefold() == m.group(2).strip().casefold():
        n = m.group(1).strip()
    m = _LAST_FIRST_RE.fullmatch(n)
    if m:
        n = "%s %s" % (m.group(2).strip(), m.group(1).strip())
    return n


# UIA control types that are never caption text: Button, MenuBar, MenuItem,
# TitleBar, ToolBar (the window's own chrome lives in the same tree).
_NOT_TEXT_TYPES = {50000, 50010, 50011, 50037, 50021}
_UIA_DOCUMENT = 50030


def _leaves(node):
    """Non-empty names of the leaf nodes under `node`, in document order,
    consecutive duplicates dropped (an avatar image + a text both carrying the
    author's name). Buttons / menus / title bars are skipped."""
    out = []

    def walk(n):
        if n.get("ct") in _NOT_TEXT_TYPES:
            return
        kids = n.get("children") or []
        if not kids:
            name = (n.get("name") or "").strip()
            if name and (not out or out[-1] != name):
                out.append(name)
            return
        for k in kids:
            walk(k)
    walk(node)
    return out


def _name_like(s):
    """A person's display name: short, a few words, not a sentence."""
    s = (s or "").strip()
    if not s or len(s) > 60:
        return False
    if len(s.split()) > 6:
        return False
    return not re.search(r"[.!?…:]$", s)


def _split_flat(lv):
    """A flattened region: author, text, author, text... -> [(author, text)],
    or None when the leaves do not alternate like that."""
    if len(lv) < 4 or len(lv) % 2:
        return None
    pairs = [(lv[i], lv[i + 1]) for i in range(0, len(lv), 2)]
    if not all(_name_like(a) for a, _ in pairs):
        return None
    return pairs


def _entries_of(node):
    """The caption lines `node` holds when it is the smallest node holding
    them: [(author, text)] - one line, or several from a flattened region."""
    lv = _leaves(node)
    if len(lv) < 2 or not _name_like(lv[0]):
        return []
    if len(lv) > 2:
        pairs = _split_flat(lv)
        if pairs:
            return pairs
    text = " ".join(lv[1:]).strip()
    return [(lv[0], text)] if text else []


def _documents(tree):
    """The web-content roots (UIA Document) of a window tree; the whole tree
    when it has none (plain dict trees in tests)."""
    docs, stack = [], [tree]
    while stack:
        n = stack.pop()
        if n.get("ct") == _UIA_DOCUMENT:
            docs.append(n)
            continue
        stack.extend(n.get("children") or [])
    return docs or [tree]


def find_caption_root(tree, pattern=CAPTION_ROOT_RE):
    """The captions region inside a window tree: the first node (document
    order) inside the web content whose name / automation id / class matches
    `pattern` AND holds at least one caption line. None when captions are off
    or not found. (The window frame has a "BrowserCaptionButtonContainer" -
    a title-bar caption, hence web content only.)"""
    for doc in _documents(tree):
        stack = [doc]
        while stack:
            n = stack.pop(0)
            label = " ".join(str(n.get(k) or "") for k in ("name", "aid", "cls"))
            if pattern.search(label) and parse_caption_entries(n):
                return n
            stack[0:0] = list(n.get("children") or [])
    return None


def parse_caption_entries(root):
    """Caption lines under `root`: [(key, author, text)] in document order.
    A line is the smallest node whose leaves read <name> <text...>; its key is
    the node's UIA runtime id (stable while the line is on screen). Lines from
    a flattened region, or a node without a runtime id, get key None and are
    matched by their text instead (TurnTracker)."""
    found = []

    def walk(n):
        kids = n.get("children") or []
        child_hits = [walk(k) for k in kids]
        if any(child_hits):
            return True
        pairs = _entries_of(n)
        if not pairs:
            return False
        rid = n.get("rid")
        key = tuple(rid) if (rid and len(pairs) == 1) else None
        for author, text in pairs:
            author = clean_name(author)
            if author:
                found.append((key, author, text))
        return True
    walk(root)
    return found


class TurnTracker:
    """Caption lines seen over time -> speaker turns [{speaker, t0, t1, text}].
    A line keeps its key while it grows (Teams rewrites the line in place as
    the words come in); t0 = when it first appeared, t1 = when its text last
    changed, both minus the caption lag."""

    def __init__(self, lag=DEFAULT_LAG_S):
        self.lag = float(lag)
        self._by_key = {}
        self._order = []
        self._lock = threading.Lock()

    GROW_S = 15.0     # a line still growing: last change within this

    def update(self, entries, now):
        """One poll's caption lines. A keyed line (runtime id) is the same line
        while its key lives; a keyless one (flattened region) is matched by
        its text: an exact match is a line already on screen, a match on the
        start of the text is the line still growing.

        Teams sometimes rebuilds the caption pane (measured 2026-10-28 when a
        screen share started): every line comes back under a NEW key, and old
        lines read as new speech (3 old lines put a speaker over someone
        else's turn). So a keyed line also matches an old one by exact text
        when it is long enough to be unique (3+ words), and a new line that
        sits ABOVE a line already known is an old one: new speech is always
        added at the bottom."""
        with self._lock:
            used = set()          # a line on screen matches ONE record per poll
            pending = []          # new lines of this poll, kept unless a known one follows
            for key, author, text in entries:
                rec = self._by_key.get(key) if key is not None else None
                if rec is None or rec["speaker"] != author:
                    rec = self._match(
                        author, text, now,
                        exact_any_age=(key is None or len(text.split()) >= 3),
                        used=used)
                if rec is None:
                    rec = {"speaker": author, "first": now, "last": now,
                           "text": text, "seen": 1,
                           "first_words": len(text.split())}
                    pending.append((key, rec))
                    used.add(id(rec))
                    continue
                pending = []      # a known line below them: they were old lines
                used.add(id(rec))
                self._grow(rec, text, now)
                if key is not None:
                    self._by_key[key] = rec
            for key, rec in pending:
                self._order.append(rec)
                if key is not None:
                    self._by_key[key] = rec

    @staticmethod
    def _grow(rec, text, now):
        if rec["text"] != text:
            rec["text"] = text
            rec["last"] = now
            rec["seen"] += 1

    @staticmethod
    def _same_line(old, new):
        """`new` is a later rendering of line `old`: it extends it, or (when
        long enough to tell) it is a cut-back of it, or they open with the
        same three words (Teams revises the last words of a partial caption).
        A short new line that merely opens like an older one is NOT the same
        line ("so" vs "so as I said ...")."""
        if not old or not new:
            return False
        if new.startswith(old):
            return True
        if old.startswith(new) and (len(new) >= 12 or len(new.split()) >= 3):
            return True
        wa, wb = old.split(), new.split()
        return len(wa) >= 3 and len(wb) >= 3 and wa[:3] == wb[:3]

    def _match(self, author, text, now, exact_any_age=False, used=()):
        if exact_any_age:
            for rec in reversed(self._order[-80:]):
                if (id(rec) not in used and rec["speaker"] == author
                        and rec["text"] == text):
                    return rec
        for rec in reversed(self._order[-8:]):
            if (id(rec) not in used and rec["speaker"] == author
                    and now - rec["last"] <= self.GROW_S
                    and self._same_line(rec["text"], text)):
                return rec
        return None

    def turns(self):
        with self._lock:
            out = []
            for r in self._order:
                t1 = r["last"] - self.lag
                t0 = r["first"] - self.lag
                if r["last"] == r["first"]:
                    # Seen once, already complete: it was spoken BEFORE now.
                    span = max(1.0, len(r["text"].split()) / _WORDS_PER_S)
                    t0 = t1 - span
                else:
                    # The words the line already showed when it first
                    # appeared were spoken before that.
                    t0 -= min(3.0, r.get("first_words", 1) / _WORDS_PER_S)
                out.append({"speaker": r["speaker"], "t0": max(0.0, t0),
                            "t1": max(0.0, t1), "text": r["text"]})
            return out


def split_audio_by_turns(audio, w0, turns, sr=16000, guard_s=2.5, min_seg_s=0.8,
                         search_s=0.6, frame_s=0.03):
    """Cut a drained meeting window at the speaker changes the captions show,
    so each piece is ONE speaker's turn (transcribed alone, named alone).

    audio: int16/float samples covering meeting seconds [w0, w0 + len/sr].
    Each change of speaker between consecutive turns gives a cut at the
    quietest moment between the old speaker's last caption and the new
    speaker's first one (+-search_s). Only the SETTLED part is cut: captions
    trail the audio, so the last `guard_s` seconds (a change there may not be
    captioned yet) are returned as carry for the next window; guard_s=0 cuts
    everything (the final window). Pieces shorter than min_seg_s join their
    neighbour.

    Returns (pieces, carry): pieces = [(start_rel, samples)], carry = the
    samples to prepend to the next window (or None). Returns None when no
    caption turn touches this window - the caller keeps its usual cut."""
    import numpy as np
    n = len(audio) if audio is not None else 0
    if n == 0 or not turns:
        return None
    w1 = w0 + n / float(sr)
    near = sorted((t for t in turns if t["t1"] > w0 - 10 and t["t0"] < w1),
                  key=lambda t: t["t0"])
    if not any(min(w1, t["t1"]) > max(w0, t["t0"]) for t in near):
        return None
    fr = max(1, int(frame_s * sr))
    min_n = int(min_seg_s * sr)

    def idx(t):
        return max(0, min(n, int(round((t - w0) * sr))))

    def quietest(a, b):
        ia, ib = idx(a), idx(b)
        if ib - ia < 2 * fr:
            return (ia + ib) // 2
        seg = np.asarray(audio[ia:ib], dtype=np.float32)
        k = (ib - ia) // fr
        rms = np.sqrt(np.mean(seg[:k * fr].reshape(k, fr) ** 2, axis=1) + 1e-12)
        return ia + int(np.argmin(rms)) * fr + fr // 2

    # The end of the settled part: a quiet moment near (end - guard).
    if guard_s > 0:
        settle = w1 - guard_s
        if settle - w0 < min_seg_s:
            return [], audio            # nothing settled yet: all of it waits
        end = quietest(settle - 1.0, min(w1, settle + 0.5))
    else:
        settle, end = w1, n
    if end < min_n:
        return [], audio
    cuts = []
    for prev, cur in zip(near, near[1:]):
        if cur["speaker"] == prev["speaker"]:
            continue
        lo, hi = sorted((prev["t1"], cur["t0"]))
        lo, hi = lo - search_s, hi + search_s
        if hi <= w0 or lo >= settle:
            continue
        c = quietest(max(lo, w0), min(hi, settle))
        if min_n <= c <= end - min_n and (not cuts or c - cuts[-1] >= min_n):
            cuts.append(c)
    bounds = [0] + cuts + [end]
    pieces = [(w0 + a / float(sr), audio[a:b]) for a, b in zip(bounds, bounds[1:]) if b > a]
    carry = audio[end:] if end < n else None
    return pieces, carry


def label_window(turns, a, b, dominant=0.7, shared=0.25):
    """The speaker label for the time window [a, b] (meeting seconds):
    the name when one speaker holds >= `dominant` of the captioned time,
    "A / B" (in speaking order) when two each hold >= `shared`, else None."""
    talk, first = {}, {}
    for t in turns:
        ov = min(b, t["t1"]) - max(a, t["t0"])
        if ov <= 0:
            continue
        s = t["speaker"]
        talk[s] = talk.get(s, 0.0) + ov
        first.setdefault(s, max(a, t["t0"]))
    total = sum(talk.values())
    if total < 0.5:
        return None
    ranked = sorted(talk, key=lambda s: -talk[s])
    if talk[ranked[0]] / total >= dominant:
        return ranked[0]
    pair = [s for s in ranked[:2] if talk[s] / total >= shared]
    if len(pair) == 2:
        return " / ".join(sorted(pair, key=lambda s: first[s]))
    return None


# ------------------------------------------------------------ the UIA reader
class _Uia:
    """comtypes UI Automation, one per thread (COM apartment)."""

    def __init__(self):
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U
        self.U = U
        self.uia = comtypes.client.CreateObject(U.CUIAutomation,
                                                interface=U.IUIAutomation)
        cr = self.uia.CreateCacheRequest()
        for pid in (U.UIA_NamePropertyId, U.UIA_ControlTypePropertyId,
                    U.UIA_AutomationIdPropertyId, U.UIA_ClassNamePropertyId,
                    U.UIA_RuntimeIdPropertyId):
            cr.AddProperty(pid)
        cr.TreeScope = U.TreeScope_Subtree
        # RAW view: the Control view drops role-less <div>s, which flattens
        # every caption line of the region into one author/text sequence
        # (measured on a WebView2 page with the Teams caption markup).
        cr.TreeFilter = self.uia.RawViewCondition
        self.cache = cr

    def windows(self, classes):
        U = self.U
        out = []
        root = self.uia.GetRootElement()
        for cls in classes:
            cond = self.uia.CreatePropertyCondition(U.UIA_ClassNamePropertyId, cls)
            arr = root.FindAll(U.TreeScope_Children, cond)
            for i in range(arr.Length if arr else 0):
                out.append(arr.GetElement(i))
        return out

    def snapshot(self, element, max_nodes=6000):
        """One cross-process call: the element's subtree as nested dicts."""
        U = self.U
        el = element.BuildUpdatedCache(self.cache)
        count = [0]

        def conv(e, depth):
            count[0] += 1
            try:
                rid = e.GetCachedPropertyValue(U.UIA_RuntimeIdPropertyId)
                rid = list(rid) if rid else None
            except Exception:
                rid = None
            node = {"name": e.CachedName or "", "ct": e.CachedControlType,
                    "aid": e.CachedAutomationId or "", "cls": e.CachedClassName or "",
                    "rid": rid, "children": [], "_el": e}
            if depth > 60 or count[0] >= max_nodes:
                return node
            try:
                kids = e.GetCachedChildren()
            except Exception:
                kids = None
            for i in range(kids.Length if kids else 0):
                if count[0] >= max_nodes:
                    break
                node["children"].append(conv(kids.GetElement(i), depth + 1))
            return node
        return conv(el, 0)


def _flatten(tree, limit=4000):
    """A compact, line-per-node dump for the diagnostics file."""
    rows = []

    def walk(n, d):
        if len(rows) >= limit:
            return
        rows.append([d, n.get("ct"), (n.get("name") or "")[:160],
                     n.get("aid") or "", n.get("cls") or ""])
        for k in n.get("children") or []:
            walk(k, d + 1)
    walk(tree, 0)
    return rows


class TeamsCaptionWatcher:
    """Polls the Teams windows while a meeting records and keeps the speaker
    turns read from the live captions. Never raises into the caller; when
    UI Automation is missing or Teams is not running it just finds nothing.

    t0: the meeting's start (time.time()); all turn times are meeting seconds.
    diag_dir: when set, a JSONL diagnostics file of what was seen (window
    structure until captions are found, then the captions region) - the
    caption reader is new and this is how its first real meetings are checked.
    """
    SEARCH_EVERY_S = 2.0        # full-window search while captions are not found (~55 ms)
    SEARCH_SLOW_S = 15.0        # ... after the first two minutes without them
    RESEARCH_EVERY_S = 30.0     # re-check the region is still the right one

    def __init__(self, t0, poll_s=0.5, lag=DEFAULT_LAG_S, diag_dir=None,
                 window_classes=TEAMS_WINDOW_CLASSES, stamp=None):
        self.t0 = float(t0)
        self.poll_s = float(poll_s)
        self.tracker = TurnTracker(lag=lag)
        self.window_classes = tuple(window_classes)
        self._stop = threading.Event()
        self._thread = None
        self.state = {"teams_windows": 0, "captions_found": False,
                      "lines": 0, "error": ""}
        self._diag = None
        self._diag_lines = 0
        if diag_dir:
            try:
                os.makedirs(diag_dir, exist_ok=True)
                self._prune(diag_dir)
                stamp = stamp or time.strftime("%Y-%m-%d_%H-%M-%S")
                self._diag = os.path.join(diag_dir, "teams_captions_%s.jsonl" % stamp)
            except Exception as e:
                log.debug("Teams captions: diagnostics dir failed: %s", e)

    # -- public
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="teams-captions")
        self._thread.start()
        return self

    def stop(self, grace=1.5):
        """Keep reading `grace` seconds more (the last words' captions land
        after the audio), then stop. Non-blocking."""
        def later():
            time.sleep(max(0.0, grace))
            self._stop.set()
        threading.Thread(target=later, daemon=True).start()

    def turns(self):
        return self.tracker.turns()

    def label(self, a, b):
        try:
            return label_window(self.turns(), a, b)
        except Exception:
            return None

    def speakers(self):
        seen = []
        for t in self.turns():
            if t["speaker"] not in seen:
                seen.append(t["speaker"])
        return seen

    # -- worker
    def _now(self):
        return time.time() - self.t0

    def _write_diag(self, kind, **data):
        if not self._diag or self._diag_lines > 400:
            return
        try:
            rec = {"t": round(self._now(), 2), "kind": kind}
            rec.update(data)
            with open(self._diag, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._diag_lines += 1
        except Exception:
            pass

    @staticmethod
    def _prune(diag_dir, keep_days=7):
        now = time.time()
        for fn in os.listdir(diag_dir):
            if fn.startswith("teams_captions_"):
                p = os.path.join(diag_dir, fn)
                try:
                    if now - os.path.getmtime(p) > keep_days * 86400:
                        os.remove(p)
                except OSError:
                    pass

    def _run(self):
        try:
            uia = _Uia()
        except Exception as e:
            self.state["error"] = "UI Automation unavailable: %s" % e
            log.info("Teams captions: %s", self.state["error"])
            return
        log.info("Teams captions: watching for live captions")
        region = None            # the captions region's UIA element
        last_search = -1e9
        dumps = 0
        last_dump = -1e9
        region_dumps = 0
        last_region_dump = -1e9
        while not self._stop.is_set():
            now = self._now()
            try:
                every = (self.RESEARCH_EVERY_S if region is not None
                         else self.SEARCH_EVERY_S if now < 120 else self.SEARCH_SLOW_S)
                if now - last_search >= every:
                    last_search = now
                    # Window dumps while captions are NOT found: the first
                    # few, then one a minute - the structure Teams really has
                    # is what a parser fix needs.
                    dump = dumps < 3 or (dumps < 15 and now - last_dump >= 60)
                    found = self._search(uia, dump)
                    if found is None and dump:
                        dumps += 1
                        last_dump = now
                    region = found
                if region is not None:
                    # Only the captions region: a small subtree, one call.
                    root = uia.snapshot(region, max_nodes=1500)
                    entries = parse_caption_entries(root)
                    self.tracker.update(entries, now)
                    self.state["lines"] = len(self.tracker._order)
                    if region_dumps < 12 and now - last_region_dump >= 60:
                        region_dumps += 1
                        last_region_dump = now
                        self._write_diag("captions", rows=_flatten(root, 600),
                                         entries=len(entries))
            except Exception as e:
                # The region went away (Teams re-rendered it, the call ended):
                # search again on the next round.
                if str(e) != self.state["error"]:
                    self.state["error"] = str(e)
                    log.debug("Teams captions poll failed: %s", e)
                region = None
                last_search = -1e9
            self._stop.wait(self.poll_s)
        n = len(self.speakers())
        log.info("Teams captions: stopped (%d caption lines, %d speakers)",
                 self.state["lines"], n)
        self._write_diag("summary", lines=self.state["lines"], speakers=n,
                         turns=self.turns())

    def _search(self, uia, dump):
        wins = uia.windows(self.window_classes)
        self.state["teams_windows"] = len(wins)
        for w in wins:
            tree = uia.snapshot(w)
            root = find_caption_root(tree)
            if root is not None:
                if not self.state["captions_found"]:
                    log.info("Teams captions: found the live captions")
                    self._write_diag("found", root_name=root.get("name", "")[:80],
                                     root_aid=root.get("aid", ""),
                                     root_cls=root.get("cls", ""))
                self.state["captions_found"] = True
                return root["_el"]
            if dump:
                self._write_diag("window", rows=_flatten(tree))
        if self.state["captions_found"]:
            log.info("Teams captions: the live captions are gone")
        self.state["captions_found"] = False
        return None
