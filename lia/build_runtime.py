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
        # ../app lets sibling-module imports (lang_pack, ui_kit, etc.) work
        # regardless of the working directory. The embeddable ._pth overrides
        # Python's default script-directory injection into sys.path.
        f.write("python313.zip\n.\nLib\nLib\\site-packages\n..\\app\nimport site\n")
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
    # get-pip.py lives in the build CACHE dir (build/), never in the
    # shipped tree (it showed up in the portable zip root - 2026-08-29).
    cache_dir = os.path.join(HERE, "build")
    os.makedirs(cache_dir, exist_ok=True)
    get_pip = os.path.join(cache_dir, "get-pip.py")
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


def _create_shortcut(lnk_path, target, icon, desc=""):
    """Create a Windows .lnk shortcut using PowerShell COM."""
    import subprocess
    # PowerShell one-liner that creates a .lnk via WScript.Shell COM
    ps = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$s = $ws.CreateShortcut("{lnk_path}"); '
        f'$s.TargetPath = "{target}"; '
        f'$s.WorkingDirectory = "{os.path.dirname(target)}"; '
        f'$s.IconLocation = "{icon}, 0"; '
        f'$s.Description = "{desc}"; '
        f'$s.WindowStyle = 7; '  # minimized (hides the cmd flash)
        f'$s.Save()'
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   check=True, capture_output=True)


