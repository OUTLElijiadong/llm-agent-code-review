#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."
source lib/common.sh

test_root="$(mktemp -d)"
trap 'rm -rf "$test_root"' EXIT
env_file="$test_root/deploy.env"
printf 'BACKUP_DIR="/srv/prism-persistent-backups"\n' > "$env_file"

unset BACKUP_DIR || true
DEPLOY_ENV_FILE="$env_file"
export DEPLOY_ENV_FILE
[[ "$(configured_backup_dir)" == "/srv/prism-persistent-backups" ]]

BACKUP_DIR="/override/backups"
export BACKUP_DIR
[[ "$(configured_backup_dir)" == "/override/backups" ]]

unset BACKUP_DIR || true
printf 'APP_DOMAIN=example.test\n' > "$env_file"
[[ "$(configured_backup_dir)" == "../backups" ]]

for script in backup.sh verify-backup.sh ops-check.sh deploy.sh; do
  grep -Fq 'configured_backup_dir' "$script" || {
    printf '脚本未使用统一 BACKUP_DIR dotenv 读取逻辑: %s\n' "$script" >&2
    exit 1
  }
done

printf 'backup directory dotenv configuration: PASS\n'
