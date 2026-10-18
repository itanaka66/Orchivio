import { describe, expect, it, vi } from 'vitest'
import { OrchivioApiError, OrchivioDataClient } from './inventory-client'

const json = (status: number, body?: unknown) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

function clientWith(handler: (url: string, init: RequestInit) => Response, extra: Partial<ConstructorParameters<typeof OrchivioDataClient>[0]> = {}) {
  const calls: { url: string; init: RequestInit }[] = []
  const fetcher = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} })
    return handler(String(url), init ?? {})
  }) as unknown as typeof fetch
  return { client: new OrchivioDataClient({ baseUrl: '/api/v1/applications/a1/preview', fetch: fetcher, ...extra }), calls }
}

describe('generated data client (inventory)', () => {
  it('builds list queries from typed parameters', async () => {
    const { client, calls } = clientWith(() => json(200, { items: [], total: 0, limit: 5, offset: 0 }))
    await client.product.list({ limit: 5, offset: 10, orderBy: 'name', desc: true, filter: { sku: 'A 1', quantity: 0, name: undefined } })
    const url = new URL(calls[0]!.url, 'http://x')
    expect(url.pathname).toBe('/api/v1/applications/a1/preview/data/core.Product')
    expect(Object.fromEntries(url.searchParams)).toEqual({ limit: '5', offset: '10', order_by: 'name', desc: 'true', 'f.sku': 'A 1', 'f.quantity': '0' })
    expect(calls[0]!.init.method).toBe('GET')
    expect(calls[0]!.init.body).toBeUndefined()
  })

  it('sends JSON bodies with the content type and per-request headers', async () => {
    const { client, calls } = clientWith(() => json(201, { id: 'p1' }), { headers: async () => ({ 'X-Preview-User': 'u1' }), credentials: 'include' })
    await client.product.create({ name: 'Bolt', sku: 'B-1', quantity: 3, reorder_level: 1 })
    const init = calls[0]!.init
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('include')
    expect(init.headers).toMatchObject({ Accept: 'application/json', 'Content-Type': 'application/json', 'X-Preview-User': 'u1' })
    expect(JSON.parse(init.body as string)).toEqual({ name: 'Bolt', sku: 'B-1', quantity: 3, reorder_level: 1 })
  })

  it('encodes ids and uses PATCH and DELETE', async () => {
    const { client, calls } = clientWith((_u, init) => (init.method === 'DELETE' ? json(204) : json(200, {})))
    await client.product.update('a/b c', { quantity: 2 })
    await expect(client.product.delete('x?y')).resolves.toBeUndefined()
    expect(calls.map((c) => [c.init.method, c.url])).toEqual([
      ['PATCH', '/api/v1/applications/a1/preview/data/core.Product/a%2Fb%20c'],
      ['DELETE', '/api/v1/applications/a1/preview/data/core.Product/x%3Fy'],
    ])
  })

  it('turns the server’s error envelope into a typed error', async () => {
    const { client } = clientWith(() => json(422, { error: { code: 'validation_failed', message: 'sku is required', details: [{ field: 'sku' }] } }))
    const err = await client.product.create({ name: 'n', sku: '', quantity: 0, reorder_level: 0 }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(OrchivioApiError)
    expect(err).toMatchObject({ status: 422, code: 'validation_failed', message: 'sku is required', details: [{ field: 'sku' }] })
  })

  it('copes with error pages that are not JSON', async () => {
    const { client } = clientWith(() => new Response('<html>bad gateway</html>', { status: 502 }))
    await expect(client.product.get('1')).rejects.toMatchObject({ status: 502, code: 'http_error', message: 'Request failed (502)' })
  })
})
