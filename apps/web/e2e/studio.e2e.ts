import { expect, test, type Page } from '@playwright/test'

// Credentials seeded by e2e/studio_api.py
const EMAIL = 'owner@example.com'
const PASSWORD = 'correct horse battery'

async function signIn(page: Page) {
  await page.goto('/')
  await page.getByLabel('Email').fill(EMAIL)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByText('Signed in as')).toBeVisible({ timeout: 10_000 })
}

/** Sign in and drill down to the seeded Inventory application. */
async function navigateToApp(page: Page) {
  await signIn(page)
  await expect(page.getByLabel('Organization')).toBeEnabled({ timeout: 10_000 })
  await page.getByLabel('Organization').selectOption({ label: 'Acme' })
  await expect(page.getByLabel('Project')).toBeEnabled({ timeout: 10_000 })
  await page.getByLabel('Project').selectOption({ label: 'Inventory' })
  await expect(page.getByLabel('Application')).toBeEnabled({ timeout: 10_000 })
  await page.getByLabel('Application').selectOption({ label: 'Inventory' })
  await expect(page.getByRole('button', { name: 'Review proposals' })).toBeVisible({ timeout: 10_000 })
}

test('sign in shows the studio; sign out returns to the login form', async ({ page }) => {
  await signIn(page)
  await expect(page.getByText('Owner')).toBeVisible()
  await page.getByRole('button', { name: 'Sign out' }).click()
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
})

test('wrong password shows an error and the sign-in form stays open', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Email').fill(EMAIL)
  await page.getByLabel('Password').fill('not the right password')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
})

test('navigate org → project → application and see action buttons', async ({ page }) => {
  await navigateToApp(page)
  await expect(page.getByRole('button', { name: 'Open the preview' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Review proposals' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Browse model' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open process designer' })).toBeVisible()
})

test('model explorer opens and shows the seeded entities', async ({ page }) => {
  await navigateToApp(page)
  await page.getByRole('button', { name: 'Browse model' }).click()
  await expect(page.getByText('Model explorer')).toBeVisible()
  // Tab buttons rendered by ModelExplorer
  await expect(page.getByRole('button', { name: 'Entities' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Pages' })).toBeVisible()
  // The sample inventory model contains a Product entity
  await expect(page.getByRole('heading', { name: 'Product' })).toBeVisible()
  await page.getByRole('button', { name: '← Back' }).click()
  await expect(page.getByText('Signed in as')).toBeVisible()
})

test('review proposals screen shows the seeded pending proposal', async ({ page }) => {
  await navigateToApp(page)
  await page.getByRole('button', { name: 'Review proposals' }).click()
  await expect(page.getByText('Proposals')).toBeVisible()
  // studio_api.py seeds one A2 proposal with 1 change ("Add note field")
  await expect(page.getByText(/A2/)).toBeVisible({ timeout: 10_000 })
  await expect(page.getByText('1 change')).toBeVisible()
  await expect(page.getByText('Select a proposal to review it.')).toBeVisible()
  await page.getByRole('button', { name: '← Back' }).click()
  await expect(page.getByText('Signed in as')).toBeVisible()
})

test('page designer opens and lists pages from the seeded model', async ({ page }) => {
  await navigateToApp(page)
  await page.getByRole('button', { name: 'Open the page designer' }).click()
  await expect(page.getByText('Page designer')).toBeVisible()
  // paged() adds product-list, product-create, product-edit, product-detail, audit-list
  await expect(page.getByText('product-list')).toBeVisible({ timeout: 10_000 })
  await page.getByRole('button', { name: '← Back' }).click()
  await expect(page.getByText('Signed in as')).toBeVisible()
})

test('preview opens for the owner', async ({ page }) => {
  await navigateToApp(page)
  await page.getByRole('button', { name: 'Open the preview' }).click()
  await expect(page.getByText('Preview')).toBeVisible()
  await page.getByRole('button', { name: '← Back' }).click()
  await expect(page.getByText('Signed in as')).toBeVisible()
})

test('the layout has no horizontal overflow on a phone screen', async ({ browser }) => {
  const phone = await browser.newContext({ viewport: { width: 375, height: 667 }, isMobile: true, hasTouch: true })
  const page = await phone.newPage()
  await page.goto('/')
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  await phone.close()
})
