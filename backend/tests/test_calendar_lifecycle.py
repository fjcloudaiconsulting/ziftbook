"""Local calendar transitions against the real app role, with synthetic credentials only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, event, text

from app import bookings, calendar_crypto, calendar_lifecycle
from app.auth import SignedIn
from app.db import tenant_context
from app.errors import ApiError
from app.main import create_app
from tests.conftest import People, member_id, signed_in
from tests.test_bookings_db import book, insert_booking, seed_service
from tests.test_calendar_db import calendar_people as calendar_people
from tests.test_calendar_db import google_block, insert_attempt, insert_connection
from tests.test_clients_db import seed_client


@pytest.mark.parametrize("state", ["initializing", "fresh", "stale", "reconnect_required"])
def test_disconnect_fences_active_states_and_clears_only_its_google_blocks(
    calendar_people: People, state: str
) -> None:
    people = calendar_people
    worker, owner = member_id(people.a, people.only_a), member_id(people.a, people.both)
    with tenant_context(people.a) as db:
        connection = insert_connection(db, people.a, worker, people.only_a)
        other = insert_connection(db, people.a, owner, people.both)
        google_block(db, people.a, worker, connection)
        google_block(db, people.a, owner, other)
        db.execute(
            text("""
            INSERT INTO time_off (tenant_id, member_id, first_day, last_day)
            VALUES (:t, :m, CURRENT_DATE, CURRENT_DATE)
            """),
            {"t": people.a, "m": worker},
        )
        pending = insert_attempt(db, people.a, worker, people.only_a)
        db.execute(
            text("""
            UPDATE google_calendar_oauth_attempts SET state = 'exchanging', consumed_at = now(),
                connection_id = :connection, connection_generation = 3, state_hash = :hash
            WHERE id = :attempt
            """),
            {"connection": connection, "attempt": pending, "hash": b"x" * 32},
        )
        insert_attempt(db, people.a, worker, people.only_a)
        db.execute(
            text("""
            UPDATE google_calendar_connections SET state = :state, generation = 3,
                token_revision = 7, lease_id = uuidv7(), lease_sequence = 4,
                lease_until = now() + interval '1 minute',
                scheduling_state = 'parked', park_reason = 'configuration', recheck_at = now(),
                coverage_start = now(), coverage_end = now() + interval '1 day',
                last_success_at = now()
            WHERE id = :id
            """),
            {"state": state, "id": connection},
        )

    with tenant_context(people.a) as db:
        assert calendar_lifecycle.disconnect(
            SignedIn(people.both, people.a, "owner", db), worker
        ).state == ("revocation_pending")
        row = db.execute(
            text("""
            SELECT state, generation, lease_sequence, token_revision, encryption_generation,
                   next_revoke_at = now() AS due, disconnected_at = now() AS disconnected,
                   (lease_id, lease_until, next_sync_at, coverage_start, coverage_end,
                    recheck_at, park_reason, last_success_at) IS NULL AS cleared, scheduling_state
            FROM google_calendar_connections WHERE id = :id
            """),
            {"id": connection},
        ).one()
        assert tuple(row) == ("revocation_pending", 4, 5, 7, 1, True, True, True, "runnable")
        assert db.execute(text("SELECT id FROM google_calendar_oauth_attempts")).all() == []
        assert set(db.execute(text("SELECT source, calendar_connection_id FROM time_off"))) == {
            ("manual", None),
            ("google", other),
        }

    with tenant_context(people.a) as db:
        assert (
            db.execute(
                text("""
            UPDATE google_calendar_connections SET last_success_at = now()
            WHERE id = :id AND generation = 3 AND lease_sequence = 4 RETURNING id
            """),
                {"id": connection},
            ).all()
            == []
        )


@pytest.mark.parametrize("revoked", [False, True])
def test_disconnect_without_live_credential_invalidates_attempts(
    calendar_people: People, revoked: bool
) -> None:
    people = calendar_people
    worker = member_id(people.a, people.only_a)
    with tenant_context(people.a) as db:
        if revoked:
            insert_connection(db, people.a, worker, people.only_a)
            db.execute(
                text("""
                UPDATE google_calendar_connections SET state = 'revoked', disconnected_at = now(),
                    revoked_at = now(), key_id = NULL, wrapped_dek = NULL, wrap_nonce = NULL,
                    payload_nonce = NULL, ciphertext = NULL
                """),
            )
        insert_attempt(db, people.a, worker, people.only_a)
        assert calendar_lifecycle.disconnect(
            SignedIn(people.only_a, people.a, "worker", db), worker
        ).state == ("disconnected")
        calendar_lifecycle.require_reconnect_allowed(db, people.a, worker)
        assert db.execute(text("SELECT id FROM google_calendar_oauth_attempts")).all() == []
        assert db.execute(
            text("SELECT state, generation FROM google_calendar_connections")
        ).all() == ([("revoked", 1)] if revoked else [])


def test_pending_disconnect_is_idempotent_and_refuses_reconnect(calendar_people: People) -> None:
    people = calendar_people
    worker = member_id(people.a, people.only_a)
    with tenant_context(people.a) as db:
        insert_connection(db, people.a, worker, people.only_a)
        calendar_lifecycle.disconnect(SignedIn(people.only_a, people.a, "worker", db), worker)
        before = db.execute(
            text("""
            UPDATE google_calendar_connections SET next_revoke_at = now() + interval '10 minutes'
            RETURNING disconnected_at, next_revoke_at, generation, lease_sequence
            """),
        ).one()
    with tenant_context(people.a) as db:
        assert calendar_lifecycle.disconnect(
            SignedIn(people.only_a, people.a, "worker", db), worker
        ).state == ("revocation_pending")
        assert (
            db.execute(
                text("""
                SELECT disconnected_at, next_revoke_at, generation, lease_sequence
                FROM google_calendar_connections
                """)
            ).one()
            == before
        )
        with pytest.raises(ApiError) as refused:
            calendar_lifecycle.require_reconnect_allowed(db, people.a, worker)
        assert (refused.value.status_code, refused.value.code) == (
            409,
            "calendar_revocation_pending",
        )


def test_disconnect_retains_envelope_without_keys_and_keeps_private_values_out_of_outputs(
    calendar_people: People, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    people = calendar_people
    worker = member_id(people.a, people.only_a)
    values = {
        "refresh_token": SecretStr("sentinel-calendar-refresh"),
        "subject": SecretStr("sentinel-calendar-subject"),
        "email": SecretStr("sentinel-calendar-label@example.test"),
    }
    keys = calendar_crypto.Keyring("test-key", {"test-key": b"k" * 32})
    with tenant_context(people.a) as db:
        connection = insert_connection(db, people.a, worker, people.only_a)
        binding = calendar_crypto.Binding("calendar-token", people.a, connection, worker, 1)
        monkeypatch.setattr(calendar_crypto, "keyring", lambda: keys)
        envelope = calendar_crypto.encrypt(values, binding)
        db.execute(
            text("""
            UPDATE google_calendar_connections
            SET wrapped_dek = :dek, wrap_nonce = :wrap,
                payload_nonce = :nonce, ciphertext = :payload
            WHERE id = :id
            """),
            {
                "dek": envelope.wrapped_dek,
                "wrap": envelope.wrap_nonce,
                "nonce": envelope.payload_nonce,
                "payload": envelope.ciphertext,
                "id": connection,
            },
        )
    monkeypatch.setattr(calendar_crypto, "keyring", lambda: None)
    with caplog.at_level("DEBUG"), tenant_context(people.a) as db:
        result = calendar_lifecycle.disconnect(
            SignedIn(people.only_a, people.a, "worker", db), worker
        )
        row = db.execute(
            text("""
            SELECT key_id, wrapped_dek, wrap_nonce, payload_nonce, ciphertext
            FROM google_calendar_connections WHERE id = :id
            """),
            {"id": connection},
        ).one()
        kept = calendar_crypto.Envelope(*row)
        with pytest.raises(ApiError) as refused:
            calendar_lifecycle.require_reconnect_allowed(db, people.a, worker)
    assert kept == envelope
    monkeypatch.setattr(calendar_crypto, "keyring", lambda: keys)
    assert calendar_crypto.decrypt(kept, binding) == values
    for value in (*values.values(), SecretStr("k" * 32)):
        assert value.get_secret_value() not in (caplog.text + repr(result) + repr(refused.value))


def test_disconnect_checks_current_role_and_cannot_lock_another_tenants_link(
    calendar_people: People,
) -> None:
    people = calendar_people
    owner, foreign = member_id(people.a, people.both), member_id(people.b, people.only_b)
    with tenant_context(people.b) as db:
        connection = insert_connection(db, people.b, foreign, people.only_b)
        insert_attempt(db, people.b, foreign, people.only_b)
    with tenant_context(people.a) as db:
        with pytest.raises(ApiError) as unrelated:
            calendar_lifecycle.disconnect(SignedIn(people.both, people.a, "owner", db), foreign)
        assert (unrelated.value.status_code, unrelated.value.code) == (404, "not_found")
        with pytest.raises(ApiError) as stale_owner:
            calendar_lifecycle.disconnect(SignedIn(people.only_a, people.a, "owner", db), owner)
        assert (stale_owner.value.status_code, stale_owner.value.code) == (403, "owner_only")
        assert db.execute(text("SELECT id FROM google_calendar_connections FOR UPDATE")).all() == []
    with tenant_context(people.b) as db:
        assert db.execute(text("SELECT id, state FROM google_calendar_connections")).one() == (
            connection,
            "initializing",
        )
        assert db.scalar(text("SELECT count(*) FROM google_calendar_oauth_attempts")) == 1


@pytest.mark.parametrize("connected, blocked", [(False, False), (True, False), (True, True)])
def test_member_removal_tombstones_after_upcoming_booking_check(
    calendar_people: People, connected: bool, blocked: bool
) -> None:
    people = calendar_people
    worker = member_id(people.a, people.only_a)
    owner = signed_in(create_app(), people.a, people.both)
    with tenant_context(people.a) as db:
        insert_attempt(db, people.a, worker, people.only_a)
        if connected:
            connection = insert_connection(db, people.a, worker, people.only_a)
            google_block(db, people.a, worker, connection)
    if blocked:
        starts = datetime.now(UTC) + timedelta(days=1)
        book(
            people.a,
            client_id=seed_client(people.a),
            worker_id=worker,
            service_id=seed_service(people.a),
            starts_at=starts,
            ends_at=starts + timedelta(minutes=30),
        )

    response = owner.request("DELETE", f"/api/members/{worker}", json={})
    assert response.status_code == (409 if blocked else 204)
    if blocked:
        assert response.json() == {"code": "has_upcoming_bookings"}
    with tenant_context(people.a) as db:
        assert db.scalar(
            text("SELECT role FROM memberships WHERE id = :member"), {"member": worker}
        ) == ("worker" if blocked else "removed")
        if connected:
            row = db.execute(
                text("SELECT state, generation, ciphertext FROM google_calendar_connections")
            ).one()
            assert tuple(row) == (
                ("initializing", 1, b"c" * 16) if blocked else ("revocation_pending", 2, b"c" * 16)
            )
        assert db.scalar(text("SELECT count(*) FROM google_calendar_oauth_attempts")) == int(
            blocked
        )
        assert db.scalar(text("SELECT count(*) FROM time_off WHERE source = 'google'")) == int(
            blocked
        )


def test_disconnect_takes_booking_membership_and_connection_locks_in_order(
    calendar_people: People, app_engine: Engine
) -> None:
    people = calendar_people
    worker = member_id(people.a, people.only_a)
    client, service = seed_client(people.a), seed_service(people.a)
    with tenant_context(people.a) as db:
        connection = insert_connection(db, people.a, worker, people.only_a)

    entering_booking_lock = Event()
    order: list[str] = []

    def inspect_statement(conn: Connection, cursor: object, statement: str, *args: object) -> None:
        if not conn.info.get("calendar_lifecycle_test"):
            return
        if "pg_advisory_xact_lock" in statement:
            order.append("bookings")
            entering_booking_lock.set()
        elif "FOR UPDATE" in statement and "FROM memberships" in statement:
            order.append("membership")
        elif "FOR UPDATE" in statement and "FROM google_calendar_connections" in statement:
            order.append("connection")

    def disconnect() -> calendar_lifecycle.DisconnectResult:
        with tenant_context(people.a) as db:
            db.connection().info["calendar_lifecycle_test"] = True
            return calendar_lifecycle.disconnect(
                SignedIn(people.only_a, people.a, "worker", db), worker
            )

    event.listen(app_engine, "before_cursor_execute", inspect_statement)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with tenant_context(people.a) as booking_db:
                booking_db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
                disconnected = executor.submit(disconnect)
                assert entering_booking_lock.wait(5), "disconnect did not reach the booking lock"
                assert order == ["bookings"]
                for table, row in (
                    ("memberships", worker),
                    ("google_calendar_connections", connection),
                ):
                    assert (
                        booking_db.scalar(
                            text(f"SELECT id FROM {table} WHERE id = :id FOR UPDATE NOWAIT"),
                            {"id": row},
                        )
                        == row
                    )
                starts = datetime.now(UTC) + timedelta(days=1)
                booking = insert_booking(
                    booking_db,
                    client_id=client,
                    worker_id=worker,
                    service_id=service,
                    starts_at=starts,
                    ends_at=starts + timedelta(minutes=30),
                )
            assert disconnected.result(timeout=10).state == "revocation_pending"
    finally:
        event.remove(app_engine, "before_cursor_execute", inspect_statement)
    assert order == ["bookings", "membership", "connection"]
    with tenant_context(people.a) as db:
        assert (
            db.scalar(text("SELECT status FROM bookings WHERE id = :id"), {"id": booking})
            == "confirmed"
        )
