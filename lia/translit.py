"""Hebrew transliteration -> the user's own English term (2026-11-14).

A Hebrew-only speech model writes an English word the way it sounds
("סקיוריטי", "טרנזיט גטווי"). When the user's vocabulary holds the term
("Security", "Transit Gateway"), the Hebrew spelling is replaced by it.

How a match is decided (no model, no network):
- both sides become a consonant key (classes: b/v, v/w, p/f, k/c/q, s/z, t,
  d, g, j, sh, ch, l, m, n, r); h and the Hebrew vowel carriers are dropped;
- the keys must be EQUAL, with at least 3 consonants;
- the Hebrew vowel letters must agree with the English spelling slot by slot
  (a Hebrew vav needs an o/u/w there and the other way round, a yod needs a
  vowel, a word that opens or closes on a vowel does so on both sides);
- a key that fits two different terms is left alone;
- a word the Hebrew dictionary knows is left alone, and without a dictionary
  nothing is replaced at all.

Measured 2026-11-14 on 130 real meeting transcripts (601,000 words) with the
user's 283 approved terms: about 1,950 replacements, a handful doubtful; the
same run without the dictionary added 558 more, most of them wrong
("תוכן" -> token, "בזק" -> basic). Initials (AWS, EKS) are not matched here.
"""
import re

HEB = "א-ת"
_FINALS = str.maketrans("ךםןףץ", "כמנפצ")
_HEB_CONS = {
    "ב": "B", "ג": "G", "ד": "D", "ז": "S", "ח": "?", "ט": "T", "כ": "K",
    "ל": "L", "מ": "M", "נ": "N", "ס": "S", "פ": "P", "צ": "?", "ק": "K",
    "ר": "R", "ש": "X", "ת": "T",
}
_GERESH = "'׳’"
PREFIXES = ("", "ה", "ב", "ל", "ו", "מ", "ש", "כ", "וה", "וב", "ול", "שה", "מה", "כש", "לה", "בה")
MIN_CONS = 3
MAX_WORDS = 3


def _he_word(w):
    """One Hebrew word -> (cons, slots): cons = list of consonant classes,
    slots = len(cons)+1 strings of vowel marks (A, I, O) around them."""
    w = w.translate(_FINALS)
    cons, slots = [], [""]
    i, n = 0, len(w)
    while i < n:
        ch = w[i]
        nxt = w[i + 1] if i + 1 < n else ""
        if nxt and nxt in _GERESH and ch in "גזצת":
            cons.append({"ג": "J", "ז": "J", "צ": "C", "ת": "T"}[ch])
            slots.append("")
            i += 2
            continue
        if ch in _GERESH:
            i += 1
            continue
        if ch == "ו":
            if nxt == "ו":                       # double vav = w / v
                cons.append("W")
                slots.append("")
                i += 2
                continue
            if i == 0 and nxt:                   # a word opens on a consonant vav
                cons.append("W")
                slots.append("")
            else:
                slots[-1] += "O"
            i += 1
            continue
        if ch == "י":
            slots[-1] += "I"
        elif ch in "אע":
            slots[-1] += "A"
        elif ch == "ה":
            if i == 0:
                slots[-1] += "H"
            elif i == n - 1:
                slots[-1] += "A"
        elif ch in _HEB_CONS:
            cons.append(_HEB_CONS[ch])
            slots.append("")
        i += 1
    return cons, slots


def _join(parts):
    """Words parsed one by one -> one (cons, slots)."""
    cons, slots = [], [""]
    for c, s in parts:
        slots[-1] += s[0]
        cons += c
        slots += s[1:]
    return cons, slots


def he_key(words):
    return _join([_he_word(w) for w in words])


_VOW = "aeiouy"
# (letters, [alternatives]); an alternative is a list of consonant classes.
_EN_MULTI = [
    ("ssion", [["X", "N"]]), ("tion", [["X", "N"]]), ("sion", [["X", "N"], ["J", "N"]]), ("gion", [["J", "N"]]),
    ("ture", [["C", "R"]]), ("dge", [["J"]]),
    ("sch", [["S", "K"]]), ("tch", [["C"]]),
    ("ch", [["C"], ["K"], ["X"]]), ("sh", [["X"]]), ("ph", [["P"]]), ("th", [["T"], ["D"]]),
    ("ck", [["K"]]), ("qu", [["K", "W"]]), ("wh", [["W"]]), ("wr", [["R"]]),
    ("kn", [["N"]]), ("gh", [[]]), ("ng", [["N", "G"]]),
]
_EN_ONE = {
    "b": "B", "d": "D", "f": "P", "p": "P", "k": "K", "q": "K",
    "l": "L", "m": "M", "n": "N", "r": "R", "t": "T", "j": "J",
}


