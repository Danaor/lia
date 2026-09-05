"""Question-mark / prosody bench for Hebrew dictation (WP0, plan 2026-09-05).

Measures how well the dictation pipeline puts a "?" on questions, using clips
captured by the app (Settings > Advanced > "Keep dictation clips for tuning").

Gold comes from a minimal-pair list the user dictated (each line ends in the
intended mark). Each captured clip is matched to its pair by WORDS (the pair's
final mark is stripped for matching), so retries / reordering are fine - the
distinguishing "?" vs "." is never used to match, only to score.

Modes:
  default            score the PRODUCTION text already in each sidecar (no model)
  --rescore <cfg...> also re-transcribe each WAV with alternative configs and
                     score them on the SAME clips (needs faster-whisper + GPU)

Usage (always -X utf8):
  python -X utf8 bench_qmark.py --pairs plans/qmark_experiments/minimal_pairs_he.txt
  python -X utf8 bench_qmark.py --pairs <file> --rescore ivrit_noprompt ivrit_terms

This is an offline TOOL, not part of run_tests.py. It never writes to the
clips or the config; read-only over %APPDATA%\\Lia\\debug_clips by default.
"""
import argparse
import glob
import json
import os
import re
import sys

APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
DEFAULT_CLIPS = os.path.join(APPDATA, "Lia", "debug_clips")


def norm(s):
    s = (s or "").replace("‏", "").replace("‎", "")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)   # drop punctuation
    return re.sub(r"\s+", " ", s).strip().lower()


def is_question(text):
    """Does the transcribed text mark a question? True if a '?' appears."""
    return "?" in (text or "")


def load_pairs(path):
    """Each non-comment line: an utterance ending in its intended mark.
    Returns [{match: <normalized words>, gold_q: bool, raw: str}]."""
    pairs = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        gold_q = line.rstrip().endswith("?")
        pairs.append({"match": norm(line), "gold_q": gold_q, "raw": line})
    return pairs


def build_groups(pairs):
    """A minimal pair ("זה עובד." / "זה עובד?") normalizes to the SAME words,
    so text alone cannot say which variant a clip is - that's the whole point.
    Group pairs by their word-set; within a group, the FILE ORDER (statement
    then question, as written) is matched to the clips' CAPTURE ORDER. Returns
    a list of {key: <words>, entries: [pair,...] in file order}."""
    groups = []
    index = {}
    for p in pairs:
        key = p["match"]
        if key not in index:
            index[key] = len(groups)
            groups.append({"key": key, "kset": set(key.split()), "entries": []})
        groups[index[key]]["entries"].append(p)
    return groups


def best_group(text, groups, min_match):
    """The group whose word-set best overlaps the clip text (Jaccard)."""
    ct = set(norm(text).split())
    if not ct:
        return None, 0.0
    best, score = None, 0.0
    for g in groups:
        if not g["kset"]:
            continue
        j = len(ct & g["kset"]) / len(ct | g["kset"])
        if j > score:
            best, score = g, j
    return (best, score) if score >= min_match else (None, score)


def score(rows, label):
    """rows: [(gold_q, pred_q)]. Print precision/recall/F1 for '?'."""
    tp = sum(1 for g, p in rows if g and p)
    fp = sum(1 for g, p in rows if not g and p)
    fn = sum(1 for g, p in rows if g and not p)
    tn = sum(1 for g, p in rows if not g and not p)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    false_q = fp / (fp + tn) if fp + tn else 0.0
    print(f"\n=== {label}  (n={len(rows)}) ===")
    print(f"  questions: TP={tp} FN={fn}   statements: TN={tn} FP={fp}")
    print(f"  '?' precision={prec:.0%}  recall={rec:.0%}  F1={f1:.0%}"
          f"   false-'?' on statements={false_q:.0%}")
    return {"n": len(rows), "prec": prec, "rec": rec, "f1": f1, "false_q": false_q}


# ---- optional rescoring configs (alternative pipelines on the SAME clips) ----
RESCORE_CONFIGS = {
    "ivrit_noprompt": ("ivrit-ai/whisper-large-v3-turbo-ct2", None),
    "ivrit_terms": ("ivrit-ai/whisper-large-v3-turbo-ct2",
                    "Common terms: AWS, Kubernetes, Docker, Bedrock"),
    "ivrit_hebpunct": ("ivrit-ai/whisper-large-v3-turbo-ct2",
                       "שלום, מה שלומך? הכל בסדר. אתה מגיע היום? כן."),
    "base_noprompt": ("large-v3-turbo", None),
}


