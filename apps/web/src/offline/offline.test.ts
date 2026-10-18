import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import { openDb, type Db } from './idb'
import { KINDS, loadSnapshot, MAX_QUEUED, Outbox, saveSnapshot, syncOutbox } from './outbox'
import { Vault, VaultError } from './vault'

let n = 0
const PROJECT = '11111111-1111-4111-8111-111111111111'
const PASS = 'a long passphrase'

async function fresh() {
  const db = await openDb(`test-${n++}`)
  const vault = new Vault(db, 1000) // few iterations: speed, not security, in tests
  return { db, vault, outbox: new Outbox(db, vault) }
}

const story = (title: string) => ({ kind: 'story' as const, path: `/projects/${PROJECT}/stories`, body: { title }, label: title, project: PROJECT })

describe('vault', () => {
  it('creates, locks, and opens again only with the same passphrase', async () => {
    const { vault, db } = await fresh()
    expect(await vault.exists()).toBe(false)
    await vault.create(PASS)
    expect(vault.unlocked && (await vault.exists())).toBe(true)
    const sealed = await vault.seal({ secret: 'hello' }, 'x')
    vault.lock()
    expect(vault.unlocked).toBe(false)
    await expect(vault.seal('x', 'x')).rejects.toBeInstanceOf(VaultError)
    expect(await vault.unlock('wrong passphrase!')).toBe(false)
    expect(vault.unlocked).toBe(false)
    expect(await vault.unlock(PASS)).toBe(true)
    expect(await vault.open(sealed, 'x')).toEqual({ secret: 'hello' })
    // a second vault object on the same database (a new session) opens it as well
    const again = new Vault(db, 1000)
    expect(await again.unlock(PASS)).toBe(true)
    expect(await again.open(sealed, 'x')).toEqual({ secret: 'hello' })
  })

  it('refuses short passphrases and a second setup', async () => {
    const { vault } = await fresh()
    await expect(vault.create('short')).rejects.toThrow(/at least 10/)
    await vault.create(PASS)
    await expect(vault.create('another long one')).rejects.toThrow(/already set up/)
  })

  it('never stores the passphrase or the plaintext, and uses a fresh IV for every record', async () => {
    const { vault, db } = await fresh()
    await vault.create(PASS)
    const a = await vault.seal({ text: 'the same text' }, 'p')
    const b = await vault.seal({ text: 'the same text' }, 'p')
    expect(a.iv).not.toBe(b.iv)
    expect(a.data).not.toBe(b.data)
    const stored = JSON.stringify(await db.get('meta', 'vault'))
    expect(stored).not.toContain(PASS)
    expect(JSON.stringify(a)).not.toContain('the same text')
  })

  it('binds a record to its purpose and detects tampering', async () => {
    const { vault } = await fresh()
    await vault.create(PASS)
    const sealed = await vault.seal({ a: 1 }, 'outbox')
    await expect(vault.open(sealed, 'snapshot:x')).rejects.toBeInstanceOf(VaultError) // not usable as another kind of record
    const flipped = { ...sealed, data: sealed.data.slice(0, -4) + (sealed.data.endsWith('AAAA') ? 'BBBB' : 'AAAA') }
    await expect(vault.open(flipped, 'outbox')).rejects.toBeInstanceOf(VaultError)
  })

  it('wipes everything', async () => {
    const { vault, db, outbox } = await fresh()
    await vault.create(PASS)
    await outbox.enqueue(story('x'))
    await saveSnapshot(db, vault, 'k', { a: 1 })
    await vault.wipe()
    expect(vault.unlocked).toBe(false)
    expect([await db.count('meta'), await db.count('outbox'), await db.count('snapshots')]).toEqual([0, 0, 0])
  })
})

