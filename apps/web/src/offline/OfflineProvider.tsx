import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { ApiClient } from '../api/client'
import { useOnline } from '../pwa/useOnline'
import { openDb, supported, type Db } from './idb'
import { loadSnapshot, Outbox, saveSnapshot, syncOutbox, type OutboxItem, type QueuedRequest, type SyncResult } from './outbox'
import { Vault, VaultError } from './vault'

export type Submitted<T> = { queued: false; value: T } | { queued: true; id: number }

export interface Offline {
  supported: boolean
  /** A vault exists on this device. */
  enabled: boolean
  unlocked: boolean
  online: boolean
  pending: number
  items: OutboxItem[]
  syncing: boolean
  lastSync: SyncResult | null
  error: string
  /** The offline data on this device belongs to another account. */
  foreign: boolean
  /** Tell the provider who is signed in (``null`` when nobody is): offline data is tied to one account. */
  bindUser(id: string | null): void
  enable(passphrase: string): Promise<void>
  unlock(passphrase: string): Promise<boolean>
  lock(): void
  /** Delete the vault, the queue and the saved copies from this device. */
  disable(): Promise<void>
  syncNow(): Promise<void>
  retry(id: number): Promise<void>
  discard(id: number): Promise<void>
  /** Send now, or (offline, or when the server cannot be reached) keep it for later. Errors from the server are thrown as they are. */
  submit<T>(req: QueuedRequest, send: () => Promise<T>): Promise<Submitted<T>>
  /** Keep a copy for reading offline (does nothing while locked). */
  snapshot<T>(key: string, data: T): Promise<void>
  /** The data from the server, or the saved copy when the server cannot be reached. */
  readThrough<T>(key: string, load: () => Promise<T>): Promise<{ data: T; stale: boolean; savedAt?: string }>
  loadSaved<T>(key: string): Promise<{ data: T; savedAt: string } | null>
}

const Ctx = createContext<Offline | null>(null)

/** The offline features, or ``null`` where no provider is mounted (tests, the demo). */
export function useOffline(): Offline | null { return useContext(Ctx) }

export class OfflineError extends Error {}

const isNetworkError = (e: unknown) => e instanceof TypeError

