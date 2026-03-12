#!/bin/bash
# 001_schema.sh — Apply schema after TimescaleDB extension is loaded.
#
# Why a .sh and not a .sql:
#   The timescaledb image's 000_install_timescaledb.sh restarts postgres
#   (to load shared_preload_libraries). After that restart the standard
#   postgres entrypoint no longer runs remaining .sql init files.
#   A .sh script is sourced by the entrypoint's for-loop and keeps running
#   even across the restart, so we can wait for postgres to be ready and
#   then apply the schema ourselves.

set -e

PGUSER="${POSTGRES_USER:-botuser}"
PGDB="${POSTGRES_DB:-usdjpy_bot}"

echo "001_schema.sh: waiting for postgres to accept connections..."
until pg_isready -U "$PGUSER" -d "$PGDB" -q; do
    sleep 1
done

echo "001_schema.sh: applying schema from init.sql..."
psql -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB" -f /docker-entrypoint-initdb.d/init.sql
echo "001_schema.sh: schema applied successfully."
