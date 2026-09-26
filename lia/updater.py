# -*- coding: utf-8 -*-
"""Lia self-update: find a newer public release and install it.

The whole app updates as one unit (new models, prompts and fixes ship inside a
release), so there is no per-model update path. Flow:

  fetch_latest()        GET the latest public GitHub release (drafts and
                        pre-releases are never offered).
  is_newer()            compare its tag with the running APP_VERSION.
  install_kind()        "installer" (Setup.exe install - can update itself),
                        "portable" (the zip - the user downloads the new zip),
                        "source" (a git checkout - never checks).
  download_verified()   stream the Setup.exe to a temp file and refuse it
                        unless its SHA-256 equals the digest GitHub computed
                        for the uploaded asset.
  installer_command()   Setup /SILENT: Inno keeps the previous folder + tasks,
                        closes the running app, and (since 1.6.6) relaunches
                        Lia after a silent install.

Stdlib + requests only; no UI here (lia.py owns the notices / Settings)."""
from __future__ import annotations

import hashlib
import os
import re

RELEASES_API = "https://api.github.com/repos/Danaor/lia/releases/latest"
# Every Lia release's SHA256SUMS.txt is signed with this Ed25519 key (security
# audit 2026-09-26 F4); an update installs only when the signed list names the
# Setup's hash AND GitHub's digest agrees. The private key never leaves the
# release machine.
PUBLIC_KEY_HEX = "10f2fdffe8de7aee7cab08244a22ccd4ee774668c524654e7b333d8b08b4765d"
DOWNLOAD_PREFIX = "https://github.com/Danaor/lia/releases/download/"
SUMS_NAME = "SHA256SUMS.txt"
RELEASES_PAGE = "https://github.com/Danaor/lia/releases/latest"
SETUP_RE = re.compile(r"^Lia-Setup-(\d+\.\d+\.\d+)\.exe$", re.I)
PORTABLE_RE = re.compile(r"^Lia-Portable-(\d+\.\d+\.\d+)\.zip$", re.I)


def parse_version(s):
    """'v1.6.6' / '1.6.6' -> (1, 6, 6); anything else -> None."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (s or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(remote, local):
    r, l = parse_version(remote), parse_version(local)
    return bool(r and l and r > l)


def _asset(a):
    digest = (a.get("digest") or "").lower()
    sha = digest[7:] if digest.startswith("sha256:") else ""
    return {"name": a.get("name", ""), "url": a.get("browser_download_url", ""),
            "size": int(a.get("size") or 0),
            "sha256": sha if re.fullmatch(r"[0-9a-f]{64}", sha) else ""}


def parse_release(data):
    """The GitHub release JSON -> the fields Lia needs, or None when it is not
    an offerable release (draft / pre-release / no version tag)."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    ver = parse_version(data.get("tag_name"))
    if ver is None:
        return None
    version = "%d.%d.%d" % ver
    out = {"version": version, "tag": data.get("tag_name", ""),
           "url": data.get("html_url", ""), "notes": (data.get("body") or "")[:4000],
           "published": data.get("published_at", ""), "setup": None, "portable": None,
           "sums": None, "sums_sig": None}
    for a in data.get("assets") or []:
        name = a.get("name", "")
        if name == SUMS_NAME:
            out["sums"] = _asset(a)
        elif name == SUMS_NAME + ".sig":
            out["sums_sig"] = _asset(a)
        for kind, rx in (("setup", SETUP_RE), ("portable", PORTABLE_RE)):
            m = rx.match(name)
            if m and m.group(1) == version:
                out[kind] = _asset(a)
    return out


def fetch_latest(current_version, session=None, timeout=10):
    """The latest public release, parsed (see parse_release), or None when the
    check fails for any reason (offline, rate limit, bad JSON)."""
    import requests
    s = session or requests
    r = s.get(RELEASES_API, timeout=(5, timeout),
              headers={"Accept": "application/vnd.github+json",
                       "User-Agent": "Lia/%s (update check)" % current_version})
    if r.status_code != 200:
        return None
    return parse_release(r.json())


