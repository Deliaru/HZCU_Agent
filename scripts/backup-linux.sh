#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="${HZCU_PROJECT_DIR:-/srv/hzcu-agent/current}"
compose_file="${project_dir}/compose.production.yml"
backup_root="/srv/hzcu-agent/backups"
stamp="$(date +%Y%m%dT%H%M%S%z)"
destination="${backup_root}/${stamp}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "backup-linux.sh must run as root" >&2
  exit 1
fi

mkdir -p "$destination"
chmod 700 "$backup_root" "$destination"

cd "$project_dir"
docker compose -f "$compose_file" run --rm --no-deps \
  -v "$destination:/app/backup" api \
  python -c "import sqlite3; source=sqlite3.connect('/app/data/hzcu_agent.db'); target=sqlite3.connect('/app/backup/hzcu_agent.db'); source.backup(target); target.close(); source.close()"

sha256sum "$destination/hzcu_agent.db" > "$destination/SHA256SUMS"
printf 'database backup created: %s\n' "$destination"
