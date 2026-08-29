"""Assemble a full-runtime Lia distribution.

Replaces the frozen PyInstaller exe with a private CPython runtime + the app
sources. Everything works: Settings, all pywebview windows, subprocess mic
recorder, diarization (when the speaker pack is installed), file dialogs.

Usage:
    python build_runtime.py          # assemble to build_runtime/
    python build_runtime.py --clean  # wipe and rebuild from scratch

The output directory is ready for Inno Setup (installer.iss) or a straight
zip (portable).

Requirements for the build machine:
- Python 3.13.x (matching the embeddable pin below) with tkinter installed
- pip, internet access (first run downloads the embeddable zip)
- Inno Setup 6 (for the installer, optional)
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(HERE, "build_runtime")
RUNTIME_DIR = os.path.join(BUILD_DIR, "runtime")
APP_DIR = os.path.join(BUILD_DIR, "app")

# ---------- pinned downloads ----------

# CPython 3.13.7 embeddable (amd64). Update both URL and hash together.
PYTHON_VERSION = "3.13.7"
PYTHON_EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)
# SHA-256 of the zip (verify after bumping version).
PYTHON_EMBED_SHA256 = ""  # TODO: fill on first download, then pin

GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# ---------- helpers ----------

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url, dest):
    print(f"  Downloading {url}")
    urllib.request.urlretrieve(url, dest)
    print(f"  -> {os.path.getsize(dest) / 1048576:.1f} MB")


def _run(args, **kw):
    """Run a subprocess, fail loudly on error."""
    print(f"  $ {' '.join(args[:4])}{'...' if len(args) > 4 else ''}")
    r = subprocess.run(args, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        print(r.stdout[-2000:] if r.stdout else "")
        print(r.stderr[-2000:] if r.stderr else "")
        raise RuntimeError(f"Command failed (exit {r.returncode}): {args[0]}")
    return r


def _python(*args):
    """Run a command under the assembled runtime's python."""
    return _run([os.path.join(RUNTIME_DIR, "python.exe"), *args])


# ---------- phases ----------

def phase_1_extract():
    """Download and extract the embeddable CPython."""
    print("\n=== Phase 1: CPython embeddable ===")
    cache_dir = os.path.join(HERE, "build")
    os.makedirs(cache_dir, exist_ok=True)
    zip_name = f"python-{PYTHON_VERSION}-embed-amd64.zip"
    zip_path = os.path.join(cache_dir, zip_name)

    if not os.path.exists(zip_path):
        _download(PYTHON_EMBED_URL, zip_path)
    else:
        print(f"  Using cached {zip_path}")

    sha = _sha256(zip_path)
    if PYTHON_EMBED_SHA256 and sha != PYTHON_EMBED_SHA256:
        raise RuntimeError(
            f"SHA-256 mismatch for {zip_name}:\n"
            f"  expected: {PYTHON_EMBED_SHA256}\n"
            f"  got:      {sha}"
        )
    elif not PYTHON_EMBED_SHA256:
        print(f"  SHA-256 (pin this): {sha}")

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(RUNTIME_DIR)
    print(f"  Extracted to {RUNTIME_DIR}")


def phase_2_configure_pth():
    """Write the ._pth file so the runtime finds stdlib + site-packages."""
    print("\n=== Phase 2: configure ._pth ===")
    pth = os.path.join(RUNTIME_DIR, f"python{PYTHON_VERSION.replace('.', '')[:3]}._pth")
    # Find the actual ._pth file (e.g. python313._pth)
    for f in os.listdir(RUNTIME_DIR):
        if f.endswith("._pth"):
            pth = os.path.join(RUNTIME_DIR, f)
            break
    with open(pth, "w") as f:
        f.write("python313.zip\n.\nLib\nLib\\site-packages\nimport site\n")
    print(f"  Wrote {pth}")


