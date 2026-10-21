import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { Feedback, Loading, Pager, Panel, Pill, useApi } from './kit'
import type { Page } from './kit'

interface AuditEvent {
  id: number
  created_at: string
  actor_id: string | null
  plane: string
  channel: string
  action: string
  target_type: string | null
  target_id: string | null
  meta: Record<string, unknown>
}

const PAGE = 50

/** Audit log viewer for an organization (FR-043). */
export function AuditLog({ client, orgId }: { client: ApiClient; orgId: string }) {
  const [offset, setOffset] = useState(0)
  const [applied, setApplied] = useState('')
  const [input, setInput] = useState('')

  const events = useApi(
    () =>
      client.request<Page<AuditEvent>>(
        'GET',
        `/organizations/${orgId}/audit-events?limit=${PAGE}&offset=${offset}${applied ? `&action_prefix=${encodeURIComponent(applied)}` : ''}`,
      ),
    [client, orgId, offset, applied],
  )

  const apply = () => {
    setOffset(0)
    setApplied(input.trim())
  }

  const clear = () => {
    setInput('')
    setApplied('')
    setOffset(0)
  }

  return (
    <Panel title="Audit log">
      <p className="hint">
        A record of all actions taken across this organization — Studio logins, model changes, process events, and
        application-plane auth.
      </p>
      <div className="toolbar">
        <label>
          Filter by action{' '}
          <input
            aria-label="Action prefix"
            value={input}
            placeholder="auth.login"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && apply()}
          />
        </label>
        <button type="button" onClick={apply}>
          Apply
        </button>
        {applied && (
          <button type="button" onClick={clear}>
            Clear
          </button>
        )}
        <button type="button" onClick={events.reload}>
          Refresh
        </button>
      </div>
      <Feedback error="" notice="" />
      <Loading loading={events.loading} error={events.error} />
      {!events.loading && events.data?.items.length === 0 && (
        <p className="hint">No audit events found{applied ? ` matching "${applied}"` : ''}.</p>
      )}
      {(events.data?.items.length ?? 0) > 0 && (
        <table className="trace">
          <thead>
            <tr>
              <th>When</th>
              <th>Action</th>
              <th>Plane</th>
              <th>Actor</th>
              <th>Target</th>
            </tr>
          </thead>
          <tbody>
            {events.data?.items.map((e) => (
              <tr key={e.id}>
                <td>
                  <small>{new Date(e.created_at).toLocaleString()}</small>
                </td>
                <td>
                  <code>{e.action}</code>
                </td>
                <td>
                  <Pill value={e.plane} />
                </td>
                <td>
                  <small className="hint">{e.actor_id ? e.actor_id.slice(0, 8) : 'system'}</small>
                </td>
                <td>
                  <small className="hint">
                    {e.target_type}
                    {e.target_id ? ` ${e.target_id.slice(0, 8)}` : ''}
                  </small>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Pager page={events.data} offset={offset} limit={PAGE} onOffset={setOffset} />
    </Panel>
  )
}
