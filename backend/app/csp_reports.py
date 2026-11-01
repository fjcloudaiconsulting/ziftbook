"""POST /api/csp-reports: browsers' Content-Security-Policy violation reports (ZIF-236).

The pages name this route in their policy (frontend/lib/csp.ts, ZIF_CSP_REPORT_URL), so violations
show before the policy is enforced. No session: browsers send these themselves. Every request it
accepts is answered 204 with no body, whatever happened to it: Chrome retries any other answer,
and a prober learns nothing.

A report quotes the page's URL (a checkout's carries Stripe's client secret in its query), the
page's referrer, the policy with its nonce, the user agent and up to 40 characters of the offending
script or string. None of that is ever read. A logged violation holds six fields, each from a closed
set: the page as its route template, a blocked or source URL as a category, never a host.
"""

import ipaddress
import json
import logging
import re
import time
from collections import Counter
from datetime import timedelta
from typing import Any, NamedTuple, get_args
from urllib.parse import SplitResult, urlsplit

from fastapi import APIRouter, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app import limits, logs, tracing
from app.booking_page import SLUG
from app.business_settings import Locale
from app.config import MailSettings, StorageSettings
from app.errors import ApiError, Error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["csp"])

PATH = "/api/csp-reports"
LEGACY = "application/csp-report"  # report-uri: one report
BATCH = "application/reports+json"  # report-to (the Reporting API): an array of reports
MEDIA_TYPES = frozenset({LEGACY, BATCH})
FORMATS = {LEGACY: "csp-report", BATCH: "reports+json"}

# Per address (IPv6 per /48, as the authentication routes: a /48 holder could rotate 65,536 /64s).
LIMIT, LIMIT_WINDOW = 30, timedelta(minutes=1)
# Violations read from one request; the rest are counted, never read.
MAX_REPORTS = 10
# Limiter calls running at once, process-wide (one API process per replica). Each holds one of the
# 15 pooled connections and one of the 40 worker threads (main.py, ZB-16): reports never hold more
# than this many, and one arriving while they are all taken is dropped, never queued.
MAX_IN_FLIGHT = 2
# `csp violation` lines a minute, process-wide: a flood must not rotate away the lines an incident
# needs.
LINE_BUDGET = 600
# At most one `csp reports dropped` line this often, process-wide, with each reason's count since
# the last one.
DROP_EVERY = 60.0  # seconds

# The route template of every page under frontend/app/[locale] (route groups flattened, Next's own
# segment names). scripts/check-csp-pages.test.mjs fails when this and the app's pages differ.
PAGES = (
    "/[locale]",
    "/[locale]/admin",
    "/[locale]/admin/sign-in",
    "/[locale]/booking",
    "/[locale]/calendar",
    "/[locale]/clients",
    "/[locale]/forgot-password",
    "/[locale]/invite",
    "/[locale]/my-hours",
    "/[locale]/my-hours/time-off",
    "/[locale]/opening-hours",
    "/[locale]/privacy",
    "/[locale]/reset-password",
    "/[locale]/services",
    "/[locale]/services/new",
    "/[locale]/services/[serviceId]",
    "/[locale]/settings",
    "/[locale]/setup",
    "/[locale]/sign-in",
    "/[locale]/sign-up",
    "/[locale]/sign-up/complete",
    "/[locale]/team",
    "/[locale]/team/[memberId]",
    "/[locale]/team/[memberId]/time-off",
    "/[locale]/terms",
    "/[locale]/[slug]",
    "/[locale]/[slug]/book",
)
_TEMPLATES = [tuple(template.split("/")[2:]) for template in PAGES]
# A first segment some page spells out is never a business's slug (as Next routes it).
_LITERAL_FIRST = {t[0] for t in _TEMPLATES if t and not t[0].startswith("[")}
_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_LOCALES = frozenset(get_args(Locale))
MAX_VALUE = 4096  # longer strings are not read at all

