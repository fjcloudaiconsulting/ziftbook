import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

ENQUEUE = text("""
INSERT INTO jobs (kind, dedupe_key, tenant_id, payload, due_at, next_attempt_at)
VALUES (:kind, :dedupe_key, :tenant_id, CAST(:payload AS jsonb),
        coalesce(CAST(:due_at AS timestamptz), now()),
        coalesce(CAST(:due_at AS timestamptz), now()))
ON CONFLICT (dedupe_key) DO NOTHING
RETURNING id
""")


def enqueue(
    session: Session,
    kind: str,
    dedupe_key: str,
    payload: dict[str, Any],
    *,
    tenant_id: UUID | None = None,
    due_at: datetime | None = None,
) -> bool:
    """Add a job in the caller's transaction, unless its dedupe_key was ever used.

    Returns whether it was added. Build the key as "kind:tenant_id:natural id", adding the due time
    when the same thing can be rescheduled ("booking.reminder:<tenant>:<booking>:<starts_at>").
    The payload holds ids only.
    """
    added = session.execute(
        ENQUEUE,
        {
            "kind": kind,
            "dedupe_key": dedupe_key,
            "tenant_id": tenant_id,
            "payload": json.dumps(payload),
            "due_at": due_at,
        },
    ).first()
    return added is not None
