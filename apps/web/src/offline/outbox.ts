import { ApiError, type ApiClient } from '../api/client'
import type { Db } from './idb'
import type { Sealed, Vault } from './vault'

// The outbox holds requests made while offline (or while the server could not be reached) until they can be sent (docs/OFFLINE.md).
// * Additive requests (creating a requirement, a story, feedback, a proposal) cannot conflict; an Idempotency-Key makes sending them again safe.
// * Edits and deletes of stories and requirements carry the version the person saw (`If-Match`). If someone changed the item meanwhile the server answers 409
//   with its current copy: when the fields this person changed were untouched, the edit is re-based and sent again; when the server already holds the same
//   values it counts as done; otherwise it becomes a *conflict* that the person resolves (keep mine / keep theirs).

export type OutboxKind = 'requirement' | 'story' | 'feedback' | 'proposal' | 'requirement_edit' | 'requirement_delete' | 'story_edit' | 'story_delete'
const U = '[0-9a-f-]{36}'
export const KINDS: Record<OutboxKind, RegExp> = {
  requirement: new RegExp(`^/projects/${U}/requirements$`),
  story: new RegExp(`^/projects/${U}/stories$`),
  feedback: new RegExp(`^/applications/${U}/feedback$`),
  proposal: new RegExp(`^/applications/${U}/proposals$`),
  requirement_edit: new RegExp(`^/projects/${U}/requirements/${U}$`),
  requirement_delete: new RegExp(`^/projects/${U}/requirements/${U}$`),
  story_edit: new RegExp(`^/projects/${U}/stories/${U}$`),
  story_delete: new RegExp(`^/projects/${U}/stories/${U}$`),
}
const METHOD: Record<OutboxKind, 'POST' | 'PATCH' | 'DELETE'> = {
  requirement: 'POST', story: 'POST', feedback: 'POST', proposal: 'POST',
  requirement_edit: 'PATCH', story_edit: 'PATCH', requirement_delete: 'DELETE', story_delete: 'DELETE',
}
export const isChange = (k: OutboxKind): boolean => METHOD[k] !== 'POST'

type Json = Record<string, unknown>
export interface QueuedRequest {
  kind: OutboxKind; path: string; body: unknown; label: string; project?: string
  /** Edits and deletes: the version of the item the person saw, and the values (before the change) of the fields they changed. */
  ifMatch?: number; base?: Json
}
export type OutboxStatus = 'waiting' | 'sending' | 'rejected' | 'conflict'

interface Content extends QueuedRequest { server?: Json | null }
/** What is stored for a queued request: bookkeeping in the clear, content (path, body, label) sealed. */
interface Stored { id?: number; createdAt: string; status: OutboxStatus; attempts: number; kind: OutboxKind; idempotencyKey: string; error?: string; sealed: Sealed }
export interface OutboxItem extends QueuedRequest {
  id: number; createdAt: string; status: OutboxStatus; attempts: number; idempotencyKey: string; error?: string
  /** For a conflict: the server's current copy (``null``: the item no longer exists). */
  server?: Json | null
}

export const MAX_QUEUED = 200
const newKey = (): string => `q-${crypto.randomUUID()}`

export class Outbox {
  constructor(private readonly db: Db, private readonly vault: Vault) {}

  count(): Promise<number> { return this.db.count('outbox') }

  async enqueue(req: QueuedRequest): Promise<number> {
    if (!KINDS[req.kind].test(req.path)) throw new Error('This kind of request cannot be saved for later.')
    if (isChange(req.kind)) {
      // several offline changes to one item collapse into one request; a delete replaces edits; changing something already queued for deletion is refused
      const queued = (await this.list()).filter((i) => i.path === req.path && isChange(i.kind) && i.status !== 'conflict')
      for (const old of queued) {
        if (old.kind.endsWith('_delete')) throw new Error('This item is already waiting to be deleted.')
        if (req.kind.endsWith('_edit')) {
          const base = { ...req.base, ...old.base }
          await this.rewrite(old.id, { body: { ...(old.body as Json), ...(req.body as Json) }, base, label: req.label }, { status: 'waiting', error: undefined })
          return old.id
        }
        await this.remove(old.id)
        req = { ...req, ifMatch: old.ifMatch ?? req.ifMatch }
      }
    }
    if (await this.count() >= MAX_QUEUED) throw new Error(`Too many unsent changes (${MAX_QUEUED}); connect to send them first.`)
    const sealed = await this.vault.seal(this.content(req), 'outbox')
    const row: Stored = { createdAt: new Date().toISOString(), status: 'waiting', attempts: 0, kind: req.kind, idempotencyKey: newKey(), sealed }
    return (await this.db.put('outbox', row)) as number
  }

