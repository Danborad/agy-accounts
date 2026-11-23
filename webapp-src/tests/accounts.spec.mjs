import { readFileSync } from 'node:fs';
import { buildSync } from 'esbuild';
import { expect, test } from '@playwright/test';

const script = readFileSync(new URL('../../powertools/accounts.js', import.meta.url), 'utf8');
const browserScript = readFileSync(new URL('../../powertools/browser.js', import.meta.url), 'utf8');
const accountCss = browserScript.match(/accountStyle\.textContent = `([\s\S]*?)`;/)?.[1] || '';
const react = buildSync({
    stdin: { contents: "import React from 'react';import*as ReactDOM from'react-dom';import{createRoot}from'react-dom/client';window.React=React;window.ReactDOM=ReactDOM;window.mountAccounts=()=>createRoot(document.getElementById('root')).render(React.createElement(window.__agyWebAccountsPanel));", resolveDir: process.cwd() },
    bundle: true, write: false, format: 'iife',
}).outputFiles[0].text;

async function openAccounts(page) {
    await page.route('http://localhost:4173/accounts-test', route => route.fulfill({
        contentType: 'text/html', body: '<div id="root"></div>',
    }));
    await page.goto('http://localhost:4173/accounts-test');
    await page.addStyleTag({ content: accountCss });
    await page.evaluate(() => {
        window.testState = {
            accounts: [
                { id: 'a', name: '主账号', email: 'a@example.com', is_current: true, has_token: true, quota: {
                    tier: 'Pro', groups: [{ key: 'gemini', label: 'Gemini', windows: { '5h': { remaining: .8 } } }],
                } },
                { id: 'b', name: '备用账号', email: 'b@example.com', has_token: true, quota: {
                    tier: 'Ultra', groups: [{ key: 'claude', label: 'Claude', windows: { weekly: { remaining: .5 } } }],
                } },
                { id: 'c', name: 'c@example.com', email: 'c@example.com', has_token: false, quota: {} },
            ], currentId: 'a', switchJob: {},
        };
        window.switchCalls = [];
        window.aliasCalls = [];
        window.__agyPowertoolsWeb = { rpc: async (method, payload) => {
            if (method === 'serverAccounts.switch') {
                window.switchCalls.push(payload);
                window.testState = { ...window.testState, switchJob: {
                    id: 'test-job', state: 'failed', active: false, message: '验证失败，已回滚原账号',
                } };
                return { jobId: 'test-job' };
            }
            if (method === 'serverAccounts.alias') {
                window.aliasCalls.push(payload);
                window.testState = { ...window.testState, accounts: window.testState.accounts.map(item =>
                    item.id === payload.accountId ? { ...item, name: payload.alias.trim() || item.email } : item) };
                return window.testState;
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

        await page.getByTestId('agy-server-privacy').click();
        await expect(first.locator('.agy-server-email')).toHaveText('***@example.com');
        await expect(first.locator('.agy-server-alias')).toHaveText('主账号');
        await page.getByTestId('agy-server-privacy').click();
        await expect(first.locator('.agy-server-email')).toHaveText('a@example.com');
        await expect(first.locator('.agy-server-alias')).toHaveText('主账号');

        await page.locator('[data-server-account="c"]').getByTestId('agy-server-alias-add').click();
        const aliasDialog = page.getByRole('dialog', { name: '编辑账号备注' });
        await expect(aliasDialog).toBeVisible();
        expect(await aliasDialog.evaluate(element => element.matches(':modal'))).toBe(true);
        const modalCard = aliasDialog.locator('.pt-card');
        const modalBox = await modalCard.boundingBox();
        const viewport = page.viewportSize();
        expect(Math.abs(modalBox.x + modalBox.width / 2 - viewport.width / 2)).toBeLessThan(3);
        expect(Math.abs(modalBox.y + modalBox.height / 2 - viewport.height / 2)).toBeLessThan(3);
        const actionTops = await aliasDialog.locator('.agy-server-modal-actions .agy-server-button')
            .evaluateAll(nodes => nodes.map(node => Math.round(node.getBoundingClientRect().top)));
        expect(new Set(actionTops).size).toBe(1);
        await page.getByTestId('agy-server-alias-input').fill('新备注');
        await page.getByTestId('agy-server-alias-save').click();
        await expect(page.locator('[data-server-account="c"] .agy-server-alias')).toHaveText('新备注');
        await page.locator('[data-server-account="c"]').getByTestId('agy-server-alias-edit').click();
        await page.getByTestId('agy-server-alias-clear').click();
        await expect(page.locator('[data-server-account="c"] .agy-server-alias')).toHaveCount(0);
        await expect(page.locator('[data-server-account="c"]').getByTestId('agy-server-alias-add')).toBeVisible();
        expect(await page.evaluate(() => window.aliasCalls)).toEqual([
            { accountId: 'c', alias: '新备注' },
            { accountId: 'c', alias: '' },
        ]);

        await page.getByTestId('agy-server-switch').first().click();
        await expect(page.getByRole('dialog')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.getByRole('dialog')).toHaveCount(0);
        expect(await page.evaluate(() => window.switchCalls.length)).toBe(0);
        await page.getByTestId('agy-server-switch').first().click();
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

for (const mode of ['transformed-container', 'no-portal', 'zoomed-root']) {
    test(`备注弹窗在滚动和页面变换下仍居中（${mode}）`, async ({ page }) => {
        await page.setViewportSize({ width: 1440, height: 900 });
        await openAccounts(page);
        await page.evaluate(mode => {
            const root = document.getElementById('root');
            root.style.cssText = 'height:500px;width:900px;overflow:auto;transform:translateX(60px);margin:100px;';
            const spacer = document.createElement('div');
            spacer.style.height = '1000px';
            root.append(spacer);
            if (mode === 'no-portal') window.ReactDOM = {};
            if (mode === 'transformed-container') document.body.style.transform = 'translateX(40px)';
            if (mode === 'zoomed-root') document.documentElement.style.zoom = '1.5';
        }, mode);
        await page.locator('[data-server-account="a"]').getByTestId('agy-server-alias-edit').click();
        const dialog = page.getByRole('dialog', { name: '编辑账号备注' });
        await expect(dialog).toBeVisible();
        expect(await dialog.evaluate(element => element.matches(':modal'))).toBe(true);
        const box = await dialog.locator('.pt-card').boundingBox();
        expect(Math.abs(box.x + box.width / 2 - 720)).toBeLessThan(3);
        expect(Math.abs(box.y + box.height / 2 - 450)).toBeLessThan(3);
        await page.evaluate(() => { document.getElementById('root').scrollTop = 600; });
        const afterScroll = await dialog.locator('.pt-card').boundingBox();
        expect(Math.abs(afterScroll.x - box.x)).toBeLessThan(3);
        expect(Math.abs(afterScroll.y - box.y)).toBeLessThan(3);
    });
}
