"""The settings key contact_email holds only a confirmed address (ZIF-232).

A trigger refuses an INSERT or UPDATE that leaves a settings row with key contact_email unless the
role running the statement is ziftbook_migrate. Only confirm_contact_email (0086), a definer owned
by ziftbook_migrate that needs the hash of a link the worker mailed, writes the key from the
running app. So every reader of the key (the mailer's Reply-To, and any page that shows it) sees
an address its inbox confirmed, from every release, old or new, and after a rollback. An allow-list,
not a deny-list: the app role, the worker, superusers and every other role are refused. WHEN on
NEW.key also catches an UPDATE that renames another row to contact_email. DELETE is not guarded:
removing an address proves nothing and harms nobody.

Operations: an address an operator sets by hand counts as confirmed by fiat; a superuser runs
`SET ROLE ziftbook_migrate` first, which makes current_user the migrate role's. Do not disable the
trigger, which would let every role through meanwhile. A data-only restore of settings runs as the
restoring role and is refused at the first contact_email row, which fails the whole COPY of
settings, for every business: it needs --disable-triggers (pg_restore or pg_dump). A full restore
is not affected: it creates the trigger after it loads the data.

A ruled exception to "a migration never breaks the running version" (CONTRIBUTING, Database
migrations): during the deploy, the older release's PUT of contact_email is refused with a 500,
and the other keys sent with it are not saved (one transaction). It fails closed: it can only
refuse, never make an unconfirmed address the one in effect. No first-party client sends the key
before this release.

Lock: CREATE TRIGGER takes SHARE ROW EXCLUSIVE on settings for an instant, within the 5 s
lock_timeout.

Revision ID: 0087
Revises: 0086
"""

from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def upgrade() -> None:
    # 42501 by name: the caller lacks the right to write the key, as a missing grant would say.
    op.execute(f"""
    CREATE FUNCTION settings_contact_email_confirmed_only() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER {SEARCH_PATH} AS $$
      BEGIN
        IF current_user <> 'ziftbook_migrate' THEN
          RAISE EXCEPTION 'only a confirmed contact email is saved'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("""
    CREATE TRIGGER settings_contact_email_confirmed_only BEFORE INSERT OR UPDATE ON settings
      FOR EACH ROW WHEN (NEW.key = 'contact_email')
      EXECUTE FUNCTION settings_contact_email_confirmed_only()""")
    # Firing a trigger checks no EXECUTE privilege (0030), so no role needs it.
    op.execute("REVOKE EXECUTE ON FUNCTION settings_contact_email_confirmed_only() FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER settings_contact_email_confirmed_only ON settings")
    op.execute("DROP FUNCTION settings_contact_email_confirmed_only()")
