"""Calendar envelope encryption. Operational generation is separate from encryption_generation.

Rewrap changes only wrapping metadata; callers persist it with a key_id/wrap_revision CAS.
Payload replacement must lock and re-read that metadata before writing (ZIF-65).
"""

import base64
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr, ValidationError

from app.config import GoogleCalendarKeySettings

VERSION = 1
Purpose = Literal["calendar-token", "calendar-attempt"]


class CalendarCryptoError(ValueError):
    """A categorical error with no underlying secret-bearing exception in its displayed chain."""


@dataclass(frozen=True)
class Binding:
    purpose: Purpose
    tenant_id: UUID
    row_id: UUID
    member_id: UUID
    encryption_generation: int


@dataclass(frozen=True)
class Envelope:
    key_id: str
    wrapped_dek: bytes = field(repr=False)
    wrap_nonce: bytes = field(repr=False)
    payload_nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)
    format_version: int = VERSION
    wrap_revision: int = 1
    token_revision: int = 1


@dataclass(frozen=True)
class Keyring:
    active_key_id: str
    keys: dict[str, bytes] = field(repr=False)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def keyring() -> Keyring | None:
    """Invalid, duplicate, noncanonical or missing keys disable crypto, including old-key reads."""
    try:
        settings = GoogleCalendarKeySettings()
        values = json.loads(
            settings.google_calendar_keks.get_secret_value(), object_pairs_hook=_unique
        )
        if not isinstance(values, dict) or not values:
            return None
        keys: dict[str, bytes] = {}
        for key_id, value in values.items():
            if (
                not key_id
                or len(key_id) > 128
                or not key_id.isascii()
                or not isinstance(value, str)
            ):
                return None
            decoded = base64.b64decode(value, validate=True)
            if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
                return None
            keys[key_id] = decoded
        active = settings.google_calendar_active_key_id
        if active not in keys:
            return None
        return Keyring(active, keys)
    except ValueError, ValidationError:
        return None


def _keys() -> Keyring:
    found = keyring()
    if found is None:
        raise CalendarCryptoError("calendar_crypto_unavailable")
    return found


def _aad(binding: Binding, version: int, key_id: str | None = None) -> bytes:
    if (
        version != VERSION
        or binding.purpose not in ("calendar-token", "calendar-attempt")
        or not 1 <= binding.encryption_generation < 2**63
    ):
        raise CalendarCryptoError("calendar_ciphertext_invalid")
    parts = [
        b"calendar-envelope",
        b"wrap" if key_id is not None else b"payload",
        binding.purpose.encode("ascii"),
        str(version).encode("ascii"),
        binding.tenant_id.bytes,
        binding.row_id.bytes,
        binding.member_id.bytes,
        str(binding.encryption_generation).encode("ascii"),
    ]
    if key_id is not None:
        parts.append(key_id.encode("utf-8"))
    return b"".join(len(part).to_bytes(4, "big") + part for part in parts)


def encrypt(values: Mapping[str, SecretStr], binding: Binding) -> Envelope:
    keys = _keys()
    aad = _aad(binding, VERSION)
    payload = json.dumps(
        {
            "version": VERSION,
            "values": {key: value.get_secret_value() for key, value in values.items()},
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    dek = secrets.token_bytes(32)
    payload_nonce, wrap_nonce = secrets.token_bytes(12), secrets.token_bytes(12)
    return Envelope(
        key_id=keys.active_key_id,
        wrapped_dek=AESGCM(keys.keys[keys.active_key_id]).encrypt(
            wrap_nonce, dek, _aad(binding, VERSION, keys.active_key_id)
        ),
        wrap_nonce=wrap_nonce,
        payload_nonce=payload_nonce,
        ciphertext=AESGCM(dek).encrypt(payload_nonce, payload, aad),
    )


def _unwrap(envelope: Envelope, binding: Binding, keys: Keyring) -> bytes:
    try:
        if (
            len(envelope.wrap_nonce) != 12
            or len(envelope.payload_nonce) != 12
            or len(envelope.wrapped_dek) != 48
            or len(envelope.ciphertext) < 16
        ):
            raise ValueError
        return AESGCM(keys.keys[envelope.key_id]).decrypt(
            envelope.wrap_nonce,
            envelope.wrapped_dek,
            _aad(binding, envelope.format_version, envelope.key_id),
        )
    except KeyError, ValueError, InvalidTag:
        raise CalendarCryptoError("calendar_ciphertext_invalid") from None


def decrypt(envelope: Envelope, binding: Binding) -> dict[str, SecretStr]:
    dek = _unwrap(envelope, binding, _keys())
    try:
        raw = AESGCM(dek).decrypt(
            envelope.payload_nonce, envelope.ciphertext, _aad(binding, envelope.format_version)
        )
        bundle = json.loads(raw)
        if (
            not isinstance(bundle, dict)
            or set(bundle) != {"version", "values"}
            or bundle["version"] != VERSION
            or not isinstance(bundle["values"], dict)
            or not all(isinstance(value, str) for value in bundle["values"].values())
        ):
            raise ValueError
        return {key: SecretStr(value) for key, value in bundle["values"].items()}
    except ValueError, InvalidTag:
        raise CalendarCryptoError("calendar_ciphertext_invalid") from None


def rewrap(envelope: Envelope, binding: Binding) -> Envelope:
    """No payload rewrite and no operational/token revision change, even for a tombstone."""
    keys = _keys()
    dek = _unwrap(envelope, binding, keys)
    nonce = secrets.token_bytes(12)
    return replace(
        envelope,
        key_id=keys.active_key_id,
        wrap_nonce=nonce,
        wrapped_dek=AESGCM(keys.keys[keys.active_key_id]).encrypt(
            nonce, dek, _aad(binding, envelope.format_version, keys.active_key_id)
        ),
        wrap_revision=envelope.wrap_revision + 1,
    )
