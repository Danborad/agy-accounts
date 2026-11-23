import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { once } from 'node:events';
import { test } from 'node:test';
import { createBridge, validateSettings, webSettings } from '../powertools/server.mjs';
import { createAccountClient } from '../powertools/account-client.mjs';

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

test('账号客户端仅访问本机专用接口，不向浏览器提供凭据', async () => {
    assert.throws(() => createAccountClient({ base: 'http://external.test:8790' }));
    const keyFile = join(mkdtempSync(join(tmpdir(), 'agy-account-client-')), 'accounts.key');
    writeFileSync(keyFile, 'a'.repeat(64), { mode: 0o600 });
    const client = createAccountClient({ keyFile, fetcher: async (url, options) => {
        assert.equal(url.href, 'http://127.0.0.1:8790/internal/powertools/accounts');
        assert.equal(options.headers['X-Agy-Accounts-Bridge'], 'a'.repeat(64));
        assert.deepEqual(JSON.parse(options.body), { action: 'list' });
        return { ok: true, json: async () => ({ ok: true, accounts: [] }) };
    } });
    assert.deepEqual(await client('list'), { ok: true, accounts: [] });
});

test('桥接认证、Origin 校验和 RPC 白名单', async t => {
    const calls = [];
    const accountClient = async (action, payload) => { calls.push({ action, payload }); return { ok: true }; };
    const server = createBridge({ plugin: fixture(), secret, scriptSource: '/* test */', accountClient });
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
    assert.equal((await rpc('serverAccounts.list')).status, 200);
    assert.equal(calls[0].action, 'list');
    assert.equal((await rpc('serverAccounts.switch', { accountId: 'a' })).status, 400);
    assert.equal((await rpc('serverAccounts.switch', { accountId: '../../etc/passwd', confirmed: true, expectedCurrentId: '' })).status, 400);
    assert.equal((await rpc('serverAccounts.switch', { accountId: '主账号', confirmed: true, expectedCurrentId: 'previous' })).status, 200);
    assert.deepEqual(calls.at(-1), { action: 'switch', payload: { accountId: '主账号', confirmed: true, expectedCurrentId: 'previous' } });
    assert.equal((await rpc('serverAccounts.alias', { accountId: '主账号', alias: '新备注' })).status, 200);
    assert.deepEqual(calls.at(-1), { action: 'alias', payload: { accountId: '主账号', alias: '新备注' } });
    assert.equal((await rpc('serverAccounts.alias', { accountId: '主账号', alias: 'x'.repeat(61) })).status, 400);
    assert.equal((await rpc('serverAccounts.alias', { accountId: '../x', alias: 'a' })).status, 400);
    assert.equal((await rpc('state', { conversationId: '../../etc/passwd' })).status, 400);
    assert.equal((await rpc('subagents.stats', { conversationIds: Array(33).fill('id') })).status, 400);
    assert.equal((await (await rpc('update.check')).json()).canInstall, false);
    assert.equal((await rpc('dashboard')).status, 200);
    const invalid = await fetch(`${base}/rpc`, { method: 'POST', headers, body: 'not-json' });
    assert.equal(invalid.status, 400);
    const large = await rpc('settings.get', { ignored: 'x'.repeat(65536) });
    assert.equal(large.status, 413);
});
