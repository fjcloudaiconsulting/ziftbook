from pydantic import BaseModel


class Error(BaseModel):
    """Every API error body: a stable code the web app translates, never prose."""

    code: str


class ApiError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
