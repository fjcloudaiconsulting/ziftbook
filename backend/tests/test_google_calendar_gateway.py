import base64
import json
import logging
import socket
import traceback
from collections import deque
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from threading import Event
from typing import Any, Never
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr
from urllib3.util import Timeout

from app import google_calendar_gateway as google
from app.config import GoogleCalendarSettings, MailSettings
from tests.google_calendar_fake import Account, FakeGateway

CLIENT_ID = "calendar-client.apps.googleusercontent.com"
CLIENT_SECRET = "client-secret-zzyzx"
CODE = "authorization-code-zzyzx"
VERIFIER = "pkce-verifier-zzyzx-" * 3
NONCE = "oidc-nonce-zzyzx"
STATE = "oauth-state-zzyzx"
EMAIL = "private-calendar-zzyzx@example.test"
SUBJECT = "account-subject-zzyzx"
ACCESS = "access-token-zzyzx"
REFRESH = "refresh-token-zzyzx"
PROVIDER_DETAIL = "provider-detail-zzyzx"
START = datetime(2026, 11, 25, tzinfo=UTC)
END = START + timedelta(days=1)
START_TEXT = "2026-11-25T00:00:00Z"
END_TEXT = "2026-11-26T00:00:00Z"


def deny_network(*_: object, **__: object) -> Never:
    raise AssertionError("Google Calendar gateway test attempted live network access")


class Http:
    def __init__(self) -> None:
        self.requests: list[requests.PreparedRequest] = []
        self.responses: deque[tuple[str, requests.Response | Exception]] = deque()

    def add(
        self,
        url: str,
        body: object = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        response = requests.Response()
        response.status_code = status
        response._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        response._content_consumed = True
        response.headers.update(headers or {})
        self.responses.append((url, response))

    def send(
        self, request: requests.PreparedRequest, *, stream: bool, timeout: Timeout
    ) -> requests.Response:
        assert stream
        connect, total = timeout.connect_timeout, timeout.total
        assert isinstance(connect, (int, float)) and 0 < connect <= 2
        assert isinstance(total, float) and 0 < total <= 6
        assert request.url and request.url.startswith("https://")
        self.requests.append(request)
        assert self.responses, "Unexpected Google Calendar request"
        url, response = self.responses.popleft()
        assert request.url == url
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Http:
    stub = Http()
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", lambda _, request, **kw: stub.send(request, **kw)
    )
    return stub


@pytest.fixture
def gateway() -> google.Gateway:
    return google.Gateway(
        GoogleCalendarSettings(
            google_calendar_client_id=CLIENT_ID,
            google_calendar_client_secret=SecretStr(CLIENT_SECRET),
            google_calendar_redirect_uri=MailSettings().app_url.rstrip("/")
            + "/api/google-calendar/callback",
        )
    )


@pytest.fixture(scope="module")
def signing_keys() -> tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]:
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


def claims() -> dict[str, Any]:
    now = int(datetime.now(UTC).timestamp())
    return {
        "iss": google.ISSUERS[0],
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "exp": now + 300,
        "iat": now - 10,
        "nonce": NONCE,
        "sub": SUBJECT,
        "email": EMAIL,
        "email_verified": True,
    }


def signed(key: rsa.RSAPrivateKey, data: dict[str, Any], kid: str = "key-1") -> str:
    return jwt.encode(data, key, algorithm="RS256", headers={"kid": kid})


def jwks(http: Http, key: rsa.RSAPrivateKey, kid: str = "key-1") -> None:
    public_key = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    http.add(
        google.JWKS_URL,
        {"keys": [{**public_key, "kid": kid, "use": "sig", "alg": "RS256"}]},
        headers={"Cache-Control": "public, max-age=600"},
    )


def token_body(**extra: object) -> dict[str, Any]:
    return {"access_token": ACCESS, "expires_in": 3600, "token_type": "Bearer", **extra}


def exchange_body(id_token: str, scopes: str = " ".join(google.SCOPES)) -> dict[str, Any]:
    return token_body(refresh_token=REFRESH, id_token=id_token, scope=scopes)


def exchange(gateway: google.Gateway) -> google.Exchanged:
    return gateway.exchange_code(code=CODE, verifier=VERIFIER, nonce=NONCE)


