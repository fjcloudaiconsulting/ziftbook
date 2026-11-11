"""Platform admin sessions (ZIF-70), their second factor (ZIF-71), and audited business access.

An admin session is its own cookie and its own table, never a business session: it needs no
membership, and the database fixes its lifetimes (migration 0037). The app reaches admin_sessions
and second_factors only through definer functions.

Password sign-in starts a half session. Three routers, each guarded by the router itself, so a route
added later can't forget its guard: admin_half_router (any live session: who-am-I, sign-out, the
second factor and its enrolment), admin_router (the second factor passed: every read) and
admin_write_router (passed within the last 15 minutes, decided in SQL: every write). Each guard
answers before the handler, or its body, is looked at. tests/test_admin_routes.py pins the list.
"""

import base64
import logging
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, computed_field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import admin, auth, limits, passwords, totp
from app.db import SessionLocal, tenant_context
from app.errors import ApiError, Error

ADMIN_COOKIE = "__Host-admin-session"
# The cookie's max_age only: the database decides when a session ends (0037).
ADMIN_ABSOLUTE = timedelta(hours=8)

RESOLVE = text("SELECT * FROM resolve_admin_session(:id_hash)")
CODE_WINDOW = timedelta(minutes=15)  # the per-address limits on codes and tickets
# ZB-18: an admin's failures from anywhere, business sign-in's included (auth.PASSWORD_ACTIONS), ask
# for a Turnstile challenge, never a lock. Business sign-in's threshold: above what one address gets
# across both routes, and the cap on guesses at an admin's password across them. The warning counts
# this route's own.
ADMIN_FAILURES_BEFORE_CHALLENGE = auth.FAILURES_BEFORE_CHALLENGE
TOTP_CODE = re.compile("[0-9]{6}")  # ASCII only: \d and str.isdigit accept Arabic-Indic digits

logger = logging.getLogger(__name__)


class AdminSession(BaseModel):
    user_id: UUID
    email: str
    name: str | None
    # From resolve_admin_session (0039), which fixes the 15 minutes of "fresh". Never in a body.
    factor_passed: bool = Field(exclude=True)
    factor_fresh: bool = Field(exclude=True)
    enrolled: bool = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def second_factor(self) -> Literal["passed", "required", "enrolment_required"]:
        """What the web app shows next: nothing, the code prompt, or how to enrol."""
        if self.factor_passed:
            return "passed"
        return "required" if self.enrolled else "enrolment_required"


class CodeIn(BaseModel):
    code: str = Field(max_length=40)  # 6 digits, or a recovery code with spaces or dashes


class TicketIn(BaseModel):
    ticket: str = Field(max_length=40)


class EnrolmentOut(BaseModel):
    secret: str  # base32, the manual key
    otpauth_uri: str
    qr: str  # an SVG data URI, for <img src>


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]  # shown once: only their hashes are stored


def set_admin_cookie(response: Response, token: str) -> None:
    # SameSite=Strict, unlike the business cookie: no admin action starts from another site's link.
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        max_age=int(ADMIN_ABSOLUTE.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_admin_cookie(response: Response) -> None:
    response.delete_cookie(ADMIN_COOKIE, path="/", secure=True, httponly=True, samesite="strict")


def admin_session(request: Request) -> AdminSession:
    """Any live admin session, the second factor passed or not."""
    token = request.cookies.get(ADMIN_COOKIE)
    row = None
    if token:
        # No db session is yielded: there is no admin tenant. business() opens one per business.
        with SessionLocal.begin() as session:
            row = session.execute(RESOLVE, {"id_hash": auth.hash_token(token)}).first()
    if row is None:
        raise ApiError(401, "unauthenticated")
    request.state.admin_user_id = row.user_id  # for the access line
    return AdminSession.model_validate(row, from_attributes=True)


HalfAdmin = Annotated[AdminSession, Depends(admin_session)]


def admin_signed_in(current: HalfAdmin) -> AdminSession:
    if not current.factor_passed:
        raise ApiError(403, "second_factor_required")
    return current


def admin_recently_verified(
    current: Annotated[AdminSession, Depends(admin_signed_in)],
) -> AdminSession:
    if not current.factor_fresh:
        raise ApiError(403, "second_factor_stale")
    return current


CurrentAdmin = Annotated[AdminSession, Depends(admin_signed_in)]

# Only what a half session needs: who-am-I, sign-out, and getting the second factor.
admin_half_router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(admin_session)]
)
# Every ZIF-72..76 read route goes here: the second factor passed in this session.
admin_router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(admin_signed_in)]
)
# Every admin write goes here: the second factor passed within 15 minutes.
admin_write_router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(admin_recently_verified)]
)
# Only sign-in: nobody holds an admin session before it.
admin_sign_in_router = APIRouter(prefix="/api/admin", tags=["admin"])


