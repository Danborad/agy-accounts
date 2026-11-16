#!/usr/bin/env bash
# 卸载 agy-accounts 面板。默认保留账号快照与配置，加 --purge 才会删除。
set -euo pipefail

UNIT_NAME="${AGY_ACCOUNTS_UNIT:-agy-accounts}"
PREFIX="${AGY_ACCOUNTS_PREFIX:-/opt/agy-accounts}"
RUN_USER="${AGY_ACCOUNTS_USER:-root}"
PURGE=0

for arg in "$@"; do
  case "${arg}" in
    --purge) PURGE=1 ;;
    -h|--help)
      echo "用法：sudo ./uninstall.sh [--purge]"
      echo "  --purge  同时删除配置、账号快照与 secret.key（不可恢复）"
      exit 0 ;;
    *) echo "未知参数：${arg}" >&2; exit 1 ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || { echo "请用 root 运行" >&2; exit 1; }

if systemctl list-unit-files | grep -q '^agy-i18n-proxy.service'; then
  systemctl disable --now agy-i18n-proxy 2>/dev/null || true
fi
rm -f /etc/systemd/system/agy-i18n-proxy.service
rm -rf /etc/agy-i18n
systemctl daemon-reload

if systemctl list-unit-files | grep -q "^${UNIT_NAME}.service"; then
  systemctl disable --now "${UNIT_NAME}" 2>/dev/null || true
fi
rm -f "/etc/systemd/system/${UNIT_NAME}.service"
systemctl daemon-reload
rm -rf "${PREFIX}"
echo "已停止并删除面板程序 ${PREFIX}"

if [[ "${PURGE}" -eq 1 ]]; then
  HOME_DIR="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
  rm -rf "${HOME_DIR}/.config/agy-accounts" "${HOME_DIR}/.local/share/agy-accounts"
  echo "已删除配置与账号快照（令牌文件本身未触碰）"
else
  echo "配置与账号快照已保留；如需彻底清除，请加 --purge"
fi
