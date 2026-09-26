# -*- coding: utf-8 -*-
"""Cross-app heavy-GPU lease (contract v1, 2026-09-23).

Two apps on this PC share one GPU and one Ollama server (the same summary
model). Each of them "waited for free VRAM" on its own, and each slipped into
the gaps of the other's multi-call jobs, evicted the other's model, or loaded
a model that did not fit (the driver then pages it into system RAM). The fix
is one lease both apps honour, agreed between them as "contract v1":

1. Directory `C:/ProgramData/GpuLease/` (NOT AppData: processes started from a
   packaged app see a redirected AppData, which would split the lease into two
   files nobody shares).
2. `heavy.lock`: an exclusive lock on byte 0 (msvcrt.locking LK_NBLCK), held for
   a WHOLE heavy job and released by closing the file - also when the holder
   crashes. Heavy = loading the summary model, pyannote (+ its re-transcription),
   a batch Whisper decode, an A/B test. Dictation never takes the lease.
3. `heavy.json` (atomic replace), written by the holder right after acquiring:
   {"v":1, "app", "job", "detail", "pid", "since", "expect_s", "need_mib"};
   removed on release; a dead pid means stale.
4. Only the holder may unload an Ollama model, and only right after acquiring.
5. The holder waits for room with measured footprints, never the CPU.
6. A waiting app writes `want-<app>.json`; an app acquires only if no live
   want file of another app is older than its own.
7. `live.json` (another app's live meeting, `from`..`until`): no heavy job
   starts inside that window; stale 30 min after `until`.
8. `alive-<app>.json`, refreshed every 60 s: a visibility self-check.
9. No preemption. A holder past 2x its expect_s is reported, never killed.

This module is stdlib-only and knows nothing about Lia, so it can be tested on
its own (LIA_GPU_LEASE_DIR points it at a scratch directory).

Security (2026-09-26 audit F1): the lease is OPT-IN (enabled() - the parent
sets LIA_GPU_LEASE=1 for itself and its windows; a public install that never
shares its GPU creates no shared folder at all). When on, the folder is used
only if it is a real directory (no junction / symlink), owned by this user,
SYSTEM or Administrators, and grants write access to nobody else (inheritance
from C:/ProgramData is removed: Users may create files there by default).
Every file is schema-checked and size-capped, heavy.json counts only while
heavy.lock is really held, a pid we may not open is not a peer, and a wait is
bounded (LeaseTimeout).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import threading
import time

VERSION = 1
DEFAULT_DIR = r"C:\ProgramData\GpuLease"
ALIVE_EVERY_S = 60
ALIVE_STALE_S = 300
LIVE_STALE_AFTER_S = 1800
# An older request of the other app is honoured while the lock is FREE; one
# that leaves the free lock untaken this long is stuck (its owner is alive but
# not polling, or a removal was lost) and stops counting. The same rule and
# value as the other app's side, so both sides agree.
FREE_LOCK_YIELD_MAX_S = 300
# Hard bounds on what another process may make this app believe or do.
MAX_FILE_BYTES = 64 * 1024
MAX_LIVE_WINDOW_S = 12 * 3600          # a live.json longer / further out = invalid
MAX_NEED_MIB = 200000
MAX_EXPECT_S = 86400
DEFAULT_MAX_WAIT_S = 45 * 60           # a heavy job gives up after this (LeaseTimeout)
APP_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
JOB_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def enabled():
    """The lease is opt-in (Settings > Advanced; config gpu_lease_enabled). The
    main process sets LIA_GPU_LEASE=1 in its own environment, so every window
    process it starts inherits the choice."""
    return os.environ.get("LIA_GPU_LEASE") == "1"


def lease_dir():
    return os.environ.get("LIA_GPU_LEASE_DIR") or DEFAULT_DIR


def now_iso():
    # Milliseconds: two requests inside the same second must still be ordered
    # (whole seconds made them tie and race - found by the order test).
    return _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def parse_iso(s):
    """An aware datetime from an ISO 8601 string, or None."""
    try:
        d = _dt.datetime.fromisoformat(str(s))
        return d if d.tzinfo else d.astimezone()
    except Exception:
        return None


def pid_alive(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
        if not h:
            # Access denied = another user's (or a protected) process: never a
            # peer of this app - both apps run as the same user (audit F1: a
            # planted "pid": 4 used to read as a live holder forever).
            return False
        try:
            code = ctypes.c_ulong(0)
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259             # STILL_ACTIVE
            return False
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def read_json(path):
    """A small JSON object from the shared folder, or None (missing, too big,
    malformed, not an object)."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, encoding="utf-8") as f:
            d = json.loads(f.read(MAX_FILE_BYTES + 1))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _int_in(v, lo, hi):
    """int(v) when it is an int-like value inside [lo, hi], else None (a bool,
    a float with a fraction, a string of letters... are all invalid)."""
    if isinstance(v, bool):
        return None
    try:
        i = int(v)
    except Exception:
        return None
    if isinstance(v, float) and v != i:
        return None
    return i if lo <= i <= hi else None


