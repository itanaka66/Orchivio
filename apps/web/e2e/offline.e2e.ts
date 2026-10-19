import { expect, test, type BrowserContext, type Page } from '@playwright/test'

const BASE = 'http://localhost:4173'
const PASS = 'a long passphrase'

// Playwright's offline emulation does not reach WebKit, so these run in Chromium and Firefox.
test.skip(({ browserName }) => browserName === 'webkit', 'Playwright cannot emulate offline for WebKit')

const control = async (path: string, method = 'GET') => (await fetch(`${BASE}/__control/${path}`, { method })).json() as Promise<Record<string, unknown>>
const serverBoard = async () => (await control('board')) as unknown as { stories: { title: string; status: string; version: number }[]; requirements: { title: string }[]; log: string[] }

test.beforeEach(async () => { await control('reset') })

async function openBoard(page: Page) {
  await page.goto('/')
  await page.getByLabel('Email').fill('a@example.com')
  await page.getByLabel('Password').fill(PASS)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByText('Ada')).toBeVisible()
  await page.getByLabel('Organization').selectOption({ label: 'Acme' })
  await page.getByLabel('Project').selectOption({ label: 'Inventory' })
  await page.getByRole('button', { name: 'Open the story board' }).click()
  await expect(page.getByText('Pay invoice', { exact: true })).toBeVisible()
}

const openPanel = (page: Page) => page.locator('details.offline-panel').evaluate((d) => { (d as HTMLDetailsElement).open = true })

async function turnOn(page: Page) {
  await openPanel(page)
  await page.getByLabel(/New passphrase/).fill(PASS)
  await page.getByLabel('Repeat the passphrase').fill(PASS)
  await page.getByRole('button', { name: 'Turn on offline work' }).click()
  await expect(page.getByText('Nothing is waiting to be sent.')).toBeVisible()
}

const goOffline = async (context: BrowserContext, page: Page) => { await context.setOffline(true); await expect(page.getByText(/Offline: changes to requirements and stories wait/)).toBeVisible({ timeout: 15_000 }) }

test('work done offline is kept on the device and sent once, when the connection returns', async ({ page, context }) => {
  await openBoard(page)
  // offline work is set up from the main page; the board overlay hides it, so set it up first
  await page.getByRole('button', { name: /Back|Close|Studio/ }).first().click().catch(() => undefined)
  await turnOn(page)
  await page.getByRole('button', { name: 'Open the story board' }).click()
  await expect(page.getByText('Pay invoice', { exact: true })).toBeVisible()

  await goOffline(context, page)
  await page.getByLabel('New story').fill('Written offline')
  await page.getByRole('button', { name: 'Add story' }).click()
  await expect(page.getByText('Saved on this device. It will be sent when you are online.')).toBeVisible()
  await expect(page.getByRole('list', { name: 'Stories waiting to be sent' }).getByText('Written offline')).toBeVisible()
  await page.locator('section.board select').first().selectOption('done') // an edit of an existing story
  expect((await serverBoard()).stories.map((s) => s.title)).toEqual(['Pay invoice']) // nothing reached the server

  await context.setOffline(false)
  await expect.poll(async () => (await serverBoard()).stories.map((s) => s.title), { timeout: 20_000 }).toEqual(['Pay invoice', 'Written offline'])
  const board = await serverBoard()
  expect(board.stories[0]).toMatchObject({ status: 'done', version: 2 })
  expect(board.log.filter((l) => l.startsWith('POST'))).toEqual(['POST stories Written offline']) // exactly once
})

test('someone else changing the same item is a conflict a person decides, not an overwrite', async ({ page, context }) => {
  await openBoard(page)
  await page.getByRole('button', { name: /Back|Close|Studio/ }).first().click().catch(() => undefined)
  await turnOn(page)
  await page.getByRole('button', { name: 'Open the story board' }).click()
  await expect(page.getByText('Pay invoice', { exact: true })).toBeVisible()

  await goOffline(context, page)
  await page.locator('section.board select').first().selectOption('done')
  await control('rename?title=Pay%20invoice%20(theirs)&status=in_progress', 'POST') // somebody else, meanwhile

  await context.setOffline(false)
  await expect(page.getByText(/could not be applied because someone else changed the item/)).toBeVisible({ timeout: 20_000 })
  expect((await serverBoard()).stories[0]).toMatchObject({ title: 'Pay invoice (theirs)', status: 'in_progress' }) // not overwritten
  await page.getByRole('button', { name: '← Back' }).click()
  await openPanel(page)
  await page.getByRole('button', { name: 'Keep mine' }).click()
  await expect.poll(async () => (await serverBoard()).stories[0]?.status, { timeout: 20_000 }).toBe('done')
  expect((await serverBoard()).stories[0]?.title).toBe('Pay invoice (theirs)') // only the field this person changed was applied
})

test('the queue survives closing the Studio and is sent after unlocking, never without the passphrase', async ({ page, context }) => {
  await openBoard(page)
  await page.getByRole('button', { name: /Back|Close|Studio/ }).first().click().catch(() => undefined)
  await turnOn(page)
  await page.getByRole('button', { name: 'Open the story board' }).click()
  await goOffline(context, page)
  await page.getByLabel('New story').fill('Survives a restart')
  await page.getByRole('button', { name: 'Add story' }).click()
  await expect(page.getByText('Saved on this device. It will be sent when you are online.')).toBeVisible()

  await page.getByRole('button', { name: '← Back' }).click()
  await openPanel(page)
  await page.getByRole('button', { name: 'Lock' }).click() // what closing the Studio does to the key: it is gone from memory
  await context.setOffline(false)
  await page.waitForTimeout(1500)
  expect((await serverBoard()).stories.map((s) => s.title)).toEqual(['Pay invoice']) // locked: nothing is sent
  const stored = await page.evaluate(() => new Promise<string>((resolve) => {
    const open = indexedDB.open('orchivio-offline')
    open.onsuccess = () => { const all = open.result.transaction('outbox').objectStore('outbox').getAll(); all.onsuccess = () => resolve(JSON.stringify(all.result)) }
  }))
  expect(stored).not.toContain('Survives a restart') // the content is encrypted at rest
  await page.getByLabel('Passphrase').fill(PASS)
  await page.getByRole('button', { name: 'Unlock' }).click()
  await expect.poll(async () => (await serverBoard()).stories.map((s) => s.title), { timeout: 20_000 }).toEqual(['Pay invoice', 'Survives a restart'])
})
