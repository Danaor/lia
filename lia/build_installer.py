"""Build the Lia installer (Setup.exe).

Pipeline:
    1. Run PyInstaller (build.py) to produce dist/Lia.exe.
       Skipped if dist/Lia.exe already exists and --skip-build is passed,
       OR if the existing exe is newer than lia.py (incremental rebuild).
    2. Run Inno Setup's ISCC.exe on installer.iss.
    3. Print the path to installer_output/Lia-Setup-<version>.exe.

Output:
    installer_output/Lia-Setup-<version>.exe — single-file installer
    that any user can run to install Lia (Next/Next/Finish wizard).

Usage:
    python build_installer.py             # full build (PyInstaller + ISCC)
    python build_installer.py --skip-build # skip PyInstaller, just compile installer
"""
import os
import subprocess
import sys
import shutil
from pathlib import Path

from make_checksums import write_sha256sums

HERE = Path(__file__).resolve().parent
DIST_EXE = HERE / "dist" / "Lia.exe"
ISS_FILE = HERE / "installer.iss"
OUT_DIR = HERE / "installer_output"
SOURCE_PY = HERE / "lia.py"

# Standard locations of the Inno Setup compiler.
ISCC_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
]


def find_iscc() -> Path:
    """Locate the Inno Setup compiler. Exit cleanly if not installed."""
    for c in ISCC_CANDIDATES:
        if c.is_file():
            return c
    # Fall back to PATH
    on_path = shutil.which("ISCC.exe") or shutil.which("iscc")
    if on_path:
        return Path(on_path)
    print()
    print("  ERROR: Inno Setup is not installed.")
    print()
    print("  Download and install (free, ~10MB):")
    print("    https://jrsoftware.org/isdl.php")
    print()
    print("  Or via Chocolatey (admin):")
    print("    choco install innosetup -y")
    print()
    sys.exit(1)


def needs_rebuild() -> bool:
    """True if dist/Lia.exe is missing or older than lia.py."""
    if not DIST_EXE.is_file():
        return True
    try:
        return SOURCE_PY.stat().st_mtime > DIST_EXE.stat().st_mtime
    except OSError:
        return True


def run_pyinstaller():
    """Run build.py to produce dist/Lia.exe."""
    print("=" * 60)
    print("  Step 1/2: PyInstaller — building Lia.exe")
    print("=" * 60)
    rc = subprocess.call([sys.executable, str(HERE / "build.py")], cwd=str(HERE))
    if rc != 0:
        print(f"\n  ERROR: PyInstaller exited with code {rc}")
        sys.exit(rc)
    if not DIST_EXE.is_file():
        print(f"\n  ERROR: PyInstaller succeeded but {DIST_EXE} is missing")
        sys.exit(1)
    size_mb = DIST_EXE.stat().st_size / (1024 * 1024)
    print(f"\n  [OK] Built {DIST_EXE.name} ({size_mb:.0f} MB)")


def run_iscc(iscc: Path):
    """Compile installer.iss with ISCC.exe."""
    print()
    print("=" * 60)
    print("  Step 2/2: Inno Setup — compiling Setup.exe")
    print("=" * 60)
    OUT_DIR.mkdir(exist_ok=True)
    rc = subprocess.call(
        [str(iscc), "/Qp", str(ISS_FILE)],
        cwd=str(HERE),
    )
    if rc != 0:
        print(f"\n  ERROR: ISCC exited with code {rc}")
        sys.exit(rc)
    # Find the produced installer
    setups = sorted(OUT_DIR.glob("Lia-Setup-*.exe"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not setups:
        print(f"\n  ERROR: No installer produced in {OUT_DIR}")
        sys.exit(1)
    setup = setups[0]
    size_mb = setup.stat().st_size / (1024 * 1024)
    print()
    print("=" * 60)
    print(f"  [OK] Installer ready: {setup.name} ({size_mb:.0f} MB)")
    print(f"     {setup}")
    print("=" * 60)
    print()
    print("  Send this single .exe to anyone — they double-click it,")
    print("  click Next/Next/Finish, and Lia is installed.")
    print()


def main():
    skip_build = "--skip-build" in sys.argv

    if not ISS_FILE.is_file():
        print(f"  ERROR: {ISS_FILE} not found")
        sys.exit(1)

    iscc = find_iscc()
    print(f"  Inno Setup compiler: {iscc}")
    print()

    if skip_build:
        if not DIST_EXE.is_file():
            print(f"  ERROR: --skip-build given but {DIST_EXE} is missing")
            sys.exit(1)
        print(f"  Skipping PyInstaller (--skip-build); using existing {DIST_EXE.name}")
    elif needs_rebuild():
        run_pyinstaller()
    else:
        size_mb = DIST_EXE.stat().st_size / (1024 * 1024)
        print(f"  [OK] {DIST_EXE.name} is up to date ({size_mb:.0f} MB) — skipping PyInstaller")
        print(f"    (delete {DIST_EXE.name} or edit lia.py to force a rebuild)")

    run_iscc(iscc)

    # SHA256SUMS.txt for the release (audit WP4 #15): covers the Setup.exe and
    # any Portable zip dropped into installer_output. SECURITY.md's checksum
    # promise is now produced by the build, not by hand.
    try:
        out, files = write_sha256sums([str(OUT_DIR)])
        print("  [OK] %s (%d artifacts):" % (Path(out).name, len(files)))
        for fp in files:
            print("     %s" % Path(fp).name)
    except Exception as e:
        print("  (checksums step skipped: %s)" % e)


if __name__ == "__main__":
    main()
