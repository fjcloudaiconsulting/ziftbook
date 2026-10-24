"""ZIF-133: the photo upload API and a service's photo at Save."""

import os
import uuid
import zlib
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mypy_boto3_s3 import S3Client
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app import auth, limits, photos, storage
from app.config import StorageSettings
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    ADMIN_DATABASE_URL,
    People,
    add_membership,
    add_user,
    events,
    failing,
    fresh_address,
    image,
    new_client,
    signed_in,
    upload,
)
from tests.test_photos import exif_of, jpeg_header, png_with_chunk
from tests.test_services import service, stored

MEDIA = "https://img.example.test"


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture
def worker(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.only_a)


def presign(client: TestClient, content_type: str = "image/png", size: int = 1234) -> Any:
    return client.post("/api/photo-uploads", json={"content_type": content_type, "size": size})


def locked(migrate_engine: Engine, tenant: uuid.UUID, service_id: str) -> bool:
    """Whether another session cannot lock the service row right now."""
    with migrate_engine.connect() as conn, conn.begin():
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        got = conn.execute(
            text("SELECT id FROM services WHERE id = :s FOR NO KEY UPDATE SKIP LOCKED"),
            {"s": service_id},
        ).first()
    return got is None


def idle_in_transaction() -> int:
    """App-role sessions of this database holding a transaction open, doing nothing. As the
    superuser: another role's session state is masked from the migrate role."""
    admin = create_engine(ADMIN_DATABASE_URL, poolclass=NullPool)
    with admin.connect() as conn:
        count: int = conn.scalar(
            text("""
            SELECT count(*) FROM pg_stat_activity
            WHERE datname = :database AND usename = 'ziftbook_app'
              AND state = 'idle in transaction'
            """),
            {"database": make_url(os.environ["ZIF_DATABASE_URL"]).database},
        )
    admin.dispose()
    return count


def jobs_of(migrate_engine: Engine, tenant: uuid.UUID) -> list[str]:
    with migrate_engine.connect() as conn:
        return list(
            conn.scalars(
                text("""
                SELECT payload->>'prefix' FROM jobs
                WHERE kind = 'media.delete' AND tenant_id = :t ORDER BY id
                """),
                {"t": tenant},
            )
        )


def photo_key(tenant: uuid.UUID, service_id: str) -> str | None:
    (row,) = [r for r in stored(tenant) if str(r["id"]) == service_id]
    key: str | None = row["photo_key"]
    return key