  private content(c: Content): Content {
    return { kind: c.kind, path: c.path, body: c.body, label: c.label, project: c.project, ifMatch: c.ifMatch, base: c.base, server: c.server }
  }

  /** The queue in the order it will be sent. Needs the vault unlocked, because the content is sealed. */
  async list(): Promise<OutboxItem[]> {
    const rows = (await this.db.all<Stored>('outbox')).sort((a, b) => (a.id ?? 0) - (b.id ?? 0))
    const out: OutboxItem[] = []
    for (const r of rows) {
      const c = await this.vault.open<Content>(r.sealed, 'outbox')
      out.push({ ...c, id: r.id!, createdAt: r.createdAt, status: r.status, attempts: r.attempts, kind: r.kind, idempotencyKey: r.idempotencyKey, error: r.error })
    }
    return out
  }

  private async update(id: number, change: Partial<Stored>): Promise<void> {
    const row = await this.db.get<Stored>('outbox', id)
    if (row) await this.db.put('outbox', { ...row, ...change })
  }

  /** Change what a queued request says (re-sealed) and, optionally, its bookkeeping. */
  async rewrite(id: number, change: Partial<Content>, bookkeeping: Partial<Stored> = {}): Promise<void> {
    const row = await this.db.get<Stored>('outbox', id)
    if (!row) return
    const now = await this.vault.open<Content>(row.sealed, 'outbox')
    await this.db.put('outbox', { ...row, ...bookkeeping, sealed: await this.vault.seal(this.content({ ...now, ...change }), 'outbox') })
  }

  remove(id: number): Promise<void> { return this.db.del('outbox', id) }
  retry(id: number): Promise<void> { return this.update(id, { status: 'waiting', error: undefined }) }

  /** A conflict decided by the person: ``mine`` sends the change on top of the server's current copy; ``theirs`` drops it. */
  async resolve(id: number, keep: 'mine' | 'theirs'): Promise<void> {
    if (keep === 'theirs') return this.remove(id)
    const item = (await this.list()).find((i) => i.id === id)
    if (!item) return
    if (item.server === null && item.kind.endsWith('_edit')) return this.remove(id) // nothing left to change: the item is gone
    const v = typeof item.server?.version === 'number' ? item.server.version : undefined
    await this.rewrite(id, { ifMatch: v, base: item.server ?? undefined, server: undefined }, { status: 'waiting', error: undefined })
  }
}

export type SyncHalt = 'signed_out' | 'unavailable' | null
export interface SyncResult { sent: number; rejected: number; conflicts: number; left: number; halted: SyncHalt }

const isNetwork = (e: unknown) => e instanceof TypeError || (typeof DOMException !== 'undefined' && e instanceof DOMException && e.name === 'AbortError')
const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null)

/**
 * Send the waiting requests in order. A request the server **refuses** (validation, permission, a missing project) is kept as ``rejected`` with the server's
 * reason for a person to decide on; it is never dropped silently and never retried automatically. A change made on a stale version is merged when that is
 * provably safe and otherwise kept as a ``conflict``. Signing out or a server that cannot be reached stops the run and leaves the rest waiting. Each send
 * carries the item's Idempotency-Key, so an answer lost on the way does not make a duplicate.
 */
