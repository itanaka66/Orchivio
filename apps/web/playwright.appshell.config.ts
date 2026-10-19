import { defineConfig, devices } from '@playwright/test'

// Browser tests of the generated-application shell (docs/PWA.md): they run against a real Orchivio API server
// started by e2e/app_server.py. Run with: npm run e2e:appshell (starts the server automatically).
export default defineConfig({
  testDir: 'e2e',
  testMatch: '**/appshell.e2e.ts',
  timeout: 45_000,
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? [['list'], ['github']] : 'list',
  use: { serviceWorkers: 'allow', trace: 'retain-on-failure' },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
  webServer: {
    command: 'python e2e/app_server.py 4180',
    url: 'http://127.0.0.1:4180/healthz',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
})
