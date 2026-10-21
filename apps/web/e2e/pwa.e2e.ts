import { expect, test, type BrowserContext, type Page } from '@playwright/test'

const BASE = 'http://localhost:4173'

async function control(path: string) {
  const res = await fetch(`${BASE}/__control/${path}`, { method: path.startsWith('release') ? 'POST' : 'GET' })
  return res.json() as Promise<Record<string, number>>
}

/** Wait until the service worker is active and controlling this page (a reload makes the first visit controlled). */
async function controlled(page: Page) {
  await page.evaluate(() => navigator.serviceWorker.ready)
  if (!(await page.evaluate(() => !!navigator.serviceWorker.controller))) await page.reload({ waitUntil: 'load' }) // a first visit is not controlled until the next load
  await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller), { timeout: 10_000 }).toBe(true)
}

// Playwright's offline emulation does not reach WebKit's service workers, so the offline behaviour is verified in Chromium and Firefox.
const needsOfflineEmulation = (browserName: string) => browserName === 'webkit' ? 'Playwright cannot emulate offline for WebKit service workers' : false

const cached = (page: Page) => page.evaluate(async () => {
  const out: string[] = []
  for (const name of await caches.keys()) for (const req of await (await caches.open(name)).keys()) out.push(`${name} ${new URL(req.url).pathname}`)
  return out
})

test.beforeEach(async () => { await control('reset') })

test('the manifest is served as a manifest and its icons are PNGs', async ({ page }) => {
  await page.goto('/')
  const res = await page.request.get('/manifest.webmanifest')
  expect(res.headers()['content-type']).toContain('application/manifest+json')
  const manifest = await res.json()
  expect(manifest).toMatchObject({ name: 'Orchivio Studio', display: 'standalone', start_url: '/', scope: '/' })
  for (const icon of manifest.icons as { src: string }[]) {
    const icon_res = await page.request.get(icon.src)
    expect(icon_res.status()).toBe(200)
    expect(icon_res.headers()['content-type']).toBe('image/png')
  }
  expect(await page.locator('link[rel=manifest]').getAttribute('href')).toBe('/manifest.webmanifest')
})

test('the app is installable: Chromium reports no manifest or worker defects', async ({ page, context, browserName }) => {
  test.skip(browserName !== 'chromium', 'the installability report is a Chromium (CDP) feature')
  await page.goto('/')
  await controlled(page)
  const cdp = await context.newCDPSession(page)
  const manifest = await cdp.send('Page.getAppManifest')
  expect(manifest.errors).toEqual([])
  const parsed = JSON.parse(manifest.data ?? '{}')
  expect(parsed.name).toBe('Orchivio Studio')
  expect(parsed.display).toBe('standalone')
  expect(parsed.icons.length).toBeGreaterThanOrEqual(3)
  const problems = await cdp.send('Page.getInstallabilityErrors')
  // headless Chromium cannot show the install prompt, but it reports every manifest or worker defect here
  expect(problems.installabilityErrors.filter((e) => !['not-from-secure-origin'].includes(e.errorId))).toEqual([])
})

test('the worker caches the shell and nothing from the API', async ({ page }) => {
  await page.goto('/')
  await controlled(page)
  await expect.poll(() => cached(page)).toEqual(expect.arrayContaining([expect.stringContaining('/offline.html'), expect.stringContaining('/manifest.webmanifest'), expect.stringContaining('/icons/icon-192.png')]))
  // exercise the API through the page, signed in and out, several times
  for (let i = 0; i < 3; i++) await page.evaluate(() => fetch('/api/v1/ping').then((r) => r.json()))
  await page.evaluate(() => fetch('/api/v1/organizations'))
  expect((await control('counts'))['/api/v1/ping']).toBe(3) // every call reached the network: none was answered by the worker
  expect((await cached(page)).filter((e) => e.includes('/api/') || e.includes('/healthz'))).toEqual([])
  // and the body that came from the API is not in any cache either
  const leaked = await page.evaluate(async () => {
    for (const name of await caches.keys()) for (const req of await (await caches.open(name)).keys()) {
      const res = await (await caches.open(name)).match(req)
      if ((await res?.text())?.includes('private-data-that-must-never-be-cached')) return true
    }
    return false
  })
  expect(leaked).toBe(false)
})

