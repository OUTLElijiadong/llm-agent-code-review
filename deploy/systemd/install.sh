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

默认 dry-run；--apply 时需要 root，并会 daemon-reload、enable --now 三个 timer。
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

services=(prism-backup.service prism-verify-backup.service prism-ops-check.service prism-ops-executor.service)
timers=(prism-backup.timer prism-verify-backup.timer prism-ops-check.timer)
if [[ "$apply" != "1" ]]; then
  printf 'DRY-RUN deploy_dir=%s unit_dir=%s\n' "$deploy_dir" "$unit_dir"
  printf '将安装 service: %s\n' "${services[*]}"
  printf '将安装 timer: %s\n' "${timers[*]}"
  printf '将启用 timer: %s\n' "${timers[*]}"
  exit 0
fi

[[ "$EUID" -eq 0 ]] || { printf '%s\n' '--apply 必须以 root 执行' >&2; exit 1; }
for command_name in cut getent groupadd install mktemp sed systemctl; do
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
# 清理临时渲染目录。
# 参数: 无。
# 返回: 始终返回 0。
cleanup() {
  rm -rf "$temp_dir"
}
trap cleanup EXIT

for service in "${services[@]}"; do
  render_service "$service.in" "$temp_dir/$service"
  install -m 0644 "$temp_dir/$service" "$unit_dir/$service"
done
for timer in "${timers[@]}"; do
  install -m 0644 "$timer" "$unit_dir/$timer"
done
systemctl daemon-reload
systemctl enable --now prism-ops-executor.service
systemctl enable --now "${timers[@]}"
systemctl list-timers --all 'prism-*'
