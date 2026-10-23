#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_dir="$(cd -- "$script_dir/../.." && pwd -P)"
readonly script_dir repo_dir
readonly release_dir="$repo_dir/deploy/.releases"
readonly maintenance_lock="$release_dir/.maintenance.lock"
readonly log_dir="/var/log/prism-maintenance"
readonly retention_minutes=10080
readonly cache_until=168h

usage() {
  printf 'Usage: %s [--apply]\n' "${0##*/}"
  printf 'Default is dry-run; cleanup runs only with explicit --apply.\n'
}

apply=0
case "${1:-}" in
  "") ;;
  --apply) apply=1 ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { usage >&2; exit 2; }

for command_name in awk chmod date docker find flock journalctl mkdir rm touch tr; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'ERROR: required command is unavailable: %s\n' "$command_name" >&2
    exit 1
  }
done

umask 0027
mkdir -p "$log_dir"
run_log="$log_dir/cleanup-$(date -u '+%Y%m%dT%H%M%SZ')-$$.log"
readonly run_log
touch "$run_log"
chmod 0640 "$run_log"
exec >>"$run_log" 2>&1

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

if (( apply )); then
  log 'mode=apply'
else
  log 'mode=dry-run'
fi

exec 9>/run/lock/prism-maintenance.lock
if ! flock -n 9; then
  log 'SKIP: another maintenance cleanup is running'
  exit 0
fi

if [[ ! -d "$release_dir" ]]; then
  log "SKIP: release state directory is missing; cannot coordinate maintenance lock"
  exit 0
fi
lock_owned=0
release_lock() {
  if (( lock_owned )); then
    rmdir "$maintenance_lock" 2>/dev/null || true
    lock_owned=0
  fi
}
if (( apply )); then
  if ! mkdir "$maintenance_lock" 2>/dev/null; then
    log 'SKIP: deployment, backup, rollback, or another maintenance task holds the shared lock'
    exit 0
  fi
  lock_owned=1
  trap release_lock EXIT
elif [[ -e "$maintenance_lock" ]]; then
  log 'SKIP: deployment, backup, rollback, or another maintenance task holds the shared lock'
  exit 0
fi

active_operation() {
  local proc_dir pid comm cmdline
  for proc_dir in /proc/[0-9]*; do
    [[ -d "$proc_dir" ]] || continue
    pid="${proc_dir##*/}"
    [[ "$pid" != "$$" ]] || continue
    IFS= read -r comm <"$proc_dir/comm" 2>/dev/null || continue
    case "$comm" in
      docker|mysqldump|mariadb-dump|pg_dump|rsync) return 0 ;;
    esac
    cmdline="$(tr '\0' ' ' <"$proc_dir/cmdline" 2>/dev/null || true)"
    case "$cmdline" in
      *deploy.sh*|*backup.sh*|*rollback.sh*|*restore.sh*|\
      *'docker build '*|*'docker compose build'*|*'docker buildx build'*)
        return 0
        ;;
    esac
  done
  return 1
}

if active_operation; then
  log 'SKIP: an active deploy, build, or backup process was detected'
  exit 0
fi

summarize_old_files() {
  local directory="$1"
  find "$directory" -xdev -mindepth 1 -maxdepth 1 -type f \
    -name 'prism-maintenance-*.tmp' -links 1 -mmin "+$retention_minutes" -amin "+$retention_minutes" -printf '%s\n' 2>/dev/null |
    awk -v directory="$directory" '
      { count++; bytes += $1 }
      END { printf "%s: files=%d bytes=%d\n", directory, count, bytes }
    '
}

clean_old_files() {
  local directory="$1" candidate
  while IFS= read -r -d '' candidate; do
    if (( apply )); then
      rm -f -- "$candidate"
      log "REMOVED: expired temporary file under $directory"
    else
      log "DRY-RUN: would remove expired temporary file under $directory"
    fi
  done < <(
    find "$directory" -xdev -mindepth 1 -maxdepth 1 -type f \
      -name 'prism-maintenance-*.tmp' -links 1 -mmin "+$retention_minutes" -amin "+$retention_minutes" -print0 2>/dev/null
  )
}

resource_snapshot() {
  df -Pk / | awk 'NR == 2 { printf "root_available_kib=%s root_used_percent=%s\n", $4, $5 }'
  awk '/^MemAvailable:/ { printf "memory_available_kib=%s\n", $2 } /^SwapFree:/ { printf "swap_free_kib=%s\n", $2 }' /proc/meminfo
}

clean_expired_transfer_bundles() {
  local bundle heads revision reference safe fuser_status
  if ! command -v fuser >/dev/null 2>&1; then
    log 'SKIP: fuser is unavailable; source transfer bundles are retained'
    return
  fi
  if [[ "$(git -C "$repo_dir" rev-parse --is-shallow-repository 2>/dev/null)" != false ]]; then
    log 'SKIP: production repository history is incomplete; source transfer bundles are retained'
    return
  fi
  while IFS= read -r -d '' bundle; do
    if fuser -- "$bundle" >/dev/null 2>&1; then
      log 'SKIP: source transfer bundle is in use'
      continue
    else
      fuser_status=$?
      [[ "$fuser_status" == 1 ]] || { log 'SKIP: source transfer bundle usage is uncertain'; continue; }
    fi
    if ! heads="$(git bundle list-heads "$bundle" 2>/dev/null)" || [[ -z "$heads" ]]; then
      log 'SKIP: unrecognized source transfer bundle'
      continue
    fi
    safe=1
    while read -r revision reference; do
      if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]] || ! git -C "$repo_dir" cat-file -e "$revision^{commit}" 2>/dev/null || ! git -C "$repo_dir" merge-base --is-ancestor "$revision" HEAD 2>/dev/null; then
        safe=0
        break
      fi
    done <<< "$heads"
    if (( ! safe )); then
      log 'SKIP: source transfer bundle contains revisions not retained by the current production history'
      continue
    fi
    if (( apply )); then
      rm -f -- "$bundle"
      log 'REMOVED: expired source transfer bundle; all referenced commits remain reachable in the production history'
    else
      log 'DRY-RUN: eligible expired source transfer bundle; all referenced commits remain reachable in the production history'
    fi
  done < <(find /tmp /var/tmp -xdev -mindepth 1 -maxdepth 1 -type f -links 1 \
    \( -name 'prism-v[0-9]*.bundle' -o -name 'prism-[0-9]*.bundle' \) -mtime +14 -print0 2>/dev/null)
}

log 'Resources before cleanup'
resource_snapshot
log "$(summarize_old_files /tmp)"
log "$(summarize_old_files /var/tmp)"
clean_expired_transfer_bundles
if (( apply )); then
  clean_old_files /tmp
  clean_old_files /var/tmp
  log "Pruning reclaimable Docker builder cache older than $cache_until"
  docker builder prune --force --filter "until=$cache_until"
  log 'Vacuuming archived systemd journal entries older than 7 days'
  journalctl --vacuum-time=7d

  find "$log_dir" -maxdepth 1 -type f -name 'cleanup-*.log' \
    -mmin "+$retention_minutes" ! -path "$run_log" -print0 |
    while IFS= read -r -d '' old_log; do
      rm -f -- "$old_log"
    done
  log 'Removed expired maintenance logs; current run log was excluded'
else
  log "DRY-RUN: --apply would remove the listed eligible temp files, builder cache older than $cache_until, archived journal entries older than 7 days, and old maintenance logs"
fi

log 'Resources after cleanup'
resource_snapshot
log 'Maintenance cleanup finished'
