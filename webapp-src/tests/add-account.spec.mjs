import { readFileSync } from 'node:fs';
import { buildSync } from 'esbuild';
import { expect, test } from '@playwright/test';

const dialog = buildSync({
    stdin: {
        contents: [
            "import React from 'react';",
            "import{createRoot}from'react-dom/client';",
            "import AddAccountDialog from './src/components/accounts/AddAccountDialog.tsx';",
            "window.mountAddAccount=()=>createRoot(document.getElementById('root')).render(",
            "React.createElement(AddAccountDialog,{onAdd:async(email,refreshToken)=>{window.addCalls.push({email,refreshToken});}}));",
        ].join(''),
        resolveDir: process.cwd(),
    },
    bundle: true, write: false, format: 'iife', jsx: 'automatic',
}).outputFiles[0].text;

const shim = readFileSync(new URL('../public/tauri-shim.js', import.meta.url), 'utf8');

async function mount(page) {
    await page.route('http://localhost:4173/add-account-test', route => route.fulfill({
        contentType: 'text/html', body: '<div id="root"></div>',
    }));
    await page.goto('http://localhost:4173/add-account-test');
    await page.evaluate(() => {
        window.__AGY_WEB_BRIDGE__ = true;
        window.addCalls = [];
        window.invokeCalls = [];
        window.__TAURI_INTERNALS__ = {
            invoke: async (cmd, args) => {
                window.invokeCalls.push({ cmd, args });
                switch (cmd) {
                    case 'list_accounts': return { accounts: [] };
                    case 'get_app_url': return 'http://antigravity.test/';
                    case 'import_local_accounts_report':
                        return { accounts: [{ id: 'live', email: 'live@example.com', name: 'live@example.com' }],
                            sources: [{ source: '当前 Antigravity 登录', status: 'imported' }] };
                    case 'import_accounts_file':
                        return { imported: 1, failed: 0, duplicate: 0,
                            accounts: [{ id: 'file', email: 'file@example.com', name: 'file@example.com' }],
                            sources: [{ source: args.filename, status: 'imported' }] };
                    default: return null;
                }
            },
            transformCallback: id => id,
            unregisterCallback: () => {},
            convertFileSrc: path => path,
            metadata: { currentWindow: { label: 'main' }, windows: [{ label: 'main' }] },
        };
    });
    await page.addScriptTag({ content: dialog });
    await page.evaluate(() => window.mountAddAccount());
    await page.getByTestId('agy-add-account-trigger').click();
    await expect(page.getByRole('dialog', { name: /添加新账号|Add Account/ })).toBeVisible();
}

test('网页版默认使用 Refresh Token 导入，不再进入不可用的桌面 OAuth', async ({ page }) => {
    await mount(page);
    await expect(page.getByTestId('agy-add-tab-token')).toHaveClass(/text-blue-600/);
    await page.locator('textarea').fill('1//web-refresh-token-value');
    await page.getByRole('button', { name: /确认添加|Confirm/ }).click();
    await expect.poll(() => page.evaluate(() => window.addCalls)).toEqual([
        { email: '', refreshToken: '1//web-refresh-token-value' },
    ]);
    expect(await page.evaluate(() => window.invokeCalls.some(call => call.cmd === 'prepare_oauth_url'))).toBe(false);
});

test('网页版登录页提供扫描、令牌和文件入口，不显示 OAuth 授权按钮', async ({ page }) => {
    await mount(page);
    await page.getByTestId('agy-add-tab-oauth').click();
    await expect(page.getByTestId('agy-add-open-antigravity')).toBeVisible();
    await expect(page.getByTestId('agy-add-scan')).toBeVisible();
    await expect(page.getByTestId('agy-add-use-token')).toBeVisible();
    await expect(page.getByTestId('agy-add-use-file')).toBeVisible();
    await expect(page.getByRole('button', { name: /开始 OAuth|Start OAuth/ })).toHaveCount(0);

    await page.getByTestId('agy-add-scan').click();
    await expect.poll(() => page.evaluate(() => window.invokeCalls.filter(call => call.cmd === 'import_local_accounts_report').length)).toBe(1);
    await expect(page.getByRole('dialog').getByRole('status')).toContainText(/1/);
});

test('网页版选择文件后由服务端解析，不再依赖本机路径', async ({ page }) => {
    await mount(page);
    await page.getByTestId('agy-add-tab-import').click();
    const input = page.locator('input[type=file][accept*="vscdb"]');
    await input.setInputFiles({
        name: 'agy-export.json',
        mimeType: 'application/json',
        buffer: Buffer.from(JSON.stringify({ accounts: [{ refresh_token: '1//file-refresh-token-value' }] })),
    });
    await expect.poll(() => page.evaluate(() => {
        const call = window.invokeCalls.find(item => item.cmd === 'import_accounts_file');
        return call && { filename: call.args.filename, hasData: Boolean(call.args.data) };
    })).toEqual({ filename: 'agy-export.json', hasData: true });
    const status = page.getByRole('dialog').getByRole('status');
    await expect(status).toContainText('1');
    await expect(status).not.toContainText(/失败来源|failed sources/);
});

