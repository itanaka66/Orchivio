import { defineConfig, devices } from '@playwright/test'

// Browser tests of the installable app (docs/PWA.md): they run the production build in real Chromium. `npm run e2e` builds first.
export default defineConfig({
  testDir: 'e2e',
  testMatch: '**/*.e2e.ts',
  timeout: 45_000,
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? [['list'], ['github']] : 'list',
  use: { baseURL: 'http://localhost:4173', serviceWorkers: 'allow', trace: 'retain-on-failure' },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
  webServer: { command: 'node e2e/server.mjs dist', url: 'http://localhost:4173/offline.html', reuseExistingServer: !process.env.CI, timeout: 30_000 },
})
