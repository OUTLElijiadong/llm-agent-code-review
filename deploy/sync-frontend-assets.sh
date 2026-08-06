#!/usr/bin/env bash
# 前端 assets 卷同步（发布 frontend 镜像后必须执行）。
#
# 背景: cr_frontend 的 /usr/share/nginx/html/assets 由命名卷 deploy_frontend_assets 挂载，
# 新构建镜像里的 dist assets 不会自动进入卷；若 index.html 引用了新哈希文件而卷内缺失，
# 页面 JS 404 直接空白。因此发布前端后必须把镜像 dist assets 同步进卷。
#
# 用法: ./sync-frontend-assets.sh [frontend-image-tag]
#   缺省时读取 deploy/.env 的 FRONTEND_RELEASE。
set -Eeuo pipefail
cd "$(dirname "$0")"
# shellcheck source=lib/common.sh
source "lib/common.sh"

usage() {
  echo "用法: ./sync-frontend-assets.sh [frontend-image-tag]"
  echo "  缺省时读取 .env 的 FRONTEND_RELEASE。"
}

tag="${1:-}"
if [[ -z "$tag" && -f .env ]]; then
  tag="$(grep -oP '(?<=^FRONTEND_RELEASE=).*' .env | head -1 || true)"
fi
if [[ -z "$tag" ]]; then
  usage
  exit 1
fi

image="prism-frontend:${tag}"
volume="${FRONTEND_ASSETS_VOLUME:-deploy_frontend_assets}"

docker image inspect "$image" >/dev/null 2>&1 || fatal "前端镜像不存在: $image"
log_info "同步前端 assets: ${image} -> 卷 ${volume}"
docker run --rm -v "${volume}:/assets" --entrypoint sh "$image" \
  -c 'cp -a /usr/share/nginx/html/assets/. /assets/'
docker run --rm -v "${volume}:/assets" --entrypoint sh "$image" \
  -c 'ls /assets/index-*.js >/dev/null 2>&1' || fatal "同步后卷内缺少 index-*.js"
log_info "前端 assets 同步完成: ${image}"