def phase_3_graft_tkinter():
    """Copy tkinter from the build machine's full Python install."""
    print("\n=== Phase 3: graft tkinter ===")
    base = sys.base_prefix  # e.g. C:\...\Python313
    # Verify version match
    build_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if build_ver != PYTHON_VERSION:
        raise RuntimeError(
            f"Build Python is {build_ver} but embeddable pin is {PYTHON_VERSION}. "
            f"They must match for the tkinter graft."
        )
    # Copy tkinter package
    tk_src = os.path.join(base, "Lib", "tkinter")
    tk_dst = os.path.join(RUNTIME_DIR, "Lib", "tkinter")
    os.makedirs(tk_dst, exist_ok=True)
    shutil.copytree(tk_src, tk_dst, dirs_exist_ok=True)
    # Copy _tkinter.pyd + ALL DLLs from the installed Python's DLLs dir.
    # The embeddable zip ships its own copies of some (libcrypto, sqlite3,
    # etc.) but tkinter needs tcl86t/tk86t/zlib1 which it does NOT include.
    # Copying the full set is safe: same version, same build, and the
    # embeddable copies are identical or get overwritten harmlessly.
    dlls_src = os.path.join(base, "DLLs")
    for item in os.listdir(dlls_src):
        src = os.path.join(dlls_src, item)
        if os.path.isfile(src) and (item.endswith(".pyd") or item.endswith(".dll")):
            if item.startswith("_test"):
                continue  # skip CPython test modules
            shutil.copy2(src, RUNTIME_DIR)
            print(f"  Copied {item}")
    # Copy tcl/tk data
    tcl_src = os.path.join(base, "tcl")
    if os.path.isdir(tcl_src):
        shutil.copytree(tcl_src, os.path.join(RUNTIME_DIR, "tcl"),
                        dirs_exist_ok=True)
        print("  Copied tcl/ data tree")
    # Verify
    _python("-c", "import tkinter; print('  tkinter graft verified')")


def phase_4_install_deps():
    """Bootstrap pip and install requirements.lock."""
    print("\n=== Phase 4: install dependencies ===")
    os.makedirs(os.path.join(RUNTIME_DIR, "Lib", "site-packages"), exist_ok=True)
    # get-pip.py
    get_pip = os.path.join(BUILD_DIR, "get-pip.py")
    if not os.path.exists(get_pip):
        _download(GET_PIP_URL, get_pip)
    _python(get_pip, "--no-warn-script-location")
    # Install from lock file
    lock = os.path.join(HERE, "requirements.lock")
    _python("-m", "pip", "install", "-r", lock,
            "--no-warn-script-location", "--quiet")
    # Count
    r = _python("-m", "pip", "list", "--format=columns")
    n = len(r.stdout.strip().splitlines()) - 2  # header lines
    print(f"  {n} packages installed")


def phase_5_create_launcher():
    """Create Lia.exe as a copy of pythonw.exe (unsigned copy keeps PSF sig)."""
    print("\n=== Phase 5: create launcher ===")
    pythonw = os.path.join(RUNTIME_DIR, "pythonw.exe")
    launcher = os.path.join(RUNTIME_DIR, "Lia.exe")
    if not os.path.exists(pythonw):
        raise RuntimeError("pythonw.exe not found in runtime")
    shutil.copy2(pythonw, launcher)
    print(f"  Created {launcher} ({os.path.getsize(launcher)} bytes)")


