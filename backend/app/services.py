"""What a business sells. Owners add and change services; everyone in the business reads them.
Services are archived, never deleted."""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import text

from app import auth
from app.accounts import printable
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
    AfterValidator(printable),
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

# Built field by field: nothing a client sends can name a business, a currency or an id.
STRICT = ConfigDict(strict=True, extra="forbid")


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

    @field_validator("name", "description", "price", "duration_minutes", "archived", mode="before")
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


FIELDS = """
id, name, description,
jsonb_build_object('amount_minor', price_amount_minor, 'currency', price_currency) AS price,
duration_minutes, buffer_minutes, archived_at IS NOT NULL AS archived
"""

LOGGED_AS_CHANGED = frozenset({"name", "description"})  # recorded as "changed", never their text

router = APIRouter(prefix="/api", tags=["services"])


def found(current: SignedIn, service_id: UUID, lock: str = "") -> ServiceOut:
    """One service of the session's business (row-level security); 404 for any other."""
    row = current.db.execute(
        text(f"SELECT {FIELDS} FROM services WHERE id = :id {lock}"), {"id": service_id}
    ).first()
    if row is None:
        raise ApiError(404, "not_found")
    return ServiceOut.model_validate(row, from_attributes=True)


@router.get("/services", name="list", responses={401: {"model": Error}})
def list_services(current: CurrentSession, response: Response) -> list[ServiceOut]:
    """Every service of the business, archived ones included; the booking flow (ZIF-51) is what
    filters those out for new bookings.

    ponytail: no pagination, since a business has tens of services. Add before= as in audit.py if
    one ever has hundreds.
    """
    rows = current.db.execute(text(f"SELECT {FIELDS} FROM services ORDER BY id")).all()
    response.headers["Cache-Control"] = "no-store"
    return [ServiceOut.model_validate(row, from_attributes=True) for row in rows]


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
    responses={s: {"model": Error} for s in (401, 403, 415, 422)},
)
def create_service(
    new: ServiceIn, current: CurrentOwner, request: Request, response: Response
) -> ServiceOut:
    row = current.db.execute(
        text(f"""
        INSERT INTO services (tenant_id, name, description, price_amount_minor, price_currency,
                              duration_minutes, buffer_minutes)
        SELECT id, CAST(:name AS jsonb), CAST(:description AS jsonb), :amount_minor, currency,
               :duration_minutes, :buffer_minutes
        FROM tenants WHERE id = current_setting('app.tenant_id')::uuid
        RETURNING {FIELDS}
        """),
        {
            "name": json.dumps(new.name),
            "description": json.dumps(new.description),
            "amount_minor": new.price.amount_minor,
            "duration_minutes": new.duration_minutes,
            "buffer_minutes": new.buffer_minutes,
        },
    ).one()
    service = ServiceOut.model_validate(row, from_attributes=True)
    auth.record(
        current.db,
        request,
        "service_created",
        actor_user_id=current.user_id,
        target=f"service:{service.id}",
    )
    response.headers["Cache-Control"] = "no-store"
    return service


@router.patch(
    "/services/{service_id}",
    name="update",
    responses={s: {"model": Error} for s in (401, 403, 404, 415, 422)},
)
def update_service(
    service_id: UUID,
    change: ServiceChange,
    current: CurrentOwner,
    request: Request,
    response: Response,
) -> ServiceOut:
    # NO KEY UPDATE: two owners' changes queue here instead of one overwriting the other's, while
    # foreign key checks from bookings (KEY SHARE, ZIF-51) don't wait.
    service = found(current, service_id, "FOR NO KEY UPDATE")
    response.headers["Cache-Control"] = "no-store"
    # The service in the request's shape, so the two compare field by field.
    before = service.model_dump(exclude={"id": True, "price": {"currency"}})
    sent = change.model_dump(exclude_unset=True)
    # The business's own text never goes in the log (it may name a person): only that it changed.
    changed: dict[str, object] = {
        key: "changed" if key in LOGGED_AS_CHANGED else {"old": before[key], "new": value}
        for key, value in sent.items()
        if before[key] != value
    }
    if not changed:
        return service  # an empty body, or the same values: nothing written, nothing recorded
    after = before | sent
    row = current.db.execute(
        text(f"""
        UPDATE services SET
          name = CAST(:name AS jsonb), description = CAST(:description AS jsonb),
          price_amount_minor = :amount_minor, duration_minutes = :duration_minutes,
          buffer_minutes = :buffer_minutes,
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
        },
    ).one()
    auth.record(
        current.db,
        request,
        "service_changed",
        actor_user_id=current.user_id,
        target=f"service:{service_id}",
        details=changed,
    )
    return ServiceOut.model_validate(row, from_attributes=True)
