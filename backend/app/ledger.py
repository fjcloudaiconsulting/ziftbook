"""The erasure ledger (ZIF-58): every erasure, recorded outside the database.

A restore rewinds the database, so the erasures made after the restore point come back, and so do
their audit events. The ledger keeps them where a restore cannot reach: one object per erasure in
a dedicated bucket with a bucket lock (LedgerSettings), ids and a time only.

An erasure enqueues an erasure.ledger job in its own transaction (enqueue), so the entry exists
only for an erasure that committed; the worker writes it (record).

    python -m app.ledger reapply [--since <ISO time>] --reason "<why>"
    python -m app.ledger backfill --since <ISO time>

reapply erases again, after a restore, every client the ledger lists (from --since on, the restore
point minus an hour, when given), with the ledger's read-only credentials. It connects as
ziftbook_migrate (ZIF_MIGRATE_DATABASE_URL), from the migrations image, as app.admin does, before
the API and the worker start (CONTRIBUTING "Restoring the database"). Exit 0 when done or nothing
was to do, 1 on any error (logged CRITICAL; nothing was applied), 2 on a usage error.

backfill queues the worker to write the entries the ledger is missing (an erasure.ledger job that
gave up), from the live database; it needs no ledger key. Same connection and exit codes.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import admin, bookings, jobs, logs, payments, storage
from app.config import LOCAL_HOSTS, LedgerBucketSettings, LedgerSettings, MailSettings
from app.db import join_tenant
from app.errors import ApiError
from app.jobs import Job

logger = logging.getLogger("app.ledger")  # not __name__: "__main__" under python -m


@dataclass(frozen=True)
class Entry:
    """One client erasure: which client of which business, and when. Nothing else, ever: the ids
    map to nothing in the live database, and to the restored rows only after a restore."""

    tenant_id: UUID
    client_id: UUID
    erased_at: datetime

    @classmethod
    def parse(cls, data: dict[str, Any]) -> Entry:
        """From a job payload or a ledger object. Only the known fields are read: whatever else a
        payload carries never reaches the ledger. Raises ValueError (KeyError, TypeError) on
        anything else."""
        # An account deletion's entry comes with it (ZIF-58 PR 8), and with its re-apply.
        if data["kind"] != "client":
            raise ValueError("not a client erasure")
        erased_at = datetime.fromisoformat(data["erased_at"])
        if erased_at.tzinfo is None:
            raise ValueError("an erasure time without a time zone")
        return cls(UUID(data["tenant_id"]), UUID(data["client_id"]), erased_at.astimezone(UTC))

    @property
    def key(self) -> str:
        """Deterministic, so a repeat writes nothing; in time order, so a listing is too."""
        at = self.erased_at
        return f"erasures/{at:%Y/%m/%d}/{at:%Y%m%dT%H%M%S.%f}Z-client-{self.client_id}.json"

    def body(self) -> bytes:
        return json.dumps(
            {
                "v": 1,
                "kind": "client",
                "tenant_id": str(self.tenant_id),
                "client_id": str(self.client_id),
                "erased_at": self.erased_at.isoformat(),
            }
        ).encode()


def require() -> None:
    """503 erasure_unavailable while the ledger bucket is unset, outside localhost (ZIF_APP_URL):
    no deployment erases what a restore would bring back unrecorded. Every erasure route calls it
    first. Development may erase without one; its ledger jobs fail until a bucket is set."""
    if LedgerBucketSettings().erasure_ledger_bucket:
        return
    if not local():
        raise ApiError(503, "erasure_unavailable")


def local() -> bool:
    """Whether this is development: ZIF_APP_URL on localhost."""
    return urlsplit(MailSettings().app_url).hostname in LOCAL_HOSTS


def enqueue(
    db: Session, tenant_id: UUID, client_id: UUID, erased_at: datetime, *, backfill: str = ""
) -> bool:
    """Add the erasure's ledger job in the erasure's own transaction: a rollback drops it with
    the erasure. backfill names a backfill run, whose jobs get keys of their own: the erasure's
    job, given up, is kept 30 days and would hold its key. Returns whether it was added."""
    entry = Entry(tenant_id, client_id, erased_at.astimezone(UTC))
    again = f":backfill:{backfill}" if backfill else ""
    return jobs.enqueue(
        db,
        "erasure.ledger",
        f"erasure.ledger:{tenant_id}:client:{client_id}{again}",
        {
            "kind": "client",
            "tenant_id": str(entry.tenant_id),
            "client_id": str(entry.client_id),
            "erased_at": entry.erased_at.isoformat(),
        },
        tenant_id=tenant_id,
    )


def record(job: Job) -> None:
    """The erasure.ledger job: write the entry, once. Already there is done."""
    entry = Entry.parse(job.payload)
    written = storage.ledger_write(entry.key, entry.body())
    logger.info("erasure ledgered", extra={"client_id": str(entry.client_id), "written": written})


class Refused(Exception):
    """A run that cannot be right, refused before anything is applied: exit with code."""

    def __init__(self, message: str, code: int, **fields: object) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


# A key that holds ids and a time only, as Entry.key builds it: logged and audited as it is. Any
# other key (anyone with the writing key can name an object anything) by its hash.
SAFE_KEY = re.compile(r"erasures/\d{4}/\d{2}/\d{2}/\d{8}T\d{6}\.\d{6}Z-client-[0-9a-f-]{36}\.json")
# An entry dated later than this is refused: an erasure cannot have happened in the future.
SKEW = timedelta(minutes=5)


def name_of(key: str) -> str:
    return key if SAFE_KEY.fullmatch(key) else "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def read(
    since: datetime | None, skip: frozenset[str] = frozenset()
) -> tuple[list[Entry], list[str]]:
    """The ledger's entries from since on (all of them without), oldest first, and the names of
    those skipped. An entry it cannot apply (unreadable, of an unknown version or kind, its body
    not the entry its key names, or dated in the future) refuses the run, each by name, so the
    operator can inspect it and pass it with --skip-key."""
    start = "erasures/" if since is None else f"erasures/{since.astimezone(UTC):%Y/%m/%d}/"
    latest = datetime.now(UTC) + SKEW
    entries, skipped, refused = [], [], []
    for key, raw in storage.ledger_entries(start):
        name = name_of(key)
        if name in skip:
            skipped.append(name)
            continue
        try:
            data = json.loads(raw)
            if data.get("v") != 1:
                raise ValueError("a ledger entry of an unknown version")
            entry = Entry.parse(data)
            if entry.key != key or entry.erased_at > latest:
                raise ValueError("a ledger entry that is not the erasure its key names")
        except ValueError, KeyError, TypeError, AttributeError:
            refused.append(name)
            continue
        if since is None or entry.erased_at >= since:
            entries.append(entry)
    if unused := skip - set(skipped):
        raise Refused("reapply refused: a --skip-key names no ledger entry", 2, keys=sorted(unused))
    if refused:
        for name in refused:
            logger.error("ledger entry refused", extra={"key": name})
        raise Refused(
            "reapply refused: ledger entries it cannot apply; inspect each, and pass one that is"
            " not an erasure with --skip-key",
            1,
            refused=len(refused),
        )
    return sorted(entries, key=lambda entry: entry.erased_at), skipped


# What the restored database holds: its newest record of anything (a restore point is no later),
# and how many clients it records erased from since on, in every business.
NEWEST_GLOBAL = text("""
SELECT greatest((SELECT max(created_at) FROM audit_events),
                (SELECT max(created_at) FROM platform_audit_events),
                (SELECT max(completed_at) FROM jobs))
