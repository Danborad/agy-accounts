#!/usr/bin/env bash
# Rebuild the vendored agy-switch web frontend into ../webapp.
set -euo pipefail
cd "$(dirname "$0")"

NODE_BIN="${NODE_BIN:-/opt/node22/bin}"
if [[ -x "${NODE_BIN}/node" ]]; then
  export PATH="${NODE_BIN}:${PATH}"
fi
command -v node >/dev/null || { echo "需要 Node.js 20.19+ / 22.12+" >&2; exit 1; }
node -e 'const v=process.versions.node.split(".").map(Number); if(v[0]<20||(v[0]===20&&v[1]<19)) { console.error("Node 版本过低: "+process.versions.node); process.exit(1); }'

npm ci --no-audit --no-fund
npm run build

rm -rf ../webapp
cp -a dist ../webapp
echo "已生成 ../webapp ($(du -sh ../webapp | cut -f1))"