@admin_sign_in_router.post(
    "/session",
    responses={status: {"model": Error} for status in (401, 415, 422, 429, 503)},
)
def sign_in(credentials: auth.Credentials, request: Request, response: Response) -> AdminSession:
    """Sign a platform admin in. An unknown email, a wrong password and someone who is not an admin
    get the same answer, after the same work."""
    ip = request.client.host if request.client else None
    address_key = limits.ip_key("admin_sign_in", ip, v6_prefix=limits.AUTH_V6_PREFIX)
    # The per-admin key embeds the narrower /64, so a neighbour in the /48 cannot spend it.
    pair_address = limits.ip_key("admin_sign_in", ip)
    # Every attempt counts, before anything else, so a correct password doesn't get past the limit.
    if limits.hit(
        {
            # Email and address together: an email alone would let anyone who knows it lock the
            # admin out. The password only buys a half session now (ZIF-71).
            limits.email_key("admin_sign_in", f"{credentials.email}|{pair_address}"): 5,
            # ZB-18: business sign-in's attempts from this address too, for every email, so one
            # address gets PER_ADDRESS_LIMIT attempts at an email across both routes, fewer than
            # the challenge needs.
            auth.attempt_key(credentials.email, limits.ip_key("sign_in", ip)): (
                auth.PER_ADDRESS_LIMIT
            ),
            address_key: 20,
        },
        auth.SIGN_IN_WINDOW,
    ):
        raise ApiError(429, "rate_limited")
    auth.require_challenge(
        "admin_sign_in",
        credentials.email,
        credentials.turnstile_token,
        ip,
        ADMIN_FAILURES_BEFORE_CHALLENGE,
    )
    with SessionLocal.begin() as session:
        account = session.execute(
            text("SELECT * FROM account_by_email(:email)"), {"email": credentials.email}
        ).first()
    # No database connection is held while hashing; an unknown email costs the same hash.
    verified = passwords.verify(account.password_hash if account else None, credentials.password)
    token = secrets.token_urlsafe(32)
    id_hash = auth.hash_token(token)
    ip_address, user_agent = auth.origin(request)
    with SessionLocal.begin() as session:
        # Called on every path, the hash only when it was just verified: a failure does the same
        # work whichever check failed, so timing tells no one who is an admin.
        started = session.scalar(
            text("SELECT start_admin_session(:user_id, :hash, :id_hash, :ip, :user_agent)"),
            {
                "user_id": account.user_id if account else None,
                "hash": account.password_hash if account and verified else None,
                "id_hash": id_hash,
                "ip": ip_address,
                "user_agent": user_agent,
            },
        )
        if not started:
            target = f"user:{account.user_id}" if account else None
            admin.record(
                session, request, "admin_sign_in_failed", actor_user_id=None, target=target
            )
        else:
            # One transaction with the start: a session row never exists without its audit event.
            assert account is not None
            # Whatever admin session this browser had ends here, as auth.start does.
            if old := request.cookies.get(ADMIN_COOKIE):
                session.execute(
                    text("SELECT end_admin_session(:id_hash)"), {"id_hash": auth.hash_token(old)}
                )
            admin.record(session, request, "admin_signed_in", actor_user_id=account.user_id)
            signed_in_as = AdminSession.model_validate(
                session.execute(RESOLVE, {"id_hash": id_hash}).one(), from_attributes=True
            )
    if not started:
        # Unknown emails count too, under the same key: the answers can't tell who is an admin.
        if auth.count_failure(credentials.email, "admin_sign_in", ADMIN_FAILURES_BEFORE_CHALLENGE):
            logger.warning(
                "admin sign-in failures",
                extra={"target": f"user:{account.user_id}" if account else None},
            )
        raise ApiError(401, "invalid_credentials")
    set_admin_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return signed_in_as