""")
NEWEST_IN_BUSINESS = text("""
SELECT greatest((SELECT max(created_at) FROM bookings WHERE tenant_id = :tenant_id),
                (SELECT max(created_at) FROM booking_events WHERE tenant_id = :tenant_id),
                (SELECT max(erased_at) FROM clients WHERE tenant_id = :tenant_id)),
       (SELECT count(*) FROM clients
        WHERE tenant_id = :tenant_id AND erased_at >= coalesce(CAST(:since AS timestamptz),
                                                              '-infinity'))
""")


def check_window(engine: Engine, since: datetime | None, entries: list[Entry]) -> None:
    """Refuse a --since later than everything the restored database holds (it would skip the
    erasures between the real restore point and it), and an empty listing for a window in which
    the database itself records erasures (a wrong bucket or key)."""
    with Session(engine) as session, session.begin():
        session.execute(text("SET LOCAL app.audit_review = 'on'"))
        newest = [session.scalar(NEWEST_GLOBAL)]
        erased = 0
        for tenant_id in session.scalars(text("SELECT id FROM tenants")).all():
            join_tenant(session, tenant_id)
            row = session.execute(
                NEWEST_IN_BUSINESS, {"tenant_id": tenant_id, "since": since}
            ).one()
            newest.append(row[0])
            erased += row[1]
    latest = max((at for at in newest if at is not None), default=None)
    if since is not None and (latest is None or since > latest):
        raise Refused(
            "reapply refused: --since is after the restored database's newest record;"
            " leave it out to read the whole ledger",
            2,
            newest=latest.isoformat() if latest else None,
        )
    if not entries and erased:
        raise Refused(
            "reapply refused: the ledger lists no erasure where the database records some;"
            " check the bucket and its key",
            1,
            erased=erased,
            bucket=LedgerSettings().erasure_ledger_bucket,
        )


CLIENT = text("""
SELECT erased_at FROM clients WHERE tenant_id = :tenant_id AND id = :id FOR NO KEY UPDATE
""")
CORE = text("SELECT erase_client_core(:id, false)")
PAID_OF = text("""
SELECT DISTINCT booking_id FROM booking_payments
WHERE tenant_id = :tenant_id AND booking_id = ANY(CAST(:ids AS uuid[]))
  AND status = 'paid' AND settled_at IS NULL
