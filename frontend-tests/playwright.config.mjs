import {defineConfig} from '@playwright/test';

export default defineConfig({
    testDir: '.',
    testMatch: 'flock-map.spec.mjs',
    timeout: 45_000,
    expect: {timeout: 10_000},
    use: {
        baseURL: 'http://127.0.0.1:4173',
        browserName: 'chromium',
        channel: 'chrome',
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure',
    },
    webServer: {
        command: 'node server.mjs',
        cwd: '.',
        port: 4173,
        reuseExistingServer: false,
        timeout: 20_000,
    },
});
