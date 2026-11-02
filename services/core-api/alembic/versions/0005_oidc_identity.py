"""Phase 5: link users to OIDC identities (issuer + subject).

Revision ID: 0005
"""

from alembic import op

revision = "0005"
down_revision = "0004"

# Statements separated by a line containing only "--;".
UPGRADE = r"""
ALTER TABLE users ADD COLUMN oidc_issuer text, ADD COLUMN oidc_subject text
--;
ALTER TABLE users ADD CONSTRAINT users_oidc_identity UNIQUE (oidc_issuer, oidc_subject)
--;
CREATE POLICY users_definer_link ON users FOR UPDATE TO documind_owner USING (true) WITH CHECK (true)
--;
-- (issuer, subject) is the identity, never the email. First login may link a pre-provisioned
-- user by email only when the IdP asserts email_verified. No just-in-time provisioning.
CREATE FUNCTION resolve_user_by_oidc(p_iss text, p_sub text, p_email text, p_verified boolean)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = documind AS $$
DECLARE uid uuid;
BEGIN
  SELECT id INTO uid FROM users WHERE oidc_issuer = p_iss AND oidc_subject = p_sub;
  IF uid IS NOT NULL THEN
    RETURN uid;
  END IF;
  IF p_verified AND p_email IS NOT NULL THEN
    UPDATE users SET oidc_issuer = p_iss, oidc_subject = p_sub
     WHERE lower(email) = lower(p_email) AND oidc_subject IS NULL
    RETURNING id INTO uid;
  END IF;
  RETURN uid;
END $$
--;
GRANT EXECUTE ON FUNCTION resolve_user_by_oidc(text, text, text, boolean) TO documind_app
"""

DOWNGRADE = r"""
DROP FUNCTION resolve_user_by_oidc(text, text, text, boolean)
--;
DROP POLICY users_definer_link ON users
--;
ALTER TABLE users DROP CONSTRAINT users_oidc_identity, DROP COLUMN oidc_subject,
  DROP COLUMN oidc_issuer
"""


def _split(sql: str) -> list[str]:
    return [s.strip() for s in sql.split("\n--;\n") if s.strip()]


def upgrade() -> None:
    for stmt in _split(UPGRADE):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _split(DOWNGRADE):
        op.execute(stmt)
