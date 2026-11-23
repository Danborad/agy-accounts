import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const script = readFileSync(new URL('../../i18n/agy-i18n.js', import.meta.url), 'utf8');

test('汉化不翻译模型正文和阶段汇报', async ({ page }) => {
    await page.setContent([
        '<div data-markdown-root>I am starting on the ESA resource cache updates.</div>',
        '<button data-testid="thinking-collapsible-trigger">Thought for 4s</button>',
    ].join(''));
    await page.addScriptTag({ content: script });
    await expect.poll(() => page.locator('button').innerText(), { timeout: 5000 }).toContain('思考');
    await page.waitForTimeout(3000);
    const content = await page.locator('[data-markdown-root]').innerText();
    expect(content).toContain('ESA resource cache');
    expect(content).not.toMatch(/[\u4e00-\u9fff]/);
});
