import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { Feedback, Loading, Panel, Pill, useAction, useApi, when } from './kit'

interface Line { id: number; time: string; level: string; source: string; message: string; trace_id: string | null; fields: Record<string, unknown> }
interface Analysis { lines_analyzed: number; summary: string; findings: { title: string; severity: string; explanation: string; suggestion: string; evidence_ids: number[] }[]; dropped_findings: number; note: string }

export function Logs({ client, appId }: { client: ApiClient; appId: string }) {
  const [level, setLevel] = useState('info')
  const [source, setSource] = useState('')
  const [q, setQ] = useState('')
  const [pages, setPages] = useState<Line[][]>([])
  const [before, setBefore] = useState<number | null>(null)
  const logs = useApi(() => client.request<{ items: Line[]; next_before_id: number | null }>('GET',
    `/applications/${appId}/logs?limit=100&level=${level}${source ? `&source=${encodeURIComponent(source)}` : ''}${q ? `&q=${encodeURIComponent(q)}` : ''}${before ? `&before_id=${before}` : ''}`), [client, appId, level, source, q, before])
  const { run, error, notice, busy } = useAction(() => undefined)
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [minutes, setMinutes] = useState(60)
  const [question, setQuestion] = useState('')
  const [aiError, setAiError] = useState('')
  const [highlight, setHighlight] = useState<number[]>([])

  const lines = [...pages.flat(), ...(logs.data?.items ?? [])]
  const reset = () => { setPages([]); setBefore(null) }

  return (
    <>
      <Panel title="Logs">
        <p className="hint">Operational lines of this application (background jobs, webhook deliveries, integration calls). They never contain record data.</p>
        <div className="toolbar">
          <label>Level <select aria-label="Level" value={level} onChange={(e) => { setLevel(e.target.value); reset() }}>{['debug', 'info', 'warning', 'error'].map((l) => <option key={l} value={l}>{l}</option>)}</select></label>
          <label>Source <select aria-label="Source" value={source} onChange={(e) => { setSource(e.target.value); reset() }}>{['', 'flow', 'job', 'webhook', 'integration', 'process', 'gitsync'].map((s) => <option key={s} value={s}>{s || 'any'}</option>)}</select></label>
          <label>Search <input aria-label="Search" value={q} onChange={(e) => { setQ(e.target.value); reset() }} /></label>
          <button type="button" onClick={() => { reset(); logs.reload() }}>Refresh</button>
        </div>
        <Loading loading={logs.loading && lines.length === 0} error={logs.error} />
        {!logs.loading && lines.length === 0 && <p className="hint">No log lines match.</p>}
        <table className="trace logs">
          <tbody>
            {lines.map((l) => (
              <tr key={l.id} className={highlight.includes(l.id) ? 'hl' : ''} data-line={l.id}>
                <td><small>{when(l.time)}</small></td><td><Pill value={l.level === 'warning' ? 'warn' : l.level} /></td><td>{l.source}</td><td>{l.message}</td>
                <td><small className="hint">{Object.entries(l.fields).map(([k, v]) => `${k}=${String(v)}`).join(' ')}</small></td><td><small className="hint">#{l.id}</small></td>
              </tr>
            ))}
          </tbody>
        </table>
        {logs.data?.next_before_id && <button type="button" onClick={() => { setPages([...pages, logs.data?.items ?? []]); setBefore(logs.data?.next_before_id ?? null) }}>Older lines</button>}
      </Panel>

      <Panel title="Ask the AI about recent lines">
        <p className="hint">Sends only the selected stored lines to the AI provider. Findings cite line numbers; claims without a real line are dropped.</p>
        <div className="toolbar">
          <label>Last <select aria-label="Window" value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>{[15, 60, 360, 1440].map((m) => <option key={m} value={m}>{m >= 60 ? `${m / 60} h` : `${m} min`}</option>)}</select></label>
          <label className="grow">Question <input aria-label="Question" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="Why do deliveries keep failing?" /></label>
          <button type="button" className="primary" disabled={busy} onClick={() => void run(async () => {
            setAiError(''); setAnalysis(null)
            try { setAnalysis(await client.request<Analysis>('POST', `/applications/${appId}/logs/analyze`, { minutes, level: 'warning', ...(question.trim() ? { question: question.trim() } : {}) })) }
            catch (e) { setAiError(e instanceof Error ? e.message : 'Analysis failed.') }
          })}>Analyse warnings and errors</button>
        </div>
        <Feedback error={error || aiError} notice={notice} />
        {analysis && (
          <div role="status">
            <p><strong>{analysis.summary}</strong> <small className="hint">({analysis.lines_analyzed} lines)</small></p>
            <ul className="list">
              {analysis.findings.map((f, i) => (
                <li key={i} className="card slim">
                  <strong>{f.title}</strong> <Pill value={f.severity === 'high' ? 'error' : f.severity === 'medium' ? 'warn' : 'plain'} />
                  <p>{f.explanation}</p>{f.suggestion && <p className="hint">Suggestion: {f.suggestion}</p>}
                  <button type="button" className="link" onClick={() => setHighlight(f.evidence_ids)}>Show lines {f.evidence_ids.map((n) => `#${n}`).join(' ')}</button>
                </li>
              ))}
            </ul>
            {analysis.dropped_findings > 0 && <p className="hint">{analysis.dropped_findings} finding(s) were dropped because they cited no real line.</p>}
            <p className="hint">{analysis.note}</p>
          </div>
        )}
      </Panel>
    </>
  )
}
