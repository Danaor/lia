"""vocab_learn.py — self-learning vocabulary core (PORTABLE module).

Shared brain for Lia's "מילון שלומד לבד" feature, deliberately written to be
lifted as-is into a private upstream project later: NO imports from the host app, stdlib
only. The host owns WHERE the store lives and WHICH texts to mine; this module
owns the store format, the mining heuristics, usage ranking, and prompt
composition.

Concepts
--------
Store    : vocabulary.json — an UNLIMITED term store with metadata (corpus /
           usage counts, sources, starred, pending/approved/rejected status).
           Atomic writes (tmp + os.replace), thread-safe.
Miner    : mine_texts() — pure heuristics extracting candidate terms from
           Hebrew-heavy transcripts: Latin-script terms embedded in Hebrew
           (the classic Whisper failure class — "git push" → "בגד פושע"),
           acronyms, and adjacent-word phrases ("API Gateway").
Composer : compose_prompt() — a ranked subset within a char budget. Whisper's
           prompt window is ~224 tokens and keeps the TAIL, so the MOST
           important terms are placed LAST. Manual/starred terms are never
           dropped by ranking.
Usage    : observe_text() — bumps usage counts for known terms that actually
           appear in produced transcripts (buffered; auto-flushes).

Host integration (Lia):
    store = VocabStore(os.path.join(CONFIG_DIR, "vocabulary.json"))
    store.migrate_manual(config["custom_vocabulary"])        # idempotent
    prompt_terms = store.compose_prompt(budget_chars=600)    # → initial_prompt
    n = store.add_candidates(
        mine_texts(texts, known=store.known_keys(), min_freq=3), source="meetings")
    store.observe_text(final_transcript)                     # usage ranking
"""
import json
import os
import re
import threading
import time