@admin_half_router.get("/session", responses={401: {"model": Error}})
def read(current: HalfAdmin, response: Response) -> AdminSession:
    # One person's identity: never for a shared cache.
    response.headers["Cache-Control"] = "no-store"
    return current


@admin_half_router.delete(
    "/session", status_code=204, responses={401: {"model": Error}, 415: {"model": Error}}
)
def sign_out(current: HalfAdmin, request: Request, response: Response) -> None:
    """Sign this browser's admin session out. Guarded, unlike business sign-out: an expired cookie
    gets 401, so no route under /api/admin answers 2xx without an admin session."""
    with SessionLocal.begin() as session:
        ended = session.scalar(
            text("SELECT end_admin_session(:id_hash)"),
            {"id_hash": auth.hash_token(request.cookies[ADMIN_COOKIE])},
        )
        if ended:
            admin.record(session, request, "admin_signed_out", actor_user_id=ended)
    clear_admin_cookie(response)


def session_hash(request: Request) -> bytes:
    return auth.hash_token(request.cookies[ADMIN_COOKIE])  # the guard has resolved it


def _limited(request: Request, action: str, limit: int) -> None:
    """Per address only: a per-user limit would let anyone lock the admin out."""
    ip = request.client.host if request.client else None
    if limits.hit({limits.ip_key(action, ip): limit}, CODE_WINDOW):
        raise ApiError(429, "rate_limited")


def _key_needed() -> None:
    if totp.key() is None:
        logger.error("totp key unset, invalid or dev value: admin second factor unavailable")
        raise ApiError(503, "second_factor_unavailable")


def _secret(session: Session, id_hash: bytes, user_id: UUID) -> bytes | None:
    """The session user's TOTP secret, begun or confirmed; None if there is none."""
    blob = session.scalar(text("SELECT totp_ciphertext(:id)"), {"id": id_hash})
    if blob is None:
        return None
    try:
        return totp.decrypt(blob, user_id)
    except InvalidTag, ValueError:
        # Another key than the one it was encrypted with: an operator mistake, never a wrong code,
        # or every admin would lock themselves out.
        logger.error("totp secret undecryptable: check ZIF_TOTP_KEY")
        raise ApiError(503, "second_factor_unavailable") from None


