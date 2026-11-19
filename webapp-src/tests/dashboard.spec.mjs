import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const cardIds = ['total_tokens', 'input_tokens', 'output_tokens', 'cache_hit_rate',
    'first_text_latency', 'body_speed', 'account_status', 'aggregate_quota', 'average_input'];
const pageUrl = 'http://localhost:4173/';
const index = readFileSync(new URL('../dist/index.html', import.meta.url), 'utf8');

async function openDashboard(page, count) {
    const cards = cardIds.slice(0, count);
    const prefs = { rev: 1, dashboard: { cards, order: cardIds } };
    const totals = { input_tokens: 100, output_tokens: 100, cached_tokens: 0, total_tokens: 200, request_count: 1 };
    const usage = {
        today: totals, yesterday: totals, last_3_days: totals, last_7_days: totals, last_30_days: totals,
        by_model: [], by_model_today: [], by_model_yesterday: [], by_model_3_days: [], by_model_7_days: [],
        recent_performance: { first_text_seconds: 3.34, body_tokens_per_second: 156.8,
            sample_count: 10, model_count: 3, source_count: 1 },
    };
    let writes = 0;
    await page.route('**/api/**', route => {
        const path = new URL(route.request().url()).pathname;
        if (path === '/api/preferences' && route.request().method() !== 'GET') writes++;
        const fixtures = {
            '/api/status': { accounts: [], service: { active: true } },
            '/api/preferences': { preferences: prefs },
            '/api/usage': usage,
            '/api/quotas': { quotas: {} },
        };
        return route.fulfill({ json: { ok: true, ...fixtures[path] } });
    });
    await page.route(pageUrl, route => route.fulfill({ contentType: 'text/html', body: index.replace(
        '<script src="/tauri-shim.js"></script>',
        `<script>window.__AGY_PREFERENCES__=${JSON.stringify(prefs)};</script>$&`,
    ) }));
    await page.goto(pageUrl);
    return () => writes;
}

for (const width of [390, 1440]) {
    for (let count = 1; count <= cardIds.length; count++) {
        test(`${count} 张卡片铺满 ${width}px 宽视口`, async ({ page }) => {
            await page.setViewportSize({ width, height: 820 });
            await openDashboard(page, count);
            const grid = page.locator('[data-dashboard-cards]');
            await expect(grid.locator('[data-dashboard-card]')).toHaveCount(count);
            const split = Math.ceil(count / 2);
            const expectedRows = count < 4 ? [count] : [split, count - split];
            const rows = await grid.evaluate(element => {
                const gridBox = element.getBoundingClientRect();
                return Array.from(element.children, row => {
                    const boxes = Array.from(row.children, card => card.getBoundingClientRect());
                    return {
                        count: boxes.length,
                        top: boxes[0].top,
                        leftGap: boxes[0].left - gridBox.left,
                        rightGap: gridBox.right - boxes.at(-1).right,
                        widthSpread: Math.max(...boxes.map(card => card.width)) - Math.min(...boxes.map(card => card.width)),
                    };
                });
            });
            expect(rows.map(row => row.count)).toEqual(expectedRows);
            if (rows.length === 2) expect(rows[1].top).toBeGreaterThan(rows[0].top);
            for (const row of rows) {
                expect(Math.abs(row.leftGap)).toBeLessThan(1);
                expect(Math.abs(row.rightGap)).toBeLessThan(1);
                expect(row.widthSpread).toBeLessThan(1);
            }
        });
    }
}

test('性能指标有值，主题和汉化命令不写卡片设置', async ({ page }) => {
    const preferenceWrites = await openDashboard(page, 6);
    await expect(page.locator('[data-dashboard-card="first_text_latency"]')).toContainText('3.34');
    await expect(page.locator('[data-dashboard-card="body_speed"]')).toContainText('156.8');
    const responses = await page.evaluate(async () => {
        const bridge = window.__TAURI_INTERNALS__;
        const theme = await bridge.invoke('set_window_theme', { theme: 'dark' });
        const translationFailed = await bridge.invoke('set_app_translation', { enabled: true }).then(
            () => false, () => true,
        );
        return { theme, translationFailed };
    });
    expect(responses).toEqual({ theme: null, translationFailed: true });
    expect(preferenceWrites()).toBe(0);
});

for (const width of [390, 1920]) {
    test(`设置页与首页、账号页宽度一致（${width}px）`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await openDashboard(page, 6);
        const pageContainer = page.locator('main .max-w-7xl').first();
        await expect(pageContainer).toBeVisible();
        const dashboard = await pageContainer.boundingBox();

        await page.goto(new URL('/accounts', pageUrl).href);
        await expect(pageContainer).toBeVisible();
        const accounts = await pageContainer.boundingBox();

        await page.goto(new URL('/settings', pageUrl).href);
        const settings = page.locator('.settings-layout');
        await expect(settings.getByRole('tablist')).toBeVisible();
        const frame = await settings.boundingBox();
        for (const reference of [dashboard, accounts]) {
            expect(frame.x).toBeCloseTo(reference.x);
            expect(frame.width).toBeCloseTo(reference.width);
        }

        const sidebar = await settings.locator('aside').boundingBox();
        const content = await settings.locator('.settings-content').boundingBox();
        expect(sidebar.x).toBeGreaterThan(frame.x);
        expect(content.x + content.width).toBeLessThan(frame.x + frame.width);
    });
}
