"""Hebrew speech recognition on a plain CPU: ivrit-ai/zipformer2-66m-streaming-he.

A 66 M-parameter streaming Zipformer2 transducer (by Yakov Kolani, mirrored by
ivrit.ai, CC-BY-4.0). The int8 export is 72 MB and decodes about 50x faster
than real time on a desktop CPU, where the Hebrew Whisper model is slower than
real time. Measured 2026-11-11 (corpus WER): Kan 11.8% vs ivrit turbo 8.5%,
FLEURS 23.5% vs 18.6%. Hebrew only: English comes out weak.

This module runs the three ONNX files on the onnxruntime Lia already ships,
with numpy only - no sherpa-onnx, no kaldi-native-fbank. It mirrors what
sherpa-onnx does for this model, and was checked against it on the 2,000 Kan
clips (see plans/asr_bench):
  - features: Kaldi fbank, 80 mel bins, 25 ms / 10 ms, povey window,
    snip_edges off, samples in [-1, 1], no dither;
  - encoder: 141 feature frames per step, advance 128 (1.28 s), state carried
    between steps, 1.5 s of silence appended so the last words are flushed;
  - search: greedy, at most one token per encoder frame, context of 2 tokens.
"""
import os
import threading

import numpy as np

REPO = "ivrit-ai/zipformer2-66m-streaming-he"
SUBDIR = "sherpa-onnx"
FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")

SAMPLE_RATE = 16000
_WIN = 400          # 25 ms
_SHIFT = 160        # 10 ms
_NFFT = 512
_NMEL = 80
_TAIL_PAD_S = 1.5   # flushes the last chunk (the model card's own usage)
_LOG_FLOOR = float(np.finfo(np.float32).eps)

_cache = {}


def _mel(f):
    return 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)


