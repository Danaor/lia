# -*- coding: utf-8 -*-
"""Lia - local speaker detection with NVIDIA Nemotron 3 Diarization (subprocess).

Finds WHO spoke WHEN in a meeting WAV and writes the speaker turns as JSON. It
does no transcription: the app then transcribes each turn with its own local
model (_rerun_utterances). Output (the --out file, else stdout):

    {"ok": true, "used_model": "...", "device": "cpu", "num_speakers": 3,
     "utterances": [{"speaker": "A", "start": <ms>, "end": <ms>, "text": ""}, ...],
     "speaker_embeddings": {}}
    {"ok": false, "error": "...", "hint": "..."}

Why a subprocess: torch + transformers are big and OPTIONAL. They live in their
own folder (--pkgs, installed on demand from speakers_requirements.lock), never in
the app's runtime, so the tray app stays importable without them and a crash
here cannot take it down.

The model (measured on 7 real meetings, 2026-11-06, plans/nemotron_bench_2026-11-06):
at most 8 speakers; labels in arrival order, kept for the whole recording by a
speaker cache handed from one step to the next. Driven as a STREAMING session
with the 30 s preset (the non-streaming call runs the embedder over the whole
file at once). CPU, 8 threads: about 33 s per hour of audio.

ALWAYS launch with  python -X utf8 .
"""

import os
import sys


def _isolate_sys_path(pkgs):
    """Put the speaker-detection packages first and drop every other
    site-packages entry, so the app runtime's own numpy / huggingface_hub
    (other versions) can never mix in. The lock lists every dependency."""
    keep = []
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for p in sys.path:
        if not p:
            continue
        low = os.path.normcase(os.path.abspath(p))
        if "site-packages" in low or low == os.path.normcase(script_dir):
            continue
        keep.append(p)
    sys.path[:] = ([pkgs] if pkgs else []) + keep


MODEL_ID = "nvidia/Nemotron-3-Diarization"
# The 30.4 s preset: step 340 x 80 ms, look-ahead 40, with its own cache sizes.
STEP_FRAMES, LOOKAHEAD_FRAMES = 340, 40
FIFO_LEN, CACHE_UPDATE = 40, 300
FRAME_S = 0.01            # the model gives one row of 8 probabilities per 10 ms
ACTIVE = 0.5


def _log(*a):
    try:
        sys.stderr.write(" ".join(str(x) for x in a) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


_OUT_PATH = None


def _emit(obj):
    import json
    payload = json.dumps(obj, ensure_ascii=False)
    if _OUT_PATH:
        try:
            tmp = _OUT_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, _OUT_PATH)
            return
        except Exception as e:
            _log("could not write --out (%r) - using stdout" % e)
    sys.stdout.write(payload)
    sys.stdout.flush()


def _load_wav(path):
    """16 kHz mono float32 in [-1, 1] from a PCM WAV (stdlib wave + numpy).
    Several channels are averaged; another rate is resampled linearly."""
    import wave
    import numpy as np
    with wave.open(path, "rb") as wf:
        sr, ch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if sw == 2:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError("unsupported sample width %d" % sw)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != 16000 and a.size:
        n = int(round(a.size * 16000.0 / sr))
        a = np.interp(np.linspace(0, a.size - 1, n), np.arange(a.size), a).astype(np.float32)
    return np.ascontiguousarray(a, dtype=np.float32)


def _probabilities(model_dir, audio, threads):
    """Run one streaming session over the whole recording. Returns (T, 8)
    sigmoid probabilities, one row per 10 ms from the start of the audio."""
    import numpy as np
    import torch
    from transformers import AutoModelForAudioFrameClassification, AutoProcessor
    from transformers.models.nemotron3_diarization.modeling_nemotron3_diarization import (
        Nemotron3DiarizationSpeakerCache,
    )
    torch.set_num_threads(max(1, int(threads)))
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    processor = AutoProcessor.from_pretrained(model_dir, local_files_only=True)
    model = AutoModelForAudioFrameClassification.from_pretrained(
        model_dir, dtype=torch.float32, local_files_only=True).eval()
    processor.streaming_modes = dict(processor.streaming_modes,
                                     lia_offline=(STEP_FRAMES, LOOKAHEAD_FRAMES))
    processor.set_streaming_mode("lia_offline")
    sr = processor.feature_extractor.sampling_rate
    first = processor.num_samples_first_audio_chunk
    if audio.shape[0] < first:          # a very short file: pad with silence
        audio = np.concatenate([audio, np.zeros(first - audio.shape[0], np.float32)])

    def chunks():
        yield processor(audio[:first], sampling_rate=sr, is_streaming=True,
                        is_first_audio_chunk=True)
        mel_idx = processor.num_mel_frames_per_step
        start = processor.audio_chunk_start(mel_idx)
        while (end := start + processor.num_samples_per_audio_chunk) <= audio.shape[0]:
            yield processor(audio[start:end], sampling_rate=sr, is_streaming=True,
                            is_first_audio_chunk=False)
            mel_idx += processor.num_mel_frames_per_step
            start = processor.audio_chunk_start(mel_idx)
        yield processor(audio[start:], sampling_rate=sr, is_streaming=True,
                        is_first_audio_chunk=False, is_last_audio_chunk=True)

    cache = Nemotron3DiarizationSpeakerCache(
        model.config.streaming_config, fifo_length=FIFO_LEN,
        speaker_cache_update_period=CACHE_UPDATE)
    rows = []
    with torch.inference_mode():
        for inputs in chunks():
            out = model(**inputs, speaker_cache=cache)
            cache = out.speaker_cache
            rows.append(out.logits.sigmoid()[0].float().numpy())
    return np.concatenate(rows, 0) if rows else np.zeros((0, 8), np.float32)


