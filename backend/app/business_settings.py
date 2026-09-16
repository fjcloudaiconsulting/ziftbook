"""A business's settings: every key typed and defaulted here, saved values in the settings table.

Add a key as a field with a default and a JSON-native type (str, bool, int, Literal). CONTRIBUTING
has the rules for removing, renaming and tightening one.
"""

import importlib.resources
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from pydantic import AfterValidator, BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth
from app.auth import CurrentOwner, CurrentSession
from app.errors import Error

Locale = Literal["en", "nl", "pt"]

# The tzdata package's own list: zoneinfo.available_timezones() also adds the system's files, so the
# accepted names would differ between machines.
ZONES = frozenset(importlib.resources.files("tzdata").joinpath("zones").read_text().split())


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
