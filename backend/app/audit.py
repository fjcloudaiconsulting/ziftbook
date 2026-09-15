"""A business's audit log, read by its owner."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel
from sqlalchemy import text

from app.auth import CurrentSession
from app.errors import ApiError, Error

router = APIRouter(prefix="/api", tags=["audit"])


class AuditEventOut(BaseModel):
    id: UUID
    created_at: datetime
    action: str
    actor_user_id: UUID | None
    target: str | None
    ip: str | None  # only on the owner's own events
    user_agent: str | None  # only on the owner's own events


@router.get("/audit-events", responses={401: {"model": Error}, 403: {"model": Error}})
def list_events(
    current: CurrentSession,
    response: Response,
    before: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AuditEventOut]:
    """The business's events, newest first. The next page is before= the last id of this one."""
    if current.role != "owner":
        raise ApiError(403, "owner_only")
    # Staff addresses and browsers stay out of the owner's view (employee monitoring); operators
    # still have them. Row-level security limits the rows to the session's business.
    rows = current.db.execute(
        text("""
        SELECT id, created_at, action, actor_user_id, target,
               CASE WHEN actor_user_id = :me THEN host(ip) END AS ip,
               CASE WHEN actor_user_id = :me THEN user_agent END AS user_agent
        FROM audit_events
        WHERE CAST(:before AS uuid) IS NULL OR id < :before
        ORDER BY id DESC
        LIMIT :limit
        """),
        {"me": current.user_id, "before": before, "limit": limit},
    )
    response.headers["Cache-Control"] = "no-store"
    return [AuditEventOut.model_validate(row, from_attributes=True) for row in rows]