def turns_from_probabilities(probs, frame_s=FRAME_S, active=ACTIVE):
    """(T, n) probabilities -> ordered [(start_s, end_s, speaker_index)].
    One speaker per instant (the most probable of the active ones): the app
    transcribes each turn on its own, so overlapping turns would transcribe the
    same audio twice. Pure numpy, no torch (unit-tested in run_tests.py)."""
    import numpy as np
    probs = np.asarray(probs, dtype=np.float32)
    if probs.ndim != 2 or probs.shape[0] == 0:
        return []
    best = probs.argmax(axis=1)
    on = probs.max(axis=1) > active
    spk = np.where(on, best, -1)
    turns = []
    start = 0
    for i in range(1, spk.shape[0] + 1):
        if i == spk.shape[0] or spk[i] != spk[start]:
            if spk[start] >= 0:
                turns.append((start * frame_s, i * frame_s, int(spk[start])))
            start = i
    return turns


def smooth(turns, merge_gap=0.4, min_dur=0.2):
    """Merge consecutive same-speaker turns across a tiny gap, then drop blips."""
    out = []
    for s, e, spk in turns:
        if out and out[-1][2] == spk and s - out[-1][1] <= merge_gap:
            out[-1] = (out[-1][0], max(out[-1][1], e), spk)
        else:
            out.append((s, e, spk))
    return [(s, e, spk) for (s, e, spk) in out if (e - s) >= min_dur]


def _alpha(i):
    s, i = "", i + 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def utterances_from_turns(turns):
    """[(s, e, idx)] -> AssemblyAI-shaped utterances, labels A, B, C... in
    order of first appearance, start/end in milliseconds."""
    mapping = {}
    out = []
    for s, e, spk in turns:
        if spk not in mapping:
            mapping[spk] = _alpha(len(mapping))
        out.append({"speaker": mapping[spk], "start": round(s * 1000.0, 1),
                    "end": round(e * 1000.0, 1), "text": ""})
    return out, len(mapping)


def runs_from_rows(rows, t0, frame_s=FRAME_S, active=ACTIVE):
    """One step's (frames, 8) probabilities starting at meeting second t0 ->
    [[start_s, end_s, speaker_index], ...] (exclusive, unsmoothed)."""
    return [[round(t0 + s, 3), round(t0 + e, 3), k]
            for s, e, k in turns_from_probabilities(rows, frame_s, active)]


