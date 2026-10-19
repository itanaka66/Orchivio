import { expect, test, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// The generated application's own PWA shell, against the real server (e2e/app_server.py): sign-in, pages drawn from the page API, install, offline.
const APP = readFileSync(join(process.cwd(), 'e2e', '.app-id'), 'utf8').trim()
const SERVER = 'http://127.0.0.1:4180'
const SHELL = `${SERVER}/api/v1/apps/${APP}/shell/`
const PASS = 'a much better pass phrase 2'

async function signIn(page: Page, user = 'ada') {
  await page.goto(SHELL)
  await page.getByLabel('Username').fill(user)
  await page.getByLabel('Password').fill(PASS)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Product list' })).toBeVisible()
}

test('signs in and draws the list, with only what the server allows', async ({ page }) => {
  await signIn(page)
  await expect(page.getByRole('cell', { name: 'Bolt', exact: true })).toBeVisible()
  await expect(page.getByRole('cell', { name: 'Nut', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'New' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Delete' }).first()).toBeVisible()
})

test('a viewer sees the list but is offered no way to change it', async ({ page }) => {
  await signIn(page, 'vera')
  await expect(page.getByRole('cell', { name: 'Bolt', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'New' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Delete' })).toHaveCount(0)
})

test('a wrong password is refused', async ({ page }) => {
  await page.goto(SHELL)
  await page.getByLabel('Username').fill('ada')
  await page.getByLabel('Password').fill('not the password at all')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('alert')).toHaveText('invalid username or password')
})

test('creates a record through the form; markup in data is shown as text and never runs', async ({ page }) => {
  await signIn(page)
  await page.getByRole('button', { name: 'New' }).click()
  const hostile = '<img src=x onerror="window.__pwned=1">'
  await page.getByLabel(/^Name/).fill(hostile)
  await page.getByLabel(/^Sku/i).fill(`x-${Date.now()}`)
  await page.getByLabel(/^Quantity/).fill('7')
  await page.getByLabel(/^Reorder level/i).fill('2')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByRole('heading', { name: 'Product list' })).toBeVisible()
  await expect(page.getByRole('cell', { name: hostile })).toBeVisible()
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined()
  expect(await page.locator('main img').count()).toBe(0)
})

test('the servers validation errors are shown on the form', async ({ page }) => {
  await signIn(page)
  await page.getByRole('button', { name: 'New' }).click()
  await page.getByLabel(/^Name/).fill('Bad')
  await page.getByLabel(/^Sku/i).fill('bad')
  await page.getByLabel(/^Quantity/).fill('-5')
  await page.getByLabel(/^Reorder level/i).fill('2')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save' })).toBeVisible() // still on the form
})

test('the application is installable: its own manifest, worker scope and icons', async ({ page, context, browserName }) => {
  await page.goto(SHELL)
  const manifest = await (await page.request.get(`${SHELL}manifest.webmanifest`)).json()
  expect(manifest).toMatchObject({ name: 'Inventory', display: 'standalone', scope: './', start_url: './' })
  for (const icon of manifest.icons as { src: string }[]) {
    const r = await page.request.get(`${SHELL}${icon.src}`)
    expect(r.headers()['content-type']).toBe('image/png')
  }
  await page.evaluate(() => navigator.serviceWorker.ready)
  expect(await page.evaluate(async () => (await navigator.serviceWorker.getRegistration())?.scope)).toBe(SHELL)
  if (browserName === 'chromium') {
    const cdp = await context.newCDPSession(page)
    expect((await cdp.send('Page.getAppManifest')).errors).toEqual([])
  }
})

test('the worker caches the shell and nothing of the applications data or session', async ({ page }) => {
  await signIn(page)
  await page.evaluate(() => navigator.serviceWorker.ready)
  await expect.poll(() => page.evaluate(async () => (await caches.keys()).length)).toBeGreaterThan(0)
  const urls = await page.evaluate(async () => {
    const out: string[] = []
    for (const n of await caches.keys()) for (const r of await (await caches.open(n)).keys()) out.push(new URL(r.url).pathname)
    return out
  })
  expect(urls.length).toBeGreaterThan(0)
  for (const u of urls) expect(u).toMatch(/\/shell\/(app\.js|app\.css|manifest\.webmanifest|offline\.html|icon-\d+\.png)?$/)
})

test('offline, the shell still opens and says that data needs a connection', async ({ page, context, browserName }) => {
  test.skip(browserName === 'webkit', 'Playwright cannot emulate offline for WebKit')
  await page.goto(SHELL)
  await page.evaluate(() => navigator.serviceWorker.ready)
  if (!(await page.evaluate(() => !!navigator.serviceWorker.controller))) await page.reload()
  await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true)
  await context.setOffline(true)
  await page.reload()
  await expect(page.getByRole('status').filter({ hasText: 'You are offline' })).toBeVisible()
  await expect(page.locator('#title')).toHaveText('Inventory') // the shell itself came from the cache
  await context.setOffline(false)
})

test('signing out ends the session and clears the shell cache', async ({ page }) => {
  await signIn(page)
  await page.evaluate(() => navigator.serviceWorker.ready)
  await page.getByRole('button', { name: 'Sign out' }).click()
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible()
  await expect.poll(() => page.evaluate(async () => (await caches.keys()).length)).toBe(0)
  const me = await page.request.get(`${SERVER}/api/v1/apps/${APP}/auth/me`)
  expect(me.status()).toBe(401)
})

test('works on a phone: no horizontal page scrolling', async ({ browser }) => {
  const phone = await browser.newContext({ viewport: { width: 375, height: 667 }, isMobile: true, hasTouch: true })
  const page = await phone.newPage()
  await signIn(page)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1)
  await phone.close()
})
