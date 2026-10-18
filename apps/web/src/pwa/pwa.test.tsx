import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { stampServiceWorker } from '../../pwa-plugin'
import { PwaBar } from './PwaBar'
import { applyUpdate, clearShellCaches, registerServiceWorker, resetPwaState } from './register'

const PUBLIC = join(__dirname, '..', '..', 'public')
const read = (name: string) => readFileSync(join(PUBLIC, name), 'utf8')

// --- the service worker ------------------------------------------------------------------------------

type Listener = (event: Record<string, unknown>) => void

function worker(network: (url: string, request: { method: string; mode?: string }) => Promise<Response> | Response) {
  const listeners: Record<string, Listener> = {}
  const stores = new Map<string, Map<string, Response>>()
  const fetched: string[] = []
  const key = (r: string | { url: string }) => new URL(typeof r === 'string' ? r : r.url, 'https://studio.example').pathname + new URL(typeof r === 'string' ? r : r.url, 'https://studio.example').search
  const fakeFetch = async (r: string | { url: string; method?: string; mode?: string }) => {
    const url = typeof r === 'string' ? r : r.url
    fetched.push(key(url))
    return network(url, typeof r === 'string' ? { method: 'GET' } : { method: r.method ?? 'GET', mode: r.mode })
  }
  const caches = {
    async open(name: string) {
      if (!stores.has(name)) stores.set(name, new Map())
      const m = stores.get(name)!
      return {
        addAll: async (urls: string[]) => { for (const u of urls) m.set(key(u), (await fakeFetch(u)).clone()) },
        put: async (r: string | { url: string }, res: Response) => { m.set(key(r), res) },
        match: async (r: string | { url: string }) => m.get(key(r))?.clone(),
      }
    },
    async keys() { return [...stores.keys()] },
    async delete(name: string) { return stores.delete(name) },
    async match(r: string | { url: string }) { for (const m of stores.values()) { const hit = m.get(key(r)); if (hit) return hit.clone() } return undefined },
  }
  const self = {
    location: { origin: 'https://studio.example' },
    addEventListener: (type: string, fn: Listener) => { listeners[type] = fn },
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn(async () => undefined) },
  }
  new Function('self', 'caches', 'fetch', 'URL', 'Response', read('sw.js'))(self, caches, fakeFetch, URL, Response)

  const lifecycle = async (type: string, extra: Record<string, unknown> = {}) => {
    const waits: Promise<unknown>[] = []
    listeners[type]!({ waitUntil: (p: Promise<unknown>) => waits.push(p), ...extra })
    await Promise.all(waits)
  }
  const request = async (url: string, init: { method?: string; mode?: string } = {}) => {
    let answer: Promise<Response> | undefined
    listeners.fetch!({ request: { url: `https://studio.example${url.startsWith('http') ? '' : ''}${url}`, method: init.method ?? 'GET', mode: init.mode ?? 'cors' }, respondWith: (p: Promise<Response>) => { answer = p } })
    return answer ? await answer : undefined
  }
  const absolute = async (url: string, init: { method?: string; mode?: string } = {}) => {
    let answer: Promise<Response> | undefined
    listeners.fetch!({ request: { url, method: init.method ?? 'GET', mode: init.mode ?? 'cors' }, respondWith: (p: Promise<Response>) => { answer = p } })
    return answer ? await answer : undefined
  }
  const stored = () => [...stores.entries()].flatMap(([name, m]) => [...m.keys()].map((k) => `${name}:${k}`))
  return { lifecycle, request, absolute, self, stores, fetched, listeners, stored, caches }
}

/** A same-origin response as a browser delivers it (type 'basic'; the Response constructor alone gives 'default'). */
const basic = (res: Response) => Object.defineProperty(res, 'type', { value: 'basic' }) as Response
const ok = (body = 'x', headers: Record<string, string> = {}) => basic(new Response(body, { status: 200, headers }))