def rescore(clips, names):
    import numpy as np
    import soundfile as sf
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.environ.setdefault("LIA_SKIP_LIVE", "1")
    import lia
    models = {}
    for n in names:
        size, _ = RESCORE_CONFIGS[n]
        if size not in models:
            print(f"loading {size} ...", flush=True)
            tr = lia.FasterWhisperTranscriber(model_size=size, cpu_threads=16,
                                              device="auto", compute_type=None)
            tr.load_model(callback=lambda m: None)
            models[size] = tr
    out = {n: [] for n in names}
    for c in clips:
        audio, sr = sf.read(c["wav"], dtype="float32")
        for n in names:
            size, prompt = RESCORE_CONFIGS[n]
            tr = models[size]
            with tr._infer_lock:
                segs, _ = tr.model.transcribe(
                    audio, beam_size=3, language="he", initial_prompt=prompt,
                    vad_filter=True, vad_parameters=dict(min_silence_duration_ms=500))
                text = " ".join(s.text.strip() for s in segs if s.text.strip())
            out[n].append((c["pair"]["gold_q"], is_question(text)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", required=True, help="minimal-pair list the user dictated")
    ap.add_argument("--clips", default=DEFAULT_CLIPS, help="captured clips dir")
    ap.add_argument("--min-match", type=float, default=0.34,
                    help="min word-overlap to accept a clip->pair match")
    ap.add_argument("--rescore", nargs="*", default=None,
                    help="also re-transcribe with these configs: "
                         + ", ".join(RESCORE_CONFIGS))
    args = ap.parse_args()

    pairs = load_pairs(args.pairs)
    groups = build_groups(pairs)
    print(f"pairs: {len(pairs)}  (questions {sum(p['gold_q'] for p in pairs)}, "
          f"statements {sum(not p['gold_q'] for p in pairs)})  in {len(groups)} word-groups")
    sidecars = sorted(glob.glob(os.path.join(args.clips, "dict_*.json")))
    print(f"clips: {len(sidecars)} in {args.clips}")

    # Assign each clip to a word-group, in capture order.
    unmatched = 0
    for g in groups:
        g["clips"] = []
    for jp in sidecars:
        try:
            sc = json.load(open(jp, encoding="utf-8"))
        except Exception:
            continue
        g, mscore = best_group(sc.get("text", ""), groups, args.min_match)
        if not g:
            unmatched += 1
            continue
        sc["ts"] = sc.get("ts") or os.path.basename(jp)
        sc["wav"] = os.path.join(args.clips, sc.get("wav") or (os.path.basename(jp)[:-5] + ".wav"))
        g["clips"].append(sc)

    # Resolve gold per clip. Single-variant group (WH question / trap): every
    # clip gets that gold (retries harmless). Multi-variant group (a minimal
    # pair): zip file-order entries to capture-order clips; a count mismatch is
    # AMBIGUOUS (a retry - can't know which variant) so those are excluded.
    matched, ambiguous = [], []
    for g in groups:
        clips = sorted(g["clips"], key=lambda c: c["ts"])
        ents = g["entries"]
        if len(ents) == 1:
            for c in clips:
                c["pair"] = ents[0]
                matched.append(c)
        elif len(clips) == len(ents):
            for c, e in zip(clips, ents):
                c["pair"] = e
                matched.append(c)
        else:
            ambiguous += clips
    print(f"matched {len(matched)} clips (unmatched: {unmatched}, "
          f"ambiguous minimal-pair retries: {len(ambiguous)})")
    if ambiguous:
        print("  (dictate each minimal pair exactly twice - statement then "
              "question - so counts line up; ambiguous groups are excluded)")
    if not matched:
        print("Nothing to score. Dictate the pair list with capture ON, then rerun.")
        return

    prod_rows = [(c["pair"]["gold_q"], is_question(c.get("text", ""))) for c in matched]
    score(prod_rows, "PRODUCTION (captured sidecar text)")

    # show the misses so they can be inspected
    misses = [(c["pair"]["raw"], c.get("text", "")) for c in matched
              if c["pair"]["gold_q"] and not is_question(c.get("text", ""))]
    if misses:
        print(f"\n  missed questions ({len(misses)}):")
        for gold, got in misses[:20]:
            print(f"    gold: {gold[:48]:48}  got: {got[:48]}")
    false = [(c["pair"]["raw"], c.get("text", "")) for c in matched
             if not c["pair"]["gold_q"] and is_question(c.get("text", ""))]
    if false:
        print(f"\n  false '?' on statements ({len(false)}):")
        for gold, got in false[:20]:
            print(f"    gold: {gold[:48]:48}  got: {got[:48]}")

    if args.rescore is not None:
        names = args.rescore or list(RESCORE_CONFIGS)
        bad = [n for n in names if n not in RESCORE_CONFIGS]
        if bad:
            print("unknown rescore configs:", bad); return
        res = rescore(matched, names)
        for n in names:
            score(res[n], f"RESCORE {n}")


if __name__ == "__main__":
    main()
