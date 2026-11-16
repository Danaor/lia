# -*- coding: utf-8 -*-
"""The fast text of a meeting: each sentence about a second after it ends (2026-11-16, after Lia Lite's live stream).

A meeting transcribed by OpenAI's `gpt-transcribe` used to show nothing until its window (10-15 s) was cut and sent.
Here the same model is asked over one open realtime connection instead: speech is sent as it is said, a turn is closed
at a pause of 0.4 s (or after 8 s), and its text comes back about 0.6 s later. Silence is not sent, so it is not paid
for. Measured in Lia Lite (2026-10-17): median 0.6 s after the end of a sentence, the price of a minute of speech the
same as a chunk's, 80% of the words equal to the final text (chunks with context: 86%).

In Lia this text is a DRAFT for the live window only: the meeting's chunks are cut, transcribed and saved exactly as
before, and their text replaces the draft. A connection that fails or drops means no draft, nothing else.

Nothing here knows the app: `Stream` takes the API key, a prompt, and `on_line({start, end, text})`; audio is pushed
in with feed(mono float32 at 16 kHz, meeting seconds of its last sample). `connect` and `mint` are replaceable (tests).
The TLS trust and the proxy are websocket-client's and requests' own, as everywhere in Lia.
"""
from __future__ import annotations

import base64
import collections
import json
import logging
import threading
import time

import numpy as np

log = logging.getLogger("Lia")

SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
WS_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
MODEL = "gpt-transcribe"
RATE_IN = 16000
RATE = 24000
FRAME = 480                    # 20 ms at 24 kHz
SEND_EVERY = 5                 # frames: about 100 ms per append
HANG, MIN_TURN, MAX_TURN = 20, 25, 400     # a 0.4 s pause ends a turn; at least 0.5 s; at most 8 s
MIN_VOICED = 15                # frames of speech (0.3 s): a shorter sound is dropped, not sent
LEAD_IN = 10                   # frames before the detected start (200 ms), so the first syllable is not cut
NOISE_WINDOW = 250             # frames (5 s) the threshold is learned from
MIN_THRESHOLD = 300.0          # int16 RMS
LOST_AFTER_S = 15.0            # a turn with no text after this long is forgotten
BACKOFF_S = (2, 5, 10, 30, 60)
GIVE_UP_AFTER = 8              # connections that failed in a row: no draft for the rest of the meeting
PROMPT_MAX = 600
SECRET_TTL_S = 600
KEEP_LINES_S = 120.0           # lines older than this are dropped (the chunks have long replaced them)


def session_config(prompt="", model=MODEL):
    tr = {"model": model}
    if prompt:
        tr["prompt"] = prompt[:PROMPT_MAX]
    return {"type": "transcription",
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": RATE},
                                "transcription": tr,
                                "turn_detection": None}}}


def mint(api_key, prompt="", timeout=15.0):
    """A short-lived secret for one connection, with the session already set (transcription, 24 kHz PCM, the turns
    closed by this side). -> the secret, or "" (logged with the HTTP status only). A session the prompt made OpenAI
    refuse (HTTP 400) is asked again without it."""
    import requests

    def post(p):
        return requests.post(SECRETS_URL, timeout=timeout, headers={"Authorization": "Bearer " + api_key},
                             json={"expires_after": {"anchor": "created_at", "seconds": SECRET_TTL_S},
                                   "session": session_config(p)})
    r = post(prompt)
    if r.status_code == 400 and prompt:
        r = post("")
    if r.status_code != 200:
        log.info("Fast meeting text: no session (HTTP %d)", r.status_code)
        return ""
    d = r.json()
    return str(d.get("value") or (d.get("client_secret") or {}).get("value") or "")


def _connect(url, secret):
    import websocket
    ws = websocket.create_connection(url, header=["Authorization: Bearer " + secret], timeout=15)
    ws.settimeout(None)             # a quiet stretch of the meeting is not a dead connection
    return ws


def foreign_script(text):
    """Letters that are neither Hebrew nor Latin: the model's guess on a short or noisy turn, never shown."""
    return any(c.isalpha() and not (ord(c) <= 0x24F or 0x590 <= ord(c) <= 0x5FF or 0xFB1D <= ord(c) <= 0xFB4F)
               for c in text)


