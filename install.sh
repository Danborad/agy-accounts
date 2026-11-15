#!/usr/bin/env bash
# agy-accounts · Debian 安装脚本
# 用法：sudo ./install.sh [选项]
set -euo pipefail

PREFIX="${AGY_ACCOUNTS_PREFIX:-/opt/agy-accounts}"
RUN_USER="${AGY_ACCOUNTS_USER:-root}"
UNIT_NAME="${AGY_ACCOUNTS_UNIT:-agy-accounts}"
UNIT_DIR="${AGY_UNIT_DIR:-/etc/systemd/system}"
AGY_SERVICE="${AGY_SERVICE_NAME:-agy-server}"
PANEL_PORT="${AGY_ACCOUNTS_PORT:-8790}"
TOKEN_PATH="${AGY_TOKEN_PATH:-}"
APP_URL="${AGY_APP_URL:-}"
PANEL_PASSWORD="${AGY_ACCOUNTS_PASSWORD:-}"
CONFIG_PATH=""
NO_START=0
SKIP_CHECK=0

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { printf '\033[31m错误：\033[0m%s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[32m ✓ \033[0m%s\n' "$*"; }
warn() { printf '\033[33m ! \033[0m%s\n' "$*"; }

usage() {
  cat <<'EOF'
用法：sudo ./install.sh [选项]

  --prefix PATH        安装目录（默认 /opt/agy-accounts）
  --user USER          运行面板的系统用户（默认 root）
  --config PATH        配置文件路径（默认 <user-home>/.config/agy-accounts/config.json）
  --port N             面板监听端口（默认 8790，仅回环）
  --agy-service NAME   受控的 agy-server systemd 单元名（默认 agy-server）
  --token PATH         language_server 的 OAuth 令牌路径
                       （默认 <user-home>/.gemini/jetski-standalone-oauth-token）
  --app-url URL        Antigravity 页面地址（默认 http://127.0.0.1:8765/）
  --password PW        面板密码；省略时交互输入
  --no-start           只安装，不启动服务
  --skip-check         安装后跳过自检
  -h, --help           显示帮助时

说明：脚本只安装面板本身，不会下载或修改 agy-server。请先按上游文档部署
     https://github.com/AFSlayer/antigravity-server 并完成一次 Google 登录。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix 需要参数}"; shift 2 ;;
    --user) RUN_USER="${2:?--user 需要参数}"; shift 2 ;;
    --config) CONFIG_PATH="${2:?--config 需要参数}"; shift 2 ;;
    --port) PANEL_PORT="${2:?--port 需要参数}"; shift 2 ;;
    --agy-service) AGY_SERVICE="${2:?--agy-service 需要参数}"; shift 2 ;;
    --token) TOKEN_PATH="${2:?--token 需要参数}"; shift 2 ;;
    --app-url) APP_URL="${2:?--app-url 需要参数}"; shift 2 ;;
    --password) PANEL_PASSWORD="${2:?--password 需要参数}"; shift 2 ;;
    --no-start) NO_START=1; shift ;;
    --skip-check) SKIP_CHECK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || die "请用 root 运行：sudo ./install.sh"
[[ -f "${SRC}/agy_accounts.py" ]] || die "找不到 agy_accounts.py，请在仓库目录内运行"
[[ -f "${SRC}/web/index.html" ]] || die "缺少 web/index.html"
command -v systemctl >/dev/null || die "未找到 systemctl，本项目只支持 systemd"

PYTHON="$(command -v python3 || true)"
[[ -n "${PYTHON}" ]] || die "未找到 python3"
"${PYTHON}" - <<'PY' || die "需要 Python 3.9 或更高版本"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY

id -u "${RUN_USER}" >/dev/null 2>&1 || die "系统用户不存在：${RUN_USER}"
HOME_DIR="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
[[ -n "${HOME_DIR}" ]] || die "无法读取用户主目录：${RUN_USER}"
GROUP_NAME="$(id -gn "${RUN_USER}")"

