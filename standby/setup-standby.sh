#!/bin/sh
set -e

PGDATA="${PGDATA:-/var/lib/postgresql/data}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  mkdir -p "$PGDATA" && chown -R postgres:postgres "$PGDATA"
  until pg_isready -h postgres_primary -U appuser -d projectdb; do sleep 2; done
  export PGPASSWORD=apppass
  gosu postgres pg_basebackup -h postgres_primary -D "$PGDATA" -U appuser -Fp -Xs -P -R
  echo "primary_conninfo = 'host=postgres_primary user=appuser password=apppass'" >> "$PGDATA/postgresql.auto.conf"
  chmod 700 "$PGDATA"
fi

exec docker-entrypoint.sh postgres -c hot_standby=on