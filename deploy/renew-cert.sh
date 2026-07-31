#!/usr/bin/env bash
# ============================================================
# 续期 Let's Encrypt 证书（webroot 模式，零停机）
# certbot 只在证书剩余 <30 天时才真正续期；可安全地每天/每周跑。
# 建议 crontab（root）：
#   0 3 * * 1 /opt/code-review/deploy/renew-cert.sh >> /var/log/cr-cert-renew.log 2>&1
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  -h|--help)
    cat <<'USAGE'
用法: ./renew-cert.sh

使用 webroot 模式检查并续期 Let's Encrypt 证书；续期后重新加载前端 Nginx。
USAGE
    exit 0
    ;;
  "")
    ;;
  *)
    printf '未知参数: %s\n' "$1" >&2
    exit 2
    ;;
esac

echo "[$(date '+%F %T')] 检查/续期证书 ..."
docker run --rm \
  -v "$PWD/certbot/conf:/etc/letsencrypt" \
  -v "$PWD/certbot/www:/var/www/certbot" \
  certbot/certbot renew --webroot -w /var/www/certbot --quiet

# 让 nginx 重新加载新证书（reload 不断连接；失败则兜底重启容器）
docker exec cr_frontend nginx -s reload 2>/dev/null \
  || docker restart cr_frontend >/dev/null 2>&1 || true
echo "[$(date '+%F %T')] 续期检查完成。"
