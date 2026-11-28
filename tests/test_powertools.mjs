import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { once } from 'node:events';
import { test } from 'node:test';
import { createBridge, validateSettings, webSettings, localizeStagePrompt, adaptNativeBindings, adaptPluginSource } from '../powertools/server.mjs';
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

test('阶段汇报提示词补充中文计划要求', () => {
    const source = 'const stageUpdateRule="During this task... Use the user language. Never invent progress.";';
    const localized = localizeStagePrompt(source);
    assert.match(localized, /Simplified Chinese/);
    assert.match(localized, /plan item/);
    assert.equal(localizeStagePrompt('no prompt here'), 'no prompt here');
});

test('弹层绑定兼容 2.34 与 2.35，仍拒绝无关组件', () => {
    const source = "s.includes('scrollContainerClassName:')&&s.includes('arrowNavigable:')&&s.includes('matchTriggerWidth:')";
    const matches = new Function('s', `return ${adaptNativeBindings(source)}`);
    assert.equal(matches('scrollContainerClassName: arrowNavigable: matchTriggerWidth:'), true);
    assert.equal(matches('data-popover-popup arrowNavigable: matchTriggerWidth:'), true);
    assert.equal(matches('arrowNavigable: matchTriggerWidth:'), false);
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
    const accountClient = async (action, payload) => { calls.push({ action, payload }); return { ok: true, canUpdate: true }; };
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
    const get = (path, override = {}) => fetch(`${base}${path}`, { headers: { ...headers, ...override } });
    assert.equal((await get('/bootstrap.js?v=2')).status, 200);
    assert.equal((await get('/accounts.js?v=2')).status, 200);
    assert.equal((await rpc('settings.get', {}, { 'X-Agy-Powertools-Bridge': 'invalid' })).status, 403);
    assert.equal((await rpc('settings.get', {}, { Origin: 'https://evil.test' })).status, 403);
    assert.equal((await rpc('settings.get', {}, { 'X-Agy-Powertools': '' })).status, 403);
    assert.equal((await (await rpc('settings.get')).json()).multiAccount, false);
    assert.equal((await (await rpc('settings.set', { settings: { compact: false } })).json()).compact, false);
    assert.equal((await rpc('settings.set', { settings: { multiAccount: true } })).status, 400);
    for (const method of ['accounts.switch', 'proxy.relaunch', 'file.open']) {
        assert.equal((await rpc(method)).status, 400);
    }
    assert.equal((await rpc('serverAccounts.list')).status, 200);
    assert.equal(calls[0].action, 'list');
    assert.equal((await rpc('coreUpdate.status')).status, 200);
    assert.equal(calls.at(-1).action, 'core_update_status');
    assert.equal((await rpc('coreUpdate.check')).status, 200);
    assert.equal(calls.at(-1).action, 'core_update_check');
    const count = calls.length;
    assert.equal((await rpc('coreUpdate.apply')).status, 400);
    assert.equal(calls.length, count);
    assert.equal((await rpc('coreUpdate.apply', { version: '2.35.1', confirmed: false })).status, 400);
    assert.equal((await rpc('coreUpdate.apply', { version: '2.35.1', confirmed: true })).status, 200);
    assert.deepEqual(calls.at(-1), { action: 'core_update_apply', payload: { version: '2.35.1', confirmed: true } });
    assert.equal((await rpc('serverAccounts.switch', { accountId: 'a' })).status, 400);
    assert.equal((await rpc('serverAccounts.switch', { accountId: '../../etc/passwd', confirmed: true, expectedCurrentId: '' })).status, 400);
    assert.equal((await rpc('serverAccounts.switch', { accountId: '主账号', confirmed: true, expectedCurrentId: 'previous' })).status, 200);
    assert.deepEqual(calls.at(-1), { action: 'switch', payload: { accountId: '主账号', confirmed: true, expectedCurrentId: 'previous' } });
    assert.equal((await rpc('serverAccounts.alias', { accountId: '主账号', alias: '新备注' })).status, 200);
    assert.deepEqual(calls.at(-1), { action: 'alias', payload: { accountId: '主账号', alias: '新备注' } });
    assert.equal((await rpc('serverAccounts.alias', { accountId: '主账号', alias: 'x'.repeat(61) })).status, 400);
    assert.equal((await rpc('serverAccounts.alias', { accountId: '../x', alias: 'a' })).status, 400);
    assert.equal((await rpc('serverAccounts.failoverGet')).status, 200);
    assert.equal(calls.at(-1).action, 'failover_status');
    assert.equal((await rpc('serverAccounts.failoverSet', { enabled: 'no' })).status, 400);
    assert.equal((await rpc('serverAccounts.failoverSet', { enabled: false })).status, 200);
    assert.deepEqual(calls.at(-1), { action: 'failover_set', payload: { enabled: false } });
    assert.equal((await rpc('state', { conversationId: '../../etc/passwd' })).status, 400);
    assert.equal((await rpc('subagents.stats', { conversationIds: Array(33).fill('id') })).status, 400);
    assert.equal((await (await rpc('update.check')).json()).canInstall, true);
    assert.equal((await rpc('update.install', { version: '2.14.3' })).status, 200);
    assert.deepEqual(calls.at(-1), { action: 'plugin_update_start', payload: { version: '2.14.3' } });
    assert.equal((await rpc('update.install', { version: '../shell' })).status, 400);
    assert.equal((await rpc('dashboard')).status, 200);
    const invalid = await fetch(`${base}/rpc`, { method: 'POST', headers, body: 'not-json' });
    assert.equal(invalid.status, 400);
    const large = await rpc('settings.get', { ignored: 'x'.repeat(65536) });
    assert.equal(large.status, 413);
});

test('插件成功检查合并并发请求并缓存，避免重复打 GitHub', async t => {
    const plugin = fixture();
    let count = 0;
    plugin.updates.checkForUpdates = async () => { count++; return { status: 'current', canInstall: false }; };
    const server = createBridge({ plugin, secret, scriptSource: '' });
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    t.after(() => new Promise(resolve => server.close(resolve)));
    const rpc = () => fetch(`http://127.0.0.1:${server.address().port}/rpc`, {
        method: 'POST', headers, body: JSON.stringify({ id: 1, method: 'update.check' }),
    });
    await Promise.all([rpc(), rpc()]);
    await rpc();
    assert.equal(count, 1);
});

test('设置页只放自动切换开关与核心更新，不再重复放账号列表（兼容 2.16 与 2.17 多分支 About）', () => {
    const sample = [
        'if(location.origin==="https://agy-powertools.invalid"){',
        'globalThis.__agyInitialSettings={};',
        'if(!options.enabled){return h("div",null,',
        '          h(About),',
        ');}',
        "const t1=['quota',opts.multiAccount?text('账号与额度','Accounts & Quota'):'额度'];",
        "const t2=['quota',opts.multiAccount?text('账号与额度','Accounts & Quota'):text('额度','Quota')];",
        "const p1=tab==='quota'?h(Quotas):h(Settings);",
        "const p2=tab==='quota'?h(Quotas):h(Settings);",
        '        h(About),',
    ].join('\n');
    const out = adaptPluginSource(sample, { compact: true });
    assert.equal(out.split('__agyWebAccountsPanel').length - 1, 2);
    assert.equal(out.split('h(globalThis.__agyWebFailoverSetting),h(globalThis.__agyWebCoreUpdate),h(About),').length - 1, 2);
    assert.ok(!out.includes('__agyWebAccountsPanel),h(globalThis.__agyWebCoreUpdate)'));
    assert.ok(!out.includes('h(Quotas)'));
});
