"""Thin boto3 wrapper for ZIF-133 photos. The only module importing boto3."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import StorageSettings

if TYPE_CHECKING:  # stubs from the dev group, absent from the prod image (annotations are lazy)
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

CACHE_CONTROL = "public, max-age=31536000, s-maxage=86400, immutable"
MISSING = {"404", "NoSuchKey", "NotFound"}
CHUNK = 64 * 1024  # read_into's step: the upload never sits in memory whole


class TooLarge(Exception):
    """The uploaded object is larger than the caller's limit."""


class StorageError(Exception):
    """The store failed (unreachable, timed out, refused us). Carries the error's class only: a
    botocore message can quote the URL or the key."""


@contextmanager
def _failures() -> Iterator[None]:
    try:
        yield
    except (BotoCoreError, ClientError) as error:
        raise StorageError(type(error).__name__) from None


def client(*, public: bool = False) -> S3Client:
    settings = StorageSettings()
    endpoint = (public and settings.s3_public_endpoint) or settings.s3_endpoint
    # A Session of its own: the default one is shared by every thread and is not thread-safe.
    return boto3.session.Session().client(
        "s3",
        endpoint_url=endpoint,
        region_name="auto",
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            # Browser presigned PUTs and S3-compatibles break on boto3's default CRC checksums.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            # Bounded waits: a stalled store must not pin a request or a job thread.
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 2},
        ),
    )


def presign_put(key: str, content_type: str, size: int) -> tuple[str, dict[str, str]]:
    """A 5-minute PUT URL on the uploads bucket. Content-Type and Content-Length are signed, and
    nothing else (R2's CORS allows only PUT and content-type); the browser sets Content-Length
    itself, so the returned headers hold only Content-Type."""
    url = client(public=True).generate_presigned_url(
        "put_object",
        Params={
            "Bucket": StorageSettings().s3_uploads_bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": size,
        },
        ExpiresIn=300,
    )
    return url, {"Content-Type": content_type}


def size(key: str) -> int | None:
    """The uploaded object's size, None if there is none."""
    with _failures():
        try:
            head = client().head_object(Bucket=StorageSettings().s3_uploads_bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in MISSING:
                return None
            raise
    return head["ContentLength"]


def read_into(key: str, sink: IO[bytes], limit: int) -> int | None:
    """Copy the uploaded object into sink and return its size: None if there is none, TooLarge
    once it passes limit. The cap holds whatever the HEAD before it said (ZIF-230): the GET asks
    for limit + 1 bytes at most, the store's total refuses a larger object before a byte is read,
    and the read itself stops one byte past limit whatever length the response claims."""
    with _failures():
        try:
            got = client().get_object(
                Bucket=StorageSettings().s3_uploads_bucket, Key=key, Range=f"bytes=0-{limit}"
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in MISSING:
                return None
            if code == "InvalidRange":  # an empty object has no byte 0
                return 0
            raise
        body = got["Body"]
        try:
            total = re.fullmatch(r"bytes \d+-\d+/(\d+)", got.get("ContentRange") or "")
            if int(total.group(1) if total else got.get("ContentLength") or 0) > limit:
                raise TooLarge
            n = 0
            while chunk := body.read(min(CHUNK, limit + 1 - n)):
                n += len(chunk)
                if n > limit:
                    raise TooLarge
                sink.write(chunk)
            return n
        finally:
            body.close()


def put_media(key: str, body: bytes) -> None:
    with _failures():
        client().put_object(
            Bucket=StorageSettings().s3_media_bucket,
            Key=key,
            Body=body,
            ContentType="image/webp",
            CacheControl=CACHE_CONTROL,
        )


def delete_prefix(prefix: str) -> None:
    """Every media object under the prefix; none is success."""
    bucket = StorageSettings().s3_media_bucket
    s3 = client()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys: list[ObjectIdentifierTypeDef] = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:  # a page holds at most 1000 keys, delete_objects' own cap
            refused = s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            if refused.get("Errors"):  # a 200 can still carry per-object failures
                raise RuntimeError(f"media delete failed for {len(refused['Errors'])} objects")
