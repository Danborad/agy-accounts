import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';
import { decodeVerifiedPackage, digest, downloadReleaseAsset } from '../powertools/update-package.mjs';
import { runPluginUpdate } from '../powertools/update-worker.mjs';

function fixture() {
  const json = { name: 'agy-powertools', version: '2.14.3' };
  const files = ['package.json', 'plugin.json', 'lib/native-host.mjs', 'lib/native-panels.js', 'lib/native-bindings.js', 'sidecars/inspector/sidecar.json']
    .map(path => {
      const bytes = Buffer.from(JSON.stringify(path.endsWith('sidecar.json') ? { command: 'node', env: { AGY_POWERTOOLS_VERSION: '2.14.3' } } : json));
      return { path, data: bytes.toString('base64'), sha256: digest(bytes) };
    });
  const bundle = { format: 1, id: 'agy-powertools', version: '2.14.3', files };
  const bytes = gzipSync(JSON.stringify(bundle));
  const release = { status: 'available', version: '2.14.3', canInstall: true, asset: {
    name: 'agy-powertools-v2.14.3.bundle.gz', sha256: digest(bytes),
    url: 'https://github.com/WhiteBr1ck/agy-powertools/releases/download/v2.14.3/agy-powertools-v2.14.3.bundle.gz',
  } };
  return { bundle, bytes, release };
}

test('安装包校验拒绝损坏、路径穿越和版本不匹配', () => {
  const { bundle, bytes } = fixture();
  assert.equal(decodeVerifiedPackage(bytes, digest(bytes), '2.14.3').version, '2.14.3');
  assert.throws(() => decodeVerifiedPackage(bytes, 'f'.repeat(64), '2.14.3'));
  assert.throws(() => decodeVerifiedPackage(bytes, digest(bytes), '9.0.0'));
  bundle.files[0].path = '../outside';
  const invalid = gzipSync(JSON.stringify(bundle));
  assert.throws(() => decodeVerifiedPackage(invalid, digest(invalid), '2.14.3'));
});

test('下载拒绝用户指定的非官方地址', async () => {
  await assert.rejects(downloadReleaseAsset('https://evil.test/plugin.gz', 1024));
  await assert.rejects(downloadReleaseAsset('http://github.com/WhiteBr1ck/agy-powertools/releases/download/a', 1024));
});

for (const fail of ['before', 'after', 'none']) {
  test(`安全更新与失败回滚：${fail}`, async () => {
    const root = mkdtempSync(join(tmpdir(), 'agy-update-test-'));
    const currentDir = join(root, 'current');
    mkdirSync(currentDir);
    writeFileSync(join(currentDir, 'plugin.json'), JSON.stringify({ name: 'agy-powertools', version: '2.13.8' }));
    writeFileSync(join(currentDir, 'old-user-file'), 'retained in backup');
    const state = [];
    let verified = 0, restarted = 0;
    const { bytes, release } = fixture();
    const run = () => runPluginUpdate({ root, currentDir, release, version: '2.14.3',
      setState: item => state.push(item), restart: async () => { restarted++; },
      verify: async () => { verified++; if (fail === 'before' || fail === 'after' && verified === 2) throw Error('incompatible'); },
      fetcher: async url => ({ ok: true, url, body: [bytes] }),
    });
    if (fail === 'none') {
      const result = await run();
      assert.equal(JSON.parse(readFileSync(join(currentDir, 'plugin.json'))).version, '2.14.3');
      assert.equal(readFileSync(join(result.backup, 'old-user-file'), 'utf8'), 'retained in backup');
      assert.equal(state.at(-1).status, 'completed');
      assert.equal(restarted, 1);
    } else {
      await assert.rejects(run());
      assert.equal(JSON.parse(readFileSync(join(currentDir, 'plugin.json'))).version, '2.13.8');
      assert.equal(readFileSync(join(currentDir, 'old-user-file'), 'utf8'), 'retained in backup');
      assert.equal(state.at(-1).status, fail === 'after' ? 'rolled-back' : 'failed');
      assert.equal(restarted, fail === 'after' ? 2 : 0);
    }
  });
}