describe('service worker', () => {
  it('precaches the shell on install and removes older shells on activate, leaving other caches alone', async () => {
    const w = worker(() => ok('shell'))
    await w.lifecycle('install')
    expect(w.stored().map((s) => s.split(':')[1]).sort()).toEqual(['/', '/icons/icon-192.png', '/icons/icon-512.png', '/manifest.webmanifest', '/offline.html'])
    await w.caches.open('orchivio-shell-old')
    await w.caches.open('someone-elses-cache')
    await w.lifecycle('activate')
    expect([...w.stores.keys()].sort()).toEqual(['orchivio-shell-__BUILD_ID__', 'someone-elses-cache'])
    expect(w.self.clients.claim).toHaveBeenCalled()
  })

  it('never touches the API, health checks, other origins or non-GET requests', async () => {
    const w = worker(() => ok('api data'))
    await w.lifecycle('install')
    const before = w.fetched.length
    for (const url of ['/api/v1/me', '/api/v1/applications/a1/preview/data/core.Product', '/healthz', '/readyz', '/metrics']) {
      expect(await w.request(url, { mode: 'cors' }), url).toBeUndefined()
    }
    expect(await w.request('/assets/app.js', { method: 'POST' })).toBeUndefined()
    expect(await w.absolute('https://evil.example/assets/app.js')).toBeUndefined()
    expect(await w.request('/api/v1/me', { mode: 'navigate' })).toBeUndefined() // even a navigation to an API URL
    expect(w.fetched.length).toBe(before) // the worker did not even fetch on their behalf
    expect(w.stored().some((s) => s.includes('/api/'))).toBe(false)
  })

  it('opens pages from the network first and falls back to the cached shell, then the offline page', async () => {
    let online = true
    let release = 1
    const w = worker((url) => { if (!online) throw new TypeError('offline'); return ok(url.endsWith('/offline.html') ? 'offline page' : `shell v${release}`) })
    await w.lifecycle('install')
    release = 2 // a new release was deployed after the shell was precached
    expect(await (await w.request('/', { mode: 'navigate' }))!.text()).toBe('shell v2') // the network wins, so the update shows
    online = false
    expect(await (await w.request('/', { mode: 'navigate' }))!.text()).toBe('shell v2') // and is what the cache now holds
    w.stores.get('orchivio-shell-__BUILD_ID__')!.delete('/')
    expect(await (await w.request('/', { mode: 'navigate' }))!.text()).toBe('offline page')
  })

  it('serves hashed assets and icons cache-first', async () => {
    const w = worker(() => ok('bytes'))
    await w.lifecycle('install')
    const n = w.fetched.length
    expect(await (await w.request('/assets/index-abc123.js'))!.text()).toBe('bytes')
    expect(w.fetched.length).toBe(n + 1)
    await w.request('/assets/index-abc123.js')
    expect(w.fetched.length).toBe(n + 1) // second time from the cache
    await w.request('/icons/icon-192.png')
    expect(w.fetched.length).toBe(n + 1) // precached
  })

  it('does not cache failures, no-store responses or other paths', async () => {
    const w = worker((url) => url.includes('missing') ? basic(new Response('no', { status: 404 })) : url.includes('private') ? ok('p', { 'Cache-Control': 'no-store' }) : ok('x'))
    await w.lifecycle('install')
    await w.request('/assets/missing.js')
    await w.request('/assets/private.js')
    expect(w.stored().some((s) => s.includes('missing') || s.includes('private'))).toBe(false)
    await w.request('/assets/fine.js')
    expect(w.stored().some((s) => s.includes('/assets/fine.js'))).toBe(true) // control: a good asset is cached
    expect(await w.request('/some/other/path.json')).toBeUndefined() // not part of the shell: left to the browser
  })

  it('takes over only when asked and can clear its caches', async () => {
    const w = worker(() => ok())
    await w.lifecycle('install')
    expect(w.self.skipWaiting).not.toHaveBeenCalled() // a waiting worker does not interrupt the page
    await w.lifecycle('message', { data: { type: 'SKIP_WAITING' } })
    expect(w.self.skipWaiting).toHaveBeenCalledOnce()
    await w.lifecycle('message', { data: { type: 'CLEAR' } })
    expect(w.stored()).toEqual([])
  })

  it('names the API paths it must never handle', () => {
    const text = read('sw.js')
    expect(text).toMatch(/NEVER = \[[^\]]*'\/api\/'/)
    expect(text).not.toMatch(/caches\.open\([^)]*\)\)\.put\(request, fresh\.clone\(\)\)[^]*\/api\//)
  })
})

// --- registration ------------------------------------------------------------------------------------

function fakeContainer(controller: object | null = {}) {
  const reg = new EventTarget() as EventTarget & { installing: unknown; waiting: unknown }
  reg.installing = null
  reg.waiting = null
  const events = new EventTarget()
  const container = {
    controller, register: vi.fn(async () => reg),
    addEventListener: (t: string, fn: EventListener) => events.addEventListener(t, fn),
  }
  const newWorker = () => Object.assign(new EventTarget(), { state: 'installing', postMessage: vi.fn() })
  return { container: container as never, reg, events, newWorker }
}