# Common English words that are NOT vocabulary material even when they show up
# inside Hebrew transcripts (bilingual meetings produce plenty of plain English).
STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from get got had
has have he her him his how i if in into is it its just like me more most my
no not now of off on one or our out over so some than that the their them then
there these they this to up us very was we were what when where which who why
will with would you your yes okay ok yeah right well see say said says need
want know think going go goes gone make makes made take takes look looks let
lets really actually maybe something anything nothing thing things way time
new good great sure also two three about after before because come came still
much many other only even back down here work works don done use used using
it's that's don't can't won't didn't isn't wasn't aren't doesn't haven't
i'm we're they're you're he's she's there's what's let's i've we've
both best better first last next again once too any all each every same
user users failed found ready start started end call today please talk
management source engine live part case point"""
                      .split())

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-']{0,29}")
_ALLDIGIT_RE = re.compile(r"^[\d.\-+#']+$")


def _norm(term):
    """Case-insensitive dedup key."""
    return term.strip().lower()


def _ok_token(tok):
    t = tok.strip("'.-")
    if len(t) < 2 or _ALLDIGIT_RE.match(t):
        return False
    low = t.lower()
    if low in STOPWORDS or low.startswith(("http", "www")):
        return False
    return True


def mine_texts(texts, known=(), min_freq=3, max_candidates=300):
    """Extract vocabulary candidates from an iterable of transcript strings.

    Heuristics (host-language agnostic, tuned for Hebrew-heavy corpora where
    any embedded Latin token is very likely domain vocabulary):
      - single Latin-script tokens (freq >= min_freq, stopword-filtered)
      - two-word phrases of ADJACENT Latin tokens where neither is a stopword
        ("git push", "API Gateway") — counted independently of their parts
    Returns [{term, count, sample}] sorted by count desc; `term` is the most
    common surface form seen (preserves the user's casing like "GitHub").
    `known` suppresses already-tracked/rejected keys.
    """
    known = set(known)
    counts = {}        # key -> total count
    surfaces = {}      # key -> {surface: count}
    samples = {}       # key -> first context snippet

    def bump(surface, text, start, end):
        # Normalize the surface: strip stray punctuation the token regex
        # allows mid-word ("AWS." → "AWS") so dedup vs known terms works.
        surface = surface.strip("'.-")
        key = _norm(surface)
        if not key or key in known:
            return
        counts[key] = counts.get(key, 0) + 1
        surfaces.setdefault(key, {})
        surfaces[key][surface] = surfaces[key].get(surface, 0) + 1
        if key not in samples:
            lo, hi = max(0, start - 30), min(len(text), end + 30)
            samples[key] = text[lo:hi].replace("\n", " ").strip()

    for text in texts:
        if not text:
            continue
        matches = list(_WORD_RE.finditer(text))
        for i, m in enumerate(matches):
            tok = m.group(0)
            if _ok_token(tok):
                bump(tok, text, m.start(), m.end())
            # Adjacent-pair phrase: exactly one space between the two tokens.
            if i + 1 < len(matches):
                n = matches[i + 1]
                if (n.start() - m.end() == 1 and text[m.end()] == " "
                        and _ok_token(tok) and _ok_token(n.group(0))):
                    bump(tok + " " + n.group(0), text, m.start(), n.end())

    out = []
    for key, cnt in counts.items():
        if cnt < min_freq:
            continue
        surface = max(surfaces[key].items(), key=lambda kv: kv[1])[0]
        out.append({"term": surface, "count": cnt, "sample": samples.get(key, "")})
    out.sort(key=lambda c: -c["count"])
    return out[:max_candidates]


def _correction_re(wrong):
    """Whole-word, case-insensitive matcher for a garble `wrong`, with
    Hebrew-aware boundaries (no letter/digit glued to either side, so "Bedrook"
    matches but "Bedrooks"/"aBedrook" don't). SHARED by apply_corrections (live
    fixes) and count_corrections (archive scan) so both use identical matching."""
    return re.compile(r"(?<![A-Za-z0-9֐-׿])"
                      + re.escape(wrong)
                      + r"(?![A-Za-z0-9֐-׿])",
                      re.IGNORECASE)


def apply_corrections(text, pairs):
    """Apply known mis-transcription fixes to `text`: for each {wrong, right}
    pair, replace whole-word, case-insensitive occurrences of `wrong` with
    `right` (the canonical casing). Deterministic find-and-replace — no model
    involved. Returns (new_text, applied_counts) where applied_counts maps
    wrong-key -> number of replacements."""
    counts = {}
    if not text or not pairs:
        return text, counts
    for p in pairs:
        wrong = (p.get("wrong") or "").strip()
        right = (p.get("right") or "").strip()
        if not wrong or not right or _norm(wrong) == _norm(right):
            continue
        text, n = _correction_re(wrong).subn(right, text)
        if n:
            counts[_norm(wrong)] = counts.get(_norm(wrong), 0) + n
    return text, counts


def count_corrections(texts, pairs):
    """Count how many times each correction's `wrong` occurs across `texts`
    (a string or list of strings), using the SAME matcher live application uses.
    Read-only relevance scan (no replacement, no model): tells the host how often
    each fix would fire on the existing archive, so never-hit corrections can be
    flagged for pruning. Returns {wrong_norm: total_hits}."""
    counts = {}
    if not pairs:
        return counts
    joined = texts if isinstance(texts, str) else "\n".join(t for t in texts if t)
    if not joined:
        return counts
    for p in pairs:
        wrong = (p.get("wrong") or "").strip()
        if not wrong:
            continue
        n = len(_correction_re(wrong).findall(joined))
        counts[_norm(wrong)] = counts.get(_norm(wrong), 0) + n
    return counts


class VocabStore:
    """vocabulary.json wrapper. Thread-safe; atomic writes; buffered usage.
    Holds TERMS (prompt-bias vocabulary) and CORRECTIONS (wrong→right
    mis-transcription fixes applied post-transcription by the host)."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._terms = {}          # norm key -> dict
        self._corrections = {}    # norm(wrong) -> {wrong, right, ...}
        self._flags = {}          # one-time seeds etc.
        self._dirty_usage = 0
        self._last_save = time.time()
        self._load()

    # ---------- persistence ----------
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            for t in data.get("terms", []):
                if isinstance(t, dict) and t.get("term"):
                    self._terms[_norm(t["term"])] = t
            for c in data.get("corrections", []):
                if isinstance(c, dict) and c.get("wrong") and c.get("right"):
                    self._corrections[_norm(c["wrong"])] = c
            self._flags = dict(data.get("flags") or {})
        except FileNotFoundError:
            pass
        except Exception:
            # Corrupt store: keep a .bad copy for forensics, start fresh —
            # losing ranks is annoying; crashing transcription is worse.
            try:
                os.replace(self.path, self.path + ".bad")
            except OSError:
                pass

    def save(self):
        with self._lock:
            data = {"version": 1, "terms": list(self._terms.values()),
                    "corrections": list(self._corrections.values()),
                    "flags": self._flags}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            self._dirty_usage = 0
            self._last_save = time.time()

    # ---------- population ----------
    def migrate_manual(self, comma_string):
        """Import the host's manual comma-list as starred manual terms.
        Idempotent; never demotes an existing entry. Returns #new."""
        added = 0
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            for raw in (comma_string or "").split(","):
                term = raw.strip()
                if not term:
                    continue
                key = _norm(term)
                cur = self._terms.get(key)
                if cur is None:
                    self._terms[key] = {
                        "term": term, "kind": "manual", "status": "approved",
                        "starred": True, "count_corpus": 0, "count_used": 0,
                        "sources": ["manual"], "first_seen": today,
                        "last_seen": today, "sample": "",
                    }
                    added += 1
                else:
                    cur["kind"] = "manual"
                    cur["status"] = "approved"
                    cur["starred"] = True
        if added:
            self.save()
        return added

    def set_manual_terms(self, comma_string):
        """Reconcile manual terms with an edited comma-list: new ones added,
        removed ones DEMOTED to rejected-manual (so the miner won't resurface
        a term the user explicitly deleted)."""
        new_keys = {_norm(t) for t in (comma_string or "").split(",") if t.strip()}
        with self._lock:
            for key, t in self._terms.items():
                if t.get("kind") == "manual" and key not in new_keys:
                    t["status"] = "rejected"
                    t["starred"] = False
        self.migrate_manual(comma_string)   # saves

    def known_keys(self):
        with self._lock:
            return set(self._terms.keys())

    def add_candidates(self, candidates, source="corpus"):
        """Add miner output as PENDING suggestions. Known keys are refreshed
        (corpus count updated) but never re-suggested once rejected."""
        added = 0
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            for c in candidates:
                key = _norm(c["term"])
                cur = self._terms.get(key)
                if cur is not None:
                    cur["count_corpus"] = max(int(cur.get("count_corpus", 0)),
                                              int(c.get("count", 0)))
                    cur["last_seen"] = today
                    continue
                self._terms[key] = {
                    "term": c["term"], "kind": "auto", "status": "pending",
                    "starred": False, "count_corpus": int(c.get("count", 0)),
                    "count_used": 0, "sources": [source], "first_seen": today,
                    "last_seen": today, "sample": c.get("sample", ""),
                }
                added += 1
        if added:
            self.save()
        return added

    # ---------- review ----------
    def pending(self):
        with self._lock:
            out = [dict(t) for t in self._terms.values()
                   if t.get("status") == "pending"]
        out.sort(key=lambda t: -int(t.get("count_corpus", 0)))
        return out

    def resolve(self, keys, approve):
        """Approve or reject a batch of pending keys."""
        status = "approved" if approve else "rejected"
        with self._lock:
            for k in keys:
                t = self._terms.get(_norm(k))
                if t is not None:
                    t["status"] = status
        self.save()

    def pending_count(self):
        with self._lock:
            return sum(1 for t in self._terms.values()
                       if t.get("status") == "pending")

    # ---------- usage ranking ----------
    def observe_text(self, text):
        """Bump count_used for approved terms appearing in `text` (word-ish
        boundaries, case-insensitive). Buffered: auto-flushes every 25 bumps
        or 3 minutes."""
        if not text:
            return 0
        low = text.lower()
        bumps = 0
        with self._lock:
            for key, t in self._terms.items():
                if t.get("status") != "approved":
                    continue
                idx = low.find(key)
                if idx < 0:
                    continue
                before = low[idx - 1] if idx > 0 else " "
                after_i = idx + len(key)
                after = low[after_i] if after_i < len(low) else " "
                if not before.isalnum() and not after.isalnum():
                    t["count_used"] = int(t.get("count_used", 0)) + 1
                    bumps += 1
            self._dirty_usage += bumps
            need_save = (self._dirty_usage >= 25
                         or time.time() - self._last_save > 180)
        if bumps and need_save:
            self.save()
        return bumps

    # ---------- composition ----------
    def compose_prompt(self, budget_chars=600):
        """Ranked vocabulary string for Whisper's initial_prompt.

        Selection: ALL manual/starred terms (never dropped), then approved
        auto terms by score (usage-weighted) while the budget allows.
        ORDER: least-important first, most-important LAST — Whisper keeps the
        prompt TAIL, so the tail is prime real estate.
        """
        def score(t):
            return int(t.get("count_used", 0)) * 3 + int(t.get("count_corpus", 0))

        with self._lock:
            manual = [t for t in self._terms.values()
                      if t.get("status") == "approved"
                      and (t.get("kind") == "manual" or t.get("starred"))]
            autos = [t for t in self._terms.values()
                     if t.get("status") == "approved"
                     and t.get("kind") == "auto" and not t.get("starred")]
        manual.sort(key=score)                      # most-used manual → last
        autos.sort(key=lambda t: -score(t))         # pick best autos first
        used = sum(len(t["term"]) + 2 for t in manual)
        picked = []
        for t in autos:
            cost = len(t["term"]) + 2
            if used + cost > budget_chars:
                continue
            picked.append(t)
            used += cost
        picked.sort(key=score)                      # least → most important
        ordered = picked + manual                   # manual (starred) at the TAIL
        return ", ".join(t["term"] for t in ordered)

    # ---------- corrections (wrong → right mis-transcription fixes) ----------
    def add_corrections(self, pairs, source="llm", force=False):
        """Add wrong→right fixes with guardrails. Returns #added/updated.
        Rejected when: wrong == right (case-insensitive), silly lengths, or —
        unless `force` (an explicit MANUAL entry) — `wrong` is itself an
        APPROVED vocabulary term (never let the LLM rewrite a correct term).
        With force, an existing pair's `right` side is UPDATED (manual fix)."""
        added = 0
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            for p in pairs or []:
                wrong = (p.get("wrong") or "").strip()
                right = (p.get("right") or "").strip()
                if not wrong or not right:
                    continue
                wk = _norm(wrong)
                if (wk == _norm(right) or not (2 <= len(wrong) <= 40)
                        or not (2 <= len(right) <= 40)):
                    continue
                if wk in self._corrections:
                    cur = self._corrections[wk]
                    if force and cur.get("right") != right:
                        cur["right"] = right
                        cur["source"] = source
                        added += 1
                    continue
                term = self._terms.get(wk)
                if (not force and term is not None
                        and term.get("status") == "approved"):
                    continue        # `wrong` is a real term — refuse the rewrite
                self._corrections[wk] = {
                    "wrong": wrong, "right": right, "source": source,
                    "count_applied": 0, "first_seen": today,
                }
                added += 1
        if added:
            self.save()
        return added

    def remove_corrections(self, wrongs):
        """Delete pairs by their `wrong` side. Returns #removed."""
        removed = 0
        with self._lock:
            for wrongkey in wrongs or []:
                if self._corrections.pop(_norm(wrongkey), None) is not None:
                    removed += 1
        if removed:
            self.save()
        return removed

    def corrections(self):
        with self._lock:
            return [dict(c) for c in self._corrections.values()]

    def set_corpus_hits(self, mapping):
        """Stamp each correction with how many times it matched the archive scan
        (`corpus_hits`, keyed by norm(wrong); 0 for anything not in `mapping`)
        plus the scan date, so the UI can flag never-hit corrections. Persists."""
        stamp = time.strftime("%Y-%m-%d")
        mapping = mapping or {}
        with self._lock:
            for wk, c in self._corrections.items():
                c["corpus_hits"] = int(mapping.get(wk, 0))
                c["corpus_scanned"] = stamp
        self.save()

    def approved_autos(self):
        """Approved AUTO-learned terms (not manual, not starred) — the set the
        'Manage learned terms' dialog offers for removal. Sorted best-first."""
        with self._lock:
            out = [dict(t) for t in self._terms.values()
                   if t.get("status") == "approved"
                   and t.get("kind") == "auto" and not t.get("starred")]
        out.sort(key=lambda t: -(int(t.get("count_used", 0)) * 3
                                 + int(t.get("count_corpus", 0))))
        return out

    def record_applied(self, counts):
        """Bump count_applied after apply_corrections(); buffered like usage."""
        if not counts:
            return
        with self._lock:
            for wk, n in counts.items():
                c = self._corrections.get(wk)
                if c is not None:
                    c["count_applied"] = int(c.get("count_applied", 0)) + int(n)
            self._dirty_usage += sum(counts.values())
            need_save = (self._dirty_usage >= 25
                         or time.time() - self._last_save > 180)
        if need_save:
            self.save()

    def seed_once(self, flag, pairs, source="seed"):
        """One-time corrections seeding, guarded by a persisted flag (safe to
        call on every startup)."""
        with self._lock:
            if self._flags.get(flag):
                return 0
            self._flags[flag] = True
        n = self.add_corrections(pairs, source=source)
        self.save()      # persist the flag even when n == 0
        return n

    def stats(self):
        with self._lock:
            n = len(self._terms)
            by = {}
            for t in self._terms.values():
                by[t.get("status", "?")] = by.get(t.get("status", "?"), 0) + 1
            by["corrections"] = len(self._corrections)
        return {"total": n, **by}
