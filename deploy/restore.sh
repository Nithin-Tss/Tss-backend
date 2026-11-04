#!/bin/sh
# Restore a backup made by backup.sh. Replaces the current database contents.
#   deploy/restore.sh backups/db-YYYYMMDD-HHMMSS.dump [backups/media-YYYYMMDD-HHMMSS.tar.gz]
set -eu
cd "$(dirname "$0")/.."
[ $# -ge 1 ] || { echo "usage: $0 <db.dump> [media.tar.gz]" >&2; exit 1; }

COMPOSE="docker compose -f deploy/docker-compose.yml --env-file deploy/.env"
printf "This REPLACES the current database. Type 'restore' to continue: "
read -r answer
[ "$answer" = "restore" ] || { echo "Cancelled."; exit 1; }

$COMPOSE stop web worker beat
$COMPOSE exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < "$1"
if [ $# -ge 2 ]; then
  $COMPOSE run --rm -T --no-deps web tar -C /app -xzf - < "$2"
fi
$COMPOSE start web worker beat
echo "Restore finished."
