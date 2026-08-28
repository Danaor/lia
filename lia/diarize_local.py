# -*- coding: utf-8 -*-
"""Lia — 100% LOCAL speaker diarization (pyannote.audio) subprocess.

Runs pyannote's speaker-diarization pipeline on a meeting WAV and prints the
speaker turns as JSON to stdout. It does DIARIZATION ONLY (who spoke when) — the
main app then transcribes each turn with the local ivrit.ai Hebrew Whisper
(_rerun_utterances), so the output shape matches the AssemblyAI path exactly:

    {"ok": true, "used_model": "...", "num_speakers": 3,
     "utterances": [{"speaker": "A", "start": <ms>, "end": <ms>, "text": ""}, ...]}
    {"ok": false, "error": "...", "hint": "..."}

Why a subprocess: pyannote + torch is a heavy, OPTIONAL dependency. Isolating it
here keeps the tray app importable without it, and a crash can't take the app
down. Diarization is language-AGNOSTIC (it clusters voice embeddings, not text),
so this works for Hebrew audio out of the box.

Setup (one-time, FREE):
  1) pip install "pyannote.audio>=4.0"   (the community-1 model needs 4.x)
  2) a free HuggingFace token (read) — https://huggingface.co/settings/tokens
  3) accept the model terms on its HF page (e.g. pyannote/speaker-diarization-3.1
     and pyannote/speaker-diarization-community-1)

ALWAYS launch with  python -X utf8 .  Only the JSON goes to stdout; all logs +
pyannote/torch chatter go to stderr, so the parent can json.loads(stdout) cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _log(*a):
    try:
        sys.stderr.write(" ".join(str(x) for x in a) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


_OUT_PATH = None


def _emit(obj):
    """Write the result JSON. Prefer a --out file (robust: pyannote/torch print
    to stdout/stderr, which would corrupt stdout-parsed JSON); fall back to
    stdout when no --out was given."""
    payload = json.dumps(obj, ensure_ascii=False)
    if _OUT_PATH:
        try:
            tmp = _OUT_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, _OUT_PATH)
            return
        except Exception as e:
            _log("could not write --out (%r) — using stdout" % e)
    sys.stdout.write(payload)
    sys.stdout.flush()


def _load_pipeline(model, token):
    """Load a pyannote pipeline, trying the requested model then falling back to
    the widely-available 3.1. Returns (pipeline, used_model). Raises on total
    failure with a message the parent can surface."""
    from pyannote.audio import Pipeline  # heavy import — inside the function

    # huggingface_hub reads these; belt-and-suspenders with the explicit arg.
    if token:
        os.environ.setdefault("HF_TOKEN", token)
        os.environ.setdefault("HUGGING_FACE_HUB_TOKEN", token)

    candidates = [model]
    if model != "pyannote/speaker-diarization-3.1":
        candidates.append("pyannote/speaker-diarization-3.1")  # compatible fallback

    # Offline bundle: load purely from the cache — do NOT resolve a token (which
    # would raise "token required" even for a fully-cached model). Online: use the
    # explicit token, else True to pick up a cached `hf auth login`.
    offline = os.environ.get("HF_HUB_OFFLINE") == "1"
    auth = None if offline else (token or True)
    last_err = None
    for m in candidates:
        try:
            try:
                pipe = Pipeline.from_pretrained(m, token=auth)        # 4.x
            except TypeError:      # older pyannote (3.x) uses use_auth_token
                pipe = Pipeline.from_pretrained(m, use_auth_token=auth)
            if pipe is None:
                raise RuntimeError(
                    "from_pretrained returned None (model terms not accepted, "
                    "or invalid/again-gated token)")
            if m != model:
                _log("falling back to", m)
            return pipe, m
        except Exception as e:
            last_err = e
            _log("load failed for %s: %r" % (m, e))
    raise last_err if last_err else RuntimeError("no pipeline")


def _to_cuda(pipe, device):
    if device == "cpu":
        return "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            pipe.to(torch.device("cuda"))
            return "cuda"
    except Exception as e:
        _log("CUDA move failed (%r) — using CPU" % e)
    return "cpu"


def _load_audio(path):
    """Read audio into a pyannote waveform dict {'waveform': (C,T) float32 tensor,
    'sample_rate': sr}, BYPASSING torchcodec/ffmpeg — which is fragile on Windows
    (community-1 on pyannote 4.x otherwise fails with 'torchcodec is not available').
    Stdlib `wave` covers the app's 16 kHz PCM WAVs. Returns (dict_or_None, err);
    None => let pyannote try the path itself (non-WAV inputs)."""
    try:
        import wave as _wave
        import numpy as _np
        import torch as _torch
        with _wave.open(path, "rb") as wf:
            sr, ch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        if sw == 2:
            a = _np.frombuffer(raw, dtype=_np.int16).astype(_np.float32) / 32768.0
        elif sw == 4:
            a = _np.frombuffer(raw, dtype=_np.int32).astype(_np.float32) / 2147483648.0
        elif sw == 1:
            a = (_np.frombuffer(raw, dtype=_np.uint8).astype(_np.float32) - 128.0) / 128.0
        else:
            return None, "unsupported sample width %d" % sw
        a = a.reshape(-1, ch).T if ch > 1 else a.reshape(1, -1)   # (channel, time)
        return {"waveform": _torch.from_numpy(_np.ascontiguousarray(a)),
                "sample_rate": sr}, None
    except Exception as e:
        return None, str(e)


def _collect_turns(diarization):
    """pyannote Annotation -> ordered [(start_s, end_s, raw_speaker)]."""
    turns = []
    for segment, _track, speaker in diarization.itertracks(yield_label=True):
        turns.append((float(segment.start), float(segment.end), str(speaker)))
    turns.sort(key=lambda t: t[0])
    return turns


def _smooth(turns, merge_gap=0.4, min_dur=0.2):
    """Merge consecutive same-speaker turns separated by a tiny gap, and drop
    ultra-short blips — pyannote over-fragments otherwise."""
    out = []
    for s, e, spk in turns:
        if out and out[-1][2] == spk and s - out[-1][1] <= merge_gap:
            out[-1] = (out[-1][0], max(out[-1][1], e), spk)
        else:
            out.append((s, e, spk))
    return [(s, e, spk) for (s, e, spk) in out if (e - s) >= min_dur]


def _label_map(turns):
    """Map pyannote's SPEAKER_00/01/... to A/B/C… in order of first appearance."""
    order, mapping = [], {}
    for _s, _e, spk in turns:
        if spk not in mapping:
            mapping[spk] = _idx_to_alpha(len(order))
            order.append(spk)
    return mapping


