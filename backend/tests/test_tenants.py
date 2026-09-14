from sqlalchemy import Engine, text


def test_app_role_creates_tenants_with_uuidv7_ids(app_engine: Engine) -> None:
    with app_engine.connect() as conn:  # rolled back on close
        tenant_id = conn.scalar(text("INSERT INTO tenants (name) VALUES ('Nail bar') RETURNING id"))
    assert tenant_id.version == 7
