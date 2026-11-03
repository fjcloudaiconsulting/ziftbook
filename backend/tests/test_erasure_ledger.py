"""The erasure ledger (ZIF-58, spec section 8): erasures recorded outside the database, on moto
(never a real bucket), and re-applied after a restore."""

import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from mypy_boto3_s3 import S3Client
from sqlalchemy import URL, Engine, create_engine, make_url, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import bookings, ledger, payments, storage
from app.db import SessionLocal, join_tenant
from app.jobs import Job
from app.main import create_app
from tests.conftest import (
    ADMIN_DATABASE_URL,
    S3_ENDPOINT,
    People,
    fresh_email,
    member_id,
    migrate_tenant_context,
    save_setting,
    signed_in,
)
from tests.stripe_fake import FakeGateway
from tests.test_booking_payments_api import make_shop, run
from tests.test_booking_refunds import booked
from tests.test_bookings_db import seed_service
from tests.test_client_erasure_db import INSERT_BOOKING, INSERT_CLIENT, take
from tests.test_erased_client_reads import client_of

BUCKET = "ziftbook-erasures"


@pytest.fixture
def ledger_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Client]:
    """The ledger bucket on moto, configured through its ZIF_* variables; yields an admin client."""
    for name, value in {
        "ZIF_S3_ENDPOINT": S3_ENDPOINT,
        "ZIF_ERASURE_LEDGER_BUCKET": BUCKET,
        "ZIF_ERASURE_LEDGER_ACCESS_KEY_ID": "dev",
        "ZIF_ERASURE_LEDGER_SECRET_ACCESS_KEY": "dev-secret",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        admin = boto3.client("s3", endpoint_url=S3_ENDPOINT, region_name="us-east-1")
        admin.create_bucket(Bucket=BUCKET)
        yield admin


def ledger_job(payload: dict[str, Any]) -> Job:
    return Job(uuid.uuid4(), "erasure.ledger", uuid.UUID(payload["tenant_id"]), payload)


def a_payload(erased_at: datetime | None = None) -> dict[str, Any]:
    return {
        "kind": "client",
        "tenant_id": str(uuid.uuid4()),
        "client_id": str(uuid.uuid4()),
        "erased_at": (erased_at or datetime.now(UTC)).isoformat(),
    }


def stored(admin: S3Client) -> dict[str, bytes]:
    listed = admin.list_objects_v2(Bucket=BUCKET).get("Contents", [])
    return {o["Key"]: admin.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read() for o in listed}


def refusing(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    """Every ledger PUT is refused with this error code, as a bucket lock refuses an overwrite."""
    real = storage._connect

    def connect(*args: Any) -> Any:
        s3 = real(*args)

        def put_object(**kwargs: Any) -> None:
            raise ClientError({"Error": {"Code": code}}, "PutObject")

        setattr(s3, "put_object", put_object)  # noqa: B010 -- mypy refuses a method assignment
        return s3

    monkeypatch.setattr(storage, "_connect", connect)


# ZIF-58 F23. FENCE. Wrong impl: a PUT that fails on an existing key (a repeat of the job, or the
# bucket lock refusing the overwrite, would fail the job and retry it into a give-up).
def test_the_entry_is_written_at_its_key_and_a_repeat_is_success(
    ledger_bucket: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = datetime(2026, 11, 3, 9, 15, 7, 123456, tzinfo=UTC)
    payload = a_payload(at)
    ledger.record(ledger_job(payload))
    key = f"erasures/2026/11/03/20261103T091507.123456Z-client-{payload['client_id']}.json"
    assert list(stored(ledger_bucket)) == [key]
    first = stored(ledger_bucket)[key]
    assert json.loads(first) == {"v": 1, **payload}

    ledger.record(ledger_job(payload))  # the job run again: already there
    # A writer that saw nothing at HEAD (a racing run), refused by the conditional PUT.
    real = storage._present
    seen: list[str] = []

    def missed_once(s3: S3Client, bucket: str, name: str) -> bool:
        seen.append(name)
        return len(seen) > 1 and real(s3, bucket, name)

    monkeypatch.setattr(storage, "_present", missed_once)
    ledger.record(ledger_job(payload))
    # And the bucket lock refusing the overwrite.
    monkeypatch.setattr(storage, "_present", missed_once)
    seen.clear()
    refusing(monkeypatch, "AccessDenied")
    ledger.record(ledger_job(payload))
    assert stored(ledger_bucket) == {key: first}


# ZIF-58 F23. FENCE. Wrong impl: a refused PUT taken as written whatever it left behind (the
# erasure would be missing from the ledger with its job done).
def test_a_refused_put_of_a_missing_entry_fails_the_job(
    ledger_bucket: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusing(monkeypatch, "AccessDenied")
    with pytest.raises(storage.StorageError):
        ledger.record(ledger_job(a_payload()))
    assert stored(ledger_bucket) == {}


# ZIF-58 F24. FENCE. Wrong impl: an entry built from the job's payload as it is (a payload that
# carries an address, a name or the trace context would put it in the ledger, which nothing ever
# rewrites).
def test_the_entry_holds_ids_and_the_time_only(ledger_bucket: S3Client) -> None:
    payload = a_payload()
    ledger.record(
        ledger_job(
            {
                **payload,
                "email": "erased-person@example.com",
                "name": "Erased Person",
                "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            }
        )
    )
    [body] = stored(ledger_bucket).values()
    assert "@" not in body.decode()
    assert json.loads(body) == {"v": 1, **payload}


# ZIF-58 F24. GUARD. A payload that is not a client erasure, or whose ids are not ids, writes
# nothing: the job fails (and gives up, an ERROR) rather than ledgering something else.
@pytest.mark.parametrize(
    "change",
    [
        {"kind": "account"},
        {"client_id": "erased-person@example.com"},
        {"erased_at": "2026-11-03T09:15:07"},
    ],
)
def test_anything_but_a_client_erasure_writes_nothing(
    ledger_bucket: S3Client, change: dict[str, str]
) -> None:
    with pytest.raises(ValueError):
        ledger.record(ledger_job({**a_payload(), **change}))
    assert stored(ledger_bucket) == {}


@contextmanager
def backup() -> Iterator[URL]:
    """This worker's database as it is now, copied whole (a template copy) as a backup holds it;
    yields its URL as the migrate role, and drops it after."""
    source = make_url(os.environ["ZIF_MIGRATE_DATABASE_URL"])
    name = f"{source.database}_backup_{uuid.uuid4().hex[:8]}"
    admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        conn.execute(
            text(f"CREATE DATABASE {name} TEMPLATE {source.database} OWNER ziftbook_migrate")
        )
    try:
        yield source.set(database=name)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


def erase_as_an_owner(
    migrate_engine: Engine, tenant_id: uuid.UUID, client_id: uuid.UUID, actor: uuid.UUID
) -> datetime:
    """The erasure the erase route will make (ZIF-58 PR 7), in one transaction: the booking lock,
    the live bookings ended and their money settled, the core, and the ledger job."""
    with Session(migrate_engine) as db, db.begin():
        join_tenant(db, tenant_id)
        db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
        ended = bookings.end_for_erasure(db, client_id, reason="client_erased", actor_user_id=actor)
        payments.settle_ended(db, tenant_id, ended)
        erased_at: datetime = db.scalar(
            text("SELECT erase_client_core(:id, true)"), {"id": client_id}
        )
        ledger.enqueue(db, tenant_id, client_id, erased_at)
    return erased_at


def reapply(since: datetime) -> int:
    return ledger.main(["reapply", "--since", since.isoformat(), "--reason", "restore drill"])


def last_reapply(engine: Engine) -> dict[str, Any]:
    with engine.begin() as conn:
        details: dict[str, Any] = conn.scalar(
            text("""
            SELECT details FROM platform_audit_events
            WHERE action = 'erasure_reapplied' AND kind = 'write' AND reason = 'restore drill'
            ORDER BY created_at DESC LIMIT 1
            """)
        )
    return details


CLIENT_ROWS = text("""
SELECT c.erased_at, c.name, c.email,
       (SELECT count(*) FROM consents WHERE client_id = c.id) AS consents,
       (SELECT count(*) FROM booking_links l JOIN bookings b ON b.id = l.booking_id
        WHERE b.client_id = c.id) AS links,
       (SELECT count(*) FROM booking_events e JOIN bookings b ON b.id = e.booking_id
        WHERE b.client_id = c.id AND e.actor_user_id IS NULL AND e.ip IS NOT NULL) AS addresses
FROM clients c WHERE c.id = :id
""")
BOOKING = text("""
SELECT b.status, b.client_name, b.checkout_hash, p.settled_at, p.refunded_minor,
       (SELECT details ->> 'reason' FROM booking_events
        WHERE booking_id = b.id AND event = b.status ORDER BY id DESC LIMIT 1) AS reason
FROM bookings b LEFT JOIN booking_payments p ON p.booking_id = b.id
WHERE b.id = :id
""")


# ZIF-58 F25. FENCE (restore probe). Wrong impls: a re-apply that skips ending the bookings (the
# core refuses the client: the erasure is not re-applied), or one that refunds without settle (the
# business's client is paid back twice). A backup taken before an owner's erasure is restored
# after it; the ledger, which the restore does not rewind, erases the client again.
def test_a_restored_backup_is_erased_again_and_refunds_nothing_twice(
    people: People,
    fake_stripe: FakeGateway,
    migrate_engine: Engine,
    ledger_bucket: S3Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app()
    owner = signed_in(app, people.a, people.both)
    save_setting(people.a, "published", True)
    service_id = make_shop(people, owner, fake_stripe, price=4000)
    _, booking_id, _ = booked(app, people, fake_stripe, service_id, "pay")
    client_id = client_of(people.a, booking_id)

    with backup() as restored:
        erased_at = erase_as_an_owner(migrate_engine, people.a, client_id, people.both)
        run("payment.settle", "erasure.ledger")
        assert fake_stripe.count("refund") == 1
        assert len(stored(ledger_bucket)) == 1

        # The restore: the backup is the database now, and the erasure is gone from it.
        monkeypatch.setenv(
            "ZIF_MIGRATE_DATABASE_URL", restored.render_as_string(hide_password=False)
        )
        restored_migrate = create_engine(restored, poolclass=NullPool)
        with migrate_tenant_context(restored_migrate, people.a) as conn:
            assert conn.execute(CLIENT_ROWS, {"id": client_id}).one().erased_at is None
        assert reapply(erased_at - timedelta(hours=1)) == 0
        # The worker starts (the runbook's step 4): settle answers the ended booking.
        bind = SessionLocal.kw["bind"]
        restored_app = create_engine(
            make_url(os.environ["ZIF_DATABASE_URL"]).set(database=restored.database),
            poolclass=NullPool,
        )
        SessionLocal.configure(bind=restored_app)
        try:
            run("payment.settle")
        finally:
            SessionLocal.configure(bind=bind)
            restored_app.dispose()

        with migrate_tenant_context(restored_migrate, people.a) as conn:
            client = conn.execute(CLIENT_ROWS, {"id": client_id}).one()
            booking = conn.execute(BOOKING, {"id": booking_id}).one()
        assert client.erased_at is not None
        assert (client.name, client.email) == ("", None)
        assert (client.consents, client.links, client.addresses) == (0, 0, 0)
        assert (booking.status, booking.reason) == ("cancelled_by_merchant", "erasure_reapplied")
        assert (booking.client_name, booking.checkout_hash) == (None, None)
        assert booking.settled_at is not None
        assert booking.refunded_minor == 4000
        assert fake_stripe.count("refund") == 1
        assert last_reapply(restored_migrate) == {
            "via": "cli",
            "entries": 1,
            "reapplied": 1,
            "already_erased": 0,
            "absent": 0,
            "cancelled": 1,
        }
        restored_migrate.dispose()


def seed_ledgered(
    people: People, migrate_engine: Engine, starts_in: timedelta, erased_ago: timedelta
) -> tuple[uuid.UUID, uuid.UUID, datetime]:
    """A client with a confirmed booking, unerased in the database as after a restore, whose
    erasure the ledger lists: (the client, the booking, the erasure time)."""
    service_id = seed_service(people.a)
    now = datetime.now(UTC)
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        client_id = conn.scalar(INSERT_CLIENT, {"email": fresh_email(), "user_id": None})
        booking_id = conn.scalar(
            INSERT_BOOKING,
            {
                "client": client_id,
                "worker": member_id(people.a, people.only_a),
                "service": service_id,
                "starts": now + starts_in,
                "status": "confirmed",
                "expires": None,
                "decline": None,
                "checkout": None,
            },
        )
    entry = ledger.Entry(people.a, client_id, now - erased_ago)
    storage.ledger_write(entry.key, entry.body())
    return client_id, booking_id, entry.erased_at


# ZIF-58 F26. FENCE. Wrong impl: no erased check (the second run re-applies the erasure again and
# says so). The second run finds the client erased and changes nothing.
def test_a_second_reapply_changes_nothing(
    people: People, migrate_engine: Engine, ledger_bucket: S3Client
) -> None:
    client_id, _, erased_at = seed_ledgered(
        people, migrate_engine, timedelta(days=2), timedelta(minutes=5)
    )
    assert reapply(erased_at - timedelta(hours=1)) == 0
    assert last_reapply(migrate_engine)["reapplied"] == 1
    first = take(migrate_engine, people.a)
    with migrate_engine.begin() as conn:
        queued = conn.scalar(text("SELECT count(*) FROM jobs"))

    assert reapply(erased_at - timedelta(hours=1)) == 0
    assert take(migrate_engine, people.a) == first
    with migrate_engine.begin() as conn:
        assert conn.scalar(text("SELECT count(*) FROM jobs")) == queued
    assert last_reapply(migrate_engine) == {
        "via": "cli",
        "entries": 1,
        "reapplied": 0,
        "already_erased": 1,
        "absent": 0,
        "cancelled": 0,
    }


# ZIF-58 F25. FENCE. Wrong impl: ending the live bookings at the time of the re-apply. The first
# erasure cancelled and refunded a booking that has started since; ended now, it would stay a
# confirmed, paid appointment of the tombstone.
def test_a_booking_the_erasure_ended_ends_again_though_it_has_started_since(
    people: People, migrate_engine: Engine, ledger_bucket: S3Client
) -> None:
    client_id, booking_id, erased_at = seed_ledgered(
        people, migrate_engine, -timedelta(minutes=30), timedelta(hours=1)
    )
    assert reapply(erased_at - timedelta(hours=1)) == 0
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        assert conn.execute(CLIENT_ROWS, {"id": client_id}).one().erased_at is not None
        booking = conn.execute(BOOKING, {"id": booking_id}).one()
    assert (booking.status, booking.reason) == ("cancelled_by_merchant", "erasure_reapplied")


# ZIF-58. GUARD. Entries before --since are not the restore's, and a client the restored database
# never had is counted and left alone.
def test_reapply_takes_the_entries_from_since_on(
    people: People, migrate_engine: Engine, ledger_bucket: S3Client
) -> None:
    early, _, early_at = seed_ledgered(people, migrate_engine, timedelta(days=2), timedelta(days=3))
    late, _, late_at = seed_ledgered(people, migrate_engine, timedelta(days=3), timedelta(hours=1))
    never = ledger.Entry(people.a, uuid.uuid4(), late_at)
    storage.ledger_write(never.key, never.body())
    assert reapply(early_at + timedelta(days=1)) == 0
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        assert conn.execute(CLIENT_ROWS, {"id": early}).one().erased_at is None
        assert conn.execute(CLIENT_ROWS, {"id": late}).one().erased_at is not None
    assert last_reapply(migrate_engine)["absent"] == 1


# ZIF-58. GUARD. An entry it cannot read stops the run before anything is applied: exit 1 (the
# uncaught error), the database as it was.
def test_an_unreadable_entry_applies_nothing(
    people: People, migrate_engine: Engine, ledger_bucket: S3Client
) -> None:
    client_id, _, erased_at = seed_ledgered(
        people, migrate_engine, timedelta(days=2), timedelta(minutes=5)
    )
    ledger_bucket.put_object(
        Bucket=BUCKET, Key="erasures/9999/12/31/x.json", Body=b'{"v": 2, "kind": "client"}'
    )
    before = take(migrate_engine, people.a)
    with pytest.raises(ValueError):
        reapply(erased_at - timedelta(hours=1))
    assert take(migrate_engine, people.a) == before
    assert client_id in before["clients"]


def erase_now(migrate_engine: Engine, tenant_id: uuid.UUID) -> uuid.UUID:
    """A client with no bookings, erased by the core alone: no audit event, no ledger job."""
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        client_id: uuid.UUID = conn.scalar(INSERT_CLIENT, {"email": fresh_email(), "user_id": None})
        conn.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
        conn.execute(text("SELECT erase_client_core(:id, true)"), {"id": client_id})
    return client_id


def key_of(migrate_engine: Engine, tenant_id: uuid.UUID, client_id: uuid.UUID) -> str:
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        erased_at = conn.scalar(
            text("SELECT erased_at FROM clients WHERE id = :id"), {"id": client_id}
        )
    return ledger.Entry(tenant_id, client_id, erased_at.astimezone(UTC)).key


# ZIF-58 F27. FENCE. Wrong impl: a backfill from the client_erased audit events (a restore rewinds
# them with the erasure, and an erasure is the tombstone, not its event: these have none). It
# writes exactly the entries the ledger lacks, in every business, and leaves the present one be.
def test_backfill_writes_exactly_the_missing_entries(
    people: People, migrate_engine: Engine, ledger_bucket: S3Client
) -> None:
    before_since = erase_now(migrate_engine, people.a)
    with migrate_engine.begin() as conn:
        since = conn.scalar(text("SELECT clock_timestamp()"))
    ledgered = erase_now(migrate_engine, people.a)
    missing = [erase_now(migrate_engine, people.a), erase_now(migrate_engine, people.b)]
    present = key_of(migrate_engine, people.a, ledgered)
    ledger_bucket.put_object(Bucket=BUCKET, Key=present, Body=b"as the job wrote it")

    assert ledger.main(["backfill", "--since", since.isoformat()]) == 0
    entries = stored(ledger_bucket)
    assert set(entries) == {present} | {
        key_of(migrate_engine, tenant, client)
        for tenant, client in zip((people.a, people.b), missing, strict=True)
    }
    assert entries[present] == b"as the job wrote it"
    assert key_of(migrate_engine, people.a, before_since) not in entries
    for key in set(entries) - {present}:
        assert json.loads(entries[key])["v"] == 1


# ZIF-58. GUARD. reapply needs a reason (the platform audit event's), backfill none; a time
# without its offset is a usage error, not a guess at the zone.
@pytest.mark.parametrize(
    "argv",
    [
        ["reapply", "--since", "2026-11-03T09:00:00+00:00"],
        ["reapply", "--since", "2026-11-03T09:00:00+00:00", "--reason", " "],
        ["backfill", "--since", "2026-11-03T09:00:00"],
        ["backfill"],
    ],
)
def test_usage_errors_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        ledger.main(argv)
    assert exit_.value.code == 2
