"""ZIF-236: POST /api/csp-reports, browsers' Content-Security-Policy violation reports.

Every field the endpoint logs comes from a closed set: the page is a route template, never a path,
query or fragment; a blocked or source URL is a category, never its host.
"""

import asyncio
import contextlib
import io
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal

import httpx2 as httpx
import pytest
import sqlalchemy.exc
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import Engine, text

from app import csp_reports, limits, logs
from app.db import SessionLocal
from app.main import MAX_BODY, create_app
from tests.conftest import fresh_address, new_client, points

Lines = Callable[[], list[dict[str, Any]]]
APP = "http://localhost:3000"  # ZIF_APP_URL's default, the origin "self" means here
LEGACY = {"content-type": "application/csp-report"}
BATCH = {"content-type": "application/reports+json"}
ALLOWLIST = {"report_format", "directive", "disposition", "page", "blocked", "source", "count"}
CONTEXT = {"ts", "level", "logger", "msg", "request_id", "client_request_id", "trace_id", "span_id"}


@pytest.fixture(autouse=True)
def _fresh_state(bound: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The process-wide budgets start empty, and the app's origin is fixed, in every test."""
    monkeypatch.setenv("ZIF_APP_URL", APP)
    monkeypatch.delenv("ZIF_MEDIA_URL", raising=False)
    monkeypatch.delenv("ZIF_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("ZIF_S3_PUBLIC_ENDPOINT", raising=False)
    csp_reports.reset()
    yield
    csp_reports.reset()


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return new_client(app)


def legacy(**fields: Any) -> bytes:
    report = {
        "document-uri": f"{APP}/en/sign-in",
        "referrer": "",
        "violated-directive": "script-src-elem",
        "effective-directive": "script-src-elem",
        "original-policy": "script-src 'nonce-abc'",
        "disposition": "report",
        "blocked-uri": "inline",
        "line-number": 1,
        "source-file": f"{APP}/en/sign-in",
        "status-code": 200,
        "script-sample": "",
    }
    report.update(fields)
    return json.dumps({"csp-report": report}).encode()


def entry(**fields: Any) -> dict[str, Any]:
    body = {
        "documentURL": f"{APP}/en/sign-in",
        "referrer": "",
        "blockedURL": "inline",
        "effectiveDirective": "style-src-attr",
        "originalPolicy": "style-src 'self'",
        "sourceFile": f"{APP}/en/sign-in",
        "sample": "",
        "disposition": "report",
        "statusCode": 200,
        "lineNumber": 3,
    }
    body.update(fields)
    return {
        "type": "csp-violation",
        "age": 10,
        "url": body["documentURL"],
        "user_agent": "zif-agent/1",
        "body": body,
    }


def batch(*entries: Any) -> bytes:
    return json.dumps(list(entries)).encode()


def violations(lines: Lines) -> list[dict[str, Any]]:
    return [line for line in lines() if line["msg"] == "csp violation"]


def dropped(lines: Lines) -> list[dict[str, Any]]:
    return [line for line in lines() if line["msg"] == "csp reports dropped"]


def post(client: TestClient, body: bytes, headers: dict[str, str] = LEGACY) -> httpx.Response:
    return client.post("/api/csp-reports", content=body, headers=headers)


def hits(migrate_engine: Engine, key: str) -> int | None:
    with migrate_engine.connect() as conn:
        found: int | None = conn.scalar(
            text("SELECT hits FROM rate_limits WHERE key = :k"), {"k": key}
        )
    return found


# 1. fence: the whole allowlist, nothing else.
def test_a_legacy_report_is_a_204_logged_with_only_the_allowlisted_fields(
    client: TestClient, log_lines: Lines
) -> None:
    response = post(client, legacy())

    assert response.status_code == 204
    assert response.content == b""
    [line] = violations(log_lines)
    assert set(line) - CONTEXT == ALLOWLIST
    assert line["level"] == "INFO"
    assert {k: line[k] for k in ALLOWLIST} == {
        "report_format": "csp-report",
        "directive": "script-src-elem",
        "disposition": "report",
        "page": "/[locale]/sign-in",
        "blocked": "inline",
        "source": "self",
        "count": 1,
    }


# 2.
def test_a_batch_logs_its_csp_violations_and_ignores_other_report_types(
    client: TestClient, log_lines: Lines
) -> None:
    other = {"type": "deprecation", "url": f"{APP}/en", "body": {"id": "x"}}
    body = batch(entry(), other, entry(effectiveDirective="img-src", blockedURL="data"))

    assert post(client, body, BATCH).status_code == 204

    got = sorted(
        (line["report_format"], line["directive"], line["blocked"])
        for line in violations(log_lines)
    )
    assert got == [
        ("reports+json", "img-src", "data"),
        ("reports+json", "style-src-attr", "inline"),
    ]


# 3. B4: identical violations in one request are one line with a count.
def test_identical_violations_in_one_request_are_one_line_with_a_count(
    client: TestClient, log_lines: Lines
) -> None:
    assert post(client, batch(*[entry() for _ in range(10)]), BATCH).status_code == 204

    [line] = violations(log_lines)
    assert line["count"] == 10


# 3b.
def test_a_batch_reads_at_most_ten_violations_and_counts_the_rest(
    client: TestClient, log_lines: Lines
) -> None:
    entries = [entry(effectiveDirective=d) for d in sorted(csp_reports.DIRECTIVES)[:14]]

    assert post(client, batch(*entries), BATCH).status_code == 204

    assert sum(line["count"] for line in violations(log_lines)) == 10
    [drop] = dropped(log_lines)
    assert drop["batch_cap"] == 4


# 4. fence: the page is a route template, never the path, query or fragment.
@pytest.mark.parametrize(
    ("document", "page"),
    [
        (
            f"{APP}/en/salon-ana/book?service=1&booking=2"
            "&payment_intent=pi_1&payment_intent_client_secret=pi_1_secret_Zzyzx&redirect_status=ok",
            "/[locale]/[slug]/book",
        ),
        (f"{APP}/en/booking#00000000-0000-0000-0000-000000000000.TokenZzyzx", "/[locale]/booking"),
        (f"{APP}/nl/invite#t.k", "/[locale]/invite"),
        (f"{APP}/pt/reset-password#TokenZzyzx", "/[locale]/reset-password"),
        (f"{APP}/en", "/[locale]"),
        (f"{APP}/en/", "/[locale]"),
        (f"{APP}/en/salon-ana", "/[locale]/[slug]"),
        (f"{APP}/en/Salon-Ana", "/[locale]/[slug]"),
        (
            f"{APP}/nl/services/0b6f4c2e-9b0e-4a53-9a39-3b9f1c0d8e11",
            "/[locale]/services/[serviceId]",
        ),
        (f"{APP}/en/services/new", "/[locale]/services/new"),
        (f"{APP}/en/team/abc/time-off", "/[locale]/team/[memberId]/time-off"),
        (f"{APP}/en/settings?stripe=return", "/[locale]/settings"),
        (f"{APP}/en/settings/book", "other"),
        (f"{APP}/en/a/b/c", "other"),
        (f"{APP}/salon-ana", "other"),
        (f"{APP}/de/salon-ana", "other"),
        (f"{APP}/en/booking;x=1", "other"),
        (f"{APP}/en/salon%20ana", "other"),
        ("about:srcdoc", "other"),
        ("https://localhost:99999/en", "other"),
        ("https://[::1/en", "other"),
        ("http://evil\uff0fexample/en", "other"),
        ("HTTP://LOCALHOST:3000/en/booking", "/[locale]/booking"),
        ("http://user:pass@localhost:3000/en/booking", "/[locale]/booking"),
        ("http://localhost.:3000/en", "foreign"),
        ("https://evil.example/en/booking", "foreign"),
        ("http://localhost:3001/en", "foreign"),
        (None, "other"),
        (5, "other"),
        ({"a": 1}, "other"),
        ("x" * 5000, "other"),
    ],
)
def test_the_page_is_its_route_template_never_its_path_query_or_fragment(
    document: Any, page: str
) -> None:
    assert csp_reports.page_template(document, csp_reports.known_origins()) == page


def test_the_default_https_port_is_the_same_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_APP_URL", "https://app.example")
    known = csp_reports.known_origins()

    assert (
        csp_reports.page_template("HTTPS://APP.EXAMPLE:443/en/booking", known)
        == "/[locale]/booking"
    )
    assert csp_reports.page_template("https://app.example./en", known) == "foreign"


# 5. B5 fence: a blocked or source URL is a closed category, never its host or scheme.
@pytest.mark.parametrize(
    ("value", "category"),
    [
        ("inline", "inline"),
        ("eval", "eval"),
        ("wasm-eval", "wasm-eval"),
        ("trusted-types-sink", "trusted-types-sink"),
        ("trusted-types-policy", "trusted-types-policy"),
        ("self", "self"),
        ("data", "data"),
        ("data:image/png;base64,AAAA", "data"),
        ("blob", "blob"),
        ("blob:http://localhost:3000/0b6f4c2e", "blob"),
        ("about:blank", "about"),
        (f"{APP}/_next/static/chunks/x.js?v=1#f", "self"),
        ("ws://localhost:3000/hmr", "self"),
        ("https://js.stripe.com/v3/", "stripe"),
        ("https://m.stripe.network/inner.html", "stripe"),
        ("https://b.stripecdn.com/x", "stripe"),
        ("https://link.com/x", "link"),
        ("https://app.link.com/x", "link"),
        ("https://challenges.cloudflare.com/turnstile/v0/api.js", "cloudflare"),
        ("https://newassets.hcaptcha.com/x", "hcaptcha"),
        ("https://notstripe.com/x", "external"),
        ("https://stripe.com.evil.example/x", "external"),
        ("https://" + "ab" * 32 + ".evil.example/x", "external"),
        ("https://1-2-3-4.nip.io/x", "external"),
        ("https://john-doe-example-com.x.test/", "external"),
        ("https://user:secret@evil.example/x", "external"),
        ("https://192.0.2.1/x", "ip-address"),
        ("https://[2001:db8::1]/x", "ip-address"),
        ("chrome-extension://abcdefghijklmnop/x.js", "extension"),
        ("moz-extension://0b6f4c2e/x.js", "extension"),
        ("safari-web-extension://x/y.js", "extension"),
        ("a" * 31 + "x://z", "other"),
        ("javascript:alert(1)", "other"),
        ("https://evil.example:99999/x", "other"),
        ("https://[::1/x", "other"),
        ("", "none"),
        (None, "none"),
        (5, "other"),
        (["https://js.stripe.com"], "other"),
        ("x" * 5000, "other"),
    ],
)
def test_blocked_and_source_are_a_closed_category(value: Any, category: str) -> None:
    assert csp_reports.resource_class(value, csp_reports.known_origins()) == category


def test_the_media_and_upload_origins_are_their_own_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZIF_MEDIA_URL", "https://media.example.test")
    monkeypatch.setenv("ZIF_S3_ENDPOINT", "https://store.example.test")
    known = csp_reports.known_origins()

    assert csp_reports.resource_class("https://media.example.test/a.webp", known) == "media"
    assert csp_reports.resource_class("https://store.example.test/up?X-Amz=1", known) == "upload"
    # The web app uses the public endpoint when it is set (proxy.ts), and so does this.
    monkeypatch.setenv("ZIF_S3_PUBLIC_ENDPOINT", "http://localhost:8333")
    known = csp_reports.known_origins()
    assert csp_reports.resource_class("http://localhost:8333/up", known) == "upload"
    assert csp_reports.resource_class("https://store.example.test/up", known) == "external"


# 6.
@pytest.mark.parametrize(
    ("fields", "directive"),
    [
        ({"effective-directive": "img-src"}, "img-src"),
        ({"effective-directive": "IMG-SRC"}, "img-src"),
        (
            {"effective-directive": "", "violated-directive": "script-src 'self' 'nonce-x'"},
            "script-src",
        ),
        ({"effective-directive": None, "violated-directive": "style-src-elem"}, "style-src-elem"),
        ({"effective-directive": "require-trusted-types-for"}, "require-trusted-types-for"),
        ({"effective-directive": "made-up-src"}, "other"),
        ({"effective-directive": 5, "violated-directive": None}, "other"),
    ],
)
def test_the_directive_is_from_a_closed_set(
    client: TestClient, log_lines: Lines, fields: dict[str, Any], directive: str
) -> None:
    assert post(client, legacy(**fields)).status_code == 204

    [line] = violations(log_lines)
    assert line["directive"] == directive


@pytest.mark.parametrize(
    ("value", "disposition"),
    [("enforce", "enforce"), ("report", "report"), ("x", "unknown"), (None, "unknown")],
)
def test_the_disposition_is_from_a_closed_set(
    client: TestClient, log_lines: Lines, value: Any, disposition: str
) -> None:
    assert post(client, legacy(disposition=value)).status_code == 204

    assert violations(log_lines)[0]["disposition"] == disposition


# 7. fence: the never-read fields never reach any line, the access line included.
def test_sample_referrer_policy_and_user_agent_never_reach_a_log(
    client: TestClient, log_lines: Lines
) -> None:
    secrets = [
        "Zzyzx Sample-7",
        "sample-7@example.com",
        "RefTokenZzyzx7",
        "nonce-Zzyzx7",
        "zif-agent-Zzyzx7",
        "QueryZzyzx7",
        "FragZzyzx7",
    ]
    report = legacy(
        **{
            "document-uri": f"{APP}/en/booking?q=QueryZzyzx7",
            "referrer": "https://hooks.stripe.com/3ds?token=RefTokenZzyzx7",
            "original-policy": "script-src 'nonce-Zzyzx7'",
            "script-sample": "Zzyzx Sample-7 sample-7@example.com",
            "source-file": f"{APP}/en/booking#FragZzyzx7",
        }
    )
    item = entry(
        documentURL=f"{APP}/en/invite?q=QueryZzyzx7",
        referrer="https://x.test/?t=RefTokenZzyzx7",
        sample="Zzyzx Sample-7",
        originalPolicy="nonce-Zzyzx7",
    )
    item["user_agent"] = "zif-agent-Zzyzx7"

    assert post(client, report).status_code == 204
    assert post(client, batch(item), BATCH).status_code == 204

    dumped = json.dumps(log_lines())
    assert len(violations(log_lines)) == 2
    for value in secrets:
        assert value not in dumped, value


HOSTILE: list[Any] = [
    None,
    5,
    -1.5e308,
    True,
    [],
    ["inline"],
    {"a": {"b": "c"}},
    "x" * 5000,
    "line\r\nforged",
    "a\u2028b\u2029c",
    "\ud800",
    "https://evil.example/\r\nlevel=ERROR",
    "https://" + "a" * 250 + ".example/",
    "https://evil.example\u2028.test/",
    "a" * 32 + "://x",
]
FIELDS = [
    "document-uri",
    "effective-directive",
    "violated-directive",
    "disposition",
    "blocked-uri",
    "source-file",
]
BODY_FIELDS = ["documentURL", "effectiveDirective", "disposition", "blockedURL", "sourceFile"]
CLOSED = csp_reports.CLOSED_VALUES


# 8. fence: whatever arrives, every logged value is a member of a closed set, in both formats.
@pytest.mark.parametrize("log_format", ["json", "text"])
def test_every_logged_value_is_from_a_closed_set_for_hostile_input(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, log_format: Literal["json", "text"]
) -> None:
    monkeypatch.setattr(csp_reports, "LIMIT", 10**6)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.Formatter(log_format))
    handler.addFilter(logs._add_context)
    reports = logging.getLogger("app.csp_reports")
    reports.addHandler(handler)
    previous = reports.level
    reports.setLevel(logging.DEBUG)
    try:
        client = new_client(app)
        for value in HOSTILE:
            for field in FIELDS:
                assert post(client, legacy(**{field: value})).status_code == 204
            items = [entry(**{field: value}) for field in BODY_FIELDS]
            items.append({"type": "csp-violation", "url": value, "body": {"documentURL": value}})
            for chunk in range(0, len(items), 10):
                assert post(client, batch(*items[chunk : chunk + 10]), BATCH).status_code == 204
    finally:
        reports.removeHandler(handler)
        reports.setLevel(previous)

    written = stream.getvalue().splitlines()
    found = [line for line in written if "csp violation" in line]
    assert found
    for line in written:
        assert line.isascii() and line.isprintable(), line
    for line in found:
        if log_format == "json":
            fields = json.loads(line)
        else:
            fields = dict(re.findall(r" (\w+)=(\S+)", line))
            fields["count"] = int(fields["count"])
        for key in ALLOWLIST - {"count"}:
            assert fields[key] in CLOSED[key], (key, fields[key])
        assert isinstance(fields["count"], int)


# 9.
@pytest.mark.parametrize(
    ("body", "headers"),
    [
        (b"", LEGACY),
        (b"{", LEGACY),
        (b"[" * (MAX_BODY - 1), BATCH),
        (b"{" + b'"a":' * 10000 + b"1", LEGACY),
        (b"\xff\xfe\x00\xd8", LEGACY),
        (b"1" + b"0" * 5000, LEGACY),
        (b'{"csp-report": 5}', LEGACY),
        (b'{"csp-report": [1]}', LEGACY),
        (b"[1, 2]", BATCH),
        (b'"x"', LEGACY),
        (b"null", BATCH),
        (b'[{"type": "csp-violation"}]', LEGACY),
        (b'{"type": "csp-violation", "body": {}}', BATCH),
        (b'[{"type": 5, "body": {}}, {"type": "csp-violation", "body": "x"}, "y", null]', BATCH),
    ],
)
def test_malformed_bodies_are_a_silent_204(
    client: TestClient, log_lines: Lines, body: bytes, headers: dict[str, str]
) -> None:
    response = post(client, body, headers)

    assert response.status_code == 204
    assert violations(log_lines) == []
    assert not [line for line in log_lines() if line["level"] in ("WARNING", "ERROR", "CRITICAL")]


def test_a_utf16_report_with_a_bom_still_reduces(client: TestClient, log_lines: Lines) -> None:
    body = json.loads(legacy(**{"document-uri": f"{APP}/en/booking?q=Utf16Zzyzx"}))
    encoded = json.dumps(body).encode("utf-16")

    assert post(client, encoded).status_code == 204

    [line] = violations(log_lines)
    assert line["page"] == "/[locale]/booking"
    assert "Utf16Zzyzx" not in json.dumps(log_lines())


# 10.
@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        "application/json",
    ],
)
def test_only_the_two_report_types_are_accepted(
    client: TestClient, log_lines: Lines, content_type: str | None
) -> None:
    headers = {"content-type": content_type} if content_type else {}

    response = post(client, legacy(), headers)

    assert (response.status_code, response.json()) == (415, {"code": "unsupported_media_type"})
    assert violations(log_lines) == []


