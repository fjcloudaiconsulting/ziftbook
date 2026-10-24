"""ZIF-71: second_factors and its definer functions (migration 0039), at the database level.

Calls run as the app role unless noted. "As migrate" reads stand for a dump or an operator.
"""

import hashlib
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from app import admin, totp
from app.main import create_app
from tests.conftest import (
    SHOWN_TICKETS,
    add_user,
    email_of,
    issue_link,
    new_client,
    wait_until_blocked,
)

# A fixed argon2 string: start_admin_session only compares it with the stored one.
HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA"
NOW_STEP = "floor(extract(epoch FROM now()) / 30)::bigint"
PASS = text("SELECT outcome, failures FROM pass_second_factor(:id, :p, :step)")
BEGIN = text("SELECT begin_totp_enrolment(:id, :ticket, :ct, :p)")
CONFIRM = text(f"SELECT outcome, failures FROM confirm_totp_enrolment(:id, :p, {NOW_STEP}, :codes)")
RECOVER = text("SELECT codes_left, was_locked FROM use_recovery_code(:id, :p)")
TICKET = re.compile(r"[A-Z2-7]{4}(?:-[A-Z2-7]{4}){3}")


def new_admin(migrate_engine: Engine) -> uuid.UUID:
    user_id = add_user(migrate_engine)
    with migrate_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO password_credentials VALUES (:u, :h)"), {"u": user_id, "h": HASH}
        )
        conn.execute(text("INSERT INTO platform_admins (user_id) VALUES (:u)"), {"u": user_id})
    return user_id


def half_session(app_engine: Engine, migrate_engine: Engine, user_id: uuid.UUID) -> bytes:
    """A new admin session that has passed only the password, minted with the stored hash."""
    with migrate_engine.connect() as conn:
        stored = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": user_id}
        )
    id_hash = hashlib.sha256(uuid.uuid4().bytes).digest()
    with app_engine.begin() as conn:
        assert conn.scalar(
            text("SELECT start_admin_session(:u, :h, :id, NULL, NULL)"),
            {"u": user_id, "h": stored, "id": id_hash},
        )
    return id_hash


def issue_ticket(user_id: uuid.UUID) -> str:
    """python -m app.admin enrol, as an operator runs it: the ticket it prints."""
    SHOWN_TICKETS.clear()
    assert admin.main(["enrol", email_of(user_id), "--reason", "new authenticator"]) == 0
    (ticket,) = SHOWN_TICKETS
    assert TICKET.fullmatch(ticket)
    return ticket


def ticket_proof(ticket: str) -> bytes:
    return totp.proof(totp.normalise(ticket))


def code_proofs(codes: list[str]) -> list[bytes]:
    return [totp.proof(totp.normalise(c)) for c in codes]


def call(engine: Engine, sql: Any, **params: Any) -> Any:
    """One definer call in its own transaction: its first row, or None."""
    with engine.begin() as conn:
        return conn.execute(sql, params).first()


def begin(app_engine: Engine, id_hash: bytes, proof: bytes, user_id: uuid.UUID, s: bytes) -> str:
    row = call(
        app_engine, BEGIN, id=id_hash, ticket=proof, ct=totp.encrypt(s, user_id), p=totp.proof(s)
    )
    return str(row[0])


def pass_(app_engine: Engine, id_hash: bytes, proof: bytes, step: int | None) -> tuple[str, int]:
    row = call(app_engine, PASS, id=id_hash, p=proof, step=step)
    return (row[0], row[1])


def recover(app_engine: Engine, id_hash: bytes, proof: bytes) -> tuple[int, bool] | None:
    row = call(app_engine, RECOVER, id=id_hash, p=proof)
    return None if row is None else (row[0], row[1])


def factor(migrate_engine: Engine, user_id: uuid.UUID) -> dict[str, Any]:
    """The second_factors row, read as migrate (a dump)."""
    with migrate_engine.connect() as conn:
        row = (
            conn.execute(text("SELECT * FROM second_factors WHERE user_id = :u"), {"u": user_id})
            .mappings()
            .first()
        )
    return dict(row) if row else {}