def _en_word(w):
    """One English word -> list of (cons, slots) readings. A slot holds the
    vowel LETTERS found there; '*' marks a slot where anything goes."""
    w = re.sub(r"[^a-z]", "", w.lower())
    out = [([], [""])]

    def add(alts, free=False):
        # free = the slot inside the cluster may hold anything (-tion is
        # written שן, שיין or שון)
        nonlocal out
        new = []
        for cons, slots in out:
            for alt in alts:
                c, s = list(cons), list(slots)
                for j, k in enumerate(alt):
                    if c and c[-1] == k and not s[-1]:
                        continue
                    c.append(k)
                    s.append("*" if (free and j == 0) else "")
                new.append((c, s))
        out = new

    def vowel(ch):
        for _c, s in out:
            s[-1] += ch

    i, n = 0, len(w)
    while i < n:
        rest = w[i:]
        ch = w[i]
        nxt = w[i + 1] if i + 1 < n else ""
        hit = None
        for letters, alts in _EN_MULTI:
            if rest.startswith(letters):
                hit = (letters, alts)
                break
        if hit and hit[0] == "kn" and i:
            hit = None
        if hit and hit[0] == "gh" and i == 0:
            hit = ("gh", [["G"]])
        if hit:
            letters, alts = hit
            add(alts, free=letters in ("ssion", "tion", "sion", "gion", "ture"))
            i += len(letters)
            continue
        if ch in "aeiou":
            if ch == "e" and i == n - 1 and any(c for c, _s in out):
                pass                             # a silent final e
            else:
                vowel(ch)
        elif ch == "y":
            vowel("y")
        elif ch == "h":
            if i == 0:
                vowel("h")
        elif ch == "w":
            if nxt and nxt in _VOW:
                add([["W"]])
                if w[i:i + 3] == "wor":          # work, network: no vowel is written
                    i += 2
                    continue
            elif i and w[i - 1] in _VOW:
                vowel("w")
            else:
                add([["W"]])
        elif ch == "v":
            add([["B"], ["W"]])
        elif ch == "c":
            if nxt and nxt in "eiy":
                add([["S"]])
            else:
                add([["K"]])
        elif ch == "g":
            if nxt and nxt in "eiy":
                add([["J"], ["G"]])
            elif i == n - 1 or nxt != "e":
                add([["G"]])
            else:
                add([["G"]])
        elif ch == "s":
            add([["S"]])
        elif ch == "z":
            add([["S"], ["J"]] if nxt == "u" else [["S"]])
        elif ch == "x":
            add([["K", "S"]])
        elif ch in _EN_ONE:
            add([[_EN_ONE[ch]]])
        i += 1
    return out


def en_keys(term):
    """Every reading of an English term: list of (cons tuple, slots)."""
    words = [x for x in re.split(r"[\s\-_/.]+", term) if x]
    readings = [([], [""])]
    for w in words:
        nxt = []
        for a in readings:
            for b in _en_word(w):
                c, s = _join([a, b])
                nxt.append((c, s))
        readings = nxt[:16]
    return [(tuple(c), s) for c, s in readings]


def _vowels_agree(he_slots, en_slots):
    last = len(he_slots) - 1
    for i, (h, e) in enumerate(zip(he_slots, en_slots)):
        if "*" in e:
            continue
        if "O" in h and not re.search("[ouw]", e):
            return False
        if "II" in h:
            if not re.search("[aiy]", e):
                return False
        elif "I" in h and not re.search("[eiyu]", e):
            return False
        if "o" in e and "O" not in h:
            return False
        if i == 0:
            if ("H" in h) != e.startswith("h"):
                return False
            if bool(h.replace("H", "")) != bool(e.replace("h", "")):
                return False
        elif i == last:
            if h and not e:
                return False
            if e and e[-1] in "yi" and "I" not in h:
                return False
            if e and e[-1] == "a" and "A" not in h:
                return False
            if h.endswith("A") and "I" not in h and "O" not in h and not e.endswith("a"):
                return False
        elif h == "A" and not e:
            return False
    return True