def install_kind(app_dir):
    """How this copy of Lia was installed. `app_dir` = the folder holding
    lia.py. An installed / portable copy is <root>\\app\\lia.py next to
    <root>\\runtime\\Lia.exe; the Setup.exe install also has an uninstaller."""
    app_dir = os.path.abspath(app_dir)
    root = os.path.dirname(app_dir)
    if (os.path.basename(app_dir).lower() != "app"
            or not os.path.isfile(os.path.join(root, "runtime", "Lia.exe"))):
        return "source"
    try:
        if any(re.fullmatch(r"unins\d+\.exe", f, re.I) for f in os.listdir(root)):
            return "installer"
    except OSError:
        pass
    return "portable"


class VerifyError(Exception):
    """The download is not the file GitHub published (or has no digest)."""


def _get_small(url, session=None, cap=64 * 1024, timeout=20):
    import requests
    s = session or requests
    with s.get(url, stream=True, timeout=(10, timeout),
               headers={"User-Agent": "Lia (update check)"}) as r:
        r.raise_for_status()
        data = b""
        for chunk in r.iter_content(chunk_size=16384):
            data += chunk
            if len(data) > cap:
                raise VerifyError("%s is larger than expected" % url.rsplit("/", 1)[-1])
        return data


def authorize(rel, session=None, public_key_hex=None):
    """Check that `rel`'s Setup is one the release key signed, BEFORE it is
    downloaded: SHA256SUMS.txt + .sig from the SAME release, the Ed25519
    signature valid for the pinned key, the Setup's line in that list equal to
    GitHub's digest, and every URL under this repo's download path for the
    release's tag. Returns the expected SHA-256; raises VerifyError."""
    import lia_ed25519
    setup, sums, sig = rel.get("setup"), rel.get("sums"), rel.get("sums_sig")
    if not setup or not setup.get("sha256"):
        raise VerifyError("the release has no verifiable installer")
    if not sums or not sig:
        raise VerifyError("the release is not signed (no %s.sig)" % SUMS_NAME)
    prefix = "%s%s/" % (DOWNLOAD_PREFIX, rel.get("tag") or ("v" + rel.get("version", "")))
    for a in (setup, sums, sig):
        if not (a.get("url") or "").startswith(prefix):
            raise VerifyError("unexpected download address for %s" % a.get("name"))
    key_hex = public_key_hex or PUBLIC_KEY_HEX
    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        raise VerifyError("this build has no release key")
    data = _get_small(sums["url"], session)
    try:
        signature = bytes.fromhex(_get_small(sig["url"], session).decode("ascii").strip())
    except (ValueError, UnicodeDecodeError):
        raise VerifyError("the release signature is malformed")
    if not lia_ed25519.verify(key, data, signature):
        raise VerifyError("the release signature is NOT valid - update refused")
    listed = {}
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            listed[parts[1].lstrip("*")] = parts[0].lower()
    want = listed.get(setup["name"])
    if not want:
        raise VerifyError("%s is not in the signed checksum list" % setup["name"])
    if want != setup["sha256"]:
        raise VerifyError("GitHub's checksum differs from the signed one - refused")
    return want


def download_verified(asset, dest_dir, session=None, progress=None, timeout=60):
    """Stream `asset` into dest_dir and return its path - only when its SHA-256
    equals the digest GitHub recorded for the upload. A mismatch, a missing
    digest or a short download deletes the file and raises VerifyError."""
    import requests
    if not asset or not asset.get("url") or not asset.get("name"):
        raise VerifyError("the release has no installer")
    if not asset.get("sha256"):
        raise VerifyError("GitHub lists no checksum for %s" % asset["name"])
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, os.path.basename(asset["name"]))
    part = path + ".part"
    h, done, total = hashlib.sha256(), 0, int(asset.get("size") or 0)
    s = session or requests
    try:
        with s.get(asset["url"], stream=True, timeout=(10, timeout),
                   headers={"User-Agent": "Lia (update download)"}) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        if total and done != total:
            raise VerifyError("download incomplete (%d of %d bytes)" % (done, total))
        if h.hexdigest() != asset["sha256"]:
            raise VerifyError("checksum mismatch for %s" % asset["name"])
        os.replace(part, path)
        return path
    except BaseException:
        for p in (part, path):
            try:
                os.remove(p)
            except OSError:
                pass
        raise