describe('outbox', () => {
  let ctx: Awaited<ReturnType<typeof fresh>>
  beforeEach(async () => { ctx = await fresh(); await ctx.vault.create(PASS) })

  it('keeps content sealed and bookkeeping readable', async () => {
    await ctx.outbox.enqueue(story('Secret plan for the warehouse'))
    const raw = JSON.stringify(await ctx.db.all('outbox'))
    expect(raw).not.toContain('Secret plan')
    expect(raw).not.toContain(PROJECT)
    expect(raw).toContain('"status":"waiting"')
    expect(await ctx.outbox.count()).toBe(1)
    ctx.vault.lock()
    expect(await ctx.outbox.count()).toBe(1) // counting needs no key
    await expect(ctx.outbox.list()).rejects.toBeInstanceOf(VaultError)
    await ctx.vault.unlock(PASS)
    const [item] = await ctx.outbox.list()
    expect(item).toMatchObject({ kind: 'story', label: 'Secret plan for the warehouse', body: { title: 'Secret plan for the warehouse' }, status: 'waiting', attempts: 0 })
    expect(item!.idempotencyKey).toMatch(/^q-[0-9a-f-]{36}$/)
  })

  it('only queues additive requests to known paths', async () => {
    for (const bad of [
      { kind: 'story' as const, path: `/projects/${PROJECT}/stories/${PROJECT}`, body: {}, label: 'x' }, // an edit
      { kind: 'story' as const, path: `/projects/${PROJECT}/requirements`, body: {}, label: 'x' }, // wrong kind for the path
      { kind: 'proposal' as const, path: `/organizations/${PROJECT}`, body: {}, label: 'x' },
      { kind: 'story' as const, path: `/projects/../stories`, body: {}, label: 'x' },
      { kind: 'feedback' as const, path: `//evil.example/applications/${PROJECT}/feedback`, body: {}, label: 'x' },
    ]) await expect(ctx.outbox.enqueue(bad)).rejects.toThrow(/cannot be saved/)
    expect(Object.keys(KINDS).sort()).toEqual(['feedback', 'proposal', 'requirement', 'story'])
    expect(await ctx.outbox.count()).toBe(0)
  })

  it('has a limit', async () => {
    for (let i = 0; i < MAX_QUEUED; i++) await ctx.db.put('outbox', { createdAt: '', status: 'waiting', attempts: 0, kind: 'story', idempotencyKey: `q-${i}`, sealed: { iv: '', data: '' } })
    await expect(ctx.outbox.enqueue(story('one too many'))).rejects.toThrow(/Too many unsent/)
  })
})

