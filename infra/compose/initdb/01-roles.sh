#!/bin/sh
# Runs once when the Postgres volume is first created.
# documind_owner:  owns schema + tables, runs migrations.
# documind_app:    API runtime role. NOT owner, NOT superuser, NO BYPASSRLS -> RLS always applies.
# documind_worker: ingestion runtime role. Same restrictions; sees one tenant per job (ADR-0005).
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
CREATE ROLE documind_owner LOGIN PASSWORD '${DOCUMIND_OWNER_PASSWORD}';
CREATE ROLE documind_app LOGIN PASSWORD '${DOCUMIND_APP_PASSWORD}' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE ROLE documind_worker LOGIN PASSWORD '${DOCUMIND_WORKER_PASSWORD}' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE SCHEMA documind AUTHORIZATION documind_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA documind TO documind_app, documind_worker;
ALTER ROLE documind_owner SET search_path = documind;
ALTER ROLE documind_app SET search_path = documind;
ALTER ROLE documind_worker SET search_path = documind;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA documind;
EOSQL