def usable_term(term):
    """Terms the sound match may produce: words, not initials or codes."""
    t = (term or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z \-/]*", t):
        return False
    letters = re.sub(r"[^A-Za-z]", "", t)
    if len(letters) < 4:
        return False
    return not any(w.isupper() for w in re.split(r"[\s\-/]+", t) if w)


class Matcher:
    """Built once from the vocabulary terms. `is_hebrew_word(w)` is the Hebrew
    dictionary: without one nothing is replaced (measured: without it words
    like "תוכן" become "token")."""

    MEMO_MAX = 20000

    def __init__(self, terms, is_hebrew_word):
        self.is_hebrew_word = is_hebrew_word
        self.index = {}
        self._memo = {}
        seen = set()
        for term in terms or ():
            t = (term or "").strip()
            if not usable_term(t) or t.lower() in seen:
                continue
            seen.add(t.lower())
            for cons, slots in en_keys(t):
                if len(cons) >= MIN_CONS:
                    self.index.setdefault(cons, []).append(
                        (t, slots, len(re.split(r"[\s\-/]+", t))))

    def _lookup(self, words, strict):
        cons, slots = he_key(words)
        cands = self.index.get(tuple(cons))
        if not cands:
            return None
        hits = {t for t, en_slots, n in cands
                if _vowels_agree(slots, en_slots) and (n == len(words) or not strict)}
        if len(hits) != 1:
            return None
        return next(iter(hits))

    def _known(self, w):
        try:
            return bool(self.is_hebrew_word(w))
        except Exception:
            return True

    def match(self, words):
        """`words` = 1..MAX_WORDS Hebrew words. -> (prefix, term) or None.
        Real Hebrew words are never turned into a term: a run made only of
        dictionary words is left alone, and a run that holds a dictionary
        word may only become a term of as many words ("זה איבנט" is not
        Subnet, "לנדינג זון" is Landing Zone)."""
        if self.is_hebrew_word is None:
            return None
        key = tuple(words)
        if key in self._memo:
            return self._memo[key]
        got = self._match(words)
        if len(self._memo) < self.MEMO_MAX:
            self._memo[key] = got
        return got

    def _match(self, words):
        first = words[0]
        if len(words) > 1 and not all(_he_word(w)[0] for w in words):
            return None                          # a word that adds no sound is not part of a term
        known = [self._known(w) for w in words]
        if all(known):
            return None
        strict = any(known)
        for p in PREFIXES:
            if p and not (first.startswith(p) and len(first) - len(p) >= 2):
                continue
            t = self._lookup([first[len(p):]] + list(words[1:]), strict)
            if t:
                return p, t
        if len(words) == 1 and first.endswith("ים") and len(first) > 5:
            for p in PREFIXES:
                if p and not first.startswith(p):
                    continue
                t = self._lookup([first[len(p):-2]], strict)
                if t and t[-1].lower() not in "xzh":
                    return p, (t if t.endswith("s") else t + "s")
        return None

    _RUN = re.compile("[%s][%s'\u05f3\u2019]*(?:[ ][%s][%s'\u05f3\u2019]*)*" % (HEB, HEB, HEB, HEB))

    def fix(self, text):
        """-> (text with the terms put in, [(hebrew, term)])."""
        if not text or not self.index or self.is_hebrew_word is None:
            return text, []
        done = []

        def run(m):
            words = m.group(0).split(" ")
            out, i = [], 0
            while i < len(words):
                hit = None
                for k in range(min(MAX_WORDS, len(words) - i), 0, -1):
                    got = self.match(words[i:i + k])
                    if got:
                        hit = (k, got)
                        break
                if hit:
                    k, (pre, t) = hit
                    out.append((pre + "-" + t) if pre else t)
                    done.append((" ".join(words[i:i + k]), t))
                    i += k
                else:
                    out.append(words[i])
                    i += 1
            return " ".join(out)

        return self._RUN.sub(run, text), done
