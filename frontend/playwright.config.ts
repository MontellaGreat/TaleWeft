import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: '**/*.spec.ts',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: 'http://127.0.0.1:4317',
    browserName: 'chromium',
    viewport: { width: 1400, height: 1000 },
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npx vite --config tests/e2e/vite.config.ts',
    url: 'http://127.0.0.1:4317',
    reuseExistingServer: false,
    timeout: 60000,
  },
})
