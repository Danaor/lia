# -*- coding: utf-8 -*-
"""Lia - live speaker labels during a chunked meeting (NVIDIA Nemotron 3).

One `diarize_nemotron.py --live` child per meeting, fed the meeting's mixdown
as it is drained (every rotation window, in order, exactly once). The child
answers after each step (default 38 x 80 ms = 3.04 s, no look-ahead) with the
speaker runs of that step, so labels trail the audio by at most one step.

The meeting cuts each window at the label changes (split_audio_by_turns, the
same cut the Teams caption names use), so a transcript chunk is one speaker's
turn and gets its label BEFORE it is transcribed: the text and the speaker
reach the live transcript in the same update (Naor, 2026-11-06).

The timeline here is the fed audio (sample count), not the wall clock; the
caller converts with its own offset. Stdlib + numpy only (runs in the app).

The child can never hold up the meeting (2026-11-08, after Lia Lite's stuck
request): feed() only queues; a writer thread writes to the child. A write
blocked for WRITE_STALL_S, or more than MAX_BACKLOG_S of audio waiting, ends
speaker detection for this meeting (the child is killed, never restarted - a
new session would number the voices from A again); the meeting goes on with
the usual cut.
"""

import json
import os
import queue
import struct
import subprocess
import threading
import time

WRITE_STALL_S = 20.0
MAX_BACKLOG_S = 60.0
# Until the child says "ready" it is loading the model and reads nothing: a
# cold load right after Lia starts (Whisper + Parakeet loading too) took over
# 20 s on 2026-11-09 and the stall check ended speakers 30 s into a meeting.
LOAD_TIMEOUT_S = 180.0
LOAD_BACKLOG_S = 240.0

FRAME_MERGE_GAP_S = 0.4
MIN_TURN_S = 0.2


def _alpha(i):
    s, i = "", i + 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


