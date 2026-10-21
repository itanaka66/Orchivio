import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { Feedback, Json, Loading, Pager, Panel, Pill, useAction, useApi, when, type Page } from './kit'

interface Instance { id: string; process: string; status: string; initiator: string; error: string | null; started_at: string; finished_at: string | null; next_wake_at: string | null; revision_id: string }
interface Task { id: string; instance_id: string; title: string; status: string; assignee_role: string | null; assignee_user: string | null; due_at: string | null; overdue: boolean; escalated_at: string | null; outcome: string | null }
interface Detail extends Instance { variables: unknown; tokens: unknown; tasks: Task[]; events: { at: string; type: string; step_id: string | null; actor: string | null }[] }
interface Analytics {
  throughput: { process: string; running: number; completed: number; failed: number; cancelled: number; avg_seconds: number | null }[]
  bottlenecks: { step_id: string; open_tasks: number; overdue: number }[]
}
interface ProcessVersion { revision_id: string; revision_no: number; created_at: string; processes: string[] }
const LIMIT = 15

function fmtSeconds(s: number | null): string {
  if (s === null) return '—'
  if (s < 120) return `${Math.round(s)}s`
  if (s < 7200) return `${Math.round(s / 60)}m`
  return `${Math.round(s / 3600)}h`
}

