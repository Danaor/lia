"""Speaker-identification bench for the LOCAL diarization path (WP0 of
plans/SPEAKER_ID_IMPROVEMENT_PLAN_2026-09-17.md).

For every meeting that kept its raw per-channel tracks (`<stem>_meeting_mic.wav`
+ `<stem>_meeting_system.wav`, the S49 rule) this tool:
  1. mixes the two tracks and runs diarize_local.py (pyannote community-1),
  2. rebuilds the 1 s mic/loopback RMS bins exactly as the app records them,
  3. runs pick_self_speaker (+ sharpen_self_turns) and scores every cluster
     against the CHANNEL ground truth (|log(mic/loop)| > 0.7 = decisive side),
  4. prints per-cluster embeddings' cosine matrix ACROSS meetings and what
     speaker_profiles.match / suggest would decide against the current store.

Read-only over the meetings dir and the profile store. Needs pyannote.audio +
the offline bundle (lia/diarization_models). Usage (always -X utf8):
  python -X utf8 bench_speakers.py                       # all meetings with raw tracks
  python -X utf8 bench_speakers.py --stems 2026-09-14_11-19-11 2026-09-16_15-07-37
  python -X utf8 bench_speakers.py --no-sharpen          # the pre-WP6 numbers
!! From a Claude session shell pass --meetings %USERPROFILE%\\AppData\\Roaming\\Lia\\meetings
   explicitly (%APPDATA% is the MSIX shadow there).
"""
import argparse
import itertools
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("LIA_SKIP_LIVE", "1")

APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
DEFAULT_MEETINGS = os.path.join(APPDATA, "Lia", "meetings")
SR = 16000


def read_wav(path):
    with wave.open(path, "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1, (path, w.getframerate())
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def rms_bins(a):
    return [float(np.sqrt(np.mean(a[i:i + SR] ** 2))) for i in range(0, len(a), SR)]


def stems_with_tracks(meetings_dir):
    names = os.listdir(meetings_dir)
    mics = {f[:-len("_meeting_mic.wav")] for f in names if f.endswith("_meeting_mic.wav")}
    syss = {f[:-len("_meeting_system.wav")] for f in names if f.endswith("_meeting_system.wav")}
    return sorted(mics & syss)


def diarize(mix_path, models_dir):
    import lia  # noqa: F401  (path + env set up above)
    out = mix_path + ".json"
    args = [sys.executable, "-X", "utf8",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "diarize_local.py"),
            mix_path, "--out", out, "--model", "pyannote/speaker-diarization-community-1",
            "--device", "auto", "--models-dir", models_dir]
    t0 = time.time()
    subprocess.run(args, capture_output=True, timeout=3600)
    return json.load(open(out, encoding="utf-8")), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meetings", default=DEFAULT_MEETINGS)
    ap.add_argument("--stems", nargs="*", default=None)
    ap.add_argument("--models-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "diarization_models"))
    ap.add_argument("--no-sharpen", action="store_true")
    args = ap.parse_args()
    import lia
    import speaker_profiles

    stems = args.stems or stems_with_tracks(args.meetings)
    print(f"meetings with raw tracks: {len(stems)}  in {args.meetings}")
    E = {}          # "stem/label[=SELF]" -> embedding
    for stem in stems:
        try:
            mic = read_wav(os.path.join(args.meetings, stem + "_meeting_mic.wav"))
            sy = read_wav(os.path.join(args.meetings, stem + "_meeting_system.wav"))
        except Exception as e:
            print(f"\n=== {stem}: skipped ({e})")
            continue
        n = min(len(mic), len(sy)); mic, sy = mic[:n], sy[:n]
        mix = np.clip((mic + sy) * 0.7, -1, 1)
        tmp = os.path.join(tempfile.gettempdir(), stem + "_bench_mix.wav")
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes((mix * 32767).astype("<i2").tobytes())
        d, dt = diarize(tmp, args.models_dir)
        utts = d.get("utterances") or []
        mb, lb = rms_bins(mic), rms_bins(sy)
        lab, gap = lia.pick_self_speaker(utts, mb, lb)
        sharp = (0, 0)
        if lab and not args.no_sharpen:
            sharp = lia.sharpen_self_turns(utts, lab, mb, lb)
        truth = {}
        for i, (m, l) in enumerate(zip(mb, lb)):
            if max(m, l) < 0.005:
                continue
            r = math.log((m + 1e-9) / (l + 1e-9))
            if r > 0.7:
                truth[i] = "me"
            elif r < -0.7:
                truth[i] = "other"
        per, talk = {}, {}
        for u in utts:
            talk[u["speaker"]] = talk.get(u["speaker"], 0) + (u["end"] - u["start"]) / 1000
            for i in range(int(u["start"] // 1000), int(math.ceil(u["end"] / 1000))):
                if i in truth:
                    per.setdefault(u["speaker"], []).append(truth[i])
        print(f"\n=== {stem}: {n / SR / 60:.1f} min | diarize {dt:.0f}s ({n / SR / dt:.0f}x RT) | "
              f"speakers={d.get('num_speakers')} turns={len(utts)} exclusive={d.get('exclusive')}")
        print("  talk s/speaker:", {k: round(v) for k, v in sorted(talk.items(), key=lambda kv: -kv[1])})
        print(f"  self: {lab!r} (margin {gap:.2f})  sharpened: {sharp[0]} -> self, {sharp[1]} -> other  "
              f"| truth s: me={sum(1 for v in truth.values() if v == 'me')} other={sum(1 for v in truth.values() if v == 'other')}")
        for k, v in sorted(per.items(), key=lambda kv: -len(kv[1])):
            me = sum(1 for x in v if x == "me")
            print(f"  cluster {k}{' (self)' if k == lab else ''}: {len(v)}s judged, {me / len(v):.0%} 'me' by channel")
        for k, v in (d.get("speaker_embeddings") or {}).items():
            E[f"{stem}/{k}" + ("=SELF" if k == lab else "")] = np.asarray(v, dtype=np.float64)
        emb = d.get("speaker_embeddings") or {}
        if emb:
            print("  store.match  ->", speaker_profiles.match(emb) or "{}")
            print("  store.suggest->", speaker_profiles.suggest(emb) or "{}")
    if len(E) >= 2:
        cos = lambda a, b: float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
        print("\n=== cross-meeting cosine (SELF vs SELF should be ~0.9, different people <= 0.4) ===")
        for a, b in itertools.combinations(sorted(E), 2):
            if a.split("/")[0] != b.split("/")[0]:
                print(f"  cos({a}, {b}) = {cos(E[a], E[b]):.2f}")


if __name__ == "__main__":
    main()
