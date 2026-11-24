import { readFileSync } from 'node:fs';
import { buildSync } from 'esbuild';
import { expect, test } from '@playwright/test';

const script = readFileSync(new URL('../../powertools/core-update.js', import.meta.url), 'utf8');
const react = buildSync({
    stdin: { contents: "import React from 'react';import{createRoot}from'react-dom/client';window.React=React;window.mountUpdate=()=>createRoot(document.getElementById('root')).render(React.createElement(window.__agyWebCoreUpdate));", resolveDir: process.cwd() },
    bundle: true, write: false, format: 'iife',
}).outputFiles[0].text;

for (const state of ['success', 'failed', 'unknown', 'available']) {
    test(`核心更新检查展示真实结果：${state}`, async ({ page }) => {
        await page.route('http://localhost:4173/update-test', route => route.fulfill({
            contentType: 'text/html', body: '<div id="root"></div>',
        }));
        await page.goto('http://localhost:4173/update-test');
        await page.evaluate(state => {
            window.calls = [];
            window.result = { running: '2.34.0', check: { state: 'idle' } };
            window.__agyPowertoolsWeb = { rpc: async method => {
                window.calls.push(method);
                if (method === 'coreUpdate.check') {
                    const latest = state === 'success' ? '2.34.0' : '2.35.0';
                    window.result = { running: state === 'unknown' ? '' : '2.34.0', latest, available: state === 'available',
                        check: { state: state === 'failed' ? 'failed' : 'success', error: state === 'failed' ? '官方查询网络超时' : '', latest, checked_at: 123 } };
                }
                return window.result;
            } };
        }, state);
        await page.addScriptTag({ content: react });
        await page.addScriptTag({ content: script });
        await page.evaluate(() => window.mountUpdate());
        await expect(page.getByTestId('agy-core-update-running')).toContainText('2.34.0');
        await expect(page.getByTestId('agy-core-update-status')).toContainText('尚未检查');
        await page.getByTestId('agy-core-update-check').click();
        const expected = { success: '官方最新版本', failed: '网络超时', unknown: '无法确认当前运行版本', available: '发现可用更新' };
        await expect(page.getByTestId('agy-core-update-status')).toContainText(expected[state]);
        expect(await page.evaluate(() => window.calls.filter(method => method !== 'coreUpdate.status'))).toEqual(['coreUpdate.check']);
    });
}

test('核心检查过程中禁用按钮并轮询，刷新不会重复查询官方', async ({ page }) => {
    await page.route('http://localhost:4173/update-test', route => route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' }));
    await page.goto('http://localhost:4173/update-test');
    await page.evaluate(() => {
        window.checkCalls = 0;
        window.checking = false;
        window.finish = false;
        window.__agyPowertoolsWeb = { rpc: async method => {
            if (method === 'coreUpdate.check') { window.checkCalls++; window.checking = true; }
            return { running: '2.35.0', latest: '2.35.0', available: false,
                check: { state: window.finish ? 'success' : window.checking ? 'checking' : 'idle', latest: '2.35.0', checked_at: 123 } };
        } };
    });
    await page.addScriptTag({ content: react });
    await page.addScriptTag({ content: script });
    await page.evaluate(() => window.mountUpdate());
    const button = page.getByTestId('agy-core-update-check');
    await button.click();
    await expect(button).toBeDisabled();
    await expect(page.getByTestId('agy-core-update-status')).toContainText('正在查询');
    await page.evaluate(() => { window.finish = true; });
    await expect(button).toBeEnabled();
    await expect(page.getByTestId('agy-core-update-status')).toContainText('已是官方最新版本');
    expect(await page.evaluate(() => window.checkCalls)).toBe(1);
});
