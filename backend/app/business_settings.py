"""A business's settings: every key typed and defaulted here, saved values in the settings table.

Add a key as a field with a default and a JSON-native type (str, bool, int, Literal). CONTRIBUTING
has the rules for removing, renaming and tightening one.
"""

import importlib.resources
import json
from email import message_from_string, policy
from email.message import EmailMessage
from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth, passwords
from app.auth import CurrentOwner, CurrentSession
from app.errors import Error
from app.locales import Locale as Locale  # re-exported: most modules import it from here

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


def header_address(email: str) -> str:
    """An address that reads back from a mail header as itself, or ValueError.

    Python sends a non-ASCII address as an encoded word, and parses "shop[1]@x" or a stray dot as
    something else, so a reply would reach nobody. An IDN domain still works in its xn-- form.
    """
    if not email.isascii():
        raise ValueError("not an address a reply can reach")
    message = EmailMessage()
    message["Reply-To"] = email
    header = message_from_string(message.as_string(), policy=policy.default)["Reply-To"]
    if header.defects or [a.addr_spec for a in header.addresses] != [email]:
        raise ValueError("not an address a reply can reach")
    return email


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
    # ZIF-143. Whether the assigned worker may accept or decline their own pending booking; owners
    # always may. Off: the API refuses a worker's answer (app/bookings.py transition) and their
    # new-request email says the owner answers it (app/mail.py send_booking).
    workers_answer_requests: bool = False
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
    # TTL expires (ZIF-5, denial of availability). Upcoming auto-confirmed bookings count too
    # (ZIF-115): see app/bookings.py PENDING_COUNT. Counted per requester IP, with a ceiling of
    # bookings.PENDING_ACROSS_IPS times this over all of them (ZB-19).
    max_pending_per_email: Annotated[int, Field(ge=1, le=50)] = 3
    # How long a pending booking holds its slot before availability stops counting it. Hours, not
    # minutes: it is a merchant-facing promise ("we answer within a day"), not a scheduling grid.
    # Nothing sweeps: availability carves expired pendings out at read time
    # (app/availability.py:175) and the booking transaction expires them before inserting
    # (app/bookings.py:264), so this key is read once, when a pending is born.
    pending_ttl_hours: Annotated[int, Field(ge=1, le=168)] = 24
    # The cancellation terms shown on the booking page, snapshotted onto every booking. ZIF-55
    # reads the SNAPSHOT, never this key: a key would be re-read at cancellation time, and ZIF-5
    # rejects evaluating mutable settings at cancellation time as the thing that destroys the
    # dispute evidence. Empty means the business publishes no terms.
    cancellation_policy_text: Annotated[str, Field(max_length=2000)] = ""
    # ZIF-55. Hours before the appointment at or above which a cancellation is refunded in full,
    # and below which rescheduling is refused. Both are SNAPSHOTTED onto every booking as
    # bookings.free_cancellation_hours / reschedule_cutoff_hours; the engine (app/cancellation.py)
    # reads the snapshot and never these keys. The bounds here and the columns' CHECKs are ONE
    # pair: a value pydantic accepts and the CHECK rejects is a 500 on the public booking POST.
    # 0 is meaningful on both ("always refundable", "reschedule until it starts"). The inverted
    # combination (cutoff above the threshold) is legal and the engine answers it - there is no
    # cross-field validator, because update_settings validates the BODY alone, where an unsent key
    # takes the class default rather than the saved value.
    free_cancellation_hours: Annotated[int, Field(ge=0, le=720)] = 48
    reschedule_cutoff_hours: Annotated[int, Field(ge=0, le=720)] = 24
    # ZIF-54. How many times a client may reschedule a booking from their own link, snapshotted
    # onto every booking as bookings.max_reschedules at insert (ZIF-55's pattern). The bounds equal
    # migration 0029's ck_bookings_max_reschedules CHECK exactly, the same pair as the two hours
    # above: a value pydantic accepts and the CHECK rejects is a 500 on the public booking POST.
    # 0 turns client-side rescheduling off for new bookings; existing bookings keep the value they
    # were sold with.
    max_reschedules: Annotated[int, Field(ge=0, le=10)] = 2
    # ZIF-145. Whether the public booking page, its availability and its booking POST answer at all.
    # False (unpublished) until the owner flips it: no row means no business has published by
    # accident, new or old. The three public routes gate on this themselves (never here, never in
    # tenant_context): see app/booking_page.py, app/availability.py and app/bookings.py.
    published: bool = False
    # ZIF-158. Checkout asks for a card check, with paying in advance offered beside it. Effective
    # only while Stripe is configured, the account can take charges and the price meets Stripe's
    # minimum (app/payments.py offer()). No PUT cross-check: the effective mode fails open.
    payment_check: bool = False
    # ZIF-99. Where a client's reply to an email with no manage link goes (Reply-To); none when
    # unset. The client-facing contact, not the responsible person, and never defaulted to an
    # owner's login.
    # Public by design (every member reads it, and the booking page will show it), so a private
    # address does not belong here; it is kept in the settings audit log.
    # shortcut: the address is not confirmed, so an owner can point client replies at any inbox; add
    # a confirm link if one is ever abused.
    contact_email: Annotated[passwords.Email, AfterValidator(header_address)] | None = None


def read(db: Session) -> BusinessSettings:
    """The transaction's business's settings, defaults filled in.

    A saved key no longer in the registry is ignored; a saved value that no longer validates raises
    rather than quietly becoming the default.
    """
    saved = db.execute(text("SELECT key, value FROM settings"))
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
