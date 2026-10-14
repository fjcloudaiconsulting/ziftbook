"""TOTP for platform admins (ZIF-71): RFC 6238 codes, the secret's encryption, and proofs.

RFC 6238 with SHA-1, 6 digits and 30-second steps, which every authenticator app supports. These
parameters are a contract: changing them means every admin re-enrols.

No secret, code or ticket ever reaches SQL as itself, only as proof(value): Postgres logs bind
parameters with any logged statement, and the database stores sha256 of the proof, so no function
takes the value it stores (migration 0039).
"""

import base64
import hashlib
import hmac
import os
import secrets
import struct
from datetime import datetime
from typing import Literal
from urllib.parse import quote, urlencode
from uuid import UUID

import segno
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import SecondFactorSettings

ISSUER = "ZiftBook"
STEP = 30  # seconds
VERSION = b"\x01"  # the key version, the first byte of every stored ciphertext
NONCE = 12
KEY_BYTES = 32


def new_secret() -> bytes:
    return secrets.token_bytes(20)


def code(secret: bytes, step: int) -> str:
    """The 6-digit code for one 30-second step (RFC 4226 dynamic truncation)."""
    digest = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 1_000_000).zfill(6)


def matching_step(secret: bytes, typed: str, now: datetime) -> int | None:
    """The step among t-1, t and t+1 whose code is `typed`, or None (also for non-ASCII text, which
    `\\d` and str.isdigit accept but can never be a code)."""
    if not typed.isascii():
        return None
    t = int(now.timestamp()) // STEP
    for step in (t - 1, t, t + 1):
        if hmac.compare_digest(code(secret, step).encode(), typed.encode("ascii")):
            return step
    return None


def proof(value: bytes | str) -> bytes:
    """sha256 of the value: the only form in which a secret, code or ticket reaches SQL."""
    if isinstance(value, str):
        # surrogatepass: a lone surrogate must not turn into a 500 here (as limits.email_key).
        value = value.encode("utf-8", "surrogatepass")
    return hashlib.sha256(value).digest()


def new_code() -> str:
    """80 random bits as 16 base32 characters, XXXX-XXXX-XXXX-XXXX: tickets and recovery codes."""
    raw = base64.b32encode(secrets.token_bytes(10)).decode()
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def normalise(typed: str) -> str:
    return typed.upper().replace(" ", "").replace("-", "")


def key() -> bytes | None:
    """ZIF_TOTP_KEY decoded, read at call time; None when it is unset or not base64 of 32 bytes."""
    value = SecondFactorSettings().totp_key.get_secret_value()
    if not value:
        return None
    try:
        # validate=True: otherwise stray characters are silently dropped.
        decoded = base64.b64decode(value, validate=True)
    except ValueError:  # binascii.Error, and a plain ValueError for non-ASCII text
        return None
    return decoded if len(decoded) == KEY_BYTES else None


def key_state() -> Literal["set", "unset", "invalid"]:
    """Never the key itself."""
    if not SecondFactorSettings().totp_key.get_secret_value():
        return "unset"
    return "set" if key() else "invalid"


# shortcut: one key; rotation = enrol each admin.
# Add a keyring on the version byte when admins are many.
def _aead() -> AESGCM:
    found = key()
    if found is None:
        raise RuntimeError("ZIF_TOTP_KEY is unset or invalid")
    return AESGCM(found)


def encrypt(secret: bytes, user_id: UUID) -> bytes:
    """VERSION || nonce || ciphertext and tag, bound to its user by the AAD."""
    # A fresh nonce every call: a repeated GCM nonce under one key leaks the plaintext and the
    # authentication key.
    nonce = os.urandom(NONCE)
    return VERSION + nonce + _aead().encrypt(nonce, secret, user_id.bytes)


def decrypt(blob: bytes, user_id: UUID) -> bytes:
    """Raises on an unknown version byte, and InvalidTag on a wrong key, user or a tampered blob."""
    if blob[:1] != VERSION:
        raise ValueError("unknown TOTP key version")
    return _aead().decrypt(blob[1 : 1 + NONCE], blob[1 + NONCE :], user_id.bytes)


def otpauth_uri(secret: bytes, email: str) -> str:
    """What an authenticator app scans: the key URI format every app reads."""
    query = urlencode(
        {
            "secret": base64.b32encode(secret).decode(),
            "issuer": ISSUER,
            "algorithm": "SHA1",
            "digits": 6,
            "period": STEP,
        }
    )
    return f"otpauth://totp/{quote(f'{ISSUER}:{email}', safe='@:')}?{query}"


def qr_data_uri(uri: str) -> str:
    """The URI as a QR code, an SVG data URI for <img src>: no markup reaches the page.

    make_qr: a full QR code, never a Micro QR that authenticator apps can't read. omitsize: a
    viewBox instead of a fixed 45px, so CSS can size the image.
    """
    return segno.make_qr(uri).svg_data_uri(omitsize=True)
