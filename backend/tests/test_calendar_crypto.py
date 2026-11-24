"""Calendar envelopes fail closed without leaking credentials or coupling rewrap to sync."""

import base64
import json
import traceback
from dataclasses import replace
from uuid import uuid7

import pytest
from pydantic import SecretStr, ValidationError

from app import calendar_crypto as crypto
from app.config import GoogleCalendarKeySettings, GoogleCalendarSettings

KEY = base64.b64encode(b"old-key-sentinel".ljust(32, b"!")).decode()
NEW_KEY = base64.b64encode(b"new-key-sentinel".ljust(32, b"!")).decode()


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in GoogleCalendarSettings.model_fields:
        if name.startswith("google_calendar_"):
            monkeypatch.delenv("ZIF_" + name.upper(), raising=False)
    monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_KEKS", json.dumps({"old": KEY, "new": NEW_KEY}))
    monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_ACTIVE_KEY_ID", "old")


@pytest.fixture
def binding() -> crypto.Binding:
    return crypto.Binding("calendar-token", uuid7(), uuid7(), uuid7(), 1)


def secret_values() -> dict[str, SecretStr]:
    return {
        "refresh_token": SecretStr("refresh-token-sentinel"),
        "subject": SecretStr("subject-sentinel"),
    }


@pytest.mark.parametrize("purpose", ["calendar-token", "calendar-attempt"])
def test_round_trip_and_fresh_keys_and_nonces(
    binding: crypto.Binding, purpose: crypto.Purpose
) -> None:
    binding = replace(binding, purpose=purpose)
    first, second = (crypto.encrypt(secret_values(), binding) for _ in range(2))
    assert crypto.decrypt(first, binding) == secret_values()
    assert first.payload_nonce != second.payload_nonce
    assert first.wrap_nonce != second.wrap_nonce
    keys = crypto.keyring()
    assert keys is not None
    assert crypto._unwrap(first, binding, keys) != crypto._unwrap(second, binding, keys)
    assert first.ciphertext != second.ciphertext
    assert first.format_version == 1


@pytest.mark.parametrize(
    "field", ["tenant_id", "member_id", "row_id", "purpose", "encryption_generation"]
)
def test_every_aad_binding_rejects_a_moved_envelope(binding: crypto.Binding, field: str) -> None:
    envelope = crypto.encrypt(secret_values(), binding)
    if field == "purpose":
        moved = replace(binding, purpose="calendar-attempt")
    elif field == "encryption_generation":
        moved = replace(binding, encryption_generation=2)
    elif field == "tenant_id":
        moved = replace(binding, tenant_id=uuid7())
    elif field == "member_id":
        moved = replace(binding, member_id=uuid7())
    else:
        moved = replace(binding, row_id=uuid7())
    with pytest.raises(crypto.CalendarCryptoError, match="calendar_ciphertext_invalid"):
        crypto.decrypt(envelope, moved)


@pytest.mark.parametrize("field", ["wrapped_dek", "wrap_nonce", "payload_nonce", "ciphertext"])
def test_tampering_is_rejected(binding: crypto.Binding, field: str) -> None:
    envelope = crypto.encrypt(secret_values(), binding)
    value = getattr(envelope, field)
    tampered = replace(envelope, **{field: bytes([value[0] ^ 1]) + value[1:]})
    with pytest.raises(crypto.CalendarCryptoError, match="calendar_ciphertext_invalid"):
        crypto.decrypt(tampered, binding)


