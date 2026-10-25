from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ZIF-58: the permanent wire contract for an erased client, shared by every model that names a
# booking's client (app.bookings.BookingRow, InTheWay) and pinned in
# tests/test_erased_client_reads.py. client_name stays a string, never null.
CLIENT_NAME = (
    'The client\'s name. "" exactly when client_erased is true: an erased client has no name, '
    "and the console shows it as an erased client."
)
CLIENT_ERASED = 'True when the client was erased; client_name is then "".'


class InTheWay(BaseModel):
    """An appointment a refused time-off block would cover (ZIF-130, overlaps_bookings)."""

    id: UUID
    starts_at: datetime
    ends_at: datetime
    client_name: str = Field(description=CLIENT_NAME)
    client_erased: bool = Field(description=CLIENT_ERASED)
    service_name: dict[str, str]  # all three locales, as snapshotted (BookingRow.service_name)
    status: Literal["pending", "confirmed"]


class Error(BaseModel):
    """Every API error body: a stable code the web app translates, never prose."""

    code: str
    # Only the opening-hours refusal sets it (ZIF-105, outside_opening_hours): the ISO weekday the
    # console names in one translated message with a {weekday} placeholder. The handler omits every
    # unset key, so every other error body stays byte-identical. On the shared model, not a
    # per-route one: tests/test_openapi.py:34 requires every 4xx and 5xx body in the contract to be
    # exactly Error.
    weekday: int | None = None
    # Only overlaps_bookings sets these (ZIF-130): the first appointments in the way, soonest
    # first, and how many there are in all.
    bookings: list[InTheWay] | None = None
    total: int | None = None


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        weekday: int | None = None,
        *,
        bookings: list[InTheWay] | None = None,
        total: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        # Response headers the error carries: only a cookie being cleared (ZIF-146).
        self.headers = headers
        self.code = code
        self.weekday = weekday
        self.bookings = bookings
        self.total = total
