import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const bootstrap = readFileSync(new URL('../../powertools/browser.js', import.meta.url), 'utf8');

test('点击插件更新先确认，取消不会提交；接口仍为同源', async ({ page }) => {
    await page.route('http://localhost:4173/plugin-update-test', route => route.fulfill({ contentType: 'text/html', body: '<button data-testid="agy-install-update">立即更新</button>' }));
    await page.route('**/__agy-powertools/rpc', route => route.fulfill({ json: { status: 'idle' } }));
    await page.goto('http://localhost:4173/plugin-update-test');
    await page.addScriptTag({ content: bootstrap });
    await page.evaluate(() => {
        window.submitted = 0;
        const button = document.querySelector('[data-testid="agy-install-update"]');
        button.addEventListener('click', () => { window.submitted++; });
        document.body.append(document.createElement('span'));
    });
    await expect(page.getByTestId('agy-install-update')).toHaveAttribute('data-web-confirmed', '1');
    page.once('dialog', dialog => dialog.dismiss());
    await page.getByTestId('agy-install-update').click();
    expect(await page.evaluate(() => window.submitted)).toBe(0);
    page.once('dialog', dialog => dialog.accept());
    await page.getByTestId('agy-install-update').click();
    expect(await page.evaluate(() => window.submitted)).toBe(1);
});
