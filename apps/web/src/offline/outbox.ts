import { ApiError, type ApiClient } from '../api/client'
import type { Db } from './idb'
import type { Sealed, Vault } from './vault'

// The outbox holds requests made while offline (or while the server could not be reached) until they can be sent. Only *additive* requests may be
// queued (docs/OFFLINE.md): creating a requirement, a story, a piece of feedback or a proposal. They cannot conflict with other people's work, and an
// Idempotency-Key makes sending them again safe. Edits and deletes need a connection.

export type OutboxKind = 'requirement' | 'story' | 'feedback' | 'proposal'
export const KINDS: Record<OutboxKind, RegExp> = {
  requirement: /^\/projects\/[0-9a-f-]{36}\/requirements$/,
  story: /^\/projects\/[0-9a-f-]{36}\/stories$/,
  feedback: /^\/applications\/[0-9a-f-]{36}\/feedback$/,
  proposal: /^\/applications\/[0-9a-f-]{36}\/proposals$/,
}

export interface QueuedRequest { kind: OutboxKind; path: string; body: unknown; label: string; project?: string }
export type OutboxStatus = 'waiting' | 'sending' | 'rejected'

/** What is stored for a queued request: bookkeeping in the clear, content (path, body, label) sealed. */
interface Stored { id?: number; createdAt: string; status: OutboxStatus; attempts: number; kind: OutboxKind; idempotencyKey: string; error?: string; sealed: Sealed }
export interface OutboxItem extends QueuedRequest { id: number; createdAt: string; status: OutboxStatus; attempts: number; idempotencyKey: string; error?: string }

export const MAX_QUEUED = 200
const newKey = (): string => `q-${crypto.randomUUID()}`

export class Outbox {
  constructor(private readonly db: Db, private readonly vault: Vault) {}

  count(): Promise<number> { return this.db.count('outbox') }

  async enqueue(req: QueuedRequest): Promise<number> {
    if (!KINDS[req.kind].test(req.path)) throw new Error('This kind of request cannot be saved for later.')
    if (await this.count() >= MAX_QUEUED) throw new Error(`Too many unsent changes (${MAX_QUEUED}); connect to send them first.`)
    const sealed = await this.vault.seal({ path: req.path, body: req.body, label: req.label, project: req.project }, 'outbox')
    const row: Stored = { createdAt: new Date().toISOString(), status: 'waiting', attempts: 0, kind: req.kind, idempotencyKey: newKey(), sealed }
    return (await this.db.put('outbox', row)) as number
  }

  /** The queue in the order it will be sent. Needs the vault unlocked, because the content is sealed. */
  async list(): Promise<OutboxItem[]> {
    const rows = (await this.db.all<Stored>('outbox')).sort((a, b) => (a.id ?? 0) - (b.id ?? 0))
    const out: OutboxItem[] = []
    for (const r of rows) {
      const c = await this.vault.open<{ path: string; body: unknown; label: string; project?: string }>(r.sealed, 'outbox')
      out.push({ id: r.id!, createdAt: r.createdAt, status: r.status, attempts: r.attempts, kind: r.kind, idempotencyKey: r.idempotencyKey, error: r.error, ...c })
    }
    return out
  }

  private async update(id: number, change: Partial<Stored>): Promise<void> {
    const row = await this.db.get<Stored>('outbox', id)
    if (row) await this.db.put('outbox', { ...row, ...change })
  }

  remove(id: number): Promise<void> { return this.db.del('outbox', id) }
  retry(id: number): Promise<void> { return this.update(id, { status: 'waiting', error: undefined }) }
}

export type SyncHalt = 'signed_out' | 'unavailable' | null
export interface SyncResult { sent: number; rejected: number; left: number; halted: SyncHalt }

const isNetwork = (e: unknown) => e instanceof TypeError || (typeof DOMException !== 'undefined' && e instanceof DOMException && e.name === 'AbortError')

/**
 * Send the waiting requests in order. A request the server **refuses** (validation, permission, a missing project) is kept as ``rejected`` with the server's
 * reason for a person to decide on; it is never dropped silently and never retried automatically. Signing out or a server that cannot be reached stops the run
 * and leaves the rest waiting. Each send carries the item's Idempotency-Key, so an answer lost on the way does not make a duplicate.
 */
export async function syncOutbox(client: Pick<ApiClient, 'request'>, outbox: Outbox, db: Db): Promise<SyncResult> {
  const result: SyncResult = { sent: 0, rejected: 0, left: 0, halted: null }
  for (const item of await outbox.list()) {
    if (item.status === 'rejected') continue
    const row = await db.get<Stored>('outbox', item.id)
    if (row) await db.put('outbox', { ...row, status: 'sending', attempts: row.attempts + 1 })
    try {
      await client.request('POST', item.path, item.body, { 'Idempotency-Key': item.idempotencyKey })
      await outbox.remove(item.id)
      result.sent += 1
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) { await restore(db, item.id); result.halted = 'signed_out'; break }
      if (isNetwork(e) || (e instanceof ApiError && (e.status === 429 || e.status >= 500))) { await restore(db, item.id); result.halted = 'unavailable'; break }
      const message = e instanceof ApiError ? e.message : 'The request could not be sent.'
      const again = await db.get<Stored>('outbox', item.id)
      if (again) await db.put('outbox', { ...again, status: 'rejected', error: message })
      result.rejected += 1
    }
  }
  result.left = await outbox.count()
  return result
}

async function restore(db: Db, id: number): Promise<void> {
  const row = await db.get<Stored>('outbox', id)
  if (row) await db.put('outbox', { ...row, status: 'waiting' })
}

// --- saved copies for reading offline ------------------------------------------------------------------------------

export interface Snapshot<T> { data: T; savedAt: string }

export async function saveSnapshot<T>(db: Db, vault: Vault, key: string, data: T): Promise<void> {
  await db.put('snapshots', await vault.seal({ data, savedAt: new Date().toISOString() } satisfies Snapshot<T>, `snapshot:${key}`), key)
}

export async function loadSnapshot<T>(db: Db, vault: Vault, key: string): Promise<Snapshot<T> | null> {
  const sealed = await db.get<Sealed>('snapshots', key)
  return sealed ? vault.open<Snapshot<T>>(sealed, `snapshot:${key}`) : null
}
