#!/usr/bin/env bash
# 为 agy-server 增加中文注入反向代理。默认监听 8766，上游 8765。
# 同时（默认）接入 Powertools 同源网页桥接；设置 AGY_POWERTOOLS=0 可只保留汉化。
set -euo pipefail

PREFIX="${AGY_ACCOUNTS_PREFIX:-/opt/agy-accounts}"
LISTEN_PORT="${AGY_I18N_PORT:-8766}"
UPSTREAM_PORT="${AGY_SERVER_PORT:-8765}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_POWERTOOLS="${AGY_POWERTOOLS:-1}"
POWERTOOLS_PORT="${AGY_POWERTOOLS_PORT:-8791}"
POWERTOOLS_KEY="/etc/agy-powertools/bridge.key"
POWERTOOLS_CONF="/etc/agy-i18n/powertools.conf"
POWERTOOLS_SCRIPT=""

die() { printf '\033[31m错误：\033[0m%s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[32m ✓ \033[0m%s\n' "$*"; }
warn() { printf '\033[33m警告：\033[0m%s\n' "$*" >&2; }

find_node() {
  local candidate
  for candidate in "${NODE_BIN:-}" /opt/node22/bin/node "$(command -v node 2>/dev/null || true)" /usr/local/bin/node; do
    [[ -n "${candidate}" && -x "${candidate}" ]] || continue
    if "${candidate}" -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=13)?0:1)' >/dev/null 2>&1; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

[[ "${EUID}" -eq 0 ]] || die "请用 root 运行"
[[ -n "${HOME:-}" ]] || die "HOME 未设置"
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

install -d -m 0755 /etc/agy-i18n
if [[ "${WITH_POWERTOOLS}" != "0" && -d "${HOME}/.gemini/config/plugins/agy-powertools" ]]; then
  if POWERTOOLS_NODE="$(find_node)"; then
    info "安装 Powertools 网页桥接"
    install -d -m 0755 "${PREFIX}/powertools"
    install -m 0644 "${SRC}/powertools/"*.mjs "${SRC}/powertools/"*.js "${PREFIX}/powertools/"
    install -m 0644 "${SRC}/powertools/"package*.json "${PREFIX}/powertools/"
    install -d -m 0700 /etc/agy-powertools
    install -d -m 0700 "${HOME}/.local/share/agy-accounts/powertools-web"
    # Keep bridge settings outside plugin_data, which is managed by Antigravity.
    if [[ -d "${HOME}/.gemini/antigravity/plugin_data/agy-powertools-web" ]]; then
      cp -an "${HOME}/.gemini/antigravity/plugin_data/agy-powertools-web/." "${HOME}/.local/share/agy-accounts/powertools-web/"
    fi
    if [[ ! -s "${POWERTOOLS_KEY}" ]]; then
      python3 -c 'import secrets; print(secrets.token_hex(32))' > "${POWERTOOLS_KEY}"
      chmod 0600 "${POWERTOOLS_KEY}"
    fi
    BRIDGE_KEY="$(cat "${POWERTOOLS_KEY}")"
    if [[ ! -s /etc/agy-powertools/accounts.key ]]; then
      (umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > /etc/agy-powertools/accounts.key)
    fi
    [[ "${BRIDGE_KEY}" =~ ^[a-f0-9]{64}$ ]] || die "桥接密钥格式无效：${POWERTOOLS_KEY}"
    if [[ -f "${HOME}/.config/agy-accounts/config.json" ]]; then
      bash "${SRC}/install-core-update.sh"
    fi
    sed "s|__BRIDGE_KEY__|${BRIDGE_KEY}|g" \
      "${SRC}/deploy/agy-powertools-nginx.inc" > "${POWERTOOLS_CONF}"
    chmod 0600 "${POWERTOOLS_CONF}"

    sed -e "s|__NODE__|${POWERTOOLS_NODE}|g" \
        -e "s|__PREFIX__|${PREFIX}|g" \
        -e "s|__HOME__|${HOME}|g" \
        "${SRC}/deploy/agy-powertools-web.service" > /etc/systemd/system/agy-powertools-web.service
    if [[ -d "${PREFIX}/powertools/node_modules/playwright" ]]; then
      install -d -m 0700 "${HOME}/.local/share/agy-accounts/powertools-updates"
      sed -e "s|__NODE__|${POWERTOOLS_NODE}|g" -e "s|__PREFIX__|${PREFIX}|g" -e "s|__HOME__|${HOME}|g" \
        "${SRC}/deploy/agy-powertools-update.service" > /etc/systemd/system/agy-powertools-update.service
    fi
    systemctl daemon-reload
    systemctl enable agy-powertools-web >/dev/null 2>&1 || true
    systemctl restart agy-powertools-web
    sleep 1
    if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${POWERTOOLS_PORT}/healthz', timeout=3)" >/dev/null 2>&1; then
      ok "Powertools 网页桥接已启动（127.0.0.1:${POWERTOOLS_PORT}）"
      POWERTOOLS_SCRIPT='<script src="/__agy-powertools/bootstrap.js?v=9" defer></script>'
    else
      warn "Powertools 网页桥接未就绪，查看 journalctl -u agy-powertools-web；原网页仍可正常使用"
    fi
  else
    warn "未找到 Node.js 22.13+，跳过 Powertools 网页桥接；可设置 NODE_BIN 指向合适版本"
  fi
else
  if [[ "${WITH_POWERTOOLS}" == "0" ]]; then
    info "已按 AGY_POWERTOOLS=0 关闭 Powertools 网页桥接"
  else
    warn "未安装 agy-powertools 插件，跳过网页桥接"
  fi
  printf '# Powertools web bridge disabled\n' > "${POWERTOOLS_CONF}"
  chmod 0644 "${POWERTOOLS_CONF}"
  systemctl disable --now agy-powertools-web >/dev/null 2>&1 || true
fi

info "写入 /etc/agy-i18n/nginx.conf"
sed -e "s|__PREFIX__|${PREFIX}|g" \
    -e "s|__LISTEN_PORT__|${LISTEN_PORT}|g" \
    -e "s|__POWERTOOLS_SCRIPT__|${POWERTOOLS_SCRIPT}|g" \
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
  if [[ -n "${POWERTOOLS_SCRIPT}" ]]; then
    echo "   Powertools：账号额度和切换已接入设置页，无需另开账号面板"
  fi
else
  die "代理启动失败，查看 journalctl -u agy-i18n-proxy"
fi
