// A small promise wrapper over IndexedDB for the offline vault (docs/OFFLINE.md). Three stores: `meta` (key/value), `outbox` (queued requests,
// auto-increment id) and `snapshots` (key/value, encrypted by the caller).

export type StoreName = 'meta' | 'outbox' | 'snapshots'

export interface Db {
  get<T>(store: StoreName, key: IDBValidKey): Promise<T | undefined>
  put(store: StoreName, value: unknown, key?: IDBValidKey): Promise<IDBValidKey>
  del(store: StoreName, key: IDBValidKey): Promise<void>
  all<T>(store: StoreName): Promise<T[]>
  count(store: StoreName): Promise<number>
  clear(store?: StoreName): Promise<void>
  close(): void
}

const wrap = <T>(req: IDBRequest<T>): Promise<T> => new Promise((resolve, reject) => { req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error) })

export const supported = (): boolean => typeof indexedDB !== 'undefined' && typeof crypto !== 'undefined' && !!crypto.subtle

export function openDb(name = 'orchivio-offline', factory: IDBFactory = indexedDB): Promise<Db> {
  return new Promise((resolve, reject) => {
    const req = factory.open(name, 1)
    req.onupgradeneeded = () => {
      const db = req.result
      db.createObjectStore('meta')
      db.createObjectStore('outbox', { keyPath: 'id', autoIncrement: true })
      db.createObjectStore('snapshots')
    }
    req.onerror = () => reject(req.error)
    req.onsuccess = () => {
      const db = req.result
      const tx = (store: StoreName, mode: IDBTransactionMode) => db.transaction(store, mode).objectStore(store)
      resolve({
        get: (store, key) => wrap(tx(store, 'readonly').get(key)),
        put: (store, value, key) => wrap(tx(store, 'readwrite').put(value, key)),
        del: (store, key) => wrap(tx(store, 'readwrite').delete(key)).then(() => undefined),
        all: (store) => wrap(tx(store, 'readonly').getAll()),
        count: (store) => wrap(tx(store, 'readonly').count()),
        clear: async (store) => { for (const s of store ? [store] : (['meta', 'outbox', 'snapshots'] as StoreName[])) await wrap(tx(s, 'readwrite').clear()) },
        close: () => db.close(),
      })
    }
  })
}
