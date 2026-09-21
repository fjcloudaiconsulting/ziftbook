"""A business's settings: every key typed and defaulted here, saved values in the settings table.

Add a key as a field with a default and a JSON-native type (str, bool, int, Literal). CONTRIBUTING
has the rules for removing, renaming and tightening one.
"""

import importlib.resources
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth
from app.auth import CurrentOwner, CurrentSession
from app.errors import Error

Locale = Literal["en", "nl", "pt"]

# The tzdata package's own list: zoneinfo.available_timezones() also adds the system's files, so the
# accepted names would differ between machines.
ZONES = frozenset(importlib.resources.files("tzdata").joinpath("zones").read_text().split())


def whole(value: object) -> object:
    # A Literal of ints matches by equality, so 15.0 and True would pass even in strict mode.
    if type(value) is not int:
        raise ValueError("not an integer")
    return value


def known_zone(name: str) -> str:
    # Membership, not ZoneInfo(name): on a case-insensitive disk that accepts "europe/amsterdam".
    if name not in ZONES:
        raise ValueError("unknown timezone")
    return name


class BusinessSettings(BaseModel):
    # strict: "true" or 1 is not a boolean. The same model reads a PUT body, where every key may be
    # left out, and answers with every key filled in.
    model_config = ConfigDict(
        strict=True, extra="forbid", json_schema_serialization_defaults_required=True
    )

    # Convert a business's local times with zoneinfo, never AT TIME ZONE: Postgres doesn't know
    # every name tzdata accepts (US/Pacific, Asia/Calcutta).
    timezone: Annotated[str, AfterValidator(known_zone)] = "Europe/Amsterdam"
    auto_confirm: bool = False
    # The language of emails to someone who chose none. Sign-up sets it from the country.
    language: Locale = "en"
    # Whether a worker may change their own working hours; owners always may.
    workers_edit_own_hours: bool = False
    # Availability (ZIF-48). The buffer after an appointment when its service has no override,
    # as a whole percentage of its duration, rounded up to a minute.
    buffer_pct: Annotated[int, Field(ge=0, le=100)] = 10
    # Slots start on this grid, counted from each shift's start rounded up to it. Every step divides
    # 60, so the grid stays on local quarter-hours across whole-hour clock changes.
    # ponytail: a 30-minute DST shift (Lord Howe) with step 20 misaligns by 10 minutes; nobody books
    # there yet.
    slot_step_minutes: Annotated[Literal[5, 10, 15, 20, 30, 60], BeforeValidator(whole)] = 15
    # How soon, and how many days ahead (counted from today in the business's timezone), a client
    # may book.
    min_notice_minutes: Annotated[int, Field(ge=0, le=10080)] = 60
    booking_horizon_days: Annotated[int, Field(ge=1, le=365)] = 60
    # How many unsettled bookings one email address may hold at this business at once. Counted
    # inside the booking transaction, after find_or_create: pendings hold slots by design, so
    # without a cap a script fills the next 60 days under throwaway addresses and refills as the
    # TTL expires (ZIF-5, denial of availability).
    max_pending_per_email: Annotated[int, Field(ge=1, le=50)] = 3
    # The cancellation terms shown on the booking page, snapshotted onto every booking. ZIF-55
    # reads the SNAPSHOT, never this key: a key would be re-read at cancellation time, and ZIF-5
    # rejects evaluating mutable settings at cancellation time as the thing that destroys the
    # dispute evidence. Empty means the business publishes no terms.
    cancellation_policy_text: Annotated[str, Field(max_length=2000)] = ""


def read(db: Session) -> BusinessSettings:
    """The transaction's business's settings, defaults filled in.

    A saved key no longer in the registry is ignored; a saved value that no longer validates raises
    rather than quietly becoming the default.
    """
    saved = db.execute(text("SELECT key, value FROM settings")).tuples()
    return BusinessSettings.model_validate(
        {key: value for key, value in saved if key in BusinessSettings.model_fields}
    )


def save(db: Session, key: str, value: object) -> object:
    """Save one key for the transaction's business. Returns the value it replaced, None if none."""
    return db.execute(
        text("""
        INSERT INTO settings (tenant_id, key, value)
        VALUES (current_setting('app.tenant_id')::uuid, :key, CAST(:value AS jsonb))
        ON CONFLICT (tenant_id, key) DO UPDATE SET value = excluded.value
        RETURNING old.value AS old
        """),
        {"key": key, "value": json.dumps(value)},
    ).scalar()


router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings", name="read", responses={401: {"model": Error}})
def read_settings(current: CurrentSession, response: Response) -> BusinessSettings:
    response.headers["Cache-Control"] = "no-store"
    return read(current.db)


@router.put(
    "/settings", name="update", responses={s: {"model": Error} for s in (401, 403, 415, 422)}
)
def update_settings(
    changes: BusinessSettings,
    current: CurrentOwner,
    request: Request,
    response: Response,
) -> BusinessSettings:
    """Save the keys sent; keys left out keep their value. Every change goes in the audit log."""
    defaults = BusinessSettings()
    # Field order, not the set of sent keys: two saves must lock rows in the same order.
    for key, value in changes.model_dump(exclude_unset=True).items():
        replaced = save(current.db, key, value)
        old = getattr(defaults, key) if replaced is None else replaced
        if old != value:
            auth.record(
                current.db,
                request,
                "setting_changed",
                actor_user_id=current.user_id,
                target=f"setting:{key}",
                details={"old": old, "new": value},
            )
    response.headers["Cache-Control"] = "no-store"
    return read(current.db)