@admin_half_router.post(
    "/session/second-factor",
    responses={status: {"model": Error} for status in (401, 403, 409, 415, 422, 429, 503)},
)
def pass_second_factor(body: CodeIn, current: HalfAdmin, request: Request) -> AdminSession:
    """Pass the second factor with a 6-digit code or a recovery code. Also the 15-minute re-check:
    a new step, or another recovery code, refreshes it."""
    if not current.enrolled:
        raise ApiError(409, "enrolment_required")
    typed = totp.normalise(body.code)
    id_hash = session_hash(request)
    error: ApiError | None = None
    if TOTP_CODE.fullmatch(typed):
        _limited(request, "admin_2fa", 20)
        _key_needed()
        # Counted first, committed in a transaction of its own that never sees the code (ZIF-261),
        # as limits.hit counts a limit: a check cut off before its commit leaves the attempt
        # counted. The secret is opened first: a key that can't open it counts nothing.
        with SessionLocal.begin() as session:
            s = _secret(session, id_hash, current.user_id)
            if s is None:  # the session died or the factor was wiped since the guard
                raise ApiError(401, "unauthenticated")
            charged, failures, charge = session.execute(
                text("SELECT outcome, failures, charge FROM charge_second_factor(:id)"),
                {"id": id_hash},
            ).one()
            if charged == "locked":
                admin.record(
                    session,
                    request,
                    "second_factor_failed",
                    actor_user_id=current.user_id,
                    details={"method": "totp", "failures": failures, "locked": True},
                )
            elif charged == "charged":
                # Commits with the charge: a check cut off later cannot erase that a code was tried.
                admin.record(
                    session,
                    request,
                    "second_factor_attempted",
                    actor_user_id=current.user_id,
                    details={"method": "totp", "failures": failures},
                )
        if charged == "locked":
            raise ApiError(403, "second_factor_locked")
        if charged == "none":  # the session died or the factor was wiped since the guard
            raise ApiError(401, "unauthenticated")
        step = totp.matching_step(s, typed, datetime.now(UTC))
        with SessionLocal.begin() as session:
            # reported: the failures since the last pass when it passed, the count now when not.
            outcome, reported = session.execute(
                text("SELECT outcome, failures FROM check_second_factor(:id, :charge, :p, :step)"),
                {"id": id_hash, "charge": charge, "p": totp.proof(s), "step": step},
            ).one()
            if outcome == "passed":
                admin.record(
                    session,
                    request,
                    "second_factor_passed",
                    actor_user_id=current.user_id,
                    details={"method": "totp", "failed_since_last": reported},
                )
                if reported:
                    logger.warning(
                        "second factor passed after failures", extra={"failures": reported}
                    )
            elif outcome == "failed":
                # failures: this attempt's own count, from its charge, for the audit and the line.
                # The count now differs when other attempts are in flight: their charges raise it,
                # their passes lower it.
                # The charge already counted it, and its attempt event is committed. Logged before
                # the commit: a check cut off after it answered still leaves this line.
                logger.warning(
                    "second factor failed",
                    extra={"user_id": str(current.user_id), "failures": failures},
                )
                admin.record(
                    session,
                    request,
                    "second_factor_failed",
                    actor_user_id=current.user_id,
                    details={"method": "totp", "failures": failures},
                )
                error = ApiError(401, "invalid_code")
                # Locked only when this attempt made it 10 and no pass has given one back since:
                # past 10 no charge succeeds, so both at 10 means this code locked it.
                if failures >= 10 and reported >= 10:
                    admin.record(
                        session,
                        request,
                        "second_factor_locked",
                        actor_user_id=current.user_id,
                        details={"failures": failures},
                    )
                    logger.error("second factor locked")
                    error = ApiError(403, "second_factor_locked")
            else:  # none: the session died, the factor was wiped or the charge voided since
                error = ApiError(401, "unauthenticated")
            passed_as = None if error else session.execute(RESOLVE, {"id_hash": id_hash}).first()
    else:
        # Anything else is a recovery code. It never needs the key: with the key lost, recovery
        # codes still get admins in.
        _limited(request, "admin_recovery", 10)
        with SessionLocal.begin() as session:
            used = session.execute(
                text("SELECT codes_left, was_locked FROM use_recovery_code(:id, :p)"),
                {"id": id_hash, "p": totp.proof(typed)},
            ).first()
            if used is not None:
                admin.record(
                    session,
                    request,
                    "recovery_code_used",
                    actor_user_id=current.user_id,
                    details={"codes_left": used.codes_left},
                )
                if used.was_locked:
                    admin.record(
                        session, request, "second_factor_unlocked", actor_user_id=current.user_id
                    )
            else:
                admin.record(
                    session,
                    request,
                    "second_factor_failed",
                    actor_user_id=current.user_id,
                    details={"method": "recovery_code"},
                )
                error = ApiError(401, "invalid_code")
            passed_as = None if error else session.execute(RESOLVE, {"id_hash": id_hash}).first()
    if error is not None:
        raise error
    if passed_as is None:
        raise ApiError(401, "unauthenticated")
    return AdminSession.model_validate(passed_as, from_attributes=True)


@admin_half_router.post(
    "/totp/enrolment",
    responses={status: {"model": Error} for status in (401, 403, 409, 415, 422, 429, 503)},
)
def begin_enrolment(body: TicketIn, current: HalfAdmin, request: Request) -> EnrolmentOut:
    """Start TOTP enrolment with the ticket `python -m app.admin enrol` printed. Until it is
    confirmed, starting again with the same ticket replaces the secret. Once 10 wrong confirm
    codes stopped it, that ticket starts nothing (403 second_factor_locked, as confirm answers):
    only a new ticket does."""
    _limited(request, "admin_enrol", 10)
    _key_needed()
    s = totp.new_secret()
    id_hash = session_hash(request)
    ticket = totp.proof(totp.normalise(body.ticket))
    locked = False
    with SessionLocal.begin() as session:
        outcome = session.scalar(
            text("SELECT begin_totp_enrolment(:id, :ticket, :ct, :p)"),
            {
                "id": id_hash,
                "ticket": ticket,
                "ct": totp.encrypt(s, current.user_id),
                "p": totp.proof(s),
            },
        )
        if outcome == "invalid_ticket":
            # begin refuses a stopped enrolment's ticket as a wrong one (0084); 0095's
            # totp_enrolment_locked tells them apart (tests pin the two to the same 10).
            locked = bool(
                session.scalar(
                    text("SELECT totp_enrolment_locked(:id, :ticket)"),
                    {"id": id_hash, "ticket": ticket},
                )
            )
            admin.record(
                session,
                request,
                "totp_enrolment_failed",
                actor_user_id=current.user_id,
                details={"locked": True} if locked else None,
            )
    if outcome == "invalid_ticket":
        raise ApiError(*((403, "second_factor_locked") if locked else (401, "invalid_ticket")))
    if outcome == "already_enrolled":
        raise ApiError(409, "already_enrolled")
    uri = totp.otpauth_uri(s, current.email)
    return EnrolmentOut(
        secret=base64.b32encode(s).decode(), otpauth_uri=uri, qr=totp.qr_data_uri(uri)
    )


