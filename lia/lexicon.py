"""Hebrew lexicon post-pass: fix the -ין/-ים plural confusion Whisper makes on
fast Hebrew ("ביטולין" -> "ביטולים") using a plain word-form set (the
hspell-derived Hunspell .dic, DOWNLOADED at runtime - AGPL data, never shipped).

Whisper/ivrit is an acoustic model with no dictionary: it faithfully writes the
sound [in] it heard for the plural suffix -ים, producing non-words like ביטולין.
This is a PATTERN (tomorrow תשלומין, הזמנין), not a per-word table entry, so a
lexical post-pass is the right layer. The ONLY thing auto-fixed here is that one
plural suffix, guarded so it never rewrites a real ktiv-haser singular
(מעונין, הבנין) or a legitimate -ין plural (נישואין). Everything else that is
out-of-lexicon is only SUGGESTED (by the caller), never rewritten.

Pure stdlib, deterministic, thread-safe. When the dictionary is absent every
method is a no-op that returns the text unchanged - the feature is opt-in and
FAIL-SAFE (never fail-open into rewriting).
"""
import os
import re
import threading

# A Hebrew word: a Hebrew letter followed by Hebrew letters / geresh / gershayim.
# Excludes the leading RLM (‏) ivrit prepends, so re.sub keeps it in place.
HEB_TOKEN = re.compile(r"[֐-׿][֐-׿\"'׳״]*")

# Hebrew proclitic prefixes (single + common stacks). Over-accepting a word as
# valid is SAFE here: a word wrongly accepted is simply never touched. The point
# is only to avoid flagging a legitimately-prefixed word (ל+ביטול) as unknown.
PREFIXES = ("", "ו", "ה", "ב", "ל", "כ", "מ", "ש",
            "וה", "וב", "ול", "וכ", "ומ", "וש", "שב", "של", "שה", "שכ", "שמ",
            "מה", "בה", "לה", "כה", "כש", "וכש", "כשה", "וכשה", "כשב", "כשל",
            "ושב", "ושה", "ושל", "מש", "ומש", "ומה", "ולה", "ובה", "לכש", "מכ",
            "שלה", "ובכ", "ממ", "וממ", "שמה", "ולכש")

# -ין words that are OOV in the dict but must never be auto-rewritten: proper
# names and rare Talmudic plurals absent from a modern word list. Extend freely;
# a stoplist hit is silent (these are simply left alone).
STOPLIST = frozenset({"רבין", "לוין", "בגין", "עירובין", "פרקין", "בעלין",
                      "סניגורין", "קטגורין", "מילין"})


class Lexicon:
    """A word-form set loaded from a Hunspell .dic (surface forms only), plus
    the single guarded -ין->-ים auto-fix. Not loaded => everything is 'valid'
    and fix() is a no-op."""

    def __init__(self, dic_path):
        self.path = dic_path
        self._words = None            # frozenset once loaded
        self._lock = threading.Lock()
        self._cache = {}

    @property
    def ready(self):
        return self._words is not None

    def word_count(self):
        return len(self._words) if self._words is not None else 0

    def load(self):
        """Load the .dic surface forms (its first line is the entry count).
        ~0.1s / ~45 MB for the 341k-form Hebrew list. Safe to call on a daemon
        thread; raises on a missing/broken file (the caller guards)."""
        words = set()
        with open(self.path, encoding="utf-8") as f:
            next(f, None)             # drop the count header line
            for line in f:
                w = line.split("/", 1)[0].strip()
                if w:
                    words.add(w)
        with self._lock:
            self._words = frozenset(words)
            self._cache = {}

    def valid(self, w):
        """True if `w` is a known form (optionally behind a Hebrew proclitic
        prefix). Not-loaded => True (so the fix is a no-op, never fail-open)."""
        words = self._words
        if words is None:
            return True
        c = self._cache.get(w)
        if c is None:
            c = any(w.startswith(p) and w[len(p):] in words for p in PREFIXES)
            if len(self._cache) < 50000:
                self._cache[w] = c
        return c

    def fix_word(self, w, protected=()):
        """The one auto-fix: OOV + ends in -ין + the yod-doubled form is NOT a
        valid word (so it's not a ktiv-haser singular like מעונין->מעוניין) +
        the -ים form IS valid -> return the -ים form. Otherwise None."""
        if len(w) < 4 or not w.endswith("ין") or w in STOPLIST or w in protected:
            return None
        if self.valid(w):
            return None
        # yod-guard: מעונין / הבנין / קנין are defective-spelling SINGULARS whose
        # ktiv-male form doubles the yod. If X-יין is a real word, X-ין is that
        # singular, not a mis-heard plural - hands off.
        if self.valid(w[:-2] + "יין"):   # X + ין -> X + יין
            return None
        cand = w[:-2] + "ים"                    # -ין -> -ים
        return cand if self.valid(cand) else None

    def fix(self, text, protected=()):
        """Apply fix_word to every Hebrew token in `text`. Returns
        (new_text, fixes, oov): `fixes` = [(wrong, right)] applied, `oov` =
        [word] left alone but out-of-lexicon (candidates for the caller's
        suggestion list). Punctuation, spacing and the leading RLM are
        preserved - only the Hebrew run is ever replaced. No-op if not loaded."""
        if not text or self._words is None:
            return text, [], []
        fixes, oov = [], []
        prot = protected if isinstance(protected, (set, frozenset)) else set(protected or ())

        def _sub(m):
            w = m.group(0)
            r = self.fix_word(w, prot)
            if r is not None:
                fixes.append((w, r))
                return r
            if len(w) >= 4 and not self.valid(w):
                oov.append(w)
            return w

        return HEB_TOKEN.sub(_sub, text), fixes, oov