def valid_heavy(d):
    """heavy.json -> a clean dict with typed fields, or None when invalid."""
    if not isinstance(d, dict):
        return None
    app, job = str(d.get("app") or ""), str(d.get("job") or "")
    pid = _int_in(d.get("pid"), 1, 2 ** 32)
    need = _int_in(d.get("need_mib") or 0, 0, MAX_NEED_MIB)
    exp = _int_in(d.get("expect_s") or 0, 0, MAX_EXPECT_S)
    if not APP_RE.match(app) or not JOB_RE.match(job) or None in (pid, need, exp):
        return None
    since = d.get("since")
    return {"v": d.get("v"), "app": app, "job": job,
            "detail": str(d.get("detail") or "")[:60], "pid": pid,
            "since": since if parse_iso(since) else None,
            "expect_s": exp, "need_mib": need}


def valid_want(d):
    if not isinstance(d, dict):
        return None
    pid = _int_in(d.get("pid"), 1, 2 ** 32)
    job = str(d.get("job") or "")
    if pid is None or not parse_iso(d.get("since")) or (job and not JOB_RE.match(job)):
        return None
    return {"pid": pid, "since": d.get("since"), "job": job or None,
            "need_mib": _int_in(d.get("need_mib") or 0, 0, MAX_NEED_MIB) or 0}


def valid_live(d, now=None):
    """live.json -> (from, until) when it is a sane window: until after from,
    at most MAX_LIVE_WINDOW_S long and ending at most that far ahead."""
    if not isinstance(d, dict):
        return None
    a, b = parse_iso(d.get("from")), parse_iso(d.get("until"))
    if not a or not b or b < a or (b - a).total_seconds() > MAX_LIVE_WINDOW_S:
        return None
    now = now or _dt.datetime.now().astimezone()
    if (b - now).total_seconds() > MAX_LIVE_WINDOW_S:
        return None
    return a, b


# ---- the folder: owner + DACL checks (Windows) -------------------------------
_SYSTEM_SID, _ADMINS_SID = "S-1-5-18", "S-1-5-32-544"
# CREATOR OWNER / OWNER RIGHTS name the (already checked) owner itself.
_OWNER_ALIASES = ("S-1-3-0", "S-1-3-4")
# Rights that let a principal plant, replace or delete files in the folder or
# change who may: add file / add subdir / delete child / DELETE / WRITE_DAC /
# WRITE_OWNER / GENERIC_WRITE / GENERIC_ALL / FILE_WRITE_DATA on files.
_WRITE_MASK = 0x2 | 0x4 | 0x40 | 0x10000 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000


def current_user_sid():
    """The string SID of this process's user, or None."""
    try:
        import ctypes
        from ctypes import wintypes
        adv, k32 = ctypes.WinDLL("advapi32"), ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE)]
        adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(tok)):
            return None
        try:
            need = wintypes.DWORD(0)
            adv.GetTokenInformation(tok, 1, None, 0, ctypes.byref(need))
            buf = ctypes.create_string_buffer(need.value)
            if not adv.GetTokenInformation(tok, 1, buf, need, ctypes.byref(need)):
                return None
            psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            return _sid_str(psid)
        finally:
            k32.CloseHandle(tok)
    except Exception:
        return None


def _sid_str(psid):
    import ctypes
    s = ctypes.c_wchar_p()
    if not ctypes.windll.advapi32.ConvertSidToStringSidW(ctypes.c_void_p(psid), ctypes.byref(s)):
        return None
    try:
        return s.value
    finally:
        ctypes.windll.kernel32.LocalFree(s)


