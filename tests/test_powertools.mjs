import assert from 'node:assert/strict';
import { once } from 'node:events';
import { test } from 'node:test';
import { createBridge, validateSettings, webSettings } from '../powertools/server.mjs';

const secret = 'f'.repeat(64);
const headers = { 'X-Agy-Powertools-Bridge': secret, 'X-Agy-Powertools': '1',
    Origin: 'http://localhost:8766', 'X-Forwarded-Host': 'localhost:8766', 'Content-Type': 'application/json' };

function fixture() {
    let settings = { enabled: true, compact: true, multiAccount: true };
    return {
        version: 'test',
        store: { get: () => settings, set: patch => (settings = { ...settings, ...patch }) },
        parse: id => ({ conversationId: id, requests: [] }),
        usage: () => ({ requests: [], errors: [], totalRequests: 0 }),
        pricing: { get: () => ({ rates: [] }) },
        quota: { samples: () => [] },
        subagents: { estimateRequestsCost: () => 0, summarizeSubagentSession: () => null },
        updates: { checkForUpdates: async () => ({ canInstall: true, asset: { url: 'secret' } }) },
    };
}

test('网页版强制关闭桌面切号与代理，设置保存前拦截', () => {
    assert.equal(webSettings({ multiAccount: true, proxyEnabled: true }).multiAccount, false);
    assert.equal(webSettings({}).fileContextMenu, false);
    assert.throws(() => validateSettings({ multiAccount: true }));
    assert.throws(() => validateSettings({ proxyUrl: 'http://internal-service' }));
    assert.throws(() => validateSettings(null));
    assert.doesNotThrow(() => validateSettings({ compact: false }));
});

test('桥接认证、Origin 校验和 RPC 白名单', async t => {
    const server = createBridge({ plugin: fixture(), secret, scriptSource: '/* test */' });
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    t.after(() => new Promise(resolve => server.close(resolve)));
    const base = `http://127.0.0.1:${server.address().port}`;
    const rpc = (method, extra = {}, override = {}) => fetch(`${base}/rpc`, {
        method: 'POST', headers: { ...headers, ...override }, body: JSON.stringify({ id: 1, method, ...extra }),
    });
    assert.equal((await fetch(`${base}/bootstrap.js`)).status, 403);
    assert.equal((await fetch(`${base}/healthz`)).status, 200);
    assert.equal((await rpc('settings.get', {}, { 'X-Agy-Powertools-Bridge': 'invalid' })).status, 403);
    assert.equal((await rpc('settings.get', {}, { Origin: 'https://evil.test' })).status, 403);
    assert.equal((await rpc('settings.get', {}, { 'X-Agy-Powertools': '' })).status, 403);
    assert.equal((await (await rpc('settings.get')).json()).multiAccount, false);
    assert.equal((await (await rpc('settings.set', { settings: { compact: false } })).json()).compact, false);
    assert.equal((await rpc('settings.set', { settings: { multiAccount: true } })).status, 400);
    for (const method of ['accounts.switch', 'proxy.relaunch', 'file.open', 'update.install']) {
        assert.equal((await rpc(method)).status, 400);
    }
    assert.equal((await rpc('state', { conversationId: '../../etc/passwd' })).status, 400);
    assert.equal((await rpc('subagents.stats', { conversationIds: Array(33).fill('id') })).status, 400);
    assert.equal((await (await rpc('update.check')).json()).canInstall, false);
    assert.equal((await rpc('dashboard')).status, 200);
    const invalid = await fetch(`${base}/rpc`, { method: 'POST', headers, body: 'not-json' });
    assert.equal(invalid.status, 400);
    const large = await rpc('settings.get', { ignored: 'x'.repeat(65536) });
    assert.equal(large.status, 413);
});
