"""The erasure ledger (ZIF-58): every erasure, recorded outside the database.

A restore rewinds the database, so the erasures made after the restore point come back, and so do
their audit events. The ledger keeps them where a restore cannot reach: one object per erasure in
a dedicated bucket with a bucket lock (LedgerSettings), ids and a time only.

An erasure enqueues an erasure.ledger job in its own transaction (enqueue), so the entry exists
only for an erasure that committed; the worker writes it (record).
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app import jobs, storage
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


def enqueue(db: Session, tenant_id: UUID, client_id: UUID, erased_at: datetime) -> None:
    """Add the erasure's ledger job in the erasure's own transaction: a rollback drops it with
    the erasure."""
    entry = Entry(tenant_id, client_id, erased_at.astimezone(UTC))
    jobs.enqueue(
        db,
        "erasure.ledger",
        f"erasure.ledger:{tenant_id}:client:{client_id}",
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