# ZIF-133 F7. fence. Kills: a presign that is not owner-only; a missing 503 while storage is unset;
# the type or size accepted unchecked; the key built from a client value.
def test_presign(
    s3: S3Client,
    people: People,
    owner: TestClient,
    worker: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert presign(worker).status_code == 403
    assert presign(owner, "image/heic").json() == {"code": "photo_type"}
    assert presign(owner, "image/heic").status_code == 422
    too_big = presign(owner, "image/png", 10 * 1024 * 1024 + 1)
    assert (too_big.status_code, too_big.json()) == (422, {"code": "photo_too_large"})
    assert presign(owner, "image/png", 10 * 1024 * 1024).status_code == 201

    response = presign(owner)
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["headers"] == {"Content-Type": "image/png"}
    url = urlparse(body["url"])
    assert url.path == f"/ziftbook-uploads/{people.a}/{body['upload_id']}"
    assert parse_qs(url.query)["X-Amz-SignedHeaders"] == ["content-length;content-type;host"]

    monkeypatch.delenv("ZIF_S3_ENDPOINT")
    unset = presign(owner)
    assert (unset.status_code, unset.json()) == (503, {"code": "photos_unavailable"})


# ZIF-133 F8. fence. Kills: the check looking an upload up by its id alone, so one business can
# read (and link) another's.
def test_another_business_s_upload_is_missing(
    s3: S3Client, app: FastAPI, people: People, owner: TestClient, app_engine: Engine
) -> None:
    upload_id = upload(s3, people.a, image("PNG", (10, 10)))
    user_b = add_user(app_engine)
    add_membership(people.b, user_b, "owner")
    owner_b = signed_in(app, people.b, user_b)

    checked = owner_b.post(f"/api/photo-uploads/{upload_id}/check", json={})
    assert (checked.status_code, checked.json()) == (409, {"code": "photo_missing"})
    saved = owner_b.post(
        "/api/services",
        json={
            "name": {"en": "Cut"},
            "price": {"amount_minor": 1},
            "duration_minutes": 30,
            "photo_upload_id": str(upload_id),
        },
    )
    assert (saved.status_code, saved.json()) == (409, {"code": "photo_missing"})
    assert stored(people.b) == []
    mine = owner.post(f"/api/photo-uploads/{upload_id}/check", json={})
    assert (mine.status_code, mine.json()) == (200, {"width": 10, "height": 10})
    assert mine.headers["cache-control"] == "no-store"


# ZIF-133 F9. fence. Kills: an unreachable upload (a real HEAD miss is a bare 404) answering 500.
def test_the_check_of_an_upload_that_never_arrived_is_409(s3: S3Client, owner: TestClient) -> None:
    missing = owner.post(f"/api/photo-uploads/{uuid.uuid4()}/check", json={})
    assert (missing.status_code, missing.json()) == (409, {"code": "photo_missing"})
    assert owner.post("/api/photo-uploads/not-a-uuid/check", json={}).status_code == 422
    # A write like any other: only application/json (the CSRF stance of every write route).
    assert owner.post(f"/api/photo-uploads/{uuid.uuid4()}/check").status_code == 415


# ZIF-133 F7. fence. Kills: the check open to any member, or answering while storage is unset.
def test_check_is_owner_only_and_needs_storage(
    s3: S3Client,
    people: People,
    worker: TestClient,
    owner: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_id = upload(s3, people.a, image("PNG", (10, 10)))
    assert worker.post(f"/api/photo-uploads/{upload_id}/check", json={}).status_code == 403
    assert (
        new_client(create_app()).post(f"/api/photo-uploads/{upload_id}/check", json={}).status_code
        == 401
    )
    monkeypatch.delenv("ZIF_S3_ENDPOINT")
    assert owner.post(f"/api/photo-uploads/{upload_id}/check", json={}).status_code == 503


# ZB-33 Review Focus 5. guard: pins the code a lossless JPEG, an animated PNG and a big eXIf
# answer, photo_unreadable, the one the console already explains.
@pytest.mark.parametrize(
    "data",
    [
        jpeg_header(9600, 16, 1, 1, sof=0xC3),
        png_with_chunk(b"acTL", b"\x00\x00\x00\x02\x00\x00\x00\x00"),
        png_with_chunk(b"eXIf", b"Exif\x00\x00" * 11_000),
    ],
    ids=["lossless-jpeg", "animated-png", "big-exif"],
)
def test_a_container_refused_before_decoding_is_photo_unreadable(
    s3: S3Client, people: People, owner: TestClient, data: bytes
) -> None:
    upload_id = upload(s3, people.a, data)
    checked = owner.post(f"/api/photo-uploads/{upload_id}/check", json={})
    assert (checked.status_code, checked.json()) == (422, {"code": "photo_unreadable"})


# ZB-33 fence. Kills: EXIF Pillow cannot parse answering 500. A PNG zTXt or iTXt chunk named exif
# hands Pillow text where it expects bytes.
@pytest.mark.parametrize(
    "data",
    [
        png_with_chunk(b"zTXt", b"exif\0\0" + zlib.compress(exif_of(6))),
        png_with_chunk(b"iTXt", b"exif\0\0\0\0\0" + exif_of(6)),
    ],
    ids=["ztxt", "itxt"],
)
def test_exif_pillow_cannot_parse_is_photo_unreadable(
    s3: S3Client, people: People, owner: TestClient, data: bytes
) -> None:
    upload_id = upload(s3, people.a, data)
    checked = owner.post(f"/api/photo-uploads/{upload_id}/check", json={})
    assert (checked.status_code, checked.json()) == (422, {"code": "photo_unreadable"})


# ZIF-133 F7. fence. Kills: a rate limit missing from the presign, the check or a Save carrying a
# photo (100 an hour per business, one shared key).
def test_all_three_share_one_hourly_limit(s3: S3Client, people: People, owner: TestClient) -> None:
    service_id = service(owner).json()["id"]
    for _ in range(100):
        assert not limits.hit({f"photo:{people.a}": 100}, timedelta(hours=1))
    upload_id = upload(s3, people.a, image("PNG", (10, 10)))
    limited = {"code": "rate_limited"}
    assert (presign(owner).status_code, presign(owner).json()) == (429, limited)
    assert owner.post(f"/api/photo-uploads/{upload_id}/check", json={}).json() == limited
    assert service(owner, photo_upload_id=str(upload_id)).json() == limited
    patched = owner.patch(f"/api/services/{service_id}", json={"photo_upload_id": str(upload_id)})
    assert (patched.status_code, patched.json()) == (429, limited)
    assert photo_key(people.a, service_id) is None
    assert len(stored(people.a)) == 1  # the one made before the limit


# ZIF-133 F13. fence. Kills: finalizing outside the request's transaction, so a refused photo
# leaves the service created without it.
def test_create_with_a_photo_and_a_bad_photo_creates_nothing(
    s3: S3Client, people: People, owner: TestClient
) -> None:
    bad = upload(s3, people.a, b"hello, not an image")
    refused = service(owner, photo_upload_id=str(bad))
    assert (refused.status_code, refused.json()) == (422, {"code": "photo_type"})
    assert stored(people.a) == []

    good = upload(s3, people.a, image("JPEG", (3000, 2000)))
    created = service(owner, photo_upload_id=str(good))
    assert created.status_code == 201
    service_id = created.json()["id"]
    base = f"{MEDIA}/{people.a}/{service_id}/{good}"
    assert created.json()["photo"] == {
        "thumb_url": f"{base}/thumb.webp",
        "detail_url": f"{base}/detail.webp",
        "width": 1200,
        "height": 800,
    }
    (row,) = stored(people.a)
    assert (row["photo_key"], row["photo_content_type"]) == (
        f"{people.a}/{service_id}/{good}",
        "image/webp",
    )
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="ziftbook-media")["Contents"]]
    assert sorted(keys) == [f"{people.a}/{service_id}/{good}/{n}.webp" for n in ("detail", "thumb")]