export function Processes({ client, appId, processes }: { client: ApiClient; appId: string; processes: string[] }) {
  const [status, setStatus] = useState('')
  const [offset, setOffset] = useState(0)
  const [tab, setTab] = useState<'instances' | 'tasks' | 'analytics'>('instances')
  const instances = useApi(() => client.request<Page<Instance>>('GET', `/applications/${appId}/process-instances?limit=${LIMIT}&offset=${offset}${status ? `&status=${status}` : ''}`), [client, appId, status, offset])
  const [taskStatus, setTaskStatus] = useState('open')
  const tasks = useApi(() => client.request<Page<Task>>('GET', `/applications/${appId}/user-tasks?limit=${LIMIT}&status=${taskStatus}`), [client, appId, taskStatus])
  const analytics = useApi<Analytics | null>(
    () => tab === 'analytics' ? client.request<Analytics>('GET', `/applications/${appId}/process-analytics`) : Promise.resolve(null),
    [client, appId, tab]
  )
  const versions = useApi<{ revisions: ProcessVersion[] } | null>(
    () => tab === 'analytics' ? client.request<{ revisions: ProcessVersion[] }>('GET', `/applications/${appId}/process-versions`) : Promise.resolve(null),
    [client, appId, tab]
  )
  const { run, error, notice } = useAction(() => { instances.reload(); tasks.reload(); if (tab === 'analytics') { analytics.reload(); versions.reload() } })
  const [start, setStart] = useState({ process: processes[0] ?? '', args: '{}' })
  const [argsError, setArgsError] = useState('')
  const [detail, setDetail] = useState<Detail | null>(null)
  const [signalMessage, setSignalMessage] = useState('')

  const open = async (id: string) => setDetail(await client.request<Detail>('GET', `/process-instances/${id}`))

  const sendSignal = (instanceId: string) => {
    if (!signalMessage.trim()) return
    void run(() => client.request('POST', `/process-instances/${instanceId}/signal`, { message: signalMessage.trim(), data: {} }), 'Signal delivered.')
  }

  return (
    <>
      <Panel title="Processes" actions={
        <div className="tabs" role="tablist" aria-label="Process views">
          {(['instances', 'tasks', 'analytics'] as const).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} className={tab === t ? 'primary' : ''} onClick={() => setTab(t)}>
              {t === 'instances' ? 'Instances' : t === 'tasks' ? 'All tasks' : 'Analytics'}
            </button>
          ))}
        </div>}>
        <Feedback error={error} notice={notice} />
        {tab === 'instances' && (
          <>
            <label>Status <select aria-label="Instance status" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0) }}>
              {['', 'running', 'completed', 'failed', 'cancelled'].map((s) => <option key={s} value={s}>{s || 'any'}</option>)}</select></label>
            <Loading loading={instances.loading} error={instances.error} />
            {instances.data?.items.length === 0 && <p className="hint">No instances.</p>}
            <ul className="list">
              {instances.data?.items.map((i) => (
                <li key={i.id} className="card slim">
                  <div className="row"><strong>{i.process}</strong> <Pill value={i.status} /> <small className="hint">by {i.initiator} · {when(i.started_at)}{i.next_wake_at ? ` · wakes ${when(i.next_wake_at)}` : ''}</small></div>
                  {i.error && <p className="sev-error">{i.error}</p>}
                  <div className="row">
                    <button type="button" onClick={() => void open(i.id)}>Details</button>
                    {i.status === 'running' && <button type="button" onClick={() => void run(() => client.request('POST', `/process-instances/${i.id}/cancel`), 'Instance cancelled.')}>Cancel</button>}
                    {i.status === 'failed' && <button type="button" onClick={() => void run(() => client.request('POST', `/process-instances/${i.id}/retry`), 'Retrying the failed step.')}>Retry failed step</button>}
                  </div>
                </li>
              ))}
            </ul>
            <Pager page={instances.data} offset={offset} limit={LIMIT} onOffset={setOffset} />
          </>
        )}
        {tab === 'tasks' && (
          <>
            <label>Status <select aria-label="Task status" value={taskStatus} onChange={(e) => setTaskStatus(e.target.value)}>{['open', 'completed', 'cancelled'].map((s) => <option key={s} value={s}>{s}</option>)}</select></label>
            <Loading loading={tasks.loading} error={tasks.error} />
            <table className="trace"><tbody>
              {tasks.data?.items.map((t) => (
                <tr key={t.id}><td>{t.title}</td><td><Pill value={t.status} /></td><td>{t.assignee_user ?? `role ${t.assignee_role}`}</td>
                  <td>{t.overdue ? <span className="sev-error">overdue {when(t.due_at)}</span> : t.due_at ? `due ${when(t.due_at)}` : ''}{t.escalated_at ? ' · escalated' : ''}</td><td>{t.outcome ?? ''}</td></tr>
              ))}
            </tbody></table>
            {tasks.data?.items.length === 0 && <p className="hint">No tasks.</p>}
          </>
        )}
        {tab === 'analytics' && (
          <>
            <Loading loading={analytics.loading} error={analytics.error} />
            {analytics.data && (
              <>
                <h3>Throughput by process</h3>
                {analytics.data.throughput.length === 0 ? <p className="hint">No instances yet.</p> : (
                  <table className="trace">
                    <thead><tr><th>Process</th><th>Running</th><th>Completed</th><th>Failed</th><th>Avg time</th></tr></thead>
                    <tbody>
                      {analytics.data.throughput.map((p) => (
                        <tr key={p.process}>
                          <td><code>{p.process}</code></td>
                          <td>{p.running || '—'}</td>
                          <td>{p.completed || '—'}</td>
                          <td>{p.failed ? <span className="sev-error">{p.failed}</span> : '—'}</td>
                          <td>{fmtSeconds(p.avg_seconds)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <h3>Task bottlenecks (open)</h3>
                {analytics.data.bottlenecks.length === 0 ? <p className="hint">No open tasks.</p> : (
                  <table className="trace">
                    <thead><tr><th>Step</th><th>Open tasks</th><th>Overdue</th></tr></thead>
                    <tbody>
                      {analytics.data.bottlenecks.map((b) => (
                        <tr key={b.step_id}>
                          <td><code>{b.step_id}</code></td>
                          <td>{b.open_tasks}</td>
                          <td>{b.overdue ? <span className="sev-error">{b.overdue}</span> : '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <h3>Process version history</h3>
                <Loading loading={versions.loading} error={versions.error} />
                {versions.data?.revisions.length === 0 && <p className="hint">No revisions with processes.</p>}
                {versions.data && versions.data.revisions.length > 0 && (
                  <table className="trace">
                    <thead><tr><th>Revision</th><th>Created</th><th>Processes</th></tr></thead>
                    <tbody>
                      {versions.data.revisions.map((r) => (
                        <tr key={r.revision_id}>
                          <td>#{r.revision_no}</td>
                          <td>{when(r.created_at)}</td>
                          <td>{r.processes.join(', ')}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            )}
          </>
        )}
      </Panel>

      {detail && (
        <Panel title={`Instance ${detail.process}`} actions={<button type="button" onClick={() => setDetail(null)}>Close</button>}>
          <p className="hint">Revision #{detail.revision_id.slice(0, 8)}</p>
          <h3>Tasks</h3>
          <ul className="list">{detail.tasks.map((t) => <li key={t.id}>{t.title} <Pill value={t.status} /> {t.outcome ? `→ ${t.outcome}` : ''}</li>)}</ul>
          <h3>Events</h3>
          <ol>{detail.events.map((e, i) => <li key={i}><small className="hint">{when(e.at)}</small> {e.type}{e.step_id ? ` · ${e.step_id}` : ''}{e.actor ? ` · ${e.actor}` : ''}</li>)}</ol>
          <h3>Variables</h3>
          <Json value={detail.variables} />
          {detail.status === 'running' && (
            <>
              <h3>Send message signal</h3>
              <p className="hint">Deliver an external message to a waiting catch_message step.</p>
              <div className="row">
                <input value={signalMessage} onChange={(e) => setSignalMessage(e.target.value)} placeholder="message name" />
                <button type="button" disabled={!signalMessage.trim()} onClick={() => sendSignal(detail.id)}>Send signal</button>
              </div>
            </>
          )}
        </Panel>
      )}

      <Panel title="Start an instance (test)">
        <p className="hint">Starts the process as the Studio account, in the preview database. For checking a process while you build it.</p>
        <form className="inline" onSubmit={(e) => {
          e.preventDefault()
          let args: unknown
          try { args = JSON.parse(start.args || '{}') } catch { setArgsError('Arguments must be valid JSON.'); return }
          if (typeof args !== 'object' || args === null || Array.isArray(args)) { setArgsError('Arguments must be a JSON object.'); return }
          setArgsError('')
          void run(() => client.request('POST', `/applications/${appId}/processes/${encodeURIComponent(start.process)}/start`, { args }), 'Instance started.')
        }}>
          <label className="field">Process<select value={start.process} onChange={(e) => setStart({ ...start, process: e.target.value })}>{processes.map((p) => <option key={p} value={p}>{p}</option>)}</select></label>
          <label className="field">Arguments (JSON)<input value={start.args} onChange={(e) => setStart({ ...start, args: e.target.value })} /></label>
          <button type="submit" disabled={!start.process}>Start</button>
        </form>
        {argsError && <p role="alert" className="sev-error">{argsError}</p>}
      </Panel>
    </>
  )
}