def _idx_to_alpha(i):
    # 0->A .. 25->Z, 26->AA … (rarely more than a handful of speakers)
    s = ""
    i += 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def main():
    # Windowless-spawn safety: stdout/stderr may be None under pythonw.
    if sys.stdout is None or sys.stderr is None:
        devnull = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = devnull
        if sys.stderr is None:
            sys.stderr = devnull

    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="?")              # optional in --download mode
    ap.add_argument("--out", default="")           # write result JSON here (robust)
    ap.add_argument("--model", default="pyannote/speaker-diarization-community-1")
    ap.add_argument("--hf-token", default="")
    ap.add_argument("--device", default="auto")   # auto|cpu
    ap.add_argument("--min-speakers", type=int, default=0)
    ap.add_argument("--max-speakers", type=int, default=0)
    # Offline BUNDLE support (for shipping in a commercial product): --models-dir
    # is a self-contained HF cache (an HF_HOME). At runtime we load from it with
    # NO network and NO token. --download populates it once (needs a token), so a
    # developer bundles the weights and end-users never touch HuggingFace.
    ap.add_argument("--models-dir", default="")
    ap.add_argument("--download", action="store_true")
    args = ap.parse_args()

    global _OUT_PATH
    _OUT_PATH = args.out or None

    # Point HF at the bundle BEFORE importing pyannote/huggingface_hub. In normal
    # (runtime) use with a bundle, force OFFLINE so it never phones home / needs a
    # token; in --download use, stay online so we can fetch the weights into it.
    if args.models_dir:
        os.environ["HF_HOME"] = args.models_dir
        os.environ["HUGGINGFACE_HUB_CACHE"] = os.path.join(args.models_dir, "hub")
        if not args.download:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"

    if not args.download:
        if not args.wav or not os.path.exists(args.wav):
            _emit({"ok": False, "error": "audio file not found: %s" % (args.wav or "")})
            return

    try:
        import pyannote.audio  # noqa: F401
    except Exception:
        _emit({"ok": False,
               "error": "pyannote.audio is not installed",
               "hint": 'pip install "pyannote.audio>=4.0"  (community-1 needs 4.x)'})
        return

    token = args.hf_token or os.environ.get("HF_TOKEN", "") \
        or os.environ.get("HUGGING_FACE_HUB_TOKEN", "")

    try:
        pipe, used_model = _load_pipeline(args.model, token)
    except Exception as e:
        offline = bool(args.models_dir) and not args.download
        _emit({"ok": False,
               "error": "could not load the diarization model: %s" % e,
               "hint": ("Bundle at %s is missing/incomplete — re-run --download"
                        % args.models_dir) if offline else
                       ("Set a free HuggingFace token and accept the model terms "
                        "at https://huggingface.co/%s" % args.model)})
        return

    # --download: we only needed to fetch the weights into the bundle dir. Done.
    if args.download:
        _emit({"ok": True, "downloaded": True, "used_model": used_model,
               "models_dir": args.models_dir})
        return

    device = _to_cuda(pipe, args.device)
    _log("running diarization on", device, "model", used_model)

    kw = {}
    if args.min_speakers > 0:
        kw["min_speakers"] = args.min_speakers
    if args.max_speakers > 0:
        kw["max_speakers"] = args.max_speakers

    # Read the WAV ourselves (torchcodec-free) and hand pyannote a waveform dict.
    audio_input, load_err = _load_audio(args.wav)
    if audio_input is None:
        _log("wave read fell back to path (%s) — pyannote will decode it" % load_err)
    # Speaker naming (2026-08-27): ask for per-cluster EMBEDDINGS too - the
    # voiceprints the cross-meeting speaker-profile store learns/matches on.
    # community-1 (pyannote 4.x) returns them as DiarizeOutput.speaker_embeddings
    # (verified live: (n_speakers, 256) ndarray aligned with labels()); 3.1
    # returns an (annotation, embeddings) tuple. Never fail diarization for them.
    src = audio_input if audio_input is not None else args.wav
    raw_embeddings = None
    try:
        output = pipe(src, return_embeddings=True, **kw)
    except TypeError:
        try:
            output = pipe(src, **kw)
        except Exception as e:
            extra = (" [wav read failed: %s]" % load_err) if load_err else ""
            _emit({"ok": False, "error": "diarization failed: %s%s" % (e, extra)})
            return
    except Exception as e:
        extra = (" [wav read failed: %s]" % load_err) if load_err else ""
        _emit({"ok": False, "error": "diarization failed: %s%s" % (e, extra)})
        return
    if isinstance(output, tuple) and len(output) == 2:       # pyannote 3.1 form
        output, raw_embeddings = output

    # community-1 (pyannote 4.x) returns an object exposing an EXCLUSIVE view
    # (only one speaker active at any instant). That's ideal here — we transcribe
    # each turn separately, so non-overlapping segments mean no double-counted
    # audio and cleaner timestamp alignment. pyannote 3.1 returns a plain
    # Annotation (no exclusive view) — fall back to it.
    annotation = getattr(output, "exclusive_speaker_diarization", None)
    used_exclusive = annotation is not None
    if annotation is None:
        annotation = getattr(output, "speaker_diarization", output)

    turns = _smooth(_collect_turns(annotation))
    mapping = _label_map(turns)
    utterances = [{
        "speaker": mapping[spk],
        "start": round(s * 1000.0, 1),   # ms, matching AssemblyAI utterances
        "end": round(e * 1000.0, 1),
        "text": "",
    } for (s, e, spk) in turns]

    # Per-cluster voiceprints keyed by the MAPPED label (A/B/...), rows aligned
    # with the base annotation's labels() order. Best-effort by contract.
    embeddings = {}
    try:
        if raw_embeddings is None:
            raw_embeddings = getattr(output, "speaker_embeddings", None)
        if raw_embeddings is not None:
            base_ann = getattr(output, "speaker_diarization", None) or annotation
            raw_labels = list(base_ann.labels())
            for i, raw_lab in enumerate(raw_labels):
                lab = mapping.get(raw_lab)
                if lab is None or i >= len(raw_embeddings):
                    continue
                row = [float(x) for x in raw_embeddings[i]]
                # ALL-finite only: a low-speech cluster can come back partially
                # NaN, and one NaN poisons every cosine downstream.
                if all(x == x for x in row):
                    embeddings[lab] = row
    except Exception as e:
        _log("speaker embeddings skipped: %s" % e)

    _emit({
        "ok": True,
        "used_model": used_model,
        "device": device,
        "exclusive": used_exclusive,
        "num_speakers": len(mapping),
        "utterances": utterances,
        "speaker_embeddings": embeddings,
    })


if __name__ == "__main__":
    main()
