"""contact_email_requests: a typed address waits for its inbox to confirm it (ZIF-232).

From 0087 on, the settings key contact_email holds only a confirmed address. The address an owner
types waits here, one row per business, and is used for nothing until someone opens the link
mailed to it. The link carries <tenant_id>.<secret>; only sha256(secret) is stored, and only the
email.contact_email job, running as ziftbook_worker, sets it (ZB-08, as invites).

The app role reads every column but token_hash and writes none: it changes the table only through
the definer functions below, which run in the caller's tenant_context (FORCE row-level security
binds their owner too, so they see only that business's row). None reads memberships or
audit_events, so no GUC needs clearing.

- change_contact_email (app): the owner's PUT.
- resend_contact_email (app): a new link for the waiting address; the old one dies at once.
- cancel_contact_email (app): drop the waiting address, keeping the one in effect.
- confirm_contact_email (app): the link's hash makes the address the one in effect, once.
- mint_contact_email_token (worker only): store the hash of the link the job is about to mail.

Every writer locks the business's request row first, then its contact_email setting, so two of
them cannot deadlock. The trust is the invite's: a compromised worker can confirm any address for
any business (it also holds the mail key, so it can already send any mail); a compromised API
cannot, as it can neither read a hash, set one nor mint.

Lock: the foreign key takes SHARE ROW EXCLUSIVE on tenants for an instant, which holds back a
sign-up or a rename for that long, within the 5 s lock_timeout.

Every confirmation is also recorded in contact_email_confirmations (address, the one it replaced,
clock_timestamp), written only by confirm_contact_email and readable, never writable, by the runtime
roles: after an API compromise, the confirmations made in the window can be listed from it
(CONTRIBUTING, Business settings). It is kept as evidence for the business's life.

Downgrading drops both tables, and with them the only copy of the addresses 0088 moved out of use
and the confirmation record. It takes ACCESS EXCLUSIVE on tenants for an instant (the foreign keys),
so queries on tenants wait that long.

Revision ID: 0086
Revises: 0056
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0086"
down_revision = "0056"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
LIFETIME = "interval '24 hours'"

CHANGE = f"""
CREATE FUNCTION change_contact_email(p_email text, p_send boolean, p_requester uuid DEFAULT NULL)
RETURNS TABLE (outcome text, request_id uuid, previous text)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- p_email NULL: no contact email at all (cleared). The address in effect: nothing waits (kept).
-- The waiting address: left as it is, so a save of other keys keeps a sent link (unchanged),
-- unless p_send and its link is dead. Any other: it waits, with a link due when p_send
-- (requested). previous is the address in effect before the call. p_requester: the owner asking,
-- recorded on a new request; its link confirms only while they are an owner.
DECLARE
  v_email text := lower(p_email);
  v_request contact_email_requests;
  v_confirmed text;
  v_id uuid;
BEGIN
  SELECT * INTO v_request FROM contact_email_requests FOR UPDATE;
  SELECT s.value #>> '{{}}' INTO v_confirmed FROM settings s
  WHERE s.key = 'contact_email' FOR UPDATE;
  IF v_email IS NULL THEN
    DELETE FROM contact_email_requests;
    DELETE FROM settings s WHERE s.key = 'contact_email';
    RETURN QUERY SELECT 'cleared', NULL::uuid, v_confirmed;
  ELSIF v_email = v_confirmed THEN
    DELETE FROM contact_email_requests;
    RETURN QUERY SELECT 'kept', NULL::uuid, v_confirmed;
  ELSIF v_request.email = v_email AND NOT (p_send AND coalesce(v_request.expires_at <= now(), true))
  THEN
    RETURN QUERY SELECT 'unchanged', v_request.id, v_confirmed;
  ELSE
    -- A new id and no hash: a queued job for the old id sends nothing, the old link is dead.
    INSERT INTO contact_email_requests AS r (tenant_id, email, expires_at, requested_by)
    VALUES (current_setting('app.tenant_id')::uuid, v_email,
            CASE WHEN p_send THEN now() + {LIFETIME} END, p_requester)
    ON CONFLICT (tenant_id) DO UPDATE
      SET id = uuidv7(), email = excluded.email, token_hash = NULL,
          expires_at = excluded.expires_at, requested_by = excluded.requested_by
    RETURNING r.id INTO v_id;
    RETURN QUERY SELECT 'requested', v_id, v_confirmed;
  END IF;
END $$"""

RESEND = f"""
CREATE FUNCTION resend_contact_email(p_requester uuid DEFAULT NULL) RETURNS uuid
LANGUAGE sql SECURITY DEFINER {SEARCH_PATH} AS $$
  -- A new link for the waiting address: a new id and no hash, so the old link dies now. NULL when
  -- nothing waits. The owner sending it becomes the requester.
  UPDATE contact_email_requests
  SET id = uuidv7(), token_hash = NULL, expires_at = now() + {LIFETIME}, requested_by = p_requester
  RETURNING id
$$"""

CANCEL = f"""
CREATE FUNCTION cancel_contact_email() RETURNS boolean
LANGUAGE sql SECURITY DEFINER {SEARCH_PATH} AS $$
  -- Drop the waiting address only: the one in effect stays, and the waiting link dies.
  WITH gone AS (DELETE FROM contact_email_requests RETURNING 1)
  SELECT EXISTS (SELECT FROM gone)
