"""ZIF-70: platform_admins, platform_audit_events and the grant/revoke CLI (python -m app.admin)."""

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app import admin, totp
from tests.conftest import REAL_SHOW_TICKET, SHOWN_TICKETS, add_user, email_of
from tests.test_second_factor_db import (
    TICKET,
    begin,
    enrolled,
    factor,
    half_session,
    issue_ticket,
    new_admin,
    pass_,
    session_factor,
    ticket_proof,
)


def sqlstate(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> str | None:
    """Run one statement in its own transaction: None when it worked, else its SQLSTATE."""
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params or {})
        return None
    except DBAPIError as error:
        return str(getattr(error.orig, "sqlstate", error))


def audit_rows(migrate_engine: Engine, target: str) -> list[dict[str, Any]]:
    with migrate_engine.connect() as conn:
        return [
            dict(row._mapping)
            for row in conn.execute(
                text(
                    "SELECT actor_user_id, action, kind, tenant_id, target, reason, details "
                    "FROM platform_audit_events WHERE target = :t ORDER BY id"
                ),
                {"t": target},
            )
        ]


def is_admin(migrate_engine: Engine, user_id: uuid.UUID) -> bool:
    with migrate_engine.connect() as conn:
        return bool(
            conn.scalar(
                text("SELECT count(*) FROM platform_admins WHERE user_id = :u"), {"u": user_id}
            )
        )


# fence: the app role must never make (or see) an admin. Kills 0001's default grants left in place.
def test_app_role_has_no_access_to_platform_admins(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    user_id = add_user(migrate_engine)
    params = {"u": user_id}
    for sql in (
        "SELECT * FROM platform_admins",
        "INSERT INTO platform_admins (user_id) VALUES (:u)",
        "UPDATE platform_admins SET granted_at = now()",
        "DELETE FROM platform_admins",
    ):
        assert sqlstate(app_engine, sql, params) == "42501", sql


GRANTED = (
    "INSERT INTO platform_audit_events "
    "(actor_user_id, action, kind, tenant_id, target, reason, details, ip, user_agent) "
    "VALUES (:u, 'business_read', 'read', :t, 'service:1', NULL, '{}', '192.0.2.1', 'ua')"
)


# fence: the app appends, and does nothing else. Kills a table-wide grant (backdating with
# created_at, choosing an id, reading, tampering).
def test_app_role_can_only_append_platform_audit(app_engine: Engine) -> None:
    params = {"u": uuid.uuid4(), "t": uuid.uuid4()}
    assert sqlstate(app_engine, GRANTED, params) is None
    for sql in (
        "INSERT INTO platform_audit_events (id, action, kind) "
        "VALUES (uuidv7(), 'business_read', 'read')",
        "INSERT INTO platform_audit_events (created_at, action, kind) "
        "VALUES (now() - interval '1 day', 'business_read', 'read')",
        "SELECT * FROM platform_audit_events",
        "UPDATE platform_audit_events SET reason = 'x'",
        "DELETE FROM platform_audit_events",
    ):
        assert sqlstate(app_engine, sql) == "42501", sql


# fence: a write without a reason is refused by the database, whoever writes it. Kills a missing or
# weaker CHECK; btrim() strips only spaces, so it would let a tab/newline-only reason through.
@pytest.mark.parametrize("reason", [None, "", "   ", "\t\n"])
def test_write_without_reason_is_refused(app_engine: Engine, reason: str | None) -> None:
    insert = (
        "INSERT INTO platform_audit_events (action, kind, reason) "
        "VALUES ('admin_granted', :kind, :r)"
    )
    assert sqlstate(app_engine, insert, {"kind": "write", "r": reason}) == "23514"
    assert sqlstate(app_engine, insert, {"kind": "read", "r": reason}) is None


# guard: grant and revoke, each with exactly one audit event; a repeated grant changes nothing.
def test_grant_and_revoke(
    migrate_engine: Engine,
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    user_id = add_user(migrate_engine)
    email = email_of(user_id)
    target = f"user:{user_id}"

    # Upper case and spaces: the CLI normalises the email as sign-in does.
    assert admin.main(["grant", f"  {email.upper()} ", "--reason", "on-call operator"]) == 0
    assert is_admin(migrate_engine, user_id)
    assert admin.main(["grant", email, "--reason", "again"]) == 0
    assert admin.main(["revoke", email, "--reason", "left the team"]) == 0
    assert not is_admin(migrate_engine, user_id)
    assert admin.main(["revoke", email, "--reason", "again"]) == 0

    cli = {"actor_user_id": None, "kind": "write", "tenant_id": None, "target": target}
    assert audit_rows(migrate_engine, target) == [
        {**cli, "action": "admin_granted", "reason": "on-call operator", "details": {"via": "cli"}},
        {**cli, "action": "admin_revoked", "reason": "left the team", "details": {"via": "cli"}},
    ]
    assert email not in json.dumps(log_lines())


# guard: an unknown email changes nothing and says so with exit code 1.
@pytest.mark.parametrize("command", ["grant", "revoke"])
def test_unknown_email_exits_1(
    migrate_engine: Engine, command: str, log_lines: Callable[[], list[dict[str, Any]]]
) -> None:
    email = f"{uuid.uuid4()}@example.com"
    assert admin.main([command, email, "--reason", "typo"]) == 1
    assert email not in json.dumps(log_lines())
    with migrate_engine.connect() as conn:
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM platform_audit_events "
                    "WHERE created_at > now() - interval '1 minute' AND reason = 'typo'"
                )
            )
            == 0
        )


