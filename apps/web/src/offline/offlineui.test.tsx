import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { ApiError, type ApiClient } from '../api/client'
import { Board } from '../board/Board'
import { FeedbackWidget } from '../board/FeedbackWidget'
import { openDb } from './idb'
import { OfflinePanel } from './OfflinePanel'
import { OfflineProvider, useOffline, type Offline } from './OfflineProvider'

let n = 0
const PASS = 'a long passphrase'
const USER = 'user-1'
const PROJECT = '11111111-1111-4111-8111-111111111111'

function setOnline(value: boolean) {
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(value)
  act(() => { window.dispatchEvent(new Event(value ? 'online' : 'offline')) })
}

const stubbed: string[] = []
function stubNav(name: string, value: unknown) { Object.defineProperty(navigator, name, { value, configurable: true }); stubbed.push(name) }
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); for (const n of stubbed.splice(0)) delete (navigator as unknown as Record<string, unknown>)[n] })

let captured: Offline | null = null
function Probe() { captured = useOffline(); return null }

function setup(ui: React.ReactNode, client: Partial<ApiClient> = {}, dbName = `ui-${n++}`) {
  captured = null
  const c = { request: vi.fn(async () => ({})), ...client } as unknown as ApiClient
  const utils = render(
    <OfflineProvider client={c} openDatabase={() => openDb(dbName)} kdfIterations={1000} syncEveryMs={0}>
      <Probe />
      {ui}
    </OfflineProvider>,
  )
  return { ...utils, client: c, dbName }
}

async function ready(): Promise<Offline> {
  await waitFor(() => expect(captured?.supported).toBe(true))
  act(() => captured!.bindUser(USER))
  return captured!
}

async function enableOffline() {
  const off = await ready()
  await act(async () => { await off.enable(PASS) })
  await waitFor(() => expect(captured!.unlocked).toBe(true))
}

const story = (title: string) => ({ kind: 'story' as const, path: `/projects/${PROJECT}/stories`, body: { title }, label: title, project: PROJECT })

describe('OfflineProvider.submit', () => {
  it('sends straight away when online and nothing is queued', async () => {
    setup(null)
    await enableOffline()
    const send = vi.fn(async () => 'created')
    let r: unknown
    await act(async () => { r = await captured!.submit(story('x'), send) })
    expect(r).toEqual({ queued: false, value: 'created' })
    expect(captured!.pending).toBe(0)
  })

  it('keeps the request when the server cannot be reached, and when the browser is offline', async () => {
    setup(null, { request: (async () => { throw new TypeError('Failed to fetch') }) as never })
    await enableOffline()
    let r: unknown
    await act(async () => { r = await captured!.submit(story('lost connection'), async () => { throw new TypeError('Failed to fetch') }) })
    expect(r).toMatchObject({ queued: true })
    setOnline(false)
    const send = vi.fn()
    await act(async () => { r = await captured!.submit(story('offline'), send) })
    expect(r).toMatchObject({ queued: true })
    expect(send).not.toHaveBeenCalled()
    await waitFor(() => expect(captured!.items.map((i) => i.label)).toEqual(['lost connection', 'offline']))
  })

  it('does not hide the server’s own answers behind the queue', async () => {
    setup(null)
    await enableOffline()
    await expect(captured!.submit(story('x'), async () => { throw new ApiError(422, 'invalid', 'title is too long') })).rejects.toThrow('title is too long')
    expect(captured!.pending).toBe(0)
  })

  it('explains why it cannot keep something offline: not set up, or locked', async () => {
    setup(null)
    await ready()
    setOnline(false)
    await waitFor(() => expect(captured!.online).toBe(false))
    await expect(captured!.submit(story('x'), vi.fn())).rejects.toThrow(/Turn on offline work/)
    await act(async () => { await captured!.enable(PASS) })
    act(() => captured!.lock())
    await expect(captured!.submit(story('x'), vi.fn())).rejects.toThrow(/Unlock offline work/)
  })
})

