"""Build Lia-Update-<version>.zip, the small update (plans/DELTA_UPDATE_PLAN_2026-11-06.md).

Run after build_runtime.py and the Portable zip, BEFORE make_checksums.py
(the pack is signed with the Setup and the Portable zip, in the same
SHA256SUMS.txt):

    python make_update_pack.py build_runtime 1.6.19 installer_output/Lia-Portable-1.6.15.zip ...
    python make_update_pack.py build_runtime 1.6.19 --auto installer_output

The pack = lia-update.json (every file of the new version: path, SHA-256,
size) + files/<path> for each file that differs from ANY of the base
releases (their published Portable zips). A copy of Lia on one of those
bases (with its files intact) updates from the pack alone; any other copy
falls back to the full Setup / Portable. --auto takes the newest BASES
(default 4) older Portable zips found in the folder.

Exit 3 = no pack (it would be larger than MAX_FRACTION of the full tree,
e.g. after a CPython bump): ship the release without one, every copy then
takes the full download as before."""
import hashlib
import json
import os
import re
import sys
import zipfile

import updater

BASES = 4
MAX_FRACTION = 0.40


def zip_manifest(path):
    """{posix path: [sha256, size]} of a published Portable zip's files."""
    out = {}
    with zipfile.ZipFile(path) as z:
        for i in z.infolist():
            if i.is_dir():
                continue
            h = hashlib.sha256()
            with z.open(i) as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            out[i.filename.replace("\\", "/")] = [h.hexdigest(), i.file_size]
    return out


def auto_bases(folder, version, n=BASES):
    """The newest n Portable zips in `folder` older than `version`."""
    new = updater.parse_version(version)
    found = []
    for name in os.listdir(folder):
        m = updater.PORTABLE_RE.match(name)
        if m and updater.parse_version(m.group(1)) < new:
            found.append((updater.parse_version(m.group(1)), os.path.join(folder, name)))
    return [p for _v, p in sorted(found, reverse=True)[:n]]


def build_pack(build_dir, version, base_zips, out_path, max_fraction=MAX_FRACTION):
    """Write the pack; returns a stats dict, or None when no pack is worth
    shipping (too large). Raises SystemExit on a bad build tree."""
    with open(os.path.join(build_dir, "app", "lia.py"), encoding="utf-8") as f:
        m = re.search(r'^APP_VERSION = "([0-9.]+)"', f.read(), re.M)
    if not m or m.group(1) != version:
        raise SystemExit("%s\\app\\lia.py is not Lia %s" % (build_dir, version))
    if not base_zips:
        raise SystemExit("no base releases - give their Portable zips")
    files = updater.tree_manifest(build_dir)
    bad = [r for r in files if not updater.valid_rel(r)]
    if bad:
        raise SystemExit("the build tree has paths the updater refuses: %s" % bad[:5])
    if "runtime/Lia.exe" not in files or "app/lia.py" not in files:
        raise SystemExit("%s is not a whole Lia build" % build_dir)
    packed, per_base = set(), {}
    for bz in base_zips:
        base = zip_manifest(bz)
        need = {r for r in files if base.get(r, [None])[0] != files[r][0]}
        per_base[os.path.basename(bz)] = {"files": len(need),
                                          "bytes": sum(files[r][1] for r in need)}
        packed |= need
    total = sum(v[1] for v in files.values())
    raw = sum(files[r][1] for r in packed)
    stats = {"version": version, "files": len(files), "total_bytes": total,
             "packed": len(packed), "packed_bytes": raw, "bases": per_base}
    if raw > max_fraction * total:
        stats["skipped"] = "the changed files are %.0f%% of the tree" % (100.0 * raw / total)
        return stats
    manifest = {"format": updater.DELTA_FORMAT, "version": version, "files": files,
                "bases": [os.path.basename(b) for b in base_zips]}
    tmp = out_path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr(updater.DELTA_MANIFEST, json.dumps(manifest, indent=0, sort_keys=True))
        for rel in sorted(packed):
            z.write(os.path.join(build_dir, *rel.split("/")), updater.DELTA_FILES + rel)
    os.replace(tmp, out_path)
    stats["pack_bytes"] = os.path.getsize(out_path)
    return stats


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    build_dir, version, rest = argv[0], argv[1], argv[2:]
    if rest[:1] == ["--auto"]:
        folder = rest[1] if len(rest) > 1 else "installer_output"
        bases = auto_bases(folder, version)
        out_dir = folder
    else:
        bases = rest
        out_dir = os.path.dirname(os.path.abspath(bases[0])) if bases else "."
    out = os.path.join(out_dir, "Lia-Update-%s.zip" % version)
    stats = build_pack(build_dir, version, bases, out)
    for name, s in stats["bases"].items():
        print("  from %-28s %4d files %7.2f MB" % (name, s["files"], s["bytes"] / 1e6))
    if stats.get("skipped"):
        print("NO PACK: %s - ship the release without one (full downloads)." % stats["skipped"])
        return 3
    print("wrote %s: %d of %d files, %.2f MB raw -> %.2f MB" % (
        out, stats["packed"], stats["files"], stats["packed_bytes"] / 1e6,
        stats["pack_bytes"] / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
