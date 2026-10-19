/* Orchivio Studio service worker.
 *
 * It makes the Studio installable and lets the app shell open without a network. It deliberately caches NOTHING that comes from the
 * API: no request to /api/, /healthz, /readyz or /metrics is ever answered from (or stored in) a cache, so no application data,
 * token or session detail is left on the device and signing out leaves nothing behind. Only the shell (HTML, hashed build assets, icons,
 * manifest, offline page) is cached.
 *
 * __BUILD_ID__ is replaced at build time, so every release changes this file and browsers notice the update; a waiting worker takes over
 * only when the page asks (message {type: 'SKIP_WAITING'}), so a reload is never forced in the middle of editing a flow.
 */
const BUILD = '__BUILD_ID__'
const PREFIX = 'orchivio-shell-'
const SHELL = PREFIX + BUILD
const PRECACHE = ['/', '/offline.html', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/icon-512.png']
const NEVER = ['/api/', '/healthz', '/readyz', '/metrics']

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(PRECACHE)))
})

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key.startsWith(PREFIX) && key !== SHELL) await caches.delete(key)
    await self.clients.claim()
  })())
})

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting()
  if (event.data && event.data.type === 'CLEAR') event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith(PREFIX)).map((k) => caches.delete(k)))))
})

// Background Sync (Chromium): when the browser reports the connection is back, tell open Studio windows to send what is waiting. The worker itself cannot send
// anything: the queue is encrypted with a passphrase it never sees (and holds no sign-in), so with no Studio window open the changes wait until one is.
self.addEventListener('sync', (event) => {
  if (event.tag !== 'orchivio-outbox') return
  event.waitUntil(self.clients.matchAll({ type: 'window' }).then((all) => all.forEach((c) => c.postMessage({ type: 'OUTBOX_SYNC' }))))
})

const cacheable = (response) => response && response.status === 200 && response.type === 'basic' && !/no-store/i.test(response.headers.get('Cache-Control') || '')

self.addEventListener('fetch', (event) => {
  const request = event.request
  if (request.method !== 'GET') return
  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return
  if (NEVER.some((p) => url.pathname.startsWith(p))) return

  if (request.mode === 'navigate') {
    // pages: the network first (so a new release shows up), the cached shell when there is none
    event.respondWith((async () => {
      try {
        const fresh = await fetch(request)
        if (cacheable(fresh) && url.pathname === '/') (await caches.open(SHELL)).put('/', fresh.clone())
        return fresh
      } catch {
        return (await caches.match('/')) || (await caches.match('/offline.html')) || Response.error()
      }
    })())
    return
  }

  if (url.pathname.startsWith('/assets/') || url.pathname.startsWith('/icons/') || PRECACHE.includes(url.pathname)) {
    // build output is content-hashed, icons and the manifest are static: cache first
    event.respondWith((async () => {
      const hit = await caches.match(request)
      if (hit) return hit
      const fresh = await fetch(request)
      if (cacheable(fresh)) (await caches.open(SHELL)).put(request, fresh.clone())
      return fresh
    })())
  }
})
