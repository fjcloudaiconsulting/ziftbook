"""The erasure ledger (ZIF-58): every erasure, recorded outside the database.

A restore rewinds the database, so the erasures made after the restore point come back, and so do
their audit events. The ledger keeps them where a restore cannot reach: one object per erasure in
a dedicated bucket with a bucket lock (LedgerSettings), ids and a time only.

An erasure enqueues an erasure.ledger job in its own transaction (enqueue), so the entry exists
only for an erasure that committed; the worker writes it (record).

    python -m app.ledger reapply --since <ISO time> --reason "<why>"
    python -m app.ledger backfill --since <ISO time>

reapply erases again, after a restore, every client the ledger lists from that time on (the
restore point minus an hour), with the ledger's read-only credentials. It connects as
ziftbook_migrate (ZIF_MIGRATE_DATABASE_URL), from the migrations image, as app.admin does, before
the API and the worker start (CONTRIBUTING "Restoring the database"). Exit 0 when done or nothing
was to do, 1 on any error (logged CRITICAL; nothing was applied), 2 on a usage error.

backfill queues the worker to write the entries the ledger is missing (an erasure.ledger job that
gave up), from the live database; it needs no ledger key. Same connection and exit codes.
"""

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import admin, bookings, jobs, logs, payments, storage
from app.config import LOCAL_HOSTS, LedgerSettings, MailSettings
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
    if LedgerSettings().erasure_ledger_bucket:
        return
    if urlsplit(MailSettings().app_url).hostname not in LOCAL_HOSTS:
        raise ApiError(503, "erasure_unavailable")


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


def read(since: datetime) -> list[Entry]:
    """The ledger's entries from since on, oldest first. Raises on an entry it cannot read."""
    entries = []
    for raw in storage.ledger_entries(f"erasures/{since.astimezone(UTC):%Y/%m/%d}/"):
        data = json.loads(raw)
        if data.get("v") != 1:
            raise ValueError("a ledger entry of an unknown version")
        entry = Entry.parse(data)
        if entry.erased_at >= since:
            entries.append(entry)
    return sorted(entries, key=lambda entry: entry.erased_at)


# Every statement names the business, besides row-level security: the entry's business comes from
# outside the database, and a widened migrate-role policy must not let it reach another one.
CLIENT = text("""
SELECT erased_at FROM clients WHERE tenant_id = :tenant_id AND id = :id FOR NO KEY UPDATE
""")
CORE = text("SELECT erase_client_core(:id, false)")


def reapply(engine: Engine, since: datetime, reason: str) -> int:
    """Erase again every client the ledger lists from since on that the restored database still
    has unerased, in erasure order, in one transaction with its platform audit event: all of it
    or, on any error, none. A client already erased, or one the restored database never had, is
    counted and left alone, so a second run changes nothing."""
    entries = read(since)  # before the transaction: no outbound call under a booking lock
    counts = {"entries": len(entries), "reapplied": 0, "already_erased": 0, "absent": 0}
    cancelled = 0
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
            details={"via": "cli", "cancelled": cancelled, **counts},
        )
    logger.info("erasures reapplied", extra={"cancelled": cancelled, **counts})
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ledger", description=__doc__)
    parser.add_argument("command", choices=("reapply", "backfill"))
    parser.add_argument(
        "--since", required=True, type=_since, help="reapply: the restore point - 1h"
    )
    parser.add_argument("--reason", type=admin._reason, help="reapply: why, for the audit log")
    args = parser.parse_args(argv)
    if args.command == "reapply" and args.reason is None:
        parser.error("reapply needs --reason")
    engine = create_engine(
        os.environ["ZIF_MIGRATE_DATABASE_URL"], poolclass=NullPool, hide_parameters=True
    )
    try:
        if args.command == "backfill":
            return backfill(engine, args.since)
        return reapply(engine, args.since, args.reason)
    finally:
        engine.dispose()


if __name__ == "__main__":
    logs.configure()
    sys.exit(main())