@pytest.mark.parametrize(
    "content_type", ["Application/CSP-Report; charset=utf-8", "application/reports+json ; x=y"]
)
def test_report_types_with_parameters_are_accepted(client: TestClient, content_type: str) -> None:
    body = legacy() if "csp" in content_type.lower() else batch(entry())

    assert post(client, body, {"content-type": content_type}).status_code == 204


# 11. fence: the exemption is the report path's alone.
@pytest.mark.parametrize("content_type", ["application/csp-report", "application/reports+json"])
@pytest.mark.parametrize("path", ["/api/sign-up", "/api/csp-reports/", "/api/sessions"])
def test_the_report_types_pass_json_only_on_the_report_path_alone(
    client: TestClient, content_type: str, path: str
) -> None:
    response = client.post(
        path, content=b'{"email": "a@b.c"}', headers={"content-type": content_type}
    )

    assert (response.status_code, response.json()) == (415, {"code": "unsupported_media_type"})


# 12.
def test_an_oversize_report_is_a_413_declared_and_chunked(
    client: TestClient, log_lines: Lines
) -> None:
    body = legacy(**{"script-sample": "x" * MAX_BODY})

    assert post(client, body).status_code == 413

    def chunks() -> Iterator[bytes]:
        yield from (body[i : i + 4096] for i in range(0, len(body), 4096))

    response = client.post("/api/csp-reports", content=chunks(), headers=LEGACY)
    assert (response.status_code, response.json()) == (413, {"code": "content_too_large"})
    assert violations(log_lines) == []


