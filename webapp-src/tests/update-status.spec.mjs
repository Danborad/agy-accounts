import { buildSync } from 'esbuild';
import { expect, test } from '@playwright/test';

const app = buildSync({
    stdin: {
        contents: [
            "import React from 'react';",
            "import{createRoot}from'react-dom/client';",
            "import UpdateButton from './src/components/navbar/UpdateButton.tsx';",
            "import UpdateSettings from './src/components/settings/UpdateSettings.tsx';",
            "window.mountUpdateStatus=()=>createRoot(document.getElementById('root')).render(",
            "React.createElement('div',null,React.createElement(UpdateButton),React.createElement(UpdateSettings)));",
        ].join(''),
        resolveDir: process.cwd(),
    },
    bundle: true, write: false, format: 'iife', jsx: 'automatic',
}).outputFiles[0].text;

async function mount(page, state) {
    await page.route('http://localhost:4173/update-status-test', route => route.fulfill({
        contentType: 'text/html', body: '<div id="root"></div>',
    }));
    await page.goto('http://localhost:4173/update-status-test');
    await page.evaluate(initial => {
        window.updateState = initial;
        window.updateCalls = [];
        window.__TAURI_INTERNALS__ = {
            invoke: async cmd => {
                window.updateCalls.push(cmd);
                if (cmd === 'get_update') return { ...window.updateState };
                if (cmd === 'check_update') return { ...window.updateState };
                return null;
            },
            transformCallback: id => id,
            unregisterCallback: () => {},
            convertFileSrc: path => path,
            metadata: { currentWindow: { label: 'main' }, windows: [{ label: 'main' }] },
        };
    }, state);
    await page.addScriptTag({ content: app });
    await page.evaluate(() => window.mountUpdateStatus());
}

test('设置页检查到已是最新后，导航栏旧更新徽标立即消失', async ({ page }) => {
    await mount(page, { running: '2.35.0', latest: '2.35.1', available: true });
    await expect(page.getByRole('button', { name: '更新 2.35.1' })).toBeVisible();
    await page.evaluate(() => {
        window.updateState = { running: '2.35.1', latest: '2.35.1', available: false };
    });
    await page.getByRole('button', { name: '检查更新' }).click();
    await expect(page.getByRole('button', { name: /更新 2\.35\.1/ })).toHaveCount(0);
    await expect(page.getByText('当前版本：')).toContainText('2.35.1');
    await expect(page.getByText('暂未发现待应用的更新。')).toBeVisible();
});

test('切回页面时重新核对版本，外部更新后不会保留旧徽标', async ({ page }) => {
    await mount(page, { running: '2.35.0', latest: '2.35.1', available: true });
    await expect(page.getByRole('button', { name: '更新 2.35.1' })).toBeVisible();
    await page.evaluate(() => {
        window.updateState = { running: '2.35.1', latest: '2.35.1', available: false };
        window.dispatchEvent(new Event('focus'));
    });
    await expect(page.getByRole('button', { name: /更新 2\.35\.1/ })).toHaveCount(0);
    expect(await page.evaluate(() => window.updateCalls.filter(cmd => cmd === 'get_update').length)).toBeGreaterThanOrEqual(2);
});
