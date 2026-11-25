#!/usr/bin/env bash
# Register the on-demand core updater. Never starts an update itself.
set -euo pipefail
[[ "${EUID}" -eq 0 ]] || { echo '核心更新服务需要管理员安装' >&2; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${AGY_ACCOUNTS_PREFIX:-/opt/agy-accounts}"
CONFIG="${AGY_ACCOUNTS_CONFIG:-${HOME}/.config/agy-accounts/config.json}"
PYTHON="${PYTHON:-python3}"
CORE_DIR="$("${PYTHON}" -c 'import json,os,sys; c=json.load(open(sys.argv[1])); print(os.path.dirname(os.path.abspath(os.path.expanduser(c.get("language_server_path") or "/opt/agy-server/language_server"))))' "${CONFIG}")"
[[ -d "${CORE_DIR}" && -d "${HOME}/.agy-remote" ]] || { echo '核心安装目录或运行配置目录不存在' >&2; exit 1; }
[[ "${CORE_DIR}" != / && "${CORE_DIR}" != /opt && "${CORE_DIR}" != /root ]] || { echo '拒绝扩大写权限至宽范围目录' >&2; exit 1; }
install -d -m 0700 "${HOME}/.local/share/agy-accounts/core-updates"
install -m 0755 "${SRC}/core_update_worker.py" "${PREFIX}/core_update_worker.py"
sed -e "s|__HOME__|${HOME}|g" -e "s|__PREFIX__|${PREFIX}|g" -e "s|__PYTHON__|$(command -v "${PYTHON}")|g" \
  -e "s|__CONFIG__|${CONFIG}|g" -e "s|__CORE_DIR__|${CORE_DIR}|g" \
  "${SRC}/deploy/agy-core-update.service" > /etc/systemd/system/agy-core-update.service
systemctl daemon-reload
echo '核心更新服务已注册；未执行升级或重启 Antigravity。'