DIRECTIVES = frozenset(
    {
        "base-uri",
        "block-all-mixed-content",
        "child-src",
        "connect-src",
        "default-src",
        "fenced-frame-src",
        "font-src",
        "form-action",
        "frame-ancestors",
        "frame-src",
        "img-src",
        "manifest-src",
        "media-src",
        "object-src",
        "require-trusted-types-for",
        "sandbox",
        "script-src",
        "script-src-attr",
        "script-src-elem",
        "style-src",
        "style-src-attr",
        "style-src-elem",
        "trusted-types",
        "upgrade-insecure-requests",
        "worker-src",
    }
)
DISPOSITIONS = frozenset({"enforce", "report"})
# What browsers write in place of a URL, and the schemes reported by name alone.
KEYWORDS = frozenset(
    {"inline", "eval", "wasm-eval", "trusted-types-policy", "trusted-types-sink", "self"}
)
SCHEMES = frozenset({"data", "blob", "filesystem", "about"})
EXTENSIONS = frozenset(
    {
        "chrome-extension",
        "moz-extension",
        "safari-web-extension",
        "safari-extension",
        "ms-browser-extension",
    }
)
# The third parties the pages use, by registrable domain. A new one is a code change, never data.
PROVIDERS = {
    "stripe.com": "stripe",
    "stripe.network": "stripe",
    "stripecdn.com": "stripe",
    "link.com": "link",
    "cloudflare.com": "cloudflare",
    "hcaptcha.com": "hcaptcha",
}
CATEGORIES = frozenset(
    KEYWORDS
    | SCHEMES
    | set(PROVIDERS.values())
    | {"media", "upload", "extension", "external", "ip-address", "other", "none"}
)
# Every value a `csp violation` line can carry, per field (tests check hostile input against it).
CLOSED_VALUES: dict[str, frozenset[str]] = {
    "report_format": frozenset(FORMATS.values()),
    "directive": DIRECTIVES | {"other"},
    "disposition": DISPOSITIONS | {"unknown"},
    "page": frozenset(PAGES) | {"other"},
    "blocked": CATEGORIES,
    "source": CATEGORIES,
}
DROP_REASONS = (
    "busy",
    "cross_site",
    "rate_limited",
    "limiter_unavailable",
    "foreign",
    "batch_cap",
    "global_cap",
)
_WEB = {"http": ("http", 80), "https": ("https", 443), "ws": ("http", 80), "wss": ("https", 443)}

Origin = tuple[str, str, int]


class Known(NamedTuple):
    """The origins a URL is compared with: the app's own, the media and upload stores'."""

    app: Origin | None
    media: Origin | None
    upload: Origin | None


class Violation(NamedTuple):
    report_format: str
    directive: str
    disposition: str
    page: str
    blocked: str
    source: str


def media_type(content_type: str | None) -> str:
    """The media type as every check here reads it, json_only included: the first `;` token, lower
    case. "text/plain; application/json" is text/plain."""
    return (content_type or "").split(";")[0].strip().lower()


def _split(value: object) -> SplitResult | None:
    if not isinstance(value, str) or len(value) > MAX_VALUE:
        return None
    try:
        parts = urlsplit(value.strip())
        parts.port  # noqa: B018 - raises ValueError for a port that is not one
    except ValueError:
        return None
    return parts


def _origin(parts: SplitResult) -> Origin | None:
    web = _WEB.get(parts.scheme)
    if web is None or not parts.hostname:
        return None
    return web[0], parts.hostname, parts.port or web[1]


def _origin_of(value: str) -> Origin | None:
    parts = _split(value)
    return _origin(parts) if parts else None


def known_origins() -> Known:
    """Read per request, like every setting the API reads at call time."""
    storage = StorageSettings()
    return Known(
        app=_origin_of(MailSettings().app_url),
        media=_origin_of(storage.media_url),
        # The store the browser uploads to: the web app's choice too (proxy.ts).
        upload=_origin_of(storage.s3_public_endpoint or storage.s3_endpoint),
    )


def _segment_matches(segment: str, pattern: str) -> bool:
    if pattern == "[slug]":
        return (
            segment.isascii()
            and len(segment) <= 40
            and segment.lower() not in _LITERAL_FIRST
            and SLUG.fullmatch(segment.lower()) is not None
        )
    if pattern.startswith("["):
        return _ID.fullmatch(segment) is not None
    return segment == pattern