TOKEN_PATH="${TOKEN_PATH:-${HOME_DIR}/.gemini/jetski-standalone-oauth-token}"
TOKEN_PATH="${TOKEN_PATH/#\~/${HOME_DIR}}"
APP_URL="${APP_URL:-http://127.0.0.1:8765/}"
CONFIG_PATH="${CONFIG_PATH:-${HOME_DIR}/.config/agy-accounts/config.json}"
CONFIG_PATH="${CONFIG_PATH/#\~/${HOME_DIR}}"
CONFIG_DIR="$(dirname "${CONFIG_PATH}")"
STATE_DIR="${HOME_DIR}/.local/share/agy-accounts/accounts"
TOKEN_DIR="$(dirname "${TOKEN_PATH}")"

CONFIG_EXISTS=0
if [[ -f "${CONFIG_PATH}" ]]; then CONFIG_EXISTS=1; fi

if [[ -z "${PANEL_PASSWORD}" && "${CONFIG_EXISTS}" -eq 0 ]]; then
  if [[ -t 0 ]]; then
    while true; do
      read -rsp "设置面板密码（至少 8 位）：" PANEL_PASSWORD; echo
      read -rsp "再输入一次：" CONFIRM; echo
      [[ "${PANEL_PASSWORD}" == "${CONFIRM}" ]] || { warn "两次输入不一致，请重试"; continue; }
      [[ ${#PANEL_PASSWORD} -ge 8 ]] || { warn "密码至少 8 位"; continue; }
      break
    done
  else
    die "首次安装且非交互环境，请用 --password 提供面板密码（至少 8 位）"
  fi
fi

HASH=""

info "安装到 ${PREFIX}"
install -d -m 0755 "${PREFIX}"
install -m 0755 "${SRC}/agy_accounts.py" "${PREFIX}/agy_accounts.py"
install -d -m 0755 "${PREFIX}/web"
install -m 0644 "${SRC}"/web/* "${PREFIX}/web/"
install -m 0644 "${SRC}/config.example.json" "${PREFIX}/config.example.json"
if [[ -f "${SRC}/LICENSE" ]]; then install -m 0644 "${SRC}/LICENSE" "${PREFIX}/LICENSE"; fi
ok "程序文件已复制"

if [[ -n "${PANEL_PASSWORD}" ]]; then
  [[ ${#PANEL_PASSWORD} -ge 8 ]] || die "面板密码至少 8 位"
  HASH="$(printf '%s' "${PANEL_PASSWORD}" | "${PYTHON}" "${PREFIX}/agy_accounts.py" hash-password --password -)"
fi

info "准备配置与凭据目录"
install -d -m 0700 -o "${RUN_USER}" -g "${GROUP_NAME}" "${CONFIG_DIR}"
install -d -m 0700 -o "${RUN_USER}" -g "${GROUP_NAME}" "$(dirname "${STATE_DIR}")"
install -d -m 0700 -o "${RUN_USER}" -g "${GROUP_NAME}" "${STATE_DIR}"
install -d -m 0700 -o "${RUN_USER}" -g "${GROUP_NAME}" "${TOKEN_DIR}"

if [[ "${CONFIG_EXISTS}" -eq 0 ]]; then
  [[ -n "${HASH}" ]] || die "缺少面板密码哈希，无法生成配置"
  AGY_CFG_TOKEN="${TOKEN_PATH}" \
  AGY_CFG_ACCOUNTS="${STATE_DIR}" \
  AGY_CFG_SERVICE="${AGY_SERVICE}" \
  AGY_CFG_APP_URL="${APP_URL}" \
  AGY_CFG_PORT="${PANEL_PORT}" \
  AGY_CFG_HASH="${HASH}" \
  AGY_CFG_PATH="${CONFIG_PATH}" \
  AGY_SRC_DIR="${PREFIX}" \
  "${PYTHON}" - <<'PY'
import json, os
with open(os.path.join(os.environ["AGY_SRC_DIR"], "config.example.json"), "r",
          encoding="utf-8") as fh:
    cfg = json.load(fh)
cfg.update({
    "token_path": os.environ["AGY_CFG_TOKEN"],
    "accounts_dir": os.environ["AGY_CFG_ACCOUNTS"],
    "service_name": os.environ["AGY_CFG_SERVICE"],
    "app_url": os.environ["AGY_CFG_APP_URL"],
    "probe_url": os.environ["AGY_CFG_APP_URL"],
    "listen_port": int(os.environ["AGY_CFG_PORT"]),
    "password_hash": os.environ["AGY_CFG_HASH"],
})
path = os.environ["AGY_CFG_PATH"]
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
  chown "${RUN_USER}:${GROUP_NAME}" "${CONFIG_PATH}"
  chmod 0600 "${CONFIG_PATH}"
  ok "已写入配置 ${CONFIG_PATH}"
else
  if [[ -n "${HASH}" ]]; then
    AGY_CFG_PATH="${CONFIG_PATH}" AGY_CFG_HASH="${HASH}" "${PYTHON}" - <<'PY'
import json, os
path = os.environ["AGY_CFG_PATH"]
with open(path, "r", encoding="utf-8") as fh:
    cfg = json.load(fh)
cfg["password_hash"] = os.environ["AGY_CFG_HASH"]
cfg["password"] = ""
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
    ok "已更新已有配置中的面板密码"
  fi
  chown "${RUN_USER}:${GROUP_NAME}" "${CONFIG_PATH}" 2>/dev/null || true
  chmod 0600 "${CONFIG_PATH}" 2>/dev/null || true
  warn "保留已有配置：${CONFIG_PATH}（端口、令牌路径、服务名等修改请直接编辑该文件）"
fi
if [[ "${CONFIG_EXISTS}" -eq 0 ]]; then
  PASSWORD_HINT="输入刚设置的密码"
elif [[ -n "${HASH}" ]]; then
  PASSWORD_HINT="输入刚设置的新密码"
else
  PASSWORD_HINT="输入配置中的密码"
fi
unset PANEL_PASSWORD CONFIRM HASH

SHOW_PORT="$("${PYTHON}" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("listen_port", 8790))' "${CONFIG_PATH}" 2>/dev/null || echo "${PANEL_PORT}")"

UNIT_PATH="${UNIT_DIR}/${UNIT_NAME}.service"
info "写入 systemd 单元 ${UNIT_PATH}"
sed -e "s|__USER__|${RUN_USER}|g" \
    -e "s|__GROUP__|${GROUP_NAME}|g" \
    -e "s|__PREFIX__|${PREFIX}|g" \
    -e "s|__PYTHON__|${PYTHON}|g" \
    -e "s|__CONFIG__|${CONFIG_PATH}|g" \
    -e "s|__HOME__|${HOME_DIR}|g" \
    -e "s|__STATE_DIR__|$(dirname "${STATE_DIR}")|g" \
    -e "s|__CONFIG_DIR__|${CONFIG_DIR}|g" \
    -e "s|__TOKEN_DIR__|${TOKEN_DIR}|g" \
    "${SRC}/deploy/agy-accounts.service.in" > "${UNIT_PATH}"
chmod 0644 "${UNIT_PATH}"

systemctl daemon-reload
systemctl enable "${UNIT_NAME}" >/dev/null
ok "已注册系统服务 ${UNIT_NAME}.service"

if [[ "${NO_START}" -eq 0 ]]; then
  info "启动面板服务"
  systemctl restart "${UNIT_NAME}"
  sleep 1
  if systemctl is-active --quiet "${UNIT_NAME}"; then
    ok "面板已启动：http://127.0.0.1:${SHOW_PORT}/"
  else
    warn "面板未能启动，查看日志：journalctl -u ${UNIT_NAME} -n 50 --no-pager"
  fi
fi

if [[ "${SKIP_CHECK}" -eq 0 ]]; then
  info "环境自检"
  "${PYTHON}" "${PREFIX}/agy_accounts.py" --config "${CONFIG_PATH}" check || true
fi

cat <<EOF

下一步
  1. 如果还没部署 agy-server，请先按 https://github.com/AFSlayer/antigravity-server 安装，
     并在网页里完成一次 Google 登录。
  2. 打开面板 http://127.0.0.1:${SHOW_PORT}/ ，${PASSWORD_HINT}。
  3. 点「保存当前登录」为账号起名，之后即可一键切换。

常用命令
  systemctl status ${UNIT_NAME}
  journalctl -u ${UNIT_NAME} -f
  ${PYTHON} ${PREFIX}/agy_accounts.py --config ${CONFIG_PATH} check

注意
  面板默认只监听 127.0.0.1。如需手机访问，请用 Tailscale 或再加一层带密码的反向代理，
  不要直接暴露到公网。

EOF
