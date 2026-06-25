#!/usr/bin/env bash
# ============================================================
# 首次签发 Let's Encrypt 证书（standalone 模式，需短暂占用 80 端口）
#   用法：  ./issue-cert.sh [域名] [邮箱]
#   默认：  域名=lijiadong.cn  邮箱=outlejackson@gmail.com
# 之后的「续期」用 renew-cert.sh（webroot，零停机），不要再用本脚本。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN="${1:-lijiadong.cn}"
EMAIL="${2:-outlejackson@gmail.com}"

mkdir -p certbot/conf certbot/www

echo "🛑 暂停 cr_frontend 以释放 80 端口（签发期间站点短暂不可用，约 20~40s）..."
docker stop cr_frontend >/dev/null 2>&1 || true

echo "📜 certbot standalone 签发证书：$DOMAIN ..."
set +e
docker run --rm -p 80:80 \
  -v "$PWD/certbot/conf:/etc/letsencrypt" \
  -v "$PWD/certbot/www:/var/www/certbot" \
  certbot/certbot certonly --standalone \
  -d "$DOMAIN" \
  --email "$EMAIL" --agree-tos --no-eff-email --non-interactive
RC=$?
set -e

echo "▶️  重新拉起 cr_frontend ..."
docker compose up -d --no-deps frontend >/dev/null 2>&1 || docker start cr_frontend >/dev/null 2>&1 || true

if [ "$RC" -ne 0 ]; then
  echo "❌ 证书签发失败（RC=$RC）。站点已恢复。请检查域名解析/80端口/限流后重试。"
  exit "$RC"
fi
echo "✅ 证书已签发：certbot/conf/live/$DOMAIN/"
echo "   现在可以部署带 443 的前端：./deploy.sh frontend"
