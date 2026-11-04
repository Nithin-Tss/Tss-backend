#!/bin/sh
# Back up PostgreSQL and uploaded files into ./backups, keeping the newest $KEEP_DAYS days.
#   deploy/backup.sh            (run from cron, e.g.  0 2 * * *)
set -eu
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f deploy/docker-compose.yml --env-file deploy/.env"
STAMP=$(date +%Y%m%d-%H%M%S)
KEEP_DAYS=${KEEP_DAYS:-14}
mkdir -p backups

$COMPOSE exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "backups/db-$STAMP.dump"
$COMPOSE exec -T web tar -C /app -czf - media > "backups/media-$STAMP.tar.gz"

# A dump that cannot be listed is not a backup
$COMPOSE exec -T db pg_restore --list < "backups/db-$STAMP.dump" > /dev/null

find backups -type f \( -name 'db-*.dump' -o -name 'media-*.tar.gz' \) -mtime +"$KEEP_DAYS" -delete
echo "Backup written: backups/db-$STAMP.dump, backups/media-$STAMP.tar.gz"