def page_template(value: object, known: Known) -> str:
    """A page URL as its route template, "foreign" on another origin, else "other". Reads the path
    only: never the query, the fragment or a path segment's value."""
    parts = _split(value)
    origin = _origin(parts) if parts else None
    if parts is None or origin is None:
        return "other"
    if origin != known.app:
        return "foreign"
    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments or segments[0] not in _LOCALES:
        return "other"
    rest = segments[1:]
    matches = [
        template
        for template in _TEMPLATES
        if len(template) == len(rest) and all(map(_segment_matches, rest, template, strict=True))
    ]
    if not matches:
        return "other"
    # As Next routes: a spelled-out segment wins over a dynamic one (/services/new).
    best = min(matches, key=lambda t: sum(segment.startswith("[") for segment in t))
    return "/".join(("", "[locale]", *best))


def _provider(host: str) -> str | None:
    host = host.removesuffix(".")
    for domain, provider in PROVIDERS.items():
        if host == domain or host.endswith("." + domain):
            return provider
    return None


def resource_class(value: object, known: Known) -> str:
    """A blocked or source URL as a closed category, never its host, path or scheme's text."""
    if value is None or value == "":
        return "none"
    if not isinstance(value, str) or len(value) > MAX_VALUE:
        return "other"
    word = value.strip().lower()
    if word in KEYWORDS or word in SCHEMES:
        return word
    parts = _split(value)
    if parts is None:
        return "other"
    if parts.scheme in SCHEMES:
        return parts.scheme
    if parts.scheme in EXTENSIONS:
        return "extension"
    origin = _origin(parts)
    if origin is None:
        return "other"
    if origin == known.app:
        return "self"
    if origin == known.media:
        return "media"
    if origin == known.upload:
        return "upload"
    try:
        ipaddress.ip_address(origin[1])
        return "ip-address"
    except ValueError:
        pass
    return _provider(origin[1]) or "external"


