#!/usr/bin/env bash
# 安全清理旧 release 镜像、构建缓存和历史状态；默认仅预览。
set -Eeuo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib/common.sh
source "lib/common.sh"

# 输出命令帮助。
# 参数: 无。
# 返回: 始终返回 0。
usage() {
  cat <<'USAGE'
用法: ./cleanup.sh [--apply] [--keep-release-images N] [--keep-release-states N]
                    [--cache-until DURATION]

默认 dry-run。只有显式 --apply 才会删除：
  1. 当前/上一 release 和运行容器之外的旧 prism-backend/prism-frontend tag；
  2. 超出保留数的 rollback-from-*.env 历史状态；
  3. 指定年龄以前的 dangling 镜像与 Docker builder cache。

如果 sandbox/.env 配置了按 digest 固定的 Playwright 镜像，脚本会先建立
本地保护 tag，再执行 dangling 镜像清理。本脚本不会删除数据库卷、
证书、备份、current.env、previous.env 或 pending.env。
USAGE
}

apply=0
keep_release_images="${CLEANUP_KEEP_RELEASE_IMAGES:-2}"
keep_release_states="${CLEANUP_KEEP_RELEASE_STATES:-10}"
cache_until="${CLEANUP_CACHE_UNTIL:-168h}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      apply=1
      shift
      ;;
    --keep-release-images)
      [[ $# -ge 2 ]] || fatal "--keep-release-images 缺少值"
      keep_release_images="$2"
      shift 2
      ;;
    --keep-release-states)
      [[ $# -ge 2 ]] || fatal "--keep-release-states 缺少值"
      keep_release_states="$2"
      shift 2
      ;;
    --cache-until)
      [[ $# -ge 2 ]] || fatal "--cache-until 缺少值"
      cache_until="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fatal "未知参数: $1"
      ;;
  esac
done

[[ "$keep_release_images" =~ ^[0-9]+$ ]] || fatal "镜像保留数必须是非负整数"
[[ "$keep_release_states" =~ ^[0-9]+$ ]] || fatal "状态保留数必须是非负整数"
[[ "$cache_until" =~ ^[0-9]+[smh]$ ]] || fatal "缓存年龄格式必须类似 168h"
require_commands docker find sort awk

docker compose version >/dev/null 2>&1 || fatal "docker compose 不可用"
release_dir="${RELEASE_STATE_DIR:-.releases}"
process_root="${PROC_ROOT:-/proc}"
current_state="$release_dir/current.env"
previous_state="$release_dir/previous.env"
lock_dir="$(maintenance_lock_path)"
if [[ "$apply" == "1" ]]; then
  mkdir -p "$release_dir"
  acquire_directory_lock "$lock_dir"
  trap 'release_directory_lock "$lock_dir"' EXIT
fi
current_backend=""
current_frontend=""
previous_backend=""
previous_frontend=""
if [[ -f "$current_state" ]]; then
  current_backend="$(read_release_value "$current_state" BACKEND_RELEASE || true)"
  current_frontend="$(read_release_value "$current_state" FRONTEND_RELEASE || true)"
fi
if [[ -f "$previous_state" ]]; then
  previous_backend="$(read_release_value "$previous_state" BACKEND_RELEASE || true)"
  previous_frontend="$(read_release_value "$previous_state" FRONTEND_RELEASE || true)"
fi

# 以 dry-run 文本展示命令，或在 --apply 时真正执行。
# 参数: 待执行命令及参数。
# 返回: 命令执行状态或 dry-run 的 0。
run_mutation() {
  if [[ "$apply" == "1" ]]; then
    "$@"
    return $?
  fi
  printf 'DRY-RUN'
  printf ' %q' "$@"
  printf '\n'
}

# 判断镜像 tag 是否属于 current/previous release。
# 参数: $1 仓库；$2 tag。
# 返回: 受保护时 0，否则 1。
is_state_protected_tag() {
  local repository="$1"
  local tag="$2"
  case "$repository" in
    prism-backend)
      [[ -n "$current_backend" && "$tag" == "$current_backend" ]] \
        || [[ -n "$previous_backend" && "$tag" == "$previous_backend" ]]
      ;;
    prism-frontend)
      [[ -n "$current_frontend" && "$tag" == "$current_frontend" ]] \
        || [[ -n "$previous_frontend" && "$tag" == "$previous_frontend" ]]
      ;;
    *)
      return 1
      ;;
  esac
}

# 获取固定容器当前运行镜像 ID。
# 参数: $1 容器名。
# 返回: stdout 输出镜像 ID；容器不存在时为空。
running_image_id() {
  docker inspect --format '{{.Image}}' "$1" 2>/dev/null || true
}

# 清理单个 release 镜像仓库中的旧 tag。
# 参数: $1 仓库；$2 当前运行镜像 ID。
# 返回: 始终返回 0，删除失败会由 set -e 中止。
cleanup_repository_tags() {
  local repository="$1"
  local running_id="$2"
  local tag candidate_id unprotected_seen=0
  while IFS= read -r tag; do
    [[ -n "$tag" && "$tag" != "<none>" ]] || continue
    if is_state_protected_tag "$repository" "$tag"; then
      log_info "保留状态引用镜像: $repository:$tag"
      continue
    fi
    candidate_id="$(docker image inspect --format '{{.Id}}' "$repository:$tag" 2>/dev/null || true)"
    if [[ -n "$running_id" && -n "$candidate_id" && "$candidate_id" == "$running_id" ]]; then
      log_info "保留运行中镜像: $repository:$tag"
      continue
    fi
    unprotected_seen="$((unprotected_seen + 1))"
    if (( unprotected_seen <= keep_release_images )); then
      log_info "保留最近镜像: $repository:$tag"
      continue
    fi
    run_mutation docker image rm "$repository:$tag"
  done < <(docker image ls "$repository" --format '{{.Tag}}' 2>/dev/null || true)
}

# 清理超出保留数的回滚历史状态文件。
# 参数: 无。
# 返回: 始终返回 0，删除失败会由 set -e 中止。
cleanup_release_history() {
  local seen=0 state_file
  [[ -d "$release_dir" ]] || return 0
  while IFS= read -r state_file; do
    [[ -n "$state_file" ]] || continue
    seen="$((seen + 1))"
    if (( seen <= keep_release_states )); then
      log_info "保留历史状态: $(basename "$state_file")"
      continue
    fi
    run_mutation rm -f -- "$state_file"
  done < <(find "$release_dir" -maxdepth 1 -type f -name 'rollback-from-*.env' -print | sort -r)
}

# 已建立保护引用的摘要，避免进程环境和单元环境重复打 tag。
protected_playwright_digests=""

# 对一组固定的 Playwright 镜像配置建立按摘要区分的保护 tag。
# 参数: $1 完整镜像引用；$2 sha256 摘要；$3 配置来源。
# 返回: 配置完整且镜像存在时返回 0，否则失败关闭。
protect_playwright_reference() {
  local browser_image="$1"
  local browser_digest="$2"
  local source_name="$3"
  local digest_hex protected_tag
  if [[ -z "$browser_image" && -z "$browser_digest" ]]; then
    return 1
  fi
  [[ "$browser_digest" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fatal "$source_name 的 PLAYWRIGHT_IMAGE_DIGEST 必须是完整 sha256 摘要"
  [[ "$browser_image" == *"@$browser_digest" ]] \
    || fatal "$source_name 的 PLAYWRIGHT_IMAGE 必须与 PLAYWRIGHT_IMAGE_DIGEST 固定到同一摘要"
  case " $protected_playwright_digests " in
    *" $browser_digest "*) return 0 ;;
  esac
  docker image inspect "$browser_image" >/dev/null 2>&1 \
    || fatal "$source_name 固定的 Playwright 镜像不存在，拒绝执行任何清理"
  digest_hex="${browser_digest#sha256:}"
  protected_tag="prism-sandbox-playwright:protected-${digest_hex}"
  log_info "保护 Playwright 镜像(source=$source_name, tag=$protected_tag)"
  run_mutation docker image tag "$browser_image" "$protected_tag"
  protected_playwright_digests="$protected_playwright_digests $browser_digest"
  return 0
}

# 从当前运行进程的 NUL 分隔环境中读取精确键值。
# 参数: $1 键名；$2 进程 PID。
# 返回: 找到时输出值，否则非 0。
read_process_env_value() {
  local key="$1"
  local pid="$2"
  tr '\0' '\n' < "$process_root/$pid/environ" \
    | awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print; found=1; exit } END { exit !found }'
}

# 保护运行中进程实际使用的镜像，以及 systemd 下次启动要使用的镜像。
# 参数: 无；SANDBOX_ENV_FILE 可显式指定候选环境文件。
# 返回: 未配置 Playwright 时返回 0；半配置或镜像缺失时失败关闭。
protect_playwright_images() {
  local sandbox_env=""
  local systemd_env=""
  local main_pid=""
  local browser_image=""
  local browser_digest=""
  local protected_source_count=0
  if command -v systemctl >/dev/null 2>&1; then
    main_pid="$(systemctl show prism-sandbox-executor.service \
      --property=MainPID --value 2>/dev/null | awk 'NR == 1 { print $1 }' || true)"
    if [[ "$main_pid" =~ ^[1-9][0-9]*$ ]]; then
      [[ -r "$process_root/$main_pid/environ" ]] \
        || fatal "运行中沙箱执行器的进程环境不可读，拒绝执行任何清理: pid=$main_pid"
      browser_image="$(read_process_env_value PLAYWRIGHT_IMAGE "$main_pid" || true)"
      browser_digest="$(read_process_env_value PLAYWRIGHT_IMAGE_DIGEST "$main_pid" || true)"
      protect_playwright_reference "$browser_image" "$browser_digest" "running-process:$main_pid" \
        || fatal "运行中沙箱执行器缺少 Playwright 固定镜像配置，拒绝执行任何清理: pid=$main_pid"
      protected_source_count="$((protected_source_count + 1))"
    fi
    systemd_env="$(systemctl show prism-sandbox-executor.service \
      --property=EnvironmentFiles --value 2>/dev/null | awk 'NR == 1 { print $1 }' || true)"
  fi
  if [[ -n "${SANDBOX_ENV_FILE:-}" ]]; then
    sandbox_env="$SANDBOX_ENV_FILE"
    [[ -f "$sandbox_env" ]] || fatal "显式指定的沙箱环境文件不存在: $sandbox_env"
  elif [[ -f "sandbox/.env" ]]; then
    sandbox_env="sandbox/.env"
  elif [[ "$systemd_env" == /* && -f "$systemd_env" ]]; then
    sandbox_env="$systemd_env"
    log_info "使用沙箱 systemd 单元环境文件: $sandbox_env"
  fi
  if [[ -n "$sandbox_env" ]]; then
    browser_image="$(read_env_value PLAYWRIGHT_IMAGE "$sandbox_env" || true)"
    browser_digest="$(read_env_value PLAYWRIGHT_IMAGE_DIGEST "$sandbox_env" || true)"
    if protect_playwright_reference "$browser_image" "$browser_digest" "env-file:$sandbox_env"; then
      protected_source_count="$((protected_source_count + 1))"
    fi
  fi
  if [[ "$protected_source_count" == "0" ]]; then
    log_info "当前进程和单元均未配置 Playwright 镜像，跳过保护"
  fi
}

# 保护必须先于任何删除。这样半配置或镜像缺失时，整个清理交易保持零删除。
protect_playwright_images

if [[ ! -f "$current_state" ]]; then
  log_warn "缺少 current.env，无法证明历史 tag 非当前版本；跳过 tagged release 镜像清理"
else
  cleanup_repository_tags prism-backend "$(running_image_id cr_backend)"
  cleanup_repository_tags prism-frontend "$(running_image_id cr_frontend)"
fi

cleanup_release_history
run_mutation docker image prune --force --filter "until=$cache_until"
run_mutation docker builder prune --force --filter "until=$cache_until"

if [[ "$apply" == "1" ]]; then
  log_info "受控清理完成"
else
  log_info "dry-run 完成；确认候选项后使用 --apply"
fi
