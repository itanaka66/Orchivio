import { describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError } from './client'

const json = (status: number, body?: unknown) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

function memoryStorage() {
  const m = new Map<string, string>()
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) }
}

describe('ApiClient', () => {
  it('sends the CSRF token on unsafe requests only, and keeps it across reloads', async () => {
    const calls: { url: string; init: RequestInit }[] = []
    const fetcher = vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return url.endsWith('/auth/login') ? json(200, { user: { id: '1', email: 'a@b.c', display_name: 'A' }, csrf_token: 'tok' }) : json(200, { items: [] })
    }) as unknown as typeof fetch
    const store = memoryStorage()
    const c = new ApiClient('/api/v1', fetcher, store)
    expect(c.signedIn).toBe(false)
    await c.login('a@b.c', 'pw')
    await c.organizations()
    await c.createProposal('app1', { x: 1 }, 'title')
    const headers = (i: number) => calls[i]?.init.headers as Record<string, string>
    expect(headers(0)['X-CSRF-Token']).toBeUndefined() // login itself
    expect(headers(1)['X-CSRF-Token']).toBeUndefined() // GET
    expect(headers(2)['X-CSRF-Token']).toBe('tok')
    expect(calls[2]?.url).toBe('/api/v1/applications/app1/proposals')
    expect(calls[2]?.init.credentials).toBe('same-origin')
    expect(JSON.parse(String(calls[2]?.init.body))).toEqual({ model: { x: 1 }, title: 'title' })
    expect(new ApiClient('/api/v1', fetcher, store).signedIn).toBe(true) // survives a page reload
  })

  it('maps API errors and forgets the session on 401', async () => {
    const store = memoryStorage()
    store.setItem('orchivio.csrf', 'tok')
    const fetcher = (async () => json(401, { error: { code: 'unauthorized', message: 'authentication required' } })) as unknown as typeof fetch
    const c = new ApiClient('/api/v1', fetcher, store)
    await expect(c.me()).rejects.toMatchObject({ status: 401, code: 'unauthorized', message: 'authentication required' })
    expect(c.signedIn).toBe(false)
    expect(store.getItem('orchivio.csrf')).toBeNull()
  })

  it('survives non-JSON error pages and empty 204 responses', async () => {
    const bad = (async () => new Response('<html>502</html>', { status: 502 })) as unknown as typeof fetch
    const err = await new ApiClient('/api/v1', bad, null).me().catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ status: 502, code: 'http_error' })
    const none = (async () => new Response(null, { status: 204 })) as unknown as typeof fetch
    await expect(new ApiClient('/api/v1', none, null).logout()).resolves.toBeUndefined()
  })

  it('validates a flow through the server and returns its issues', async () => {
    const fetcher = vi.fn(async () => json(200, { valid: false, issues: [{ severity: 'error', code: 'x', path: '/p', message: 'm' }] })) as unknown as typeof fetch
    const issues = await new ApiClient('/api/v1', fetcher, null).validateFlow({}, 'core', { key: 'K', kind: 'server', params: [], steps: [], edges: [] })
    expect(issues).toHaveLength(1)
  })
})
