"""ZIF-71: app/totp.py (RFC 6238 codes, the secret's encryption, proofs) and the startup field."""

import base64
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import Engine

from app import totp
from app.main import create_app
from tests.conftest import new_client

SECRET = b"12345678901234567890"  # RFC 6238 Appendix B, SHA-1


# fence: wrong truncation or a lost leading zero. Injected by dropping zfill.
@pytest.mark.parametrize(
    ("seconds", "expected"), [(59, "287082"), (1111111109, "081804"), (1234567890, "005924")]
)
def test_rfc6238_vectors(seconds: int, expected: str) -> None:
    assert totp.code(SECRET, seconds // 30) == expected


# guard
def test_matching_step() -> None:
    now = datetime(2026, 10, 14, 12, 0, 10, tzinfo=UTC)
    t = int(now.timestamp()) // 30
    for step in (t - 1, t, t + 1):
        assert totp.matching_step(SECRET, totp.code(SECRET, step), now) == step
    assert totp.matching_step(SECRET, totp.code(SECRET, t + 2), now) is None
    assert totp.matching_step(SECRET, totp.code(SECRET, t - 2), now) is None
    for typed in ("٠١٢٣٤٥", "\ud800"):  # Arabic-Indic digits, a lone surrogate: None, not a raise
        assert totp.matching_step(SECRET, typed, now) is None


# fence: no AAD, so one user's ciphertext decrypts as another's. Injected by passing None as AAD.
def test_ciphertext_is_bound_to_its_user() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    secret = totp.new_secret()
    assert totp.decrypt(totp.encrypt(secret, a), a) == secret
    with pytest.raises(InvalidTag):
        totp.decrypt(totp.encrypt(secret, a), b)


# fence: a fixed or derived nonce (GCM nonce reuse). Injected by a constant b"\0" * 12.
def test_encrypt_uses_a_fresh_nonce() -> None:
    a = uuid.uuid4()
    secret = totp.new_secret()
    first, second = totp.encrypt(secret, a), totp.encrypt(secret, a)
    assert first[1:13] != second[1:13]
    assert totp.decrypt(first, a) == totp.decrypt(second, a) == secret


# fence: proof encoding without surrogatepass (UnicodeEncodeError). Injected by dropping it.
@pytest.mark.parametrize("value", ["\ud800", "٠١٢٣٤٥"])
def test_proof_survives_a_lone_surrogate(value: str) -> None:
    assert len(totp.proof(value)) == 32


# guard
def test_key_states(monkeypatch: pytest.MonkeyPatch) -> None:
    secret, user = totp.new_secret(), uuid.uuid4()
    blob = totp.encrypt(secret, user)
    with pytest.raises(ValueError, match="version"):
        totp.decrypt(b"\x02" + blob[1:], user)
    good = base64.b64encode(bytes(range(32))).decode()
    for value, state in [
        (good, "set"),
        (base64.b64encode(bytes(31)).decode(), "invalid"),
        ("not base64 at all", "invalid"),
        (good[:20] + "!" + good[20:], "invalid"),  # validate=False would drop the "!"
        ("\u201c" + good + "\u201d", "invalid"),  # pasted with curly quotes: non-ASCII
    ]:
        monkeypatch.setenv("ZIF_TOTP_KEY", value)
        assert totp.key_state() == state, value
    monkeypatch.delenv("ZIF_TOTP_KEY")
    assert totp.key_state() == "unset"
    assert totp.key() is None
    with pytest.raises(RuntimeError):
        totp.encrypt(secret, user)


# fence: the development key is refused on a public URL and works on localhost.
def test_dev_key_refused_on_a_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_TOTP_KEY", "ZGV2LW9ubHktdG90cC1rZXktbm90LWEtc2VjcmV0ISE=")
    monkeypatch.setenv("ZIF_APP_URL", "https://app.example.com")
    assert totp.key_state() == "dev"
    assert totp.key() is None
    for local in ["http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000"]:
        monkeypatch.setenv("ZIF_APP_URL", local)
        assert totp.key_state() == "set", local
        assert totp.key() is not None


# guard
def test_codes_and_normalise() -> None:
    ticket = totp.new_code()
    assert len(ticket) == 19 and ticket.count("-") == 3
    assert totp.normalise(f" {ticket.lower()} ") == ticket.replace("-", "")
    assert totp.new_code() != ticket


# fence: a startup that refuses to run without the key (one replica, Recreate), or the key in a
# log. Injected by raising in the lifespan when unset.
@pytest.mark.parametrize(
    ("value", "state", "errors"),
    [
        (None, "unset", 1),
        ("bm90IDMyIGJ5dGVz", "invalid", 1),
        ("\u201cbm90IGFzY2lp\u201d", "invalid", 1),
        ("c3RhcnR1cC10ZXN0LWtleS10aGlydHktdHdvLWJ5dGU=", "set", 0),
    ],
    ids=["unset", "invalid", "non-ascii", "set"],
)
def test_startup_totp_key_field(
    app_engine: Engine,
    log_lines: Callable[[], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    state: str,
    errors: int,
) -> None:
    if value is None:
        monkeypatch.delenv("ZIF_TOTP_KEY", raising=False)
    else:
        monkeypatch.setenv("ZIF_TOTP_KEY", value)
    with new_client(create_app()) as client:
        assert client.get("/api/healthz").status_code == 200
    lines = log_lines()
    assert [line["totp_key"] for line in lines if line["msg"] == "api started"] == [state]
    unavailable = [
        line
        for line in lines
        if line["msg"] == "totp key unset, invalid or dev value: admin second factor unavailable"
    ]
    assert [line["level"] for line in unavailable] == ["ERROR"] * errors
    if value:
        assert value not in json.dumps(lines, ensure_ascii=False)  # the raw text, unescaped
