import { useState } from 'react'
import { Feedback, Loading, Pager, Panel, Pill, useAction, useApi } from '../admin/kit'
import type { ApiClient, Application } from '../api/client'

interface Props { client: ApiClient; application: Application; canEdit: boolean }

export function DiffReview({ client, application, canEdit }: Props) {
  const [offset, setOffset] = useState(0)
  const limit = 20
  const list = useApi(() => client.proposals(application.id, limit, offset), [application.id, offset, limit])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const detail = useApi(
    () => selectedId ? client.proposalDetail(application.id, selectedId) : Promise.resolve(null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [application.id, selectedId],
  )
  const { run, error: actError, notice: actNotice, busy } = useAction(list.reload)
  const [acknowledge, setAcknowledge] = useState(false)

  const selected = detail.data

  const approve = () => {
    const id = selectedId
    if (!id) return
    void run(() => client.approveProposal(application.id, id, acknowledge), 'Proposal approved.')
  }

  const reject = () => {
    const id = selectedId
    if (!id) return
    void run(() => client.rejectProposal(application.id, id), 'Proposal rejected.')
  }

  return (
    <div className="admin">
      <div>
        <Panel title="Proposals">
          <Loading loading={list.loading} error={list.error} />
          <ul className="list">
            {(list.data?.items ?? []).map((p) => (
              <li key={p.id}>
                <button type="button" className="link" aria-current={selectedId === p.id ? true : undefined}
                  onClick={() => { setSelectedId(p.id); setAcknowledge(false) }}>
                  <Pill value={p.status} />{' '}
                  {p.approval_level} · {p.diff.changes.length} change{p.diff.changes.length === 1 ? '' : 's'}
                </button>
              </li>
            ))}
          </ul>
          <Pager page={list.data ?? null} offset={offset} limit={limit} onOffset={setOffset} />
        </Panel>
      </div>

      <div>
        {!selectedId && <p className="hint">Select a proposal to review it.</p>}
        {selectedId && (
          <>
            <Loading loading={detail.loading} error={detail.error} />
            {selected && (
              <Panel title={`Proposal · ${selected.status}`} actions={<Pill value={selected.approval_level} />}>
                <Feedback error={actError} notice={actNotice} />

                {selected.diff.changes.length > 0 && (
                  <table className="trace">
                    <thead><tr><th>Kind</th><th>Path</th><th>Summary</th></tr></thead>
                    <tbody>
                      {selected.diff.changes.map((c, i) => (
                        <tr key={i} className={c.kind === 'add' ? 'change-add' : c.kind === 'remove' ? 'change-remove' : 'change-modify'}>
                          <td><span className="badge">{c.kind}</span></td>
                          <td><code>{c.path}</code></td>
                          <td>{c.summary}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}

                {selected.lint.length > 0 && (
                  <section>
                    <h3>Lint findings ({selected.lint.length})</h3>
                    <ul className="issues-inline">
                      {selected.lint.map((f, i) => (
                        <li key={i}>
                          <span className={f.severity === 'error' ? 'sev-error' : 'sev-warning'}>{f.severity}</span>{' '}
                          <code>{f.rule}</code> — {f.message}
                        </li>
                      ))}
                    </ul>
                  </section>
                )}

                {selected.warnings.length > 0 && (
                  <ul className="issues-inline">
                    {selected.warnings.map((w, i) => <li key={i} className="sev-warning">{w}</li>)}
                  </ul>
                )}

                {canEdit && selected.status === 'pending_approval' && (
                  <div className="toolbar">
                    {selected.approval_level === 'A3' && (
                      <label className="field check">
                        <input type="checkbox" checked={acknowledge} onChange={(e) => setAcknowledge(e.target.checked)} />
                        <span>I acknowledge the security and structural risks</span>
                      </label>
                    )}
                    <button type="button" className="primary"
                      disabled={busy || (selected.approval_level === 'A3' && !acknowledge)}
                      onClick={approve}>
                      Approve
                    </button>
                    <button type="button" disabled={busy} onClick={reject}>Reject</button>
                  </div>
                )}
              </Panel>
            )}
          </>
        )}
      </div>
    </div>
  )
}
