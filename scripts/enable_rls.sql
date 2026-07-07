-- onebrain — tenant-isolation Row-Level Security backstop for the `chunks` table.
--
-- WHAT THIS IS
--   Defense in depth BENEATH AccessFilter. Today every query is built through
--   AccessFilter.to_sql(), which already includes `meta->>'tenant_id' = <tenant>`
--   as its first clause. This policy makes the DATABASE ITSELF refuse cross-tenant
--   rows, so a FUTURE code path that queries `chunks` without that filter (a new
--   service endpoint, a raw query) still cannot leak across tenants.
--
-- HOW IT WORKS — deny-when-unset
--   A connection must `SET app.tenant_id = '<tenant>'` before it can see any row.
--   current_setting('app.tenant_id', true) returns NULL when unset, and
--   `tenant_id = NULL` is never true, so an un-scoped connection sees NOTHING
--   (fail closed). FORCE ROW LEVEL SECURITY makes even the table owner subject to
--   the policy — a superuser still bypasses it (used for maintenance, below).
--
-- OPERATIONAL REQUIREMENTS — why this is wired in Phase 4, not just applied
--   * The application must connect as a NON-superuser role WITHOUT BYPASSRLS, and
--     must `SET app.tenant_id` per connection/transaction from Principal.tenant_id.
--     (Read paths get it from AccessFilter.tenant_id; write/delete/status paths
--     must be given the tenant explicitly.)
--   * Global operations that span tenants — schema migration, demo/sample seeding,
--     the /health row count — must run as the OWNER/superuser role, which bypasses
--     the policy. In the current single-role Railway setup the app IS the owner, so
--     enabling this safely means splitting into (a) an owner role for migrations +
--     seeding and (b) a restricted app role for request queries, and threading the
--     tenant into every store call. That split is the Phase-4 service-auth work.
--
-- VALIDATED: the deny-when-unset / SET app.tenant_id / WITH CHECK semantics are
-- standard PostgreSQL RLS. Apply and exercise this on a STAGING database first —
-- with the app still connecting as the owner and NOT setting app.tenant_id, every
-- query correctly returns zero rows, which is why the role split above is required
-- before this goes live.

ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON chunks;
CREATE POLICY tenant_isolation ON chunks
  USING      (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- Example application role (run once, as owner/superuser):
--   CREATE ROLE onebrain_app LOGIN PASSWORD '<strong>' NOSUPERUSER NOBYPASSRLS;
--   GRANT SELECT, INSERT, UPDATE, DELETE ON chunks TO onebrain_app;
-- The application then runs, per connection:
--   SET app.tenant_id = '<principal.tenant_id>';