def folder_security(path):
    """(owner_sid, [(sid, mask, ace_flags) for each ACCESS_ALLOWED ace]) of a
    directory, or None when it cannot be read."""
    try:
        import ctypes
        from ctypes import wintypes
        adv, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32
        owner, dacl, sd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        # SE_FILE_OBJECT = 1; OWNER (1) | DACL (4)
        rc = adv.GetNamedSecurityInfoW(ctypes.c_wchar_p(path), 1, 1 | 4,
                                       ctypes.byref(owner), None, ctypes.byref(dacl),
                                       None, ctypes.byref(sd))
        if rc != 0:
            return None
        try:
            osid = _sid_str(owner.value)
            aces = []
            if dacl.value:
                class ACL(ctypes.Structure):
                    _fields_ = [("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte),
                                ("AclSize", wintypes.WORD), ("AceCount", wintypes.WORD),
                                ("Sbz2", wintypes.WORD)]
                acl = ACL.from_address(dacl.value)
                for i in range(acl.AceCount):
                    pace = ctypes.c_void_p()
                    if not adv.GetAce(dacl, i, ctypes.byref(pace)):
                        continue
                    ace_type = ctypes.c_ubyte.from_address(pace.value).value
                    flags = ctypes.c_ubyte.from_address(pace.value + 1).value
                    if ace_type != 0:                   # ACCESS_ALLOWED_ACE_TYPE only
                        continue
                    mask = ctypes.c_uint32.from_address(pace.value + 4).value
                    aces.append((_sid_str(pace.value + 8), mask, flags))
            return osid, aces
        finally:
            k32.LocalFree(sd)
    except Exception:
        return None


def folder_problem(path, user_sid=None):
    """Why `path` must NOT be used as the lease folder (text), or None when it
    is safe: a real directory, owned by this user / SYSTEM / Administrators,
    and no other principal may write, delete or re-permission in it."""
    try:
        st = os.lstat(path)
    except OSError as e:
        return "cannot read it (%s)" % e
    if getattr(st, "st_file_attributes", 0) & 0x400:        # REPARSE_POINT
        return "it is a junction / symbolic link"
    if os.name != "nt":
        return None
    user_sid = user_sid or current_user_sid()
    sec = folder_security(path)
    if not user_sid or not sec:
        return "its owner / permissions cannot be read"
    owner, aces = sec
    trusted = {user_sid, _SYSTEM_SID, _ADMINS_SID}
    if owner not in trusted:
        return "it is owned by another account (%s)" % owner
    for sid, mask, _flags in aces:
        if sid in trusted or sid in _OWNER_ALIASES:
            continue
        if mask & _WRITE_MASK:
            return "another account (%s) may write in it" % sid
    return None


def _retry(fn, attempts=20, delay=0.05):
    """Windows refuses to delete/replace a file another process has open for
    reading (a sharing violation) - and the other app reads these files every
    few seconds. A lost removal left a want/heavy file behind that blocked the
    other app for good (found by the order test). Retry briefly."""
    for i in range(attempts):
        try:
            return fn()
        except FileNotFoundError:
            return None
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def write_json_atomic(path, obj):
    tmp = "%s.%s.tmp" % (path, secrets.token_hex(8))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    try:
        _retry(lambda: os.replace(tmp, path))
    except Exception:
        _remove(tmp)
        raise


def _remove(path):
    try:
        _retry(lambda: os.remove(path))
    except Exception:
        pass


def _hhmm(iso):
    d = parse_iso(iso)
    return d.strftime("%H:%M") if d else "?"


class LeaseCancelled(Exception):
    pass


class LeaseTimeout(Exception):
    """The GPU stayed busy longer than the job's max_wait_s."""


class Lease:
    """One app's side of the lease. Process-level and re-entrant: nested
    acquires inside the same process (a summary run and each of its calls)
    share one hold; the file lock is released when the outermost job ends."""

    def __init__(self, app, directory=None, log=None, clock=time.monotonic,
                 sleep=time.sleep):
        self.app = app
        self.dir = directory or lease_dir()
        self.log = log
        self._clock = clock
        self._sleep = sleep
        self._mu = threading.Lock()
        self._fh = None
        self._depth = 0
        self._info = None
        self._started = now_iso()
        self._warned = set()
        self._yield_seen = {}     # (app, since) -> when a FREE lock was first yielded
        self.ok = self._ensure_dir()

    # ---- paths -----------------------------------------------------------
    def _p(self, name):
        return os.path.join(self.dir, name)

    def _ensure_dir(self):
        """Create / check the folder. Refused (the lease is then off for this
        process, one WARNING) when it is a junction or symlink, owned by another
        account, or writable by another account - another user could otherwise
        steer this app through planted files (audit F1)."""
        try:
            os.makedirs(self.dir, exist_ok=True)
            why = folder_problem(self.dir)
            if why and "may write" in why:
                # Our own folder still carries C:/ProgramData's inherited
                # "Users may create files" (or the old Authenticated Users
                # grant): make it private, then check again.
                self._private_acl()
                why = folder_problem(self.dir)
            if why:
                self._warn_once("dir", "GPU lease: not using %s - %s; heavy jobs run "
                                "without the cross-app lease", self.dir, why)
                return False
            return True
        except Exception as e:
            self._warn_once("dir", "GPU lease unavailable (%s): %s - heavy jobs "
                            "run without the cross-app lease", self.dir, e)
            return False

    def _private_acl(self):
        """This user + SYSTEM + Administrators only, inherited by every file (an
        elevated process's files stay replaceable by the same user's
        non-elevated peer - the reason the old code granted Authenticated
        Users). Runs only on a folder that already passed the junction and
        owner checks. Best-effort."""
        user = current_user_sid()
        if os.name != "nt" or not user:
            return
        try:
            import subprocess
            icacls = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                  "System32", "icacls.exe")
            for args in (["/inheritance:r", "/grant:r", "*%s:(OI)(CI)F" % user,
                          "*%s:(OI)(CI)F" % _SYSTEM_SID, "*%s:(OI)(CI)F" % _ADMINS_SID],
                         ["/remove:g", "*S-1-5-11"],           # the pre-1.6.11 grant
                         ["/remove:g", "*S-1-5-32-545"]):      # BUILTIN\Users
                subprocess.run([icacls, self.dir] + args, capture_output=True,
                               timeout=15, creationflags=0x08000000)
        except Exception:
            pass

    def _warn_once(self, key, msg, *args):
        if key in self._warned:
            return
        self._warned.add(key)
        if self.log is not None:
            try:
                self.log.warning(msg, *args)
            except Exception:
                pass

    # ---- reading the shared state ------------------------------------------
    def holder(self):
        """heavy.json of a LIVE holder (any app, including this one), or None.
        Counts only when valid, its pid is a live process this user may open,
        and heavy.lock is really held (contract rule: the lock is the truth - a
        heavy.json next to a FREE lock is stale or planted)."""
        d = valid_heavy(read_json(self._p("heavy.json")))
        if not d or not pid_alive(d["pid"]):
            return None
        with self._mu:
            mine = self._fh is not None
        if mine and d["pid"] == os.getpid():
            return d
        fh = self._try_lock()
        if fh is not None:                       # nobody holds it: stale
            self._unlock(fh)
            return None
        return d

    def other_holder(self):
        """A live holder that is not THIS process: another app, or another
        process of this app (Lia's Ask / Chat windows are their own processes
        and take the lease under the same app name for their model calls)."""
        d = self.holder()
        if not d:
            return None
        if d.get("app") == self.app and d.get("pid") == os.getpid():
            return None
        return d

    def live_window(self, now=None):
        """live.json when now is inside its from..until window, else None."""
        d = read_json(self._p("live.json"))
        now = now or _dt.datetime.now().astimezone()
        win = valid_live(d, now)
        if not win:
            return None
        a, b = win
        if (now - b).total_seconds() > LIVE_STALE_AFTER_S:
            return None                         # stale
        return d if a <= now <= b else None

    def older_want(self, since):
        """The oldest live want file of ANOTHER app requested before `since`
        (an exact tie goes to the app whose name sorts first: deterministic,
        both sides compute the same answer)."""
        mine = parse_iso(since)
        best = None
        try:
            names = os.listdir(self.dir)
        except Exception:
            return None
        for n in names:
            if not (n.startswith("want-") and n.endswith(".json")):
                continue
            if n == "want-%s.json" % self.app:
                continue
            other = n[5:-5]
            if not APP_RE.match(other):
                continue
            d = valid_want(read_json(self._p(n)))
            if not d or not pid_alive(d["pid"]):
                continue
            t = parse_iso(d.get("since"))
            earlier = t and mine and (t < mine or (t == mine and other < self.app))
            if earlier and (best is None or t < parse_iso(best["since"])):
                best = dict(d, app=other)
        return best

    def held(self):
        with self._mu:
            return self._depth > 0

    # ---- the lock ------------------------------------------------------------
    def _try_lock(self):
        import msvcrt
        path = self._p("heavy.lock")
        try:
            if not os.path.exists(path):
                open(path, "ab").close()
            fh = open(path, "rb")                # read access is enough to lock
        except Exception:
            return None
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return fh
        except OSError:
            fh.close()
            return None

    def _unlock(self, fh):
        try:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    def blocked_reason(self, since):
        """Why this app may not take the lease now (human text), or None.
        The live window and an older request are hard gates; a holder is only
        TEXT - the file lock is the truth (a heavy.json whose lock is free is
        stale, e.g. left by a removal Windows refused)."""
        return self._gate(since) or self.holder_text()

    def _gate(self, since):
        live = self.live_window()
        if live:
            return ("another app's meeting bot is live until %s"
                    % _hhmm(live.get("until")))
        w = self.older_want(since)
        if w:
            return "waiting for %s's earlier request (%s)" % (
                w.get("app", "another app").capitalize(), w.get("job") or "a job")
        return None

    def _stuck_want(self, w):
        """True once the other app's older request has left a FREE lock
        untaken for FREE_LOCK_YIELD_MAX_S (then it no longer counts)."""
        key = (w.get("app"), w.get("since"))
        first = self._yield_seen.setdefault(key, self._clock())
        if self._clock() - first > FREE_LOCK_YIELD_MAX_S:
            self._warn_once("stuck:%s:%s" % key,
                            "GPU lease: %s has asked for the GPU since %s but left the "
                            "FREE lease untaken for %d s - no longer yielding to it",
                            key[0], key[1], FREE_LOCK_YIELD_MAX_S)
            return True
        return False

    def holder_text(self):
        h = self.other_holder()
        if h:
            txt = "%s is running %s since %s" % (
                str(h.get("app") or "another app").capitalize(),
                h.get("job") or "a job", _hhmm(h.get("since")))
            t = parse_iso(h.get("since"))
            exp = h.get("expect_s") or 0
            if t and exp:
                el = (_dt.datetime.now().astimezone() - t).total_seconds()
                txt += (" (over its expected %d min)" % round(exp / 60.0)
                        if el > 2 * exp else
                        " (about %d min)" % max(1, round((exp - el) / 60.0)))
            return txt
        return None

    def acquire(self, job, detail="", expect_s=300, need_mib=0, *, on_wait=None,
                should_cancel=None, cancel_exc=None, poll_s=3.0, wait=True,
                max_wait_s=DEFAULT_MAX_WAIT_S):
        """Take the lease for a heavy job (blocking; re-entrant inside this
        process). on_wait(text) gets "GPU busy - <why> m:ss" while waiting;
        should_cancel() -> raise cancel_exc (a Discard). Returns True when this
        call took the FILE lock (the outermost acquire), False when nested or
        when the lease directory is unavailable. wait=False: one try, None
        when the lease is not free right now (no want file is written).
        Raises LeaseTimeout after max_wait_s of waiting (None = no limit)."""
        with self._mu:
            if self._depth > 0:
                self._depth += 1
                return False
        if not self.ok:
            with self._mu:
                self._depth += 1
            return False
        since = now_iso()
        want = self._p("want-%s.json" % self.app)
        t0 = self._clock()
        last = None
        wrote_want = False
        try:
            while True:
                if should_cancel is not None and should_cancel():
                    raise (cancel_exc or LeaseCancelled)("cancelled while waiting for the GPU lease")
                live = self.live_window()
                fh = None
                if live:
                    why = ("another app's meeting bot is live until %s"
                           % _hhmm(live.get("until")))
                else:
                    # The lock first (it is the truth); an older request of the
                    # other app is then honoured only while the lock is FREE.
                    fh = self._try_lock()
                    why = None
                    if fh is not None:
                        w = self.older_want(since)
                        if w is not None and not self._stuck_want(w):
                            self._unlock(fh)
                            fh = None
                            why = "waiting for %s's earlier request (%s)" % (
                                w.get("app", "another app").capitalize(),
                                w.get("job") or "a job")
                    else:
                        why = self.holder_text() or "another app holds the GPU lease"
                if fh is not None:
                    break
                if not wait:
                    return None
                if max_wait_s is not None and self._clock() - t0 > max_wait_s:
                    raise LeaseTimeout("the GPU has been busy for %d min (%s)" % (
                        round((self._clock() - t0) / 60.0), why))
                try:        # (re)written every poll: a fresh want = a live waiter
                    write_json_atomic(want, {"v": VERSION, "pid": os.getpid(),
                                             "since": since, "job": job,
                                             "need_mib": int(need_mib or 0)})
                    if not wrote_want and self.log is not None:
                        self.log.info("GPU lease: %s waits - %s", job, why)
                    wrote_want = True
                except Exception:
                    pass
                note = "GPU busy - %s %s" % (why, _fmt(self._clock() - t0))
                if on_wait is not None and note != last:
                    last = note
                    try:
                        on_wait(note)
                    except Exception:
                        pass
                end = self._clock() + poll_s
                while self._clock() < end:
                    if should_cancel is not None and should_cancel():
                        raise (cancel_exc or LeaseCancelled)(
                            "cancelled while waiting for the GPU lease")
                    self._sleep(min(1.0, poll_s))
            info = {"v": VERSION, "app": self.app, "job": job,
                    "detail": (detail or "")[:60], "pid": os.getpid(),
                    "since": now_iso(), "expect_s": int(expect_s or 0),
                    "need_mib": int(need_mib or 0)}
            try:
                write_json_atomic(self._p("heavy.json"), info)
            except Exception as e:
                self._warn_once("heavy.json", "GPU lease: writing heavy.json failed: %s", e)
            with self._mu:
                self._fh, self._depth, self._info = fh, 1, info
            if self.log is not None:
                waited = self._clock() - t0
                self.log.info("GPU lease: taken for %s%s", job,
                              (" after %s" % _fmt(waited)) if waited >= 1 else "")
            return True
        finally:
            if wrote_want:
                _remove(want)

    def release(self):
        with self._mu:
            if self._depth <= 0:
                return
            self._depth -= 1
            if self._depth > 0:
                return
            fh, info = self._fh, self._info
            self._fh, self._info = None, None
        if fh is None:
            return
        try:
            d = valid_heavy(read_json(self._p("heavy.json")))
            if d and d["pid"] == os.getpid():
                _remove(self._p("heavy.json"))
        finally:
            self._unlock(fh)
        if self.log is not None and info:
            self.log.info("GPU lease: released (%s)", info.get("job"))

    # ---- visibility self-check (rule 8) --------------------------------------
    def heartbeat(self, version=""):
        if not self.ok:
            return
        try:
            write_json_atomic(self._p("alive-%s.json" % self.app),
                              {"v": VERSION, "pid": os.getpid(),
                               "since": self._started, "version": version,
                               "at": now_iso()})
        except Exception as e:
            self._warn_once("alive", "GPU lease: heartbeat failed: %s", e)

    def alive_age_s(self, app):
        """Seconds since `app` last refreshed its alive file (None = never).
        The contract's file carries pid/since/version only, so the refresh time
        is the file's mtime (a 60 s rewrite bumps it)."""
        try:
            return max(0.0, time.time() - os.path.getmtime(
                self._p("alive-%s.json" % app)))
        except Exception:
            return None


def _fmt(seconds):
    s = int(max(0, seconds))
    return "%d:%02d" % (s // 60, s % 60)


class NullLease:
    """The lease when it is off (the default): no folder, no files, nobody else
    to wait for. Same interface as Lease; nesting is still counted so held()
    and release() behave."""

    def __init__(self, app, log=None):
        self.app, self.log, self.dir, self.ok = app, log, "", False
        self._mu = threading.Lock()
        self._depth = 0

    def acquire(self, *a, **k):
        with self._mu:
            self._depth += 1
        return False

    def release(self):
        with self._mu:
            self._depth = max(0, self._depth - 1)

    def held(self):
        with self._mu:
            return self._depth > 0

    def holder(self):
        return None

    other_holder = holder

    def live_window(self, now=None):
        return None

    def older_want(self, since):
        return None

    def blocked_reason(self, since):
        return None

    def holder_text(self):
        return None

    def heartbeat(self, version=""):
        return None

    def alive_age_s(self, app):
        return None


def make(app, log=None):
    """The process's lease: a real Lease when the user turned it on, else a
    NullLease (no shared folder is ever created)."""
    return Lease(app, log=log) if enabled() else NullLease(app, log=log)
