"""ZIF-71: second_factors and its definer functions (migration 0039), at the database level.

Calls run as the app role unless noted. "As migrate" reads stand for a dump or an operator. A TOTP
code is sent as the API sends it since ZIF-261 (0083): charged in a transaction of its own, then
checked in another.
"""

import hashlib
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

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
# 0069's one-transaction pass, which the release before ZIF-261 calls.
PASS = text("SELECT outcome, failures FROM pass_second_factor(:id, :p, :step)")
CHARGE = text("SELECT outcome, failures, charge FROM charge_second_factor(:id)")
CHECK = text("SELECT outcome, failures FROM check_second_factor(:id, :charge, :p, :step)")
BEGIN = text("SELECT begin_totp_enrolment(:id, :ticket, :ct, :p)")
# 0069's one-transaction confirm, which the release before ZIF-261 calls.
CONFIRM = text(f"SELECT outcome, failures FROM confirm_totp_enrolment(:id, :p, {NOW_STEP}, :codes)")
CHARGE_ENROLMENT = text("SELECT outcome, failures, charge FROM charge_totp_enrolment(:id)")
CHECK_ENROLMENT = text(
    f"SELECT outcome, failures FROM check_totp_enrolment(:id, :charge, :p, {NOW_STEP}, :codes)"
)
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


def charge(app_engine: Engine, id_hash: bytes) -> tuple[str, int, uuid.UUID | None]:
    """charge_second_factor, committed: (outcome, failures, the charge)."""
    row = call(app_engine, CHARGE, id=id_hash)
    return (row[0], row[1], row[2])


def check(
    app_engine: Engine, id_hash: bytes, charged: uuid.UUID | None, proof: bytes, step: int | None
) -> tuple[str, int]:
    row = call(app_engine, CHECK, id=id_hash, charge=charged, p=proof, step=step)
    return (row[0], row[1])


def pass_(app_engine: Engine, id_hash: bytes, proof: bytes, step: int | None) -> tuple[str, int]:
    """One code, as the API sends it: charged and committed, then checked. A charge that answers
    locked or none is the answer."""
    outcome, failures, charged = charge(app_engine, id_hash)
    if outcome != "charged":
        return (outcome, failures)
    return check(app_engine, id_hash, charged, proof, step)


def recover(app_engine: Engine, id_hash: bytes, proof: bytes) -> tuple[int, bool] | None:
    row = call(app_engine, RECOVER, id=id_hash, p=proof)
    return None if row is None else (row[0], row[1])


def confirm(
    app_engine: Engine, id_hash: bytes, proof: bytes, codes: list[bytes]
) -> tuple[str, int]:
    """An enrolment confirm code at the database's current step, as the API sends it: charged and
    committed, then checked. A charge that answers locked or none is the answer."""
    outcome, failures, charged = call(app_engine, CHARGE_ENROLMENT, id=id_hash)
    if outcome != "charged":
        return (outcome, failures)
    row = call(app_engine, CHECK_ENROLMENT, id=id_hash, charge=charged, p=proof, codes=codes)
    return (row[0], row[1])


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
    assert confirm(app_engine, session, totp.proof(s), code_proofs(codes)) == ("enrolled", 0)
    return Enrolled(user_id, session, s, codes)


def wrong() -> bytes:
    return secrets.token_bytes(32)


# One wrong code leaks away per this long (ZIF-234, 0069). The tests' own literal, never the
# migration's constant: a test that read the constant could not catch a wrong one.
LEAK = timedelta(hours=8)
ABOUT = timedelta(seconds=5)  # how far a test's clock reading may trail the function's now()


def set_failures(
    migrate_engine: Engine,
    user_id: uuid.UUID,
    count: int,
    ago: timedelta | None,
    since_pass: int = 0,
) -> None:
    """As migrate: the count, its leak anchor `ago` before now (None: no anchor), the report."""
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE second_factors SET failed_attempts = :n, failures_since_pass = :s, "
                "failures_leaked_at = now() - CAST(:ago AS interval) WHERE user_id = :u"
            ),
            {"n": count, "s": since_pass, "ago": ago, "u": user_id},
        )


def shift_anchor(migrate_engine: Engine, user_id: uuid.UUID, by: timedelta) -> None:
    """As migrate: the leak anchor moved `by` into the past, as if that much time went by."""
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE second_factors SET failures_leaked_at = failures_leaked_at - :by "
                "WHERE user_id = :u"
            ),
            {"by": by, "u": user_id},
        )


def anchor_age(migrate_engine: Engine, user_id: uuid.UUID) -> timedelta | None:
    """How long ago the leak anchor is; None when there is none."""
    with migrate_engine.connect() as conn:
        age: timedelta | None = conn.scalar(
            text("SELECT now() - failures_leaked_at FROM second_factors WHERE user_id = :u"),
            {"u": user_id},
        )
    return age