def session_factor(migrate_engine: Engine, id_hash: bytes) -> Any:
    """That session's second_factor_at; "gone" when the session no longer exists."""
    with migrate_engine.connect() as conn:
        row = conn.execute(
            text("SELECT second_factor_at FROM admin_sessions WHERE id_hash = :id"), {"id": id_hash}
        ).first()
    return "gone" if row is None else row[0]


def resolved(app_engine: Engine, id_hash: bytes) -> dict[str, Any]:
    with app_engine.begin() as conn:
        row = (
            conn.execute(text("SELECT * FROM resolve_admin_session(:id)"), {"id": id_hash})
            .mappings()
            .first()
        )
    return dict(row) if row else {}


def next_step(migrate_engine: Engine, user_id: uuid.UUID) -> int:
    step: int = factor(migrate_engine, user_id)["last_step"] + 1
    return step


@dataclass(frozen=True)
class Enrolled:
    """An admin enrolled through the CLI, begin and confirm.

    Step rule: confirm already used t0, the database's step at that moment (last_step = t0), so a
    pass at the same step is refused by `p_step > last_step` before the proof is looked at. A test
    that expects `passed` uses next_step() (last_step + 1, read as migrate just before the call),
    valid while the database's step is t0, t0 + 1 or t0 + 2: at least 60 s after enrolled(). A test
    that expects a refusal reuses the exact step it already used, never a recomputed one.
    """

    user_id: uuid.UUID
    session: bytes  # the confirming session, its factor passed
    secret: bytes
    codes: list[str]


def enrolled(app_engine: Engine, migrate_engine: Engine) -> Enrolled:
    user_id = new_admin(migrate_engine)
    ticket = issue_ticket(user_id)
    session = half_session(app_engine, migrate_engine, user_id)
    s = totp.new_secret()
    assert begin(app_engine, session, ticket_proof(ticket), user_id, s) == "begun"
    codes = [totp.new_code() for _ in range(10)]
    row = call(app_engine, CONFIRM, id=session, p=totp.proof(s), codes=code_proofs(codes))
    assert tuple(row) == ("enrolled", 0)
    return Enrolled(user_id, session, s, codes)


def wrong() -> bytes:
    return secrets.token_bytes(32)


# fence: the app role must never read or change a second factor. Kills 0001's default DML left in
# place. Injected by deleting the REVOKE.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM second_factors",
        "INSERT INTO second_factors (user_id) VALUES (:u)",
        "UPDATE second_factors SET failed_attempts = 0",
        "DELETE FROM second_factors",
    ],
)
def test_app_role_has_no_access_to_second_factors(
    app_engine: Engine, migrate_engine: Engine, sql: str
) -> None:
    user_id = add_user(migrate_engine)  # a real user: the INSERT must fail on privilege, not the FK
    with pytest.raises(ProgrammingError) as error, app_engine.begin() as conn:
        conn.execute(text(sql), {"u": user_id})
    assert isinstance(error.value.orig, InsufficientPrivilege)


