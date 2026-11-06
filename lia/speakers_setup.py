# -*- coding: utf-8 -*-
"""Lia - install, check and remove local speaker detection (NVIDIA Nemotron 3
Diarization). Stdlib + requests only: this runs inside the tray app.

What gets installed - by itself in the background the first time it is
needed, or on the Install click (Settings > Meetings > Speakers):
  1. the Python packages in speakers_requirements.lock (CPU torch + transformers),
     with pip --target --no-deps --require-hashes into
     %LOCALAPPDATA%\\Lia\\speakers\\pkgs-<lock hash>  (never into the app runtime);
  2. the model files from Hugging Face at a pinned revision, each checked against
     its sha256, into %LOCALAPPDATA%\\Lia\\speakers\\model-<revision>.
diarize_nemotron.py then runs in a subprocess with only that package folder on
sys.path. A finished step writes a marker file; a half-done step is never used.
"""

import hashlib
import os
import shutil
import subprocess
import threading
import time

MODEL_REPO = "nvidia/Nemotron-3-Diarization"
MODEL_REVISION = "f667ed73aee57d40cc39428eb768b4fd87a0a29e"
# name -> (sha256, bytes). model.safetensors = the HF LFS oid.
MODEL_FILES = {
    "config.json": ("dd795ec07ffa09acf18d231aa3fa8642aa5c521760953754e02559a7413b6873", 1458),
    "processor_config.json": ("91726746788a024a2f0f2f699a6c3667f0d74e6a0e9288fc44fe3018e23af68e", 623),
    "model.safetensors": ("c074d86335b3b794f8fa5edc25594558f128bdb3914d27806a3a5a2e44963cb6", 396954592),
}
PYTORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYPI_INDEX = "https://pypi.org/simple"
DOWNLOAD_MB = 650           # measured 2026-11-06: packages 263 MB + model 379 MB (~1.6 GB on disk)
_MARKER = ".lia-ok"

_HERE = os.path.dirname(os.path.abspath(__file__))
_lock = threading.Lock()
_progress = {"busy": False, "step": "", "pct": 0, "error": ""}


def root():
    """The speakers folder. LIA_SPEAKERS_DIR overrides it (tests)."""
    over = os.environ.get("LIA_SPEAKERS_DIR")
    if over:
        return over
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "Lia", "speakers")


def lock_path():
    return os.path.join(_HERE, "speakers_requirements.lock")


def script_path():
    return os.path.join(_HERE, "diarize_nemotron.py")