def phase_5_create_launcher():
    """Create Lia.exe as a copy of pythonw.exe, stamped with the Lia icon
    and version resource so it looks like Lia in Explorer, Task Manager,
    and the tray icon list."""
    print("\n=== Phase 5: create launcher ===")
    pythonw = os.path.join(RUNTIME_DIR, "pythonw.exe")
    launcher = os.path.join(RUNTIME_DIR, "Lia.exe")
    if not os.path.exists(pythonw):
        raise RuntimeError("pythonw.exe not found in runtime")
    shutil.copy2(pythonw, launcher)
    print(f"  Created {launcher} ({os.path.getsize(launcher)} bytes)")
    # Stamp version resource (FileDescription = "Lia" in Task Manager)
    try:
        from PyInstaller.utils.win32.versioninfo import (
            VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
            StringStruct, VarFileInfo, VarStruct,
            write_version_info_to_executable)
        v = tuple(int(x) for x in PYTHON_VERSION.split(".")[0:2]) + (1, 0)
        vi = VSVersionInfo(
            ffi=FixedFileInfo(filevers=v, prodvers=v, mask=0x3f,
                              flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
            kids=[
                StringFileInfo([StringTable('040904B0', [
                    StringStruct('CompanyName', 'Naor Daniel'),
                    StringStruct('FileDescription',
                                 'Lia - Local Inference Assistant'),
                    StringStruct('FileVersion', '1.0.1'),
                    StringStruct('InternalName', 'Lia'),
                    StringStruct('OriginalFilename', 'Lia.exe'),
                    StringStruct('ProductName', 'Lia'),
                    StringStruct('ProductVersion', '1.0.1'),
                ])]),
                VarFileInfo([VarStruct('Translation', [0x0409, 1200])]),
            ],
        )
        write_version_info_to_executable(launcher, vi)
        print("  Stamped version resource")
    except Exception as e:
        print(f"  Version stamp skipped ({e})")
    # Replace the icon with lia.ico
    ico_path = os.path.join(HERE, "lia.ico")
    if os.path.exists(ico_path):
        try:
            _replace_exe_icon(launcher, ico_path)
            print("  Embedded lia.ico into launcher")
        except Exception as e:
            print(f"  Icon embed failed ({e}) - launcher uses default Python icon")


def _replace_exe_icon(exe_path, ico_path):
    """Replace the main icon in an exe with the contents of an .ico file.
    Uses the Win32 UpdateResource API via ctypes."""
    import ctypes
    from ctypes import wintypes

    # Read .ico file: header (6 bytes) + entries (16 bytes each) + image data
    with open(ico_path, "rb") as f:
        ico_data = f.read()

    # Parse ICO header
    reserved, ico_type, num_images = (
        int.from_bytes(ico_data[0:2], 'little'),
        int.from_bytes(ico_data[2:4], 'little'),
        int.from_bytes(ico_data[4:6], 'little'),
    )
    if ico_type != 1:
        raise ValueError(f"Not an ICO file (type={ico_type})")

    # Parse directory entries
    entries = []
    for i in range(num_images):
        off = 6 + i * 16
        entry = ico_data[off:off + 16]
        width = entry[0] or 256
        height = entry[1] or 256
        color_count = entry[2]
        planes = int.from_bytes(entry[4:6], 'little')
        bit_count = int.from_bytes(entry[6:8], 'little')
        data_size = int.from_bytes(entry[8:12], 'little')
        data_offset = int.from_bytes(entry[12:16], 'little')
        entries.append((width, height, color_count, planes, bit_count,
                        data_size, data_offset))

    # Win32 API with proper prototypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.BeginUpdateResourceW.argtypes = [ctypes.c_wchar_p, ctypes.c_bool]
    kernel32.BeginUpdateResourceW.restype = ctypes.c_void_p
    kernel32.UpdateResourceW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_ushort, ctypes.c_void_p, ctypes.c_uint]
    kernel32.UpdateResourceW.restype = ctypes.c_bool
    kernel32.EndUpdateResourceW.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    kernel32.EndUpdateResourceW.restype = ctypes.c_bool

    RT_ICON = 3
    RT_GROUP_ICON = 14

    handle = kernel32.BeginUpdateResourceW(exe_path, False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    # Write each icon image as RT_ICON resource (IDs 1, 2, 3, ...)
    for i, (w, h, cc, planes, bpp, size, offset) in enumerate(entries):
        img = ico_data[offset:offset + size]
        ok = kernel32.UpdateResourceW(
            handle, RT_ICON, i + 1, 0x0409, img, len(img))
        if not ok:
            kernel32.EndUpdateResourceW(handle, True)
            raise ctypes.WinError(ctypes.get_last_error())

    # Build GRPICONDIR structure for RT_GROUP_ICON
    grp = bytearray()
    grp += (0).to_bytes(2, 'little')  # reserved
    grp += (1).to_bytes(2, 'little')  # type = ICO
    grp += num_images.to_bytes(2, 'little')
    for i, (w, h, cc, planes, bpp, size, offset) in enumerate(entries):
        grp += bytes([w & 0xFF])       # width (0 = 256)
        grp += bytes([h & 0xFF])       # height
        grp += bytes([cc])             # color count
        grp += bytes([0])              # reserved
        grp += planes.to_bytes(2, 'little')
        grp += bpp.to_bytes(2, 'little')
        grp += size.to_bytes(4, 'little')
        grp += (i + 1).to_bytes(2, 'little')  # nID = resource ID

    ok = kernel32.UpdateResourceW(
        handle, RT_GROUP_ICON, 1, 0x0409, bytes(grp), len(grp))
    if not ok:
        kernel32.EndUpdateResourceW(handle, True)
        raise ctypes.WinError(ctypes.get_last_error())

    if not kernel32.EndUpdateResourceW(handle, False):
        raise ctypes.WinError(ctypes.get_last_error())


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
    # Create a root launcher so users can double-click to start
    bat_path = os.path.join(BUILD_DIR, "Lia.bat")
    with open(bat_path, "w") as f:
        f.write('@echo off\ncd /d "%~dp0app"\nstart "" "%~dp0runtime\\Lia.exe" "%~dp0app\\lia.py"\n')
    print("  Created Lia.bat (root launcher)")
    # NO build-time .lnk: shortcuts store ABSOLUTE paths, so one created
    # here is broken (and icon-less) on every user's machine (2026-08-29
    # field report). The app itself creates/refreshes Lia.lnk on first run
    # with paths correct for THAT machine - see _ensure_portable_shortcut.


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