@admin_half_router.post(
    "/totp/enrolment/confirm",
    responses={status: {"model": Error} for status in (401, 403, 409, 415, 422, 429, 503)},
)
def confirm_enrolment(body: CodeIn, current: HalfAdmin, request: Request) -> RecoveryCodesOut:
    """Confirm enrolment with a code from the app. Passes the second factor, ends this admin's
    other sessions, and answers the 10 recovery codes, once."""
    _limited(request, "admin_2fa", 20)
    _key_needed()
    id_hash = session_hash(request)
    codes = [totp.new_code() for _ in range(10)]
    # Counted first, in a transaction of its own, as pass_second_factor's code (ZIF-261).
    with SessionLocal.begin() as session:
        s = _secret(session, id_hash, current.user_id)
        if s is None:
            raise ApiError(409, "no_pending_enrolment")
        charged, failures, charge = session.execute(
            text("SELECT outcome, failures, charge FROM charge_totp_enrolment(:id)"),
            {"id": id_hash},
        ).one()
        if charged == "charged":
            # Commits with the charge, as at sign-in.
            admin.record(
                session,
                request,
                "totp_enrolment_attempted",
                actor_user_id=current.user_id,
                details={"failures": failures},
            )
    if charged == "locked":
        raise ApiError(403, "second_factor_locked")
    if charged == "none":  # nothing begun, or the ticket expired
        raise ApiError(409, "no_pending_enrolment")
    step = totp.matching_step(s, totp.normalise(body.code), datetime.now(UTC))
    with SessionLocal.begin() as session:
        # failures stays the charge's: this attempt's own count, as at sign-in.
        outcome, _ = session.execute(
            text(
                "SELECT outcome, failures "
                "FROM check_totp_enrolment(:id, :charge, :p, :step, :codes)"
            ),
            {
                "id": id_hash,
                "charge": charge,
                "p": totp.proof(s),
                "step": step,
                "codes": [totp.proof(totp.normalise(c)) for c in codes],
            },
        ).one()
        if outcome == "enrolled":
            admin.record(session, request, "totp_enrolled", actor_user_id=current.user_id)
        elif outcome == "failed":
            # Before the commit, as pass_second_factor's failure line.
            logger.warning(
                "totp enrolment failed",
                extra={"user_id": str(current.user_id), "failures": failures},
            )
            admin.record(
                session,
                request,
                "totp_enrolment_failed",
                actor_user_id=current.user_id,
                details={"failures": failures},
            )
    if outcome == "failed":
        raise ApiError(
            *((403, "second_factor_locked") if failures >= 10 else (401, "invalid_code"))
        )
    if outcome == "none":  # the ticket expired, or the charge was voided, since the charge
        raise ApiError(409, "no_pending_enrolment")
    return RecoveryCodesOut(recovery_codes=codes)


@contextmanager
def business(
    current: AdminSession,
    request: Request,
    tenant_id: UUID,
    *,
    target: str,
    action: admin.Action = "business_read",
    reason: str | None = None,
) -> Iterator[Session]:
    """One business's data, for an admin read or write: a tenant_context, with it recorded first.

    The record is in the same transaction and before the yield: if it fails the body never runs,
    and a body that fails (an ApiError included) leaves no record and no change. One business per
    transaction. 404 for an unknown one. A write passes its action and reason.
    """
    with tenant_context(tenant_id) as session:
        # No row-level security on tenants: this is a plain existence check.
        if session.scalar(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant_id}) is None:
            raise ApiError(404, "not_found")
        admin.record(
            session,
            request,
            action,
            actor_user_id=current.user_id,
            tenant_id=tenant_id,
            target=target,
            reason=reason,
        )
        request.state.tenant_id = tenant_id
        yield session
