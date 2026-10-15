"""services: a photo's storage key and metadata, and the placeholder kind (ZIF-133).

Never the image: the bytes live in object storage, the row keeps the key, content type and the
detail variant's size. The key's first two segments are pinned to the row's tenant and id, so a
prefix delete (a replaced photo, an erased business) can only reach that service's or that
business's objects, and no bytes or base64 fit. photo_placeholder is backfilled once from the
service name by the web app's rule (frontend/lib/placeholders.ts, frozen here); it is never
recomputed, so a rename keeps the pick.

Revision ID: 0040
Revises: 0039
"""

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Accents folded before lower(), so lower() only meets ASCII and no collation decides the result.
FROM = "áàâãäåçéèêëíìîïñóòôõöúùûüýÿÁÀÂÃÄÅÇÉÈÊËÍÌÎÏÑÓÒÔÕÖÚÙÛÜÝ"
TO = "aaaaaaceeeeiiiinooooouuuuyyAAAAAACEEEEIIIINOOOOOUUUUY"
KINDS = {
    "nails": "nail|mani|pedi|gel|polish|acryl|shellac|nagel|gellak|lak|unha|esmalte|alongamento",
    "hair": "hair|haircut|trim|colour|color|blow|highlight|beard|haar|knip|kleur|fohn|baard|"
    "cabelo|corte|escova|coloracao|mechas|barba",
    "beauty": "brow|lash|facial|make-up|makeup|wax|wenkbrauw|wimper|gezicht|harsen|sobrancelha|"
    "cilio|limpeza de pele|maquiagem|depilacao",
    "massage": "massage|massagem",
}
# NFC first: a name pasted in decomposed form (macOS) carries its accents as separate marks.
FOLDED = f"lower(translate(normalize(v, NFC), '{FROM}', '{TO}'))"
CASES = " ".join(
    f"WHEN EXISTS (SELECT 1 FROM jsonb_each_text(name) AS t(k, v) "
    f"WHERE {FOLDED} ~ '\\m({words})') THEN '{kind}'"
    for kind, words in KINDS.items()
)


def upgrade() -> None:
    op.execute(f"""
    ALTER TABLE services
      ADD COLUMN photo_key text,
      ADD COLUMN photo_content_type text
        CONSTRAINT ck_services_photo_content_type CHECK (photo_content_type = 'image/webp'),
      ADD COLUMN photo_width integer
        CONSTRAINT ck_services_photo_width CHECK (photo_width BETWEEN 1 AND 1200),
      ADD COLUMN photo_height integer
        CONSTRAINT ck_services_photo_height CHECK (photo_height BETWEEN 1 AND 1200),
      ADD COLUMN photo_placeholder text NOT NULL DEFAULT 'generic'
        CONSTRAINT ck_services_photo_placeholder
          CHECK (photo_placeholder IN ('generic','nails','hair','beauty','massage')),
      ADD CONSTRAINT ck_services_photo_key CHECK (
        photo_key ~ '^{UUID}/{UUID}/{UUID}$'
        AND split_part(photo_key, '/', 1) = tenant_id::text
        AND split_part(photo_key, '/', 2) = id::text),
      ADD CONSTRAINT ck_services_photo_whole CHECK (
        num_nulls(photo_key, photo_content_type, photo_width, photo_height) IN (0, 4))
    """)
    # services has FORCE ROW LEVEL SECURITY and a migration sets no app.tenant_id, so a bare
    # UPDATE fails 42704; lift FORCE for the owner inside this transaction only.
    op.execute("ALTER TABLE services NO FORCE ROW LEVEL SECURITY")
    op.execute(f"UPDATE services SET photo_placeholder = CASE {CASES} ELSE 'generic' END")
    op.execute("ALTER TABLE services FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("""
    ALTER TABLE services DROP COLUMN photo_key, DROP COLUMN photo_content_type,
      DROP COLUMN photo_width, DROP COLUMN photo_height, DROP COLUMN photo_placeholder
    """)