class TurnDetector:
    """Energy turns on 20 ms frames (Lia Lite's, unchanged)."""

    def __init__(self):
        self.recent = collections.deque(maxlen=NOISE_WINDOW)
        self.in_turn, self.quiet, self.length, self.voiced = False, 0, 0, 0

    def threshold(self):
        if len(self.recent) < 25:
            return MIN_THRESHOLD * 2
        return max(MIN_THRESHOLD, float(np.percentile(np.asarray(self.recent), 30)) * 3.0)

    def push(self, frame):
        """-> "start" | "continue" | "end" | "drop" | "idle" ("drop": the turn was only a short sound)."""
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2))) if len(frame) else 0.0
        thr = self.threshold()
        self.recent.append(rms)
        speech = rms > thr
        if not self.in_turn:
            if speech:
                self.in_turn, self.quiet, self.length, self.voiced = True, 0, 1, 1
                return "start"
            return "idle"
        self.length += 1
        self.quiet = 0 if speech else self.quiet + 1
        self.voiced += 1 if speech else 0
        if self.quiet >= HANG and self.voiced < MIN_VOICED:
            self.in_turn = False
            return "drop"
        if (self.quiet >= HANG and self.length >= MIN_TURN) or self.length >= MAX_TURN:
            self.in_turn = False
            return "end"
        return "continue"


def to_pcm24(audio):
    """Mono float32 at 16 kHz -> int16 at 24 kHz."""
    a = np.asarray(audio, dtype=np.float32)
    if not len(a):
        return np.zeros(0, dtype=np.int16)
    n = int(len(a) * RATE / RATE_IN)
    a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
    return np.clip(a * 32767.0, -32768, 32767).astype(np.int16)


