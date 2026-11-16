# -*- coding: utf-8 -*-
"""Lia - the voice bank: a voice named in one meeting is named again in the next.

When a live speaker label gets a real name (typed in the live window, a Teams
caption vote, or the user's own voice), the meeting's audio for that label is
turned into a voiceprint (a 256-number speaker embedding) and saved LOCALLY in
%APPDATA%/Lia/voice_bank.json. In a later meeting, a live label whose voice
matches a saved print gets that name (source "bank").

The embedding model is WeSpeaker ResNet34-LM (VoxCeleb, CC-BY-4.0, 26.5 MB
ONNX), run with the onnxruntime Lia already ships (no torch, no new package).
Downloaded on first use next to the speaker-detection pack, sha256-checked.
The features are Kaldi log-mel filterbanks (80 bins, 25 ms / 10 ms, hamming,
pre-emphasis 0.97) in numpy, the same as WeSpeaker computes them.

Measured on Naor's real meetings (plans/VOICE_BANK_PLAN_2026-11-09.md):
the thresholds below come from those numbers, not from the model card.

Stdlib + numpy (+ onnxruntime when embedding). Every function is safe to call
when the model is missing: it returns None and the meeting goes on.
"""

import hashlib
import json
import math
import os
import threading
import time

MODEL_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
MODEL_REV = "f0c48c298fd835726c27956a5d617bad7115627e"
MODEL_FILE = "voxceleb_resnet34_LM.onnx"
MODEL_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"
MODEL_SIZE = 26530309
MODEL_URL = "https://huggingface.co/%s/resolve/%s/%s" % (MODEL_REPO, MODEL_REV, MODEL_FILE)

SR = 16000
N_FFT = 512
WIN = 400
HOP = 160
N_MELS = 80

# ---- thresholds (set from the measurements, see the plan) ----
MATCH_MIN = 0.60          # cosine to the best saved voice to name a label
MATCH_MARGIN = 0.10       # ... and that far above the second best voice
MIN_MATCH_SPEECH_S = 8.0  # a label's speech before it is matched at all
MIN_SAVE_SPEECH_S = 20.0  # a label's speech before its print is saved
MAX_SEGMENT_S = 20.0      # long turns are embedded in pieces of this size
MIN_SEGMENT_S = 1.5       # shorter turn pieces are not embedded
MAX_PRINTS_PER_VOICE = 8  # one print per meeting, the newest kept


# ---------------------------------------------------------------- features
_MEL = None