# 13. B4: past the address's limit, a silent 204 counted in the one drop line.
def test_the_31st_report_in_a_minute_from_one_address_is_a_silent_204(
    app: FastAPI, log_lines: Lines
) -> None:
    client = new_client(app, fresh_address())
    for _ in range(csp_reports.LIMIT):
        assert post(client, legacy()).status_code == 204
    assert len(violations(log_lines)) == csp_reports.LIMIT

    assert post(client, legacy()).status_code == 204
    assert post(client, legacy()).status_code == 204

    assert len(violations(log_lines)) == csp_reports.LIMIT
    [drop] = dropped(log_lines)
    assert drop["rate_limited"] == 1
    assert drop["level"] == "INFO"


# 14. B4: IPv6 is keyed per /48, so rotating /64s inside one /48 buys nothing.
def test_two_64s_inside_one_48_share_one_budget(app: FastAPI, migrate_engine: Engine) -> None:
    first = fresh_address()
    prefix = ":".join(first.split(":")[:3])
    for _ in range(csp_reports.LIMIT):
        assert post(new_client(app, f"{prefix}:1::1"), legacy()).status_code == 204
    assert post(new_client(app, f"{prefix}:2::1"), legacy()).status_code == 204

    key = limits.ip_key("csp_report", f"{prefix}:2::1", v6_prefix=limits.AUTH_V6_PREFIX)
    assert hits(migrate_engine, key) == csp_reports.LIMIT + 1