# ZIF-133 F13. fence. Kills: a replace that enqueues no delete, or two; a delete that survives a
# rolled-back request; a refused photo that still saves the rest of the PATCH.
def test_replace_remove_and_refuse(
    s3: S3Client,
    people: People,
    owner: TestClient,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = (upload(s3, people.a, image("PNG", (30, 20))) for _ in range(2))
    service_id = service(owner).json()["id"]
    url = f"/api/services/{service_id}"
    assert jobs_of(migrate_engine, people.a) == []

    added = owner.patch(url, json={"photo_upload_id": str(first)})
    assert added.status_code == 200
    assert added.json()["photo"]["width"] == 30
    assert photo_key(people.a, service_id) == f"{people.a}/{service_id}/{first}"
    assert jobs_of(migrate_engine, people.a) == []  # nothing to delete yet

    # A failure after the swap rolls it all back: no new key, no job.
    with monkeypatch.context() as broken:
        failing(broken, auth, "record")
        assert owner.patch(url, json={"photo_upload_id": str(second)}).status_code == 500
    assert photo_key(people.a, service_id) == f"{people.a}/{service_id}/{first}"
    assert jobs_of(migrate_engine, people.a) == []

    replaced = owner.patch(url, json={"photo_upload_id": str(second)})
    assert replaced.status_code == 200
    assert photo_key(people.a, service_id) == f"{people.a}/{service_id}/{second}"
    assert jobs_of(migrate_engine, people.a) == [f"{people.a}/{service_id}/{first}/"]

    bad = upload(s3, people.a, b"hello, not an image")
    refused = owner.patch(url, json={"name": {"en": "Renamed"}, "photo_upload_id": str(bad)})
    assert (refused.status_code, refused.json()) == (422, {"code": "photo_type"})
    assert [r["name"] for r in stored(people.a)] == [{"en": "Cut"}]
    assert photo_key(people.a, service_id) == f"{people.a}/{service_id}/{second}"

    removed = owner.patch(url, json={"photo_upload_id": None})
    assert removed.status_code == 200 and removed.json()["photo"] is None
    assert photo_key(people.a, service_id) is None
    assert jobs_of(migrate_engine, people.a) == [
        f"{people.a}/{service_id}/{first}/",
        f"{people.a}/{service_id}/{second}/",
    ]
    (row,) = stored(people.a)
    assert (row["photo_content_type"], row["photo_width"], row["photo_height"]) == (None,) * 3


# ZIF-133 G3. guard: the same upload id twice, removing a photo that is not there, and an unrelated
# PATCH change nothing (no job, no audit event, no second decode); archiving keeps the photo.
def test_no_ops_and_archive(
    s3: S3Client,
    people: People,
    owner: TestClient,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_id = upload(s3, people.a, image("PNG", (30, 20)))
    service_id = service(owner).json()["id"]
    url = f"/api/services/{service_id}"
    target = f"service:{service_id}"
    assert owner.patch(url, json={"photo_upload_id": None}).status_code == 200
    assert events(migrate_engine, target=target, action="service_changed") == []

    assert owner.patch(url, json={"photo_upload_id": str(upload_id)}).status_code == 200
    (event,) = events(migrate_engine, target=target, action="service_changed")
    assert event["details"] == {"photo": "added"}

    def never(*args: object) -> None:
        raise AssertionError("processed again")

    monkeypatch.setattr(photos, "finalize", never)
    again = owner.patch(url, json={"photo_upload_id": str(upload_id)})
    assert again.status_code == 200 and again.json()["photo"]["width"] == 30
    assert len(events(migrate_engine, target=target, action="service_changed")) == 1
    assert jobs_of(migrate_engine, people.a) == []

    archived = owner.patch(url, json={"archived": True})
    assert archived.json()["archived"] is True
    assert archived.json()["photo"]["width"] == 30
    assert photo_key(people.a, service_id) == f"{people.a}/{service_id}/{upload_id}"


# ZIF-133 G3. guard: the photo is null while ZIF_MEDIA_URL is unset, and the URL has no double slash
# when it ends in one.
def test_photo_urls_follow_media_url(
    s3: S3Client, people: People, owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = upload(s3, people.a, image("PNG", (30, 20)))
    created = service(owner, photo_upload_id=str(upload_id)).json()
    assert created["photo"]["thumb_url"].startswith(f"{MEDIA}/{people.a}/")
    monkeypatch.delenv("ZIF_MEDIA_URL")
    assert owner.get(f"/api/services/{created['id']}").json()["photo"] is None


# ZIF-133 G3. guard: with storage unset a photo is refused (503) but removing one needs no storage.
def test_without_storage_a_photo_is_refused_and_a_removal_is_not(
    s3: S3Client, people: People, owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = upload(s3, people.a, image("PNG", (30, 20)))
    created = service(owner, photo_upload_id=str(upload_id)).json()
    monkeypatch.delenv("ZIF_S3_ENDPOINT")
    url = f"/api/services/{created['id']}"
    refused = owner.patch(url, json={"photo_upload_id": str(uuid.uuid4())})
    assert (refused.status_code, refused.json()) == (503, {"code": "photos_unavailable"})
    assert service(owner, photo_upload_id=str(upload_id)).status_code == 503
    assert owner.patch(url, json={"photo_upload_id": None}).json()["photo"] is None


# ZIF-133 G4. guard: the settings read exactly the documented names, and are configured only when
# all six are set (the public endpoint is optional).
def test_storage_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    names = {
        "ZIF_S3_ENDPOINT": "s3_endpoint",
        "ZIF_S3_UPLOADS_BUCKET": "s3_uploads_bucket",
        "ZIF_S3_MEDIA_BUCKET": "s3_media_bucket",
        "ZIF_S3_ACCESS_KEY_ID": "s3_access_key_id",
        "ZIF_S3_SECRET_ACCESS_KEY": "s3_secret_access_key",
        "ZIF_MEDIA_URL": "media_url",
        "ZIF_S3_PUBLIC_ENDPOINT": "s3_public_endpoint",
    }
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert not StorageSettings().configured()
    for name in names:
        monkeypatch.setenv(name, "x")
    settings = StorageSettings()
    assert settings.configured()
    assert settings.s3_secret_access_key.get_secret_value() == "x"
    assert {
        field: getattr(settings, field)
        for field in names.values()
        if field != "s3_secret_access_key"
    } == {field: "x" for field in names.values() if field != "s3_secret_access_key"}
    for name in names:
        monkeypatch.delenv(name)
        assert StorageSettings().configured() is (name == "ZIF_S3_PUBLIC_ENDPOINT")
        monkeypatch.setenv(name, "x")


# ZIF-133 G4. guard: a startup warning (never an error, never a refusal to start) while storage is
# unset, and the "storage" field of the startup line.
@pytest.mark.parametrize("on", [False, True])
def test_startup_warns_while_storage_is_unset(
    log_lines: Callable[[], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    app_engine: Engine,
    on: bool,
) -> None:
    for name in ("ZIF_S3_ENDPOINT", "ZIF_S3_UPLOADS_BUCKET", "ZIF_S3_MEDIA_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    if on:
        for name, value in {
            "ZIF_S3_ENDPOINT": "https://s3.example.test",
            "ZIF_S3_UPLOADS_BUCKET": "u",
            "ZIF_S3_MEDIA_BUCKET": "m",
            "ZIF_S3_ACCESS_KEY_ID": "k",
            "ZIF_S3_SECRET_ACCESS_KEY": "canary-s3cr3t-startup",
            "ZIF_MEDIA_URL": "https://img.example.test",
        }.items():
            monkeypatch.setenv(name, value)
    else:
        monkeypatch.delenv("ZIF_MEDIA_URL", raising=False)
    with new_client(create_app()) as client:
        assert client.get("/api/healthz").status_code == 200
    lines = log_lines()
    assert [line["storage"] for line in lines if line["msg"] == "api started"] == [
        "on" if on else "off"
    ]
    warned = [
        line["level"] for line in lines if line["msg"] == "storage unset: photo uploads refused"
    ]
    assert warned == ([] if on else ["WARNING"])
    assert "s3.example.test" not in str(lines) and "canary-s3cr3t-startup" not in str(lines)


# ZIF-133 F13. fence. Kills: the service row locked before the image work (bookings take FOR SHARE
# on services, so a NO KEY UPDATE held across a decode turns every booking into 503 busy).
def test_save_holds_no_row_lock_while_it_processes_the_image(
    s3: S3Client,
    people: People,
    owner: TestClient,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_id = service(owner).json()["id"]
    upload_id = upload(s3, people.a, image("PNG", (30, 20)))
    real = photos.finalize
    seen: list[bool] = []

    def spy(tenant_id: uuid.UUID, service: uuid.UUID, upload: uuid.UUID) -> photos.Variants:
        seen.append(locked(migrate_engine, people.a, service_id))
        return real(tenant_id, service, upload)

    monkeypatch.setattr(photos, "finalize", spy)
    patched = owner.patch(f"/api/services/{service_id}", json={"photo_upload_id": str(upload_id)})
    assert patched.status_code == 200
    assert seen == [False]
    assert not locked(migrate_engine, people.a, service_id)  # and released with the request


# ZIF-133 F13. fence. Kills: the check running inside the request's tenant transaction, which pins a
# pooled connection for the whole decode.
def test_the_check_holds_no_transaction_while_it_processes(
    s3: S3Client,
    people: People,
    owner: TestClient,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_id = upload(s3, people.a, image("PNG", (30, 20)))
    real = photos.process
    seen: list[int] = []

    def spy(tenant_id: uuid.UUID, upload: uuid.UUID) -> photos.Variants:
        seen.append(idle_in_transaction())
        return real(tenant_id, upload)

    monkeypatch.setattr(photos, "process", spy)
    assert owner.post(f"/api/photo-uploads/{upload_id}/check", json={}).status_code == 200
    assert seen == [0]


# ZB-16. fence. Kills: a Save that decodes its photo inside the request's tenant transaction, which
# pins a pooled connection while it waits on the decode lock.
@pytest.mark.parametrize("save", ["create", "update"])
def test_a_save_holds_no_transaction_while_it_processes_the_photo(
    s3: S3Client,
    people: People,
    owner: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    save: str,
) -> None:
    upload_id = upload(s3, people.a, image("PNG", (30, 20)))
    real = photos.process
    seen: list[int] = []

    def spy(tenant_id: uuid.UUID, upload: uuid.UUID) -> photos.Variants:
        seen.append(idle_in_transaction())
        return real(tenant_id, upload)

    monkeypatch.setattr(photos, "process", spy)
    if save == "create":
        saved = service(owner, photo_upload_id=str(upload_id))
    else:
        url = f"/api/services/{service(owner).json()['id']}"  # no photo: no process yet
        saved = owner.patch(url, json={"photo_upload_id": str(upload_id)})
    assert saved.status_code == (201 if save == "create" else 200)
    assert saved.json()["photo"]["width"] == 30
    assert seen == [0]


# ZB-16. guard: the write re-reads the service under its lock in its own transaction, so a change
# another save committed while this one decoded is kept, not overwritten from the earlier read.
def test_a_save_writes_over_the_service_as_it_is_after_the_decode(
    s3: S3Client,
    people: People,
    owner: TestClient,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_id = upload(s3, people.a, image("PNG", (30, 20)))
    service_id = service(owner).json()["id"]
    real = photos.finalize

    def renamed_meanwhile(tenant_id: uuid.UUID, svc: uuid.UUID, up: uuid.UUID) -> photos.Variants:
        result = real(tenant_id, svc, up)
        with tenant_context(people.a) as db:
            db.execute(
                text("UPDATE services SET name = CAST(:n AS jsonb) WHERE id = :s"),
                {"n": '{"en": "Renamed"}', "s": service_id},
            )
        return result

    monkeypatch.setattr(photos, "finalize", renamed_meanwhile)
    patched = owner.patch(f"/api/services/{service_id}", json={"photo_upload_id": str(upload_id)})
    assert patched.status_code == 200
    assert (patched.json()["name"], patched.json()["photo"]["width"]) == ({"en": "Renamed"}, 30)
    assert [r["name"] for r in stored(people.a)] == [{"en": "Renamed"}]
    (event,) = events(migrate_engine, target=f"service:{service_id}", action="service_changed")
    assert event["details"] == {"photo": "added"}


# ZB-16. guard: a service the business does not have is a 404 before the photo is decoded.
def test_an_unknown_service_is_a_404_before_any_decode(
    s3: S3Client, people: People, owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = upload(s3, people.a, image("PNG", (30, 20)))

    def never(*args: object) -> None:
        raise AssertionError("processed")

    monkeypatch.setattr(photos, "process", never)
    missing = owner.patch(f"/api/services/{uuid.uuid7()}", json={"photo_upload_id": str(upload_id)})
    assert (missing.status_code, missing.json()) == (404, {"code": "not_found"})


# ZIF-133 F13. fence. Kills: a media.delete dedupe key with no unique tail: finished jobs keep their
# key for 30 days, so deleting the same prefix a second time would never be enqueued.
def test_the_same_prefix_can_be_deleted_twice(
    s3: S3Client, people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    a, b = (upload(s3, people.a, image("PNG", (30, 20))) for _ in range(2))
    service_id = service(owner).json()["id"]
    for upload_id in (a, b, a, b):
        owner.patch(f"/api/services/{service_id}", json={"photo_upload_id": str(upload_id)})
    prefix = f"{people.a}/{service_id}"
    assert jobs_of(migrate_engine, people.a) == [
        f"{prefix}/{a}/",
        f"{prefix}/{b}/",
        f"{prefix}/{a}/",
    ]


# ZIF-133 F7. fence. Kills: the hourly limit keyed by business only (many businesses from one
# address can then each spend a full budget on the decoder).
def test_the_limit_is_also_per_address(
    s3: S3Client, app: FastAPI, people: People, owner: TestClient
) -> None:
    address = fresh_address()
    client = new_client(app, address)
    client.cookies.set(auth.COOKIE, owner.cookies[auth.COOKIE])
    for _ in range(100):
        assert not limits.hit({limits.ip_key("photo", address): 100}, timedelta(hours=1))
    assert presign(client).status_code == 429
    assert presign(owner).status_code == 201  # another address, same business: fine


# ZIF-133 F13. fence. Kills: a Save whose upload another Save set while it waited for the lock
# still replacing the photo (a second audit event and a job that deletes the live photo's prefix).
def test_a_concurrent_save_of_the_same_upload_is_a_no_op(
    s3: S3Client,
    people: People,
    owner: TestClient,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_id = upload(s3, people.a, image("PNG", (30, 20)))
    service_id = service(owner).json()["id"]
    real = photos.finalize

    def other_save_wins(tenant_id: uuid.UUID, svc: uuid.UUID, up: uuid.UUID) -> photos.Variants:
        result = real(tenant_id, svc, up)
        with tenant_context(people.a) as db:
            db.execute(
                text("""
                UPDATE services SET photo_key = :k, photo_content_type = 'image/webp',
                  photo_width = 30, photo_height = 20 WHERE id = :s
                """),
                {"k": f"{people.a}/{service_id}/{up}", "s": service_id},
            )
        return result

    monkeypatch.setattr(photos, "finalize", other_save_wins)
    patched = owner.patch(
        f"/api/services/{service_id}",
        json={"name": {"en": "Renamed"}, "photo_upload_id": str(upload_id)},
    )
    assert patched.status_code == 200
    assert jobs_of(migrate_engine, people.a) == []
    (event,) = events(migrate_engine, target=f"service:{service_id}", action="service_changed")
    assert "photo" not in event["details"]


# ZIF-133 F9. fence. Kills: Save's write of the variants unguarded (HEAD and GET work, only the PUT
# to the media bucket fails): the answer must be 503, with nothing written to the row.
def test_a_media_write_failure_on_save_is_a_503_and_changes_nothing(
    s3: S3Client, people: People, owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = upload(s3, people.a, image("PNG", (30, 20)))
    service_id = service(owner).json()["id"]
    real = storage.client

    def media_down(**kw: bool) -> S3Client:
        c = real(**kw)

        def fail(**_: object) -> None:
            raise EndpointConnectionError(endpoint_url="https://secret.example/x")

        c.put_object = fail  # type: ignore[method-assign,assignment]
        return c

    monkeypatch.setattr(storage, "client", media_down)
    patched = owner.patch(
        f"/api/services/{service_id}",
        json={"name": {"en": "Renamed"}, "photo_upload_id": str(upload_id)},
    )
    assert (patched.status_code, patched.json()) == (503, {"code": "photos_unavailable"})
    assert photo_key(people.a, service_id) is None
    assert [r["name"] for r in stored(people.a)] == [{"en": "Cut"}]
