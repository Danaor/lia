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

# Release signing (security audit 2026-09-26 F4): SHA256SUMS.txt is signed with
# an Ed25519 key that lives ONLY on the release machine (DPAPI-protected at
# DEFAULT_KEY), and updater.py refuses an update whose list is not signed by
# the pinned public key.
DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".lia-release", "ed25519.key")


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


def generate_key(path=DEFAULT_KEY):
    """Create the release key (32 random bytes) DPAPI-protected at `path`
    (refuses to overwrite). Returns the PUBLIC key hex - pin it in
    updater.PUBLIC_KEY_HEX. Back the key file up offline: a lost key means a
    new key and one manual update for every install."""
    import secrets
    import lia_ed25519
    import secret_store
    if os.path.exists(path):
        raise SystemExit("refusing to overwrite an existing key: %s" % path)
    seed = secrets.token_bytes(32)
    blob = secret_store.protect(seed.hex())
    if not secret_store.is_protected(blob):
        raise SystemExit("DPAPI is unavailable - the key was NOT written")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(blob)
    return lia_ed25519.public_key(seed).hex()


def _load_key(path=DEFAULT_KEY):
    import secret_store
    seed_hex = secret_store.unprotect(open(path, encoding="utf-8").read().strip())
    seed = bytes.fromhex(seed_hex)
    if len(seed) != 32:
        raise SystemExit("the release key could not be read (wrong user / machine?)")
    return seed


def sign_sums(sums_path, key_path=DEFAULT_KEY):
    """Write <sums_path>.sig = hex Ed25519 signature of the file's exact bytes."""
    import lia_ed25519
    seed = _load_key(key_path)
    data = open(sums_path, "rb").read()
    sig = lia_ed25519.sign(seed, data)
    assert lia_ed25519.verify(lia_ed25519.public_key(seed), data, sig)
    with open(sums_path + ".sig", "w", encoding="utf-8", newline="\n") as f:
        f.write(sig.hex() + "\n")
    return sums_path + ".sig", lia_ed25519.public_key(seed).hex()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--genkey"]:
        print("public key:", generate_key(args[1] if len(args) > 1 else DEFAULT_KEY))
        sys.exit(0)
    sign = "--sign" in args
    if sign:
        args.remove("--sign")
    if not args:
        print("usage: python make_checksums.py [--sign] <file-or-dir> [...]\n"
              "       python make_checksums.py --genkey [key-path]")
        sys.exit(2)
    out, files = write_sha256sums(args)
    for fp in files:
        print("  %s" % os.path.basename(fp))
    print("wrote %s (%d artifacts)" % (out, len(files)))
    if sign:
        sig, pub = sign_sums(out)
        print("signed -> %s (public key %s...)" % (sig, pub[:16]))