$$"""

MINT = f"""
CREATE FUNCTION mint_contact_email_token(p_id uuid, p_token_hash bytea)
RETURNS TABLE (email text, business text)
LANGUAGE sql SECURITY DEFINER {SEARCH_PATH} AS $$
  -- Only a request still due a link: replaced, cleared, confirmed or lapsed ones mint nothing.
  -- Minting again (a retry) replaces the hash, so only the newest link works.
  UPDATE contact_email_requests r
  SET token_hash = p_token_hash, expires_at = now() + {LIFETIME}
  FROM tenants t
  WHERE r.id = p_id AND r.expires_at > now() AND t.id = r.tenant_id
  RETURNING r.email, t.name
$$"""

CONFIRM = f"""
CREATE FUNCTION confirm_contact_email(p_token_hash bytea)
RETURNS TABLE (confirmed text, previous text)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- A live link's address becomes the business's contact email; the link is used up. No row for a
-- dead one, nor while the owner who asked for it is no longer an owner (the request stays, for
-- another owner to resend). previous is the address in effect before.
DECLARE
  v_email text;
  v_requester uuid;
  v_previous text;
  v_sign_in text := current_setting('app.sign_in', true);
BEGIN
  -- It reads memberships: only this business's (app.sign_in would widen the migrate role's view).
  -- The caller's value comes back right after, as in 0032.
  PERFORM set_config('app.sign_in', '', true);
  DELETE FROM contact_email_requests r
  WHERE r.token_hash = p_token_hash AND r.expires_at > now()
    AND (r.requested_by IS NULL
         OR EXISTS (SELECT FROM memberships m
                    WHERE m.user_id = r.requested_by AND m.role = 'owner'))
  RETURNING r.email, r.requested_by INTO v_email, v_requester;
  PERFORM set_config('app.sign_in', coalesce(v_sign_in, ''), true);
  IF v_email IS NULL THEN
    RETURN;
  END IF;
  INSERT INTO settings AS s (tenant_id, key, value)
  VALUES (current_setting('app.tenant_id')::uuid, 'contact_email', to_jsonb(v_email))
  ON CONFLICT (tenant_id, key) DO UPDATE SET value = excluded.value
  RETURNING old.value #>> '{{}}' INTO v_previous;
  -- Written here, not by the API: a compromised API cannot confirm an address and erase the trace.
  INSERT INTO contact_email_confirmations (tenant_id, email, previous, requested_by)
  VALUES (current_setting('app.tenant_id')::uuid, v_email, v_previous, v_requester);
  RETURN QUERY SELECT v_email, v_previous;
END $$"""

FUNCTIONS = {
    "change_contact_email(text, boolean, uuid)": ("ziftbook_app", CHANGE),
    "resend_contact_email(uuid)": ("ziftbook_app", RESEND),
    "cancel_contact_email()": ("ziftbook_app", CANCEL),
    "confirm_contact_email(bytea)": ("ziftbook_app", CONFIRM),
    "mint_contact_email_token(uuid, bytea)": ("ziftbook_worker", MINT),
}


def upgrade() -> None:
    op.execute("""
    CREATE TABLE contact_email_requests (
      id uuid CONSTRAINT pk_contact_email_requests PRIMARY KEY DEFAULT uuidv7(),
      -- The request means nothing without its business.
      tenant_id uuid NOT NULL CONSTRAINT fk_contact_email_requests_tenant_id_tenants
        REFERENCES tenants (id) ON DELETE CASCADE,
      email text NOT NULL
        CONSTRAINT ck_contact_email_requests_email_lowercase CHECK (email = lower(email))
        CONSTRAINT ck_contact_email_requests_email_length CHECK (char_length(email) <= 254),
      -- NULL until the email job mints the link it sends.
      token_hash bytea
        CONSTRAINT ck_contact_email_requests_token_hash_length
          CHECK (octet_length(token_hash) = 32),
      -- NULL: no link is due. Set when a link is queued, and again when it is minted.
      expires_at timestamptz,
      -- The owner who asked; NULL only for 0088's moved addresses, which get no link until a resend
      -- records its owner. No foreign key: confirm checks the membership itself.
      requested_by uuid,
      CONSTRAINT uq_contact_email_requests_tenant_id UNIQUE (tenant_id)
    )""")
    enable_tenant_isolation("contact_email_requests")
    # 0001's default privileges give the app role DML; it writes only through the functions.
    op.execute("REVOKE ALL ON contact_email_requests FROM ziftbook_app")
    op.execute(
        "GRANT SELECT (id, tenant_id, email, expires_at) ON contact_email_requests TO ziftbook_app"
    )
    # Every confirmation, as evidence: only confirm_contact_email writes it; runtime roles read it.
    op.execute("""
    CREATE TABLE contact_email_confirmations (
      id uuid CONSTRAINT pk_contact_email_confirmations PRIMARY KEY DEFAULT uuidv7(),
      tenant_id uuid NOT NULL CONSTRAINT fk_contact_email_confirmations_tenant_id_tenants
        REFERENCES tenants (id) ON DELETE CASCADE,
      email text NOT NULL,
      previous text,
      requested_by uuid,
      confirmed_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")
    enable_tenant_isolation("contact_email_confirmations")
    op.execute("REVOKE ALL ON contact_email_confirmations FROM ziftbook_app")
    op.execute("GRANT SELECT ON contact_email_confirmations TO ziftbook_app")
    for signature, (role, body) in FUNCTIONS.items():
        op.execute(body)
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {role}")


def downgrade() -> None:
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP TABLE contact_email_confirmations")
    op.execute("DROP TABLE contact_email_requests")
