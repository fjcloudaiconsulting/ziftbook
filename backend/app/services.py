"""What a business sells. Owners add and change services; everyone in the business reads them.
Services are archived, never deleted."""

import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Request, Response
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import Row, text

from app import auth, jobs, photos
from app.accounts import named, printable
from app.auth import CurrentOwner, CurrentSession, SignedIn
from app.business_settings import Locale
from app.errors import ApiError, Error

# Line and paragraph separators are Zl/Zp, not a C* category, so printable() alone lets them
# through; a textarea's value never carries them.
LINE_BREAKS = frozenset({"\u2028", "\u2029"})


def multiline(value: str) -> str:
    # printable, except that \n is allowed. \r, \t and the two separators above are still refused.
    if LINE_BREAKS & set(value):
        raise ValueError("unprintable characters")
    printable(value.replace("\n", ""))
    return value


NameText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    AfterValidator(named),
]
DescriptionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
    AfterValidator(multiline),
]
# The business's own text, by language; any other key is a 422. The web app shows the viewer's
# language, else the business's (the language setting), else the first one present.
Name = Annotated[dict[Locale, NameText], Field(min_length=1)]
Description = dict[Locale, DescriptionText]  # {} for none
Amount = Annotated[int, Field(ge=0, le=1_000_000)]  # minor units, in the business's currency
Duration = Annotated[int, Field(ge=5, le=720)]
Buffer = Annotated[int, Field(ge=0, le=240)]
# What a service without a photo shows (ZIF-133); the web app pre-selects it from the name.
Placeholder = Literal["generic", "nails", "hair", "beauty", "massage"]

# Built field by field: nothing a client sends can name a business, a currency or an id.
STRICT = ConfigDict(strict=True, extra="forbid")
MAX_WORKERS = 200  # ponytail: a business has tens of members; raise it if one ever has more


class PriceIn(BaseModel):
    model_config = STRICT
    amount_minor: Amount  # no currency: always the business's


class Price(BaseModel):
    amount_minor: int
    currency: str


class ServiceIn(BaseModel):
    model_config = STRICT
    name: Name
    description: Description = Field(default_factory=dict)
    price: PriceIn
    duration_minutes: Duration
    buffer_minutes: Buffer | None = None  # None: the business's default
    # Member ids, assigned as PUT /workers would, in the same transaction; [] for none.
    # strict=False on the item, as bookings.BookingIn: strict mode refuses every UUID string.
    worker_ids: Annotated[
        list[Annotated[UUID, Field(strict=False)]], Field(max_length=MAX_WORKERS)
    ] = Field(default_factory=list)
    placeholder: Placeholder = "generic"
    # An upload checked and linked when this is saved (ZIF-133); strict=False as worker_ids.
    photo_upload_id: Annotated[UUID, Field(strict=False)] | None = None


class ServiceChange(BaseModel):
    """Only the fields sent change. buffer_minutes may be null (back to the business's default); no
    other field may. name and description are replaced whole, not merged per language: the web app
    always sends every language it holds."""

    model_config = STRICT
    name: Name | SkipJsonSchema[None] = None
    description: Description | SkipJsonSchema[None] = None
    price: PriceIn | SkipJsonSchema[None] = None
    duration_minutes: Duration | SkipJsonSchema[None] = None
    buffer_minutes: Buffer | None = None
    archived: bool | SkipJsonSchema[None] = None
    placeholder: Placeholder | SkipJsonSchema[None] = None
    # A new upload sets or replaces the photo, null removes it (like buffer_minutes).
    photo_upload_id: Annotated[UUID, Field(strict=False)] | None = None

    @field_validator(
        "name", "description", "price", "duration_minutes", "archived", "placeholder", mode="before"
    )
    @classmethod
    def not_null(cls, value: object) -> object:
        # Runs only for fields that were sent: leaving one out is fine, null isn't.
        if value is None:
            raise ValueError("null")
        return value