def _mel_banks():
    """Kaldi mel filterbank (257 x 80), low 20 Hz, high = Nyquist."""
    global _MEL
    if _MEL is not None:
        return _MEL
    import numpy as np

    def mel(f):
        return 1127.0 * np.log(1.0 + f / 700.0)

    lo, hi = mel(20.0), mel(SR / 2.0)
    delta = (hi - lo) / (N_MELS + 1)
    bins = mel(np.arange(N_FFT // 2) * (SR / N_FFT))
    w = np.zeros((N_FFT // 2 + 1, N_MELS), np.float32)
    for m in range(N_MELS):
        left, center, right = lo + m * delta, lo + (m + 1) * delta, lo + (m + 2) * delta
        up = (bins - left) / (center - left)
        down = (right - bins) / (right - center)
        w[: N_FFT // 2, m] = np.maximum(0.0, np.minimum(up, down))
    _MEL = w
    return w


def fbank(audio):
    """16 kHz mono float audio -> (frames, 80) log-mel features with the
    per-bin mean removed (what the WeSpeaker model reads). None when the
    audio is shorter than one 25 ms frame."""
    import numpy as np
    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    if a.shape[0] < WIN:
        return None
    a = a * 32768.0
    frames = np.lib.stride_tricks.sliding_window_view(a, WIN)[::HOP].astype(np.float32)
    frames = frames - frames.mean(axis=1, keepdims=True)
    prev = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    frames = (frames - 0.97 * prev) * np.hamming(WIN).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames, N_FFT)).astype(np.float32) ** 2
    feats = np.log(np.maximum(spec @ _mel_banks(), np.finfo(np.float32).eps))
    return feats - feats.mean(axis=0, keepdims=True)


# ---------------------------------------------------------------- model
def model_dir(root=None):
    """Where the model lives: next to the speaker-detection pack."""
    if root:
        return root
    env = os.environ.get("LIA_SPEAKERS_DIR")
    if env:
        return os.path.join(env, "voiceprint")
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Lia", "speakers", "voiceprint")


def model_path(root=None):
    return os.path.join(model_dir(root), MODEL_FILE)


def model_ready(root=None):
    p = model_path(root)
    try:
        return os.path.getsize(p) == MODEL_SIZE
    except OSError:
        return False


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_model(root=None, timeout=60, log=None):
    """Fetch the ONNX file once (26.5 MB), check its sha256, then move it in
    place. True when the model is ready."""
    if model_ready(root):
        return True
    log = log or (lambda *a: None)
    import requests
    d = model_dir(root)
    os.makedirs(d, exist_ok=True)
    tmp = model_path(root) + ".part"
    try:
        with requests.get(MODEL_URL, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for block in r.iter_content(1 << 20):
                    f.write(block)
        got = _sha256(tmp)
        if got != MODEL_SHA256:
            log("Voice bank: model checksum mismatch (%s) - not used" % got[:12])
            os.remove(tmp)
            return False
        os.replace(tmp, model_path(root))
        log("Voice bank: voiceprint model ready (%d bytes)" % os.path.getsize(model_path(root)))
        return True
    except Exception as e:
        log("Voice bank: model download failed: %s" % e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


class Embedder:
    """One onnxruntime session (lazy, thread-safe). embed(audio) -> unit
    vector (256,) or None."""

    def __init__(self, path=None, threads=2):
        self.path = path or model_path()
        self.threads = int(threads)
        self._sess = None
        self._lock = threading.Lock()
        self.error = ""

    def _session(self):
        if self._sess is None:
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = max(1, self.threads)
            so.inter_op_num_threads = 1
            so.log_severity_level = 3
            self._sess = ort.InferenceSession(self.path, sess_options=so,
                                              providers=["CPUExecutionProvider"])
        return self._sess

    def embed(self, audio):
        import numpy as np
        f = fbank(audio)
        if f is None or f.shape[0] < 50:
            return None
        try:
            with self._lock:
                (e,) = self._session().run(["embs"], {"feats": f[None].astype(np.float32)})
        except Exception as ex:
            self.error = str(ex)[:200]
            return None
        return unit(e[0])


def unit(v):
    import numpy as np
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def cosine(a, b):
    import numpy as np
    return float(np.dot(unit(a), unit(b)))


def pieces(t0, t1, max_s=MAX_SEGMENT_S, min_s=MIN_SEGMENT_S):
    """Split [t0, t1] into embeddable pieces of at most max_s (the last one is
    dropped when shorter than min_s)."""
    out = []
    t = t0
    while t1 - t >= min_s:
        e = min(t1, t + max_s)
        if t1 - e < min_s:
            e = t1
        out.append((t, e))
        t = e
    return out


class LabelPrints:
    """Per live label: a duration-weighted running mean of the embeddings of
    its turns (one meeting). The caller feeds (label, audio) pieces."""

    def __init__(self):
        self._sum = {}
        self._sec = {}
        self._lock = threading.Lock()

    def add(self, label, emb, seconds):
        import numpy as np
        if emb is None or seconds <= 0:
            return
        with self._lock:
            v = np.asarray(emb, np.float32) * float(seconds)
            self._sum[label] = self._sum.get(label, 0) + v
            self._sec[label] = self._sec.get(label, 0.0) + float(seconds)

    def seconds(self, label):
        with self._lock:
            return self._sec.get(label, 0.0)

    def print_of(self, label):
        with self._lock:
            s = self._sum.get(label)
        return None if s is None else unit(s)

    def labels(self):
        with self._lock:
            return sorted(self._sec)

    def merge(self, src, dst):
        """Two labels found to be one voice: fold src into dst."""
        with self._lock:
            if src in self._sum:
                self._sum[dst] = self._sum.get(dst, 0) + self._sum.pop(src)
                self._sec[dst] = self._sec.get(dst, 0.0) + self._sec.pop(src)


# ---------------------------------------------------------------- the store
def store_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Lia", "voice_bank.json")


_store_lock = threading.Lock()


def _norm_name(name):
    return " ".join(str(name or "").split()).casefold()


def load(path=None):
    """{"version": 1, "voices": [{"id", "name", "prints": [{"v": [...],
    "s": seconds, "src", "meeting", "at"}], "updated"}]}"""
    p = path or store_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("voices"), list):
            return d
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {"version": 1, "voices": []}


def save(data, path=None):
    p = path or store_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, p)


def voice_print(voice):
    """A voice's matching print: the seconds-weighted mean of its prints."""
    import numpy as np
    acc, tot = None, 0.0
    for pr in voice.get("prints") or ():
        try:
            v = np.asarray(pr["v"], np.float32)
            s = max(1.0, float(pr.get("s") or 1.0))
        except Exception:
            continue
        acc = v * s if acc is None else acc + v * s
        tot += s
    return None if acc is None else unit(acc)


def remember(name, emb, seconds, src="", meeting="", path=None, self_voice=False):
    """Save (or add to) the voice called `name`. One print per meeting: a
    second call for the same meeting replaces that meeting's print."""
    name = " ".join(str(name or "").split())
    if not name or emb is None:
        return None
    with _store_lock:
        d = load(path)
        key = _norm_name(name)
        voice = None
        if self_voice:     # the user: "Naor" (Your name) and "Naor Daniel" (Teams) are one voice
            voice = next((v for v in d["voices"] if v.get("self")), None)
        if voice is None:
            voice = next((v for v in d["voices"] if _norm_name(v.get("name")) == key), None)
        if voice is None:
            voice = {"id": hashlib.sha1(("%s|%f" % (key, time.time())).encode("utf-8")).hexdigest()[:10],
                     "name": name, "prints": []}
            d["voices"].append(voice)
        prints = [p for p in voice.get("prints") or () if not meeting or p.get("meeting") != meeting]
        prints.append({"v": [round(float(x), 5) for x in unit(emb)],
                       "s": round(float(seconds), 1), "src": str(src or ""),
                       "meeting": str(meeting or ""), "at": time.strftime("%Y-%m-%d %H:%M")})
        voice["prints"] = prints[-MAX_PRINTS_PER_VOICE:]
        voice["updated"] = time.strftime("%Y-%m-%d %H:%M")
        if self_voice:
            voice["self"] = True
        save(d, path)
        return voice["id"]


def forget(voice_id, path=None):
    with _store_lock:
        d = load(path)
        n = len(d["voices"])
        d["voices"] = [v for v in d["voices"] if v.get("id") != voice_id]
        if len(d["voices"]) != n:
            save(d, path)
            return True
        return False


def forget_all(path=None):
    p = path or store_path()
    with _store_lock:
        try:
            os.remove(p)
            return True
        except FileNotFoundError:
            return False


def summary(path=None):
    """For Settings: [{"id", "name", "meetings", "seconds", "updated"}]."""
    out = []
    for v in load(path)["voices"]:
        pr = v.get("prints") or []
        out.append({"id": v.get("id"), "name": v.get("name"),
                    "meetings": len(pr), "self": bool(v.get("self")),
                    "seconds": round(sum(float(p.get("s") or 0) for p in pr)),
                    "updated": v.get("updated") or ""})
    out.sort(key=lambda r: _norm_name(r["name"]))
    return out


SAVE_SELF_MIN = 0.45     # a new print this far from its own saved voice = suspect


def save_conflict(emb, name, seconds, voices, trusted=False):
    """Why this print should NOT be saved under `name` ("" = save it):
    too little speech, or (unless the user typed the name) it disagrees with
    the voice saved under that name, or it sounds like another saved voice.
    Teams captions misname a turn now and then; one wrong print must not
    teach the bank a wrong voice."""
    if emb is None:
        return "no print"
    if seconds < MIN_SAVE_SPEECH_S:
        return "only %.0f s of speech" % seconds
    if trusted:
        return ""
    key = _norm_name(name)
    own, other, other_name = None, -1.0, ""
    for v in voices or ():
        pr = voice_print(v)
        if pr is None:
            continue
        c = cosine(emb, pr)
        if _norm_name(v.get("name")) == key:
            own = c
        elif c > other:
            other, other_name = c, v.get("name") or ""
    if own is not None and own < SAVE_SELF_MIN:
        return "unlike the saved voice (%.2f)" % own
    if other >= MATCH_MIN and other >= (own if own is not None else -1.0) + MATCH_MARGIN:
        return "sounds like another saved voice (%.2f)" % other
    return ""


def best_match(emb, voices, exclude=(), min_cos=MATCH_MIN, margin=MATCH_MARGIN):
    """(name, cosine) of the saved voice this print matches, else (None, best).
    `voices` = [(name, print)]; a match needs min_cos AND a margin over the
    next different name (two people with near prints stay unnamed)."""
    if emb is None:
        return None, 0.0
    skip = {_norm_name(x) for x in exclude}
    scores = []
    for name, pr in voices:
        if pr is None or _norm_name(name) in skip:
            continue
        scores.append((cosine(emb, pr), name))
    if not scores:
        return None, 0.0
    scores.sort(reverse=True)
    best, name = scores[0]
    second = next((s for s, n in scores[1:] if _norm_name(n) != _norm_name(name)), -1.0)
    if best >= min_cos and best - second >= margin:
        return name, best
    return None, best


def assign(label_prints, voices, taken=(), min_cos=MATCH_MIN, margin=MATCH_MARGIN):
    """{label: (name, cos)} for labels that match a saved voice, at most one
    label per name (the closest wins). label_prints = {label: unit print}."""
    cands = []
    for lab, pr in label_prints.items():
        name, c = best_match(pr, voices, exclude=taken, min_cos=min_cos, margin=margin)
        if name:
            cands.append((c, lab, name))
    cands.sort(reverse=True)
    used, out = set(), {}
    for c, lab, name in cands:
        k = _norm_name(name)
        if k in used or lab in out:
            continue
        used.add(k)
        out[lab] = (name, c)
    return out


def bank_voices(path=None):
    """[(name, print)] of every saved voice (for matching)."""
    out = []
    for v in load(path)["voices"]:
        pr = voice_print(v)
        if pr is not None:
            out.append((v.get("name") or "", pr))
    return out


def fold_prints(prints, merge, root):
    """{label: (sum vector, seconds)} after "same person as" merges."""
    out = {}
    for lab in prints.labels():
        r = root(lab, merge)
        v, s = prints._sum.get(lab), prints.seconds(lab)
        if v is None:
            continue
        if r in out:
            out[r] = (out[r][0] + v, out[r][1] + s)
        else:
            out[r] = (v, s)
    return out


# ---------------------------------------------------------------- live
CHANNEL_DOMINANCE = 3.0   # a piece is one side's when its RMS is 3x the other's
CHANNEL_MIN_RMS = 0.005   # ... and it is not near silence


def _rms(a):
    import numpy as np
    return float(np.sqrt(np.mean(np.square(a, dtype=np.float32)))) if len(a) else 0.0


def pick_channel(mic, system, mix):
    """The audio a voiceprint is taken from: the channel that carries the
    voice ALONE (the user on the mic, a remote voice on the system track);
    None when both sides are loud (talk-over) or both near silence.
    Measured: the dedicated channel scores above the mixdown (see the plan)."""
    rm, rs = _rms(mic) if mic is not None else 0.0, _rms(system) if system is not None else 0.0
    if mic is None or system is None:
        # one source only (an in-room meeting): the mix is that source
        return mix if _rms(mix) >= CHANNEL_MIN_RMS else None
    if rm >= CHANNEL_MIN_RMS and rm >= CHANNEL_DOMINANCE * rs:
        return mic
    if rs >= CHANNEL_MIN_RMS and rs >= CHANNEL_DOMINANCE * rm:
        return system
    return None


class LiveTracker:
    """One meeting: keeps the last few minutes of the fed audio (mixdown + mic +
    system, on the speaker tracker's fed-sample timeline) and turns each live
    label's closed turns into embeddings (LabelPrints). Never blocks the
    meeting: feed() only appends; update() runs on the caller's worker thread."""

    KEEP_S = 180.0
    CLOSED_GUARD_S = 1.0     # a turn is closed when it ended this long before `covered`
    OPEN_PIECE_S = 15.0      # a long open turn is embedded in pieces of this size

    def __init__(self, embedder):
        import numpy as np
        self.embedder = embedder
        self.prints = LabelPrints()
        self._lock = threading.Lock()
        self._mix = np.zeros(0, np.float32)
        self._mic = np.zeros(0, np.float32)
        self._sys = np.zeros(0, np.float32)
        self._have_mic = False
        self._have_sys = False
        self._base = 0              # fed sample index of _mix[0]
        self._fed = 0
        self._upto = {}             # label -> fed second embedded up to
        self.embedded_s = 0.0
        self.skipped_s = 0.0
        self.wall_s = 0.0

    def feed(self, mix, mic=None, system=None):
        import numpy as np
        if mix is None or len(mix) == 0:
            return

        def f32(a, n):
            if a is None:
                return np.zeros(n, np.float32)
            a = np.asarray(a)
            a = a.astype(np.float32) / 32768.0 if a.dtype == np.int16 else a.astype(np.float32)
            return np.pad(a, (0, n - len(a))) if len(a) < n else a[:n]
        n = len(mix)
        with self._lock:
            self._have_mic |= mic is not None
            self._have_sys |= system is not None
            self._mix = np.concatenate([self._mix, f32(mix, n)])
            self._mic = np.concatenate([self._mic, f32(mic, n)])
            self._sys = np.concatenate([self._sys, f32(system, n)])
            self._fed += n
            over = len(self._mix) - int(self.KEEP_S * SR)
            if over > 0:
                self._mix, self._mic, self._sys = self._mix[over:], self._mic[over:], self._sys[over:]
                self._base += over

    def _slice(self, t0, t1):
        with self._lock:
            a = max(0, int(t0 * SR) - self._base)
            b = max(0, int(t1 * SR) - self._base)
            if b <= a:
                return None
            return (self._mic[a:b].copy() if self._have_mic else None,
                    self._sys[a:b].copy() if self._have_sys else None,
                    self._mix[a:b].copy())

    def update(self, turns, covered, final=False):
        """Embed what is new in each label's turns (fed-audio seconds)."""
        t_start = time.perf_counter()
        limit = covered if final else covered - self.CLOSED_GUARD_S
        for t in turns:
            lab, t0, t1 = t["speaker"], float(t["t0"]), float(t["t1"])
            start = max(t0, self._upto.get(lab, 0.0))
            closed = final or t1 <= limit
            end = t1 if closed else min(t1, limit)
            if end - start < (MIN_SEGMENT_S if closed else self.OPEN_PIECE_S):
                if closed and end > start:
                    self._upto[lab] = max(self._upto.get(lab, 0.0), end)
                continue
            for p0, p1 in pieces(start, end):
                got = self._slice(p0, p1)
                audio = pick_channel(*got) if got else None
                emb = self.embedder.embed(audio) if audio is not None else None
                if emb is None:
                    self.skipped_s += p1 - p0
                else:
                    self.prints.add(lab, emb, p1 - p0)
                    self.embedded_s += p1 - p0
                self._upto[lab] = p1
            if closed:
                self._upto[lab] = max(self._upto.get(lab, 0.0), end)
        self.wall_s += time.perf_counter() - t_start


def speech_seconds(turns):
    return sum(max(0.0, t["t1"] - t["t0"]) for t in turns)


def finite(x):
    try:
        return math.isfinite(float(x))
    except Exception:
        return False
