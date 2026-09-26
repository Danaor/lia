# -*- coding: utf-8 -*-
"""The GPU gate + cross-app lease for ONE local Ollama call from a Lia window.

The Ask-your-meetings, Email search and Chat windows are their own processes
and called the shared summary model (gemma) directly, outside the gate and the
lease (found 2026-09-24): a question while the other app held the card loaded
~19.8 GB into a card with no room - the driver then PAGES it (09-23 18:04: 678
nvlddmkm errors) - and a num_ctx different from the resident runner's made
Ollama reload the shared model under the other app's run.

local_call() now wraps every such call:
  1. the cross-app lease (gpu_lease, app "lia": for the other app a window's
     hold is Lia's hold; Lia's main process sees another process's hold and
     releases its own Whisper models for it, as it does for the other app);
  2. a runner already loaded WHOLE with a big-enough context is reused as it is
     (its context is sent back: no reload);
  3. otherwise the load waits for the model's measured footprint (never a CPU
     split, never a paging load), idle models are unloaded only right after
     taking the lease (contract rule 4), and the fresh load is checked.
The window shows the wait ("GPU busy - ...") and can cancel it.

Inside the main Lia process (Voice Ask runs the same meetings RAG) lia.py plugs
its own gate in (IN_PROCESS_GATE): the same lease object as its summaries - a
second Lease in one process would wait on that process's own file lock.

stdlib + requests only: the windows must not import lia.py.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
import time

import gpu_lease

# The measured footprint (2026-09-23, an EMPTY 3090, dedicated delta via
# nvidia-smi): gemma4:31b-it-qat = 19,105 MiB at num_ctx 8192, 19,453 at 20480,
# 19,831 at 32768 -> ~18,863 MiB + ~0.0296 MiB per context token. lia.py gates
# its own summaries on the same table.
FOOTPRINT = {"gemma4:31b-it-qat": (18863, 0.0296)}
VRAM_MARGIN_MB = 400
PAGING_FREE_MB = 256        # free VRAM left after a load below this = the card
                            # is at its ceiling: suspect paging
LEARNED = {}                # model -> MiB a fresh load really needed
CTX_FLOOR, CTX_BUCKET, CTX_CAP = 8192, 4096, 32768
CHARS_PER_TOKEN = 1.9       # Hebrew (the conservative side; English ~4)
WAIT_POLL_S = 2.0
EXPECT_S = {"ask": 120, "chat": 180}

# Set by the window process: where a wait is shown and how the user cancels it.
UI = {"on_wait": None, "should_cancel": None}
# Set by lia.py in the main process: a context manager with local_call's
# signature (model, root, load_ctx, need_ctx, job, detail, on_wait,
# should_cancel) that yields the num_ctx to send.
IN_PROCESS_GATE = [None]

_CALL_LOCK = threading.Lock()       # one gated call at a time per window process
_LEASE = [None]
# The windows have no lia.log: warnings reach their stderr sink (lastResort).
_log = logging.getLogger("lia.ollama_gate")


class GpuWaitCancelled(Exception):
    """The user cancelled a call that was waiting for the GPU."""


def is_local(url):
    return bool(re.match(r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(/|$)",
                         (url or "").strip()))


def root_of(base_url):
    """http://127.0.0.1:11434 from any Ollama URL (127.0.0.1, not localhost:
    a new localhost connection on Windows tries IPv6 first and costs ~2 s)."""
    h = (base_url or "http://127.0.0.1:11434").strip().rstrip("/")
    h = re.sub(r"/(v1/chat/completions|v1|api/[a-z]+)$", "", h)
    return h.replace("//localhost:", "//127.0.0.1:")


def ctx_for(chars, reserve_tokens):
    """The context a call needs: its text at CHARS_PER_TOKEN + the answer's
    reserve, bucketed up to CTX_BUCKET, clamped to [CTX_FLOOR, CTX_CAP]."""
    need = int((chars or 0) / CHARS_PER_TOKEN) + int(reserve_tokens or 0)
    need = -(-need // CTX_BUCKET) * CTX_BUCKET
    return max(CTX_FLOOR, min(CTX_CAP, need))


def vram_need(model, num_ctx):
    """Free VRAM (MiB) `model` needs to load at `num_ctx`, or None (unknown)."""
    fp = FOOTPRINT.get(model)
    need = int(fp[0] + fp[1] * int(num_ctx or 0)) + VRAM_MARGIN_MB if fp else None
    learned = LEARNED.get(model)
    if learned and (need is None or learned > need):
        need = learned
    return need


def gpu_free_mb():
    """Free VRAM on GPU 0 in MiB (NVML, else nvidia-smi), or None (unknown)."""
    try:
        import ctypes
        dll = ctypes.WinDLL(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                         "System32", "nvml.dll"))
        if dll.nvmlInit_v2() == 0:
            h = ctypes.c_void_p()
            if dll.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) == 0:
                class _Mem(ctypes.Structure):
                    _fields_ = [("total", ctypes.c_ulonglong),
                                ("free", ctypes.c_ulonglong),
                                ("used", ctypes.c_ulonglong)]
                m = _Mem()
                if dll.nvmlDeviceGetMemoryInfo(h, ctypes.byref(m)) == 0:
                    return int(m.free // (1 << 20))
    except Exception:
        pass
    try:
        import subprocess
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                           "nvidia-smi.exe")
        r = subprocess.run([exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        if r.returncode == 0 and (r.stdout or "").strip():
            return int(float(r.stdout.strip().splitlines()[0]))
    except Exception:
        pass
    return None


def ps(root):
    """{name: (size MiB, size_vram MiB, context_length)} of the models Ollama
    holds now, or None when Ollama is unreachable."""
    try:
        import requests
        r = requests.get(root + "/api/ps", timeout=5)
        out = {}
        for m in (r.json().get("models") or []):
            name = m.get("name") or m.get("model")
            if name:
                out[name] = (int(m.get("size") or 0) >> 20,
                             int(m.get("size_vram") or 0) >> 20,
                             int(m.get("context_length") or 0))
        return out
    except Exception:
        return None


def _unload(root, names, wait_s=20.0):
    import requests
    for n in names:
        try:
            requests.post(root + "/api/generate", json={"model": n, "keep_alive": 0},
                          timeout=30)
        except Exception:
            pass
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if not set(names) & set(ps(root) or {}):
            return True
        time.sleep(0.5)
    return False


class _Waiter:
    """Reports a wait (de-duplicated) and turns a cancel into GpuWaitCancelled."""

    def __init__(self, on_wait, should_cancel):
        self.on_wait, self.should_cancel = on_wait, should_cancel
        self._last = None

    def note(self, text):
        if self.on_wait is not None and text != self._last:
            self._last = text
            try:
                self.on_wait(text)
            except Exception:
                pass

    def cancelled(self):
        try:
            return bool(self.should_cancel and self.should_cancel())
        except Exception:
            return False

    def sleep(self, seconds):
        end = time.monotonic() + seconds
        while True:
            if self.cancelled():
                raise GpuWaitCancelled("cancelled while waiting for the GPU")
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(1.0, left))


def _ensure(model, load_ctx, need_ctx, root, w, log=None, post=None):
    """Under the lease: the num_ctx to send once `model` runs whole in VRAM."""
    import requests
    post = post or requests.post
    first = True
    t0 = time.monotonic()
    while True:
        p = ps(root)
        if p is None:
            return load_ctx                    # Ollama unreachable: the call says so
        cur = p.get(model)
        if cur and cur[1] >= cur[0] and (not cur[2] or cur[2] >= need_ctx):
            return cur[2] or load_ctx          # resident whole, big enough: no reload
        free = gpu_free_mb()
        need = vram_need(model, load_ctx)
        if free is None or not need:
            return load_ctx                    # never block blind
        # The same model resident (a smaller window, or split) is replaced by
        # this load, so its VRAM counts as room.
        avail = free + (cur[1] if cur else 0)
        if avail < need and first and p:
            # Contract rule 4: right after taking the lease - nobody is mid-run -
            # the holder may unload idle models.
            first = False
            gone = _unload(root, list(p))
            if log is not None:
                log.info("GPU lease: unloaded idle Ollama %s for %s (free %d MiB, need "
                         "%d)%s", ", ".join(p), model, free, need,
                         "" if gone else " - still listed after 20 s")
            continue
        first = False
        if avail < need:
            w.note("GPU busy - waiting for %.1f GB free (%.1f GB now) %s"
                   % (need / 1024.0, avail / 1024.0, gpu_lease._fmt(time.monotonic() - t0)))
            w.sleep(WAIT_POLL_S)
            continue
        try:
            post(root + "/api/generate", json={"model": model, "prompt": "", "stream": False,
                                               "options": {"num_ctx": int(load_ctx)}},
                 timeout=(10, 600))
        except Exception as e:
            if log is not None:
                log.warning("Ollama pre-load of %s failed (%s) - the call will load it",
                            model, e)
            return load_ctx
        size, vram, _c = (ps(root) or {}).get(model, (0, 0, 0))
        free1 = gpu_free_mb()
        if not size:
            return load_ctx
        if vram >= size and (free1 is None or free1 >= PAGING_FREE_MB):
            return load_ctx
        # This fresh load does not fit (split, or the card is at its ceiling =
        # the driver pages): unload it, raise the need, wait again.
        LEARNED[model] = max(need, size + 1400, (avail - (free1 or 0)) + 1000)
        if log is not None:
            log.warning("GPU: %s did not fit (%d of %d MiB in VRAM, %s MiB left free) - "
                        "unloading this load and waiting for %d MiB free", model, vram,
                        size, "?" if free1 is None else free1, LEARNED[model])
        _unload(root, [model])


@contextlib.contextmanager
def local_call(model, base_url, need_ctx, load_ctx=None, *, job="ask", detail="",
               on_wait=None, should_cancel=None, log=None):
    """Hold the GPU for ONE local Ollama call; yields the num_ctx to send.
    need_ctx = the context the call needs (a resident runner at least this big
    is reused as it is); load_ctx = the window a fresh load takes (>= need_ctx).
    A non-local URL, or the test suite (LIA_TEST_NO_GPU_GATE), passes through.
    Raises GpuWaitCancelled when the user cancels the wait."""
    log = log or _log
    load_ctx = max(int(need_ctx), int(load_ctx or 0))
    if not is_local(base_url) or os.environ.get("LIA_TEST_NO_GPU_GATE"):
        yield load_ctx
        return
    on_wait = on_wait or UI["on_wait"]
    should_cancel = should_cancel or UI["should_cancel"]
    root = root_of(base_url)
    gate = IN_PROCESS_GATE[0]
    if gate is not None:
        with gate(model, root, load_ctx, int(need_ctx), job, detail, on_wait,
                  should_cancel) as ctx:
            yield ctx
        return
    w = _Waiter(on_wait, should_cancel)
    with _CALL_LOCK:
        lease = _lease(log)
        lease.acquire(job, detail=detail, expect_s=EXPECT_S.get(job, 120),
                      need_mib=vram_need(model, load_ctx) or 0, on_wait=w.note,
                      should_cancel=w.cancelled, cancel_exc=GpuWaitCancelled)
        try:
            yield _ensure(model, load_ctx, int(need_ctx), root, w, log)
        finally:
            lease.release()


def prewarm(model, base_url, num_ctx, *, keep_alive="10m", room_wait_s=20.0, log=None):
    """Load `model` for a window's first question, only when that is free now:
    never waits for the lease (the other app's job wins), waits at most
    room_wait_s for VRAM (Lia's main process releases its own models within a
    few seconds of seeing the hold), never loads into a card without room.
    Returns "warm" / "resident" / "busy" / "no-room" / "failed"."""
    import requests
    log = log or _log
    root = root_of(base_url)

    def _warm(ctx):
        requests.post(root + "/api/chat", json={
            "model": model, "stream": False, "keep_alive": keep_alive,
            "options": {"num_predict": 1, "num_ctx": int(ctx)},
            "messages": [{"role": "user", "content": "hi"}]}, timeout=(10, 180))

    try:
        if not is_local(base_url) or os.environ.get("LIA_TEST_NO_GPU_GATE"):
            _warm(num_ctx)
            return "warm"
        cur = (ps(root) or {}).get(model)
        if cur and cur[1] >= cur[0]:
            return "resident"          # a question reuses (or reloads) it under the lease
        if IN_PROCESS_GATE[0] is not None or not _CALL_LOCK.acquire(blocking=False):
            return "busy"
        try:
            lease = _lease(log)
            took = lease.acquire("ask", detail="prewarm", expect_s=60,
                                 need_mib=vram_need(model, num_ctx) or 0, wait=False)
            if took is None:
                return "busy"
            try:
                deadline = time.monotonic() + room_wait_s
                w = _Waiter(None, lambda: time.monotonic() > deadline)
                try:
                    ctx = _ensure(model, num_ctx, num_ctx, root, w, log)
                except GpuWaitCancelled:
                    return "no-room"
                _warm(ctx)
                return "warm"
            finally:
                lease.release()
        finally:
            _CALL_LOCK.release()
    except Exception as e:
        if log is not None:
            log.warning("Prewarm of %s failed: %s", model, e)
        return "failed"


def _lease(log=None):
    """The cross-app lease when the user turned it on (inherited
    LIA_GPU_LEASE), else a no-op one - see gpu_lease.make."""
    if _LEASE[0] is None:
        _LEASE[0] = gpu_lease.make("lia", log=log)
    return _LEASE[0]
