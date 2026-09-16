"""services: what a business sells, priced in the business's own currency.

The app role can add and change services but never delete one: an archived service stays, so
bookings (ZIF-51) can keep pointing at it. The price's currency is the business's, enforced by
a foreign key to tenants (id, currency), so that currency can't change while the business has
services.

Revision ID: 0016
Revises: 0013
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0016"
down_revision = "0013"
branch_labels = None
depends_on = None


def languages(column: str) -> str:
    # An object whose keys are only en, nl or pt. CASE, not AND: Postgres doesn't promise to
    # evaluate AND's sides in order, and `-` raises on a scalar instead of failing the check.
    # ELSE false: a NULL would pass a CHECK.
    return (
        f"CASE WHEN jsonb_typeof({column}) = 'object' "
        f"THEN {column} - ARRAY['en', 'nl', 'pt'] = '{{}}'::jsonb ELSE false END"
    )


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE services (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      tenant_id uuid NOT NULL
        CONSTRAINT fk_services_tenant_id_tenants REFERENCES tenants (id),
      name jsonb NOT NULL
        CONSTRAINT ck_services_name CHECK ({languages("name")} AND name <> '{{}}'::jsonb),
      description jsonb NOT NULL DEFAULT '{{}}'
        CONSTRAINT ck_services_description CHECK ({languages("description")}),
      price_amount_minor integer NOT NULL
        CONSTRAINT ck_services_price_amount_minor CHECK (price_amount_minor BETWEEN 0 AND 1000000),
      -- Never sent by a client: copied from tenants when the service is created.
      price_currency text NOT NULL,
      duration_minutes integer NOT NULL
        CONSTRAINT ck_services_duration_minutes CHECK (duration_minutes BETWEEN 5 AND 720),
      -- NULL: the business's default buffer (ZIF-48).
      buffer_minutes integer
        CONSTRAINT ck_services_buffer_minutes CHECK (buffer_minutes BETWEEN 0 AND 240),
      archived_at timestamptz,
      -- NO ACTION: a business's currency can't change under its prices.
      CONSTRAINT fk_services_tenant_id_price_currency_tenants
        FOREIGN KEY (tenant_id, price_currency) REFERENCES tenants (id, currency)
    )""")
    enable_tenant_isolation("services")
    # 0001's default privileges granted DELETE; services are archived, never deleted.
    op.execute("REVOKE DELETE ON services FROM ziftbook_app")


def downgrade() -> None:
    op.execute("DROP TABLE services")
