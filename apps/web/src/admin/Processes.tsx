import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { Feedback, Field, Json, Loading, Pager, Panel, Pill, useAction, useApi, when, type Page } from './kit'

interface Instance { id: string; process: string; status: string; initiator: string; error: string | null; started_at: string; finished_at: string | null; next_wake_at: string | null }
interface Task { id: string; instance_id: string; title: string; status: string; assignee_role: string | null; assignee_user: string | null; due_at: string | null; overdue: boolean; escalated_at: string | null; outcome: string | null }
interface Detail extends Instance { variables: unknown; tokens: unknown; tasks: Task[]; events: { at: string; type: string; step_id: string | null; actor: string | null }[] }
const LIMIT = 15

export function Processes({ client, appId, processes }: { client: ApiClient; appId: string; processes: string[] }) {
  const [status, setStatus] = useState('')
  const [offset, setOffset] = useState(0)
  const [tab, setTab] = useState<'instances' | 'tasks'>('instances')
  const instances = useApi(() => client.request<Page<Instance>>('GET', `/applications/${appId}/process-instances?limit=${LIMIT}&offset=${offset}${status ? `&status=${status}` : ''}`), [client, appId, status, offset])
  const [taskStatus, setTaskStatus] = useState('open')
  const tasks = useApi(() => client.request<Page<Task>>('GET', `/applications/${appId}/user-tasks?limit=${LIMIT}&status=${taskStatus}`), [client, appId, taskStatus])
  const { run, error, notice } = useAction(() => { instances.reload(); tasks.reload() })
  const [start, setStart] = useState({ process: processes[0] ?? '', args: '{}' })
  const [argsError, setArgsError] = useState('')
  const [detail, setDetail] = useState<Detail | null>(null)

  const open = async (id: string) => setDetail(await client.request<Detail>('GET', `/process-instances/${id}`))

  return (
    <>
      <Panel title="Processes" actions={
        <div className="tabs" role="tablist" aria-label="Process views">
          {(['instances', 'tasks'] as const).map((t) => <button key={t} type="button" role="tab" aria-selected={tab === t} className={tab === t ? 'primary' : ''} onClick={() => setTab(t)}>{t === 'instances' ? 'Instances' : 'All tasks'}</button>)}
        </div>}>
        <Feedback error={error} notice={notice} />
        {tab === 'instances' ? (
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
        ) : (
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
      </Panel>

      {detail && (
        <Panel title={`Instance ${detail.process}`} actions={<button type="button" onClick={() => setDetail(null)}>Close</button>}>
          <h3>Tasks</h3>
          <ul className="list">{detail.tasks.map((t) => <li key={t.id}>{t.title} <Pill value={t.status} /> {t.outcome ? `→ ${t.outcome}` : ''}</li>)}</ul>
          <h3>Events</h3>
          <ol>{detail.events.map((e, i) => <li key={i}><small className="hint">{when(e.at)}</small> {e.type}{e.step_id ? ` · ${e.step_id}` : ''}{e.actor ? ` · ${e.actor}` : ''}</li>)}</ol>
          <h3>Variables</h3>
          <Json value={detail.variables} />
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
          <Field label="Process"><select value={start.process} onChange={(e) => setStart({ ...start, process: e.target.value })}>{processes.map((p) => <option key={p} value={p}>{p}</option>)}</select></Field>
          <Field label="Arguments (JSON)"><input value={start.args} onChange={(e) => setStart({ ...start, args: e.target.value })} /></Field>
          <button type="submit" disabled={!start.process}>Start</button>
        </form>
        {argsError && <p role="alert" className="sev-error">{argsError}</p>}
      </Panel>
    </>
  )
}
