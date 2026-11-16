#!/usr/bin/env bash
# 为 agy-server 增加中文注入反向代理。默认监听 8766，上游 8765。
set -euo pipefail

PREFIX="${AGY_ACCOUNTS_PREFIX:-/opt/agy-accounts}"
LISTEN_PORT="${AGY_I18N_PORT:-8766}"
UPSTREAM_PORT="${AGY_SERVER_PORT:-8765}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { printf '\033[31m错误：\033[0m%s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[32m ✓ \033[0m%s\n' "$*"; }

[[ "${EUID}" -eq 0 ]] || die "请用 root 运行"
[[ -f "${SRC}/i18n/agy-i18n.js" && -f "${SRC}/i18n/zh-CN.json" ]] || die "缺少 i18n 文件"

if ! command -v nginx >/dev/null 2>&1; then
  info "安装 nginx-light"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx-light
fi
command -v nginx >/dev/null || die "nginx 安装失败"

# 系统默认 nginx 实例会抢 80/443；这里只用独立配置跑 8766。
if systemctl list-unit-files | grep -q '^nginx\.service'; then
  systemctl disable --now nginx 2>/dev/null || true
fi

info "复制词库到 ${PREFIX}/i18n"
install -d -m 0755 "${PREFIX}/i18n"
install -m 0644 "${SRC}/i18n/agy-i18n.js" "${PREFIX}/i18n/agy-i18n.js"
install -m 0644 "${SRC}/i18n/zh-CN.json" "${PREFIX}/i18n/zh-CN.json"

info "写入 /etc/agy-i18n/nginx.conf"
install -d -m 0755 /etc/agy-i18n
sed -e "s|__PREFIX__|${PREFIX}|g" \
    -e "s|__LISTEN_PORT__|${LISTEN_PORT}|g" \
    "${SRC}/deploy/agy-i18n-nginx.conf" > /etc/agy-i18n/nginx.conf
sed -i "s|proxy_pass http://127.0.0.1:8765;|proxy_pass http://127.0.0.1:${UPSTREAM_PORT};|" /etc/agy-i18n/nginx.conf

nginx -t -c /etc/agy-i18n/nginx.conf

install -m 0644 "${SRC}/deploy/agy-i18n-proxy.service" /etc/systemd/system/agy-i18n-proxy.service
systemctl daemon-reload
systemctl enable agy-i18n-proxy >/dev/null 2>&1 || true
systemctl restart agy-i18n-proxy
sleep 1

if systemctl is-active --quiet agy-i18n-proxy; then
  ok "汉化代理已启动：http://0.0.0.0:${LISTEN_PORT}/"
  echo "   关闭汉化：在网址后加 ?no-i18n=1"
  echo "   恢复汉化：在网址后加 ?i18n=1"
else
  die "代理启动失败，查看 journalctl -u agy-i18n-proxy"
fi