test('网页桥接把新增账号命令映射到服务端导入接口', async ({ page }) => {
    const requests = [];
    await page.route('http://localhost:4173/shim-test', route => route.fulfill({
        contentType: 'text/html', body: '<div id="root"></div>',
    }));
    await page.route('http://localhost:4173/api/**', route => {
        const request = route.request();
        requests.push({ path: new URL(request.url()).pathname, method: request.method(), body: request.postData() });
        if (request.url().endsWith('/api/status')) {
            return route.fulfill({ json: { ok: true, app_url: 'http://antigravity.test/' } });
        }
        return route.fulfill({ json: { ok: true, imported: 1, duplicate: 0, accounts: [], sources: [] } });
    });
    await page.goto('http://localhost:4173/shim-test');
    await page.addScriptTag({ content: shim });
    const result = await page.evaluate(async () => {
        const imported = await window.__TAURI_INTERNALS__.invoke('add_account', {
            email: 'new@example.com', refreshToken: '1//shim-refresh-token-value',
        });
        await window.__TAURI_INTERNALS__.invoke('import_accounts_file', {
            filename: 'accounts.json', data: 'e30=',
        });
        await window.__TAURI_INTERNALS__.invoke('import_local_accounts_report', {});
        let oauthError = '';
        try {
            await window.__TAURI_INTERNALS__.invoke('prepare_oauth_url', {});
        } catch (error) {
            oauthError = String(error);
        }
        return { imported, oauthError };
    });
    const byPath = path => requests.find(item => item.path === path);
    expect(byPath('/api/accounts/import-token')).toBeTruthy();
    expect(byPath('/api/accounts/import-file')).toBeTruthy();
    expect(byPath('/api/accounts/scan')).toBeTruthy();
    expect(JSON.parse(byPath('/api/accounts/import-token').body)).toEqual({
        refreshToken: '1//shim-refresh-token-value', name: '', email: 'new@example.com',
    });
    expect(JSON.parse(byPath('/api/accounts/import-file').body)).toEqual({ filename: 'accounts.json', data: 'e30=' });
    expect(result.oauthError).toContain('不托管 Google 授权');
});

test('扫描结果全部为重复账号时显示成功提示，失败来源使用友好文案', async ({ page }) => {
    await mount(page);
    await page.evaluate(() => {
        const original = window.__TAURI_INTERNALS__.invoke;
        window.__TAURI_INTERNALS__.invoke = async (cmd, args) => {
            if (cmd === 'import_local_accounts_report') {
                return { accounts: [], sources: [{ source: '当前 Antigravity 登录', status: 'duplicate' }] };
            }
            if (cmd === 'import_accounts_file') {
                return {
                    imported: 0, failed: 1, duplicate: 0, accounts: [],
                    sources: [{ source: 'broken.json', status: 'failed', message: '令牌已失效' }],
                };
            }
            return original(cmd, args);
        };
    });
    await page.getByTestId('agy-add-tab-oauth').click();
    await page.getByTestId('agy-add-scan').click();
    const status = page.getByRole('dialog').getByRole('status');
    await expect(status).toContainText(/已经保存过了|already saved/);

    await page.getByTestId('agy-add-tab-import').click();
    await page.locator('input[type=file][accept*="vscdb"]').setInputFiles({
        name: 'broken.json', mimeType: 'application/json',
        buffer: Buffer.from(JSON.stringify({ accounts: [{ refresh_token: '1//expired-refresh-token' }] })),
    });
    const alert = page.getByRole('dialog').getByRole('alert');
    await expect(alert).toContainText(/导入失败|Import failed/);
    await expect(alert).not.toContainText('status_failed');
});

test('核心更新轮询容忍瞬时网络错误，恢复后仍能确认结果', async ({ page }) => {
    let polls = 0;
    await page.route('http://localhost:4173/shim-update-test', route => route.fulfill({
        contentType: 'text/html', body: '<div id="root"></div>',
    }));
    await page.route('http://localhost:4173/api/**', route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/api/status') return route.fulfill({ json: { ok: true } });
        if (url.pathname === '/api/update' && route.request().method() === 'POST') {
            return route.fulfill({ json: { ok: true, jobId: 'job-1' } });
        }
        if (url.pathname === '/api/update' && route.request().method() === 'GET') {
            polls += 1;
            if (polls === 1) return route.abort('failed');
            return route.fulfill({
                json: { ok: true, running: '2.35.2', latest: '2.35.2', available: false,
                    update: { id: 'job-1', state: 'success', message: '已更新并重启：2.35.2' } },
            });
        }
        return route.fulfill({ json: { ok: true } });
    });
    await page.goto('http://localhost:4173/shim-update-test');
    await page.addScriptTag({ content: shim });
    const result = await page.evaluate(() => window.__TAURI_INTERNALS__.invoke('apply_update', {}));
    expect(polls).toBe(2);
    expect(result.running).toBe('2.35.2');
    expect(result.message).toContain('2.35.2');
});
