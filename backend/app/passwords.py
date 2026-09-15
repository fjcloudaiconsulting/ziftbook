"""Password hashing (argon2id), and the rules for the emails and passwords people type."""

import re
import secrets
import threading
import unicodedata
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from pydantic import AfterValidator

from app.errors import ApiError

MAX_PASSWORD = 256

# 19 MiB and two passes (OWASP's minimum for argon2id): the library default of 64 MiB per hash
# would run a small pod out of memory under a handful of concurrent sign-ins.
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
# What an unknown account is checked against, so it takes as long as a wrong password.
DUMMY = _hasher.hash(secrets.token_urlsafe())
# At most four hashes at once, and nobody waits for a slot: waiting would park request threads
# until the whole API stalls, so a full house answers 503 straight away.
# ponytail: about 190 verifies a second in total. Someone with an IPv6 /48 (65,536 /64 keys) can
# keep all four busy and turn sign-in into 503 for everyone; add edge rate limiting before launch.
_slots = threading.BoundedSemaphore(4)

# One address, no display names or lists: characters that parse into something else are refused.
_EMAIL = re.compile(
    r'[^@\s<>(),;:"\\\x00-\x1f]+@[^@\s<>(),;:"\\\x00-\x1f]+\.[^@\s<>(),;:"\\\x00-\x1f]+'
)


def normalise_email(email: str) -> str:
    """The stored form of an email, or ValueError."""
    email = unicodedata.normalize("NFC", email.strip().lower())
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        raise ValueError("not an email address")
    try:
        email.encode()
    except UnicodeEncodeError:
        raise ValueError("not an email address") from None
    return email


# An email field in a request body: normalised, or a 422.
Email = Annotated[str, AfterValidator(normalise_email)]


def normalise_password(password: str) -> str:
    """NFC, never stripped: a password manager's decomposed accents still match."""
    return unicodedata.normalize("NFC", password)


def _take_slot() -> None:
    if not _slots.acquire(blocking=False):
        raise ApiError(503, "busy")


def hash_password(password: str) -> str:
    _take_slot()
    try:
        return _hasher.hash(normalise_password(password))
    finally:
        _slots.release()


def _verify_hash(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except VerificationError, InvalidHashError:
        return False


def verify(stored: str | None, password: str) -> bool:
    """Whether password matches the stored hash. Hashes once even with nothing stored, so an unknown
    account takes as long to refuse as a wrong password."""
    _take_slot()
    try:
        matches = _verify_hash(stored or DUMMY, normalise_password(password))
    finally:
        _slots.release()
    return matches and stored is not None