# fence: no reason, no change. Kills an optional --reason, which would fail mid-transaction on the
# CHECK with a traceback instead of a usage error.
@pytest.mark.parametrize(
    "reason", [[], ["--reason", ""], ["--reason", " \t"], ["--reason", "x" * 501]]
)
def test_grant_needs_a_reason(migrate_engine: Engine, reason: list[str]) -> None:
    user_id = add_user(migrate_engine)
    with pytest.raises(SystemExit) as exited:
        admin.main(["grant", email_of(user_id), *reason])
    assert exited.value.code == 2
    assert not is_admin(migrate_engine, user_id)
    assert audit_rows(migrate_engine, f"user:{user_id}") == []


# fence: the change and its audit event commit together or not at all. Kills an audit row written
# in its own transaction after the grant commits (an admin with no record of why).
def test_grant_and_its_event_are_one_transaction(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = add_user(migrate_engine)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr(admin, "record", broken)
    with pytest.raises(RuntimeError):
        admin.main(["grant", email_of(user_id), "--reason", "on-call"])
    assert not is_admin(migrate_engine, user_id)


# guard: the table's other CHECKs, whoever inserts.
@pytest.mark.parametrize(
    ("kind", "reason", "user_agent"),
    [("other", None, None), ("read", "x" * 501, None), ("read", None, "x" * 513)],
)
def test_platform_audit_checks(
    app_engine: Engine, kind: str, reason: str | None, user_agent: str | None
) -> None:
    insert = (
        "INSERT INTO platform_audit_events (action, kind, reason, user_agent) "
        "VALUES ('business_read', :k, :r, :ua)"
    )
    assert sqlstate(app_engine, insert, {"k": kind, "r": reason, "ua": user_agent}) == "23514"


# guard: every action has a kind, or record() raises KeyError on its first use.
def test_every_action_has_a_kind() -> None:
    assert set(admin.KIND) == set(get_args(admin.Action))


# guard: deleting a user ends their admin grant (ON DELETE CASCADE).
def test_deleting_a_user_ends_the_grant(migrate_engine: Engine) -> None:
    user_id = add_user(migrate_engine)
    assert admin.main(["grant", email_of(user_id), "--reason", "on-call"]) == 0
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
    assert not is_admin(migrate_engine, user_id)


ISSUED = text(
    "SELECT ticket_hash, ticket_expires_at - now() AS lasts, recovery_code_hashes, failed_attempts "
    "FROM second_factors WHERE user_id = :u"
)


# fence: the ticket goes to the operator's terminal and never to a log or stdout (both ship to
# Loki in a container). Kills the ticket printed or logged. Injected by print(ticket) or by adding
# it to the INFO line's extra.
def test_enrol_shows_the_ticket_on_the_terminal_only(
    migrate_engine: Engine,
    capsys: pytest.CaptureFixture[str],
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    user_id = new_admin(migrate_engine)

    assert admin.main(["enrol", email_of(user_id), "--reason", "new phone"]) == 0

    (ticket,) = SHOWN_TICKETS
    assert TICKET.fullmatch(ticket)
    captured = capsys.readouterr()
    assert ticket not in captured.out + captured.err
    lines = log_lines()
    assert [
        line["user_id"] for line in lines if line["msg"] == "second factor enrolment issued"
    ] == [str(user_id)]
    logged = json.dumps(lines)
    assert ticket not in logged and totp.normalise(ticket) not in logged


# fence: with no terminal enrol issues nothing: exit 2, the old factor stays, no audit event, no
# "issued" log line. Kills a commit before the ticket is shown.
def test_enrol_without_a_terminal_issues_nothing(
    app_engine: Engine,
    migrate_engine: Engine,
    capsys: pytest.CaptureFixture[str],
    log_lines: Callable[[], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    e = enrolled(app_engine, migrate_engine)
    monkeypatch.setattr(admin, "show_ticket", REAL_SHOW_TICKET)  # pointed at no terminal
    monkeypatch.setattr(admin, "TTY", str(tmp_path / "no" / "tty"))
    before = factor(migrate_engine, e.user_id)
    audited = audit_rows(migrate_engine, f"user:{e.user_id}")
    issued = len([line for line in log_lines() if line["msg"] == "second factor enrolment issued"])

    assert admin.main(["enrol", email_of(e.user_id), "--reason", "new phone"]) == 2

    assert capsys.readouterr().err.strip() == "enrolment needs a terminal: run it with -it"
    assert factor(migrate_engine, e.user_id) == before
    assert session_factor(migrate_engine, e.session) != "gone"
    assert audit_rows(migrate_engine, f"user:{e.user_id}") == audited
    assert issued == len(
        [line for line in log_lines() if line["msg"] == "second factor enrolment issued"]
    )


# fence: the real show_ticket puts the ticket on the terminal and nowhere else. Kills a print to
# stdout or stderr (a container's log stream).
def test_show_ticket_writes_the_terminal_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tty = tmp_path / "tty"
    tty.touch()
    monkeypatch.setattr(admin, "TTY", str(tty))

    REAL_SHOW_TICKET("the-ticket")

    assert "the-ticket" in tty.read_text()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# fence: a ticket lasts one hour. Kills a 24-hour or missing expiry. Injected by '24 hours'.
def test_enrol_ticket_lasts_one_hour(migrate_engine: Engine) -> None:
    user_id = new_admin(migrate_engine)
    ticket = issue_ticket(user_id)
    with migrate_engine.connect() as conn:
        row = conn.execute(ISSUED, {"u": user_id}).one()
    assert row.ticket_hash == hashlib.sha256(ticket_proof(ticket)).digest()
    assert timedelta(minutes=59) < row.lasts <= timedelta(minutes=60)


# fence: enrol starts over: the old factor, its codes and its failures are gone, and so are the
# admin's sessions. Kills a re-enrol that keeps old recovery codes, or a lock that survives.
# Injected by deleting `recovery_code_hashes = '{}'` from the upsert.
def test_enrol_wipes_the_factor_and_ends_sessions(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    e = enrolled(app_engine, migrate_engine)
    pass_(app_engine, e.session, totp.proof(e.secret), None)  # one failure to wipe
    assert factor(migrate_engine, e.user_id)["failed_attempts"] == 1

    ticket = issue_ticket(e.user_id)

    assert session_factor(migrate_engine, e.session) == "gone"
    with migrate_engine.connect() as conn:
        row = conn.execute(ISSUED, {"u": e.user_id}).one()
        assert (
            conn.scalar(
                text("SELECT count(*) FROM admin_sessions WHERE user_id = :u"), {"u": e.user_id}
            )
            == 0
        )
    assert (row.recovery_code_hashes, row.failed_attempts) == ([], 0)
    session = half_session(app_engine, migrate_engine, e.user_id)
    step = factor(migrate_engine, e.user_id)["last_step"] + 1
    assert pass_(app_engine, session, totp.proof(e.secret), step) == ("none", 0)
    s = totp.new_secret()
    assert begin(app_engine, session, ticket_proof(ticket), e.user_id, s) == "begun"


# guard: one write event with the reason, from the CLI, about that user.
def test_enrol_is_audited(migrate_engine: Engine) -> None:
    user_id = new_admin(migrate_engine)
    assert admin.main(["enrol", email_of(user_id), "--reason", "lost phone"]) == 0
    target = f"user:{user_id}"
    assert audit_rows(migrate_engine, target) == [
        {
            "actor_user_id": None,
            "action": "totp_enrolment_issued",
            "kind": "write",
            "tenant_id": None,
            "target": target,
            "reason": "lost phone",
            "details": {"via": "cli"},
        }
    ]


# guard: enrol only for an admin; nothing written otherwise. --reason is required.
def test_enrol_refuses_a_non_admin(migrate_engine: Engine) -> None:
    user_id = add_user(migrate_engine)
    assert admin.main(["enrol", f"{uuid.uuid4()}@example.com", "--reason", "typo"]) == 1
    assert admin.main(["enrol", email_of(user_id), "--reason", "not an admin"]) == 1
    with pytest.raises(SystemExit) as exited:
        admin.main(["enrol", email_of(new_admin(migrate_engine))])
    assert exited.value.code == 2
    assert factor(migrate_engine, user_id) == {}
    assert audit_rows(migrate_engine, f"user:{user_id}") == []