def test_the_limit_is_per_address_not_global(app: FastAPI, log_lines: Lines) -> None:
    exhausted = new_client(app, fresh_address())
    for _ in range(csp_reports.LIMIT + 1):
        post(exhausted, legacy())
    before = len(violations(log_lines))

    assert post(new_client(app, fresh_address()), legacy()).status_code == 204
    assert len(violations(log_lines)) == before + 1


# 15. fence: the limiter counts before the body is read: malformed bodies spend the budget too.
def test_the_limiter_counts_before_the_body_is_parsed(app: FastAPI, log_lines: Lines) -> None:
    client = new_client(app, fresh_address())
    for _ in range(csp_reports.LIMIT):
        assert post(client, b"{").status_code == 204

    assert post(client, legacy()).status_code == 204
    assert violations(log_lines) == []


class _LockTimeout(Exception):
    sqlstate = "55P03"  # lock_timeout on the limiter row's lock (ZIF-114)


# What a database incident raises from the limiter: the pool's own timeout, a lost or refused
# connection, and a lock wait past lock_timeout.
LIMITER_ERRORS: list[Callable[[], Exception]] = [
    lambda: sqlalchemy.exc.TimeoutError(),
    lambda: sqlalchemy.exc.OperationalError("SELECT 1", {}, ConnectionRefusedError()),
    lambda: sqlalchemy.exc.OperationalError("SELECT 1", {}, _LockTimeout()),
]
LIMITER_ERROR_IDS = ["pool_timeout", "connection", "lock_timeout"]


