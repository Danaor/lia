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
  --decoder-stats    Phase-0 of plans/DECODER_THRESHOLDS_PLAN_2026-09-16.md:
                     re-transcribe every clip with the PRODUCTION decode
                     settings of a decoder profile (--profile dictation|chunk|
                     file) and report what the temperature ladder costs
                     (windows that needed a re-decode, hottest temperature),
                     the avg-logprob spread, and how many clips still carry a
                     strippable hallucinated tail at the DECODER output vs in
                     the sidecar text. --pairs is not needed in this mode.

Usage (always -X utf8):
  python -X utf8 bench_qmark.py --pairs plans/qmark_experiments/minimal_pairs_he.txt
  python -X utf8 bench_qmark.py --pairs <file> --rescore ivrit_noprompt ivrit_terms
  python -X utf8 bench_qmark.py --decoder-stats --profile dictation
  python -X utf8 bench_qmark.py --decoder-stats --profile chunk --clips <dir>

This is an offline TOOL, not part of run_tests.py. It never writes to the
clips or the config; read-only over %APPDATA%\\Lia\\debug_clips by default.
!! From a Claude session shell %APPDATA% is the MSIX-virtualized shadow - pass
   --clips %USERPROFILE%\\AppData\\Roaming\\Lia\\debug_clips explicitly.
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


def decoder_stats_mode(clips_dir, profile, model_size, limit=None, dump=None):
    """Re-transcribe every captured clip with the production decode settings
    of `profile` and print the Phase-0 numbers. Read-only over the clips;
    `dump` (optional) writes {clip: {text, fallback, max_temp, min_logprob,
    secs}} so two profiles can be diffed clip by clip."""
    import statistics
    import soundfile as sf
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.environ.setdefault("LIA_SKIP_LIVE", "1")
    import lia
    sidecars = sorted(glob.glob(os.path.join(clips_dir, "dict_*.json")))
    if limit:
        sidecars = sidecars[-int(limit):]
    print(f"clips: {len(sidecars)} in {clips_dir}   profile={profile} "
          f"-> extra kwargs {lia.decoder_kwargs(profile)}")
    if not sidecars:
        return
    print(f"loading {model_size} ...", flush=True)
    tr = lia.FasterWhisperTranscriber(model_size=model_size, cpu_threads=16,
                                      device="auto", compute_type=None)
    tr.load_model(callback=lambda m: None)
    import time as _time
    tot = {"windows": 0, "fallback": 0}
    temps, logprobs, per_clip, secs, empties = [], [], [], 0.0, 0
    tail_decoder, tail_sidecar = 0, 0
    for sc in sidecars:
        with open(sc, encoding="utf-8") as f:
            meta = json.load(f)
        wav = os.path.join(clips_dir, meta.get("wav") or os.path.basename(sc)[:-5] + ".wav")
        if not os.path.exists(wav):
            continue
        audio, sr = sf.read(wav, dtype="float32")
        prompt = None
        if meta.get("bias_ok") and tr.custom_vocabulary:
            prompt = f"Common terms: {tr.custom_vocabulary.strip()}"
        t0 = _time.time()
        with tr._infer_lock:
            segs, _ = tr.model.transcribe(
                audio, beam_size=int(os.environ.get("LIA_BENCH_BEAM", "5")),
                language="he", initial_prompt=prompt,
                condition_on_previous_text=False, vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                **lia.decoder_kwargs(profile))
            segs = list(segs)
        dt = _time.time() - t0
        secs += dt
        st = lia.decoder_stats(segs)
        tot["windows"] += st["windows"]; tot["fallback"] += st["fallback"]
        temps.append(st["max_temp"])
        if st["min_logprob"] is not None:
            logprobs.append(st["min_logprob"])
        text = " ".join(s.text.strip() for s in segs if s.text.strip())
        if not text:
            empties += 1

        def _has_tail(t):
            # Compare on the same footing: the production text carries a
            # leading RLM (the transcriber adds it) that the strip normalizes.
            t = (t or "").replace("‏", "").replace("‎", "").strip()
            return bool(t) and lia.strip_hallucinated_tail(t).strip() != t

        if _has_tail(text):
            tail_decoder += 1
        if _has_tail(meta.get("text")):
            tail_sidecar += 1
        per_clip.append((os.path.basename(wav), st["fallback"], st["max_temp"],
                         st["min_logprob"], round(dt, 2), text))
    n = len(per_clip)
    if dump:
        with open(dump, "w", encoding="utf-8") as f:
            json.dump({name: {"text": txt, "fallback": fb, "max_temp": mt,
                              "min_logprob": lp, "secs": dt}
                       for name, fb, mt, lp, dt, txt in per_clip},
                      f, ensure_ascii=False, indent=1)
        print(f"  per-clip dump -> {dump}")
    print(f"\n=== decoder stats  profile={profile}  clips={n} ===")
    print(f"  windows={tot['windows']}  needed the ladder={tot['fallback']} "
          f"({(tot['fallback'] / tot['windows'] * 100) if tot['windows'] else 0:.0f}%)")
    hist = {}
    for t in temps:
        hist[t] = hist.get(t, 0) + 1
    print("  hottest temperature per clip:", ", ".join(
        f"{k:.1f}x{v}" for k, v in sorted(hist.items())))
    if logprobs:
        q = statistics.quantiles(logprobs, n=10) if len(logprobs) >= 10 else sorted(logprobs)
        print(f"  min avg_logprob per clip: p10={q[0]:.2f} p50={statistics.median(logprobs):.2f} "
              f"p90={q[-1]:.2f}  (fallback fires below -1.00)")
    print(f"  hallucinated tail still present at the DECODER output: {tail_decoder}/{n}"
          f"   in the production sidecar text: {tail_sidecar}/{n}")
    print(f"  empty results: {empties}/{n}   total decode time: {secs:.1f}s "
          f"({secs / n if n else 0:.2f}s/clip)")
    print("\n  clips that needed the ladder:")
    for name, fb, mt, lp, dt, txt in per_clip:
        if fb:
            print(f"    {name}  fallback={fb} max_temp={mt:.1f} min_logprob={lp:.2f} "
                  f"{dt}s  {txt[:60]!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", help="minimal-pair list the user dictated")
    ap.add_argument("--clips", default=DEFAULT_CLIPS, help="captured clips dir")
    ap.add_argument("--min-match", type=float, default=0.34,
                    help="min word-overlap to accept a clip->pair match")
    ap.add_argument("--rescore", nargs="*", default=None,
                    help="also re-transcribe with these configs: "
                         + ", ".join(RESCORE_CONFIGS))
    ap.add_argument("--decoder-stats", action="store_true",
                    help="Phase-0 decoder telemetry over the clips (no --pairs needed)")
    ap.add_argument("--profile", default="dictation",
                    help="decoder profile for --decoder-stats: dictation | chunk | file")
    ap.add_argument("--model", default="ivrit-ai/whisper-large-v3-turbo-ct2",
                    help="model for --decoder-stats")
    ap.add_argument("--limit", type=int, default=None,
                    help="--decoder-stats: only the newest N clips")
    ap.add_argument("--dump", default=None,
                    help="--decoder-stats: write per-clip text/stats JSON here")
    args = ap.parse_args()

    if args.decoder_stats:
        decoder_stats_mode(args.clips, args.profile, args.model, args.limit, args.dump)
        return
    if not args.pairs:
        ap.error("--pairs is required (except with --decoder-stats)")

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
