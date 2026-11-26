"""Deterministic stand-in for the Google Calendar gateway."""

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.google_calendar_gateway import (
    ISSUERS,
    MAX_RETRY_AFTER,
    Authorization,
    Exchanged,
    Failed,
    FreeBusy,
    Gateway,
    Interval,
    Operation,
    Outcome,
    Refreshed,
    RevokeResult,
    StatusClass,
    _utc_bounds,
    _validate_exchange,
)


@dataclass
class Account:
    subject: str = field(repr=False)
    email: str = field(repr=False)
    code: str = field(repr=False)
    refresh_token: str = field(repr=False)
    busy: tuple[Interval, ...] = ()
    replacement: str | None = field(default=None, repr=False)
    revoked: bool = False


type Response = Authorization | Exchanged | Refreshed | FreeBusy | RevokeResult


class FakeGateway(Gateway):
    """Use a real open-transaction counter for transaction_open in integration fixtures.

    Wire it like Stripe's open_sessions counter so each external operation can detect
    a transaction held open by its caller.
    """

    def __init__(
        self,
        *,
        transaction_open: Callable[[], bool],
        now: datetime = datetime(2026, 1, 1, tzinfo=UTC),
    ) -> None:
        super().__init__()
        self.now = now
        self.transaction_open = transaction_open
        self.accounts: dict[str, Account] = {}
        self.calls: list[Operation] = []
        self.responses: dict[Operation, deque[Response | Failed | Callable[[], Response]]] = (
            defaultdict(deque)
        )
        self.failures: dict[Operation, tuple[int, Outcome, StatusClass, int | None]] = {}
        self._access: dict[str, tuple[Account, datetime]] = {}

    def fail_next(
        self,
        op: Operation,
        category: Outcome = Outcome.TRANSIENT,
        times: int = 1,
        *,
        status_class: StatusClass = StatusClass.NONE,
        retry_after: int | None = None,
    ) -> None:
        assert times > 0
        assert retry_after is None or 0 <= retry_after <= MAX_RETRY_AFTER
        self.failures[op] = (times, category, status_class, retry_after)

    def _enter[T: Response](self, op: Operation, expected: type[T]) -> T | None:
        if op != Operation.AUTHORIZE:
            assert not self.transaction_open(), "Google Calendar call inside a database transaction"
        self.calls.append(op)
        if op in self.failures:
            times, category, status_class, retry_after = self.failures.pop(op)
            if times > 1:
                self.failures[op] = (times - 1, category, status_class, retry_after)
            raise Failed(category, status_class, retry_after) from None
        if not self.responses[op]:
            return None
        scripted = self.responses[op].popleft()
        result = scripted() if callable(scripted) else scripted
        if isinstance(result, Failed):
            raise result from None
        assert isinstance(result, expected), "Unexpected scripted Google Calendar response"
        return result

    def _issue(self, account: Account) -> str:
        token = f"fake-access-{len(self._access) + 1}"
        self._access[token] = (account, self.now + timedelta(hours=1))
        return token

    def authorization_url(self, *, state: str, nonce: str, verifier: str) -> Authorization:
        scripted = self._enter(Operation.AUTHORIZE, Authorization)
        return scripted or Authorization("https://google-calendar.test/authorize")

    def exchange_code(self, *, code: str, verifier: str, nonce: str) -> Exchanged:
        _validate_exchange(code, verifier, nonce)
        scripted = self._enter(Operation.EXCHANGE, Exchanged)
        if scripted is not None:
            return scripted
        for account in self.accounts.values():
            if account.code == code and not account.revoked:
                return Exchanged(
                    self._issue(account),
                    3600,
                    account.refresh_token,
                    ISSUERS[0],
                    account.subject,
                    account.email,
                )
        raise Failed(Outcome.RECONNECT_REQUIRED) from None

    def _account(self, refresh_token: str) -> Account | None:
        return next(
            (
                account
                for account in self.accounts.values()
                if account.refresh_token == refresh_token and not account.revoked
            ),
            None,
        )

    def refresh(self, *, refresh_token: str) -> Refreshed:
        scripted = self._enter(Operation.REFRESH, Refreshed)
        if scripted is not None:
            return scripted
        account = self._account(refresh_token)
        if account is None:
            raise Failed(Outcome.RECONNECT_REQUIRED) from None
        replacement = account.replacement
        if replacement is not None:
            account.refresh_token = replacement
            account.replacement = None
        return Refreshed(self._issue(account), 3600, replacement)

    def freebusy(self, *, access_token: str, start: datetime, end: datetime) -> FreeBusy:
        lower, upper = _utc_bounds(start, end)
        scripted = self._enter(Operation.FREEBUSY, FreeBusy)
        if scripted is not None:
            return scripted
        grant = self._access.get(access_token)
        if grant is None or grant[0].revoked or grant[1] <= self.now:
            raise Failed(Outcome.REFRESH_REQUIRED) from None
        busy = tuple(item for item in grant[0].busy if item.start < upper and lower < item.end)
        return FreeBusy(lower, upper, busy)

    def revoke(self, *, refresh_token: str) -> RevokeResult:
        scripted = self._enter(Operation.REVOKE, RevokeResult)
        if scripted is not None:
            return scripted
        account = self._account(refresh_token)
        if account is None:
            return RevokeResult.ALREADY_INVALID
        for grant in self.accounts.values():
            if grant.subject == account.subject:
                grant.revoked = True
        return RevokeResult.ACCEPTED
