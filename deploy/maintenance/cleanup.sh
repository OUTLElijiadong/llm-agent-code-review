#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_dir="$(cd -- "$script_dir/../.." && pwd -P)"
readonly script_dir repo_dir
readonly release_dir="$repo_dir/deploy/.releases"
readonly maintenance_lock="$release_dir/.maintenance.lock"
# 以下三个路径默认指向生产位置；PRISM_* 覆盖项只供 deploy/tests 把脚本指到临时目录，
# systemd 单元不设置它们。
readonly log_dir="${PRISM_MAINTENANCE_LOG_DIR:-/var/log/prism-maintenance}"
readonly proc_root="${PRISM_PROC_ROOT:-/proc}"
readonly flock_file="${PRISM_MAINTENANCE_FLOCK_FILE:-/run/lock/prism-maintenance.lock}"
readonly retention_minutes=10080
readonly cache_until=168h

# 锁持有者元数据与遗留锁判断复用部署脚本的同一套实现，格式只定义一处。
# shellcheck source=../lib/common.sh
source "$repo_dir/deploy/lib/common.sh"

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

for command_name in awk chmod date docker find flock git journalctl mkdir rm stat touch tr; do
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

# ---- 各清理阶段互相独立 ---------------------------------------------------
# 任一阶段失败只记录“阶段名、退出码、错误摘要”，继续执行后面的阶段；最后汇总，
# 只要有失败阶段整个脚本以非 0 退出，让 systemd 与巡检都能看到。
stages_ok=()
stages_failed=()
stages_skipped=()
# 阶段函数用这个返回码表示“前置条件不满足而有意跳过”（不是失败）。
readonly STAGE_SKIPPED=3

# 运行一个阶段：不让 errexit 吞掉失败，也不让一个阶段的失败中断后面的阶段。
# 参数: $1 阶段名；其余为要执行的命令。
# 返回: 始终返回 0（失败登记在 stages_failed）。
run_stage() {
  local name="$1" rc=0 err_file
  shift
  err_file="$(mktemp "${TMPDIR:-/tmp}/prism-maintenance-stage.XXXXXX")"
  # 注意：在 `cmd || ...` 里调用的函数，bash 会整体忽略其中的 set -e。因此各阶段函数
  # 必须自己用 `|| return $?` / 失败计数把失败传出来，不能依赖 errexit。
  "$@" 2>"$err_file" || rc=$?
  if [[ -s "$err_file" ]]; then
    while IFS= read -r line; do log "[$name] $line"; done < <(head -n 20 "$err_file")
  fi
  rm -f "$err_file"
  if (( rc == 0 )); then
    stages_ok+=("$name")
    log "STAGE ok: $name"
  elif (( rc == STAGE_SKIPPED )); then
    stages_skipped+=("$name")
    log "STAGE skipped: $name"
  else
    stages_failed+=("$name(exit=$rc)")
    log "STAGE FAILED: $name exit=$rc"
  fi
  return 0
}

# ---- Docker CLI 的配置目录 -------------------------------------------------
# prism-maintenance.service 带 ProtectHome=true，/root 对进程是只读空目录，docker buildx
# 在 $HOME/.docker 建状态目录失败："mkdir /root/.docker: read-only file system"
# （生产 2026-10-24 至 10-30 连续 7 次因此失败，已在生产用 systemd-run 复现）。
# unit 通过 RuntimeDirectory 提供可写目录并设置 DOCKER_CONFIG；手工运行时若没有设置，
# 这里用私有临时目录兜底，退出时清理。
docker_config_tmp=""
ensure_docker_config() {
  if [[ -n "${DOCKER_CONFIG:-}" ]]; then
    mkdir -p -- "$DOCKER_CONFIG" 2>/dev/null || true
    return 0
  fi
  docker_config_tmp="$(mktemp -d "${TMPDIR:-/tmp}/prism-docker-config.XXXXXX")"
  export DOCKER_CONFIG="$docker_config_tmp"
}

# ---- 并发保护 ---------------------------------------------------------------
exec 9>"$flock_file"
if ! flock -n 9; then
  log 'SKIP: another maintenance cleanup is running'
  exit 0
fi

if [[ ! -d "$release_dir" ]]; then
  log "SKIP: release state directory is missing; cannot coordinate maintenance lock"
  exit 0
fi

lock_owned=0
cleanup_on_exit() {
  if (( lock_owned )); then
    release_directory_lock "$maintenance_lock"
    lock_owned=0
  fi
  if [[ -n "$docker_config_tmp" && "$docker_config_tmp" == "${TMPDIR:-/tmp}"/prism-docker-config.* ]]; then
    rm -rf -- "$docker_config_tmp"
  fi
}
trap cleanup_on_exit EXIT

