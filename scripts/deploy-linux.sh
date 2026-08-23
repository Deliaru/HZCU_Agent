#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="${HZCU_PROJECT_DIR:-/srv/hzcu-agent/current}"
compose_file="${project_dir}/compose.production.yml"
data_dir="/srv/hzcu-agent/data"
backup_dir="/srv/hzcu-agent/backups"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "deploy-linux.sh must run as root" >&2
  exit 1
fi

for required in "$compose_file" "$data_dir/hzcu_agent.db" \
  /etc/hzcu-agent/model_config.secret /etc/hzcu-agent/auth_session.secret; do
  if [[ ! -e "$required" ]]; then
    echo "missing required deployment input: $required" >&2
    exit 1
  fi
done

mkdir -p "$backup_dir"
chmod 700 /etc/hzcu-agent "$backup_dir"
chmod 600 /etc/hzcu-agent/model_config.secret /etc/hzcu-agent/auth_session.secret

cd "$project_dir"
docker compose -f "$compose_file" build
docker compose -f "$compose_file" run --rm migrate
docker compose -f "$compose_file" up -d --remove-orphans
docker compose -f "$compose_file" ps
