set -e

echo "Configuring PostgreSQL for replication..."

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER replicator WITH REPLICATION ENCRYPTED PASSWORD '${POSTGRES_REPLICATION_PASSWORD}';
    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO replicator;
EOSQL

cat >> "$PGDATA/pg_hba.conf" <<EOF

# Replication connections
host    replication     replicator      postgres-shop-replica        md5
host    replication     replicator      0.0.0.0/0                       md5
EOF

echo "Replication configuration complete!"