def reopen_steps(migrate_engine: Engine, user_id: uuid.UUID) -> int:
    """As migrate: forget the last accepted step, so the database's current step passes again
    (another pass for the same admin without waiting 30 s). That step."""
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE second_factors SET last_step = 0 WHERE user_id = :u"), {"u": user_id}
        )
        step: int = conn.scalar(text(f"SELECT {NOW_STEP}"))
    return step


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


# fence (ZIF-234): neither runtime role holds any privilege on any column of second_factors, the
# columns 0069 adds included: a column grant on failures_leaked_at would let the app role set the
# anchor back and leak the whole count. Kills a column-level grant the table-level checks miss.
# Injected by `GRANT UPDATE (failures_leaked_at) ON second_factors TO ziftbook_app` in 0069, then
# separately `GRANT SELECT (failures_since_pass) ... TO ziftbook_worker`.
@pytest.mark.parametrize("role", ["ziftbook_app", "ziftbook_worker"])
def test_runtime_roles_hold_no_column_of_second_factors(migrate_engine: Engine, role: str) -> None:
    with migrate_engine.connect() as conn:
        held = {
            privilege: conn.scalar(
                text("SELECT has_any_column_privilege(:role, 'second_factors', :privilege)"),
                {"role": role, "privilege": privilege},
            )
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES")
        }
    assert held == {"SELECT": False, "INSERT": False, "UPDATE": False, "REFERENCES": False}


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


