import { defineConfig, devices } from '@playwright/test'

// Studio end-to-end tests: the full Orchivio Studio UI against a real API server.
// Run with: npm run e2e:studio  (builds the Studio first, then starts two servers).
export default defineConfig({
  testDir: 'e2e',
  testMatch: '**/studio.e2e.ts',
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? [['list'], ['github']] : 'list',
  use: { baseURL: 'http://localhost:4191', trace: 'retain-on-failure' },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
  webServer: [
    {
      command: 'python e2e/studio_api.py 4190',
      url: 'http://127.0.0.1:4190/healthz',
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: 'node e2e/studio_proxy.mjs dist 4190',
      url: 'http://localhost:4191',
      reuseExistingServer: !process.env.CI,
      timeout: 10_000,
      env: { PORT: '4191' },
    },
  ],
})