def phase_6_copy_sources():
    """Copy app sources to the build tree."""
    print("\n=== Phase 6: copy app sources ===")
    os.makedirs(APP_DIR, exist_ok=True)
    # Use git archive to respect .gitattributes export-ignore
    repo_root = os.path.dirname(HERE)
    try:
        archive = os.path.join(BUILD_DIR, "app_sources.tar")
        _run(["git", "archive", "--format=tar", "--prefix=", "-o", archive,
              "HEAD", "--", "lia/"], cwd=repo_root)
        import tarfile
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                # Strip the lia/ prefix
                if member.name.startswith("lia/"):
                    member.name = member.name[4:]
                    tf.extract(member, APP_DIR)
        os.remove(archive)
        print(f"  Exported sources via git archive")
    except Exception as e:
        # Fallback: direct copy (includes private files but works without git)
        print(f"  git archive failed ({e}), falling back to direct copy")
        for item in os.listdir(HERE):
            src = os.path.join(HERE, item)
            if item in ("build", "build_runtime", "dist", "installer_output",
                        "__pycache__", "build_installer.py"):
                continue
            dst = os.path.join(APP_DIR, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        print(f"  Copied sources directly")
    # Also copy top-level files
    for f in ("README.md", "LICENSE"):
        src = os.path.join(repo_root, f)
        if os.path.exists(src):
            shutil.copy2(src, BUILD_DIR)


def phase_7_prune():
    """Remove unnecessary files to reduce size."""
    print("\n=== Phase 7: prune ===")
    removed = 0
    for root, dirs, files in os.walk(RUNTIME_DIR):
        # Remove __pycache__ dirs
        for d in list(dirs):
            if d == "__pycache__":
                p = os.path.join(root, d)
                shutil.rmtree(p)
                dirs.remove(d)
                removed += 1
        # Remove .pyc in non-zip locations (site-packages has them)
        for f in files:
            if f.endswith(".pyc"):
                os.remove(os.path.join(root, f))
                removed += 1
    print(f"  Removed {removed} __pycache__ dirs / .pyc files")


def phase_8_smoke_test():
    """Run critical import tests and the full suite."""
    print("\n=== Phase 8: smoke tests ===")
    # Import tests
    _python("-c", "import tkinter; print('  tkinter OK')")
    _python("-c", "import webview; print('  webview OK')")
    _python("-c", "import win32api; print('  win32api OK')")
    _python("-c", "import keyboard; print('  keyboard OK')")
    _python("-c", "import pyaudio; print('  pyaudio OK')")
    _python("-c", "import pystray; print('  pystray OK')")
    _python("-c", "import faster_whisper; print('  faster_whisper OK')")
    _python("-c", "import numpy; print('  numpy OK')")

    # Full suite
    print("  Running test suite...")
    test_appdata = tempfile.mkdtemp(prefix="lia_build_test_")
    try:
        env = os.environ.copy()
        env["LIA_SKIP_LIVE"] = "1"
        env["APPDATA"] = test_appdata
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            [os.path.join(RUNTIME_DIR, "python.exe"), "-X", "utf8",
             os.path.join(APP_DIR, "run_tests.py")],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=300
        )
        # Extract summary
        for line in r.stdout.splitlines():
            if "passed" in line or "FAIL" in line or "GREEN" in line:
                print(f"  {line.strip()}")
        if r.returncode != 0:
            print(r.stderr[-1000:] if r.stderr else "")
            raise RuntimeError("Test suite failed")
    finally:
        shutil.rmtree(test_appdata, ignore_errors=True)


def phase_9_report():
    """Print the final size report."""
    print("\n=== Build complete ===")
    total = 0
    for root, dirs, files in os.walk(BUILD_DIR):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    rt_size = 0
    for root, dirs, files in os.walk(RUNTIME_DIR):
        for f in files:
            rt_size += os.path.getsize(os.path.join(root, f))
    app_size = 0
    if os.path.isdir(APP_DIR):
        for root, dirs, files in os.walk(APP_DIR):
            for f in files:
                app_size += os.path.getsize(os.path.join(root, f))

    print(f"  Runtime:  {rt_size / 1048576:.1f} MB")
    print(f"  App:      {app_size / 1048576:.1f} MB")
    print(f"  Total:    {total / 1048576:.1f} MB (installed)")
    print(f"\n  Output:   {BUILD_DIR}")
    print(f"  Next:     python build_installer.py  (or ISCC installer.iss)")


# ---------- main ----------

def main():
    if "--clean" in sys.argv and os.path.isdir(BUILD_DIR):
        print("Cleaning previous build...")
        shutil.rmtree(BUILD_DIR)

    if os.path.isdir(RUNTIME_DIR) and os.listdir(RUNTIME_DIR):
        print(f"Build dir exists ({BUILD_DIR}). Use --clean to rebuild.")
        print("Running smoke tests on existing build...")
        phase_8_smoke_test()
        phase_9_report()
        return

    phase_1_extract()
    phase_2_configure_pth()
    phase_3_graft_tkinter()
    phase_4_install_deps()
    phase_5_create_launcher()
    phase_6_copy_sources()
    phase_7_prune()
    phase_8_smoke_test()
    phase_9_report()


if __name__ == "__main__":
    main()
