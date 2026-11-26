import { readFileSync } from 'node:fs';
import { once } from 'node:events';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { homedir } from 'node:os';
import { chromium } from 'playwright';
import { createBridge, loadPlugin, managedHub } from './server.mjs';
import { createAccountClient } from './account-client.mjs';

export async function verifyPlugin(pluginDir, dataDir) {
  const home = homedir();
  const appData = join(home, '.gemini', 'antigravity');
  const plugin = await loadPlugin(pluginDir, dataDir, appData);
  const hub = managedHub(home);
  const origin = `https://127.0.0.1:${hub.httpsPort}`;
  const secret = randomBytes(32).toString('hex');
  const accountScript = readFileSync(new URL('./accounts.js', import.meta.url), 'utf8') + '\n'
    + readFileSync(new URL('./core-update.js', import.meta.url), 'utf8');
  const bridge = createBridge({ plugin, secret, home, accountScript,
    scriptSource: readFileSync(new URL('./browser.js', import.meta.url), 'utf8'), accountClient: createAccountClient() });
  bridge.listen(0, '127.0.0.1');
  await once(bridge, 'listening');
  const backend = `http://127.0.0.1:${bridge.address().port}`;
  let browser;
  let context;
  try {
    browser = await chromium.launch();
    context = await browser.newContext({ ignoreHTTPSErrors: true, serviceWorkers: 'block', viewport: { width: 1440, height: 900 } });
    await context.route('**/*', async route => {
      const req = route.request();
      const url = new URL(req.url());
      if (url.origin !== origin) return route.abort();
      if (url.pathname.startsWith('/__agy-powertools/')) {
        if (url.pathname.endsWith('/rpc')) {
          const body = JSON.parse(req.postData());
          if (body.method === 'quota.observe') return route.fulfill({ json: { estimates: [] } });
          if (body.method === 'update.status') return route.fulfill({ json: { status: 'idle' } });
          if (!['settings.get', 'dashboard', 'state', 'subagents.stats', 'serverAccounts.list', 'serverAccounts.failoverGet', 'coreUpdate.status', 'proxy.status'].includes(body.method)) {
            return route.fulfill({ status: 403, json: { error: '更新验证只允许只读操作' } });
          }
        }
        const response = await context.request.fetch(backend + url.pathname.slice('/__agy-powertools'.length) + url.search, {
          method: req.method(), data: req.postDataBuffer(), headers: { ...req.headers(),
            'X-Agy-Powertools-Bridge': secret, 'X-Forwarded-Host': url.host, Origin: origin },
        });
        return route.fulfill({ response });
      }
      const rpc = url.pathname.split('/').at(-1);
      const readonly = /^(Get|List|Read|Subscribe|Watch|Stream|JetboxSubscribe)/.test(rpc)
        || ['HasAuthToken', 'ProjectUpdatesStream', 'FetchAdminControls'].includes(rpc);
      if (req.method() !== 'GET' && !readonly) return route.abort();
      if (url.pathname === '/customizations') {
        const response = await context.request.get(req.url(), { headers: { 'x-codeium-csrf-token': hub.csrfToken } });
        return route.fulfill({ response, body: (await response.text()).replace('</head>',
          '<script src="/__agy-powertools/bootstrap.js" defer></script></head>') });
      }
      return route.continue({ headers: { ...req.headers(), 'x-codeium-csrf-token': hub.csrfToken } });
    });
    await context.routeWebSocket('**/*', socket => socket.close());
    const page = await context.newPage();
    await page.goto(`${origin}/customizations`, { waitUntil: 'domcontentloaded' });
    await page.getByTestId('agy-management-trigger').waitFor({ state: 'visible', timeout: 25000 });
    const version = await page.evaluate(() => window.__agyPowertools?.version);
    if (version !== plugin.version) throw Error('插件版本未正确挂载');
    await page.getByTestId('agy-management-trigger').click();
    await page.getByTestId('agy-tab-quota').click();
    await page.getByTestId('agy-server-accounts').waitFor({ state: 'visible' });
    await page.waitForFunction(() => document.querySelector('[data-server-account]')
      || document.body.innerText.includes('暂无已保存账号'), null, { timeout: 15000 });
    const edit = page.locator('[data-testid="agy-server-alias-edit"], [data-testid="agy-server-alias-add"]').first();
    if (await edit.count()) {
      await edit.click();
      const dialog = page.getByRole('dialog', { name: '编辑账号备注' });
      if (!await dialog.evaluate(node => node.matches(':modal'))) throw Error('原生备注弹窗未正常挂载');
      await page.keyboard.press('Escape');
    }
    await page.getByTestId('agy-tab-settings').click();
    await page.getByTestId('agy-server-failover').waitFor({ state: 'visible' });
    await page.getByTestId('agy-core-update').waitFor({ state: 'visible' });
    await page.getByTestId('agy-about-version').filter({ hasText: plugin.version }).waitFor();
    await context.unrouteAll({ behavior: 'ignoreErrors' });
    return { healthy: true, version: plugin.version };
  } finally {
    if (context) await context.unrouteAll({ behavior: 'ignoreErrors' });
    if (browser) await browser.close();
    await new Promise(resolve => bridge.close(resolve));
  }
}

if (process.argv[2] === '--verify') {
  try {
    console.log(JSON.stringify(await verifyPlugin(process.argv[3], process.argv[4])));
  } catch {
    // Browser errors can contain headers. Keep authentication material out of logs.
    console.error('插件网页兼容性验证失败：入口、账号页或设置页未正常加载');
    process.exitCode = 1;
  }
}