# 共享维护锁（与发布、备份、恢复、回滚共用）被占用时的处理：
#   持有者仍在运行            -> 正常跳过，下次再来；
#   持有者已不存在（遗留锁）  -> 失败并给出确认后的手工处理命令，不自动删除；
#   无法判断持有者且锁存在很久 -> 同样视为可疑遗留锁而失败，避免永远静默跳过。
# 参数: 无。返回: 不返回（exit 0 或 exit 1）。
readonly lock_stale_seconds="${PRISM_LOCK_STALE_SECONDS:-21600}"
shared_lock_busy() {
  local owner_text alive_rc=0 lock_mtime now age
  owner_text="$(describe_lock_owner "$maintenance_lock")"
  lock_owner_alive "$maintenance_lock" || alive_rc=$?
  if (( alive_rc == 0 )); then
    log "SKIP: deployment, backup, rollback, or another maintenance task holds the shared lock (${owner_text})"
    exit 0
  fi
  if (( alive_rc == 1 )); then
    log "ERROR: shared maintenance lock is a leftover (${owner_text}); after confirming no deploy, backup, restore or rollback is running, remove it with: rmdir '${maintenance_lock}'"
    exit 1
  fi
  lock_mtime="$(path_mtime_epoch "$maintenance_lock" || true)"
  now="$(date -u '+%s')"
  if [[ "$lock_mtime" =~ ^[0-9]+$ ]] && (( now - lock_mtime > lock_stale_seconds )); then
    age=$((now - lock_mtime))
    log "ERROR: shared maintenance lock has existed for ${age}s and its owner cannot be confirmed (${owner_text}); after confirming no deploy, backup, restore or rollback is running, remove it with: rmdir '${maintenance_lock}'"
    exit 1
  fi
  log "SKIP: deployment, backup, rollback, or another maintenance task holds the shared lock (${owner_text})"
  exit 0
}

if (( apply )); then
  if ! mkdir "$maintenance_lock" 2>/dev/null; then
    shared_lock_busy
  fi
  lock_owned=1
  write_lock_owner "$maintenance_lock"
elif [[ -e "$maintenance_lock" ]]; then
  shared_lock_busy
fi