class ServiceOut(BaseModel):
    id: UUID
    name: dict[Locale, str]
    description: dict[Locale, str]
    price: Price
    duration_minutes: int
    buffer_minutes: int | None
    archived: bool
    archived_at: datetime | None  # when it was first archived; null while it is live
    worker_ids: list[UUID]  # member ids, in id order; [] for none
    placeholder: Placeholder
    photo: photos.Photo | None  # null while it has none, or while ZIF_MEDIA_URL is unset


FIELDS = """
id, name, description,
jsonb_build_object('amount_minor', price_amount_minor, 'currency', price_currency) AS price,
duration_minutes, buffer_minutes, archived_at IS NOT NULL AS archived, archived_at,
photo_placeholder AS placeholder, photo_key, photo_width, photo_height,
ARRAY(SELECT sw.member_id FROM service_workers sw
      WHERE sw.service_id = services.id ORDER BY sw.member_id) AS worker_ids
"""

LOGGED_AS_CHANGED = frozenset({"name", "description"})  # recorded as "changed", never their text

router = APIRouter(prefix="/api", tags=["services"])


def out(row: Row[Any]) -> ServiceOut:
    return ServiceOut.model_validate(
        {**row._mapping, "photo": photos.urls(row.photo_key, row.photo_width, row.photo_height)}
    )


def found_row(current: SignedIn, service_id: UUID, lock: str = "") -> Row[Any]:
    """One service of the session's business (row-level security); 404 for any other."""
    row = current.db.execute(
        text(f"SELECT {FIELDS} FROM services WHERE id = :id {lock}"), {"id": service_id}
    ).first()
    if row is None:
        raise ApiError(404, "not_found")
    return row


def found(current: SignedIn, service_id: UUID, lock: str = "") -> ServiceOut:
    return out(found_row(current, service_id, lock))


@router.get("/services", name="list", responses={401: {"model": Error}})
def list_services(current: CurrentSession, response: Response) -> list[ServiceOut]:
    """Every service of the business, archived ones included; the booking flow (ZIF-51) is what
    filters those out for new bookings.

    ponytail: no pagination, since a business has tens of services. Add before= as in audit.py if
    one ever has hundreds.
    """
    rows = current.db.execute(text(f"SELECT {FIELDS} FROM services ORDER BY id")).all()
    response.headers["Cache-Control"] = "no-store"
    return [out(row) for row in rows]


@router.get(
    "/services/{service_id}", name="read", responses={s: {"model": Error} for s in (401, 404, 422)}
)
def read_service(service_id: UUID, current: CurrentSession, response: Response) -> ServiceOut:
    service = found(current, service_id)
    response.headers["Cache-Control"] = "no-store"
    return service


@router.post(
    "/services",
    name="create",
    status_code=201,
    responses={s: {"model": Error} for s in (401, 403, 415, 422, 429, 503)},
)
def create_service(
    new: ServiceIn, current: CurrentOwner, request: Request, response: Response
) -> ServiceOut:
    row = current.db.execute(
        text(f"""
        INSERT INTO services (tenant_id, name, description, price_amount_minor, price_currency,
                              duration_minutes, buffer_minutes, photo_placeholder)
        SELECT id, CAST(:name AS jsonb), CAST(:description AS jsonb), :amount_minor, currency,
               :duration_minutes, :buffer_minutes, :placeholder
        FROM tenants WHERE id = current_setting('app.tenant_id')::uuid
        RETURNING {FIELDS}
        """),
        {
            "name": json.dumps(new.name),
            "description": json.dumps(new.description),
            "amount_minor": new.price.amount_minor,
            "duration_minutes": new.duration_minutes,
            "buffer_minutes": new.buffer_minutes,
            "placeholder": new.placeholder,
        },
    ).one()
    service = out(row)
    auth.record(
        current.db,
        request,
        "service_created",
        actor_user_id=current.user_id,
        target=f"service:{service.id}",
    )
    response.headers["Cache-Control"] = "no-store"
    if new.photo_upload_id is not None:
        # The new row is invisible to others until commit, so holding it during the decode blocks
        # nobody; a refused photo rolls the whole create back.
        photos.guard(current.tenant_id, request)
        stored = photos.finalize(current.tenant_id, service.id, new.photo_upload_id)
        service = out(
            current.db.execute(
                text(f"""
                UPDATE services SET photo_key = :key, photo_content_type = 'image/webp',
                  photo_width = :width, photo_height = :height
                WHERE id = :id RETURNING {FIELDS}
                """),
                {
                    "id": service.id,
                    "key": f"{current.tenant_id}/{service.id}/{new.photo_upload_id}",
                    "width": stored.width,
                    "height": stored.height,
                },
            ).one()
        )
    if not new.worker_ids:
        return service
    # A refused id is an ApiError, which rolls back the insert above too: no unbookable leftover.
    # Nobody else sees the new row yet, so locking only the members makes no cycle with PUT.
    assign_workers(current, request, service.id, new.worker_ids)
    return found(current, service.id)


