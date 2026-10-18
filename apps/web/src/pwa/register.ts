// Registers the service worker (public/sw.js) and reports when a new version is waiting. Production builds only: a worker in the dev
// server would serve stale files.

export interface PwaState { update: boolean }
type Listener = (s: PwaState) => void

export interface RegisterOptions {
  enabled?: boolean
  container?: Pick<ServiceWorkerContainer, 'register' | 'addEventListener' | 'controller'> | undefined
}

let waiting: ServiceWorker | null = null
const listeners = new Set<Listener>()
const emit = () => listeners.forEach((l) => l({ update: waiting !== null }))

export function onPwaState(l: Listener): () => void {
  listeners.add(l)
  l({ update: waiting !== null })
  return () => { listeners.delete(l) }
}

/** Ask the waiting worker to take over; the page reloads once it has (see `controllerchange`). */
export function applyUpdate(): void {
  waiting?.postMessage({ type: 'SKIP_WAITING' })
}

/** Forget everything the worker cached (used when signing out; the worker never caches API data, this clears the shell too). */
export function clearShellCaches(container: Pick<ServiceWorkerContainer, 'controller'> | undefined = typeof navigator === 'undefined' ? undefined : navigator.serviceWorker): void {
  container?.controller?.postMessage({ type: 'CLEAR' })
}

export async function registerServiceWorker(opts: RegisterOptions = {}): Promise<boolean> {
  const container = 'container' in opts ? opts.container : typeof navigator === 'undefined' ? undefined : navigator.serviceWorker
  const enabled = opts.enabled ?? import.meta.env.PROD
  if (!enabled || !container) return false
  const reg = await container.register('/sw.js', { scope: '/', updateViaCache: 'none' })
  const track = (worker: ServiceWorker | null) => {
    if (!worker) return
    worker.addEventListener('statechange', () => {
      // an installed worker with an existing controller is an update; the first install is not
      if (worker.state === 'installed' && container.controller) { waiting = worker; emit() }
    })
  }
  if (reg.waiting && container.controller) { waiting = reg.waiting; emit() }
  track(reg.installing)
  reg.addEventListener('updatefound', () => track(reg.installing))
  let reloaded = false
  container.addEventListener('controllerchange', () => {
    if (reloaded || !waiting) return
    reloaded = true
    location.reload()
  })
  return true
}

/** Test helper: forget a pending update. */
export function resetPwaState(): void {
  waiting = null
  listeners.clear()
}