def live_main(args):
    """--live: one streaming session fed from stdin for a whole meeting.
    stdin: frames of <uint32 little-endian byte count><int16 mono 16 kHz PCM>;
    a count of 0 (or EOF) ends the meeting and flushes the last step.
    stdout: one JSON line per step: {"covered": seconds labelled so far,
    "runs": [[t0, t1, speaker], ...]}, first {"ready": true}. The step is
    `--step` frames of 80 ms with no look-ahead (measured 2026-11-06: no
    look-ahead scores as well as 1.04 s of it), so a step's labels are out as
    soon as its audio is in."""
    import json
    import struct
    import numpy as np
    import torch
    from transformers import AutoModelForAudioFrameClassification, AutoProcessor
    torch.set_num_threads(max(1, int(args.threads or 4)))
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    out = sys.stdout

    def say(obj):
        out.write(json.dumps(obj) + "\n")
        out.flush()

    processor = AutoProcessor.from_pretrained(args.model_dir, local_files_only=True)
    model = AutoModelForAudioFrameClassification.from_pretrained(
        args.model_dir, dtype=torch.float32, local_files_only=True).eval()
    step = max(1, int(args.step))
    processor.streaming_modes = dict(processor.streaming_modes, lia_live=(step, 0))
    processor.set_streaming_mode("lia_live")
    sr = processor.feature_extractor.sampling_rate
    say({"ready": True, "step_s": round(step * 0.08, 3)})
    src = sys.stdin.buffer
    buf = np.zeros(0, np.float32)     # audio from absolute sample `base` on
    base = 0
    total = 0                          # samples received
    first = True
    mel_idx = 0
    start = 0
    cache = None
    covered = 0.0

    def run(inputs):
        nonlocal cache, covered
        with torch.inference_mode():
            o = model(**inputs, speaker_cache=cache)
        cache = o.speaker_cache
        rows = o.logits.sigmoid()[0].float().numpy()
        t0 = covered
        covered = round(covered + rows.shape[0] * FRAME_S, 3)
        say({"covered": covered, "runs": runs_from_rows(rows, t0)})

    ended = False
    while not ended:
        head = src.read(4)
        n = struct.unpack("<I", head)[0] if len(head) == 4 else 0
        if n == 0:
            ended = True
        else:
            raw = src.read(n)
            a = np.frombuffer(raw[: len(raw) // 2 * 2], np.int16).astype(np.float32) / 32768.0
            buf = np.concatenate([buf, a])
            total += a.shape[0]
        if first:
            need = processor.num_samples_first_audio_chunk
            if total >= need or (ended and total > 0):
                chunk = buf[:need]
                if chunk.shape[0] < need:
                    chunk = np.concatenate([chunk, np.zeros(need - chunk.shape[0], np.float32)])
                run(processor(chunk, sampling_rate=sr, is_streaming=True,
                              is_first_audio_chunk=True))
                first = False
                mel_idx = processor.num_mel_frames_per_step
                start = processor.audio_chunk_start(mel_idx)
        while not first and start + processor.num_samples_per_audio_chunk <= total:
            end = start + processor.num_samples_per_audio_chunk
            run(processor(buf[start - base:end - base], sampling_rate=sr,
                          is_streaming=True, is_first_audio_chunk=False))
            mel_idx += processor.num_mel_frames_per_step
            start = processor.audio_chunk_start(mel_idx)
            if start - base > sr * 30:          # drop audio no step reads again
                cut = start - base - sr * 5
                buf, base = buf[cut:], base + cut
        if ended and not first and start < total:
            run(processor(buf[start - base:], sampling_rate=sr, is_streaming=True,
                          is_first_audio_chunk=False, is_last_audio_chunk=True))
    say({"done": True, "covered": covered, "audio_s": round(total / 16000.0, 3)})


def main():
    global _OUT_PATH
    if sys.stdout is None or sys.stderr is None:      # windowless spawn
        devnull = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = devnull
        if sys.stderr is None:
            sys.stderr = devnull
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="?", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--pkgs", required=True)
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--step", type=int, default=38)   # 80 ms frames: 38 = 3.04 s
    args = ap.parse_args()
    _OUT_PATH = args.out or None
    _isolate_sys_path(args.pkgs)
    if args.live:
        try:
            live_main(args)
        except Exception as e:
            try:
                import json
                sys.stdout.write(json.dumps({"error": str(e)[:300]}) + "\n")
                sys.stdout.flush()
            except Exception:
                pass
        return
    threads = args.threads or max(1, min(8, (os.cpu_count() or 2) // 2))
    try:
        import numpy  # noqa: F401
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except Exception as e:
        _emit({"ok": False, "error": "speaker detection is not installed (%s)" % e,
               "hint": "Settings > Meetings > Install speaker detection"})
        return
    try:
        audio = _load_wav(args.wav)
    except Exception as e:
        _emit({"ok": False, "error": "could not read the recording: %s" % e})
        return
    import time
    t0 = time.perf_counter()
    try:
        probs = _probabilities(args.model_dir, audio, threads)
    except Exception as e:
        _emit({"ok": False, "error": "speaker detection failed: %s" % e,
               "hint": "Settings > Meetings > Install speaker detection"})
        return
    wall = time.perf_counter() - t0
    utts, n = utterances_from_turns(smooth(turns_from_probabilities(probs)))
    dur = audio.shape[0] / 16000.0
    _log("nemotron: %.0f s audio in %.1f s (RTF %.3f, %d threads), %d speakers, %d turns"
         % (dur, wall, wall / dur if dur else 0, threads, n, len(utts)))
    _emit({"ok": True, "used_model": MODEL_ID, "device": "cpu", "threads": threads,
           "audio_s": round(dur, 2), "wall_s": round(wall, 2),
           "num_speakers": n, "utterances": utts, "speaker_embeddings": {}})


if __name__ == "__main__":
    main()
