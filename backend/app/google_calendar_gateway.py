"""Private Google Calendar transport. Call outside database transactions."""

import base64
import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Any
from urllib.parse import urlencode

import jwt
import requests
from pydantic import ValidationError
from urllib3.util import Timeout

from app.config import GoogleCalendarSettings

AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
FREEBUSY_URL = "https://www.googleapis.com/calendar/v3/freeBusy"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SCOPES = ("openid", "email", "https://www.googleapis.com/auth/calendar.freebusy")
DEADLINE = 6.0
MAX_BYTES = 2 * 1024 * 1024
MAX_INTERVALS = 50_000
MAX_RETRY_AFTER = 30 * 60
PKCE_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)

logger = logging.getLogger(__name__)
# requests' read timeout resets with each socket read. Bound caller latency separately and
# reserve capacity until the transport really exits, even if the caller timed out.
_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="google-calendar-http")
_slots = BoundedSemaphore(4)


class Operation(StrEnum):
    AUTHORIZE = "authorization_url"
    EXCHANGE = "exchange_code"
    REFRESH = "refresh"
    FREEBUSY = "freebusy"
    REVOKE = "revoke"


class Outcome(StrEnum):
    OK = "ok"
    ALREADY_INVALID = "already_invalid"
    TRANSIENT = "transient"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    REFRESH_REQUIRED = "refresh_required"
    RECONNECT_REQUIRED = "reconnect_required"
    CALENDAR_MISSING = "calendar_missing"
    INVALID_REQUEST = "invalid_request"
    INVALID_CLIENT = "invalid_client"
    INVALID_IDENTITY = "invalid_identity"
    INVALID_SCOPE = "invalid_scope"
    INVALID_RESPONSE = "invalid_response"
    REDIRECT = "redirect"


class StatusClass(StrEnum):
    NONE = "none"
    SUCCESS = "2xx"
    REDIRECTION = "3xx"
    CLIENT_ERROR = "4xx"
    SERVER_ERROR = "5xx"
    OTHER = "other"