@router.patch(
    "/services/{service_id}",
    name="update",
    responses={s: {"model": Error} for s in (401, 403, 404, 415, 422, 429, 503)},
)
def update_service(
    service_id: UUID,
    change: ServiceChange,
    current: CurrentOwner,
    request: Request,
    response: Response,
) -> ServiceOut:
    row = found_row(current, service_id)  # a 404 before any image work
    sent = change.model_dump(exclude_unset=True)
    swap = "photo_upload_id" in sent
    upload_id: UUID | None = sent.pop("photo_upload_id", None)
    if swap:  # the same upload again (a double Save) or no photo to remove: nothing to do
        swap = not (
            row.photo_key is None
            if upload_id is None
            else (row.photo_key or "").endswith(f"/{upload_id}")
        )
    stored = None
    if swap and upload_id is not None:
        # Before the lock, which is taken only afterwards (ZIF-133): bookings take FOR SHARE on
        # services, so a NO KEY UPDATE held across a decode would turn them into 503 busy.
        photos.guard(current.tenant_id, request)
        stored = photos.finalize(current.tenant_id, service_id, upload_id)
    # NO KEY UPDATE: two owners' changes queue here instead of one overwriting the other's, while
    # foreign key checks from bookings (KEY SHARE, ZIF-51) don't wait.
    row = found_row(current, service_id, "FOR NO KEY UPDATE")
    service = out(row)
    response.headers["Cache-Control"] = "no-store"
    # The service in the request's shape, so the two compare field by field.
    before = service.model_dump(exclude={"id": True, "price": {"currency"}, "photo": True})
    # The business's own text never goes in the log (it may name a person): only that it changed.
    changed: dict[str, object] = {
        key: "changed" if key in LOGGED_AS_CHANGED else {"old": before[key], "new": value}
        for key, value in sent.items()
        if before[key] != value
    }
    if swap and (
        row.photo_key is None
        if stored is None
        else row.photo_key == f"{current.tenant_id}/{service_id}/{upload_id}"
    ):
        swap = False  # another save already removed it, or set this very upload, while we waited
    if swap:
        changed["photo"] = (
            "removed" if stored is None else "added" if row.photo_key is None else "replaced"
        )
    if not changed:
        return service  # an empty body, or the same values: nothing written, nothing recorded
    after = before | sent
    key, width, height = row.photo_key, row.photo_width, row.photo_height
    if swap:
        key, width, height = (
            (f"{current.tenant_id}/{service_id}/{upload_id}", stored.width, stored.height)
            if stored
            else (None, None, None)
        )
    updated = current.db.execute(
        text(f"""
        UPDATE services SET
          name = CAST(:name AS jsonb), description = CAST(:description AS jsonb),
          price_amount_minor = :amount_minor, duration_minutes = :duration_minutes,
          buffer_minutes = :buffer_minutes, photo_placeholder = :placeholder,
          photo_key = :photo_key, photo_width = :photo_width, photo_height = :photo_height,
          photo_content_type = CASE WHEN CAST(:photo_key AS text) IS NULL THEN NULL
                                    ELSE 'image/webp' END,
          -- An archived service keeps the time it was first archived.
          archived_at = CASE WHEN :archived THEN coalesce(archived_at, now()) END
        WHERE id = :id
        RETURNING {FIELDS}
        """),
        {
            "id": service_id,
            "name": json.dumps(after["name"]),
            "description": json.dumps(after["description"]),
            "amount_minor": after["price"]["amount_minor"],
            "duration_minutes": after["duration_minutes"],
            "buffer_minutes": after["buffer_minutes"],
            "archived": after["archived"],
            "placeholder": after["placeholder"],
            "photo_key": key,
            "photo_width": width,
            "photo_height": height,
        },
    ).one()
    if swap and row.photo_key:
        # The unique tail: finished jobs keep their dedupe key for 30 days, and the same prefix
        # may need deleting again after a later Save.
        prefix = f"{row.photo_key}/"
        jobs.enqueue(
            current.db,
            "media.delete",
            f"media.delete:{current.tenant_id}:{prefix}:{uuid.uuid4().hex}",
            {"prefix": prefix},
            tenant_id=current.tenant_id,
        )
    auth.record(
        current.db,
        request,
        "service_changed",
        actor_user_id=current.user_id,
        target=f"service:{service_id}",
        details=changed,
    )
    return out(updated)


