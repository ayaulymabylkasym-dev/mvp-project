#!/bin/sh
set -e

# pg_hba.conf файлына репликацияға рұқсат қосу
echo "host replication appuser all md5" >> "$PGDATA/pg_hba.conf"

# appuser қолданушысына репликация (REPLICATION) құқығын беру
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    ALTER USER appuser WITH REPLICATION;
EOSQL