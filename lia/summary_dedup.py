"""Near-duplicate detection for merged summary fragments (pure functions, stdlib only).

Copy of the private upstream project's dedup module (2026-08-27 port) - keep the two in
sync the way vocab_learn.py is kept in sync. ONE Lia-side addition on top of
the origin file (english-support (2026-08), 2026-08-28): _token_matches also
tolerates English plural/possessive tails ('s / s), the EN mirror of the
Hebrew glue-prefix tolerance - port it back to the upstream project on the next sync.

Map-reduce windows are summarised independently, so the same task or topic can come
back twice in different wording - a boundary utterance summarised by both of its
windows, or a topic the meeting RETURNED to later whose revisit landed in another
window. The window merges dedup on EXACT normalized text only, which lets every
paraphrase through (measured 2026-08-26/27: mr output reads "too long" mostly
because of this class). This module is the closed-rule detector those merges call.

The rule is deliberately CLOSED and deterministic (the project rule: protect
meaning with code guards, never prompt alone), and biased to KEEP both lines when
unsure - a duplicate that survives is a wart, a false merge loses content:

  1. A number-token CONFLICT (each side carries numbers the other lacks) makes the
     lines DISTINCT no matter how similar - "VLAN לרשת 10" vs "VLAN לרשת 20" are
     two tasks. A SUBSET (the revisit only added a date/amount) is not a conflict:
     that is exactly the revisited-topic enrichment case and it must merge.
  2. Token CONTAINMENT: when one side's content tokens all appear in the other
     (Hebrew-prefix tolerant), the shorter says nothing the longer does not -
     merge. Requires >= 3 content tokens so trivia never merges by containment.
  3. Otherwise a similarity gate: prefix-tolerant token Jaccard or a difflib
     sequence ratio. Thresholds calibrated on real window-merge pairs from the
     2026-08-27 baseline runs.

Everything here is pure so it unit-tests offline (no GPU / no Ollama), matching
the rest of the suite.
"""

from __future__ import annotations

import difflib
import re

# The same closed number normalization the condense guards use ('16,000' == '16000';
# a trailing sentence period is not part of the number). summarize._condense_numbers
# delegates here so there is exactly ONE definition of "a number token".
_NUM_RE = re.compile(r"\d[\d,.:]*")

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)

# Single-letter Hebrew prefixes that glue onto the next word (ו/ה/ב/ל/מ/ש/כ):
# 'פגישה' and 'הפגישה' are the same content token for similarity purposes.
_HEB_PREFIXES = "והבלמשכ"

# Calibrated on the 2026-08-27 baseline window-merge pairs: boundary duplicates are
# REPHRASINGS of one utterance (ratio typically >= 0.85); genuinely distinct tasks
# about the same system sit well below both gates.
RATIO_THRESHOLD = 0.82
JACCARD_THRESHOLD = 0.60
CONTAINMENT_MIN_TOKENS = 3


def number_tokens(text: str) -> set:
    """The normalized number tokens of `text` - the facts a merge may never conflate."""
    return {t.rstrip(".,:").replace(",", "") for t in _NUM_RE.findall(text or "")}


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _tokens(text: str) -> list:
    """Content tokens: casefolded words of >= 2 chars (checkbox/punctuation noise out)."""
    return [t for t in _WORD_RE.findall(_norm_text(text)) if len(t) >= 2]


def _strip_en_tail(t: str) -> str:
    """English plural/possessive tail off a token ('vlans'/'vlan's' -> 'vlan').
    Min length 4 so short real words (its, has) never lose their s."""
    if len(t) >= 5 and t.endswith("'s"):
        return t[:-2]
    if len(t) >= 4 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def _token_matches(t: str, other: set) -> bool:
    """True when `t` appears in `other` directly, modulo ONE Hebrew glue prefix,
    or modulo an English plural/possessive tail (the EN morphology mirror)."""
    if t in other:
        return True
    if len(t) >= 3 and t[0] in _HEB_PREFIXES and t[1:] in other:
        return True
    if any(len(o) >= 3 and o[0] in _HEB_PREFIXES and o[1:] == t for o in other):
        return True
    ts = _strip_en_tail(t)
    return ts != t and ts in other or any(_strip_en_tail(o) == t for o in other)


def _matched_count(ta: list, tb_set: set) -> int:
    return sum(1 for t in set(ta) if _token_matches(t, tb_set))


def is_near_duplicate(a: str, b: str, *, ratio: float = RATIO_THRESHOLD,
                      jaccard: float = JACCARD_THRESHOLD) -> bool:
    """The closed rule above. Empty input never matches anything."""
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    num_a, num_b = number_tokens(a), number_tokens(b)
    if (num_a - num_b) and (num_b - num_a):
        return False   # true conflict: each side has numbers the other lacks
    ta, tb = _tokens(a), _tokens(b)
    sa, sb = set(ta), set(tb)
    if sa and sb:
        m_ab, m_ba = _matched_count(ta, sb), _matched_count(tb, sa)
        # containment: the smaller side adds no content token of its own
        if m_ab == len(sa) and len(sa) >= CONTAINMENT_MIN_TOKENS:
            return True
        if m_ba == len(sb) and len(sb) >= CONTAINMENT_MIN_TOKENS:
            return True
        union = len(sa) + len(sb) - min(m_ab, m_ba)
        if union and min(m_ab, m_ba) / union >= jaccard:
            return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= ratio


def prefer(a: str, b: str) -> int:
    """Which of two near-duplicate texts to KEEP: 0 for `a`, 1 for `b`.
    The number-superset side wins (it carries the added date/amount - the revisit),
    then the longer text; on a tie the first (earlier window) stays."""
    num_a, num_b = number_tokens(a), number_tokens(b)
    if num_a > num_b:
        return 0
    if num_b > num_a:
        return 1
    return 1 if len(_norm_text(b)) > len(_norm_text(a)) else 0