def test_wrap_aad_binds_key_id_even_when_two_ids_have_the_same_key(
    binding: crypto.Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_KEKS", json.dumps({"old": KEY, "alias": KEY}))
    envelope = crypto.encrypt(secret_values(), binding)
    with pytest.raises(crypto.CalendarCryptoError):
        crypto.decrypt(replace(envelope, key_id="alias"), binding)


def test_rewrap_preserves_payload_generation_and_token_revision(
    binding: crypto.Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = replace(crypto.encrypt(secret_values(), binding), token_revision=7, wrap_revision=3)
    monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_ACTIVE_KEY_ID", "new")
    after = crypto.rewrap(before, binding)
    assert after.key_id == "new"
    assert after.wrap_nonce != before.wrap_nonce
    assert after.wrapped_dek != before.wrapped_dek
    assert after.wrap_revision == 4
    assert after.token_revision == 7
    assert after.ciphertext == before.ciphertext
    assert after.payload_nonce == before.payload_nonce
    assert after.format_version == before.format_version
    assert crypto.decrypt(after, binding) == secret_values()
    assert crypto.decrypt(before, binding) == secret_values()
    monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_KEKS", json.dumps({"new": NEW_KEY}))
    assert crypto.decrypt(after, binding) == secret_values()
    with pytest.raises(crypto.CalendarCryptoError):
        crypto.decrypt(before, binding)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "[]",
        "null",
        "{}",
        "invalid-json-sentinel",
        '{"old":"secret-sentinel"}',
        json.dumps({"old": base64.b64encode(b"short").decode()}),
        json.dumps({"old": KEY + "\n"}),
        json.dumps({"old": KEY + "="}),
        json.dumps({"old": 12}),
        json.dumps({"": KEY}),
        json.dumps({"old": "é"}),
        '{"old":"' + KEY + '","old":"' + KEY + '"}',
    ],
)
def test_invalid_keyring_disables_crypto(
    raw: str, binding: crypto.Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_KEKS", raw)
    assert crypto.keyring() is None
    with pytest.raises(crypto.CalendarCryptoError, match="calendar_crypto_unavailable"):
        crypto.encrypt(secret_values(), binding)


def test_unknown_key_version_and_missing_active_key_fail(
    binding: crypto.Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = crypto.encrypt(secret_values(), binding)
    for changed in (
        replace(envelope, key_id="unknown"),
        replace(envelope, format_version=2),
        replace(envelope, wrap_nonce=b"short"),
    ):
        with pytest.raises(crypto.CalendarCryptoError):
            crypto.decrypt(changed, binding)
    monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_ACTIVE_KEY_ID", "missing")
    assert crypto.keyring() is None
    with pytest.raises(crypto.CalendarCryptoError):
        crypto.decrypt(envelope, binding)


def test_secrets_are_absent_from_repr_logs_and_displayed_exception_chains(
    binding: crypto.Binding, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = crypto.encrypt(secret_values(), binding)
    secrets = [
        "refresh-token-sentinel",
        "subject-sentinel",
        KEY,
        NEW_KEY,
        repr(envelope.ciphertext),
        repr(envelope.wrap_nonce),
        repr(envelope.payload_nonce),
        repr(envelope.wrapped_dek),
    ]
    shown = repr(envelope) + repr(crypto.keyring()) + repr(secret_values())
    shown += repr(GoogleCalendarKeySettings()) + repr(GoogleCalendarSettings())
    monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_KEKS", "keyring-sentinel")
    with pytest.raises(crypto.CalendarCryptoError) as raised:
        crypto.decrypt(envelope, binding)
    shown += "".join(traceback.format_exception(raised.value)) + caplog.text
    assert "keyring-sentinel" not in shown
    assert all(secret not in shown for secret in secrets)


def test_unset_calendar_config_is_optional_and_never_reuses_totp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZIF_GOOGLE_CALENDAR_KEKS")
    monkeypatch.delenv("ZIF_GOOGLE_CALENDAR_ACTIVE_KEY_ID")
    monkeypatch.setenv("ZIF_TOTP_KEY", KEY)
    settings = GoogleCalendarSettings()
    assert not settings.google_calendar_connect_enabled
    assert settings.request_budget == 60
    assert settings.request_budget_valid
    assert crypto.keyring() is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ZIF_GOOGLE_CALENDAR_REDIRECT_URI", "https://other.test/api/google-calendar/callback"),
        ("ZIF_GOOGLE_CALENDAR_CONNECT_ENABLED", "sometimes"),
    ],
)
def test_invalid_enrollment_config_keeps_decryption_and_rewrap_available(
    name: str, value: str, binding: crypto.Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = crypto.encrypt(secret_values(), binding)
    keys = crypto.keyring()
    monkeypatch.setenv("ZIF_APP_URL", "https://example.test")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        GoogleCalendarSettings()
    assert crypto.keyring() == keys
    assert crypto.decrypt(envelope, binding) == secret_values()
    assert crypto.decrypt(crypto.rewrap(envelope, binding), binding) == secret_values()


@pytest.mark.parametrize(
    "value", ["0", "-1", "100001", "60.0", "oops", " 60", "060", "９", "9" * 100]
)
def test_invalid_budget_keeps_a_cleanup_fallback(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_REQUESTS_PER_MINUTE", value)
    settings = GoogleCalendarSettings()
    assert not settings.request_budget_valid
    assert settings.request_budget == 60


@pytest.mark.parametrize("value", ["1", "60", "100000"])
def test_budget_bounds(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_REQUESTS_PER_MINUTE", value)
    settings = GoogleCalendarSettings()
    assert settings.request_budget_valid
    assert settings.request_budget == int(value)


def test_redirect_is_fixed_to_configured_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_APP_URL", "https://example.test")
    uri = "https://example.test/api/google-calendar/callback"
    monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_REDIRECT_URI", uri)
    assert GoogleCalendarSettings().google_calendar_redirect_uri == uri
    for wrong in (uri + "?secret-sentinel", uri + "/", uri.replace("example.test", "other.test")):
        monkeypatch.setenv("ZIF_GOOGLE_CALENDAR_REDIRECT_URI", wrong)
        with pytest.raises(ValidationError) as raised:
            GoogleCalendarSettings()
        assert "secret-sentinel" not in str(raised.value)
        assert "calendar_redirect_invalid" in str(raised.value)
