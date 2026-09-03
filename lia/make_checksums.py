"""Write SHA256SUMS.txt for release artifacts (security audit WP4 #15).

So SECURITY.md's "each release lists SHA256 checksums" is produced by the build,
not promised by hand. Run it after building the Portable zip and the Setup.exe:

    python make_checksums.py installer_output
    python make_checksums.py Lia-Portable-1.3.1.zip installer_output\\Lia-Setup-1.3.1.exe

A directory contributes its top-level *.exe and *.zip. SHA256SUMS.txt is written
into the directory of the first artifact (the layout a release uploads).
"""
import hashlib
import os
import sys


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(paths):
    """Expand files + dirs into a de-duplicated, ordered list of artifacts."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if (name.lower().endswith((".exe", ".zip"))
                        and name != "SHA256SUMS.txt"):
                    files.append(os.path.join(p, name))
        elif os.path.isfile(p):
            files.append(p)
    seen, out = set(), []
    for f in files:
        a = os.path.abspath(f)
        if a not in seen:
            seen.add(a)
            out.append(f)
    return out


def write_sha256sums(paths, out_path=None):
    """Write '<sha256>  <name>' lines (sha256sum -c compatible). Returns
    (out_path, files)."""
    files = collect(paths)
    if not files:
        raise RuntimeError("no .exe/.zip artifacts found to checksum")
    lines = ["%s  %s" % (_sha256(f), os.path.basename(f)) for f in files]
    if out_path is None:
        out_path = os.path.join(os.path.dirname(os.path.abspath(files[0])),
                                "SHA256SUMS.txt")
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return out_path, files


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python make_checksums.py <file-or-dir> [...]")
        sys.exit(2)
    out, files = write_sha256sums(sys.argv[1:])
    for fp in files:
        print("  %s" % os.path.basename(fp))
    print("wrote %s (%d artifacts)" % (out, len(files)))
