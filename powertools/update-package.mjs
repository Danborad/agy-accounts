import { createHash } from 'node:crypto';
import { gunzipSync } from 'node:zlib';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

const ITEMS = new Set(['ui', 'assets', 'rules', 'skills', 'sidecars', 'lib', 'scripts', 'docs', 'package.json',
  'package-lock.json', 'plugin.json', 'hooks.json', 'project.json', 'README.md', 'README_EN.md', 'LICENSE',
  'install.ps1', 'uninstall.ps1', 'install.sh', 'uninstall.sh']);
export const digest = bytes => createHash('sha256').update(bytes).digest('hex');

export function decodeVerifiedPackage(bytes, sha256, version) {
  if (!/^[a-f0-9]{64}$/.test(sha256 || '') || digest(bytes) !== sha256) throw Error('安装包 SHA-256 校验失败');
  const bundle = JSON.parse(gunzipSync(bytes, { maxOutputLength: 72 * 1024 * 1024 }).toString('utf8'));
  if (bundle.format !== 1 || bundle.id !== 'agy-powertools' || bundle.version !== version
      || !Array.isArray(bundle.files) || bundle.files.length > 2000) throw Error('安装包信息无效');
  const names = new Set();
  let size = 0;
  for (const file of bundle.files) {
    const parts = typeof file.path === 'string' ? file.path.split('/') : [];
    if (!ITEMS.has(parts[0]) || file.path.length > 240
        || parts.some(part => !part || part === '.' || part === '..' || /[\\:\x00-\x1f]/.test(part))
        || names.has(file.path.toLowerCase())) throw Error('安装包路径无效');
    names.add(file.path.toLowerCase());
    if (typeof file.data !== 'string' || !/^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(file.data)) throw Error('安装包编码无效');
    const decoded = Buffer.from(file.data, 'base64');
    size += decoded.length;
    if (size > 48 * 1024 * 1024 || digest(decoded) !== file.sha256) throw Error('安装包文件校验失败');
  }
  for (const path of ['package.json', 'plugin.json', 'lib/native-host.mjs', 'lib/native-panels.js', 'lib/native-bindings.js', 'sidecars/inspector/sidecar.json']) {
    if (!names.has(path)) throw Error(`安装包缺少文件：${path}`);
  }
  for (const path of ['package.json', 'plugin.json']) {
    const record = JSON.parse(Buffer.from(bundle.files.find(file => file.path === path).data, 'base64'));
    if (record.name !== 'agy-powertools' || record.version !== version) throw Error('安装包版本不一致');
  }
  const sidecar = JSON.parse(Buffer.from(bundle.files.find(file => file.path === 'sidecars/inspector/sidecar.json').data, 'base64'));
  if (sidecar.env?.AGY_POWERTOOLS_VERSION !== version) throw Error('插件伴生进程版本不一致');
  return bundle;
}

export function writeVerifiedPackage(bundle, directory) {
  for (const file of bundle.files) {
    const path = join(directory, file.path);
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    writeFileSync(path, Buffer.from(file.data, 'base64'), { mode: file.path.endsWith('.sh') ? 0o755 : 0o644 });
  }
}

export async function downloadReleaseAsset(url, maxBytes, fetcher = fetch) {
  const initial = new URL(url);
  if (initial.origin !== 'https://github.com' || !initial.pathname.startsWith('/WhiteBr1ck/agy-powertools/releases/download/')
      || initial.username || initial.password) throw Error('非官方插件下载地址');
  const response = await fetcher(url, { redirect: 'follow', signal: AbortSignal.timeout(60000) });
  if (!response.ok) throw Error(`插件下载失败：HTTP ${response.status}`);
  const final = new URL(response.url || url);
  if (final.protocol !== 'https:' || !(final.hostname === 'github.com' || final.hostname.endsWith('.githubusercontent.com'))) throw Error('下载跳转地址无效');
  const chunks = [];
  let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > maxBytes) throw Error('安装包超过大小限制');
    chunks.push(Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}