def _directive(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            name = value.split()[0].lower() if len(value) <= MAX_VALUE else ""
            return name if name in DIRECTIVES else "other"
    return "other"


def _disposition(value: object) -> str:
    return value if isinstance(value, str) and value in DISPOSITIONS else "unknown"


def _reports(media: str, body: bytes) -> list[dict[str, Any]]:
    """The reports a body holds, in one shape (the legacy field names), or none when it is not one
    of the two formats. Never raises on what a client sends."""
    try:
        data = json.loads(body)
    except ValueError, RecursionError:
        return []
    if media == LEGACY:
        report = data.get("csp-report") if isinstance(data, dict) else None
        return [report] if isinstance(report, dict) else []
    if not isinstance(data, list):
        return []
    reports = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("type") != "csp-violation":
            continue
        fields = entry.get("body")
        if not isinstance(fields, dict):
            continue
        reports.append(
            {
                "document-uri": fields.get("documentURL", entry.get("url")),
                "effective-directive": fields.get("effectiveDirective"),
                "disposition": fields.get("disposition"),
                "blocked-uri": fields.get("blockedURL"),
                "source-file": fields.get("sourceFile"),
            }
        )
    return reports


def _violation(report_format: str, report: dict[str, Any], known: Known) -> Violation:
    return Violation(
        report_format=report_format,
        directive=_directive(report.get("effective-directive"), report.get("violated-directive")),
        disposition=_disposition(report.get("disposition")),
        page=page_template(report.get("document-uri"), known),
        blocked=resource_class(report.get("blocked-uri"), known),
        source=resource_class(report.get("source-file"), known),
    )


# Process-wide state, touched only on the event loop (the handler is async; only the limiter call
# runs on a thread), so no lock is needed.
_now = time.monotonic
_in_flight = 0
_drops: Counter[str] = Counter()
_drop_error: str | None = None
_drops_logged_at: float | None = None
_lines_since: float | None = None
_lines = 0


def reset() -> None:
    """Back to a fresh process: for tests."""
    global _in_flight, _drop_error, _drops_logged_at, _lines_since, _lines
    _in_flight = 0
    _drops.clear()
    _drop_error = None
    _drops_logged_at = None
    _lines_since = None
    _lines = 0


def _flush_drops() -> None:
    """One `csp reports dropped` line, at most every DROP_EVERY seconds, with each reason's count
    since the last one. WARNING when the limiter could not count (a database problem)."""
    global _drop_error, _drops_logged_at
    now = _now()
    if not _drops or (_drops_logged_at is not None and now - _drops_logged_at < DROP_EVERY):
        return
    level = logging.WARNING if _drops["limiter_unavailable"] else logging.INFO
    extra: dict[str, Any] = {reason: _drops[reason] for reason in DROP_REASONS if _drops[reason]}
    if _drop_error:
        extra["error"] = _drop_error
    logger.log(level, "csp reports dropped", extra=extra)
    _drops.clear()
    _drop_error = None
    _drops_logged_at = now


def _drop(reason: str, count: int = 1) -> Response:
    _drops[reason] += count
    _flush_drops()
    return Response(status_code=204)


def _spend_line() -> bool:
    """One line of this minute's LINE_BUDGET, if any is left."""
    global _lines_since, _lines
    now = _now()
    if _lines_since is None or now - _lines_since >= DROP_EVERY:
        _lines_since, _lines = now, 0
    if _lines >= LINE_BUDGET:
        return False
    _lines += 1
    return True


@router.post(
    "/csp-reports",
    name="report",
    status_code=204,
    response_class=Response,
    responses={415: {"model": Error}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                LEGACY: {"schema": {"type": "object"}},
                BATCH: {"schema": {"type": "array", "items": {"type": "object"}}},
            },
        }
    },
)
async def report(request: Request) -> Response:
    """A browser's violation reports, either format. 204 for everything it accepts."""
    global _in_flight, _drop_error
    media = media_type(request.headers.get("content-type"))
    # json_only lets only the two report types and application/json through to here.
    if media not in MEDIA_TYPES:
        raise ApiError(415, "unsupported_media_type")
    # Another site's page can name this URL in its own policy and have its visitors' browsers send
    # reports here (report delivery needs no CORS preflight). Dropped before any database work. A
    # request without the header (an old browser, curl) goes on to the limiter.
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() != "same-origin":
        return _drop("cross_site")
    # Built before the slot is taken: nothing between the take and the try may raise.
    ip = request.client.host if request.client else None
    key = limits.ip_key("csp_report", ip, v6_prefix=limits.AUTH_V6_PREFIX)
    # Checked and taken with no await in between, on the event loop: never more than MAX_IN_FLIGHT.
    if _in_flight >= MAX_IN_FLIGHT:
        return _drop("busy")
    _in_flight += 1
    try:
        hits = await run_in_threadpool(limits.charge, key, LIMIT_WINDOW)
    except SQLAlchemyError as error:
        # A database that cannot count: no violation is logged (no limit, no line), and the 204
        # keeps browsers from retrying into it. One line a minute, not one a report.
        _drop_error = logs.error_summary(error)
        return _drop("limiter_unavailable")
    finally:
        _in_flight -= 1
    if hits > LIMIT:
        return _drop("rate_limited")

    body = await request.body()  # capped while read: PathBodyLimit, MAX_BODY
    reports = _reports(media, body)
    if len(reports) > MAX_REPORTS:
        _drops["batch_cap"] += len(reports) - MAX_REPORTS
    known = known_origins()
    found = Counter(_violation(FORMATS[media], r, known) for r in reports[:MAX_REPORTS])
    foreign = sum(count for v, count in found.items() if v.page == "foreign")
    if foreign:
        # By construction another origin's pages: counted, never logged one by one.
        _drops["foreign"] += foreign
    for violation, count in sorted(found.items()):
        if violation.page == "foreign":
            continue
        # Counted whether or not the line budget lets it be logged.
        tracing.CSP_VIOLATIONS.add(count, {f"csp.{k}": v for k, v in violation._asdict().items()})
        if not _spend_line():
            _drops["global_cap"] += count
            continue
        logger.info("csp violation", extra={**violation._asdict(), "count": count})
    _flush_drops()
    return Response(status_code=204)
