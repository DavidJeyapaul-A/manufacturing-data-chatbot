#!/bin/bash
# Creates the SELECT-only role used by the chatbot's query path.
#
# Postgres runs everything in /docker-entrypoint-initdb.d exactly once: on the
# FIRST start against an empty data volume. The tables do not exist yet at this
# point, so this script creates only the ROLE and its default privileges; the
# GRANT SELECT on actual tables happens in seed_data.py, right after it creates
# them.
#
# To change the readonly password later, either `docker compose down -v` (wipes
# all data) or run ALTER ROLE by hand — re-running this file is not enough.
set -euo pipefail

: "${APP_READONLY_USER:?APP_READONLY_USER is not set}"
: "${APP_READONLY_PASSWORD:?APP_READONLY_PASSWORD is not set}"

echo "[db-init] creating readonly role '${APP_READONLY_USER}'"

psql -v ON_ERROR_STOP=1 \
     --username "${POSTGRES_USER}" \
     --dbname "${POSTGRES_DB}" \
     -v ro_user="${APP_READONLY_USER}" \
     -v ro_password="${APP_READONLY_PASSWORD}" \
     -v db_name="${POSTGRES_DB}" \
     -v admin_user="${POSTGRES_USER}" <<-'EOSQL'
    -- LOGIN, but no CREATEDB / CREATEROLE / SUPERUSER and no write privilege
    -- anywhere. NOINHERIT stops it picking up privileges via role membership.
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user') THEN
            EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT PASSWORD %L',
                           :'ro_user', :'ro_password');
        ELSE
            EXECUTE format('ALTER ROLE %I LOGIN NOINHERIT PASSWORD %L',
                           :'ro_user', :'ro_password');
        END IF;
    END
    $$;

    -- See the database and the schema, but nothing inside them yet.
    GRANT CONNECT ON DATABASE :"db_name" TO :"ro_user";
    GRANT USAGE   ON SCHEMA   public     TO :"ro_user";

    -- No creating objects in public. PG 15+ already defaults this way for
    -- non-owners; say it explicitly so the guarantee is readable and survives
    -- a version change.
    REVOKE CREATE ON SCHEMA public FROM :"ro_user";
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;

    -- Safety net: anything the admin creates later is automatically readable,
    -- and only readable, by the chatbot role. seed_data.py grants explicitly
    -- as well, so a table added by hand later is still covered.
    ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
        GRANT SELECT ON TABLES TO :"ro_user";
    ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
        GRANT SELECT ON SEQUENCES TO :"ro_user";
EOSQL

echo "[db-init] role '${APP_READONLY_USER}' ready: CONNECT + USAGE + SELECT only."
