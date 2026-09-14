from sqlalchemy import Engine, text


def test_app_role_creates_tenants_with_uuidv7_ids(app_engine: Engine) -> None:
    with app_engine.begin() as conn:
        tenant_id = conn.scalar(text("INSERT INTO tenants (name) VALUES ('Nail bar') RETURNING id"))
        version = conn.scalar(
            text("SELECT uuid_extract_version(id) FROM tenants WHERE id = :id"), {"id": tenant_id}
        )
        conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
    assert version == 7


def test_tenants_primary_key_follows_the_naming_convention(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        name = conn.scalar(
            text(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = 'tenants'::regclass AND contype = 'p'"
            )
        )
    assert name == "pk_tenants"
