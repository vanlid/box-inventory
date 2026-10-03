"""Minimal WebAuthn (passkey) verification using only the Python standard library.

Supports what passkeys use in practice: "none"/self attestation (we don't verify
attestation statements, only the credential), ES256 (ECDSA P-256) and RS256 keys.
Only signature *verification* is done here: no private keys, so no secrets to leak.
"""
import base64
import hashlib
import json


class WebAuthnError(Exception):
    pass


def b64u_decode(s):
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64u_encode(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ---------- CBOR (definite-length subset used by WebAuthn) ----------

def cbor_decode(data, pos=0, depth=0):
    """Decode one CBOR item at pos; returns (value, next_pos)."""
    if depth > 16:
        raise WebAuthnError("CBOR nested too deeply")
    if pos >= len(data):
        raise WebAuthnError("truncated CBOR")
    ib = data[pos]
    major, info = ib >> 5, ib & 31
    pos += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        n = 1 << (info - 24)
        if pos + n > len(data):
            raise WebAuthnError("truncated CBOR")
        arg = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    else:
        raise WebAuthnError("unsupported CBOR encoding")
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if pos + arg > len(data):
            raise WebAuthnError("truncated CBOR")
        raw = data[pos:pos + arg]
        if major == 3:
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise WebAuthnError("bad CBOR text")
        return raw, pos + arg
    if major == 4:
        out = []
        for _ in range(arg):
            v, pos = cbor_decode(data, pos, depth + 1)
            out.append(v)
        return out, pos
    if major == 5:
        out = {}
        for _ in range(arg):
            k, pos = cbor_decode(data, pos, depth + 1)
            if not isinstance(k, (int, str)):
                raise WebAuthnError("bad CBOR map key")
            v, pos = cbor_decode(data, pos, depth + 1)
            out[k] = v
        return out, pos
    if major == 7:
        if info in (20, 21):
            return info == 21, pos
        if info == 22:
            return None, pos
    raise WebAuthnError("unsupported CBOR type")


# ---------- ECDSA P-256 verification ----------

P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
A = P - 3
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
     0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)


def _on_curve(pt):
    x, y = pt
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A * x + B)) % P == 0


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return x3, (lam * (x1 - x3) - y1) % P


def _mul(k, pt):
    out = None
    while k:
        if k & 1:
            out = _add(out, pt)
        pt = _add(pt, pt)
        k >>= 1
    return out


def _der_ints(sig):
    """Parse a DER ECDSA signature SEQUENCE { INTEGER r, INTEGER s }."""
    def length(pos):
        n = sig[pos]
        if n < 0x80:
            return n, pos + 1
        k = n & 0x7f
        if k not in (1, 2):
            raise WebAuthnError("bad DER length")
        return int.from_bytes(sig[pos + 1:pos + 1 + k], "big"), pos + 1 + k
    try:
        if sig[0] != 0x30:
            raise WebAuthnError("bad DER signature")
        total, pos = length(1)
        if pos + total != len(sig):
            raise WebAuthnError("bad DER signature")
        vals = []
        for _ in range(2):
            if sig[pos] != 0x02:
                raise WebAuthnError("bad DER signature")
            n, pos = length(pos + 1)
            vals.append(int.from_bytes(sig[pos:pos + n], "big"))
            pos += n
        if pos != len(sig):
            raise WebAuthnError("bad DER signature")
        return vals
    except IndexError:
        raise WebAuthnError("bad DER signature")


def verify_es256(x, y, signature, message):
    q = (x, y)
    if not _on_curve(q):
        return False
    r, s = _der_ints(signature)
    if not (1 <= r < N and 1 <= s < N):
        return False
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = pow(s, -1, N)
    pt = _add(_mul(e * w % N, G), _mul(r * w % N, q))
    return pt is not None and pt[0] % N == r


# ---------- RSA PKCS#1 v1.5 / SHA-256 verification ----------

_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def verify_rs256(n, e, signature, message):
    k = (n.bit_length() + 7) // 8
    if len(signature) != k or k < 256:  # require >= 2048-bit keys
        return False
    m = pow(int.from_bytes(signature, "big"), e, n).to_bytes(k, "big")
    digest = _SHA256_PREFIX + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest
    return m == expected


# ---------- COSE keys ----------