# guard: live_admin_user is no definer and not the app's to call (calling it would fail anyway on
# admin_sessions, which the app role can't read). Kills a REVOKE left out.
def test_app_role_cannot_call_live_admin_user(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        assert not conn.scalar(
            text("SELECT has_function_privilege('live_admin_user(bytea)', 'EXECUTE')")
        )


# fence: a half session plus a made-up proof never passes. Kills a function that trusts the step
# without a proof. Injected by deleting `f.proof_hash = sha256(p_proof) AND`.
def test_injection_cannot_pass_the_factor(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    step = next_step(migrate_engine, e.user_id)

    assert pass_(app_engine, session, wrong(), step) == ("failed", 1)
    assert resolved(app_engine, session)["factor_passed"] is False

    assert pass_(app_engine, session, totp.proof(e.secret), step) == ("passed", 1)  # the control
    assert resolved(app_engine, session)["factor_passed"] is True


# fence: what a dump holds is never a proof. Kills comparing the parameter with the stored value.
# Injected by replacing sha256(p_...) with p_... in pass, use_recovery_code and begin, one by one.
def test_dumped_values_are_not_proofs(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    dump = factor(migrate_engine, e.user_id)
    kept = ("secret_ct", "proof_hash", "recovery_code_hashes")

    session = half_session(app_engine, migrate_engine, e.user_id)
    step = next_step(migrate_engine, e.user_id)
    assert pass_(app_engine, session, dump["proof_hash"], step)[0] == "failed"
    assert session_factor(migrate_engine, session) is None
    assert recover(app_engine, session, dump["recovery_code_hashes"][0]) is None
    assert session_factor(migrate_engine, session) is None
    after = factor(migrate_engine, e.user_id)
    assert {k: after[k] for k in kept} == {k: dump[k] for k in kept}
    assert after["failed_attempts"] == 1  # the pass leg's failure, and nothing else

    # The controls, after their refusals and at the same step (a control first would make the
    # refusal come from replay).
    assert pass_(app_engine, session, totp.proof(e.secret), step)[0] == "passed"
    other = half_session(app_engine, migrate_engine, e.user_id)
    assert recover(app_engine, other, code_proofs(e.codes)[0]) == (9, False)

    # begin, on an admin with a pending ticket and no confirmed factor (a confirmed one answers
    # already_enrolled before the ticket is compared).
    pending = new_admin(migrate_engine)
    ticket = issue_ticket(pending)
    pending_session = half_session(app_engine, migrate_engine, pending)
    stored_ticket = factor(migrate_engine, pending)["ticket_hash"]
    s = totp.new_secret()
    assert begin(app_engine, pending_session, stored_ticket, pending, s) == "invalid_ticket"
    assert factor(migrate_engine, pending)["secret_ct"] is None
    assert begin(app_engine, pending_session, ticket_proof(ticket), pending, s) == "begun"


# fence: the decrypted secret itself is not the proof. Kills totp.proof returning its input, which
# puts the secret in Postgres statement logs. Injected by making proof the identity on bytes.
def test_the_raw_secret_is_not_the_proof(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    step = next_step(migrate_engine, e.user_id)

    assert pass_(app_engine, session, e.secret, step)[0] == "failed"
    assert pass_(app_engine, session, totp.proof(e.secret), step)[0] == "passed"


# fence: a step is accepted once, across sessions. Kills no `p_step > last_step`. Injected by
# deleting that condition.
def test_a_step_is_accepted_once(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    proof = totp.proof(e.secret)
    step = next_step(migrate_engine, e.user_id)
    first = half_session(app_engine, migrate_engine, e.user_id)
    assert pass_(app_engine, first, proof, step) == ("passed", 0)

    second = half_session(app_engine, migrate_engine, e.user_id)
    assert pass_(app_engine, second, proof, step) == ("failed", 1)
    assert session_factor(migrate_engine, second) is None


# fence: two submissions of one code at once pass once. Kills a read without FOR UPDATE (both see
# the old last_step). Injected by deleting FOR UPDATE.
def test_concurrent_submissions_of_one_code_pass_once(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    proof = totp.proof(e.secret)
    step = next_step(migrate_engine, e.user_id)
    first = half_session(app_engine, migrate_engine, e.user_id)
    second = half_session(app_engine, migrate_engine, e.user_id)
    results: list[Any] = []

    def submit() -> None:
        results.append(pass_(app_engine, second, proof, step))

    with app_engine.connect() as conn1:
        tx = conn1.begin()
        assert tuple(conn1.execute(PASS, {"id": first, "p": proof, "step": step}).one()) == (
            "passed",
            0,
        )
        thread = threading.Thread(target=submit)
        thread.start()
        wait_until_blocked(migrate_engine, 1)
        tx.commit()
    thread.join(10)
    assert results == [("failed", 1)]


# fence: the right proof at a step outside now +-1 is refused and moves nothing. Kills no window
# bound (one poisoned future step locks the admin out for good) and no lower bound (an old, unused
# code still passes). Injected by deleting BETWEEN, then separately its lower bound.
@pytest.mark.parametrize("ahead", [2, 1000, -2])
def test_steps_outside_the_window_are_refused(
    app_engine: Engine, migrate_engine: Engine, ahead: int
) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    with migrate_engine.begin() as conn:  # so only the window can refuse a step in the past
        conn.execute(
            text("UPDATE second_factors SET last_step = 0 WHERE user_id = :u"), {"u": e.user_id}
        )
    before = factor(migrate_engine, e.user_id)["last_step"]
    sql = text(f"SELECT outcome FROM pass_second_factor(:id, :p, {NOW_STEP} + :ahead)")

    row = call(app_engine, sql, id=session, p=totp.proof(e.secret), ahead=ahead)

    assert row[0] == "failed"
    assert factor(migrate_engine, e.user_id)["last_step"] == before


# fence: ten failures in a row lock TOTP, and a success before that resets the count. Kills no
# lock, a lock at another count, or a success that doesn't reset. Injected by deleting the
# `>= 10` check, then separately the reset.
def test_ten_failures_lock_and_success_resets(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    proof = totp.proof(e.secret)
    session = half_session(app_engine, migrate_engine, e.user_id)
    step = next_step(migrate_engine, e.user_id)
    for n in range(1, 10):
        assert pass_(app_engine, session, wrong(), step) == ("failed", n)
    assert pass_(app_engine, session, proof, step) == ("passed", 9)
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 0

    # A second admin: a second success for the first would need step t0 + 2, which the window
    # refuses until the database's clock reaches t0 + 1, so the lock would not be what refused it.
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    step = next_step(migrate_engine, e.user_id)
    for n in range(1, 11):
        assert pass_(app_engine, session, wrong(), step) == ("failed", n)
    assert pass_(app_engine, session, totp.proof(e.secret), step) == ("locked", 10)
    assert resolved(app_engine, session)["factor_passed"] is False


# fence: a recovery code works once and unlocks TOTP. Kills a reusable code, or a use that doesn't
# unlock. Injected by deleting the array_remove assignment, then separately `failed_attempts = 0`.
def test_a_recovery_code_works_once_and_unlocks(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    for _ in range(10):
        pass_(app_engine, session, wrong(), None)
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 10
    code = code_proofs(e.codes)[3]

    assert recover(app_engine, session, code) == (9, True)
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 0
    assert resolved(app_engine, session)["factor_passed"] is True

    again = half_session(app_engine, migrate_engine, e.user_id)
    assert recover(app_engine, again, code) is None
    assert session_factor(migrate_engine, again) is None
    step = next_step(migrate_engine, e.user_id)
    assert pass_(app_engine, again, totp.proof(e.secret), step) == ("passed", 0)


# fence: begin needs a live ticket and never overwrites a confirmed secret. Kills overwriting a
# confirmed secret. Injected by deleting the already_enrolled check.
def test_begin_needs_a_live_ticket(app_engine: Engine, migrate_engine: Engine) -> None:
    s = totp.new_secret()
    user_id = new_admin(migrate_engine)
    session = half_session(app_engine, migrate_engine, user_id)
    assert begin(app_engine, session, wrong(), user_id, s) == "invalid_ticket"  # no ticket at all
    assert factor(migrate_engine, user_id) == {}

    ticket = issue_ticket(user_id)
    session = half_session(app_engine, migrate_engine, user_id)
    assert begin(app_engine, session, ticket_proof(totp.new_code()), user_id, s) == "invalid_ticket"
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE second_factors SET ticket_expires_at = now() - interval '1 second' "
                "WHERE user_id = :u"
            ),
            {"u": user_id},
        )
    assert begin(app_engine, session, ticket_proof(ticket), user_id, s) == "invalid_ticket"
    assert factor(migrate_engine, user_id)["secret_ct"] is None

    e = enrolled(app_engine, migrate_engine)
    secret_ct = factor(migrate_engine, e.user_id)["secret_ct"]
    ticket = totp.new_code()
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE second_factors SET ticket_hash = sha256(:p), "
                "ticket_expires_at = now() + interval '1 hour' WHERE user_id = :u"
            ),
            {"p": ticket_proof(ticket), "u": e.user_id},
        )
    assert begin(app_engine, e.session, ticket_proof(ticket), e.user_id, s) == "already_enrolled"
    assert factor(migrate_engine, e.user_id)["secret_ct"] == secret_ct


# fence: confirming uses the ticket up and signs out every other session of the admin. Kills a
# ticket left behind after enrolment. Injected by deleting `ticket_hash = NULL,
# ticket_expires_at = NULL`.
def test_confirm_uses_up_the_ticket_and_ends_other_sessions(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    user_id = new_admin(migrate_engine)
    ticket = issue_ticket(user_id)
    session = half_session(app_engine, migrate_engine, user_id)
    other = half_session(app_engine, migrate_engine, user_id)
    s = totp.new_secret()
    assert begin(app_engine, session, ticket_proof(ticket), user_id, s) == "begun"
    codes = code_proofs([totp.new_code() for _ in range(10)])

    row = call(app_engine, CONFIRM, id=session, p=totp.proof(s), codes=codes)

    assert tuple(row) == ("enrolled", 0)
    stored = factor(migrate_engine, user_id)
    assert (stored["ticket_hash"], stored["ticket_expires_at"]) == (None, None)
    assert stored["recovery_code_hashes"] == [hashlib.sha256(c).digest() for c in codes]
    assert session_factor(migrate_engine, other) == "gone"
    assert session_factor(migrate_engine, session) is not None
    assert resolved(app_engine, session)["enrolled"] is True
    assert begin(app_engine, session, ticket_proof(ticket), user_id, s) == "already_enrolled"


# fence: confirming counts failures too. Kills a password holder guess-confirming the operator's
# pending secret. Injected by deleting the counter check in confirm.
def test_confirm_counts_failures(app_engine: Engine, migrate_engine: Engine) -> None:
    user_id = new_admin(migrate_engine)
    ticket = issue_ticket(user_id)
    session = half_session(app_engine, migrate_engine, user_id)
    s = totp.new_secret()
    assert begin(app_engine, session, ticket_proof(ticket), user_id, s) == "begun"
    codes = code_proofs([totp.new_code() for _ in range(10)])
    for n in range(1, 11):
        row = call(app_engine, CONFIRM, id=session, p=wrong(), codes=codes)
        assert tuple(row) == ("failed", n)

    row = call(app_engine, CONFIRM, id=session, p=totp.proof(s), codes=codes)

    assert tuple(row) == ("locked", 10)
    assert factor(migrate_engine, user_id)["confirmed_at"] is None


# fence: a password reset leaves the second factor in place (whoever controls the inbox still faces
# TOTP) and ends admin sessions. Kills a reset that wipes the factor. Injected by adding
# `DELETE FROM second_factors s WHERE s.user_id = v_user;` to reset_password.
def test_password_reset_keeps_the_second_factor(
    app_engine: Engine, migrate_engine: Engine, bound: None
) -> None:
    e = enrolled(app_engine, migrate_engine)
    before = factor(migrate_engine, e.user_id)
    token = issue_link(app_engine, "password_reset", email_of(e.user_id))
    assert token is not None

    response = new_client(create_app()).post(
        "/api/password-reset/complete", json={"token": token, "password": "quiet-copper-kettle-7"}
    )

    assert response.status_code == 204
    after = factor(migrate_engine, e.user_id)
    assert after == before
    assert after["confirmed_at"] is not None and len(after["recovery_code_hashes"]) == 10
    assert session_factor(migrate_engine, e.session) == "gone"
