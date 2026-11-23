import { createServer } from 'node:http';
import { readFileSync, readdirSync } from 'node:fs';
import { timingSafeEqual } from 'node:crypto';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { createAccountClient } from './account-client.mjs';

const MAX_BODY = 64 * 1024;
const RESTRICTED = { multiAccount: false, autoSwitchAccount: false, proxyEnabled: false, fileContextMenu: false };
const ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/;
const ACCOUNT_ID = /^[\w.\-\u4e00-\u9fff]{1,64}$/u;
const STAGE_PROMPT = 'Use the user language.';
const STAGE_PROMPT_ZH = 'Use the user language. If the user writes in Chinese, write every visible progress update, plan item, and task description in Simplified Chinese. Keep code, commands, file paths, and proper nouns unchanged.';

export function localizeStagePrompt(source) {
  return typeof source === 'string' ? source.replace(STAGE_PROMPT, STAGE_PROMPT_ZH) : source;
}

export function webSettings(settings) {
  return { ...settings, ...RESTRICTED };
}

export function validateSettings(patch) {
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) throw Error('设置格式无效');
  for (const [key, value] of Object.entries(RESTRICTED)) {
    if (Object.hasOwn(patch, key) && patch[key] !== value) throw Error('此功能暂不支持网页版，请使用原账号面板或主机配置。');
  }
  if (Object.keys(patch).some(key => key.startsWith('proxy'))) {
    throw Error('网页版不能修改服务端代理，请在主机配置。');
  }
}

export function managedHub(home, service = 'agy-server') {
  const unit = service.endsWith('.service') ? service : `${service}.service`;
  const dir = join(home, '.gemini', 'antigravity', 'daemon');
  const candidates = [];
  for (const name of readdirSync(dir)) {
    if (!/^hub_.*\.json$/.test(name)) continue;
    try {
      const hub = JSON.parse(readFileSync(join(dir, name), 'utf8'));
      if (!Number.isInteger(hub.pid) || hub.pid <= 0 || !Number.isInteger(hub.httpsPort)
          || hub.httpsPort < 1 || hub.httpsPort > 65535) continue;
      const groups = readFileSync(`/proc/${hub.pid}/cgroup`, 'utf8').split('\n');
      if (groups.some(line => line.split(':').at(-1).split('/').includes(unit))) candidates.push(hub);
    } catch { /* Ignore stale daemon records. */ }
  }
  if (candidates.length !== 1) throw Error('无法确认受管 Antigravity 进程，请稍后刷新。');
  return candidates[0];
}

export async function loadPlugin(pluginDir, dataDir, appDataDir) {
  const module = name => import(pathToFileURL(join(pluginDir, 'lib', `${name}.mjs`)).href);
  const [native, cache, sessions, analytics, settings, prices, quotas, subagents, updates] = await Promise.all(
    ['native-host', 'native-cache', 'session-parser', 'analytics', 'settings', 'pricing', 'quota', 'subagents', 'updates'].map(module),
  );
  const version = JSON.parse(readFileSync(join(pluginDir, 'package.json'), 'utf8')).version;
  const store = settings.createSettingsStore(dataDir);
  const pricing = prices.createPricingStore(dataDir);
  const quota = quotas.createQuotaTracker(dataDir);
  const parse = (id, execution) => sessions.parseSessionData(id, { appDataDir, includeSessions: false, strict: true, execution });
  const usage = () => analytics.usageDashboard(appDataDir);
  return { version, store, pricing, quota, parse, usage, native, cache, subagents, updates };
}

