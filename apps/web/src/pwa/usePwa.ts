import { useCallback, useEffect, useState } from 'react'
import { applyUpdate, onPwaState } from './register'
import { PROBE_MS, useOnline } from './useOnline'

export { PROBE_MS }

/** The browser's install prompt event, which TypeScript's DOM types do not include. */
export interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

/** How often an offline Studio checks whether the server is reachable again. */
export const PROBE_MS = 4000

export function usePwa() {
  const [online, setOnline] = useState(typeof navigator === 'undefined' ? true : navigator.onLine)
  const [update, setUpdate] = useState(false)
  const [installEvent, setInstallEvent] = useState<BeforeInstallPromptEvent | null>(null)
  const [installed, setInstalled] = useState(false)

  useEffect(() => {
    const on = () => setOnline(true)
    const off = () => setOnline(false)
    const before = (e: Event) => { e.preventDefault(); setInstallEvent(e as BeforeInstallPromptEvent) }
    const done = () => { setInstalled(true); setInstallEvent(null) }
    window.addEventListener('online', on)
    window.addEventListener('offline', off)
    window.addEventListener('beforeinstallprompt', before)
    window.addEventListener('appinstalled', done)
    const stop = onPwaState((s) => setUpdate(s.update))
    return () => {
      window.removeEventListener('online', on)
      window.removeEventListener('offline', off)
      window.removeEventListener('beforeinstallprompt', before)
      window.removeEventListener('appinstalled', done)
      stop()
    }
  }, [])

  // `online`/`offline` events are not reliable everywhere (some browsers and emulations never send `online`): while offline, ask the server now and then.
  useEffect(() => {
    if (online) return
    const timer = setInterval(() => {
      fetch('/healthz', { cache: 'no-store' }).then((r) => { if (r.ok) setOnline(true) }, () => undefined)
    }, PROBE_MS)
    return () => clearInterval(timer)
  }, [online])

  const install = useCallback(async () => {
    if (!installEvent) return
    await installEvent.prompt()
    const choice = await installEvent.userChoice
    setInstallEvent(null)
    if (choice.outcome === 'accepted') setInstalled(true)
  }, [installEvent])

  return { online, update, applyUpdate, canInstall: installEvent !== null && !installed, install }
}