class RevokeResult(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_INVALID = "already_invalid"


class Failed(Exception):
    def __init__(
        self,
        category: Outcome,
        status_class: StatusClass = StatusClass.NONE,
        retry_after: int | None = None,
    ) -> None:
        self.category = category
        self.status_class = status_class
        self.retry_after = retry_after
        super().__init__(category, status_class)


@dataclass(frozen=True)
class Authorization:
    url: str = field(repr=False)


@dataclass(frozen=True)
class Refreshed:
    access_token: str = field(repr=False)
    expires_in: int
    refresh_token: str | None = field(repr=False)


@dataclass(frozen=True)
class Exchanged:
    access_token: str = field(repr=False)
    expires_in: int
    refresh_token: str = field(repr=False)
    issuer: str
    subject: str = field(repr=False)
    email: str = field(repr=False)


@dataclass(frozen=True)
class Interval:
    start: datetime = field(repr=False)
    end: datetime = field(repr=False)


@dataclass(frozen=True)
class FreeBusy:
    start: datetime = field(repr=False)
    end: datetime = field(repr=False)
    busy: tuple[Interval, ...] = field(repr=False)


@dataclass(frozen=True)
class _Reply:
    status: int
    body: dict[str, Any] | None = field(repr=False)
    retry_after: int | None
    cache_max_age: int


def _status(status: int) -> StatusClass:
    return {
        2: StatusClass.SUCCESS,
        3: StatusClass.REDIRECTION,
        4: StatusClass.CLIENT_ERROR,
        5: StatusClass.SERVER_ERROR,
    }.get(status // 100, StatusClass.OTHER)


def _retry_after(value: str) -> int | None:
    value = value.strip()
    if value.isascii() and value.isdecimal():
        digits = value.lstrip("0")
        return min(int(digits or "0"), MAX_RETRY_AFTER) if len(digits) <= 4 else MAX_RETRY_AFTER
    if value:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is not None:
                return max(0, min(MAX_RETRY_AFTER, int((when - datetime.now(UTC)).total_seconds())))
        except TypeError, ValueError, OverflowError:
            pass
    return None


def _elapsed_budget(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise Failed(Outcome.TIMEOUT) from None
    return remaining


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate JSON member")
    return obj


def _reject_constant(_: str) -> None:
    raise ValueError("invalid JSON number")


def _json_response(response: requests.Response, deadline: float) -> dict[str, Any] | None:
    if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
        return None
    data = bytearray()
    for chunk in response.iter_content(chunk_size=16 * 1024):
        _elapsed_budget(deadline)
        if len(data) + len(chunk) > MAX_BYTES:
            return None
        data.extend(chunk)
    try:
        parsed = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except ValueError, RecursionError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _google_reasons(errors: object) -> set[str]:
    if not isinstance(errors, list):
        return set()
    return {
        error["reason"]
        for error in errors
        if isinstance(error, dict) and isinstance(error.get("reason"), str)
    }


def _calendar_category(reasons: set[str], *, per_calendar: bool = False) -> Outcome:
    if reasons & {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}:
        return Outcome.QUOTA
    if reasons & {"timeRangeEmpty", "invalid", "invalidArgument", "badRequest", "required"}:
        return Outcome.INVALID_REQUEST
    if per_calendar and "notFound" in reasons:
        return Outcome.CALENDAR_MISSING
    if reasons & {"insufficientPermissions", "forbidden"}:
        return Outcome.RECONNECT_REQUIRED
    return Outcome.TRANSIENT


def _failure(op: Operation, reply: _Reply) -> Failed:
    status = reply.status
    body = reply.body or {}
    oauth_error = body.get("error")
    category = Outcome.TRANSIENT
    if 300 <= status < 400:
        category = Outcome.REDIRECT
    elif status == 429:
        category = Outcome.QUOTA
    elif op == Operation.FREEBUSY:
        if status == 401:
            category = Outcome.REFRESH_REQUIRED
        elif status in (400, 403) and isinstance(oauth_error, dict):
            category = _calendar_category(_google_reasons(oauth_error.get("errors")))
            if status != 403 and category == Outcome.RECONNECT_REQUIRED:
                category = Outcome.TRANSIENT
    elif status in (400, 401):
        if op in (Operation.EXCHANGE, Operation.REFRESH) and oauth_error == "invalid_grant":
            category = Outcome.RECONNECT_REQUIRED
        elif oauth_error == "invalid_client":
            category = Outcome.INVALID_CLIENT
        elif oauth_error == "invalid_request":
            category = Outcome.INVALID_REQUEST
    return Failed(category, _status(status), reply.retry_after)


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable():
        raise Failed(Outcome.INVALID_RESPONSE, StatusClass.SUCCESS) from None
    return value


def _validate_scopes(scope_text: object) -> None:
    if not isinstance(scope_text, str):
        raise Failed(Outcome.INVALID_SCOPE, StatusClass.SUCCESS) from None
    granted = {
        "email" if scope == "https://www.googleapis.com/auth/userinfo.email" else scope
        for scope in scope_text.split()
    }
    if granted != set(SCOPES):
        raise Failed(Outcome.INVALID_SCOPE, StatusClass.SUCCESS) from None


def _credentials(data: dict[str, Any]) -> Refreshed:
    access_token = _string(data, "access_token")
    expires = data.get("expires_in")
    refresh_token = _string(data, "refresh_token") if "refresh_token" in data else None
    if (
        _string(data, "token_type").lower() != "bearer"
        or isinstance(expires, bool)
        or not isinstance(expires, int)
        or not 0 < expires <= 2_147_483_647
        or not access_token.isascii()
        or " " in access_token
    ):
        raise Failed(Outcome.INVALID_RESPONSE, StatusClass.SUCCESS) from None
    return Refreshed(access_token, expires, refresh_token)


def _time(value: object) -> datetime:
    if isinstance(value, str) and RFC3339.fullmatch(value):
        try:
            # fromisoformat normalizes out-of-range timezone minutes, which RFC3339 forbids.
            if value.endswith("Z") or int(value[-5:-3]) <= 23 and int(value[-2:]) <= 59:
                return datetime.fromisoformat(value).astimezone(UTC)
        except ValueError, OverflowError:
            pass
    raise Failed(Outcome.INVALID_RESPONSE, StatusClass.SUCCESS) from None


def _utc_bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.utcoffset() is not None and end.utcoffset() is not None:
        try:
            lower, upper = start.astimezone(UTC), end.astimezone(UTC)
            if timedelta(0) < upper - lower <= timedelta(days=90):
                return lower, upper
        except ValueError, OverflowError:
            pass
    raise Failed(Outcome.INVALID_REQUEST) from None


def _window(body: dict[str, Any], start: datetime, end: datetime) -> FreeBusy:
    def invalid() -> Failed:
        return Failed(Outcome.INVALID_RESPONSE, StatusClass.SUCCESS)

    if not body.keys() <= {"kind", "timeMin", "timeMax", "groups", "calendars"}:
        raise invalid() from None
    if (
        body.get("kind") != "calendar#freeBusy"
        or _time(body.get("timeMin")) != start
        or _time(body.get("timeMax")) != end
        or body.get("groups", {}) != {}
    ):
        raise invalid() from None
    calendars = body.get("calendars")
    if not isinstance(calendars, dict):
        raise invalid() from None
    if "primary" not in calendars:
        raise Failed(Outcome.CALENDAR_MISSING, StatusClass.SUCCESS) from None
    calendar = calendars["primary"]
    if calendars.keys() != {"primary"} or not isinstance(calendar, dict):
        raise invalid() from None
    if not calendar.keys() <= {"errors", "busy"}:
        raise invalid() from None
    if "errors" in calendar:
        errors = calendar["errors"]
        if not isinstance(errors, list):
            raise invalid() from None
        if errors:
            category = _calendar_category(_google_reasons(errors), per_calendar=True)
            raise Failed(category, StatusClass.SUCCESS) from None
    busy = calendar.get("busy")
    if not isinstance(busy, list) or len(busy) > MAX_INTERVALS:
        raise invalid() from None
    intervals = []
    for item in busy:
        if not isinstance(item, dict) or item.keys() != {"start", "end"}:
            raise invalid() from None
        opens, closes = _time(item["start"]), _time(item["end"])
        if not (opens < closes and opens < end and start < closes):
            raise invalid() from None
        intervals.append(Interval(opens, closes))
    return FreeBusy(start, end, tuple(intervals))


class Gateway:
    def __init__(self, settings: GoogleCalendarSettings | None = None) -> None:
        self._settings = settings
        self._key_lock = Lock()
        self._key_set: tuple[float, jwt.PyJWKSet] | None = None

    def _config(self) -> GoogleCalendarSettings:
        try:
            settings = self._settings or GoogleCalendarSettings()
            if (
                settings.google_calendar_client_id
                and settings.google_calendar_client_secret.get_secret_value()
                and settings.google_calendar_redirect_uri
            ):
                return settings
        except ValidationError:
            pass
        raise Failed(Outcome.INVALID_CLIENT) from None

    def _call[T](self, op: Operation, run: Callable[[float], T], *, local: bool = False) -> T:
        started = monotonic()
        deadline = started + DEADLINE
        category, status = Outcome.TRANSIENT, StatusClass.NONE
        try:
            if local:
                result = run(deadline)
            else:
                if not _slots.acquire(blocking=False):
                    raise Failed(Outcome.TRANSIENT) from None
                try:
                    future = _workers.submit(run, deadline)
                except RuntimeError:
                    _slots.release()
                    raise Failed(Outcome.TRANSIENT) from None
                future.add_done_callback(lambda _: _slots.release())
                try:
                    result = future.result(timeout=_elapsed_budget(deadline))
                    _elapsed_budget(deadline)
                except TimeoutError:
                    future.cancel()
                    raise Failed(Outcome.TIMEOUT) from None
                status = StatusClass.SUCCESS
            category = Outcome.OK
            if result is RevokeResult.ALREADY_INVALID:
                category, status = Outcome.ALREADY_INVALID, StatusClass.CLIENT_ERROR
            return result
        except Failed as error:
            category, status = error.category, error.status_class
            raise
        finally:
            logger.info(
                "google calendar gateway",
                extra={
                    "op": op,
                    "outcome": category,
                    "status_class": status,
                    "duration_ms": round((monotonic() - started) * 1000),
                },
            )

    def authorization_url(self, *, state: str, nonce: str, verifier: str) -> Authorization:
        def run(_: float) -> Authorization:
            settings = self._config()
            if not state or not nonce or not PKCE_VERIFIER.fullmatch(verifier):
                raise Failed(Outcome.INVALID_REQUEST) from None
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            query = urlencode(
                {
                    "response_type": "code",
                    "client_id": settings.google_calendar_client_id,
                    "redirect_uri": settings.google_calendar_redirect_uri,
                    "state": state,
                    "nonce": nonce,
                    "code_challenge": challenge.decode("ascii").rstrip("="),
                    "code_challenge_method": "S256",
                    "access_type": "offline",
                    "prompt": "select_account consent",
                    "scope": " ".join(SCOPES),
                }
            )
            return Authorization(f"{AUTHORIZATION_URL}?{query}")

        return self._call(Operation.AUTHORIZE, run, local=True)

    @contextmanager
    def _send(
        self, request: requests.PreparedRequest, timeout: Timeout
    ) -> Iterator[requests.Response]:
        # Send through the adapter directly so requests does not prepare or drain a redirect.
        with (
            closing(requests.adapters.HTTPAdapter(max_retries=0)) as adapter,
            # The adapter supports urllib3 Timeout at runtime, but omits it from its annotation.
            adapter.send(request, stream=True, timeout=timeout) as response,  # type: ignore[arg-type]
        ):
            yield response

    def _http(
        self,
        deadline: float,
        url: str,
        *,
        form: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
        access_token: str | None = None,
        read_success: bool = True,
    ) -> _Reply:
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if access_token is not None:
            headers["Authorization"] = f"Bearer {access_token}"
        method = "POST" if form is not None or query is not None else "GET"
        try:
            request = requests.Request(
                method, url, data=form, json=query, headers=headers
            ).prepare()
            remaining = _elapsed_budget(deadline)
            timeout = Timeout(connect=min(2, remaining), read=remaining, total=remaining)
            with self._send(request, timeout) as response:
                status = response.status_code
                read_body = read_success if 200 <= status < 300 else 400 <= status < 500
                if status in (401, 429):
                    read_body = read_body and url == TOKEN_URL and status == 401
                body = _json_response(response, deadline) if read_body else None
                cache = re.search(
                    r"(?:^|,)\s*max-age=([0-9]{1,9})(?=\s*(?:,|$))",
                    response.headers.get("Cache-Control", ""),
                    re.IGNORECASE,
                )
                max_age = min(int(cache[1]), 3600) if cache else 0
                return _Reply(
                    status, body, _retry_after(response.headers.get("Retry-After", "")), max_age
                )
        except requests.Timeout:
            category = Outcome.TIMEOUT
        except requests.RequestException, UnicodeError, ValueError:
            category = Outcome.TIMEOUT if monotonic() >= deadline else Outcome.TRANSIENT
        raise Failed(category) from None

    def _data(self, op: Operation, reply: _Reply) -> dict[str, Any]:
        if reply.status != 200:
            raise _failure(op, reply) from None
        if reply.body is None:
            raise Failed(Outcome.INVALID_RESPONSE, StatusClass.SUCCESS) from None
        return reply.body

    def _signing_key(self, kid: str, deadline: float) -> jwt.PyJWK:
        now = monotonic()
        with self._key_lock:
            cached = self._key_set
        if cached is not None and cached[0] > now:
            try:
                return cached[1][kid]
            except KeyError:
                pass  # Google rotated to a key outside our cached set.
        reply = self._http(deadline, JWKS_URL)
        if reply.status != 200:
            raise _failure(Operation.EXCHANGE, reply) from None
        try:
            keys = jwt.PyJWKSet.from_dict(reply.body or {})
            with self._key_lock:
                self._key_set = (monotonic() + reply.cache_max_age, keys)
            return keys[kid]
        except jwt.PyJWTError, KeyError, TypeError, ValueError, OverflowError, RecursionError:
            pass
        raise Failed(Outcome.INVALID_IDENTITY, StatusClass.SUCCESS) from None

    def _identity(
        self, token: str, client_id: str, nonce: str, deadline: float
    ) -> tuple[str, str, str]:
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                raise Failed(Outcome.INVALID_IDENTITY, StatusClass.SUCCESS) from None
            key = self._signing_key(kid, deadline)
            if (
                key.key_type != "RSA"
                or key.algorithm_name != "RS256"
                or key.public_key_use not in ("sig", None)
            ):
                raise Failed(Outcome.INVALID_IDENTITY, StatusClass.SUCCESS) from None
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                issuer=ISSUERS,
                audience=client_id,
                options={
                    "require": ["iss", "aud", "exp", "nonce", "sub", "email", "email_verified"],
                    "enforce_minimum_key_length": True,
                    "strict_aud": True,
                },
            )
            issuer, subject, email = claims["iss"], claims["sub"], claims["email"]
            if (
                issuer not in ISSUERS
                or ("azp" in claims and claims["azp"] != client_id)
                or any(
                    type(claims[name]) not in (int, float)
                    for name in ("exp", "nbf", "iat")
                    if name in claims
                )
                or not nonce
                or claims["nonce"] != nonce
                or not isinstance(subject, str)
                or not subject.strip()
                or len(subject) > 255
                or not isinstance(email, str)
                or not email.strip()
                or not email.isprintable()
                or claims["email_verified"] is not True
            ):
                raise Failed(Outcome.INVALID_IDENTITY, StatusClass.SUCCESS) from None
            # Google documents both spellings for the same issuer.
            return ISSUERS[0], subject, email
        except jwt.PyJWTError, KeyError, TypeError, ValueError, OverflowError, RecursionError:
            pass
        raise Failed(Outcome.INVALID_IDENTITY, StatusClass.SUCCESS) from None

    def exchange_code(self, *, code: str, verifier: str, nonce: str) -> Exchanged:
        def run(deadline: float) -> Exchanged:
            if not code or not nonce or not PKCE_VERIFIER.fullmatch(verifier):
                raise Failed(Outcome.INVALID_REQUEST) from None
            settings = self._config()
            reply = self._http(
                deadline,
                TOKEN_URL,
                form={
                    "grant_type": "authorization_code",
                    "client_id": settings.google_calendar_client_id,
                    "client_secret": settings.google_calendar_client_secret.get_secret_value(),
                    "redirect_uri": settings.google_calendar_redirect_uri,
                    "code": code,
                    "code_verifier": verifier,
                },
            )
            data = self._data(Operation.EXCHANGE, reply)
            _validate_scopes(data.get("scope"))
            tokens = _credentials(data)
            if tokens.refresh_token is None:
                raise Failed(Outcome.INVALID_RESPONSE, StatusClass.SUCCESS) from None
            issuer, subject, email = self._identity(
                _string(data, "id_token"), settings.google_calendar_client_id, nonce, deadline
            )
            return Exchanged(
                tokens.access_token, tokens.expires_in, tokens.refresh_token, issuer, subject, email
            )

        return self._call(Operation.EXCHANGE, run)

    def refresh(self, *, refresh_token: str) -> Refreshed:
        def run(deadline: float) -> Refreshed:
            settings = self._config()
            reply = self._http(
                deadline,
                TOKEN_URL,
                form={
                    "grant_type": "refresh_token",
                    "client_id": settings.google_calendar_client_id,
                    "client_secret": settings.google_calendar_client_secret.get_secret_value(),
                    "refresh_token": refresh_token,
                },
            )
            data = self._data(Operation.REFRESH, reply)
            if "scope" in data:
                _validate_scopes(data["scope"])
            return _credentials(data)

        return self._call(Operation.REFRESH, run)

    def freebusy(self, *, access_token: str, start: datetime, end: datetime) -> FreeBusy:
        def run(deadline: float) -> FreeBusy:
            lower, upper = _utc_bounds(start, end)
            reply = self._http(
                deadline,
                FREEBUSY_URL,
                query={
                    "items": [{"id": "primary"}],
                    "timeZone": "UTC",
                    "timeMin": lower.isoformat().replace("+00:00", "Z"),
                    "timeMax": upper.isoformat().replace("+00:00", "Z"),
                },
                access_token=access_token,
            )
            return _window(self._data(Operation.FREEBUSY, reply), lower, upper)

        return self._call(Operation.FREEBUSY, run)

    def revoke(self, *, refresh_token: str) -> RevokeResult:
        def run(deadline: float) -> RevokeResult:
            reply = self._http(
                deadline, REVOKE_URL, form={"token": refresh_token}, read_success=False
            )
            if reply.status == 200:
                return RevokeResult.ACCEPTED
            if reply.status == 400 and reply.body and reply.body.get("error") == "invalid_token":
                return RevokeResult.ALREADY_INVALID
            raise _failure(Operation.REVOKE, reply) from None

        return self._call(Operation.REVOKE, run)


gateway = Gateway()