""")


def reapply(
    engine: Engine, since: datetime | None, reason: str, skip: frozenset[str] = frozenset()
) -> int:
    """Erase again every client the ledger lists from since on that the restored database still
    has unerased, in erasure order, in one transaction with its platform audit event: all of it
    or, on any error, none. A client already erased, or one the restored database never had, is
    counted and left alone, so a second run changes nothing."""
    # Before the transaction: no outbound call under a booking lock.
    entries, skipped = read(since, skip)
    check_window(engine, since, entries)
    counts = {"entries": len(entries), "reapplied": 0, "already_erased": 0, "absent": 0}
    cancelled = 0
    paid_ended: list[str] = []  # settle pays these no more than Stripe shows (payments._refund)
    with Session(engine) as session, session.begin():
        for entry in entries:
            join_tenant(session, entry.tenant_id)
            session.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})  # first, as every writer
            row = session.execute(
                CLIENT, {"tenant_id": entry.tenant_id, "id": entry.client_id}
            ).first()
            if row is None:
                counts["absent"] += 1
                continue
            if row.erased_at is not None:
                counts["already_erased"] += 1
                continue
            ended = bookings.end_for_erasure(
                session,
                entry.tenant_id,
                entry.client_id,
                reason="erasure_reapplied",
                at=entry.erased_at,
            )
            # Stripe is safe to ask again: settle reads what was refunded already.
            payments.settle_ended(session, entry.tenant_id, ended)
            paid_ended += [
                str(booking)
                for booking in session.scalars(
                    PAID_OF, {"tenant_id": entry.tenant_id, "ids": ended}
                )
            ]
            # false: the cancellations were announced the first time.
            if session.scalar(CORE, {"id": entry.client_id}) is None:
                # The core and the lookup above disagree on the business: nothing is applied.
                raise RuntimeError("the erase core found no client")
            counts["reapplied"] += 1
            cancelled += len(ended)
        admin.record(
            session,
            None,
            "erasure_reapplied",
            actor_user_id=None,
            reason=reason,
            details={
                "via": "cli",
                "cancelled": cancelled,
                "paid_ended": len(paid_ended),
                "skipped": skipped,
                **counts,
            },
        )
    # The paid bookings it ended, by id: the operator checks each in Stripe (runbook step 3).
    logger.info(
        "erasures reapplied",
        extra={
            "bucket": LedgerSettings().erasure_ledger_bucket,
            "cancelled": cancelled,
            "paid_ended": paid_ended,
            "skipped": skipped,
            **counts,
        },
    )
    return 0


ERASED_SINCE = text("""
SELECT id, erased_at FROM clients WHERE tenant_id = :tenant_id AND erased_at >= :since
""")


def backfill(engine: Engine, since: datetime) -> int:
    """Queue an erasure.ledger job for every client erased from since on, in every business: the
    worker writes the entries the ledger lacks (one already there is a no-op, written: false).
    The CLI holds no ledger key: only the worker writes (owner decision 8). From the database's
    own record of an erasure, clients.erased_at, never the audit events: the tombstone is the
    erasure, its event is the route's. Each run's jobs have keys of their own, so a run re-arms a
    backfill job that gave up. Account deletions' entries come with them (ZIF-58 PR 8)."""
    run = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    queued = 0
    with Session(engine) as session, session.begin():
        for tenant_id in session.scalars(text("SELECT id FROM tenants")).all():
            join_tenant(session, tenant_id)
            rows = session.execute(ERASED_SINCE, {"tenant_id": tenant_id, "since": since}).all()
            for row in rows:
                queued += enqueue(session, tenant_id, row.id, row.erased_at, backfill=run)
    logger.info("ledger backfill queued", extra={"queued": queued})
    return 0


