from pydantic import BaseModel


class Error(BaseModel):
    """Every API error body: a stable code the web app translates, never prose."""

    code: str
    # Only the opening-hours refusal sets it (ZIF-105, outside_opening_hours): the ISO weekday the
    # console names in one translated message with a {weekday} placeholder. The handler omits the
    # key entirely when it is unset, so every other error body stays byte-identical. On the shared
    # model, not a per-route one: tests/test_openapi.py:34 requires every 4xx and 5xx body in the
    # contract to be exactly Error.
    weekday: int | None = None


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, weekday: int | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.weekday = weekday