def cose_to_stored(cose):
    """Validate a COSE public key and return a JSON-storable form."""
    kty, alg = cose.get(1), cose.get(3)
    if kty == 2 and alg == -7 and cose.get(-1) == 1:
        x, y = cose.get(-2), cose.get(-3)
        if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == 32 and len(y) == 32):
            raise WebAuthnError("bad EC key")
        if not _on_curve((int.from_bytes(x, "big"), int.from_bytes(y, "big"))):
            raise WebAuthnError("EC key not on curve")
        return {"alg": -7, "x": b64u_encode(x), "y": b64u_encode(y)}
    if kty == 3 and alg == -257:
        n, e = cose.get(-1), cose.get(-2)
        if not (isinstance(n, bytes) and isinstance(e, bytes)) or len(n) < 256:
            raise WebAuthnError("bad RSA key")
        return {"alg": -257, "n": b64u_encode(n), "e": b64u_encode(e)}
    raise WebAuthnError("unsupported key type (need ES256 or RS256)")


def verify_signature(stored, signature, message):
    if stored["alg"] == -7:
        return verify_es256(int.from_bytes(b64u_decode(stored["x"]), "big"),
                            int.from_bytes(b64u_decode(stored["y"]), "big"), signature, message)
    if stored["alg"] == -257:
        return verify_rs256(int.from_bytes(b64u_decode(stored["n"]), "big"),
                            int.from_bytes(b64u_decode(stored["e"]), "big"), signature, message)
    return False


# ---------- ceremonies ----------

def _client_data(raw, expected_type, challenge, origin):
    try:
        cd = json.loads(raw)
    except ValueError:
        raise WebAuthnError("bad clientDataJSON")
    if cd.get("type") != expected_type:
        raise WebAuthnError("wrong ceremony type")
    if cd.get("challenge") != challenge:
        raise WebAuthnError("challenge mismatch")
    if cd.get("origin") != origin:
        raise WebAuthnError("origin mismatch")
    if cd.get("crossOrigin"):
        raise WebAuthnError("cross-origin request")
    return cd


def _auth_data(ad, rp_id):
    if len(ad) < 37:
        raise WebAuthnError("authenticator data too short")
    if ad[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise WebAuthnError("wrong relying party")
    flags = ad[32]
    if not flags & 0x01:
        raise WebAuthnError("user not present")
    return flags, int.from_bytes(ad[33:37], "big")


def _guard(fn):
    """Malformed input of any kind is a rejected ceremony, never a crash."""
    def wrapped(*a):
        try:
            return fn(*a)
        except WebAuthnError:
            raise
        except (ValueError, TypeError, IndexError, KeyError, AttributeError) as e:
            raise WebAuthnError("malformed passkey data") from e
    return wrapped


@_guard
def verify_registration(client_data_b64, attestation_b64, challenge, origin, rp_id):
    """Returns {"id": b64url credential id, "key": stored key, "signCount": int}."""
    raw_cd = b64u_decode(client_data_b64)
    _client_data(raw_cd, "webauthn.create", challenge, origin)
    att, _ = cbor_decode(b64u_decode(attestation_b64))
    if not isinstance(att, dict) or not isinstance(att.get("authData"), bytes):
        raise WebAuthnError("bad attestation object")
    ad = att["authData"]
    flags, sign_count = _auth_data(ad, rp_id)
    if not flags & 0x40:
        raise WebAuthnError("no credential data")
    if len(ad) < 55:
        raise WebAuthnError("authenticator data too short")
    cred_len = int.from_bytes(ad[53:55], "big")
    cred_id = ad[55:55 + cred_len]
    if len(cred_id) != cred_len or not 16 <= cred_len <= 1023:
        raise WebAuthnError("bad credential id")
    cose, _ = cbor_decode(ad, 55 + cred_len)
    if not isinstance(cose, dict):
        raise WebAuthnError("bad public key")
    return {"id": b64u_encode(cred_id), "key": cose_to_stored(cose), "signCount": sign_count}


@_guard
def verify_assertion(client_data_b64, auth_data_b64, signature_b64, challenge, origin, rp_id, stored_key):
    """Verifies a login; returns the authenticator's signCount."""
    raw_cd = b64u_decode(client_data_b64)
    _client_data(raw_cd, "webauthn.get", challenge, origin)
    ad = b64u_decode(auth_data_b64)
    _, sign_count = _auth_data(ad, rp_id)
    if not verify_signature(stored_key, b64u_decode(signature_b64), ad + hashlib.sha256(raw_cd).digest()):
        raise WebAuthnError("bad signature")
    return sign_count
