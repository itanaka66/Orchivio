import { useEffect, useState } from 'react'

/** How often an offline Studio checks whether the server is reachable again. */
export const PROBE_MS = 4000

/**
 * Whether the server can be reached. ``online``/``offline`` events are not reliable everywhere (some browsers and emulations never send ``online``), so while
 * offline the server is asked now and then (``/healthz`` is never handled by the service worker).
 */
export function useOnline(): boolean {
  const [online, setOnline] = useState(typeof navigator === 'undefined' ? true : navigator.onLine)
  useEffect(() => {
    const on = () => setOnline(true)
    const off = () => setOnline(false)
    window.addEventListener('online', on)
    window.addEventListener('offline', off)
    return () => { window.removeEventListener('online', on); window.removeEventListener('offline', off) }
  }, [])
  useEffect(() => {
    if (online) return
    const timer = setInterval(() => { fetch('/healthz', { cache: 'no-store' }).then((r) => { if (r.ok) setOnline(true) }, () => undefined) }, PROBE_MS)
    return () => clearInterval(timer)
  }, [online])
  return online
}
