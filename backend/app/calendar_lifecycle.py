"""Local calendar disconnect and member removal, in the caller's tenant transaction."""

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.auth import SignedIn
from app.errors import ApiError


@dataclass(frozen=True)
class DisconnectResult:
    state: Literal["disconnected", "revocation_pending"]


LOCK_CONNECTION = text("""
SELECT id, state FROM google_calendar_connections
WHERE tenant_id = :tenant AND member_id = :member
FOR UPDATE
""")


def _connection(db: Session, tenant_id: UUID, member_id: UUID) -> Row[Any] | None:
    return db.execute(LOCK_CONNECTION, {"tenant": tenant_id, "member": member_id}).one_or_none()


def require_reconnect_allowed(db: Session, tenant_id: UUID, member_id: UUID) -> None:
    """Check under the membership lock at OAuth start and again at credential commit."""
    connection = _connection(db, tenant_id, member_id)
    if connection is not None and connection.state == "revocation_pending":
        raise ApiError(409, "calendar_revocation_pending")


def disconnect(current: SignedIn, member_id: UUID) -> DisconnectResult:
    """Disconnect one's own link, or another member's link as the current owner."""
    from app import time_off

    time_off.lock_bookings(current)
    members = current.db.execute(
        text("""
        SELECT id, user_id, role FROM memberships
        WHERE tenant_id = :tenant AND role <> 'removed' AND (id = :member OR user_id = :actor)
        ORDER BY id FOR UPDATE
        """),
        {"tenant": current.tenant_id, "member": member_id, "actor": current.user_id},
    ).all()
    actor = next((row for row in members if row.user_id == current.user_id), None)
    if actor is None:
        raise ApiError(401, "unauthenticated")
    member = next((row for row in members if row.id == member_id), None)
    if member is None:
        raise ApiError(404, "not_found")
    if actor.id != member.id and actor.role != "owner":
        raise ApiError(403, "owner_only")
    return _disable(current.db, current.tenant_id, member_id)


def remove_member(db: Session, tenant_id: UUID, member_id: UUID) -> None:
    """Tombstone the link after the caller locks bookings and the membership."""
    _disable(db, tenant_id, member_id)


def _disable(db: Session, tenant_id: UUID, member_id: UUID) -> DisconnectResult:
    connection = _connection(db, tenant_id, member_id)
    member = {"tenant": tenant_id, "member": member_id}
    db.execute(
        text("""
        DELETE FROM google_calendar_oauth_attempts WHERE tenant_id = :tenant AND member_id = :member
        """),
        member,
    )
    if connection is None or connection.state == "revoked":
        return DisconnectResult("disconnected")
    if connection.state == "revocation_pending":
        return DisconnectResult("revocation_pending")

    # The envelope and its immutable encryption generation still bind the revocation credential.
    # shortcut: 3b NULLs the calendar-identity envelope here (§14)
    link = {**member, "connection": connection.id}
    db.execute(
        text("""
        UPDATE google_calendar_connections
        SET state = 'revocation_pending', disconnected_at = now(), next_revoke_at = now(),
            generation = generation + 1, lease_sequence = lease_sequence + 1,
            lease_id = NULL, lease_until = NULL, next_sync_at = NULL,
            scheduling_state = 'runnable', park_reason = NULL, recheck_at = NULL,
            coverage_start = NULL, coverage_end = NULL, fetch_started_at = NULL,
            last_attempt_at = NULL, last_success_at = NULL, failure_count = 0,
            failure_episode = NULL, last_notified_episode = NULL, error_category = NULL
        WHERE tenant_id = :tenant AND member_id = :member AND id = :connection
        """),
        link,
    )
    db.execute(
        text("""
        DELETE FROM time_off WHERE tenant_id = :tenant AND member_id = :member
          AND calendar_connection_id = :connection AND source = 'google'
        """),
        link,
    )
    return DisconnectResult("revocation_pending")