# fence: two submissions of one code at once pass once. Kills a check that reads without FOR UPDATE
# (both see the old last_step). Injected by deleting FOR UPDATE from check_second_factor. Both are
# charged before either is checked, so the pass reports the other, still in flight (ZIF-261).
def test_concurrent_submissions_of_one_code_pass_once(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    proof = totp.proof(e.secret)
    step = next_step(migrate_engine, e.user_id)
    first = half_session(app_engine, migrate_engine, e.user_id)
    second = half_session(app_engine, migrate_engine, e.user_id)
    charges = [charge(app_engine, session)[2] for session in (first, second)]
    results: list[Any] = []

    def submit() -> None:
        results.append(check(app_engine, second, charges[1], proof, step))

    with app_engine.connect() as conn1:
        tx = conn1.begin()
        row = conn1.execute(CHECK, {"id": first, "charge": charges[0], "p": proof, "step": step})
        assert tuple(row.one()) == ("passed", 1)
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
    sql = text(f"SELECT outcome FROM check_second_factor(:id, :c, :p, {NOW_STEP} + :ahead)")
    charged = charge(app_engine, session)[2]

    row = call(app_engine, sql, id=session, c=charged, p=totp.proof(e.secret), ahead=ahead)

    assert row[0] == "failed"
    assert factor(migrate_engine, e.user_id)["last_step"] == before


# fence: ten failures lock TOTP, and a pass before that keeps the count (ZIF-234: only time lowers
# it). Kills no lock, a lock at another count, or a pass that resets. Injected by deleting the
# `>= 10` check, then separately by restoring 0039's `failed_attempts = 0` on a pass.
def test_ten_failures_lock_and_a_pass_keeps_the_count(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    proof = totp.proof(e.secret)
    session = half_session(app_engine, migrate_engine, e.user_id)
    step = next_step(migrate_engine, e.user_id)
    for n in range(1, 10):
        assert pass_(app_engine, session, wrong(), step) == ("failed", n)
    assert pass_(app_engine, session, proof, step) == ("passed", 9)
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 9

    # A second admin: a second success for the first would need step t0 + 2, which the window
    # refuses until the database's clock reaches t0 + 1, so the lock would not be what refused it.
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    step = next_step(migrate_engine, e.user_id)
    for n in range(1, 11):
        assert pass_(app_engine, session, wrong(), step) == ("failed", n)
    assert pass_(app_engine, session, totp.proof(e.secret), step) == ("locked", 10)
    assert resolved(app_engine, session)["factor_passed"] is False


# fence: a recovery code works once and lifts the lock, leaving the count one short of it (ZIF-234:
# no pass refills the budget), so the next wrong code locks again. Kills a reusable code, a use
# that doesn't unlock, or one that refills. Injected by deleting the array_remove assignment, then
# separately by `least(failed_attempts, 9)` -> `failed_attempts`, then by -> `0`.
def test_a_recovery_code_works_once_and_unlocks(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    for _ in range(10):
        pass_(app_engine, session, wrong(), None)
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 10
    code = code_proofs(e.codes)[3]

    assert recover(app_engine, session, code) == (9, True)
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 9
    assert resolved(app_engine, session)["factor_passed"] is True

    again = half_session(app_engine, migrate_engine, e.user_id)
    assert recover(app_engine, again, code) is None
    assert session_factor(migrate_engine, again) is None
    step = next_step(migrate_engine, e.user_id)
    assert pass_(app_engine, again, totp.proof(e.secret), step) == ("passed", 0)
    assert pass_(app_engine, again, wrong(), step) == ("failed", 10)


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

    row = confirm(app_engine, session, totp.proof(s), codes)

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
        row = confirm(app_engine, session, wrong(), codes)
        assert tuple(row) == ("failed", n)

    row = confirm(app_engine, session, totp.proof(s), codes)

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


# fence (ZIF-234, the regression): a pass lowers no count, so ten wrong codes lock whatever passes
# come between them. Kills a pass that resets the count. Injected by restoring 0039's
# `failed_attempts = 0` on a pass.
def test_a_pass_does_not_reset_the_failure_count(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    proof = totp.proof(e.secret)
    session = half_session(app_engine, migrate_engine, e.user_id)
    step = next_step(migrate_engine, e.user_id)
    for n in range(1, 10):
        assert pass_(app_engine, session, wrong(), step) == ("failed", n)

    assert pass_(app_engine, session, proof, step) == ("passed", 9)

    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 9
    assert pass_(app_engine, session, wrong(), step) == ("failed", 10)
    assert pass_(app_engine, session, proof, reopen_steps(migrate_engine, e.user_id)) == (
        "locked",
        10,
    )


# fence (ZIF-234): passes between rounds of wrong codes refill nothing: the tenth wrong code locks.
# Kills a pass that lowers the count by any amount. Injected by `failed_attempts =
# greatest(failed_attempts - 3, 0)` on a pass.
def test_interleaved_passes_do_not_refill_the_budget(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    proof = totp.proof(e.secret)
    session = half_session(app_engine, migrate_engine, e.user_id)
    for round_ in range(3):
        for n in range(1, 4):
            assert pass_(app_engine, session, wrong(), None) == ("failed", 3 * round_ + n)
        step = reopen_steps(migrate_engine, e.user_id)
        assert pass_(app_engine, session, proof, step) == ("passed", 3)

    assert pass_(app_engine, session, wrong(), None) == ("failed", 10)


# fence (ZIF-234): one failure leaks away per 8 hours since the anchor, none before, and a bucket
# that empties starts its leak again from now. Kills a longer interval or none; the boundary (and
# test_the_leak_keeps_its_remainder) kills a shorter one; the last case kills an emptied bucket
# that keeps its part interval. Injected by '8 hours' -> '16 hours', then -> '6 hours', then by
# deleting the leak, then by `v_anchor := v_anchor + v_leaked * LEAK` for an emptied bucket too.
def test_one_failure_leaks_away_per_eight_hours(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)

    def after(count: int, ago: timedelta) -> tuple[str, int]:
        set_failures(migrate_engine, e.user_id, count, ago)
        return pass_(app_engine, session, wrong(), None)

    assert after(5, LEAK - timedelta(minutes=1)) == ("failed", 6)
    assert after(9, LEAK) == ("failed", 9)
    assert after(9, 2 * LEAK) == ("failed", 8)
    assert after(9, 5 * LEAK) == ("failed", 5)
    assert after(9, 20 * LEAK + timedelta(hours=3)) == ("failed", 1)
    age = anchor_age(migrate_engine, e.user_id)
    assert age is not None and age < ABOUT  # emptied: the anchor starts again now


# fence (ZIF-234): the anchor moves by whole intervals, so the part of an interval already gone
# still counts toward the next leak. Kills an anchor reset to now() on every failure (a slower leak
# for real admins), and pins the interval to the second. Injected by `v_anchor := now()` for every
# failure, then by '8 hours' -> '7 hours'.
def test_the_leak_keeps_its_remainder(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    set_failures(migrate_engine, e.user_id, 5, LEAK + timedelta(hours=7))

    assert pass_(app_engine, session, wrong(), None) == ("failed", 5)

    age = anchor_age(migrate_engine, e.user_id)
    assert age is not None and abs(age - timedelta(hours=7)) < ABOUT


# fence (ZIF-234, the real admin's side): one wrong code per 8 hours never builds up. Kills a slower
# leak. Injected by '8 hours' -> '9 hours'.
def test_one_wrong_code_per_eight_hours_never_locks(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    outcomes = []
    for _ in range(30):
        outcomes.append(pass_(app_engine, session, wrong(), None))
        shift_anchor(migrate_engine, e.user_id, LEAK)

    assert outcomes == [("failed", 1)] * 30


# fence (ZIF-234): a lock never leaks away, however long ago its last failure; only a recovery code
# or `app.admin enrol` lifts it. Kills the leak applied before the lock check. Injected by checking
# the leaked level instead of the stored count against 10.
def test_a_lock_never_leaks_away(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    for _ in range(10):
        pass_(app_engine, session, wrong(), None)
    shift_anchor(migrate_engine, e.user_id, timedelta(days=7))

    step = reopen_steps(migrate_engine, e.user_id)
    assert pass_(app_engine, session, totp.proof(e.secret), step) == ("locked", 10)
    assert pass_(app_engine, session, wrong(), None) == ("locked", 10)


# fence (ZIF-234): an anchor after the transaction's now() (a transaction that began before the
# last failure committed) leaks nothing, and never adds one. Kills a leak that is not clamped at
# zero. Injected by deleting `greatest(..., 0)`.
def test_an_anchor_ahead_of_now_leaks_nothing(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    set_failures(migrate_engine, e.user_id, 3, -timedelta(hours=1))

    assert pass_(app_engine, session, wrong(), None) == ("failed", 4)


# fence (ZIF-234): two codes at once both count. Kills a charge that reads the count without the row
# lock (the second writes the count it read before the first committed: a lost update). Injected
# by deleting FOR UPDATE from charge_second_factor.
def test_concurrent_wrong_codes_both_count(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    first = half_session(app_engine, migrate_engine, e.user_id)
    second = half_session(app_engine, migrate_engine, e.user_id)
    results: list[Any] = []

    def submit() -> None:
        results.append(charge(app_engine, second)[:2])

    with app_engine.connect() as conn1:
        tx = conn1.begin()
        row = conn1.execute(CHARGE, {"id": first}).one()
        assert tuple(row)[:2] == ("charged", 1)
        thread = threading.Thread(target=submit)
        thread.start()
        wait_until_blocked(migrate_engine, 1)
        tx.commit()
    thread.join(10)

    assert results == [("charged", 2)]
    stored = factor(migrate_engine, e.user_id)
    assert (stored["failed_attempts"], stored["failures_since_pass"]) == (2, 2)


# fence (ZIF-234): `passed` answers the failures since the last pass, not the count (the audit's
# failed_since_last). Kills a pass that answers the count. Injected by answering f.failed_attempts.
def test_passed_reports_the_failures_since_the_last_pass(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    proof = totp.proof(e.secret)
    session = half_session(app_engine, migrate_engine, e.user_id)
    for _ in range(3):
        pass_(app_engine, session, wrong(), None)
    assert pass_(app_engine, session, proof, reopen_steps(migrate_engine, e.user_id)) == (
        "passed",
        3,
    )
    pass_(app_engine, session, wrong(), None)

    assert pass_(app_engine, session, proof, reopen_steps(migrate_engine, e.user_id)) == (
        "passed",
        1,
    )
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 4


# fence (ZIF-234): a pass leaves the leak anchor alone. Kills a pass that restarts the leak (a
# slower leak for an admin who passes often). Injected by adding `failures_leaked_at = now()` to
# the pass's UPDATE. Counted first (ZIF-261), the attempt's charge takes the leak due by now, which
# the pass then keeps: the whole interval gone, its remainder still running.
def test_a_pass_leaves_the_leak_alone(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    set_failures(migrate_engine, e.user_id, 5, LEAK + timedelta(hours=7), since_pass=5)

    step = reopen_steps(migrate_engine, e.user_id)
    assert pass_(app_engine, session, totp.proof(e.secret), step) == ("passed", 5)

    age = anchor_age(migrate_engine, e.user_id)
    assert age is not None and abs(age - timedelta(hours=7)) < ABOUT
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 4
    assert pass_(app_engine, session, wrong(), None) == ("failed", 5)


# fence (ZIF-234): a recovery code that lifts a lock restarts the leak from now, so an old lock's
# anchor can't leak the 9 it leaves away at once (10 fresh guesses). Kills the anchor left as it
# was. Injected by replacing the CASE with f.failures_leaked_at.
def test_a_recovery_code_off_a_lock_restarts_the_leak(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    for _ in range(10):
        pass_(app_engine, session, wrong(), None)
    shift_anchor(migrate_engine, e.user_id, timedelta(days=7))

    assert recover(app_engine, session, code_proofs(e.codes)[0]) == (9, True)

    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 9
    age = anchor_age(migrate_engine, e.user_id)
    assert age is not None and age < ABOUT
    assert pass_(app_engine, session, wrong(), None) == ("failed", 10)


# fence (ZIF-234): a recovery code with no lock leaves the count and its anchor, and clears only
# the report, up to the count one short of the lock. Kills a recovery code that lowers the count or
# restarts the leak, also at 9. Injected by `failed_attempts = 0`, then separately by
# `failures_leaked_at = now()` for every use, then by the anchor's `>= 10` -> `>= 9`.
@pytest.mark.parametrize("count", [4, 9])
def test_a_recovery_code_off_lock_leaves_the_count(
    app_engine: Engine, migrate_engine: Engine, count: int
) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    set_failures(migrate_engine, e.user_id, count, timedelta(hours=3), since_pass=count)

    assert recover(app_engine, session, code_proofs(e.codes)[0]) == (9, False)

    stored = factor(migrate_engine, e.user_id)
    assert (stored["failed_attempts"], stored["failures_since_pass"]) == (count, 0)
    age = anchor_age(migrate_engine, e.user_id)
    assert age is not None and abs(age - timedelta(hours=3)) < ABOUT


# fence (ZIF-234): a new enrolment starts with no failures, whichever CLI version issued its ticket
# (the CLI's upsert resets only failed_attempts). Kills confirm leaving the report or the anchor
# behind. Injected by deleting `failures_leaked_at = NULL, failures_since_pass = 0` from confirm.
def test_a_new_enrolment_starts_with_no_failures(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    set_failures(migrate_engine, e.user_id, 5, timedelta(hours=1), since_pass=5)
    ticket = issue_ticket(e.user_id)
    session = half_session(app_engine, migrate_engine, e.user_id)
    s = totp.new_secret()
    assert begin(app_engine, session, ticket_proof(ticket), e.user_id, s) == "begun"
    codes = code_proofs([totp.new_code() for _ in range(10)])

    row = confirm(app_engine, session, totp.proof(s), codes)

    assert tuple(row) == ("enrolled", 0)
    stored = factor(migrate_engine, e.user_id)
    assert (
        stored["failed_attempts"],
        stored["failures_since_pass"],
        stored["failures_leaked_at"],
    ) == (0, 0, None)


# fence (ZIF-234): the anchor is a finite time and the report never negative. Kills an anchor of
# -infinity (every code an error) or +infinity (no leak, ever). Injected by deleting each CHECK.
@pytest.mark.parametrize(
    "assignment",
    [
        "failures_leaked_at = '-infinity'",
        "failures_leaked_at = 'infinity'",
        "failures_since_pass = -1",
    ],
)
def test_the_failure_columns_hold_sane_values(
    app_engine: Engine, migrate_engine: Engine, assignment: str
) -> None:
    e = enrolled(app_engine, migrate_engine)
    with pytest.raises(IntegrityError) as error, migrate_engine.begin() as conn:
        conn.execute(
            text(f"UPDATE second_factors SET {assignment} WHERE user_id = :u"), {"u": e.user_id}
        )
    assert isinstance(error.value.orig, CheckViolation)


def charges_of(migrate_engine: Engine, user_id: uuid.UUID) -> list[uuid.UUID]:
    found: list[uuid.UUID] = factor(migrate_engine, user_id)["charges"]
    return found


# fence (ZIF-261, the attack): an attempt is counted in a transaction of its own before its code is
# looked at, so a check cut off before its commit, rolled back or its backend terminated (which the
# app role can do to its own backends), still leaves the count, and ten of them lock. Kills a count
# taken inside the check's transaction. Injected by moving the `+ 1` from charge_second_factor into
# check_second_factor's failure branch.
@pytest.mark.parametrize("cut", ["rollback", "terminate"])
def test_a_check_cut_off_before_its_commit_still_counts(
    app_engine: Engine, migrate_engine: Engine, cut: str
) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    for n in range(1, 11):
        outcome, failures, charged = charge(app_engine, session)
        assert (outcome, failures) == ("charged", n)
        conn = app_engine.connect()
        try:
            conn.begin()
            pid = conn.scalar(text("SELECT pg_backend_pid()"))
            row = conn.execute(
                CHECK, {"id": session, "charge": charged, "p": wrong(), "step": None}
            ).one()
            assert tuple(row) == ("failed", n)
            if cut == "rollback":
                conn.rollback()
            else:
                with app_engine.connect() as killer:
                    assert killer.scalar(
                        text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid}
                    )
                conn.invalidate()
        finally:
            conn.close()
        assert factor(migrate_engine, e.user_id)["failed_attempts"] == n

    assert charge(app_engine, session)[:2] == ("locked", 10)
    step = reopen_steps(migrate_engine, e.user_id)
    assert pass_(app_engine, session, totp.proof(e.secret), step) == ("locked", 10)


# fence (ZIF-261): a check needs a live charge of this admin, made and not yet checked, or it looks
# at nothing, the right code included. Kills a check that passes without a charge (an uncounted
# attempt), with another admin's, or after it expired. Injected by deleting the charge test, then by
# dropping its NULL guard (coalesce), then by deleting the age test.
@pytest.mark.parametrize("which", ["none", "made_up", "expired", "other_admin"])
def test_a_check_needs_its_own_live_charge(
    app_engine: Engine, migrate_engine: Engine, which: str
) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    set_failures(migrate_engine, e.user_id, 3, timedelta(hours=1))
    charge(app_engine, session)  # another attempt in flight: there is a live charge to confuse
    charged: uuid.UUID | None = None
    if which == "made_up":
        charged = uuid.uuid4()
    elif which == "expired":
        with migrate_engine.begin() as conn:
            charged = conn.scalar(
                text(
                    "UPDATE second_factors SET charges = charges || uuidv7(- interval '121 s') "
                    "WHERE user_id = :u RETURNING charges[2]"
                ),
                {"u": e.user_id},
            )
    elif which == "other_admin":
        other = enrolled(app_engine, migrate_engine)
        charged = charge(app_engine, half_session(app_engine, migrate_engine, other.user_id))[2]
    step = next_step(migrate_engine, e.user_id)

    assert check(app_engine, session, charged, totp.proof(e.secret), step) == ("none", 0)

    assert session_factor(migrate_engine, session) is None
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 4
    assert pass_(app_engine, session, totp.proof(e.secret), step) == ("passed", 1)  # the control


# fence (ZIF-261): a charge lives 2 minutes. Kills a longer or shorter life. Injected by
# '2 minutes' -> '3 minutes', then -> '1 minute'.
def test_a_charge_lives_two_minutes(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    with migrate_engine.begin() as conn:
        young, old = conn.execute(
            text(
                "UPDATE second_factors SET charges = ARRAY[uuidv7(- interval '110 seconds'), "
                "uuidv7(- interval '130 seconds')] WHERE user_id = :u "
                "RETURNING charges[1], charges[2]"
            ),
            {"u": e.user_id},
        ).one()

    assert check(app_engine, session, old, wrong(), None) == ("none", 0)
    assert check(app_engine, session, young, wrong(), None) == ("failed", 0)


# fence (ZIF-261): a charge is checked once, so a pass gives back one attempt, once. Kills a check
# that leaves its charge behind (a second pass with it refunds again). Injected by deleting the
# array_remove from the pass branch, then from the failure branch.
def test_a_charge_is_checked_once(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    proof = totp.proof(e.secret)
    session = half_session(app_engine, migrate_engine, e.user_id)
    set_failures(migrate_engine, e.user_id, 5, timedelta(hours=1))
    _, _, passed = charge(app_engine, session)
    _, _, failed = charge(app_engine, session)

    # 1: the other attempt, still in flight, counts as a failure since the last pass.
    assert check(app_engine, session, passed, proof, next_step(migrate_engine, e.user_id)) == (
        "passed",
        1,
    )
    assert check(app_engine, session, failed, wrong(), None) == ("failed", 6)
    for used in (passed, failed):
        step = reopen_steps(migrate_engine, e.user_id)
        assert check(app_engine, session, used, proof, step) == ("none", 0)

    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 6
    assert charges_of(migrate_engine, e.user_id) == []


# fence (ZIF-261): a pass gives back its own charge only; attempts cut off before their check stay
# counted. Kills a pass that clears every charge or the whole count. Injected by `charges = '{}'`
# with `failed_attempts - cardinality(charges)` in the pass branch.
def test_a_pass_gives_back_only_its_own_charge(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    cut_off = [charge(app_engine, session)[2] for _ in range(3)]  # never checked
    _, failures, own = charge(app_engine, session)
    assert failures == 4

    step = next_step(migrate_engine, e.user_id)
    assert check(app_engine, session, own, totp.proof(e.secret), step) == ("passed", 3)

    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 3
    assert charges_of(migrate_engine, e.user_id) == cut_off


# fence (ZIF-261): the lock is decided at the charge: at 10 a charge counts nothing and answers
# locked, while the attempt that made it 10 is still checked, as the 10th code always was. Kills a
# charge past the lock, or a check that refuses its own 10th. Injected by deleting the `>= 10` test
# in charge_second_factor, then by adding it to check_second_factor.
def test_the_lock_is_decided_at_the_charge(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    set_failures(migrate_engine, e.user_id, 9, timedelta(hours=1))
    _, failures, tenth = charge(app_engine, session)
    assert failures == 10

    assert charge(app_engine, session) == ("locked", 10, None)
    assert len(charges_of(migrate_engine, e.user_id)) == 1

    step = next_step(migrate_engine, e.user_id)
    assert check(app_engine, session, tenth, totp.proof(e.secret), step) == ("passed", 0)
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 9


# fence (ZIF-261): a charge drops the expired ones, so charges of checks that never came do not
# pile up. Kills a list that only grows. Injected by deleting the filter on the kept charges.
def test_a_charge_drops_expired_charges(app_engine: Engine, migrate_engine: Engine) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    with migrate_engine.begin() as conn:
        young = conn.scalar(
            text(
                "UPDATE second_factors SET charges = ARRAY[uuidv7(- interval '1 hour'), "
                "uuidv7(- interval '10 seconds')] WHERE user_id = :u RETURNING charges[2]"
            ),
            {"u": e.user_id},
        )

    _, _, made = charge(app_engine, session)

    assert charges_of(migrate_engine, e.user_id) == [young, made]


# fence (ZIF-261): a recovery code that lifts a lock leaves 9 and voids the attempts in flight, so
# none of their passes gives back an attempt the 9 already forgot. Kills charges kept across the
# lift (a pass then refunds below 9: one more guess). Injected by deleting the CASE on charges in
# use_recovery_code.
def test_a_recovery_code_off_a_lock_voids_the_charges(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    set_failures(migrate_engine, e.user_id, 9, timedelta(hours=1))
    _, failures, in_flight = charge(app_engine, session)
    assert failures == 10

    assert recover(app_engine, session, code_proofs(e.codes)[0]) == (9, True)

    step = next_step(migrate_engine, e.user_id)
    assert check(app_engine, session, in_flight, totp.proof(e.secret), step) == ("none", 0)
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 9
    assert charges_of(migrate_engine, e.user_id) == []


# guard (ZIF-261): a recovery code with no lock leaves the count, so the attempts in flight keep
# their charges and a pass gives back its own, exactly.
def test_a_recovery_code_off_lock_keeps_the_charges(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    session = half_session(app_engine, migrate_engine, e.user_id)
    set_failures(migrate_engine, e.user_id, 3, timedelta(hours=1))
    _, _, in_flight = charge(app_engine, session)

    assert recover(app_engine, session, code_proofs(e.codes)[0]) == (9, False)

    step = next_step(migrate_engine, e.user_id)
    assert check(app_engine, session, in_flight, totp.proof(e.secret), step) == ("passed", 0)
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 3


def plant_charge(migrate_engine: Engine, user_id: uuid.UUID) -> uuid.UUID:
    """As migrate: a live charge on the row, as an attempt in flight leaves one."""
    with migrate_engine.begin() as conn:
        planted: uuid.UUID = conn.scalar(
            text(
                "UPDATE second_factors SET charges = charges || uuidv7() WHERE user_id = :u "
                "RETURNING charges[cardinality(charges)]"
            ),
            {"u": user_id},
        )
    return planted


# fence (ZIF-261): every reset of the secret or the count voids the attempts in flight: the CLI's
# new ticket, beginning an enrolment and confirming one. A charge made before must never be checked
# after: against the new secret, or as a failure the reset already forgot (an uncounted attempt).
# Kills a reset that keeps the charges. Injected by deleting `charges = '{}'` from ISSUE_TICKET,
# then from begin_totp_enrolment, then from check_totp_enrolment, then from 0069's
# confirm_totp_enrolment (the release before ZIF-261 still confirms with it).
@pytest.mark.parametrize("reset", ["cli", "begin", "confirm", "confirm_0069"])
def test_a_reset_voids_the_charges(app_engine: Engine, migrate_engine: Engine, reset: str) -> None:
    if reset == "cli":
        e = enrolled(app_engine, migrate_engine)
        _, _, before = charge(app_engine, half_session(app_engine, migrate_engine, e.user_id))
        ticket = issue_ticket(e.user_id)
        assert charges_of(migrate_engine, e.user_id) == []
        # The whole way back, within the charge's 2 minutes: it is refused, never a check.
        session = half_session(app_engine, migrate_engine, e.user_id)
        s = totp.new_secret()
        assert begin(app_engine, session, ticket_proof(ticket), e.user_id, s) == "begun"
        codes = code_proofs([totp.new_code() for _ in range(10)])
        assert confirm(app_engine, session, totp.proof(s), codes) == ("enrolled", 0)
        assert check(app_engine, session, before, wrong(), None) == ("none", 0)
        return
    user_id = new_admin(migrate_engine)
    ticket = issue_ticket(user_id)
    session = half_session(app_engine, migrate_engine, user_id)
    s = totp.new_secret()
    if reset == "begin":
        plant_charge(migrate_engine, user_id)
        assert begin(app_engine, session, ticket_proof(ticket), user_id, s) == "begun"
    else:
        assert begin(app_engine, session, ticket_proof(ticket), user_id, s) == "begun"
        plant_charge(migrate_engine, user_id)
        codes = code_proofs([totp.new_code() for _ in range(10)])
        if reset == "confirm":
            assert confirm(app_engine, session, totp.proof(s), codes) == ("enrolled", 0)
        else:
            row = call(app_engine, CONFIRM, id=session, p=totp.proof(s), codes=codes)
            assert tuple(row) == ("enrolled", 0)
    assert charges_of(migrate_engine, user_id) == []


def pending(app_engine: Engine, migrate_engine: Engine) -> tuple[uuid.UUID, bytes, bytes]:
    """An admin with an enrolment begun and not confirmed: (user, session, secret)."""
    user_id = new_admin(migrate_engine)
    ticket = issue_ticket(user_id)
    session = half_session(app_engine, migrate_engine, user_id)
    s = totp.new_secret()
    assert begin(app_engine, session, ticket_proof(ticket), user_id, s) == "begun"
    return user_id, session, s


TEN = [secrets.token_bytes(32) for _ in range(10)]


# fence (ZIF-261, the attack at enrolment): a confirm code is counted in a transaction of its own
# before it is checked, so a check cut off before its commit still leaves it counted, and ten of
# them lock the enrolment. Kills the count taken in check_totp_enrolment. Injected by moving the
# `+ 1` from charge_totp_enrolment into check_totp_enrolment's failure branch.
@pytest.mark.parametrize("cut", ["rollback", "terminate"])
def test_a_confirm_cut_off_before_its_commit_still_counts(
    app_engine: Engine, migrate_engine: Engine, cut: str
) -> None:
    user_id, session, s = pending(app_engine, migrate_engine)
    for n in range(1, 11):
        outcome, failures, charged = call(app_engine, CHARGE_ENROLMENT, id=session)
        assert (outcome, failures) == ("charged", n)
        conn = app_engine.connect()
        try:
            conn.begin()
            pid = conn.scalar(text("SELECT pg_backend_pid()"))
            row = conn.execute(
                CHECK_ENROLMENT, {"id": session, "charge": charged, "p": wrong(), "codes": TEN}
            ).one()
            assert tuple(row) == ("failed", n)
            if cut == "rollback":
                conn.rollback()
            else:
                with app_engine.connect() as killer:
                    assert killer.scalar(
                        text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid}
                    )
                conn.invalidate()
        finally:
            conn.close()
        assert factor(migrate_engine, user_id)["failed_attempts"] == n

    assert confirm(app_engine, session, totp.proof(s), TEN) == ("locked", 10)
    assert factor(migrate_engine, user_id)["confirmed_at"] is None


# fence (ZIF-261): an enrolment check needs a live charge of this admin, or it looks at nothing,
# the right code included. Kills a check that confirms without a charge, with another admin's, or
# after it expired. Injected by deleting the charge test from check_totp_enrolment, then its
# coalesce, then its age test.
@pytest.mark.parametrize("which", ["none", "made_up", "expired", "other_admin"])
def test_a_confirm_needs_its_own_live_charge(
    app_engine: Engine, migrate_engine: Engine, which: str
) -> None:
    user_id, session, s = pending(app_engine, migrate_engine)
    call(app_engine, CHARGE_ENROLMENT, id=session)  # another attempt in flight
    charged: uuid.UUID | None = None
    if which == "made_up":
        charged = uuid.uuid4()
    elif which == "expired":
        with migrate_engine.begin() as conn:
            charged = conn.scalar(
                text(
                    "UPDATE second_factors SET charges = charges || uuidv7(- interval '121 s') "
                    "WHERE user_id = :u RETURNING charges[2]"
                ),
                {"u": user_id},
            )
    elif which == "other_admin":
        _, other, _ = pending(app_engine, migrate_engine)
        charged = call(app_engine, CHARGE_ENROLMENT, id=other)[2]

    row = call(app_engine, CHECK_ENROLMENT, id=session, charge=charged, p=totp.proof(s), codes=TEN)

    assert tuple(row) == ("none", 0)
    assert factor(migrate_engine, user_id)["confirmed_at"] is None
    assert factor(migrate_engine, user_id)["failed_attempts"] == 1
    assert confirm(app_engine, session, totp.proof(s), TEN) == ("enrolled", 0)  # the control


# fence (ZIF-261): at enrolment too the lock is decided at the charge, and the attempt that made it
# 10 is still checked. Kills a charge past the lock. Injected by deleting the `>= 10` test in
# charge_totp_enrolment.
def test_the_enrolment_lock_is_decided_at_the_charge(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    user_id, session, s = pending(app_engine, migrate_engine)
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE second_factors SET failed_attempts = 9 WHERE user_id = :u"), {"u": user_id}
        )
    _, failures, tenth = call(app_engine, CHARGE_ENROLMENT, id=session)
    assert failures == 10

    assert tuple(call(app_engine, CHARGE_ENROLMENT, id=session)) == ("locked", 10, None)
    row = call(app_engine, CHECK_ENROLMENT, id=session, charge=tenth, p=totp.proof(s), codes=TEN)
    assert tuple(row) == ("enrolled", 0)