describe('OfflineProvider.sync', () => {
  it('sends what is waiting when the connection returns, with the idempotency key, and empties the queue', async () => {
    const request = vi.fn(async (..._a: unknown[]) => ({})) as never
    setup(null, { request })
    await enableOffline()
    setOnline(false)
    await act(async () => { await captured!.submit(story('later'), vi.fn()) })
    expect(request).not.toHaveBeenCalled()
    setOnline(true)
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1))
    expect(request).toHaveBeenCalledWith('POST', `/projects/${PROJECT}/stories`, { title: 'later' }, { 'Idempotency-Key': expect.stringMatching(/^q-/) })
    await waitFor(() => expect(captured!.pending).toBe(0))
    expect(captured!.lastSync).toMatchObject({ sent: 1, rejected: 0, left: 0 })
  })

  it('waits for the vault: nothing is sent while locked, and unlocking sends it', async () => {
    const request = vi.fn(async (..._a: unknown[]) => ({})) as never
    setup(null, { request })
    await enableOffline()
    setOnline(false)
    await act(async () => { await captured!.submit(story('x'), vi.fn()) })
    act(() => captured!.lock())
    setOnline(true)
    await new Promise((r) => setTimeout(r, 60))
    expect(request).not.toHaveBeenCalled()
    expect(captured!.pending).toBe(1)
    await act(async () => { expect(await captured!.unlock('wrong passphrase!!')).toBe(false) })
    expect(captured!.error).toMatch(/does not open/)
    await act(async () => { expect(await captured!.unlock(PASS)).toBe(true) })
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1))
  })

  it('survives a reload: the queue is still there, encrypted, in the next session', async () => {
    const first = setup(null)
    await enableOffline()
    setOnline(false)
    await act(async () => { await captured!.submit(story('persisted'), vi.fn()) })
    first.unmount()
    setup(null, {}, first.dbName)
    await ready()
    await waitFor(() => expect(captured!.enabled).toBe(true))
    expect(captured!.pending).toBe(1)
    expect(captured!.unlocked).toBe(false)
    expect(captured!.items).toEqual([]) // nothing readable until unlocked
    await act(async () => { await captured!.unlock(PASS) })
    await waitFor(() => expect(captured!.items.map((i) => i.label)).toEqual(['persisted']))
  })

  it('refuses to open another account’s offline data', async () => {
    const first = setup(null)
    await enableOffline()
    first.unmount()
    setup(null, {}, first.dbName)
    await waitFor(() => expect(captured?.supported).toBe(true))
    act(() => captured!.bindUser('someone-else'))
    await waitFor(() => expect(captured!.foreign).toBe(true))
    await act(async () => { expect(await captured!.unlock(PASS)).toBe(false) })
    expect(captured!.error).toMatch(/another account/)
    await act(async () => { await captured!.disable() })
    expect(captured!.foreign).toBe(false)
    expect(captured!.enabled).toBe(false)
  })
})