describe('registration', () => {
  beforeEach(() => resetPwaState())

  it('does nothing when disabled or unsupported', async () => {
    expect(await registerServiceWorker({ enabled: false, container: fakeContainer().container })).toBe(false)
    expect(await registerServiceWorker({ enabled: true, container: undefined })).toBe(false)
  })

  it('registers /sw.js with the whole origin as scope and no HTTP caching of the worker file', async () => {
    const f = fakeContainer()
    expect(await registerServiceWorker({ enabled: true, container: f.container })).toBe(true)
    expect((f.container as unknown as { register: ReturnType<typeof vi.fn> }).register).toHaveBeenCalledWith('/sw.js', { scope: '/', updateViaCache: 'none' })
  })

  it('reports an update only when a worker installs while another already controls the page', async () => {
    const seen: boolean[] = []
    const { onPwaState } = await import('./register')
    const stop = onPwaState((s) => seen.push(s.update))
    const first = fakeContainer(null) // first ever install: no controller, so no update
    await registerServiceWorker({ enabled: true, container: first.container })
    const w1 = first.newWorker()
    first.reg.installing = w1
    first.reg.dispatchEvent(new Event('updatefound'))
    w1.state = 'installed'
    w1.dispatchEvent(new Event('statechange'))
    expect(seen.at(-1)).toBe(false)

    const upd = fakeContainer({})
    await registerServiceWorker({ enabled: true, container: upd.container })
    const w2 = upd.newWorker()
    upd.reg.installing = w2
    upd.reg.dispatchEvent(new Event('updatefound'))
    w2.state = 'installed'
    w2.dispatchEvent(new Event('statechange'))
    expect(seen.at(-1)).toBe(true)
    applyUpdate()
    expect(w2.postMessage).toHaveBeenCalledWith({ type: 'SKIP_WAITING' })
    stop()
  })

  it('reloads once after the new worker took over', async () => {
    const reload = vi.fn()
    vi.stubGlobal('location', { ...window.location, reload })
    const f = fakeContainer({})
    await registerServiceWorker({ enabled: true, container: f.container })
    f.events.dispatchEvent(new Event('controllerchange'))
    expect(reload).not.toHaveBeenCalled() // nothing was waiting: not our update
    const w = f.newWorker()
    f.reg.installing = w
    f.reg.dispatchEvent(new Event('updatefound'))
    w.state = 'installed'
    w.dispatchEvent(new Event('statechange'))
    f.events.dispatchEvent(new Event('controllerchange'))
    f.events.dispatchEvent(new Event('controllerchange'))
    expect(reload).toHaveBeenCalledTimes(1)
    vi.unstubAllGlobals()
  })

  it('asks the controller to clear its caches on sign-out', () => {
    const postMessage = vi.fn()
    clearShellCaches({ controller: { postMessage } as never })
    expect(postMessage).toHaveBeenCalledWith({ type: 'CLEAR' })
    expect(() => clearShellCaches(undefined)).not.toThrow()
  })
})

// --- the bar -----------------------------------------------------------------------------------------

describe('PwaBar', () => {
  afterEach(() => { resetPwaState(); vi.restoreAllMocks() })

  it('renders nothing when online with nothing to offer', () => {
    const { container } = render(<PwaBar />)
    expect(container).toBeEmptyDOMElement()
  })

  it('says so when the connection is lost and clears when it is back', () => {
    render(<PwaBar />)
    act(() => { window.dispatchEvent(new Event('offline')) })
    expect(screen.getByRole('status')).toHaveTextContent('You are offline')
    act(() => { window.dispatchEvent(new Event('online')) })
    expect(screen.queryByText(/You are offline/)).toBeNull()
  })

  it('offers installation when the browser allows it, once', async () => {
    const prompt = vi.fn(async () => undefined)
    render(<PwaBar />)
    const event = Object.assign(new Event('beforeinstallprompt', { cancelable: true }), { prompt, userChoice: Promise.resolve({ outcome: 'accepted' as const }) })
    act(() => { window.dispatchEvent(event) })
    expect(event.defaultPrevented).toBe(true) // we show our own button instead of the browser's mini-bar
    await userEvent.click(screen.getByRole('button', { name: 'Install Studio as an app' }))
    expect(prompt).toHaveBeenCalledOnce()
    expect(screen.queryByRole('button', { name: /Install Studio/ })).toBeNull()
  })

  it('offers the update that is waiting and applies it on request', async () => {
    const f = fakeContainer({})
    await registerServiceWorker({ enabled: true, container: f.container })
    render(<PwaBar />)
    const w = f.newWorker()
    f.reg.installing = w
    act(() => {
      f.reg.dispatchEvent(new Event('updatefound'))
      w.state = 'installed'
      w.dispatchEvent(new Event('statechange'))
    })
    await userEvent.click(await screen.findByRole('button', { name: 'Reload to update' }))
    expect(w.postMessage).toHaveBeenCalledWith({ type: 'SKIP_WAITING' })
  })
})