# 16. B2 fence: a limiter that cannot count is a 204, never a 503, and one drop line, not one each.
@pytest.mark.parametrize("error", LIMITER_ERRORS, ids=LIMITER_ERROR_IDS)
def test_a_limiter_failure_is_a_204_and_one_warning(
    client: TestClient,
    log_lines: Lines,
    monkeypatch: pytest.MonkeyPatch,
    error: Callable[[], Exception],
) -> None:
    def broken(key: str, window: Any) -> int:
        raise error()

    monkeypatch.setattr(limits, "charge", broken)
    for _ in range(50):
        assert post(client, legacy()).status_code == 204

    assert violations(log_lines) == []
    [drop] = dropped(log_lines)
    assert (drop["level"], drop["limiter_unavailable"], drop["error"]) == (
        "WARNING",
        1,
        type(error()).__name__,
    )
    noisy = {"connection pool exhausted", "lock wait timed out", "unhandled error"}
    assert not [line for line in log_lines() if line["msg"] in noisy]


# 16a. Build review 1: a failed limiter call gives its slot back, so once the database recovers,
# reports are counted and logged again (never "busy" until a restart).
@pytest.mark.parametrize("error", LIMITER_ERRORS, ids=LIMITER_ERROR_IDS)
def test_the_slot_is_given_back_when_the_limiter_fails(
    client: TestClient,
    log_lines: Lines,
    monkeypatch: pytest.MonkeyPatch,
    error: Callable[[], Exception],
) -> None:
    failing = [True]

    def charge(key: str, window: Any) -> int:
        if failing[0]:
            raise error()
        return 1

    monkeypatch.setattr(limits, "charge", charge)
    for _ in range(csp_reports.MAX_IN_FLIGHT + 1):
        assert post(client, legacy()).status_code == 204
    failing[0] = False

    assert post(client, legacy()).status_code == 204

    assert len(violations(log_lines)) == 1
    assert csp_reports._in_flight == 0