describe('OfflinePanel', () => {
  it('sets up offline work: short or different passphrases are refused', async () => {
    setup(<OfflinePanel />)
    await ready()
    await userEvent.click(await screen.findByText('Offline work'))
    await userEvent.type(screen.getByLabelText(/^New passphrase/), 'short')
    await userEvent.type(screen.getByLabelText('Repeat the passphrase'), 'different')
    await userEvent.click(screen.getByRole('button', { name: 'Turn on offline work' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('differ')
    await userEvent.clear(screen.getByLabelText('Repeat the passphrase'))
    await userEvent.type(screen.getByLabelText('Repeat the passphrase'), 'short')
    await userEvent.click(screen.getByRole('button', { name: 'Turn on offline work' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('at least 10')
    expect(captured!.enabled).toBe(false)
  })

  it('shows what waits, lets a person retry or discard a refused change, and deletes everything on request', async () => {
    const request = vi.fn(async (..._a: unknown[]) => { throw new ApiError(422, 'validation_failed', 'title is too long') }) as never
    setup(<OfflinePanel />, { request })
    await ready()
    await userEvent.click(await screen.findByText('Offline work'))
    await userEvent.type(screen.getByLabelText(/^New passphrase/), PASS)
    await userEvent.type(screen.getByLabelText('Repeat the passphrase'), PASS)
    await userEvent.click(screen.getByRole('button', { name: 'Turn on offline work' }))
    await waitFor(() => expect(captured!.unlocked).toBe(true))
    setOnline(false)
    await waitFor(() => expect(captured!.online).toBe(false))
    await act(async () => { await captured!.submit(story('A story the server will refuse'), vi.fn()) })
    const list = await screen.findByRole('list', { name: 'Changes waiting to be sent' })
    expect(within(list).getByText('A story the server will refuse')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send now' })).toBeDisabled() // offline
    setOnline(true)
    expect(await screen.findByText(/The server refused this change: title is too long/)).toBeInTheDocument()
    expect(request).toHaveBeenCalledTimes(1) // refused once, not retried by itself
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
    await userEvent.click(await screen.findByRole('button', { name: 'Discard' }))
    await waitFor(() => expect(captured!.pending).toBe(0))
    await userEvent.click(screen.getByRole('button', { name: 'Turn off and delete' }))
    await userEvent.click(screen.getByRole('button', { name: /Delete 0 waiting changes and all saved copies/ }))
    await waitFor(() => expect(captured!.enabled).toBe(false))
  })

  it('renders nothing without a provider', () => {
    const { container } = render(<OfflinePanel />)
    expect(container).toBeEmptyDOMElement()
  })
})

// --- the board ---------------------------------------------------------------------------------------------------

function boardClient(over: Record<string, unknown> = {}) {
  return {
    request: vi.fn(async () => ({})),
    stories: vi.fn(async () => ({ items: [{ id: '33333333-3333-4333-8333-333333333333', title: 'Existing story', body: '', acceptance: '', status: 'backlog', source: 'manual', position: 1, requirement_id: null }] })),
    requirements: vi.fn(async () => ({ items: [{ id: 'r1', title: 'Existing requirement', body: '', status: 'draft', source: 'manual' }] })),
    createStory: vi.fn(async (_p: string, title: string) => ({ id: 'new', title })),
    createRequirement: vi.fn(async () => ({ id: 'newr' })),
    updateStory: vi.fn(async () => ({})),
    ...over,
  } as unknown as ApiClient
}

describe('Board offline', () => {
  it('creates online as before, without touching the queue', async () => {
    const client = boardClient()
    setup(<Board client={client} projectId={PROJECT} elements={[]} canEdit />, client)
    await enableOffline()
    await userEvent.type(await screen.findByLabelText('New story'), 'Online story')
    await userEvent.click(screen.getByRole('button', { name: 'Add story' }))
    await waitFor(() => expect(client.createStory).toHaveBeenCalledWith(PROJECT, 'Online story'))
    expect(captured!.pending).toBe(0)
  })

  it('keeps new and changed stories on the device while offline', async () => {
    const client = boardClient({ stories: vi.fn(async () => { throw new TypeError('offline') }) })
    // a saved copy exists from an earlier visit
    const first = setup(<Board client={boardClient()} projectId={PROJECT} elements={[]} canEdit />)
    await enableOffline()
    await screen.findByText('Existing story')
    await waitFor(async () => expect(await captured!.loadSaved(`board:${PROJECT}`)).not.toBeNull())
    first.unmount()
    setOnline(false)
    setup(<Board client={client} projectId={PROJECT} elements={[]} canEdit />, {}, first.dbName)
    await ready()
    await act(async () => { await captured!.unlock(PASS) })
    expect(await screen.findByText(/You are looking at the copy saved/)).toBeInTheDocument()
    expect(screen.getByText('Existing story')).toBeInTheDocument()
    // changing an existing story offline is kept on the device and shown as done
    await userEvent.selectOptions(screen.getAllByRole('combobox')[0]!, 'done')
    await waitFor(() => expect(captured!.items.map((i) => i.kind)).toEqual(['story_edit']))
    expect(captured!.items[0]).toMatchObject({ body: { status: 'done', position: 2 }, base: { status: 'backlog', position: 1 } })
    expect(client.updateStory).not.toHaveBeenCalled()
    await userEvent.type(screen.getByLabelText('New story'), 'Made offline')
    await userEvent.click(screen.getByRole('button', { name: 'Add story' }))
    expect(await screen.findByText('Saved on this device. It will be sent when you are online.')).toBeInTheDocument()
    const waiting = await screen.findByRole('list', { name: 'Stories waiting to be sent' })
    expect(within(waiting).getByText('Made offline')).toBeInTheDocument()
    expect(client.createStory).not.toHaveBeenCalled()
  })

  it('tells people what needs a connection', async () => {
    setOnline(false)
    const client = boardClient({ stories: vi.fn(async () => ({ items: [] })), requirements: vi.fn(async () => ({ items: [] })) })
    setup(<Board client={client} projectId={PROJECT} applicationId="a1" elements={[]} canEdit />, client)
    await ready()
    await userEvent.click(await screen.findByRole('tab', { name: 'Feedback' }))
    expect(screen.getByText('Reviewing feedback needs a connection.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Traceability' }))
    expect(screen.getByText('Traceability needs a connection.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Requirements' }))
    expect(screen.queryByLabelText('Document')).toBeNull() // AI extraction needs the server
    expect(screen.getByLabelText('Title')).toBeInTheDocument() // but a requirement can be added
  })
})

describe('FeedbackWidget offline', () => {
  it('keeps feedback on the device and says so', async () => {
    const submitFeedback = vi.fn()
    setup(<FeedbackWidget client={{ submitFeedback } as never} applicationId="22222222-2222-4222-8222-222222222222" page="studio" />)
    await enableOffline()
    setOnline(false)
    await userEvent.click(screen.getByRole('button', { name: 'Feedback' }))
    await userEvent.type(screen.getByLabelText('What should change?'), 'The list is hard to read')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByText('Saved on this device. It will be sent when you are online.')).toBeInTheDocument()
    expect(submitFeedback).not.toHaveBeenCalled()
    await waitFor(() => expect(captured!.items[0]).toMatchObject({ kind: 'feedback', label: 'The list is hard to read' }))
  })
})

// --- the whole app ---------------------------------------------------------------------------------------------------

describe('App offline start', () => {
  it('opens the saved data without a connection, once unlocked, and offers sign-in for later', async () => {
    const dbName = `app-${n++}`
    // a previous session: offline work set up, a project remembered
    const seed = render(<OfflineProvider client={{} as ApiClient} openDatabase={() => openDb(dbName)} kdfIterations={1000} syncEveryMs={0}><Probe /></OfflineProvider>)
    await waitFor(() => expect(captured?.supported).toBe(true))
    act(() => captured!.bindUser('u1'))
    await act(async () => { await captured!.enable(PASS) })
    await act(async () => {
      await captured!.snapshot('index', [{ projectId: PROJECT, projectName: 'Inventory', organization: 'Acme', canEdit: true, applications: [] }])
      await captured!.snapshot('profile', { displayName: 'Ada' })
      await captured!.snapshot(`board:${PROJECT}`, { stories: [{ id: 's1', title: 'Saved story', body: '', acceptance: '', status: 'backlog', source: 'manual', position: 1, requirement_id: null }], requirements: [] })
    })
    seed.unmount()

    setOnline(false)
    const client = { signedIn: true, me: vi.fn(async () => { throw new TypeError('Failed to fetch') }), request: vi.fn(), stories: vi.fn(async () => { throw new TypeError('x') }), requirements: vi.fn(async () => { throw new TypeError('x') }) } as unknown as ApiClient
    render(<App client={client} offline={{ openDatabase: () => openDb(dbName), kdfIterations: 1000, syncEveryMs: 0 }} />)
    expect(await screen.findByText(/cannot reach the server/)).toBeInTheDocument()
    await userEvent.click((await screen.findAllByText(/Offline work/))[0]!)
    await userEvent.type(await screen.findByLabelText('Passphrase'), PASS)
    await userEvent.click(screen.getByRole('button', { name: 'Unlock' }))
    expect(await screen.findByText('Saved for Ada')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Open story board' }))
    expect(await screen.findByText('Saved story')).toBeInTheDocument()
    expect(screen.getByText(/You are looking at the copy saved/)).toBeInTheDocument()
  })

  it('does not offer saved data to a browser that never set up offline work', async () => {
    const client = { signedIn: true, me: vi.fn(async () => { throw new TypeError('Failed to fetch') }), request: vi.fn() } as unknown as ApiClient
    render(<App client={client} offline={{ openDatabase: () => openDb(`app-${n++}`), kdfIterations: 1000, syncEveryMs: 0 }} />)
    expect(await screen.findByText(/Offline work was never set up on this device/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Saved projects')).toBeNull()
  })
})

describe('conflicts in the panel', () => {
  it('shows both versions and applies the person’s choice', async () => {
    const id = '33333333-3333-4333-8333-333333333333'
    const path = `/projects/${PROJECT}/stories/${id}`
    const request = vi.fn(async (..._a: unknown[]) => { throw new ApiError(409, 'version_conflict', 'changed', { current: { title: 'Theirs', status: 'in_progress', version: 4 } }) }) as never
    setup(<OfflinePanel />, { request })
    await enableOffline()
    setOnline(false)
    await waitFor(() => expect(captured!.online).toBe(false))
    await act(async () => { await captured!.submit({ kind: 'story_edit', path, body: { status: 'done' }, base: { status: 'ready' }, ifMatch: 1, label: 'Pay invoice', project: PROJECT }, vi.fn()) })
    setOnline(true)
    expect(await screen.findByText('Someone else changed this item while you were offline.')).toBeInTheDocument()
    const row = screen.getByRole('row', { name: /status/ })
    expect(within(row).getByText('done')).toBeInTheDocument()
    expect(within(row).getByText('in_progress')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Keep theirs' }))
    await waitFor(() => expect(captured!.pending).toBe(0))
  })
})

describe('storage and wake-ups', () => {
  it('refuses to keep a change when the browser is nearly out of space, and says so', async () => {
    stubNav('storage', { estimate: async () => ({ usage: 95, quota: 100 }), persisted: async () => false, persist: async () => true })
    setup(null)
    await enableOffline()
    setOnline(false)
    await waitFor(() => expect(captured!.online).toBe(false))
    await expect(captured!.submit(story('x'), vi.fn())).rejects.toThrow(/almost out of storage/)
    expect(captured!.pending).toBe(0)
  })

  it('sends what is waiting when the service worker says the connection is back', async () => {
    const listeners: ((e: MessageEvent) => void)[] = []
    stubNav('serviceWorker', { addEventListener: (_: string, l: never) => listeners.push(l), removeEventListener: () => undefined, ready: new Promise(() => undefined) })
    const request = vi.fn(async (..._a: unknown[]) => ({})) as never
    setup(null, { request })
    await enableOffline()
    setOnline(false)
    await waitFor(() => expect(captured!.online).toBe(false))
    await act(async () => { await captured!.submit(story('later'), vi.fn()) })
    // the browser still says offline, but the worker knows better: send now
    for (const l of listeners) act(() => l({ data: { type: 'OUTBOX_SYNC' } } as MessageEvent))
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1))
  })

  it('shows storage use and lets a person ask to keep it or clear saved copies', async () => {
    const persist = vi.fn(async () => true)
    stubNav('storage', { estimate: async () => ({ usage: 2 * 1_048_576, quota: 100 * 1_048_576 }), persisted: async () => false, persist })
    setup(<OfflinePanel />)
    await enableOffline()
    await userEvent.click(await screen.findByText(/Offline work/))
    expect(await screen.findByLabelText('Storage')).toHaveTextContent('2.0 MB of 100 MB')
    await userEvent.click(screen.getByRole('button', { name: 'Ask the browser to keep it' }))
    expect(persist).toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Delete saved copies' }))
  })
})