// --- manifest, icons and build -------------------------------------------------------------------------

function pngSize(bytes: Buffer): [number, number] {
  expect(bytes.subarray(0, 8).toString('hex')).toBe('89504e470d0a1a0a')
  return [bytes.readUInt32BE(16), bytes.readUInt32BE(20)]
}

describe('manifest and assets', () => {
  const manifest = JSON.parse(read('manifest.webmanifest'))

  it('has what browsers need to offer installation', () => {
    expect(manifest).toMatchObject({ name: 'Orchivio Studio', short_name: 'Orchivio', display: 'standalone', start_url: '/', scope: '/' })
    expect(manifest.theme_color).toMatch(/^#[0-9a-f]{6}$/i)
    expect(manifest.background_color).toMatch(/^#[0-9a-f]{6}$/i)
    expect(manifest.short_name.length).toBeLessThanOrEqual(12)
  })

  it('lists icons that exist with the sizes they claim, including a maskable one', () => {
    const purposes = new Set<string>()
    for (const icon of manifest.icons as { src: string; sizes: string; type: string; purpose: string }[]) {
      const file = join(PUBLIC, icon.src)
      expect(existsSync(file), icon.src).toBe(true)
      const [w, h] = icon.sizes.split('x').map(Number)
      expect(pngSize(readFileSync(file))).toEqual([w, h])
      expect(icon.type).toBe('image/png')
      purposes.add(icon.purpose)
    }
    expect(purposes).toEqual(new Set(['any', 'maskable']))
    expect((manifest.icons as { sizes: string }[]).some((i) => i.sizes === '192x192') && (manifest.icons as { sizes: string }[]).some((i) => i.sizes === '512x512')).toBe(true)
  })

  it('has everything the worker precaches', () => {
    const precache = /PRECACHE = \[([^\]]+)\]/.exec(read('sw.js'))![1]!.match(/'([^']+)'/g)!.map((s) => s.slice(1, -1))
    for (const p of precache) if (p !== '/') expect(existsSync(join(PUBLIC, p)), p).toBe(true)
  })

  it('links the manifest and icons from the page', () => {
    const html = readFileSync(join(PUBLIC, '..', 'index.html'), 'utf8')
    expect(html).toContain('rel="manifest" href="/manifest.webmanifest"')
    expect(html).toContain('name="theme-color"')
    expect(html).toContain('viewport-fit=cover')
    expect(html).toContain('rel="apple-touch-icon"')
  })

  it('has an offline page that needs nothing external', () => {
    const html = read('offline.html')
    expect(html).toContain('You are offline')
    expect(html).not.toMatch(/https?:\/\//)
  })
})

describe('build stamping', () => {
  it('puts a content-derived id into the worker, and a new release gets a new id', () => {
    const dir = mkdtempSync(join(tmpdir(), 'pwa-'))
    writeFileSync(join(dir, 'sw.js'), "const BUILD = '__BUILD_ID__'; const X = '__BUILD_ID__'")
    writeFileSync(join(dir, 'index.html'), '<script src="/assets/a-111.js"></script>')
    const id = stampServiceWorker(dir)!
    expect(id).toMatch(/^[0-9a-f]{12}$/)
    expect(readFileSync(join(dir, 'sw.js'), 'utf8')).toBe(`const BUILD = '${id}'; const X = '${id}'`)

    const dir2 = mkdtempSync(join(tmpdir(), 'pwa-'))
    writeFileSync(join(dir2, 'sw.js'), "const BUILD = '__BUILD_ID__'")
    writeFileSync(join(dir2, 'index.html'), '<script src="/assets/a-222.js"></script>')
    expect(stampServiceWorker(dir2)).not.toBe(id)
  })

  it('does nothing without a build and refuses a worker without the placeholder', () => {
    expect(stampServiceWorker(mkdtempSync(join(tmpdir(), 'pwa-')))).toBeNull()
    const dir = mkdtempSync(join(tmpdir(), 'pwa-'))
    writeFileSync(join(dir, 'sw.js'), 'const BUILD = "fixed"')
    writeFileSync(join(dir, 'index.html'), '<p>')
    expect(() => stampServiceWorker(dir)).toThrow(/placeholder/)
  })
})
