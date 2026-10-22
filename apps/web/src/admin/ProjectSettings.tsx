import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { Feedback, Loading, Panel, useAction, useApi } from './kit'

interface Settings { quota?: { ai_jobs_per_month?: number }; policy_exceptions?: string[] }
interface Pack { id: string; name: string; status: string }

export function ProjectSettings({ client, projectId, orgId }: { client: ApiClient; projectId: string; orgId: string }) {
  const proj = useApi(() => client.request<{ settings: Settings | null }>('GET', `/projects/${projectId}`), [client, projectId])
  const packs = useApi(() => client.request<{ items: Pack[] }>('GET', `/organizations/${orgId}/policy-packs`), [client, orgId])
  const { run, error, notice } = useAction(proj.reload)

  const settings: Settings = proj.data?.settings ?? {}
  const quota = settings.quota ?? {}
  const exceptions: string[] = settings.policy_exceptions ?? []

  const [quotaVal, setQuotaVal] = useState<string>('')
  const [editingQuota, setEditingQuota] = useState(false)

  const startEditQuota = () => {
    setQuotaVal(quota.ai_jobs_per_month != null ? String(quota.ai_jobs_per_month) : '')
    setEditingQuota(true)
  }

  const saveQuota = () => void run(async () => {
    const n = quotaVal.trim() === '' ? null : parseInt(quotaVal, 10)
    if (quotaVal.trim() !== '' && (isNaN(n!) || n! < 0 || n! > 100_000)) throw new Error('Enter a number between 0 and 100 000, or leave blank to remove the limit.')
    await client.request('PATCH', `/projects/${projectId}/settings`, { quota: { ai_jobs_per_month: n } })
    setEditingQuota(false)
  }, 'AI quota updated.')

  const addException = (packId: string) => void run(() => client.request('POST', `/projects/${projectId}/policy-exceptions/${encodeURIComponent(packId)}`), 'Exception added.')
  const removeException = (packId: string) => void run(() => client.request('DELETE', `/projects/${projectId}/policy-exceptions/${encodeURIComponent(packId)}`), 'Exception removed.')

  const activePacks = (packs.data?.items ?? []).filter((p) => p.status === 'active')

  return (
    <>
      <Panel title="AI quota">
        <p className="hint">Limit how many AI Builder jobs this project may start per calendar month. Requests over the quota are refused until the next month.</p>
        <Loading loading={proj.loading} error={proj.error} />
        <Feedback error={error} notice={notice} />
        {!editingQuota ? (
          <div className="row">
            <span>{quota.ai_jobs_per_month != null ? `${quota.ai_jobs_per_month} jobs / month` : 'No limit set'}</span>
            <button type="button" onClick={startEditQuota}>Edit</button>
          </div>
        ) : (
          <div className="toolbar">
            <label>AI jobs per month (blank = no limit)
              <input type="number" min={0} max={100000} value={quotaVal} onChange={(e) => setQuotaVal(e.target.value)} placeholder="e.g. 500" style={{ width: '10rem', marginLeft: '0.5rem' }} />
            </label>
            <button type="button" className="primary" onClick={saveQuota}>Save</button>
            <button type="button" onClick={() => setEditingQuota(false)}>Cancel</button>
          </div>
        )}
      </Panel>

      <Panel title="Policy-pack exceptions">
        <p className="hint">Packs listed here are not enforced on model changes in this project. Each exception requires a governance justification in your change management process.</p>
        <Loading loading={packs.loading} error={packs.error} />
        {activePacks.length === 0 && !packs.loading && <p className="hint">No active policy packs in this organization.</p>}
        <table className="trace">
          <thead>
            <tr><th>Pack</th><th>Status in this project</th><th /></tr>
          </thead>
          <tbody>
            {activePacks.map((p) => {
              const exempt = exceptions.includes(p.id) || exceptions.includes(p.name)
              return (
                <tr key={p.id}>
                  <td>{p.name}</td>
                  <td>{exempt ? <span className="chip sev-warning">excepted</span> : <span className="chip">enforced</span>}</td>
                  <td>
                    {exempt
                      ? <button type="button" className="link" onClick={() => removeException(p.id)}>Restore enforcement</button>
                      : <button type="button" className="link" onClick={() => addException(p.id)}>Add exception</button>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </Panel>
    </>
  )
}