def _since(value: str) -> datetime:
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("give an ISO 8601 time") from None
    if at.tzinfo is None:
        raise argparse.ArgumentTypeError("give the time with its offset (Z or +00:00)")
    return at


def _skip_key(value: str) -> str:
    # Names only: an audit detail never holds what an operator might mistype into it.
    if not (SAFE_KEY.fullmatch(value) or re.fullmatch(r"sha256:[0-9a-f]{64}", value)):
        raise argparse.ArgumentTypeError("give a key as 'ledger entry refused' names it")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ledger", description=__doc__)
    parser.add_argument("command", choices=("reapply", "backfill"))
    parser.add_argument(
        "--since",
        type=_since,
        help="reapply: the restore point - 1h (default: the whole ledger); backfill: required",
    )
    parser.add_argument("--reason", type=admin._reason, help="reapply: why, for the audit log")
    parser.add_argument(
        "--skip-key",
        action="append",
        default=[],
        type=_skip_key,
        help="reapply: pass this entry (repeatable), as 'ledger entry refused' names it",
    )
    args = parser.parse_args(argv)
    if args.command == "reapply" and args.reason is None:
        parser.error("reapply needs --reason")
    if args.command == "backfill" and args.since is None:
        parser.error("backfill needs --since")
    if args.command == "reapply" and not LedgerSettings().configured():
        parser.error(
            "reapply needs ZIF_S3_ENDPOINT, ZIF_ERASURE_LEDGER_BUCKET and the ledger's read-only"
            " key (ZIF_ERASURE_LEDGER_ACCESS_KEY_ID, ZIF_ERASURE_LEDGER_SECRET_ACCESS_KEY)"
        )
    engine = create_engine(
        os.environ["ZIF_MIGRATE_DATABASE_URL"], poolclass=NullPool, hide_parameters=True
    )
    try:
        if args.command == "backfill":
            return backfill(engine, args.since)
        return reapply(engine, args.since, args.reason, frozenset(args.skip_key))
    except Refused as refused:
        logger.error(str(refused), extra=refused.fields)
        return refused.code
    finally:
        engine.dispose()


if __name__ == "__main__":
    logs.configure()
    sys.exit(main())
