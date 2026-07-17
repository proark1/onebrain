#!/bin/sh
# One Postgres server, per-product databases, and isolated runtime logins.
#
# Docker executes this from /docker-entrypoint-initdb.d on a fresh data
# directory. The rendered postgres-roles one-shot service executes the same
# idempotent script against an existing volume before migrations, so role and
# ACL hardening is never limited to brand-new boxes.
set -eu

die() {
  echo "postgres-init: $*" >&2
  exit 1
}

validate_role() {
  role_name=$1
  role_value=$2
  if ! printf '%s\n' "$role_value" | grep -Eq '^[A-Za-z_][A-Za-z0-9_$]{0,62}$'; then
    die "$role_name must be a simple PostgreSQL login role name"
  fi
}

validate_password() {
  password_name=$1
  password_value=$2
  [ "${#password_value}" -ge 32 ] || die "$password_name must be at least 32 characters"
}

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${POSTGRES_APP_ROLE:?POSTGRES_APP_ROLE is required}"
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD is required}"
: "${POSTGRES_WORKER_ROLE:?POSTGRES_WORKER_ROLE is required}"
: "${POSTGRES_WORKER_PASSWORD:?POSTGRES_WORKER_PASSWORD is required}"
: "${POSTGRES_ASSISTANT_ROLE:?POSTGRES_ASSISTANT_ROLE is required}"
: "${POSTGRES_ASSISTANT_PASSWORD:?POSTGRES_ASSISTANT_PASSWORD is required}"
: "${POSTGRES_COMMUNICATION_ROLE:?POSTGRES_COMMUNICATION_ROLE is required}"
: "${POSTGRES_COMMUNICATION_PASSWORD:?POSTGRES_COMMUNICATION_PASSWORD is required}"

validate_password "POSTGRES_PASSWORD" "$POSTGRES_PASSWORD"
validate_password "POSTGRES_APP_PASSWORD" "$POSTGRES_APP_PASSWORD"
validate_password "POSTGRES_WORKER_PASSWORD" "$POSTGRES_WORKER_PASSWORD"
validate_password "POSTGRES_ASSISTANT_PASSWORD" "$POSTGRES_ASSISTANT_PASSWORD"
validate_password "POSTGRES_COMMUNICATION_PASSWORD" "$POSTGRES_COMMUNICATION_PASSWORD"

for role_name in \
  "$POSTGRES_APP_ROLE" \
  "$POSTGRES_WORKER_ROLE" \
  "$POSTGRES_ASSISTANT_ROLE" \
  "$POSTGRES_COMMUNICATION_ROLE"; do
  validate_role "runtime role" "$role_name"
  [ "$role_name" != "$POSTGRES_USER" ] || die "runtime roles must not use POSTGRES_USER"
done

if printf '%s\n' \
  "$POSTGRES_APP_ROLE" \
  "$POSTGRES_WORKER_ROLE" \
  "$POSTGRES_ASSISTANT_ROLE" \
  "$POSTGRES_COMMUNICATION_ROLE" | sort | uniq -d | grep -q .; then
  die "all runtime roles must differ"
fi

# The entrypoint-initdb hook talks to the local trust socket. postgres-roles
# uses PGHOST=postgres over SCRAM instead, so pass the owner password only in
# that child process environment (never as a psql command-line argument).
if [ -n "${PGHOST:-}" ]; then
  PGPASSWORD=$POSTGRES_PASSWORD
  export PGPASSWORD
fi

for db in onebrain assistant communication; do
  if ! psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_USER" \
        -tAc "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1; then
    createdb --username "$POSTGRES_USER" "$db"
    echo "postgres-init: created database $db"
  fi
done

# Use psql's \getenv rather than command-line -v password=value so no
# database password appears in a process argument. format(%I/%L) quotes role
# names and passwords as SQL identifiers/literals.
configure_runtime_logins() {
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname onebrain <<'SQL'
\getenv app_role POSTGRES_APP_ROLE
\getenv app_password POSTGRES_APP_PASSWORD
\getenv worker_role POSTGRES_WORKER_ROLE
\getenv worker_password POSTGRES_WORKER_PASSWORD
\getenv assistant_role POSTGRES_ASSISTANT_ROLE
\getenv assistant_password POSTGRES_ASSISTANT_PASSWORD
\getenv communication_role POSTGRES_COMMUNICATION_ROLE
\getenv communication_password POSTGRES_COMMUNICATION_PASSWORD

SET password_encryption = 'scram-sha-256';

WITH runtime_roles(role_name, role_password) AS (
    VALUES
        (:'app_role', :'app_password'),
        (:'worker_role', :'worker_password'),
        (:'assistant_role', :'assistant_password'),
        (:'communication_role', :'communication_password')
)
SELECT format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS NOREPLICATION PASSWORD %L',
    role_name, role_password
)
FROM runtime_roles
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name)
\gexec

