import { vi } from 'vitest'
import type { ApiClient } from '../api/client'

type Handler = (body: unknown, path: string) => unknown
export interface Call { method: string; path: string; body?: unknown }

const pattern = (key: string): { method: string; re: RegExp } => {
  const [method, ...rest] = key.split(' ')
  const path = rest.join(' ').replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/:[a-z_]+/g, '[^/]+')
  return { method: method!, re: new RegExp(`^${path}$`) }
}

/** A fake ApiClient: routes are "METHOD /path/:param" → handler or value. Throwing from a handler becomes the request's error. */
export function mockApi(routes: Record<string, Handler | unknown>) {
  const table = Object.entries(routes).map(([key, h]) => ({ ...pattern(key), h }))
  const calls: Call[] = []
  const answer = async (method: string, path: string, body?: unknown) => {
    calls.push({ method, path, body })
    const bare = path.split('?')[0]!
    const hit = table.find((r) => r.method === method && r.re.test(bare))
    if (!hit) throw new Error(`unmocked ${method} ${path}`)
    const out = typeof hit.h === 'function' ? (hit.h as Handler)(body, path) : hit.h
    return out
  }
  const client = {
    request: vi.fn(answer),
    requestText: vi.fn(async (method: string, path: string, text: string) => answer(method, path, text)),
    url: (p: string) => `/api/v1${p}`,
  } as unknown as ApiClient
  const find = (method: string, pathPart: string) => calls.filter((c) => c.method === method && c.path.includes(pathPart))
  return { client, calls, find }
}
