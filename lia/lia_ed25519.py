# -*- coding: utf-8 -*-
"""Ed25519 (RFC 8032) - pure Python, stdlib only.

Used to verify the signature on a release's SHA256SUMS.txt before Lia installs
an update (security audit 2026-09-26 F4): the download's SHA-256 came from the
same GitHub response as the file, so a hijacked GitHub account could publish a
malicious release that passed every check. The public key is pinned in
updater.py; the private key never leaves the release machine.

This is the reference algorithm from RFC 8032 section 6 (slow but plenty for a
single signature per update), checked against the RFC's test vectors in the
suite. sign() is here for the release tool (make_checksums.py --sign).
"""
import hashlib

_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493


def _sha512(b):
    return hashlib.sha512(b).digest()


def _inv(x):
    return pow(x, _p - 2, _p)


_d = -121665 * _inv(121666) % _p
_SQRT_M1 = pow(2, (_p - 1) // 4, _p)


def _sha512_modq(b):
    return int.from_bytes(_sha512(b), "little") % _q


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F, G * H, F * G, E * H)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    if (P[0] * Q[2] - Q[0] * P[2]) % _p != 0:
        return False
    return (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _recover_x(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * _inv(_d * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _SQRT_M1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * _inv(5) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _compress(P):
    zinv = _inv(P[2])
    x, y = P[0] * zinv % _p, P[1] * zinv % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


def _expand(secret):
    if len(secret) != 32:
        raise ValueError("an Ed25519 private key is 32 bytes")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(secret):
    """The 32-byte public key of a 32-byte private seed."""
    a, _ = _expand(secret)
    return _compress(_mul(a, _G))


def sign(secret, msg):
    """A 64-byte signature of `msg` (bytes)."""
    a, prefix = _expand(secret)
    A = _compress(_mul(a, _G))
    r = _sha512_modq(prefix + msg)
    Rs = _compress(_mul(r, _G))
    h = _sha512_modq(Rs + A + msg)
    s = (r + h * a) % _q
    return Rs + int.to_bytes(s, 32, "little")


def verify(public, msg, signature):
    """True only when `signature` (64 bytes) is `public`'s (32 bytes) signature
    of `msg`. Never raises on bad input - returns False."""
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        A = _decompress(public)
        R = _decompress(signature[:32])
        if not A or not R:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= _q:
            return False
        h = _sha512_modq(signature[:32] + public + msg)
        return _equal(_mul(s, _G), _add(R, _mul(h, A)))
    except Exception:
        return False
