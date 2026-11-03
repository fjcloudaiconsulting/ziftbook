"""The erasure ledger (ZIF-58, spec section 8): erasures recorded outside the database, on moto
(never a real bucket), and re-applied after a restore."""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from mypy_boto3_s3 import S3Client

from app import ledger, storage
from app.jobs import Job
from tests.conftest import S3_ENDPOINT

BUCKET = "ziftbook-erasures"


@pytest.fixture
def ledger_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Client]:
    """The ledger bucket on moto, configured through its ZIF_* variables; yields an admin client."""
    for name, value in {
        "ZIF_S3_ENDPOINT": S3_ENDPOINT,
        "ZIF_ERASURE_LEDGER_BUCKET": BUCKET,
        "ZIF_ERASURE_LEDGER_ACCESS_KEY_ID": "dev",
        "ZIF_ERASURE_LEDGER_SECRET_ACCESS_KEY": "dev-secret",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        admin = boto3.client("s3", endpoint_url=S3_ENDPOINT, region_name="us-east-1")
        admin.create_bucket(Bucket=BUCKET)
        yield admin


def ledger_job(payload: dict[str, Any]) -> Job:
    return Job(uuid.uuid4(), "erasure.ledger", uuid.UUID(payload["tenant_id"]), payload)


def a_payload(erased_at: datetime | None = None) -> dict[str, Any]:
    return {
        "kind": "client",
        "tenant_id": str(uuid.uuid4()),
        "client_id": str(uuid.uuid4()),
        "erased_at": (erased_at or datetime.now(UTC)).isoformat(),
    }


def stored(admin: S3Client) -> dict[str, bytes]:
    listed = admin.list_objects_v2(Bucket=BUCKET).get("Contents", [])
    return {o["Key"]: admin.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read() for o in listed}


def refusing(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    """Every ledger PUT is refused with this error code, as a bucket lock refuses an overwrite."""
    real = storage._connect

    def connect(*args: Any) -> Any:
        s3 = real(*args)

        def put_object(**kwargs: Any) -> None:
            raise ClientError({"Error": {"Code": code}}, "PutObject")

        setattr(s3, "put_object", put_object)  # noqa: B010 -- mypy refuses a method assignment
        return s3

    monkeypatch.setattr(storage, "_connect", connect)


# ZIF-58 F23. FENCE. Wrong impl: a PUT that fails on an existing key (a repeat of the job, or the
# bucket lock refusing the overwrite, would fail the job and retry it into a give-up).
def test_the_entry_is_written_at_its_key_and_a_repeat_is_success(
    ledger_bucket: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = datetime(2026, 11, 3, 9, 15, 7, 123456, tzinfo=UTC)
    payload = a_payload(at)
    ledger.record(ledger_job(payload))
    key = f"erasures/2026/11/03/20261103T091507.123456Z-client-{payload['client_id']}.json"
    assert list(stored(ledger_bucket)) == [key]
    first = stored(ledger_bucket)[key]
    assert json.loads(first) == {"v": 1, **payload}

    ledger.record(ledger_job(payload))  # the job run again: already there
    # A writer that saw nothing at HEAD (a racing run), refused by the conditional PUT.
    real = storage._present
    seen: list[str] = []

    def missed_once(s3: S3Client, bucket: str, name: str) -> bool:
        seen.append(name)
        return len(seen) > 1 and real(s3, bucket, name)

    monkeypatch.setattr(storage, "_present", missed_once)
    ledger.record(ledger_job(payload))
    # And the bucket lock refusing the overwrite.
    monkeypatch.setattr(storage, "_present", missed_once)
    seen.clear()
    refusing(monkeypatch, "AccessDenied")
    ledger.record(ledger_job(payload))
    assert stored(ledger_bucket) == {key: first}


# ZIF-58 F23. FENCE. Wrong impl: a refused PUT taken as written whatever it left behind (the
# erasure would be missing from the ledger with its job done).
def test_a_refused_put_of_a_missing_entry_fails_the_job(
    ledger_bucket: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusing(monkeypatch, "AccessDenied")
    with pytest.raises(storage.StorageError):
        ledger.record(ledger_job(a_payload()))
    assert stored(ledger_bucket) == {}


# ZIF-58 F24. FENCE. Wrong impl: an entry built from the job's payload as it is (a payload that
# carries an address, a name or the trace context would put it in the ledger, which nothing ever
# rewrites).
def test_the_entry_holds_ids_and_the_time_only(ledger_bucket: S3Client) -> None:
    payload = a_payload()
    ledger.record(
        ledger_job(
            {
                **payload,
                "email": "erased-person@example.com",
                "name": "Erased Person",
                "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            }
        )
    )
    [body] = stored(ledger_bucket).values()
    assert "@" not in body.decode()
    assert json.loads(body) == {"v": 1, **payload}


# ZIF-58 F24. GUARD. A payload that is not a client erasure, or whose ids are not ids, writes
# nothing: the job fails (and gives up, an ERROR) rather than ledgering something else.
@pytest.mark.parametrize(
    "change",
    [
        {"kind": "account"},
        {"client_id": "erased-person@example.com"},
        {"erased_at": "2026-11-03T09:15:07"},
    ],
)
def test_anything_but_a_client_erasure_writes_nothing(
    ledger_bucket: S3Client, change: dict[str, str]
) -> None:
    with pytest.raises(ValueError):
        ledger.record(ledger_job({**a_payload(), **change}))
    assert stored(ledger_bucket) == {}
