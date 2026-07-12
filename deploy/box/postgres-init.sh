#!/bin/sh
# A13: one Postgres server, per-product databases. Runs from
# /docker-entrypoint-initdb.d on first init as the POSTGRES_USER superuser. Creates
# the three product databases so onebrain and assistant (both alembic, independent
# lineages) never share an alembic_version table, and communication's pnpm-migrated
# schema is isolated. These names MUST equal the Phase-6 pg_restore targets.
set -eu

for db in onebrain assistant communication; do
  if ! psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_USER" \
        -tAc "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1; then
    createdb --username "$POSTGRES_USER" "$db"
    echo "postgres-init: created database $db"
  fi
done