def freebusy(gateway: google.Gateway) -> google.FreeBusy:
    return gateway.freebusy(access_token=ACCESS, start=START, end=END)


def window(primary: dict[str, Any] | None = None, **extra: object) -> dict[str, Any]:
    calendar = {"busy": []} if primary is None else primary
    return {
        "kind": "calendar#freeBusy",
        "timeMin": START_TEXT,
        "timeMax": END_TEXT,
        "calendars": {"primary": calendar},
        **extra,
    }


def test_authorization_pkce_and_exact_scopes(gateway: google.Gateway, http: Http) -> None:
    authorization = gateway.authorization_url(state=STATE, nonce=NONCE, verifier=VERIFIER)
    url = urlsplit(authorization.url)
    assert url._replace(query="").geturl() == google.AUTHORIZATION_URL
    redirect = MailSettings().app_url.rstrip("/") + "/api/google-calendar/callback"
    assert parse_qs(url.query) == {
        "response_type": ["code"],
        "client_id": [CLIENT_ID],
        "redirect_uri": [redirect],
        "state": [STATE],
        "nonce": [NONCE],
        "access_type": ["offline"],
        "prompt": ["select_account consent"],
        "scope": [" ".join(google.SCOPES)],
        "code_challenge_method": ["S256"],
        "code_challenge": ["a2lFih9Y3Lx4kQYTfMcCX745Ri4iqozaoH6y-gEAG5U"],
    }
    assert STATE not in repr(authorization)
    assert not http.requests


def test_exchange_uses_bound_params_and_cached_google_keys(
    gateway: google.Gateway,
    http: Http,
    signing_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> None:
    key, rotated = signing_keys
    aliases = " ".join(google.SCOPES).replace(
        "email", "https://www.googleapis.com/auth/userinfo.email"
    )
    token = signed(key, {**claims(), "iss": "accounts.google.com"})
    http.add(google.TOKEN_URL, exchange_body(token, aliases))
    jwks(http, key)
    http.add(google.TOKEN_URL, exchange_body(token))
    http.add(google.TOKEN_URL, exchange_body(signed(rotated, claims(), "key-2")))
    jwks(http, rotated, "key-2")

    grant = exchange(gateway)
    assert (grant.issuer, grant.subject, grant.email) == (google.ISSUERS[0], SUBJECT, EMAIL)
    assert (grant.access_token, grant.refresh_token, grant.expires_in) == (ACCESS, REFRESH, 3600)
    assert grant == exchange(gateway) == exchange(gateway)
    assert [request.url for request in http.requests] == [
        google.TOKEN_URL,
        google.JWKS_URL,
        google.TOKEN_URL,
        google.TOKEN_URL,
        google.JWKS_URL,
    ]
    request = http.requests[0]
    assert request.method == "POST"
    assert isinstance(request.body, str)
    assert parse_qs(request.body) == {
        "grant_type": ["authorization_code"],
        "client_id": [CLIENT_ID],
        "client_secret": [CLIENT_SECRET],
        "code": [CODE],
        "code_verifier": [VERIFIER],
        "redirect_uri": [MailSettings().app_url.rstrip("/") + "/api/google-calendar/callback"],
    }
    for secret in (ACCESS, REFRESH, SUBJECT, EMAIL, token):
        assert secret not in repr(grant) and secret not in str(grant)


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://elsewhere.test"),
        ("aud", "other-client"),
        ("aud", [CLIENT_ID, "other"]),
        ("azp", "other-client"),
        ("exp", 1),
        ("nbf", 4_000_000_000),
        ("nonce", "other-nonce"),
        ("sub", None),
        ("email_verified", False),
        ("email_verified", "true"),
    ],
)
def test_invalid_identity_claims(
    gateway: google.Gateway,
    http: Http,
    signing_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
    claim: str,
    value: object,
) -> None:
    key, _ = signing_keys
    data = claims()
    if value is None:
        del data[claim]
    else:
        data[claim] = value
    http.add(google.TOKEN_URL, exchange_body(signed(key, data)))
    jwks(http, key)
    with pytest.raises(google.Failed) as failure:
        exchange(gateway)
    assert failure.value.category == google.Outcome.INVALID_IDENTITY