export async function syncOutbox(client: Pick<ApiClient, 'request'>, outbox: Outbox, db: Db): Promise<SyncResult> {
  const result: SyncResult = { sent: 0, rejected: 0, conflicts: 0, left: 0, halted: null }
  const mark = async (id: number, change: Partial<Stored>) => { const r = await db.get<Stored>('outbox', id); if (r) await db.put('outbox', { ...r, ...change }) }
  for (let item of await outbox.list()) {
    if (item.status === 'rejected' || item.status === 'conflict') continue
    const method = METHOD[item.kind]
    for (let round = 0; round < 3; round++) {
      const row = await db.get<Stored>('outbox', item.id)
      await mark(item.id, { status: 'sending', attempts: (row?.attempts ?? 0) + 1 })
      const headers: Record<string, string> = method === 'POST' ? { 'Idempotency-Key': item.idempotencyKey } : item.ifMatch !== undefined ? { 'If-Match': `"${item.ifMatch}"` } : {}
      try {
        await client.request(method, item.path, method === 'DELETE' ? undefined : item.body, headers)
        await outbox.remove(item.id)
        result.sent += 1
        break
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) { await mark(item.id, { status: 'waiting' }); result.halted = 'signed_out'; return finish(result, outbox) }
        if (isNetwork(e) || (e instanceof ApiError && (e.status === 429 || e.status >= 500))) { await mark(item.id, { status: 'waiting' }); result.halted = 'unavailable'; return finish(result, outbox) }
        if (isChange(item.kind) && e instanceof ApiError && (e.status === 404 || e.code === 'version_conflict')) {
          const current = e.status === 404 ? null : (e.details as { current?: Json } | undefined)?.current ?? null
          const body = (item.body ?? {}) as Json
          if (current === null && method === 'DELETE') { await outbox.remove(item.id); result.sent += 1; break } // already gone: the wish is fulfilled
          if (current !== null && method === 'PATCH' && Object.keys(body).every((k) => same(current[k], body[k]))) { await outbox.remove(item.id); result.sent += 1; break } // the server already holds these values
          const base = item.base
          if (current !== null && method === 'PATCH' && round < 2 && base && typeof current.version === 'number' && Object.keys(body).every((k) => same(current[k], base[k]))) {
            // the fields this person changed were not touched by anyone else: send the same change on top of the newer version
            await outbox.rewrite(item.id, { ifMatch: current.version, base: { ...base, ...Object.fromEntries(Object.keys(body).map((k) => [k, current[k]])) } })
            item = { ...item, ifMatch: current.version, base: { ...base, ...Object.fromEntries(Object.keys(body).map((k) => [k, current[k]])) } }
            continue
          }
          await outbox.rewrite(item.id, { server: current }, { status: 'conflict', error: current === null ? 'The item no longer exists on the server.' : 'Someone else changed this item while you were offline.' })
          result.conflicts += 1
          break
        }
        const message = e instanceof ApiError ? e.message : 'The request could not be sent.'
        await mark(item.id, { status: 'rejected', error: message })
        result.rejected += 1
        break
      }
    }
  }
  return finish(result, outbox)
}

async function finish(r: SyncResult, outbox: Outbox): Promise<SyncResult> { r.left = await outbox.count(); return r }

// --- saved copies for reading offline ------------------------------------------------------------------------------

export interface Snapshot<T> { data: T; savedAt: string }

/** Saved copies kept at most; the least recently saved go first. Waiting changes are never pruned. */
export const MAX_SNAPSHOTS = 40
type SnapshotRow = Sealed & { key?: string; at?: string }

export async function saveSnapshot<T>(db: Db, vault: Vault, key: string, data: T): Promise<void> {
  const at = new Date().toISOString()
  const sealed = await vault.seal({ data, savedAt: at } satisfies Snapshot<T>, `snapshot:${key}`)
  await db.put('snapshots', { ...sealed, key, at } satisfies SnapshotRow, key)
  const rows = (await db.all<SnapshotRow>('snapshots')).filter((r) => r.key !== undefined)
  if (rows.length > MAX_SNAPSHOTS) {
    for (const old of rows.sort((a, b) => (a.at ?? '').localeCompare(b.at ?? '')).slice(0, rows.length - MAX_SNAPSHOTS)) await db.del('snapshots', old.key!)
  }
}

export async function loadSnapshot<T>(db: Db, vault: Vault, key: string): Promise<Snapshot<T> | null> {
  const row = await db.get<SnapshotRow>('snapshots', key)
  return row ? vault.open<Snapshot<T>>(row, `snapshot:${key}`) : null
}