export function createBridge({ plugin, secret, home = homedir(), service = 'agy-server', scriptSource,
                                accountScript = '', accountClient }) {
  let cachedUi;
  let buildingUi;
  const authorized = req => {
    const actual = Buffer.from(req.headers['x-agy-powertools-bridge'] || '');
    const expected = Buffer.from(secret);
    return actual.length === expected.length && timingSafeEqual(actual, expected);
  };
  const frontend = async () => {
    const hub = managedHub(home, service);
    const key = `${hub.pid}:${hub.httpsPort}:${plugin.version}`;
    if (cachedUi?.key === key) return cachedUi.source;
    if (buildingUi) return buildingUi;
    buildingUi = (async () => {
      const bundle = await plugin.cache.readLocalBundle(`https://127.0.0.1:${hub.httpsPort}`);
      const names = plugin.cache.declaredNativeNames(bundle.source);
      if (!names.length) throw Error('未找到网页组件');
      const sentinel = 'https://agy-powertools.invalid';
      let source = plugin.native.nativeSource(sentinel, names);
      source = source.replace(`location.origin===${JSON.stringify(sentinel)}`, 'Boolean(globalThis.__agyPowertoolsWeb)');
      source = source.replace(/^globalThis\.__agyInitialSettings=.*;$/m,
        `globalThis.__agyInitialSettings=${JSON.stringify(webSettings(plugin.store.get()))};`);
      source = localizeStagePrompt(source);
      const slot = "tab==='quota'?h(Quotas)";
      if (!source.includes(slot) || !source.includes("['quota',opts.multiAccount?")
          || !source.includes('        h(About),')) throw Error('插件账号页结构不兼容，停止注入');
      source = source.replace(slot, "tab==='quota'?h(globalThis.__agyWebAccountsPanel)");
      source = source.replace("['quota',opts.multiAccount?text('账号与额度','Accounts & Quota'):'额度']", "['quota','账号与额度']");
      source = source.replace('        h(About),', '        h(globalThis.__agyWebAccountsPanel),h(About),');
      cachedUi = { key, source };
      return source;
    })();
    try { return await buildingUi; } finally { buildingUi = null; }
  };
  const rpc = async request => {
    if (!request || typeof request !== 'object' || Array.isArray(request)
        || !Number.isSafeInteger(request.id) || request.id <= 0 || typeof request.method !== 'string') throw Error('请求格式无效');
    const current = webSettings(plugin.store.get());
    if (!current.enabled && !request.method.startsWith('serverAccounts.')
        && !['settings.get', 'settings.set', 'update.check', 'update.status'].includes(request.method)) {
      throw Error('Powertools 已关闭');
    }
    switch (request.method) {
      case 'serverAccounts.list':
      case 'serverAccounts.refresh':
        if (!accountClient) throw Error('账号后端未接入');
        return accountClient(request.method.split('.')[1]);
      case 'serverAccounts.switch':
        if (!accountClient) throw Error('账号后端未接入');
        if (request.confirmed !== true || typeof request.accountId !== 'string'
            || !ACCOUNT_ID.test(request.accountId)
            || ['.', '..'].includes(request.accountId) || typeof request.expectedCurrentId !== 'string') {
          throw Error('切换请求无效或未确认');
        }
        return accountClient('switch', { accountId: request.accountId,
          expectedCurrentId: request.expectedCurrentId, confirmed: true });
      case 'serverAccounts.alias':
        if (!accountClient) throw Error('账号后端未接入');
        if (typeof request.accountId !== 'string' || !ACCOUNT_ID.test(request.accountId)
            || ['.', '..'].includes(request.accountId)
            || typeof request.alias !== 'string' || request.alias.length > 60) {
          throw Error('账号备注无效');
        }
        return accountClient('alias', { accountId: request.accountId, alias: request.alias });
      case 'settings.get': return current;
      case 'settings.set':
        validateSettings(request.settings);
        return webSettings(plugin.store.set(request.settings));
      case 'state': {
        if (typeof request.conversationId !== 'string' || !ID.test(request.conversationId)) throw Error('会话标识无效');
        const parsed = plugin.parse(request.conversationId, request.execution);
        return { ...parsed, costUsd: plugin.subagents.estimateRequestsCost(parsed.requests, plugin.pricing.get().rates), settings: current };
      }
      case 'subagents.stats': {
        if (!Array.isArray(request.conversationIds) || request.conversationIds.length > 32
            || request.conversationIds.some(id => typeof id !== 'string' || !ID.test(id))) throw Error('会话列表无效');
        const items = {};
        for (const id of new Set(request.conversationIds)) {
          const summary = plugin.subagents.summarizeSubagentSession(plugin.parse(id), plugin.pricing.get().rates);
          if (summary) items[id] = summary;
        }
        return { items };
      }
      case 'dashboard': {
        const data = plugin.usage();
        const pricing = plugin.pricing.get();
        const email = typeof request.accountKey === 'string' ? request.accountKey.trim().toLowerCase().slice(0, 254) : '';
        return { ...data, pricing, quotaSamples: plugin.quota.samples(email), accountEmails: [], activeAccountEmail: email,
          requests: data.requests.map(row => ({ ...row, costUsd: plugin.subagents.estimateRequestsCost([row], pricing.rates) })) };
      }
      case 'quota.observe':
        if (!Array.isArray(request.groups) || request.groups.length > 30) throw Error('额度格式无效');
        return plugin.quota.observe(request.groups, plugin.usage().requests, plugin.pricing.get().rates, Date.now(), request.accountKey,
          { knownAccountCount: 2 }); // Do not attribute old requests to the current account.
      case 'pricing.refresh': return plugin.pricing.refresh();
      case 'proxy.status': return { configured: false, active: false, mismatch: false, canRelaunch: false, managedByPowertools: false };
      case 'update.status': return { status: 'idle', managedBy: 'web-adapter' };
      case 'update.check': {
        const result = await plugin.updates.checkForUpdates({ currentVersion: plugin.version });
        return { ...result, canInstall: false, asset: undefined };
      }
      default: throw Error('网页版暂不支持此桌面操作；切号请使用内置账号页。');
    }
  };
  const send = (res, status, body, type = 'application/json; charset=utf-8') => {
    res.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
    res.end(type.startsWith('application/json') ? JSON.stringify(body) : body);
  };
  const server = createServer(async (req, res) => {
    try {
      let path;
      try { path = new URL(req.url, 'http://127.0.0.1').pathname; }
      catch { send(res, 400, { error: '请求路径无效' }); return; }
      if (req.method === 'GET' && path === '/healthz') {
        send(res, 200, { ok: true, version: plugin.version, transport: 'same-origin' });
        return;
      }
      if (!authorized(req)) { send(res, 403, { error: '拒绝直接访问插件桥接服务' }); return; }
      if (req.method === 'GET' && path === '/bootstrap.js') { send(res, 200, scriptSource, 'application/javascript; charset=utf-8'); return; }
      if (req.method === 'GET' && path === '/accounts.js') { send(res, 200, accountScript, 'application/javascript; charset=utf-8'); return; }
      if (req.method === 'GET' && path === '/ui.js') { send(res, 200, await frontend(), 'application/javascript; charset=utf-8'); return; }
      if (req.method !== 'POST' || path !== '/rpc') { send(res, 404, { error: '未找到接口' }); return; }
      let origin;
      try { origin = new URL(req.headers.origin); } catch { /* Missing or invalid origin. */ }
      if (!origin || !['http:', 'https:'].includes(origin.protocol) || origin.host !== req.headers['x-forwarded-host']
          || req.headers['x-agy-powertools'] !== '1' || !String(req.headers['content-type']).startsWith('application/json')) {
        send(res, 403, { error: '拒绝跨站请求' }); return;
      }
      let raw = '';
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > MAX_BODY) { send(res, 413, { error: '请求体过大' }); return; }
        raw += chunk;
      }
      let request;
      try { request = JSON.parse(raw); } catch { send(res, 400, { error: '请求必须是 JSON' }); return; }
      try { send(res, 200, await rpc(request)); }
      catch (error) { send(res, 400, { error: error.message }); }
    } catch (error) {
      console.error('[Powertools web]', error.message);
      if (!res.headersSent) send(res, 503, { error: '插件暂时不可用，原 Antigravity 页面不受影响。' });
    }
  });
  server.requestTimeout = 20000;
  server.headersTimeout = 10000;
  return server;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const home = homedir();
  const pluginDir = process.env.AGY_POWERTOOLS_DIR || join(home, '.gemini', 'config', 'plugins', 'agy-powertools');
  const dataDir = process.env.AGY_POWERTOOLS_DATA || join(home, '.gemini', 'antigravity', 'plugin_data', 'agy-powertools-web');
  const appDataDir = join(home, '.gemini', 'antigravity');
  const secret = readFileSync(process.env.AGY_POWERTOOLS_KEY || '/etc/agy-powertools/bridge.key', 'utf8').trim();
  if (!/^[a-f0-9]{64}$/.test(secret)) throw Error('桥接密钥格式无效');
  const plugin = await loadPlugin(pluginDir, dataDir, appDataDir);
  const scriptSource = readFileSync(new URL('./browser.js', import.meta.url), 'utf8');
  const accountScript = readFileSync(new URL('./accounts.js', import.meta.url), 'utf8');
  const accountClient = createAccountClient({ base: process.env.AGY_ACCOUNTS_URL || 'http://127.0.0.1:8790' });
  const server = createBridge({ plugin, secret, home, service: process.env.AGY_POWERTOOLS_SERVICE || 'agy-server',
    scriptSource, accountScript, accountClient });
  server.listen(Number(process.env.AGY_POWERTOOLS_PORT || 8791), '127.0.0.1', () => console.log(`Powertools ${plugin.version} 网页桥接就绪`));
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => server.close(() => process.exit(0)));
}