# 16c. Build review 5: building the limiter key (it reads the pepper's settings) can fail too; that
# never keeps a slot either.
def test_a_failure_building_the_limiter_key_never_keeps_a_slot(
    client: TestClient, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = limits.ip_key
    failing = [True]

    def ip_key(*args: Any, **kwargs: Any) -> str:
        if failing[0]:
            raise RuntimeError("settings unreadable")
        return real(*args, **kwargs)

    monkeypatch.setattr(limits, "ip_key", ip_key)
    for _ in range(csp_reports.MAX_IN_FLIGHT + 1):
        assert post(client, legacy()).status_code == 500
    failing[0] = False

    assert post(client, legacy()).status_code == 204

    assert len(violations(log_lines)) == 1
    assert csp_reports._in_flight == 0


# 16b. B4: the drop line is at most one a minute, and carries what accumulated since.
def test_drops_are_one_line_a_minute_with_a_count_per_reason(
    client: TestClient, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(csp_reports, "_now", lambda: now[0])
    cross = {**LEGACY, "sec-fetch-site": "cross-site"}
    for _ in range(3):
        post(client, legacy(), cross)
    post(client, legacy(**{"document-uri": "https://evil.example/en"}))
    assert [d["cross_site"] for d in dropped(log_lines)] == [1]

    now[0] += csp_reports.DROP_EVERY
    post(client, legacy(), cross)

    [first, second] = dropped(log_lines)
    assert {k: second[k] for k in ("cross_site", "foreign")} == {"cross_site": 3, "foreign": 1}


# 16d. Build review 7: counts are written within a minute or two by a timer, not only by the next
# report, and each drop line says how many seconds it covers.
def test_drop_counts_are_written_by_the_timer_with_their_window(
    client: TestClient, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(csp_reports, "_now", lambda: now[0])
    cross = {**LEGACY, "sec-fetch-site": "cross-site"}
    post(client, legacy(), cross)  # the first drop is written at once
    now[0] += 5
    post(client, legacy(), cross)  # this one waits for the minute
    now[0] += 7
    post(client, legacy(), cross)
    [first] = dropped(log_lines)
    assert (first["cross_site"], first["seconds"]) == (1, 0)

    now[0] = 1000.0 + csp_reports.DROP_EVERY

    async def tick() -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(csp_reports.flush_drops_every(0.01), 0.2)

    asyncio.run(tick())

    [_, second] = dropped(log_lines)
    assert (second["cross_site"], second["seconds"]) == (2, csp_reports.DROP_EVERY - 5)


def test_pending_drop_counts_are_written_at_shutdown(
    client: TestClient, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(csp_reports, "_now", lambda: now[0])
    cross = {**LEGACY, "sec-fetch-site": "cross-site"}
    post(client, legacy(), cross)
    now[0] += 3
    post(client, legacy(), cross)
    now[0] += 4

    csp_reports.flush_drops_now()

    [_, last] = dropped(log_lines)
    assert (last["cross_site"], last["seconds"]) == (1, 4)
    csp_reports.flush_drops_now()  # nothing pending: no line
    assert len(dropped(log_lines)) == 2


def test_the_api_lifespan_runs_the_drop_timer_and_flushes_at_shutdown(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    async def every(interval: float = csp_reports.DROP_EVERY) -> None:
        events.append("timer started")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("timer stopped")
            raise

    monkeypatch.setattr(csp_reports, "flush_drops_every", every)
    monkeypatch.setattr(csp_reports, "flush_drops_now", lambda: events.append("flushed"))

    with TestClient(create_app()):
        assert events == ["timer started"]

    assert events == ["timer started", "timer stopped", "flushed"]


# 16f. Re-verify N1: a timer that died anyway never stops the shutdown: the final flush and the
# engine's dispose still run.
def test_a_dead_timer_never_takes_the_shutdown_down(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    async def dead(interval: float = csp_reports.DROP_EVERY) -> None:
        raise RuntimeError("timer died")

    monkeypatch.setattr(csp_reports, "flush_drops_every", dead)
    monkeypatch.setattr(csp_reports, "flush_drops_now", lambda: events.append("flushed"))
    monkeypatch.setattr(
        SessionLocal,
        "configure",
        lambda **kw: events.append("unbound" if kw.get("bind") is None else "bound"),
    )

    with TestClient(create_app()):
        pass

    assert events == ["bound", "flushed", "unbound"]


# 16e. Re-verify N1: a tick that raises is logged once, as an ERROR, and the timer keeps ticking.
def test_a_failing_tick_is_logged_once_and_the_timer_keeps_ticking(
    log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks: list[int] = []

    def failing(force: bool = False) -> None:
        ticks.append(1)
        raise RuntimeError("tick")

    monkeypatch.setattr(csp_reports, "_flush_drops", failing)

    async def run() -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(csp_reports.flush_drops_every(0.01), 0.3)

    asyncio.run(run())

    assert len(ticks) > 2
    failed = [line for line in log_lines() if line["msg"] == "csp drop timer failed"]
    assert [line["level"] for line in failed] == ["ERROR"]
    assert failed[0]["exc"][0]["type"] == "builtins.RuntimeError"


# 17. B3 fence: a report another site's page sent is dropped before any database work.
@pytest.mark.parametrize("site", ["cross-site", "same-site", "none", "CROSS-SITE", "junk"])
def test_a_report_not_from_this_origin_is_dropped_before_the_limiter(
    client: TestClient, log_lines: Lines, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    calls: list[str] = []

    def charge(key: str, window: Any) -> int:
        calls.append(key)
        return 1

    monkeypatch.setattr(limits, "charge", charge)

    response = post(client, legacy(), {**LEGACY, "sec-fetch-site": site})

    assert response.status_code == 204
    assert calls == []
    assert violations(log_lines) == []
    assert dropped(log_lines)[0]["cross_site"] == 1


@pytest.mark.parametrize("site", ["same-origin", "Same-Origin", None])
def test_a_same_origin_or_unlabelled_report_is_charged(
    client: TestClient, log_lines: Lines, monkeypatch: pytest.MonkeyPatch, site: str | None
) -> None:
    calls: list[str] = []

    def charge(key: str, window: Any) -> int:
        calls.append(key)
        return 1

    monkeypatch.setattr(limits, "charge", charge)
    headers = {**LEGACY, "sec-fetch-site": site} if site else LEGACY

    assert post(client, legacy(), headers).status_code == 204

    assert len(calls) == 1
    assert len(violations(log_lines)) == 1


# 17b. B3: violations on another origin's pages are counted, never logged one by one.
def test_a_foreign_page_is_counted_not_logged(client: TestClient, log_lines: Lines) -> None:
    body = batch(entry(documentURL="https://evil.example/en"), entry())

    assert post(client, body, BATCH).status_code == 204

    assert [line["page"] for line in violations(log_lines)] == ["/[locale]/sign-in"]
    assert dropped(log_lines)[0]["foreign"] == 1


# 18. B1 fence: at most MAX_IN_FLIGHT limiter calls at once; the rest are a 204 with no database
# work.
def test_reports_never_hold_more_than_two_connections(
    app: FastAPI, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    calls: list[str] = []

    def blocked(key: str, window: Any) -> int:
        calls.append(key)
        release.wait(10)
        return 1

    monkeypatch.setattr(limits, "charge", blocked)

    async def scenario() -> list[int]:
        transport = httpx.ASGITransport(app=app, client=(fresh_address(), 1))
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:

            async def send() -> int:
                response = await http.post("/api/csp-reports", content=legacy(), headers=LEGACY)
                return response.status_code

            held = [asyncio.create_task(send()) for _ in range(csp_reports.MAX_IN_FLIGHT)]
            deadline = time.monotonic() + 10
            while len(calls) < csp_reports.MAX_IN_FLIGHT and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            extra = await send()
            release.set()
            return [extra, *await asyncio.gather(*held)]

    statuses = asyncio.run(scenario())

    assert statuses == [204] * (csp_reports.MAX_IN_FLIGHT + 1)
    assert len(calls) == csp_reports.MAX_IN_FLIGHT
    assert dropped(log_lines)[0]["busy"] == 1
    assert len(violations(log_lines)) == csp_reports.MAX_IN_FLIGHT
    assert csp_reports._in_flight == 0


def _blocking_charge(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, list[str]]:
    release = threading.Event()
    calls: list[str] = []

    def blocked(key: str, window: Any) -> int:
        calls.append(key)
        release.wait(10)
        return 1

    monkeypatch.setattr(limits, "charge", blocked)
    return release, calls


async def _until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not condition() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


# 18b. Build review 4: a burst arriving all at once still takes at most MAX_IN_FLIGHT slots (the
# check and the take are atomic on the event loop).
def test_a_burst_never_takes_more_than_two_slots(
    app: FastAPI, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, calls = _blocking_charge(monkeypatch)
    burst = csp_reports.MAX_IN_FLIGHT + 3

    async def scenario() -> list[int]:
        transport = httpx.ASGITransport(app=app, client=(fresh_address(), 1))
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:

            async def send() -> int:
                response = await http.post("/api/csp-reports", content=legacy(), headers=LEGACY)
                return response.status_code

            tasks = [asyncio.create_task(send()) for _ in range(burst)]
            in_flight = csp_reports.MAX_IN_FLIGHT
            # The right code: two calls held, the other three already answered "busy".
            await _until(
                lambda: (
                    len(calls) >= in_flight
                    and sum(task.done() for task in tasks) >= burst - in_flight
                )
            )
            await asyncio.sleep(0.2)  # a mutant's late takers would call charge by now
            release.set()
            return list(await asyncio.gather(*tasks))

    statuses = asyncio.run(scenario())

    assert statuses == [204] * burst
    assert len(calls) == csp_reports.MAX_IN_FLIGHT
    assert csp_reports._in_flight == 0


# 18c. Build review 4: a cross-site report is dropped as cross_site even while every slot is held:
# the Sec-Fetch-Site check comes before the cap.
def test_a_cross_site_report_is_counted_as_such_while_the_slots_are_held(
    app: FastAPI, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, calls = _blocking_charge(monkeypatch)

    async def scenario() -> int:
        transport = httpx.ASGITransport(app=app, client=(fresh_address(), 1))
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
            held = [
                asyncio.create_task(http.post("/api/csp-reports", content=legacy(), headers=LEGACY))
                for _ in range(csp_reports.MAX_IN_FLIGHT)
            ]
            await _until(lambda: len(calls) >= csp_reports.MAX_IN_FLIGHT)
            cross = {**LEGACY, "sec-fetch-site": "cross-site"}
            response = await http.post("/api/csp-reports", content=legacy(), headers=cross)
            release.set()
            await asyncio.gather(*held)
            return response.status_code

    assert asyncio.run(scenario()) == 204
    first = dropped(log_lines)[0]
    assert (first.get("cross_site"), first.get("busy")) == (1, None)


# 19. B4 fence: a process-wide budget of violation lines a minute.
def test_past_the_line_budget_no_violation_is_logged_and_one_drop_line_says_so(
    app: FastAPI, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(csp_reports, "LINE_BUDGET", 3)
    directives = sorted(csp_reports.DIRECTIVES)[:6]
    for directive in directives:
        client = new_client(app, fresh_address())
        assert post(client, legacy(**{"effective-directive": directive})).status_code == 204

    assert len(violations(log_lines)) == 3
    [drop] = dropped(log_lines)
    assert drop["global_cap"] == 1


# 19b. Build review 2: the budget is a minute's, so violations are logged again the next minute.
def test_the_line_budget_is_per_minute(
    app: FastAPI, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(csp_reports, "_now", lambda: now[0])
    monkeypatch.setattr(csp_reports, "LINE_BUDGET", 1)
    for directive in ("img-src", "font-src"):
        post(new_client(app, fresh_address()), legacy(**{"effective-directive": directive}))
    assert len(violations(log_lines)) == 1

    now[0] += csp_reports.DROP_EVERY
    post(new_client(app, fresh_address()), legacy(**{"effective-directive": "media-src"}))

    assert [line["directive"] for line in violations(log_lines)] == ["img-src", "media-src"]


# 20. N6: every violation is counted, including those the line budget drops; foreign ones never.
def test_every_violation_is_counted_in_the_metric(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, metric_reader: InMemoryMetricReader
) -> None:
    monkeypatch.setattr(csp_reports, "LINE_BUDGET", 0)
    body = batch(entry(), entry(), entry(documentURL="https://evil.example/en"))

    assert post(client, body, BATCH).status_code == 204

    [point] = points(metric_reader, "ziftbook.csp.violations")
    assert point.value == 2
    assert dict(point.attributes) == {
        "csp.directive": "style-src-attr",
        "csp.disposition": "report",
        "csp.blocked": "inline",
    }


# 20a. Build review (security 1): the metric's series are bounded twice. Its three attributes allow
# at most 26 x 3 x 21 combinations; past MAX_SERIES distinct ones in a process, every violation is
# counted in one overflow series instead, whatever a client sends.
def test_the_metric_has_three_closed_attributes_and_a_bounded_product() -> None:
    product = (
        len(csp_reports.CLOSED_VALUES["directive"])
        * len(csp_reports.CLOSED_VALUES["disposition"])
        * len(csp_reports.CLOSED_VALUES["blocked"])
    )
    assert product <= 1638
    assert csp_reports.MAX_SERIES < product


def test_hostile_input_cannot_pass_the_series_cap(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, metric_reader: InMemoryMetricReader
) -> None:
    monkeypatch.setattr(csp_reports, "MAX_SERIES", 5)
    monkeypatch.setattr(csp_reports, "LIMIT", 10**6)
    client = new_client(app)
    blocked = ["inline", "eval", "data", "blob", "https://js.stripe.com/", "https://x.test/"]
    sent = 0
    for directive in sorted(csp_reports.DIRECTIVES)[:6]:
        items = [
            entry(effectiveDirective=directive, blockedURL=b, disposition=d)
            for b in blocked
            for d in ("enforce", "report")
        ]
        for chunk in range(0, len(items), 10):
            part = items[chunk : chunk + 10]
            assert post(client, batch(*part), BATCH).status_code == 204
            sent += len(part)

    found = points(metric_reader, "ziftbook.csp.violations")
    assert len(found) <= csp_reports.MAX_SERIES + 1
    assert sum(point.value for point in found) == sent
    overflow = [p for p in found if dict(p.attributes) == csp_reports.OVERFLOW]
    assert overflow and overflow[0].value == sent - sum(
        p.value for p in found if dict(p.attributes) != csp_reports.OVERFLOW
    )


# 20b. Build review 6: a client that hangs up halfway through its report is a quiet 204, never an
# ERROR line with a traceback (the route reads its body by hand, so FastAPI's own handling of a
# disconnect does not apply).
def test_a_client_that_disconnects_mid_report_is_a_204_with_no_error(
    app: FastAPI, log_lines: Lines
) -> None:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/csp-reports",
        "raw_path": b"/api/csp-reports",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/csp-report")],
        "client": (fresh_address(), 1),
        "server": ("testserver", 443),
    }
    incoming: list[dict[str, Any]] = [
        {"type": "http.request", "body": legacy()[:20], "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if incoming:
            return incoming.pop(0)
        await asyncio.sleep(3600)  # never: the app has seen the disconnect
        raise AssertionError

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))  # type: ignore[arg-type]

    starts = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert starts == [204]
    assert not [line for line in log_lines() if line["level"] in ("ERROR", "CRITICAL")]
    assert violations(log_lines) == []
    assert dropped(log_lines)[0]["disconnected"] == 1


# 21.
def test_no_session_is_needed_and_a_cookie_changes_nothing(
    client: TestClient, log_lines: Lines
) -> None:
    assert post(client, legacy()).status_code == 204
    client.cookies.set("__Host-session", "junk-Zzyzx")
    assert post(client, legacy()).status_code == 204

    assert len(violations(log_lines)) == 2
    assert "junk-Zzyzx" not in json.dumps(log_lines())


def test_other_methods_are_not_allowed(client: TestClient) -> None:
    assert client.get("/api/csp-reports").status_code == 405