active_operation() {
  local proc_dir pid comm cmdline
  for proc_dir in "$proc_root"/[0-9]*; do
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

resource_snapshot() {
  df -Pk / | awk 'NR == 2 { printf "root_available_kib=%s root_used_percent=%s\n", $4, $5 }' || true
  awk '/^MemAvailable:/ { printf "memory_available_kib=%s\n", $2 } /^SwapFree:/ { printf "swap_free_kib=%s\n", $2 }' \
    "$proc_root/meminfo" 2>/dev/null || true
}

# ---- 诊断信息 ---------------------------------------------------------------
log "diagnostics: user=$(id -un 2>/dev/null || echo unknown) HOME=${HOME:-unset} DOCKER_CONFIG=${DOCKER_CONFIG:-unset}"
ensure_docker_config
log "diagnostics: docker_config=${DOCKER_CONFIG:-unset} docker_client=$(docker version --format '{{.Client.Version}}' 2>&1 | head -n 1 | cut -c1-80)"

# ---- 阶段 1：过期源码传输包 -----------------------------------------------
# 读取仓库的所有 git 调用统一走 repo_git，并显式声明 safe.directory：
# 维护 unit 带 ProtectHome=true 时 root 的 ~/.gitconfig 不可见，而 /opt/code-review 的属主是
# uid 501，git 会报 "detected dubious ownership"，旧脚本把这个报错当成“仓库历史不完整”，
# 导致传输包清理在生产一直被跳过（已用 systemd-run 在生产复现；加 -c safe.directory 后正常）。
repo_git() {
  git -c "safe.directory=$repo_dir" -C "$repo_dir" "$@"
}

clean_expired_transfer_bundles() {
  local bundle heads revision reference safe fuser_status original_stat shallow shallow_err
  local bundle_failures=0
  if ! command -v fuser >/dev/null 2>&1; then
    log 'SKIP: fuser is unavailable; source transfer bundles are retained'
    return 0
  fi
  shallow_err="$(mktemp "${TMPDIR:-/tmp}/prism-maintenance-git.XXXXXX")"
  if ! shallow="$(repo_git rev-parse --is-shallow-repository 2>"$shallow_err")" \
    || [[ "$shallow" != true && "$shallow" != false ]]; then
    # 读取失败不等于浅克隆：明确写出原因，便于区分。
    log "SKIP: cannot read repository state; source transfer bundles are retained (reason: $(head -c 200 "$shallow_err" | tr '\n' ' '))"
    rm -f "$shallow_err"
    return 0
  fi
  rm -f "$shallow_err"
  if [[ "$shallow" == true ]]; then
    log 'SKIP: production repository history is incomplete (shallow clone); source transfer bundles are retained'
    return 0
  fi
  while IFS= read -r -d '' bundle; do
    original_stat="$(stat -c '%d:%i:%s:%Y:%h' -- "$bundle" 2>/dev/null)" || continue
    if fuser -- "$bundle" >/dev/null 2>&1; then
      log 'SKIP: source transfer bundle is in use'
      continue
    else
      fuser_status=$?
      [[ "$fuser_status" == 1 ]] || { log 'SKIP: source transfer bundle usage is uncertain'; continue; }
    fi
    if ! heads="$(repo_git bundle list-heads "$bundle" 2>/dev/null)" || [[ -z "$heads" ]]; then
      log 'SKIP: unrecognized source transfer bundle'
      continue
    fi
    safe=1
    while read -r revision reference; do
      if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]] || ! repo_git cat-file -e "$revision^{commit}" 2>/dev/null || ! repo_git merge-base --is-ancestor "$revision" HEAD 2>/dev/null; then
        safe=0
        break
      fi
    done <<< "$heads"
    if (( ! safe )); then
      log 'SKIP: source transfer bundle contains revisions not retained by the current production history'
      continue
    fi
    # Validate again after inspecting the bundle, immediately before unlinking.
    if [[ ! -f "$bundle" || -L "$bundle" || "$(stat -c '%d:%i:%s:%Y:%h' -- "$bundle" 2>/dev/null)" != "$original_stat" ]]; then
      log 'SKIP: source transfer bundle changed during inspection'
      continue
    fi
    if fuser -- "$bundle" >/dev/null 2>&1; then
      log 'SKIP: source transfer bundle became active during inspection'
      continue
    else
      fuser_status=$?
      [[ "$fuser_status" == 1 ]] || { log 'SKIP: source transfer bundle usage is uncertain'; continue; }
    fi
    if (( apply )); then
      if rm -f -- "$bundle"; then
        log 'REMOVED: expired source transfer bundle; all referenced commits remain reachable in the production history'
      else
        printf 'could not remove an expired source transfer bundle\n' >&2
        bundle_failures=$((bundle_failures + 1))
      fi
    else
      log 'DRY-RUN: eligible expired source transfer bundle; all referenced commits remain reachable in the production history'
    fi
  done < <(find "${PRISM_BUNDLE_SEARCH_DIRS_FIRST:-/tmp}" "${PRISM_BUNDLE_SEARCH_DIRS_SECOND:-/var/tmp}" -xdev -mindepth 1 -maxdepth 1 -type f -links 1 \
    \( -name 'prism-v[0-9]*.bundle' -o -name 'prism-[0-9]*.bundle' \) -mtime +14 -print0 2>/dev/null)
  (( bundle_failures == 0 )) || return 1
  return 0
}

# ---- 阶段 2/3：Docker 构建缓存、journal ------------------------------------
prune_builder_cache() {
  log "Pruning reclaimable Docker builder cache older than $cache_until"
  docker builder prune --force --filter "until=$cache_until" || return $?
}

vacuum_journal() {
  log 'Vacuuming archived systemd journal entries older than 7 days'
  journalctl --vacuum-time=7d || return $?
}

# ---- 阶段 4：过期维护日志 ---------------------------------------------------
remove_expired_logs() {
  local old_log failures=0
  while IFS= read -r -d '' old_log; do
    rm -f -- "$old_log" || failures=$((failures + 1))
  done < <(find "$log_dir" -maxdepth 1 -type f -name 'cleanup-*.log' \
    -mmin "+$retention_minutes" ! -path "$run_log" -print0)
  (( failures == 0 )) || { printf 'could not remove %s expired maintenance log(s)\n' "$failures" >&2; return 1; }
  log 'Removed expired maintenance logs; current run log was excluded'
}

log 'Resources before cleanup'
resource_snapshot
run_stage transfer_bundles clean_expired_transfer_bundles
if (( apply )); then
  run_stage docker_builder_cache prune_builder_cache
  run_stage journal_vacuum vacuum_journal
  run_stage maintenance_logs remove_expired_logs
else
  log "DRY-RUN: --apply would remove the eligible source transfer bundles, builder cache older than $cache_until, archived journal entries older than 7 days, and old maintenance logs"
fi

log 'Resources after cleanup'
resource_snapshot

# ---- 汇总 -----------------------------------------------------------------
log "SUMMARY ok=${#stages_ok[@]} failed=${#stages_failed[@]} skipped=${#stages_skipped[@]}"
if (( ${#stages_failed[@]} > 0 )); then
  log "SUMMARY failed stages: $(IFS=' '; printf '%s' "${stages_failed[*]}")"
  log 'Maintenance cleanup finished with failures'
  exit 1
fi
log 'Maintenance cleanup finished'