def _mel_bank():
    """Kaldi's triangular mel filters (20 Hz to 7600 Hz) over FFT bins 0..255."""
    if "bank" not in _cache:
        lo, hi = _mel(20.0), _mel(SAMPLE_RATE / 2.0 - 400.0)
        delta = (hi - lo) / (_NMEL + 1)
        m = _mel(np.arange(_NFFT // 2) * (SAMPLE_RATE / float(_NFFT)))
        bank = np.zeros((_NMEL, _NFFT // 2), dtype=np.float64)
        for j in range(_NMEL):
            left, center, right = lo + j * delta, lo + (j + 1) * delta, lo + (j + 2) * delta
            up = (m - left) / (center - left)
            down = (right - m) / (right - center)
            w = np.where(m <= center, up, down)
            bank[j] = np.where((m > left) & (m < right), w, 0.0)
        _cache["bank"] = bank.astype(np.float32)
    return _cache["bank"]


def _window():
    if "win" not in _cache:
        i = np.arange(_WIN, dtype=np.float64)
        _cache["win"] = np.power(0.5 - 0.5 * np.cos(2.0 * np.pi * i / (_WIN - 1)),
                                 0.85).astype(np.float32)
    return _cache["win"]


def fbank(audio):
    """Kaldi-compatible log mel features, shape (frames, 80), float32."""
    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    return fbank_frames(a, 0, (len(a) + _SHIFT // 2) // _SHIFT)


def frames_ready(n):
    """How many frames of a signal of `n` samples no longer depend on samples
    still to come (frame i reads up to sample i*160 + 279)."""
    return 0 if n < _WIN - (_WIN // 2 - _SHIFT // 2) else (n - 280) // _SHIFT + 1


def fbank_frames(a, f0, f1):
    """Frames [f0, f1) of the signal `a` (float32, 1-D), shape (f1 - f0, 80)."""
    n = len(a)
    frames = f1 - f0
    if frames <= 0 or n == 0:
        return np.zeros((0, _NMEL), dtype=np.float32)
    # snip_edges off: frame i covers [i*shift - 120, i*shift + 280), and
    # samples outside the signal are mirrored.
    idx = (np.arange(f0, f1)[:, None] * _SHIFT + (_SHIFT // 2 - _WIN // 2)
           + np.arange(_WIN)[None, :])
    idx = np.where(idx < 0, -idx - 1, idx)
    idx = np.where(idx >= n, 2 * n - 1 - idx, idx)
    idx = np.clip(idx, 0, n - 1)
    out = np.empty((frames, _NMEL), dtype=np.float32)
    win, bank = _window(), _mel_bank()
    step = 4000   # frames per block (bounds memory on a long file)
    for s in range(0, frames, step):
        fr = a[idx[s:s + step]].astype(np.float32)
        fr = fr - fr.mean(axis=1, keepdims=True)                     # DC offset
        fr = np.concatenate([fr[:, :1] * (1.0 - 0.97),               # pre-emphasis
                             fr[:, 1:] - 0.97 * fr[:, :-1]], axis=1)
        spec = np.fft.rfft(fr * win, n=_NFFT, axis=1)
        power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)[:, :_NFFT // 2]
        out[s:s + step] = np.log(np.maximum(power @ bank.T, _LOG_FLOOR))
    return out


def _dim(d, batch=1):
    return int(d) if isinstance(d, int) else batch


class ZipformerHe:
    """The loaded model. `decode(audio)` takes 16 kHz mono float32 in [-1, 1]."""

    def __init__(self, model_dir, threads=4):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, int(threads))
        so.inter_op_num_threads = 1
        prov = ["CPUExecutionProvider"]

        def _sess(name):
            return ort.InferenceSession(os.path.join(model_dir, name), sess_options=so,
                                        providers=prov)
        self.encoder = _sess(FILES[0])
        self.decoder = _sess(FILES[1])
        self.joiner = _sess(FILES[2])
        meta = self.encoder.get_modelmeta().custom_metadata_map
        self.T = int(meta.get("T", 141))
        self.chunk = int(meta.get("decode_chunk_len", 128))
        dmeta = self.decoder.get_modelmeta().custom_metadata_map
        self.context = int(dmeta.get("context_size", 2))
        self.tokens = {}
        with open(os.path.join(model_dir, FILES[3]), encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                piece, _, num = line.rpartition(" ")
                self.tokens[int(num)] = piece
        self.blank = 0
        self.unk = next((i for i, p in self.tokens.items() if p == "<unk>"), -1)
        ins = self.encoder.get_inputs()
        self._x_name = ins[0].name
        self._state_in = [(i.name, [_dim(d) for d in i.shape],
                           np.int64 if "int64" in i.type else np.float32)
                          for i in ins[1:]]
        outs = self.encoder.get_outputs()
        self._enc_out = outs[0].name
        self._state_out = [o.name for o in outs[1:]]
        self._lock = threading.Lock()

    def _decoder_out(self, hyp):
        y = np.asarray([hyp[-self.context:]], dtype=np.int64)
        return self.decoder.run(None, {"y": y})[0]

    def decode(self, audio):
        a = np.asarray(audio, dtype=np.float32).reshape(-1)
        if len(a) == 0:
            return ""
        a = np.concatenate([a, np.zeros(int(_TAIL_PAD_S * SAMPLE_RATE), dtype=np.float32)])
        feats = fbank(a)
        with self._lock:
            return self._decode_feats(feats)

    def _decode_feats(self, feats):
        st = self._new_state()
        self._advance(st, feats)
        return self._text(st["ids"])

    def _new_state(self):
        hyp = [-1] * (self.context - 1) + [self.blank]
        return {"states": {name: np.zeros(shape, dtype=dt)
                           for name, shape, dt in self._state_in},
                "hyp": hyp, "dec": self._decoder_out(hyp), "ids": [], "pos": 0}

    def _text(self, ids):
        text = "".join(self.tokens.get(i, "") for i in ids)
        return " ".join(text.replace("\u2581", " ").split())

    def _advance(self, st, feats):
        """Run every full encoder step that `feats` (all the frames so far)
        allows, from st["pos"]. The state is carried, so calling this again
        with more frames continues where it stopped (streaming)."""
        states, hyp, dec, out_ids, pos = (st["states"], st["hyp"], st["dec"],
                                          st["ids"], st["pos"])
        while pos + self.T <= len(feats):
            feed = {self._x_name: feats[pos:pos + self.T][None]}
            feed.update(states)
            res = self.encoder.run(None, feed)
            enc = res[0][0]                                   # (frames, 512)
            states = {name: res[1 + k] for k, (name, _, _) in enumerate(self._state_in)}
            pos += self.chunk
            t = 0
            while t < len(enc):
                # the decoder output only changes when a token is emitted, so
                # the rest of the chunk is scored in one joiner call
                rest = enc[t:]
                logit = self.joiner.run(None, {
                    "encoder_out": rest,
                    "decoder_out": np.repeat(dec, len(rest), axis=0)})[0]
                best = logit.argmax(axis=1)
                hit = np.nonzero((best != self.blank) & (best != self.unk))[0]
                if len(hit) == 0:
                    break
                k = int(hit[0])
                tok = int(best[k])
                hyp.append(tok)
                out_ids.append(tok)
                dec = self._decoder_out(hyp)
                t += k + 1
        st["states"], st["dec"], st["pos"] = states, dec, pos


class ZipformerStream:
    """The same model, fed as the audio arrives (the live draft in meetings).
    feed() takes whatever came since the last call and returns the text so
    far; the model steps every 1.28 s of audio, so the text trails the sound
    by about 1.5-2.5 s. finish() flushes the last words exactly as decode()
    does (a 1.5 s silent tail), so feed() in pieces + finish() gives the same
    text as decode() on the whole clip. reset() starts a new utterance."""

    def __init__(self, model):
        self.m = model
        self.reset()

    def reset(self):
        self._audio = np.zeros(0, dtype=np.float32)
        self._feats = np.zeros((0, _NMEL), dtype=np.float32)
        self._st = None

    def _run(self, upto):
        new = fbank_frames(self._audio, len(self._feats), upto)
        if len(new):
            self._feats = np.concatenate([self._feats, new])
        with self.m._lock:
            if self._st is None:
                self._st = self.m._new_state()
            self.m._advance(self._st, self._feats)
        return self.m._text(self._st["ids"])

    def feed(self, audio):
        a = np.asarray(audio, dtype=np.float32).reshape(-1)
        if len(a):
            self._audio = np.concatenate([self._audio, a])
        return self._run(frames_ready(len(self._audio)))

    def text(self):
        return self.m._text(self._st["ids"]) if self._st is not None else ""

    def finish(self):
        if len(self._audio) == 0:
            return ""
        self._audio = np.concatenate(
            [self._audio, np.zeros(int(_TAIL_PAD_S * SAMPLE_RATE), dtype=np.float32)])
        return self._run((len(self._audio) + _SHIFT // 2) // _SHIFT)


def download(revision=None, local_only=False):
    """The model folder in the Hugging Face cache (downloads ~72 MB the first
    time). `revision` pins the reviewed commit."""
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    from huggingface_hub import snapshot_download
    kw = {"revision": revision, "allow_patterns": [SUBDIR + "/" + f for f in FILES]}
    if local_only:
        root = snapshot_download(REPO, local_files_only=True, **kw)
    else:
        try:
            root = snapshot_download(REPO, **kw)
        except Exception:
            root = snapshot_download(REPO, local_files_only=True, **kw)
    path = os.path.join(root, SUBDIR)
    missing = [f for f in FILES if not os.path.isfile(os.path.join(path, f))]
    if missing:
        raise RuntimeError("Hebrew Zipformer download is incomplete: %s" % ", ".join(missing))
    return path