export function OfflineProvider({ client, children, openDatabase = () => openDb(), syncEveryMs = 30_000, kdfIterations }: { client: ApiClient; children: ReactNode; openDatabase?: () => Promise<Db>; syncEveryMs?: number; kdfIterations?: number }) {
  const online = useOnline()
  const canUse = supported()
  const [db, setDb] = useState<Db | null>(null)
  const vault = useRef<Vault | null>(null)
  const outbox = useRef<Outbox | null>(null)
  const [enabled, setEnabled] = useState(false)
  const [unlocked, setUnlocked] = useState(false)
  const [pending, setPending] = useState(0)
  const [items, setItems] = useState<OutboxItem[]>([])
  const [syncing, setSyncing] = useState(false)
  const [lastSync, setLastSync] = useState<SyncResult | null>(null)
  const [error, setError] = useState('')
  const [foreign, setForeign] = useState(false)
  const owner = useRef<string | null>(null)
  const busy = useRef(false)

  useEffect(() => {
    if (!canUse) return
    let live = true
    void openDatabase().then(async (d) => {
      if (!live) return
      vault.current = new Vault(d, kdfIterations)
      outbox.current = new Outbox(d, vault.current)
      setDb(d)
      setEnabled(await vault.current.exists())
      setPending(await outbox.current.count())
    }, () => setError('Offline storage is not available in this browser.'))
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canUse])

  const checkOwner = useCallback(async () => {
    const stored = db ? await db.get<string>('meta', 'owner') : undefined
    setForeign(stored !== undefined && owner.current !== null && stored !== owner.current)
  }, [db])
  useEffect(() => { void checkOwner() }, [checkOwner])

  const refresh = useCallback(async () => {
    if (!outbox.current) return
    setPending(await outbox.current.count())
    setItems(vault.current?.unlocked ? await outbox.current.list() : [])
  }, [])

  const syncNow = useCallback(async () => {
    if (!db || !outbox.current || !vault.current?.unlocked || busy.current) return
    busy.current = true
    setSyncing(true)
    try { setLastSync(await syncOutbox(client, outbox.current, db)) } catch (e) { setError(e instanceof Error ? e.message : 'Sending failed.') } finally { busy.current = false; setSyncing(false); await refresh() }
  }, [client, db, refresh])

  // send what is waiting when the connection comes back, when the vault is unlocked, and now and then while something is waiting
  useEffect(() => { if (online && unlocked && pending > 0) void syncNow() }, [online, unlocked, pending, syncNow])
  useEffect(() => {
    if (!online || !unlocked || pending === 0 || syncEveryMs <= 0) return
    const t = setInterval(() => void syncNow(), syncEveryMs)
    return () => clearInterval(t)
  }, [online, unlocked, pending, syncNow, syncEveryMs])

  const api = useMemo<Offline>(() => ({
    supported: canUse && db !== null, enabled, unlocked, online, pending, items, syncing, lastSync, error, foreign,
    bindUser(id) { owner.current = id; void checkOwner() },
    async enable(passphrase) {
      setError('')
      try { if (!owner.current) throw new VaultError('Sign in first.'); await vault.current!.create(passphrase); await db!.put('meta', owner.current, 'owner'); setForeign(false); setEnabled(true); setUnlocked(true); await refresh() } catch (e) { setError(e instanceof VaultError ? e.message : 'Could not set up offline work.'); throw e }
    },
    async unlock(passphrase) {
      setError('')
      if (foreign) { setError('This device holds offline data of another account. Delete it to use offline work with this account.'); return false }
      const ok = await vault.current!.unlock(passphrase)
      if (!ok) { setError('That passphrase does not open the saved data.'); return false }
      setUnlocked(true)
      await refresh()
      return true
    },
    lock() { vault.current?.lock(); setUnlocked(false); setItems([]) },
    async disable() { await vault.current?.wipe(); setForeign(false); setEnabled(false); setUnlocked(false); setPending(0); setItems([]); setLastSync(null) },
    syncNow,
    async retry(id) { await outbox.current?.retry(id); await refresh(); void syncNow() },
    async discard(id) { await outbox.current?.remove(id); await refresh() },
    async submit<T>(req: QueuedRequest, send: () => Promise<T>): Promise<Submitted<T>> {
      const queue = async (): Promise<Submitted<T>> => {
        if (!enabled) throw new OfflineError('You are offline. Turn on offline work to keep changes on this device until you are connected again.')
        if (!vault.current?.unlocked) throw new OfflineError('You are offline. Unlock offline work to keep this change on this device.')
        const id = await outbox.current!.enqueue(req)
        await refresh()
        return { queued: true, id }
      }
      if (!online) return queue()
      try { return { queued: false, value: await send() } } catch (e) {
        if (isNetworkError(e)) return queue()
        throw e
      }
    },
    async snapshot(key, data) { if (db && vault.current?.unlocked) await saveSnapshot(db, vault.current, key, data) },
    async readThrough<T>(key: string, load: () => Promise<T>) {
      try {
        const data = await load()
        if (db && vault.current?.unlocked) await saveSnapshot(db, vault.current, key, data)
        return { data, stale: false }
      } catch (e) {
        if (!isNetworkError(e) || !db || !vault.current?.unlocked) throw e
        const saved = await loadSnapshot<T>(db, vault.current, key)
        if (!saved) throw e
        return { data: saved.data, stale: true, savedAt: saved.savedAt }
      }
    },
    async loadSaved<T>(key: string) { return db && vault.current?.unlocked ? loadSnapshot<T>(db, vault.current, key) : null },
  }), [canUse, db, enabled, unlocked, online, pending, items, syncing, lastSync, error, foreign, checkOwner, refresh, syncNow])

  return <Ctx.Provider value={api}>{children}</Ctx.Provider>
}