class LiveSpeakers:
    """Feed audio, read speaker turns. Every method is safe to call after the
    child died: the tracker then reports no turns and the meeting cuts as usual."""

    def __init__(self, interp, script, pkgs, model_dir, step_frames=38, threads=4,
                 log=None):
        self._args = [interp, "-X", "utf8", "-I", script, "--live",
                      "--pkgs", pkgs, "--model-dir", model_dir,
                      "--threads", str(int(threads)), "--step", str(int(step_frames))]
        self.step_s = int(step_frames) * 0.08
        self._log = log or (lambda *a: None)
        self._proc = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._runs = []           # merged [t0, t1, idx]
        self._labels = {}         # model index -> "A", "B", ... (arrival order)
        self._covered = 0.0
        self._fed = 0             # samples written
        self._ready = False
        self._ready_at = 0.0
        self._started = time.monotonic()
        self._done = False
        self.error = ""
        self._q = queue.Queue()
        self._backlog = 0.0       # seconds queued, not yet written
        self._writing_since = None

    # ---- lifecycle ----
    def start(self):
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                   HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            env.pop(k, None)
        # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
        self._started = time.monotonic()
        self._proc = subprocess.Popen(self._args, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      env=env, creationflags=0x08000000 | 0x00004000)
        threading.Thread(target=self._reader, daemon=True,
                         name="live-speakers").start()
        threading.Thread(target=self._writer, daemon=True,
                         name="live-speakers-writer").start()
        return self

    def _writer(self):
        """Write queued frames to the child, in order. None = close stdin."""
        while True:
            item = self._q.get()
            p = self._proc
            if item is None or p is None:
                try:
                    if p is not None:
                        p.stdin.close()
                except Exception:
                    pass
                return
            data, dur = item
            self._writing_since = time.monotonic()
            try:
                p.stdin.write(data)
                p.stdin.flush()
            except Exception as e:
                self._fail("feed failed: %s" % e)
                return
            finally:
                self._writing_since = None
                with self._lock:
                    self._backlog = max(0.0, self._backlog - dur)

    def _fail(self, why):
        """Speaker detection ends for this meeting (no restart)."""
        with self._cond:
            if not self.error:
                self.error = why
            self._cond.notify_all()
        p = self._proc
        try:
            if p is not None and p.poll() is None:
                p.kill()
        except Exception:
            pass
        self._log("Live speakers: off for this meeting (%s)" % why)

    def _check_stall(self):
        t = self._writing_since
        if t is None or self.error:
            return
        if not self._ready:
            if time.monotonic() - self._started > LOAD_TIMEOUT_S:
                self._fail("the model did not load in %.0f s" % LOAD_TIMEOUT_S)
            return
        # the stall clock starts no earlier than "ready" (a write queued
        # during the load waits for the load, not for a stuck model)
        if time.monotonic() - max(t, self._ready_at) > WRITE_STALL_S:
            self._fail("the model stopped reading for %.0f s" % WRITE_STALL_S)

    @property
    def proc(self):
        return self._proc

    def alive(self):
        return (self._proc is not None and self._proc.poll() is None
                and not self.error)

    def _reader(self):
        try:
            for line in self._proc.stdout:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                with self._cond:
                    if m.get("ready"):
                        self._ready = True
                        self._ready_at = time.monotonic()
                    if m.get("error"):
                        self.error = str(m["error"])
                    for r in m.get("runs") or ():
                        self._add_run(float(r[0]), float(r[1]), int(r[2]))
                    if "covered" in m:
                        self._covered = float(m["covered"])
                    if m.get("done"):
                        self._done = True
                    self._cond.notify_all()
        except Exception as e:
            self.error = self.error or str(e)
        finally:
            with self._cond:
                if not self._done and not self.error:
                    self.error = "speaker detection stopped"
                self._cond.notify_all()
            if self.error:
                self._log("Live speakers: %s" % self.error)

    def _add_run(self, t0, t1, idx):
        if idx not in self._labels:
            self._labels[idx] = _alpha(len(self._labels))
        if (self._runs and self._runs[-1][2] == idx
                and t0 - self._runs[-1][1] <= FRAME_MERGE_GAP_S):
            self._runs[-1][1] = max(self._runs[-1][1], t1)
        else:
            self._runs.append([t0, t1, idx])

    # ---- feeding ----
    def feed(self, audio):
        """Queue one drained window (float in [-1, 1] or int16), in order.
        Never blocks: the writer thread hands it to the child."""
        self._check_stall()
        if audio is None or not self.alive():
            return
        try:
            import numpy as np
            a = np.asarray(audio)
            if a.dtype != np.int16:
                a = (np.clip(a.astype(np.float32), -1.0, 1.0) * 32767.0).astype(np.int16)
            b = a.tobytes()
            if not b:
                return
            dur = a.shape[0] / 16000.0
            with self._lock:
                cap = MAX_BACKLOG_S if self._ready else LOAD_BACKLOG_S
                if self._backlog + dur > cap:
                    over = True
                else:
                    over = False
                    self._backlog += dur
                    self._fed += a.shape[0]
            if over:
                self._fail("more than %.0f s of audio waiting for the model" % cap)
                return
            self._q.put((struct.pack("<I", len(b)) + b, dur))
        except Exception as e:
            self.error = self.error or ("feed failed: %s" % e)

    @property
    def fed_s(self):
        with self._lock:
            return self._fed / 16000.0

    @property
    def covered_s(self):
        with self._lock:
            return self._covered

    def wait_labels(self, timeout=4.0):
        """Wait until every full step of the fed audio is labelled (the last
        partial step stays open). True when it got there."""
        self._check_stall()
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                if self.error or self._done:
                    return not self.error
                if self._covered >= self._fed / 16000.0 - self.step_s - 0.02:
                    return True
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)

    def finish(self, timeout=10.0):
        """End of the meeting: flush the last partial step and wait for it."""
        self._check_stall()
        if not self.alive():
            return False
        self._q.put((struct.pack("<I", 0), 0.0))     # after every queued frame
        self._q.put(None)
        end = time.monotonic() + timeout
        with self._cond:
            while not self._done and not self.error:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            return self._done

    def stop(self):
        p = self._proc
        if p is None:
            return
        self._q.put(None)
        try:
            if p.poll() is None:
                if self._writing_since is not None:
                    p.kill()                # a blocked write: do not wait on it
                p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # ---- reading ----
    def turns(self):
        """[{"t0", "t1", "speaker": "A"}] on the fed-audio timeline, merged and
        without blips (the same shape as the Teams caption turns)."""
        with self._lock:
            runs = [list(r) for r in self._runs]
            labels = dict(self._labels)
        out = []
        for t0, t1, idx in runs:
            if t1 - t0 < MIN_TURN_S:
                continue
            lab = labels.get(idx, "?")
            if out and out[-1]["speaker"] == lab and t0 - out[-1]["t1"] <= FRAME_MERGE_GAP_S:
                out[-1]["t1"] = max(out[-1]["t1"], t1)
            else:
                out.append({"t0": t0, "t1": t1, "speaker": lab})
        return out

    def speakers(self):
        with self._lock:
            return sorted(set(self._labels.values()))


def dominant_label(turns, a, b, dominant=0.55):
    """The label holding >= `dominant` of the labelled time in [a, b], else
    None (a window with nobody or a real mix)."""
    tot = {}
    for t in turns:
        ov = min(b, t["t1"]) - max(a, t["t0"])
        if ov > 0:
            tot[t["speaker"]] = tot.get(t["speaker"], 0.0) + ov
    if not tot:
        return None
    lab, sec = max(tot.items(), key=lambda kv: kv[1])
    return lab if sec >= dominant * sum(tot.values()) else None