WITH runtime_roles(role_name, role_password) AS (
    VALUES
        (:'app_role', :'app_password'),
        (:'worker_role', :'worker_password'),
        (:'assistant_role', :'assistant_password'),
        (:'communication_role', :'communication_password')
)
SELECT format(
    'ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS NOREPLICATION PASSWORD %L',
    role_name, role_password
)
FROM runtime_roles
\gexec

-- Strip every direct membership from these logins. NOINHERIT alone does not
-- prevent SET ROLE, so this removes old/manual paths into an owner or another
-- privileged role before anything connects with a runtime credential.
SELECT format('REVOKE %I FROM %I', granted.rolname, member.rolname)
FROM pg_auth_members memberships
JOIN pg_roles granted ON granted.oid = memberships.roleid
JOIN pg_roles member ON member.oid = memberships.member
WHERE member.rolname IN (
    :'app_role',
    :'worker_role',
    :'assistant_role',
    :'communication_role'
)
\gexec
SQL
}

configure_onebrain_access() {
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname onebrain <<'SQL'
\getenv app_role POSTGRES_APP_ROLE
\getenv worker_role POSTGRES_WORKER_ROLE
\getenv assistant_role POSTGRES_ASSISTANT_ROLE
\getenv communication_role POSTGRES_COMMUNICATION_ROLE

-- No runtime login may inherit database/schema access through PUBLIC. The
-- owner remains the migration-only identity. 0029 grants the detailed table
-- privileges for the app and worker queue roles after this service completes.
SELECT format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM PUBLIC, %I, %I, %I, %I',
    current_database(), :'app_role', :'worker_role', :'assistant_role', :'communication_role'
)
\gexec
SELECT format(
    'GRANT CONNECT ON DATABASE %I TO %I, %I',
    current_database(), :'app_role', :'worker_role'
)
\gexec

REVOKE ALL ON SCHEMA public FROM PUBLIC;
SELECT format(
    'REVOKE ALL ON SCHEMA public FROM %I, %I, %I, %I',
    :'app_role', :'worker_role', :'assistant_role', :'communication_role'
)
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I, %I', :'app_role', :'worker_role')
\gexec

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I, %I',
    :'assistant_role', :'communication_role'
)
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I, %I',
    :'assistant_role', :'communication_role'
)
\gexec
SQL
}

configure_product_access() {
  db_name=$1
  runtime_role=$2
  POSTGRES_RUNTIME_ROLE="$runtime_role" \
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db_name" <<'SQL'
\getenv owner_role POSTGRES_USER
\getenv app_role POSTGRES_APP_ROLE
\getenv worker_role POSTGRES_WORKER_ROLE
\getenv assistant_role POSTGRES_ASSISTANT_ROLE
\getenv communication_role POSTGRES_COMMUNICATION_ROLE
\getenv runtime_role POSTGRES_RUNTIME_ROLE

-- Start from a closed database boundary on every run, then grant exactly the
-- product's runtime login enough access for normal DML. This intentionally
-- revokes historical owner/app/worker cross-product access from the restricted
-- roles; only the migration owner retains cross-product authority.
SELECT format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM PUBLIC, %I, %I, %I, %I',
    current_database(), :'app_role', :'worker_role', :'assistant_role', :'communication_role'
)
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'runtime_role')
\gexec

REVOKE ALL ON SCHEMA public FROM PUBLIC;
SELECT format(
    'REVOKE ALL ON SCHEMA public FROM %I, %I, %I, %I',
    :'app_role', :'worker_role', :'assistant_role', :'communication_role'
)
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'runtime_role')
\gexec

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I, %I, %I, %I',
    :'app_role', :'worker_role', :'assistant_role', :'communication_role'
)
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I, %I, %I, %I',
    :'app_role', :'worker_role', :'assistant_role', :'communication_role'
)
\gexec
SELECT format(
    'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I',
    :'runtime_role'
)
\gexec
SELECT format(
    'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO %I',
    :'runtime_role'
)
\gexec

-- The owner runs every product's migration. Default privileges make tables
-- created by a future migration immediately usable by the runtime login on
-- both a fresh database and an existing volume.
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
    :'owner_role', :'runtime_role'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I',
    :'owner_role', :'runtime_role'
)
\gexec
SQL
}

configure_runtime_logins
configure_onebrain_access
configure_product_access assistant "$POSTGRES_ASSISTANT_ROLE"
configure_product_access communication "$POSTGRES_COMMUNICATION_ROLE"

echo "postgres-init: configured isolated product runtime logins"
