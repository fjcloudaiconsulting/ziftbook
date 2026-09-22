"""Cloudflare Turnstile verification. No new dependency: stdlib urllib, as app/worker.py:6,32."""

import http.client
import json
import logging
import urllib.parse
import urllib.request

from app.config import TurnstileSettings

SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT = 3  # seconds. The endpoint runs in the threadpool (a sync `def`), so this blocks a thread.
MAX_TOKEN = 2048  # Cloudflare's documented maximum; a longer one is a forgery, not a token.

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(TurnstileSettings().turnstile_secret)


def verify(token: str | None, ip: str | None) -> bool:
    """Whether Cloudflare says this token passed. FAILS CLOSED on any verification failure and on
    any network or parse error: a visitor who cannot be verified is not admitted.

    No secret configured means verification is skipped and this returns True, so development and the
    test suite need no network. app/main.py prints that state on the `api started` line.

    EXACTLY ONE call to urlopen per request, and NEVER a retry - spec C9, and a rule rather than a
    preference. A Turnstile token is single use and lives 300 seconds, and the first call spends it
    whether or not the answer reaches us. Retrying therefore re-submits a SPENT token, which comes
    back success: false / timeout-or-duplicate, turning a transient network blip into a permanent
    rejection for that visitor. Do not wrap this in a retry helper and do not retry at the call
    site. idempotency_key exists for the retry case and is deliberately unused.
    """
    secret = TurnstileSettings().turnstile_secret
    if not secret:
        return True
    if not token or len(token) > MAX_TOKEN:
        return False
    form = {"secret": secret, "response": token}
    if ip:
        form["remoteip"] = ip
    request = urllib.request.Request(
        SITEVERIFY,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            result = json.load(answer)
    # http.client.HTTPException is listed because it is NOT an OSError: a response truncated
    # mid-body raises http.client.IncompleteRead, which subclasses HTTPException only. Without it
    # a dropped TLS connection is a 500 on the product's first unauthenticated write, not the
    # fail-closed 403 this function promises. Do not "simplify" the tuple.
    except (OSError, ValueError, http.client.HTTPException) as error:
        # The class only: an error's text can quote the secret or the address
        # (CONTRIBUTING.md:253-257, "log the class ... never %r/str(error)").
        logger.warning("turnstile unreachable", extra={"error": type(error).__name__})
        return False
    # isinstance before .get: siteverify is documented to answer an object, but `[]`, `"ok"`,
    # `null` and `123` are all valid JSON that json.load accepts and .get raises AttributeError
    # on - again a 500 rather than a refusal. Anything that is not an object is not a pass.
    return isinstance(result, dict) and result.get("success") is True
