import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError } from '../api/client'

export const errMsg = (e: unknown): string => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : 'Something went wrong.')

export const when = (iso?: string | null): string => (iso ? new Date(iso).toLocaleString() : '—')

/** Load data once and again on `reload()`; a failed load keeps the message instead of throwing. */
export function useApi<T>(load: () => Promise<T>, deps: unknown[]): { data: T | null; error: string; loading: boolean; reload: () => void } {
  const [state, setState] = useState<{ data: T | null; error: string; loading: boolean }>({ data: null, error: '', loading: true })
  const [tick, setTick] = useState(0)
  const loader = useRef(load)
  loader.current = load
  useEffect(() => {
    let live = true
    setState((s) => ({ ...s, loading: true }))
    loader.current().then(
      (data) => live && setState({ data, error: '', loading: false }),
      (e) => live && setState({ data: null, error: errMsg(e), loading: false }),
    )
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])
  return { ...state, reload: useCallback(() => setTick((t) => t + 1), []) }
}

/** Runs actions, shows the server's message when one fails and reloads when it works. */
export function useAction(reload: () => void) {
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const run = useCallback(async (work: () => Promise<unknown>, done?: string) => {
    setBusy(true)
    try { await work(); setError(''); setNotice(done ?? ''); reload() } catch (e) { setError(errMsg(e)); setNotice('') } finally { setBusy(false) }
  }, [reload])
  return { run, error, notice, busy, setError, setNotice }
}

export function Feedback({ error, notice }: { error?: string; notice?: string }) {
  return (
    <>
      {error && <p role="alert" className="sev-error">{error}</p>}
      {notice && <p role="status" className="hint">{notice}</p>}
    </>
  )
}

export function Panel({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card" aria-label={title}>
      <div className="panel-head"><h2>{title}</h2>{actions}</div>
      {children}
    </section>
  )
}

export function Loading({ loading, error }: { loading: boolean; error: string }) {
  if (error) return <p role="alert" className="sev-error">{error}</p>
  return loading ? <p>Loading…</p> : null
}

/** A destructive button that needs a second click ("Sure?") before it does anything. */
export function Danger({ label, confirm = 'Sure?', onConfirm, disabled }: { label: string; confirm?: string; onConfirm: () => void; disabled?: boolean }) {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return
    const t = setTimeout(() => setArmed(false), 4000)
    return () => clearTimeout(t)
  }, [armed])
  return armed
    ? <button type="button" className="danger" onClick={() => { setArmed(false); onConfirm() }}>{confirm}</button>
    : <button type="button" className="link danger-link" disabled={disabled} onClick={() => setArmed(true)}>{label}</button>
}

export function Pill({ value }: { value: string }) {
  const tone = /^(succeeded|delivered|active|deployed|completed|compliant|approved|ok)$/.test(value) ? 'ok'
    : /^(dead|failed|rejected|violations|error|disabled)$/.test(value) ? 'bad' : /^(queued|running|pending|pending_approval|open|paused|warnings|sending)$/.test(value) ? 'warn' : 'plain'
  return <span className={`pill pill-${tone}`}>{value}</span>
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <label className="field"><span>{label}</span>{children}{hint && <small className="hint">{hint}</small>}</label>
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>
}

export interface Page<T> { items: T[]; total: number; limit: number; offset: number }

/** Previous / next controls for paged lists. */
export function Pager({ page, offset, limit, onOffset }: { page: Page<unknown> | null; offset: number; limit: number; onOffset: (n: number) => void }) {
  if (!page || page.total <= limit) return null
  return (
    <div className="toolbar" aria-label="Pages">
      <button type="button" disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - limit))}>Previous</button>
      <span className="hint">{offset + 1}–{Math.min(offset + limit, page.total)} of {page.total}</span>
      <button type="button" disabled={offset + limit >= page.total} onClick={() => onOffset(offset + limit)}>Next</button>
    </div>
  )
}
