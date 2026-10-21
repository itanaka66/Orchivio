import { useState } from 'react'
import type { ApiClient, TestRunResult } from '../api/client'
import { Feedback, Loading, Pager, Panel, Pill, useAction, useApi } from './kit'

interface Props { client: ApiClient; appId: string; revisionId: string | null }

export function TestRuns({ client, appId, revisionId }: Props) {
  const [offset, setOffset] = useState(0)
  const limit = 10
  const list = useApi(() => client.testRuns(appId, limit, offset), [appId, offset])
  const [detail, setDetail] = useState<TestRunResult | null>(null)
  const { run, error, notice, busy } = useAction(list.reload)

  const create = () => {
    if (!revisionId) return
    void run(() => client.createTestRun(appId, revisionId), 'Test run started.')
  }

  const repair = (id: string) => {
    void run(() => client.repairTests(appId, id), 'Repair proposal submitted.')
  }

  const openDetail = async (id: string) => {
    const r = await client.testRun(appId, id)
    setDetail(r)
  }

  const summaryLabel = (r: TestRunResult) =>
    `${r.summary.passed}/${r.summary.total} passed${r.summary.failed > 0 ? `, ${r.summary.failed} failed` : ''}${r.summary.skipped > 0 ? `, ${r.summary.skipped} skipped` : ''}`

  return (
    <>
      <Panel title="Test runs" actions={
        revisionId
          ? <button type="button" className="primary" disabled={busy} onClick={create}>Run tests</button>
          : <span className="hint">No revision yet</span>
      }>
        <Feedback error={error} notice={notice} />
        <Loading loading={list.loading} error={list.error} />
        {list.data?.items.length === 0 && <p className="hint">No test runs yet. Click "Run tests" to generate and execute the test suite for the current revision.</p>}
        <ul className="list">
          {list.data?.items.map((r) => (
            <li key={r.id} className="card slim">
              <div className="row">
                <Pill value={r.status} />
                <span>{summaryLabel(r)}</span>
              </div>
              <div className="row">
                <button type="button" onClick={() => void openDetail(r.id)}>Details</button>
                {r.summary.failed > 0 && (
                  <button type="button" disabled={busy} onClick={() => repair(r.id)}>
                    Repair failing tests
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
        <Pager page={list.data ?? null} offset={offset} limit={limit} onOffset={setOffset} />
      </Panel>

      {detail && (
        <Panel title={`Run · ${detail.status}`} actions={
          <button type="button" onClick={() => setDetail(null)}>Close</button>
        }>
          <p>
            <span className="chip sev-ok">{detail.summary.passed} passed</span>{' '}
            {detail.summary.failed > 0 && <span className="chip sev-error">{detail.summary.failed} failed</span>}{' '}
            {detail.summary.skipped > 0 && <span className="chip">{detail.summary.skipped} skipped</span>}
          </p>
          {detail.cases.length > 0 && (
            <table className="trace">
              <thead><tr><th>Test</th><th>Outcome</th></tr></thead>
              <tbody>
                {detail.cases.map((c) => (
                  <tr key={c.id} className={c.outcome === 'pass' ? 'change-add' : c.outcome === 'skip' ? '' : 'change-remove'}>
                    <td>{c.title}</td>
                    <td><span className="badge">{c.outcome}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {detail.summary.failed > 0 && (
            <p>
              <button type="button" disabled={busy} onClick={() => repair(detail.id)}>
                Repair failing tests
              </button>
            </p>
          )}
        </Panel>
      )}
    </>
  )
}