def system_dir():
    """C:\\Windows\\System32 from the OS itself (GetSystemDirectoryW) - never
    from %ComSpec% / %SystemRoot%, which a user can redefine in HKCU\\Environment
    (audit 2026-09-26 F5)."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260)
        if 0 < n < 260:
            return buf.value
    except Exception:
        pass
    return r"C:\Windows\System32"


class HeldFile:
    """The verified installer, held open so no other process can change,
    rename or delete it until Setup has started (audit F5: it used to be
    verified, closed, and run later by path from a writable folder)."""

    def __init__(self, path):
        import ctypes
        import msvcrt
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        # GENERIC_READ, FILE_SHARE_READ only (no write / delete sharing),
        # OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
        h = k32.CreateFileW(path, 0x80000000, 0x1, None, 3, 0x80, None)
        if h in (None, wintypes.HANDLE(-1).value, -1):
            raise VerifyError("cannot hold %s open (%d)" % (os.path.basename(path),
                                                           ctypes.get_last_error()))
        self.path = path
        self._f = os.fdopen(msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY), "rb")

    def sha256(self):
        h = hashlib.sha256()
        self._f.seek(0)
        for chunk in iter(lambda: self._f.read(1 << 20), b""):
            h.update(chunk)
        return h.hexdigest()

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


def hold_and_verify(path, sha256):
    """Open `path` so nothing can change it, re-hash THOSE bytes, and return the
    HeldFile (keep it until Setup has started). Raises VerifyError."""
    held = HeldFile(path)
    if held.sha256() != (sha256 or "").lower():
        held.close()
        raise VerifyError("the installer changed after it was verified")
    return held


def installer_command(setup_path, log_path=None):
    """Silent in-place upgrade. Inno reuses the previous install folder and
    task choices and, from 1.6.6 on, relaunches Lia after a silent install.
    /FORCECLOSEAPPLICATIONS: a process from the install folder that the
    Restart Manager cannot close politely (a windowless pythonw: the serve
    child, a mic worker, Lia still exiting) otherwise ABORTS a silent setup
    (exit 5, measured 2026-09-24 on the real 1.6.6 Setup)."""
    # /LIAUPDATE=1: only an in-app update relaunches Lia after a silent
    # install (an admin's /SILENT deployment, e.g. as SYSTEM, must not start
    # Lia - audit F19; installer.iss IsLiaUpdate).
    cmd = [setup_path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
           "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS", "/LIAUPDATE=1"]
    if log_path:
        cmd.append("/LOG=%s" % log_path)
    return cmd


def _q(arg):
    """Quote one argument for cmd.exe (Windows paths never contain quotes). A
    '%' is refused: cmd expands %VAR% even inside quotes (audit F22)."""
    arg = str(arg)
    if "%" in arg or '"' in arg:
        raise VerifyError("a path with %% or a quote cannot be passed to cmd.exe: %s" % arg)
    return '"%s"' % arg if (not arg or any(c in arg for c in ' \t&()^|<>,;=')) else arg


def update_launch_line(setup_cmd, fallback_cmd, fallback_cwd=None):
    """ONE cmd.exe command line (a string - cmd does not parse Python's list
    quoting): run Setup and wait for it; if it fails (exit >= 1: aborted,
    cancelled, could not replace a file) start the PREVIOUS Lia again with
    fallback_cmd, so a failed update never leaves the user without Lia (Lia
    has already quit). cmd.exe lives in System32 and holds no file in the
    Lia folder, so Setup never needs to close it."""
    comspec = os.path.join(system_dir(), "cmd.exe")
    run = " ".join(_q(a) for a in setup_cmd)
    back = 'start "" ' + ('/D %s ' % _q(fallback_cwd) if fallback_cwd else "") + \
        " ".join(_q(a) for a in fallback_cmd)
    return '%s /d /s /c "%s & if errorlevel 1 %s"' % (_q(comspec), run, back)
