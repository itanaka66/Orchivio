// Storage management for offline data (docs/OFFLINE.md): how much the browser lets this site keep, asking for it not to be evicted, and refusing to
// fill the last of the quota (a full disk would otherwise lose what a person just typed).

export interface StorageInfo { usage: number | null; quota: number | null; persisted: boolean | null }

/** Above this share of the quota nothing new is kept (saved copies are skipped, new offline changes are refused with a clear message). */
export const FULL = 0.9

export async function storageInfo(storage: Pick<StorageManager, 'estimate' | 'persisted'> | undefined = typeof navigator === 'undefined' ? undefined : navigator.storage): Promise<StorageInfo> {
  try {
    const e = await storage?.estimate?.()
    const persisted = (await storage?.persisted?.()) ?? null
    return { usage: e?.usage ?? null, quota: e?.quota ?? null, persisted }
  } catch { return { usage: null, quota: null, persisted: null } }
}

export const isFull = (i: StorageInfo): boolean => i.usage !== null && i.quota !== null && i.quota > 0 && i.usage / i.quota >= FULL

/** Ask the browser not to evict this site's data under storage pressure. It may say no (and does without asking in some browsers). */
export async function requestPersistent(storage: Pick<StorageManager, 'persist'> | undefined = typeof navigator === 'undefined' ? undefined : navigator.storage): Promise<boolean> {
  try { return (await storage?.persist?.()) ?? false } catch { return false }
}

export const megabytes = (n: number | null): string => (n === null ? 'unknown' : `${(n / 1_048_576).toFixed(n < 10_485_760 ? 1 : 0)} MB`)

/** Wake the Studio when the browser thinks the connection is back, even if its tab is in the background (Background Sync, Chromium only). */
export const SYNC_TAG = 'orchivio-outbox'
export async function requestBackgroundSync(): Promise<boolean> {
  try {
    const reg = await navigator.serviceWorker?.ready
    const sync = (reg as unknown as { sync?: { register(tag: string): Promise<void> } } | undefined)?.sync
    if (!sync) return false
    await sync.register(SYNC_TAG)
    return true
  } catch { return false }
}
