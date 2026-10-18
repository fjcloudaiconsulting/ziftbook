"""Thin boto3 wrapper for ZIF-133 photos. The only module importing boto3."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import StorageSettings

if TYPE_CHECKING:  # stubs from the dev group, absent from the prod image (annotations are lazy)
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

CACHE_CONTROL = "public, max-age=31536000, s-maxage=86400, immutable"
MISSING = {"404", "NoSuchKey", "NotFound"}


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


def read(key: str) -> bytes | None:
    """The uploaded object, None if there is none (it can vanish between size() and this)."""
    with _failures():
        try:
            return (
                client()
                .get_object(Bucket=StorageSettings().s3_uploads_bucket, Key=key)["Body"]
                .read()
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in MISSING:
                return None
            raise


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
