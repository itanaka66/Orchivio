import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { Feedback, Field, Json, Loading, Pager, Panel, Pill, useAction, useApi, when, type Page } from './kit'

interface Schedule { flow: string; cron: string; enabled: boolean; run_as_role: string; next_run_at: string | null; last_run: { status: string } | null }
interface Job { id: string; flow: string; trigger: string; status: string; attempt: number; max_attempts: number; run_as_role: string | null; created_at: string; finished_at: string | null; error: string | null; result: unknown }
const LIMIT = 20
const STATUSES = ['', 'queued', 'running', 'succeeded', 'dead', 'cancelled']

export function Jobs({ client, appId, roles }: { client: ApiClient; appId: string; roles: string[] }) {
  const [status, setStatus] = useState('')
  const [offset, setOffset] = useState(0)
  const schedules = useApi(() => client.request<{ items: Schedule[] }>('GET', `/applications/${appId}/schedules`), [client, appId])
  const jobs = useApi(() => client.request<Page<Job>>('GET', `/applications/${appId}/jobs?limit=${LIMIT}&offset=${offset}${status ? `&status=${status}` : ''}`), [client, appId, status, offset])
  const { run, error, notice } = useAction(jobs.reload)
  const [flow, setFlow] = useState('')
  const [role, setRole] = useState(roles[0] ?? '')
  const [open, setOpen] = useState<string | null>(null)

  return (
    <>
      <Panel title="Schedules">
        <Loading loading={schedules.loading} error={schedules.error} />
        {schedules.data?.items.length === 0 && <p className="hint">No flow has a schedule.</p>}
        <table className="trace"><tbody>
          {schedules.data?.items.map((s) => (
            <tr key={s.flow}><td>{s.flow}</td><td><code>{s.cron}</code></td><td>{s.enabled ? <Pill value="active" /> : <Pill value="disabled" />}</td>
              <td>as {s.run_as_role}</td><td>next {when(s.next_run_at)}</td><td>{s.last_run ? <Pill value={s.last_run.status} /> : '—'}</td></tr>
          ))}
        </tbody></table>
      </Panel>

      <Panel title="Run a flow now">
        <form className="inline" onSubmit={(e) => { e.preventDefault(); void run(() => client.request('POST', `/applications/${appId}/jobs`, { flow: flow.trim(), run_as_role: role || undefined }), 'Job queued.') }}>
          <Field label="Server flow"><input value={flow} onChange={(e) => setFlow(e.target.value)} placeholder="core.ReorderLowStock" /></Field>
          <Field label="Run as role"><select value={role} onChange={(e) => setRole(e.target.value)}>{roles.map((r) => <option key={r} value={r}>{r}</option>)}</select></Field>
          <button type="submit" className="primary" disabled={!flow.trim()}>Queue</button>
        </form>
      </Panel>

      <Panel title="Jobs" actions={
        <label>Status <select aria-label="Filter by status" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0) }}>
          {STATUSES.map((s) => <option key={s} value={s}>{s || 'any'}</option>)}</select></label>}>
        <Loading loading={jobs.loading} error={jobs.error} />
        <Feedback error={error} notice={notice} />
        {jobs.data?.items.length === 0 && <p className="hint">No jobs.</p>}
        <ul className="list">
          {jobs.data?.items.map((j) => (
            <li key={j.id} className="card slim">
              <div className="row"><strong>{j.flow}</strong> <Pill value={j.status} /> <small className="hint">{j.trigger} · attempt {j.attempt}/{j.max_attempts} · {when(j.created_at)}</small>
                <button type="button" className="link" onClick={() => setOpen(open === j.id ? null : j.id)}>{open === j.id ? 'Hide' : 'Details'}</button>
                {(j.status === 'dead' || j.status === 'cancelled') && <button type="button" onClick={() => void run(() => client.request('POST', `/jobs/${j.id}/retry`), 'Job queued again.')}>Retry</button>}
                {(j.status === 'queued' || j.status === 'running') && <button type="button" onClick={() => void run(() => client.request('POST', `/jobs/${j.id}/cancel`), 'Job cancelled.')}>Cancel</button>}
              </div>
              {j.error && <p className="sev-error">{j.error}</p>}
              {open === j.id && <Json value={{ run_as_role: j.run_as_role, finished_at: j.finished_at, result: j.result }} />}
            </li>
          ))}
        </ul>
        <Pager page={jobs.data} offset={offset} limit={LIMIT} onOffset={setOffset} />
      </Panel>
    </>
  )
}
