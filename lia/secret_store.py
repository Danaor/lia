# -*- coding: utf-8 -*-
"""Lia - DPAPI-backed secret helpers (Windows, per-user).

API keys / tokens at rest (config.json, child-window payload files) are stored
as "dpapi:<base64>" blobs encrypted with the Windows per-user master key
(CryptProtectData). Any process of the SAME Windows user can decrypt them -
including de-elevated child windows (DPAPI is keyed to the user, not the
token's integrity level) - while other users and offline file theft cannot.
This is NOT a defense against malware already running as the same user
(stated plainly in SECURITY.md).

protect() is a pure function: on DPAPI failure it returns its input unchanged
(so it can never corrupt a key in memory). The FAIL-CLOSED enforcement lives
at the write boundary: lia.save_config refuses to persist a secret that did
not come back protected - a key is never written to disk in plaintext.
unprotect() of a blob that cannot be decrypted HERE (another machine/user)
returns "" - the blob is unusable by definition, the app must boot anyway.
"""

from __future__ import annotations

import base64

PREFIX = "dpapi:"
_ENTROPY = None   # per-user scope only; no extra entropy (children must decrypt)

try:
    import win32crypt  # pywin32 (a hard dependency of the app)
    _HAVE_DPAPI = True
except Exception:  # pragma: no cover - non-Windows dev machines
    win32crypt = None
    _HAVE_DPAPI = False


def available():
    """True when DPAPI is usable in this process."""
    return _HAVE_DPAPI


def is_protected(text):
    return isinstance(text, str) and text.startswith(PREFIX)


def protect(text):
    """Encrypt a secret for storage. '' / None / already-protected values and
    non-strings pass through; on any DPAPI failure the plaintext is returned
    (fail-open: never lose a key to an encryption hiccup)."""
    if not text or not isinstance(text, str) or is_protected(text):
        return text
    if not _HAVE_DPAPI:
        return text
    try:
        blob = win32crypt.CryptProtectData(
            text.encode("utf-8"), "Lia", _ENTROPY, None, None, 0)
        return PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception:
        return text


def unprotect(text):
    """Decrypt a stored secret. Plaintext (no prefix) passes through unchanged
    - that is the automatic migration path for pre-encryption configs. A blob
    that cannot be decrypted on this machine/user returns ''."""
    if not is_protected(text):
        return text
    if not _HAVE_DPAPI:
        return ""
    try:
        raw = base64.b64decode(text[len(PREFIX):])
        _desc, data = win32crypt.CryptUnprotectData(raw, _ENTROPY, None, None, 0)
        return data.decode("utf-8")
    except Exception:
        return ""