class Stream:
    """start() -> feed(audio, now_s) from the meeting's thread -> lines() / on_line -> stop()."""

    def __init__(self, api_key, prompt="", on_line=None, connect=None, mint_fn=None):
        self.api_key, self.prompt, self.on_line = api_key, prompt or "", on_line
        self.connect = connect or _connect
        self.mint = mint_fn or mint
        self.q = collections.deque(maxlen=600)     # (int16 at 24 kHz, meeting seconds of its last sample)
        self.stop_evt = threading.Event()
        self.ws = None
        self.up = False
        self.pending = collections.deque()         # (start, end, committed_at) in commit order
        self.items = {}                            # item_id -> (start, end, committed_at)
        self._lines = []                           # [{start, end, text}] in order of arrival
        self.count = 0                             # lines since the start
        self.sent_s = 0.0                          # seconds of audio sent (what is paid for)
        self.lat = []                              # seconds from a turn's commit to its text
        self.failures = 0
        self.dead = False                          # gave up: no draft for the rest of the meeting
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True, name="lia-fast-text")

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_evt.set()
        ws = self.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def feed(self, audio, now_s):
        """Audio that just arrived (mono float32, 16 kHz); `now_s` = the meeting's seconds at its last sample."""
        if self.dead or audio is None or not len(audio):
            return
        self.q.append((to_pcm24(audio), float(now_s)))

    def lines(self, since_s=0.0):
        """The lines whose middle is after `since_s` (meeting seconds), in order of their time."""
        with self._lock:
            got = [dict(x) for x in self._lines if (x["start"] + x["end"]) / 2.0 >= since_s]
        return sorted(got, key=lambda x: x["start"])

    def waiting(self):
        old = time.monotonic() - LOST_AFTER_S
        with self._lock:
            return sum(1 for (_s, _e, c) in list(self.pending) + list(self.items.values()) if c >= old)

    # ---- the loop ----
    def _run(self):
        attempt = 0
        while not self.stop_evt.is_set():
            ok = False
            try:
                secret = self.mint(self.api_key, self.prompt)
                if not secret:
                    raise ConnectionError("no session")
                self.ws = self.connect(WS_URL, secret)
                ok = True
                attempt = 0
                self.failures = 0
                self._session()
            except Exception as e:
                if not self.stop_evt.is_set():
                    self.failures += 1
                    log.info("Fast meeting text down: %s%s", type(e).__name__, "" if ok else " (not connected)")
            finally:
                ws, self.ws = self.ws, None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self.up = False
                with self._lock:
                    self.pending.clear()
                    self.items.clear()
            if self.failures >= GIVE_UP_AFTER:
                self.dead = True
                log.warning("Fast meeting text stopped for this meeting after %d failed connections", self.failures)
                break
            if self.stop_evt.wait(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]):
                break
            attempt += 1

    def _session(self):
        ws = self.ws
        reader = threading.Thread(target=self._read, args=(ws,), daemon=True, name="lia-fast-text-read")
        reader.start()
        self.q.clear()                             # nothing older than this connection
        self.up = True
        log.info("Fast meeting text: connected")
        det, buf = TurnDetector(), np.zeros(0, dtype=np.int16)
        lead = collections.deque(maxlen=LEAD_IN)
        end_s = 0.0                                # meeting seconds at the end of `buf`
        turn_start, out = None, []
        while not self.stop_evt.is_set() and reader.is_alive():
            blocks = []
            while self.q:
                try:
                    blocks.append(self.q.popleft())
                except IndexError:
                    break
            if not blocks:
                time.sleep(0.03)
                self._forget_lost()
                continue
            buf = np.concatenate([buf] + [b for b, _t in blocks])
            end_s = blocks[-1][1]
            while len(buf) >= FRAME:
                fr, buf = buf[:FRAME], buf[FRAME:]
                at = end_s - len(buf) / RATE       # meeting seconds at the end of this frame
                state = det.push(fr)
                if state == "start":
                    turn_start = at - (len(lead) + 1) * FRAME / RATE
                    out = list(lead) + [fr]
                    lead.clear()
                elif state == "continue":
                    out.append(fr)
                elif state == "end":
                    out.append(fr)
                    self._append(ws, out)
                    out = []
                    ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    with self._lock:
                        self.pending.append((round(max(0.0, turn_start), 2), round(at, 2), time.monotonic()))
                    turn_start = None
                elif state == "drop":
                    out = []
                    ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    turn_start = None
                else:
                    lead.append(fr)
                if det.in_turn and len(out) >= SEND_EVERY:
                    self._append(ws, out)
                    out = []
        if not reader.is_alive() and not self.stop_evt.is_set():
            raise ConnectionError("the reader stopped")

    def _append(self, ws, frames):
        if frames:
            pcm = np.concatenate(frames)
            self.sent_s += len(pcm) / RATE
            ws.send(json.dumps({"type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm.tobytes()).decode("ascii")}))

    def _forget_lost(self):
        now = time.monotonic()
        with self._lock:
            for k, (_s, _e, c) in list(self.items.items()):
                if c < now - LOST_AFTER_S:
                    self.items.pop(k, None)

    def _read(self, ws):
        while not self.stop_evt.is_set():
            try:
                msg = ws.recv()
            except Exception:
                return
            if not msg:
                return
            try:
                ev = json.loads(msg)
            except ValueError:
                continue
            typ = ev.get("type") or ""
            if typ == "input_audio_buffer.committed":
                with self._lock:
                    if self.pending:
                        self.items[ev.get("item_id")] = self.pending.popleft()
            elif typ.endswith("input_audio_transcription.completed"):
                with self._lock:
                    t = self.items.pop(ev.get("item_id"), None)
                text = " ".join(str(ev.get("transcript") or "").split())
                if t is None or not text or foreign_script(text):
                    continue
                line = {"start": t[0], "end": t[1], "text": text}
                with self._lock:
                    self.count += 1
                    self.lat.append(time.monotonic() - t[2])
                    self._lines.append(line)
                    keep = t[1] - KEEP_LINES_S
                    self._lines = [x for x in self._lines if x["end"] >= keep]
                if self.on_line is not None:
                    try:
                        self.on_line(dict(line))
                    except Exception:
                        log.debug("Fast meeting text: the line handler failed")
            elif typ == "error":
                log.info("Fast meeting text: %s", ((ev.get("error") or {}).get("code") or "error"))

    def summary(self):
        """Numbers for the log at the end of a meeting (no text)."""
        with self._lock:
            lat = sorted(self.lat)
        med = lat[len(lat) // 2] if lat else 0.0
        return "%d line(s), %.0f s of speech sent, text %.1f s after a sentence (median)" % (self.count, self.sent_s, med)