test('offline: the shell still opens, says it is offline, and API calls fail instead of being served from a cache', async ({ page, context, browserName }) => {
  test.skip(!!needsOfflineEmulation(browserName), needsOfflineEmulation(browserName) || '')
  await page.goto('/')
  await controlled(page)
  await page.evaluate(() => fetch('/api/v1/ping'))
  await context.setOffline(true)
  await page.reload()
  await expect(page.getByRole('heading', { name: /Orchivio Studio/ })).toBeVisible()
  await expect(page.getByText('You are offline.')).toBeVisible()
  const result = await page.evaluate(() => fetch('/api/v1/ping').then(() => 'answered', () => 'failed'))
  expect(result).toBe('failed') // no stale answer from a cache
  await context.setOffline(false)
  await expect(page.getByText('You are offline.')).toBeHidden({ timeout: 15_000 })
})

test('offline: a page that was never loaded falls back to the cached shell, not to a browser error', async ({ page, context, browserName }) => {
  test.skip(!!needsOfflineEmulation(browserName), needsOfflineEmulation(browserName) || '')
  await page.goto('/')
  await controlled(page)
  await context.setOffline(true)
  await page.goto('/some/deep/link')
  await expect(page.getByRole('heading', { name: /Orchivio Studio/ })).toBeVisible()
  await context.setOffline(false)
})

async function newRelease(page: Page, context: BrowserContext) {
  await control('release?n=2')
  await page.evaluate(() => navigator.serviceWorker.getRegistration().then((r) => r?.update()))
  await context.setOffline(false)
}

test('a new release waits until the person asks for it, then takes over', async ({ page, context }) => {
  await page.goto('/')
  await controlled(page)
  const before = await page.evaluate(() => fetch('/sw.js', { cache: 'no-store' }).then((r) => r.text()))
  expect(before).not.toContain('release2')
  await newRelease(page, context)
  await expect(page.getByText('A new version of Studio is ready.')).toBeVisible({ timeout: 15_000 })
  // nothing reloaded by itself: the old worker still controls the page and the page is the one we opened
  expect(await page.evaluate(() => navigator.serviceWorker.controller?.state)).toBe('activated')
  const marker = await page.evaluate(() => { (window as unknown as { __kept: number }).__kept = 42; return 42 })
  expect(marker).toBe(42)
  await Promise.all([page.waitForEvent('load', { timeout: 15_000 }), page.getByRole('button', { name: 'Reload to update' }).click()])
  expect(await page.evaluate(() => (window as unknown as { __kept?: number }).__kept)).toBeUndefined() // the page really reloaded
  await controlled(page)
  await expect(page.getByText('A new version of Studio is ready.')).toBeHidden()
  const keys = await cached(page)
  expect(keys.every((k) => k.startsWith('orchivio-shell-release2') || !k.startsWith('orchivio-shell-'))).toBe(true) // old shell caches are gone
  expect(await page.evaluate(() => fetch('/sw.js', { cache: 'no-store' }).then((r) => r.text()))).toContain('release2')
})

test('signing out clears what the worker cached', async ({ page }) => {
  await page.goto('/')
  await controlled(page)
  await page.getByLabel('Email').fill('a@example.com')
  await page.getByLabel('Password').fill('a long passphrase')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByText('Ada')).toBeVisible()
  expect((await cached(page)).length).toBeGreaterThan(0)
  await page.getByRole('button', { name: 'Sign out' }).click()
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
  await expect.poll(() => cached(page)).toEqual([])
})

test('the layout works on a phone: no horizontal scrolling and a visible sign-in form', async ({ browser }) => {
  const phone = await browser.newContext({ viewport: { width: 375, height: 667 }, isMobile: true, hasTouch: true })
  const page = await phone.newPage()
  await page.goto(`${BASE}/`)
  await page.waitForLoadState('networkidle')
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  await phone.close()
})