def _lock_tag():
    try:
        with open(lock_path(), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "missing"


def pkgs_dir():
    return os.path.join(root(), "pkgs-" + _lock_tag())


def model_dir():
    return os.path.join(root(), "model-" + MODEL_REVISION[:12])


def _done(path):
    return os.path.isfile(os.path.join(path, _MARKER))


def installed():
    """True when both steps finished (cheap: two marker files)."""
    return _done(pkgs_dir()) and _done(model_dir()) and os.path.isfile(script_path())


def status():
    with _lock:
        p = dict(_progress)
    p.update(installed=installed(), packages=_done(pkgs_dir()),
             model=_done(model_dir()), dir=root(), download_mb=DOWNLOAD_MB)
    return p


def _set(**kw):
    with _lock:
        _progress.update(kw)


def _pip_python(interp):
    """pip needs a console-capable python next to the given one (pythonw has no
    stdout); falls back to the given interpreter."""
    d = os.path.dirname(interp or "")
    cand = os.path.join(d, "python.exe")
    return cand if os.path.isfile(cand) else interp


def _install_packages(interp, log, cancel):
    dest = pkgs_dir()
    if _done(dest):
        return
    part = dest + ".part"
    shutil.rmtree(part, ignore_errors=True)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    args = [_pip_python(interp), "-m", "pip", "install",
            "--no-deps", "--require-hashes", "--only-binary=:all:",
            "--no-input", "--disable-pip-version-check", "--no-warn-script-location",
            "--target", part,
            "--index-url", PYTORCH_CPU_INDEX, "--extra-index-url", PYPI_INDEX,
            "-r", lock_path()]
    log_path = os.path.join(root(), "install.log")
    with open(log_path, "ab") as lf:
        lf.write(("\n== %s pip install ==\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
        lf.flush()
        proc = subprocess.Popen(args, stdout=lf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                # no window, below-normal priority (a background job)
                                creationflags=0x08000000 | 0x00004000)
        while proc.poll() is None:
            if cancel is not None and cancel():
                proc.kill()
                proc.wait(10)
                shutil.rmtree(part, ignore_errors=True)
                raise RuntimeError("cancelled")
            time.sleep(0.5)
    if proc.returncode != 0:
        tail = ""
        try:
            with open(log_path, "rb") as f:
                lines = f.read().decode("utf-8", "replace").strip().splitlines()
            tail = next((l for l in reversed(lines) if l.strip()), "")
        except OSError:
            pass
        shutil.rmtree(part, ignore_errors=True)
        raise RuntimeError("package install failed (pip exit %s)%s"
                           % (proc.returncode, (": " + tail[:160]) if tail else ""))
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(part, dest)
    with open(os.path.join(dest, _MARKER), "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    if log:
        log("Speaker detection: packages installed at %s" % dest)


def _download_model(log, cancel, on_pct):
    import requests
    dest = model_dir()
    if _done(dest):
        return
    os.makedirs(dest, exist_ok=True)
    total = sum(size for _h, size in MODEL_FILES.values())
    got_before = 0
    for name, (sha, size) in MODEL_FILES.items():
        final = os.path.join(dest, name)
        if os.path.isfile(final) and os.path.getsize(final) == size \
                and _sha256_file(final) == sha:
            got_before += size
            continue
        url = "https://huggingface.co/%s/resolve/%s/%s" % (MODEL_REPO, MODEL_REVISION, name)
        part = final + ".part"
        h = hashlib.sha256()
        got = 0
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if cancel is not None and cancel():
                        raise RuntimeError("cancelled")
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if on_pct:
                        on_pct(int(100 * (got_before + got) / total))
        if got != size or h.hexdigest() != sha:
            _remove(part)
            raise RuntimeError("model file %s failed its check (%d bytes, sha256 %s...)"
                               % (name, got, h.hexdigest()[:12]))
        os.replace(part, final)
        got_before += size
    with open(os.path.join(dest, _MARKER), "w", encoding="utf-8") as f:
        f.write(MODEL_REVISION)
    if log:
        log("Speaker detection: model ready at %s" % dest)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _prune_old():
    """Drop package / model folders of an older lock or revision."""
    keep = {os.path.basename(pkgs_dir()), os.path.basename(model_dir())}
    try:
        for n in os.listdir(root()):
            if (n.startswith("pkgs-") or n.startswith("model-")) and n not in keep:
                shutil.rmtree(os.path.join(root(), n), ignore_errors=True)
    except OSError:
        pass


def install(interp, log=None, cancel=None):
    """Blocking install (call it on a worker thread). Returns (ok, message).
    Progress is readable meanwhile through status()."""
    with _lock:
        if _progress["busy"]:
            return False, "Already installing."
        _progress.update(busy=True, step="packages", pct=0, error="")
    try:
        os.makedirs(root(), exist_ok=True)
        _install_packages(interp, log, cancel)
        _set(step="model", pct=0)
        _download_model(log, cancel, lambda p: _set(pct=p))
        _prune_old()
        _set(busy=False, step="", pct=100, error="")
        return True, "Speaker detection is ready."
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        _set(busy=False, step="", error=msg)
        if log:
            log("Speaker detection install failed: %s" % msg)
        return False, msg


def remove():
    """Delete everything speaker detection installed. Returns (ok, message)."""
    if status().get("busy"):
        return False, "Installing - try again when it ends."
    shutil.rmtree(root(), ignore_errors=True)
    _set(error="", pct=0)
    return (not os.path.exists(root())), "Speaker detection removed."
