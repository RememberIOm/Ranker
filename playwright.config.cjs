const { defineConfig } = require('@playwright/test');
const baseURL = process.env.RANKER_E2E_BASE_URL || 'http://127.0.0.1:8097';
if (!['localhost', '127.0.0.1'].includes(new URL(baseURL).hostname)) throw new Error('브라우저 테스트는 로컬 테스트 서버만 사용합니다.');
module.exports = defineConfig({
  testDir: './tests/browser',
  workers: 1,
  timeout: 30000,
  reporter: 'list',
  use: {
    baseURL,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    launchOptions: process.env.RANKER_CHROME_PATH ? { executablePath: process.env.RANKER_CHROME_PATH } : {},
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1280, height: 900 }, colorScheme: 'light' } },
    { name: 'mobile-dark', use: { viewport: { width: 390, height: 844 }, colorScheme: 'dark', isMobile: true, hasTouch: true } },
  ],
  webServer: process.env.RANKER_E2E_BASE_URL ? undefined : {
    command: 'uv run python scripts/browser_server.py',
    url: `${baseURL}/health`, reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5000 },
  },
});