def lock_members(current: SignedIn, member_ids: list[UUID]) -> dict[UUID, UUID]:
    """The user behind each member id of this business, locked; ids of no current member are left
    out."""
    # One statement, in id order, like members.target, so there is no cycle with a removal or role
    # change. A member removed while we waited is simply not returned (READ COMMITTED), which is a
    # 422 in assign_workers instead of a foreign key 500 at the insert.
    return dict(
        current.db.execute(
            text("""
            SELECT id, user_id FROM memberships
            WHERE id = ANY(CAST(:ids AS uuid[])) AND role <> 'removed'
            ORDER BY id FOR NO KEY UPDATE
            """),
            {"ids": member_ids},
        ).all()
    )


@router.put(
    "/services/{service_id}/workers",
    name="replace_workers",
    responses={s: {"model": Error} for s in (401, 403, 404, 415, 422)},
)
def replace_workers(
    service_id: UUID,
    member_ids: Annotated[list[UUID], Body(max_length=MAX_WORKERS)],
    current: CurrentOwner,
    request: Request,
    response: Response,
) -> ServiceOut:
    """Set who performs a service: every member id, owners included. Members left out are
    unassigned. An archived service keeps its workers."""
    # The service first, as update_service locks it: two saves of one service queue here.
    found(current, service_id, "FOR NO KEY UPDATE")
    response.headers["Cache-Control"] = "no-store"
    assign_workers(current, request, service_id, member_ids)
    return found(current, service_id)


def assign_workers(
    current: SignedIn, request: Request, service_id: UUID, member_ids: list[UUID]
) -> None:
    """Make member_ids the service's workers and record what changed; 422 if any id is not a
    current member of this business, before anything is written. The caller holds the service."""
    ids = sorted(set(member_ids))
    # Then the members (lock_members). Arrays go as CAST(:ids AS uuid[]): psycopg sends an empty
    # list as an untyped '{}'.
    users = lock_members(current, ids)
    if len(users) != len(ids):
        raise ApiError(422, "unknown_member")  # another business's, removed, or made up
    removed = current.db.scalars(
        text("""
        DELETE FROM service_workers sw USING memberships m
        WHERE sw.service_id = :service_id AND m.id = sw.member_id
          AND sw.member_id <> ALL(CAST(:ids AS uuid[]))
        RETURNING m.user_id
        """),
        {"service_id": service_id, "ids": ids},
    ).all()
    added = current.db.scalars(
        text("""
        INSERT INTO service_workers (tenant_id, service_id, member_id)
        SELECT :tenant_id, :service_id, unnest(CAST(:ids AS uuid[]))
        ON CONFLICT DO NOTHING
        RETURNING member_id
        """),
        {"tenant_id": current.tenant_id, "service_id": service_id, "ids": ids},
    ).all()
    if added or removed:  # the same list again: nothing written, nothing recorded
        auth.record(
            current.db,
            request,
            "service_workers_changed",
            actor_user_id=current.user_id,
            target=f"service:{service_id}",
            details={
                "added": sorted(str(users[m]) for m in added),
                "removed": sorted(str(u) for u in removed),
            },
        )