def test_wrong_signing_key(
    gateway: google.Gateway,
    http: Http,
    signing_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> None:
    trusted, other = signing_keys
    http.add(google.TOKEN_URL, exchange_body(signed(other, claims())))
    jwks(http, trusted)
    with pytest.raises(google.Failed) as failure:
        exchange(gateway)
    assert failure.value.category == google.Outcome.INVALID_IDENTITY


@pytest.mark.parametrize("algorithm", ["none", "HS256"])
def test_rejects_non_rs256_header_before_loading_signing_keys(
    gateway: google.Gateway,
    http: Http,
    algorithm: str,
) -> None:
    header = base64.urlsafe_b64encode(json.dumps({"alg": algorithm, "kid": "key-1"}).encode())
    fixture = header.decode().rstrip("=") + ".e30.c3R1Yg"
    http.add(google.TOKEN_URL, exchange_body(fixture))
    with pytest.raises(google.Failed) as failure:
        exchange(gateway)
    assert failure.value.category == google.Outcome.INVALID_IDENTITY
    assert [request.url for request in http.requests] == [google.TOKEN_URL]


@pytest.mark.parametrize(
    "scopes",
    ["openid email", " ".join(google.SCOPES) + " https://www.googleapis.com/auth/calendar"],
)
def test_exchange_rejects_denied_and_broader_grants(
    gateway: google.Gateway, http: Http, scopes: str
) -> None:
    http.add(google.TOKEN_URL, exchange_body("unread-id-token", scopes))
    with pytest.raises(google.Failed) as failure:
        exchange(gateway)
    assert failure.value.category == google.Outcome.INVALID_SCOPE


def test_exchange_requires_a_refresh_token(gateway: google.Gateway, http: Http) -> None:
    body = exchange_body("unread-id-token")
    del body["refresh_token"]
    http.add(google.TOKEN_URL, body)
    with pytest.raises(google.Failed) as failure:
        exchange(gateway)
    assert failure.value.category == google.Outcome.INVALID_RESPONSE


def test_refresh_distinguishes_rotated_omitted_and_invalid(
    gateway: google.Gateway, http: Http
) -> None:
    replacement = "rotated-refresh-zzyzx"
    http.add(google.TOKEN_URL, token_body(refresh_token=replacement))
    http.add(google.TOKEN_URL, token_body())
    http.add(google.TOKEN_URL, token_body(refresh_token=None))
    rotated = gateway.refresh(refresh_token=REFRESH)
    assert (rotated.access_token, rotated.expires_in, rotated.refresh_token) == (
        ACCESS,
        3600,
        replacement,
    )
    assert ACCESS not in repr(rotated) and replacement not in repr(rotated)
    assert gateway.refresh(refresh_token=replacement).refresh_token is None
    with pytest.raises(google.Failed) as failure:
        gateway.refresh(refresh_token=replacement)
    assert failure.value.category == google.Outcome.INVALID_RESPONSE


def test_freebusy_requests_exact_window_and_normalizes_offsets(
    gateway: google.Gateway, http: Http
) -> None:
    http.add(
        google.FREEBUSY_URL,
        window(
            {
                "busy": [
                    {"start": "2026-11-25T01:15:00+01:00", "end": "2026-11-25T02:30:00+01:00"},
                ]
            }
        ),
    )
    http.add(google.FREEBUSY_URL, window())
    plus_two = timezone(timedelta(hours=2))
    result = gateway.freebusy(access_token=ACCESS, start=START.astimezone(plus_two), end=END)
    assert (result.start, result.end) == (START, END)
    assert result.busy == (
        google.Interval(START + timedelta(minutes=15), START + timedelta(hours=1, minutes=30)),
    )
    request = http.requests[0]
    assert request.headers["Authorization"] == f"Bearer {ACCESS}"
    assert request.method == "POST"
    assert isinstance(request.body, bytes)
    assert json.loads(request.body) == {
        "items": [{"id": "primary"}],
        "timeZone": "UTC",
        "timeMin": START_TEXT,
        "timeMax": END_TEXT,
    }
    assert freebusy(gateway).busy == ()


@pytest.mark.parametrize(
    ("body", "category"),
    [
        (window(calendars={}), google.Outcome.CALENDAR_MISSING),
        (window({"errors": [{"reason": "notFound"}]}), google.Outcome.CALENDAR_MISSING),
        (window({"errors": [{"reason": "internalError"}], "busy": []}), google.Outcome.TRANSIENT),
        (window({"errors": [{"reason": "timeRangeEmpty"}]}), google.Outcome.INVALID_REQUEST),
        (window({"errors": [{"reason": "someOtherReason"}]}), google.Outcome.TRANSIENT),
        (window({"errors": []}), google.Outcome.INVALID_RESPONSE),
        (window({"busy": None}), google.Outcome.INVALID_RESPONSE),
        (window({"busy": [{"start": START_TEXT}]}), google.Outcome.INVALID_RESPONSE),
        (
            window({"busy": [{"start": END_TEXT, "end": START_TEXT}]}),
            google.Outcome.INVALID_RESPONSE,
        ),
        (
            window({"busy": [{"start": START_TEXT[:-1], "end": END_TEXT}]}),
            google.Outcome.INVALID_RESPONSE,
        ),
        (
            window({"busy": [{"start": "2026-11-25T12:01:00+00:80", "end": END_TEXT}]}),
            google.Outcome.INVALID_RESPONSE,
        ),
        (
            window({"busy": [{"start": END_TEXT, "end": "2026-11-27T00:00:00Z"}]}),
            google.Outcome.INVALID_RESPONSE,
        ),
        (window(timeMax=START_TEXT), google.Outcome.INVALID_RESPONSE),
        (
            window(groups={"unexpected": {"errors": [{"reason": "internalError"}]}}),
            google.Outcome.INVALID_RESPONSE,
        ),
        (window(error={"message": PROVIDER_DETAIL}), google.Outcome.INVALID_RESPONSE),
        (b'{"calendars": {}, "calendars": {}}', google.Outcome.INVALID_RESPONSE),
        (b"{", google.Outcome.INVALID_RESPONSE),
    ],
)
def test_freebusy_fails_closed(
    gateway: google.Gateway,
    http: Http,
    body: object,
    category: google.Outcome,
) -> None:
    http.add(google.FREEBUSY_URL, body)
    with pytest.raises(google.Failed) as failure:
        freebusy(gateway)
    assert (failure.value.category, failure.value.status_class) == (
        category,
        google.StatusClass.SUCCESS,
    )


@pytest.mark.parametrize("end", [START, START.replace(tzinfo=None), START + timedelta(days=91)])
def test_freebusy_rejects_invalid_request_bounds(
    gateway: google.Gateway, http: Http, end: datetime
) -> None:
    with pytest.raises(google.Failed) as failure:
        gateway.freebusy(access_token=ACCESS, start=START, end=end)
    assert failure.value.category == google.Outcome.INVALID_REQUEST
    assert not http.requests


def test_freebusy_rejects_oversized_body_or_interval_count(
    gateway: google.Gateway,
    http: Http,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(google, "MAX_INTERVALS", 1)
    interval = {"start": START_TEXT, "end": END_TEXT}
    http.add(google.FREEBUSY_URL, window({"busy": [interval, interval]}))
    http.add(google.FREEBUSY_URL, b" " * (google.MAX_BYTES + 1))
    for _ in range(2):
        with pytest.raises(google.Failed) as failure:
            freebusy(gateway)
        assert failure.value.category == google.Outcome.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("status", "reason", "retry_header", "category", "retry"),
    [
        (401, None, "", google.Outcome.REFRESH_REQUIRED, None),
        (403, "rateLimitExceeded", "32", google.Outcome.QUOTA, 32),
        (403, "userRateLimitExceeded", "", google.Outcome.QUOTA, None),
        (403, "quotaExceeded", "", google.Outcome.QUOTA, None),
        (403, "insufficientPermissions", "", google.Outcome.RECONNECT_REQUIRED, None),
        (403, "forbidden", "", google.Outcome.RECONNECT_REQUIRED, None),
        (403, None, "", google.Outcome.TRANSIENT, None),
        (403, "someOtherReason", "", google.Outcome.TRANSIENT, None),
        (400, "timeRangeEmpty", "", google.Outcome.INVALID_REQUEST, None),
        (429, None, "999999", google.Outcome.QUOTA, google.MAX_RETRY_AFTER),
        (503, None, "42", google.Outcome.TRANSIENT, 42),
        (503, None, "yesterday", google.Outcome.TRANSIENT, None),
    ],
)
def test_calendar_http_failure_classification(
    gateway: google.Gateway,
    http: Http,
    status: int,
    reason: str | None,
    retry_header: str,
    category: google.Outcome,
    retry: int | None,
) -> None:
    body = {"error": {"errors": [{"reason": reason}]}} if reason else None
    http.add(google.FREEBUSY_URL, body, status=status, headers={"Retry-After": retry_header})
    with pytest.raises(google.Failed) as failure:
        freebusy(gateway)
    assert failure.value.category == category
    assert failure.value.status_class == (
        google.StatusClass.SERVER_ERROR if status >= 500 else google.StatusClass.CLIENT_ERROR
    )
    assert failure.value.retry_after == retry


@pytest.mark.parametrize(
    ("status", "reason", "category"),
    [
        (400, "invalid_grant", google.Outcome.RECONNECT_REQUIRED),
        (401, "invalid_client", google.Outcome.INVALID_CLIENT),
        (400, "invalid_request", google.Outcome.INVALID_REQUEST),
        (400, "unknown", google.Outcome.TRANSIENT),
    ],
)
def test_oauth_errors_use_structured_reason(
    gateway: google.Gateway,
    http: Http,
    status: int,
    reason: str,
    category: google.Outcome,
) -> None:
    http.add(
        google.TOKEN_URL, {"error": reason, "error_description": PROVIDER_DETAIL}, status=status
    )
    with pytest.raises(google.Failed) as failure:
        gateway.refresh(refresh_token=REFRESH)
    assert failure.value.category == category


def test_revoke_distinguishes_accepted_invalid_and_unknown(
    gateway: google.Gateway,
    http: Http,
) -> None:
    http.add(google.REVOKE_URL, b"", status=200)
    http.add(google.REVOKE_URL, {"error": "invalid_token"}, status=400)
    http.add(google.REVOKE_URL, {"error": "unrecognized"}, status=400)
    assert gateway.revoke(refresh_token=REFRESH) == google.RevokeResult.ACCEPTED
    assert gateway.revoke(refresh_token=REFRESH) == google.RevokeResult.ALREADY_INVALID
    with pytest.raises(google.Failed) as failure:
        gateway.revoke(refresh_token=REFRESH)
    assert failure.value.category == google.Outcome.TRANSIENT
    request = http.requests[0]
    assert isinstance(request.body, str)
    assert parse_qs(request.body) == {"token": [REFRESH]}
    assert request.method == "POST"


def test_redirect_is_rejected_without_following_location(
    gateway: google.Gateway, http: Http
) -> None:
    http.add(
        google.TOKEN_URL, b"", status=302, headers={"Location": f"https://private.test/{CODE}"}
    )
    with pytest.raises(google.Failed) as failure:
        exchange(gateway)
    assert (failure.value.category, failure.value.status_class) == (
        google.Outcome.REDIRECT,
        google.StatusClass.REDIRECTION,
    )
    assert [request.url for request in http.requests] == [google.TOKEN_URL]


def test_transport_errors_are_sanitized(
    gateway: google.Gateway,
    http: Http,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    sentinel = f"{CODE} {CLIENT_SECRET} {VERIFIER} {NONCE} {EMAIL} {PROVIDER_DETAIL}"
    http.responses.append((google.TOKEN_URL, requests.Timeout(sentinel)))
    with pytest.raises(google.Failed) as failure:
        exchange(gateway)
    assert failure.value.category == google.Outcome.TIMEOUT
    assert failure.value.__context__ is None
    error = failure.value
    rendered = "".join(traceback.format_exception(error)) + repr(error) + str(error) + caplog.text
    for value in sentinel.split():
        assert value not in rendered
    [record] = [record for record in caplog.records if record.name == google.__name__]
    details = vars(record)
    assert (details["op"], details["outcome"], details["status_class"]) == (
        google.Operation.EXCHANGE,
        google.Outcome.TIMEOUT,
        google.StatusClass.NONE,
    )
    assert details["duration_ms"] >= 0


def test_total_deadline_returns_while_transport_is_blocked(
    gateway: google.Gateway,
    http: Http,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = Event()
    entered = Event()
    finished = Event()

    def stalled(*_: object, **__: object) -> Never:
        entered.set()
        try:
            assert release.wait(3)
            raise requests.Timeout()
        finally:
            finished.set()

    monkeypatch.setattr(google, "DEADLINE", 0.1)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", stalled)
    try:
        with pytest.raises(google.Failed) as failure:
            exchange(gateway)
        assert failure.value.category == google.Outcome.TIMEOUT
        assert entered.is_set() and not finished.is_set()
    finally:
        release.set()
        assert finished.wait(3)


def test_fake_gateway_guards_transactions_and_simulates_grant_lifecycle() -> None:
    active_tx = True
    fake = FakeGateway(transaction_open=lambda: active_tx, now=START)
    with pytest.raises(AssertionError, match="inside a database transaction"):
        fake.refresh(refresh_token=REFRESH)
    active_tx = False
    interval = google.Interval(START, END)
    fake.accounts[SUBJECT] = Account(SUBJECT, EMAIL, CODE, REFRESH, (interval,), "rotated")
    grant = exchange(fake)
    fake.fail_next(google.Operation.FREEBUSY, google.Outcome.QUOTA, times=2)
    for _ in range(2):
        with pytest.raises(google.Failed) as quota:
            fake.freebusy(access_token=grant.access_token, start=START, end=END)
        assert quota.value.category == google.Outcome.QUOTA
    assert fake.freebusy(access_token=grant.access_token, start=START, end=END).busy == (interval,)
    assert fake.refresh(refresh_token=REFRESH).refresh_token == "rotated"
    second = fake.refresh(refresh_token="rotated")
    assert second.refresh_token is None
    scripted = google.FreeBusy(START, END, ())
    fake.responses[google.Operation.FREEBUSY].append(lambda: scripted)
    assert fake.freebusy(access_token=second.access_token, start=START, end=END) is scripted
    fake.now += timedelta(hours=1)
    with pytest.raises(google.Failed) as expired:
        fake.freebusy(access_token=second.access_token, start=START, end=END)
    assert expired.value.category == google.Outcome.REFRESH_REQUIRED
    fake.accounts["another-grant"] = Account(SUBJECT, EMAIL, "another-code", "another-refresh")
    other = fake.refresh(refresh_token="another-refresh")
    assert fake.revoke(refresh_token="rotated") == google.RevokeResult.ACCEPTED
    with pytest.raises(google.Failed) as revoked_access:
        fake.freebusy(access_token=other.access_token, start=START, end=END)
    assert revoked_access.value.category == google.Outcome.REFRESH_REQUIRED
    assert fake.revoke(refresh_token="another-refresh") == google.RevokeResult.ALREADY_INVALID
    assert fake.revoke(refresh_token="rotated") == google.RevokeResult.ALREADY_INVALID
    with pytest.raises(google.Failed) as revoked:
        fake.refresh(refresh_token="rotated")
    assert revoked.value.category == google.Outcome.RECONNECT_REQUIRED


def test_retry_after_date_is_bounded(gateway: google.Gateway, http: Http) -> None:
    future = format_datetime(datetime.now(UTC) + timedelta(days=1), usegmt=True)
    http.add(google.FREEBUSY_URL, status=503, headers={"Retry-After": future})
    with pytest.raises(google.Failed) as failure:
        freebusy(gateway)
    assert failure.value.retry_after == google.MAX_RETRY_AFTER


def test_refresh_checks_scopes_if_returned(gateway: google.Gateway, http: Http) -> None:
    http.add(google.TOKEN_URL, token_body(scope="openid email"))
    with pytest.raises(google.Failed) as failure:
        gateway.refresh(refresh_token=REFRESH)
    assert failure.value.category == google.Outcome.INVALID_SCOPE
