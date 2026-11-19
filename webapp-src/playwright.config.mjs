import { defineConfig } from '@playwright/test';

export default defineConfig({
    testDir: './tests',
    workers: 2,
    webServer: {
        command: 'npm run preview -- --strictPort',
        url: 'http://localhost:4173/',
    },
});
