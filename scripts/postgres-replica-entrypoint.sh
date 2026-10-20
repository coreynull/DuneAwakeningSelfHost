#!/usr/bin/env sh
set -eu

pgdata="${PGDATA:-/var/lib/postgresql/data}"
primary_host="${POSTGRES_PRIMARY_HOST:-postgres}"
primary_port="${POSTGRES_PRIMARY_PORT:-5432}"
slot="${POSTGRES_REPLICATION_SLOT:-dune_standby}"
repl_user="${POSTGRES_REPLICATION_USER:-dune_replicator}"
repl_password="${POSTGRES_REPLICATION_PASSWORD:-}"
repl_password_file="${POSTGRES_REPLICATION_PASSWORD_FILE:-}"

if [ -n "$repl_password_file" ]; then
  if [ ! -r "$repl_password_file" ]; then
    echo "POSTGRES_REPLICATION_PASSWORD_FILE is not readable" >&2
    exit 1
  fi
  repl_password="$(cat "$repl_password_file")"
fi

if [ -z "$repl_password" ]; then
  echo "POSTGRES_REPLICATION_PASSWORD is required" >&2
  exit 1
fi

mkdir -p "$pgdata"
chown -R postgres:postgres "$pgdata"
chmod 700 "$pgdata"

install -d -m 0700 -o postgres -g postgres /run/postgresql
pgpass_file="$(mktemp /run/postgresql/.replica-pgpass.XXXXXX)"
escape_pgpass_field() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/:/\\:/g'
}
escaped_primary_host="$(escape_pgpass_field "$primary_host")"
escaped_primary_port="$(escape_pgpass_field "$primary_port")"
escaped_repl_user="$(escape_pgpass_field "$repl_user")"
escaped_repl_password="$(escape_pgpass_field "$repl_password")"
printf '%s:%s:replication:%s:%s\n' \
  "$escaped_primary_host" "$escaped_primary_port" "$escaped_repl_user" "$escaped_repl_password" \
  > "$pgpass_file"
chown postgres:postgres "$pgpass_file"
chmod 0600 "$pgpass_file"
export PGPASSFILE="$pgpass_file"
unset POSTGRES_REPLICATION_PASSWORD POSTGRES_REPLICATION_PASSWORD_FILE
unset repl_password repl_password_file escaped_repl_password

if [ ! -s "$pgdata/PG_VERSION" ]; then
  echo "initializing standby from ${primary_host}:${primary_port} slot ${slot}"
  rm -rf "$pgdata"/*
  until pg_isready -h "$primary_host" -p "$primary_port" -U "$repl_user"; do
    sleep 2
  done
  gosu postgres pg_basebackup \
    -h "$primary_host" \
    -p "$primary_port" \
    -U "$repl_user" \
    -D "$pgdata" \
    -Fp \
    -Xs \
    -P \
    -R \
    -S "$slot"
fi

exec gosu postgres postgres -D "$pgdata"
