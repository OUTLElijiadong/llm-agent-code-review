#!/usr/bin/env bash
# 渲染并安装 Prism systemd service/timer；默认只预览。
set -Eeuo pipefail

cd "$(dirname "$0")"

# 输出命令帮助。
# 参数: 无。
# 返回: 始终返回 0。
usage() {
  cat <<'USAGE'
用法: ./install.sh [--apply] [--deploy-dir DIR] [--unit-dir DIR]

默认 dry-run；--apply 时需要 root，并会安装单元、重启运维执行器、启用 Prism 运维 timer。
USAGE
}

apply=0
deploy_dir="$(cd .. && pwd -P)"
unit_dir="/etc/systemd/system"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      apply=1
      shift
      ;;
    --deploy-dir)
      [[ $# -ge 2 ]] || { printf '%s\n' '--deploy-dir 缺少值' >&2; exit 2; }
      deploy_dir="$2"
      shift 2
      ;;
    --unit-dir)
      [[ $# -ge 2 ]] || { printf '%s\n' '--unit-dir 缺少值' >&2; exit 2; }
      unit_dir="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '未知参数: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

deploy_dir="$(cd "$deploy_dir" && pwd -P)"

# 转义 sed 替换文本中的特殊字符。
# 参数: $1 原始文本。
# 返回: stdout 输出可用于 | 分隔替换式的文本。
sed_replacement_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//&/\\&}"
  value="${value//|/\\|}"
  printf '%s' "$value"
}

# 渲染单个 service 模板。
# 参数: $1 模板；$2 输出路径。
# 返回: sed 的执行状态。
render_service() {
  local template="$1"
  local output="$2"
  sed "s|@DEPLOY_DIR@|$(sed_replacement_escape "$deploy_dir")|g" "$template" > "$output"
}

services=(prism-backup.service prism-verify-backup.service prism-ops-check.service prism-ops-executor.service prism-cert-renew.service prism-security-block.service)
timers=(prism-backup.timer prism-verify-backup.timer prism-ops-check.timer prism-cert-renew.timer prism-security-block.timer)
if [[ "$apply" != "1" ]]; then
  printf 'DRY-RUN deploy_dir=%s unit_dir=%s\n' "$deploy_dir" "$unit_dir"
  printf '将安装 service: %s\n' "${services[*]}"
  printf '将安装 timer: %s\n' "${timers[*]}"
  printf '将启用 timer: %s\n' "${timers[*]}"
  exit 0
fi

[[ "$EUID" -eq 0 ]] || { printf '%s\n' '--apply 必须以 root 执行' >&2; exit 1; }
for command_name in cp cut getent groupadd install mktemp readlink rm sed systemctl; do
  command -v "$command_name" >/dev/null 2>&1 || { printf '缺少命令: %s\n' "$command_name" >&2; exit 1; }
done
if ! getent group prism-ops >/dev/null 2>&1; then
  groupadd --system --gid 991 prism-ops
fi
[[ "$(getent group prism-ops | cut -d: -f3)" == "991" ]] || {
  printf '%s\n' 'prism-ops 组已存在但 GID 不是 991，拒绝安装' >&2
  exit 1
}
mkdir -p "$unit_dir"
temp_dir="$(mktemp -d)"
unit_files=("${services[@]}" "${timers[@]}")
backup_dir="$temp_dir/previous-units"
mkdir -p "$backup_dir"
units_changed=0
executor_was_active=0
if systemctl is-active --quiet prism-ops-executor.service; then
  executor_was_active=1
fi
executor_was_enabled=0
if systemctl is-enabled --quiet prism-ops-executor.service; then
  executor_was_enabled=1
fi
security_block_service_was_active=0
if systemctl is-active --quiet prism-security-block.service; then
  security_block_service_was_active=1
fi
for unit in "${unit_files[@]}"; do
  if [[ -f "$unit_dir/$unit" ]]; then
    cp -a "$unit_dir/$unit" "$backup_dir/$unit"
  else
    : > "$backup_dir/missing-$unit"
  fi
done
for timer in "${timers[@]}"; do
  if systemctl is-active --quiet "$timer"; then
    : > "$backup_dir/active-$timer"
  fi
  if systemctl is-enabled --quiet "$timer"; then
    : > "$backup_dir/enabled-$timer"
  fi
done

# 安装失败时恢复旧 unit，并将执行器恢复到变更前的运行状态。
# 参数: 无。
# 返回: 恢复旧状态；保留触发失败的原始退出码。
cleanup() {
  local rc=$?
  if [[ "$rc" != 0 && "$units_changed" == 1 ]]; then
    trap - EXIT
    set +e
    # 先停新 timer，避免回滚文件时新的封禁作业继续运行。
    for timer in "${timers[@]}"; do
      systemctl stop "$timer"
      if [[ ! -f "$backup_dir/enabled-$timer" ]]; then
        systemctl disable "$timer"
      fi
    done
    systemctl stop prism-security-block.service
    for unit in "${unit_files[@]}"; do
      if [[ -f "$backup_dir/$unit" ]]; then
        install -m 0644 "$backup_dir/$unit" "$unit_dir/$unit"
      elif [[ -f "$backup_dir/missing-$unit" ]]; then
        rm -f "$unit_dir/$unit"
      fi
    done
    systemctl daemon-reload
    for timer in "${timers[@]}"; do
      if [[ -f "$backup_dir/enabled-$timer" ]]; then
        systemctl enable "$timer"
      fi
      if [[ -f "$backup_dir/active-$timer" ]]; then
        systemctl start "$timer"
      fi
    done
    if [[ "$executor_was_enabled" == 1 ]]; then
      systemctl enable prism-ops-executor.service
    else
      systemctl disable prism-ops-executor.service
    fi
    if [[ "$security_block_service_was_active" == 1 ]]; then
      systemctl start prism-security-block.service
    fi
    if [[ "$executor_was_active" == 1 ]]; then
      systemctl restart prism-ops-executor.service
    else
      systemctl stop prism-ops-executor.service
    fi
    printf '%s\n' 'systemd 单元安装失败，已恢复变更前的 unit 和执行器状态。' >&2
    set -e
  fi
  rm -rf "$temp_dir"
  return "$rc"
}
trap cleanup EXIT

for service in "${services[@]}"; do
  render_service "$service.in" "$temp_dir/$service"
  units_changed=1
  install -m 0644 "$temp_dir/$service" "$unit_dir/$service"
done
for timer in "${timers[@]}"; do
  units_changed=1
  install -m 0644 "$timer" "$unit_dir/$timer"
done
systemctl daemon-reload
systemctl enable prism-ops-executor.service
systemctl restart prism-ops-executor.service
systemctl is-active --quiet prism-ops-executor.service || {
  printf '%s\n' 'prism-ops-executor.service 重启后未处于 active 状态。' >&2
  exit 1
}
working_directory="$(systemctl show prism-ops-executor.service --property=WorkingDirectory --value)"
environment_files="$(systemctl show prism-ops-executor.service --property=EnvironmentFiles --value)"
exec_start="$(systemctl show prism-ops-executor.service --property=ExecStart --value)"
main_pid="$(systemctl show prism-ops-executor.service --property=MainPID --value)"
[[ "$working_directory" == "$deploy_dir" ]] || {
  printf '执行器 WorkingDirectory 不匹配：%s\n' "$working_directory" >&2
  exit 1
}
[[ "$environment_files" == *"$deploy_dir/.env"* ]] || {
  printf '执行器 EnvironmentFile 未指向当前发布目录：%s\n' "$environment_files" >&2
  exit 1
}
[[ "$exec_start" == *"$deploy_dir/prism_ops_executor.py"* ]] || {
  printf '执行器 ExecStart 未指向当前发布目录：%s\n' "$exec_start" >&2
  exit 1
}
[[ "$main_pid" =~ ^[1-9][0-9]*$ ]] || {
  printf '执行器 MainPID 无效：%s\n' "$main_pid" >&2
  exit 1
}
process_directory="$(readlink -f "/proc/$main_pid/cwd")"
[[ "$process_directory" == "$deploy_dir" ]] || {
  printf '执行器进程 cwd 不匹配：%s\n' "$process_directory" >&2
  exit 1
}
systemctl enable --now "${timers[@]}"
systemctl list-timers --all 'prism-*'
