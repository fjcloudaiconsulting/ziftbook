"""The business's currency on the settings screen (ZIF-102): every member reads it, the owner
changes it until the business's first booking and before it connects Stripe (owner's ruling,
2026-10-25). The rules live in the database (change_currency, migration 0066); this module maps its
outcomes to the API. A tenants column, not a BusinessSettings key: the money foreign keys need it.
"""

from typing import Annotated, Literal, get_args

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth
from app.auth import CurrentOwner, CurrentSession
from app.currencies import Currency, minor_units
from app.errors import ApiError, Error

router = APIRouter(prefix="/api", tags=["settings"])

Lock = Literal["bookings", "payment_account"]


class CurrencyOption(BaseModel):
    code: Currency
    minor_units: int


class CurrencyState(BaseModel):
    currency: Currency
    minor_units: int
    options: list[CurrencyOption]  # the currencies a business can hold
    change: Literal["open", "locked"]
    locked_by: list[Lock]  # empty while open
    prices: int  # saved prices, archived ones included, that keep their numbers on a change


class CurrencyChange(BaseModel):
    # Field by field: nothing else a client sends is accepted.
    model_config = ConfigDict(strict=True, extra="forbid")
    currency: Currency
    # The number of saved prices the owner agreed to relabel (GET's prices); None: not confirmed.
    confirmed_prices: Annotated[int, Field(ge=0)] | None = None


STATE = text("""
SELECT t.currency,
       EXISTS (SELECT FROM bookings) AS bookings,
       EXISTS (SELECT FROM payment_accounts) AS payment_account,
       (SELECT count(*) FROM services) AS prices
FROM tenants t WHERE t.id = current_setting('app.tenant_id')::uuid
""")
REFUSED = {
    "locked_bookings": "currency_locked_bookings",
    "locked_payments": "currency_locked_payments",
}


def state(db: Session) -> CurrencyState:
    row = db.execute(STATE).one()
    held: dict[Lock, bool] = {"bookings": row.bookings, "payment_account": row.payment_account}
    locked_by = [lock for lock, is_held in held.items() if is_held]
    return CurrencyState(
        currency=row.currency,
        minor_units=minor_units(row.currency),
        options=[CurrencyOption(code=c, minor_units=minor_units(c)) for c in get_args(Currency)],
        change="locked" if locked_by else "open",
        locked_by=locked_by,
        prices=row.prices,
    )


@router.get(
    "/settings/currency", name="read_currency", responses={s: {"model": Error} for s in (401, 403)}
)
def read_currency(current: CurrentSession, response: Response) -> CurrencyState:
    response.headers["Cache-Control"] = "no-store"
    return state(current.db)


@router.put(
    "/settings/currency",
    name="change_currency",
    responses={s: {"model": Error} for s in (401, 403, 409, 415, 422)},
)
def change_currency(
    body: CurrencyChange, current: CurrentOwner, request: Request, response: Response
) -> CurrencyState:
    """Change the business's currency. Saved prices keep their numbers in the new currency, so the
    owner first confirms how many there are (a 409 currency_prices_exist says, in total)."""
    response.headers["Cache-Control"] = "no-store"
    row = current.db.execute(
        text("SELECT outcome, prices, previous FROM change_currency(:currency, :confirmed)"),
        {"currency": body.currency, "confirmed": body.confirmed_prices},
    ).one()
    if row.outcome in REFUSED:
        raise ApiError(409, REFUSED[row.outcome])
    if row.outcome == "prices_exist":
        raise ApiError(409, "currency_prices_exist", total=row.prices)
    if row.outcome == "changed":
        # As a settings change, so the settings history shows it (app/business_settings.py).
        auth.record(
            current.db,
            request,
            "setting_changed",
            actor_user_id=current.user_id,
            target="setting:currency",
            details={"old": row.previous, "new": body.currency},
        )
    return state(current.db)