describe('sync', () => {
  let ctx: Awaited<ReturnType<typeof fresh>>
  beforeEach(async () => { ctx = await fresh(); await ctx.vault.create(PASS) })
  const client = (handler: (method: string, path: string, body: unknown, headers: Record<string, string>) => unknown) =>
    ({ request: vi.fn(async (m: string, p: string, b: unknown, h: Record<string, string>) => handler(m, p, b, h)) }) as never

  it('sends in order with an idempotency key each and removes what was accepted', async () => {
    await ctx.outbox.enqueue(story('first'))
    await ctx.outbox.enqueue(story('second'))
    const c = client(() => ({}))
    const result = await syncOutbox(c, ctx.outbox, ctx.db)
    expect(result).toEqual({ sent: 2, rejected: 0, left: 0, halted: null })
    const calls = (c as unknown as { request: ReturnType<typeof vi.fn> }).request.mock.calls
    expect(calls.map((x) => (x[2] as { title: string }).title)).toEqual(['first', 'second'])
    expect(calls[0]![0]).toBe('POST')
    expect(calls[0]![3]).toEqual({ 'Idempotency-Key': expect.stringMatching(/^q-/) })
    expect(calls[0]![3]['Idempotency-Key']).not.toBe(calls[1]![3]['Idempotency-Key'])
  })

  it('keeps the same key when a send has to be repeated, so a lost answer cannot make a duplicate', async () => {
    await ctx.outbox.enqueue(story('once'))
    const keys: string[] = []
    let attempt = 0
    const c = client((_m, _p, _b, h) => { keys.push(h['Idempotency-Key']!); attempt += 1; if (attempt === 1) throw new TypeError('connection dropped after the server processed it'); return {} })
    expect((await syncOutbox(c, ctx.outbox, ctx.db)).halted).toBe('unavailable')
    expect(await ctx.outbox.count()).toBe(1)
    expect((await syncOutbox(c, ctx.outbox, ctx.db)).sent).toBe(1)
    expect(keys).toHaveLength(2)
    expect(keys[0]).toBe(keys[1])
  })

  it('keeps a refused change with the server’s reason and carries on with the others', async () => {
    await ctx.outbox.enqueue(story('bad'))
    await ctx.outbox.enqueue(story('good'))
    const c = client((_m, _p, b) => { if ((b as { title: string }).title === 'bad') throw new ApiError(422, 'validation_failed', 'title is too long'); return {} })
    expect(await syncOutbox(c, ctx.outbox, ctx.db)).toEqual({ sent: 1, rejected: 1, left: 1, halted: null })
    const [kept] = await ctx.outbox.list()
    expect(kept).toMatchObject({ label: 'bad', status: 'rejected', error: 'title is too long' })
    // a refused change is not retried by itself ...
    const again = client(() => ({}))
    expect(await syncOutbox(again, ctx.outbox, ctx.db)).toEqual({ sent: 0, rejected: 0, left: 1, halted: null })
    expect((again as unknown as { request: ReturnType<typeof vi.fn> }).request).not.toHaveBeenCalled()
    // ... until a person asks
    await ctx.outbox.retry(kept!.id)
    expect((await syncOutbox(again, ctx.outbox, ctx.db)).sent).toBe(1)
    expect(await ctx.outbox.count()).toBe(0)
  })

  it('stops, and keeps the order, when signed out or when the server cannot be reached', async () => {
    for (const t of ['a', 'b', 'c']) await ctx.outbox.enqueue(story(t))
    const signedOut = client((_m, _p, b) => { if ((b as { title: string }).title === 'b') throw new ApiError(401, 'unauthorized', 'authentication required'); return {} })
    expect(await syncOutbox(signedOut, ctx.outbox, ctx.db)).toEqual({ sent: 1, rejected: 0, left: 2, halted: 'signed_out' })
    expect((await ctx.outbox.list()).map((i) => [i.label, i.status])).toEqual([['b', 'waiting'], ['c', 'waiting']])
    for (const e of [new ApiError(503, 'unavailable', 'x'), new ApiError(429, 'rate', 'x'), new TypeError('network')]) {
      expect((await syncOutbox(client(() => { throw e }), ctx.outbox, ctx.db)).halted).toBe('unavailable')
    }
    expect(await ctx.outbox.count()).toBe(2)
    const sent = (await ctx.outbox.list()).map((i) => i.attempts)
    expect(sent.every((a) => a >= 1) || sent[0]! >= 1).toBe(true)
  })

  it('treats the other 4xx answers (conflict, forbidden, not found) as refusals, not as outages', async () => {
    for (const status of [403, 404, 409, 410, 413]) {
      const { outbox, db, vault } = await fresh()
      await vault.create(PASS)
      await outbox.enqueue(story('x'))
      const r = await syncOutbox(client(() => { throw new ApiError(status, 'e', `refused ${status}`) }), outbox, db)
      expect(r).toMatchObject({ rejected: 1, halted: null })
      expect((await outbox.list())[0]!.error).toBe(`refused ${status}`)
    }
  })
})

describe('saved copies', () => {
  it('round-trips, is bound to its key, and is unreadable while locked', async () => {
    const { vault, db } = await fresh()
    await vault.create(PASS)
    await saveSnapshot(db, vault, 'board:p1', { stories: [{ title: 'secret story' }] })
    const got = await loadSnapshot<{ stories: { title: string }[] }>(db, vault, 'board:p1')
    expect(got!.data.stories[0]!.title).toBe('secret story')
    expect(new Date(got!.savedAt).getTime()).toBeLessThanOrEqual(Date.now())
    expect(JSON.stringify(await db.all('snapshots'))).not.toContain('secret story')
    expect(await loadSnapshot(db, vault, 'board:other')).toBeNull()
    await db.put('snapshots', await db.get('snapshots', 'board:p1'), 'board:p2') // the same ciphertext filed under another key
    await expect(loadSnapshot(db, vault, 'board:p2')).rejects.toBeInstanceOf(VaultError)
    vault.lock()
    await expect(loadSnapshot(db, vault, 'board:p1')).rejects.toBeInstanceOf(VaultError)
  })
})

describe('storage', () => {
  it('is a no-op to close and clear an empty database', async () => {
    const db: Db = await openDb(`test-${n++}`)
    await db.clear()
    expect(await db.count('outbox')).toBe(0)
    db.close()
  })
})
