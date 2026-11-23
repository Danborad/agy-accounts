import { readFileSync } from 'node:fs';
import { buildSync } from 'esbuild';
import { expect, test } from '@playwright/test';

const script = readFileSync(new URL('../../powertools/accounts.js', import.meta.url), 'utf8');
const react = buildSync({
    stdin: { contents: "import React from 'react';import{createRoot}from'react-dom/client';window.React=React;window.mountAccounts=()=>createRoot(document.getElementById('root')).render(React.createElement(window.__agyWebAccountsPanel));", resolveDir: process.cwd() },
    bundle: true, write: false, format: 'iife',
}).outputFiles[0].text;

async function openAccounts(page) {
    await page.route('http://localhost:4173/accounts-test', route => route.fulfill({
        contentType: 'text/html', body: '<div id="root"></div>',
    }));
    await page.goto('http://localhost:4173/accounts-test');
    await page.evaluate(() => {
        window.testState = {
            accounts: [
                { id: 'a', name: '主账号', email: 'a@example.com', is_current: true, has_token: true, quota: {
                    tier: 'Pro', groups: [{ key: 'gemini', label: 'Gemini', windows: { '5h': { remaining: .8 } } }],
                } },
                { id: 'b', name: '备用账号', email: 'b@example.com', has_token: true, quota: {
                    tier: 'Ultra', groups: [{ key: 'claude', label: 'Claude', windows: { weekly: { remaining: .5 } } }],
                } },
                { id: 'c', name: '待登录', email: 'c@example.com', has_token: false, quota: {} },
            ], currentId: 'a', switchJob: {},
        };
        window.switchCalls = [];
        window.__agyPowertoolsWeb = { rpc: async (method, payload) => {
            if (method === 'serverAccounts.switch') {
                window.switchCalls.push(payload);
                window.testState.switchJob = { id: 'test-job', state: 'failed', active: false, message: '验证失败，已回滚原账号' };
                return { jobId: 'test-job' };
            }
            return window.testState;
        } };
    });
    await page.addScriptTag({ content: react });
    await page.addScriptTag({ content: script });
    await page.evaluate(() => window.mountAccounts());
}

for (const width of [390, 1440]) {
    test(`内置账号仅显示额度与切换，确认后才提交（${width}px）`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await openAccounts(page);
        await expect(page.locator('[data-server-account]')).toHaveCount(3);
        await expect(page.getByTestId('agy-server-current')).toHaveText('当前账号');
        await expect(page.getByTestId('agy-server-accounts')).toContainText('80%');
        await expect(page.getByTestId('agy-server-accounts')).toContainText('50%');
        const first = page.locator('[data-server-account="a"]');
        await expect(first.locator('.agy-server-email')).toHaveText('a@example.com');
        await expect(first.locator('.agy-server-alias')).toHaveText('主账号');
        await expect(first.locator('.agy-server-login')).toHaveText('已保存登录');
        await expect(first.getByRole('progressbar').first()).toHaveAttribute('aria-valuenow', '80');
        await expect(first.locator('.agy-server-fill')).toHaveAttribute('style', /width:\s*80%/);
        const missing = page.locator('[data-server-account="c"]');
        await expect(missing.locator('.agy-server-login')).toHaveText('缺少登录');
        await expect(missing.getByTestId('agy-server-switch')).toBeDisabled();
        await page.getByTestId('agy-server-switch').first().click();
        await expect(page.getByRole('dialog')).toBeVisible();
        expect(await page.evaluate(() => window.switchCalls.length)).toBe(0);
        await page.getByRole('button', { name: '取消', exact: true }).click();
        expect(await page.evaluate(() => window.switchCalls.length)).toBe(0);
        await page.getByTestId('agy-server-switch').first().click();
        await page.getByTestId('agy-server-switch-confirm').click();
        await expect(page.getByRole('alert')).toHaveText('验证失败，已回滚原账号');
        expect(await page.evaluate(() => window.switchCalls)).toEqual([
            { accountId: 'b', expectedCurrentId: 'a', confirmed: true },
        ]);
        expect(await page.evaluate(() => sessionStorage.getItem('agy-web-account-switch-job'))).toBeNull();
        await expect(page.getByTestId('agy-server-current')).toHaveText('当前账号');
    });
}
