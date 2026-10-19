/* Service worker of a generated application (docs/PWA.md). Scope: this application's shell directory only.
 * It caches the shell (page, script, styles, icons, manifest, offline page) and nothing else: the application's data and its sign-in live at
 * ../pages, ../data and ../auth, outside this scope, so no row, token or session detail can end up in a cache. __BUILD__ is replaced per release. */
const BUILD = '__BUILD__'
const PREFIX = 'orchivio-app-shell-'
const SHELL = PREFIX + BUILD
const FILES = ['./', 'app.js?v=' + BUILD, 'app.css?v=' + BUILD, 'manifest.webmanifest', 'offline.html', 'icon-192.png', 'icon-512.png']

self.addEventListener('install', (event) => { event.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES.map((f) => new Request(f, { cache: 'reload' }))))) })
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key.startsWith(PREFIX) && key !== SHELL) await caches.delete(key)
    await self.clients.claim()
  })())
})
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting()
  if (event.data && event.data.type === 'CLEAR') event.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k.startsWith(PREFIX)).map((k) => caches.delete(k)))))
})
self.addEventListener('fetch', (event) => {
  const request = event.request
  const url = new URL(request.url)
  if (request.method !== 'GET' || url.origin !== self.location.origin) return
  const scope = new URL(self.registration.scope).pathname
  if (!url.pathname.startsWith(scope)) return
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(async () => (await caches.match('./', { ignoreSearch: true })) || caches.match('offline.html')))
    return
  }
  event.respondWith(caches.match(request).then((hit) => hit || fetch(request)))
})
