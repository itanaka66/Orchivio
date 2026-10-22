import { useState } from 'react'
import { type ApiClient } from '../api/client'
import { Loading, Panel, useApi } from '../admin/kit'

interface ThroughputItem { process: string; status: string; bucket: string; count: number }
interface ThroughputResp { bucket: string; items: ThroughputItem[] }
interface BottleneckItem { process: string; step_id: string; token_count: number }
interface BottlenecksResp { items: BottleneckItem[]; running_instances_sampled: number }
interface DurationItem { process: string; count: number; avg_seconds: number; median_seconds: number; p90_seconds: number }
interface DurationsResp { items: DurationItem[] }

type Tab = 'throughput' | 'bottlenecks' | 'durations'

export function ProcessMonitor({ client, appId }: { client: ApiClient; appId: string }) {
  const [tab, setTab] = useState<Tab>('throughput')

  const throughput = useApi(() => client.request<ThroughputResp>('GET', `/applications/${appId}/process-analytics/throughput?days=30`), [client, appId])
  const bottlenecks = useApi(() => client.request<BottlenecksResp>('GET', `/applications/${appId}/process-analytics/bottlenecks`), [client, appId])
  const durations = useApi(() => client.request<DurationsResp>('GET', `/applications/${appId}/process-analytics/durations`), [client, appId])

  return (
    <Panel title="Process Monitor">
      <div role="tablist" className="tabs">
        {(['throughput', 'bottlenecks', 'durations'] as Tab[]).map((t) => (
          <button key={t} role="tab" type="button" aria-selected={tab === t} className={tab === t ? 'tab active' : 'tab'} onClick={() => setTab(t)}>
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      {tab === 'throughput' && (
        <div role="tabpanel">
          <Loading loading={throughput.loading} error={throughput.error} />
          <ThroughputChart items={throughput.data?.items ?? []} />
        </div>
      )}

      {tab === 'bottlenecks' && (
        <div role="tabpanel">
          <Loading loading={bottlenecks.loading} error={bottlenecks.error} />
          {bottlenecks.data && (
            <p className="hint" style={{ marginBottom: '0.5rem' }}>
              {bottlenecks.data.running_instances_sampled} running instance{bottlenecks.data.running_instances_sampled === 1 ? '' : 's'} sampled
            </p>
          )}
          <table className="trace">
            <thead><tr><th>Process</th><th>Step</th><th>Tokens waiting</th></tr></thead>
            <tbody>
              {(bottlenecks.data?.items ?? []).map((s) => (
                <tr key={`${s.process}:${s.step_id}`}>
                  <td>{s.process}</td>
                  <td><code>{s.step_id}</code></td>
                  <td>{s.token_count}</td>
                </tr>
              ))}
              {(bottlenecks.data?.items ?? []).length === 0 && !bottlenecks.loading && (
                <tr><td colSpan={3} className="hint">No waiting tokens.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'durations' && (
        <div role="tabpanel">
          <Loading loading={durations.loading} error={durations.error} />
          <table className="trace">
            <thead><tr><th>Process</th><th>Count</th><th>Avg (s)</th><th>Median (s)</th><th>P90 (s)</th></tr></thead>
            <tbody>
              {(durations.data?.items ?? []).map((f) => (
                <tr key={f.process}>
                  <td>{f.process}</td>
                  <td>{f.count}</td>
                  <td>{f.avg_seconds.toFixed(1)}</td>
                  <td>{f.median_seconds.toFixed(1)}</td>
                  <td>{f.p90_seconds.toFixed(1)}</td>
                </tr>
              ))}
              {(durations.data?.items ?? []).length === 0 && !durations.loading && (
                <tr><td colSpan={5} className="hint">No completed instances yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function ThroughputChart({ items }: { items: ThroughputItem[] }) {
  if (!items.length) return <p className="hint">No data for the last 30 days.</p>

  // Aggregate by bucket: count started (all), completed, failed
  const byBucket = new Map<string, { started: number; completed: number; failed: number }>()
  for (const item of items) {
    const entry = byBucket.get(item.bucket) ?? { started: 0, completed: 0, failed: 0 }
    entry.started += item.count
    if (item.status === 'completed') entry.completed += item.count
    if (item.status === 'failed') entry.failed += item.count
    byBucket.set(item.bucket, entry)
  }
  const data = Array.from(byBucket.entries()).sort((a, b) => a[0].localeCompare(b[0]))

  const maxVal = Math.max(...data.map(([, d]) => d.started), 1)
  const barW = 20
  const gap = 4
  const h = 120
  const pad = 24
  const w = data.length * (barW + gap) + pad

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg width={w} height={h + 32} aria-label="Throughput chart" role="img">
        {data.map(([bucket, d], i) => {
          const x = pad + i * (barW + gap)
          const bH = (d.started / maxVal) * h
          const cH = (d.completed / maxVal) * h
          const fH = (d.failed / maxVal) * h
          const segW = Math.floor(barW / 3)
          const label = bucket.slice(0, 10) // ISO date prefix
          return (
            <g key={bucket}>
              <rect x={x} y={h - bH} width={segW} height={bH} fill="var(--accent, #4a90e2)" opacity={0.7}>
                <title>Started: {d.started}</title>
              </rect>
              <rect x={x + segW} y={h - cH} width={segW} height={cH} fill="#52c41a" opacity={0.7}>
                <title>Completed: {d.completed}</title>
              </rect>
              <rect x={x + segW * 2} y={h - fH} width={segW} height={fH} fill="#ff4d4f" opacity={0.7}>
                <title>Failed: {d.failed}</title>
              </rect>
              {i % 5 === 0 && (
                <text x={x} y={h + 20} fontSize={9} fill="currentColor" opacity={0.5}>
                  {label.slice(5)}
                </text>
              )}
            </g>
          )
        })}
        <line x1={pad} y1={h} x2={w} y2={h} stroke="currentColor" opacity={0.2} />
      </svg>
      <div className="row" style={{ gap: '1rem', fontSize: '0.8rem' }}>
        <span><span style={{ color: 'var(--accent, #4a90e2)' }}>■</span> Started</span>
        <span><span style={{ color: '#52c41a' }}>■</span> Completed</span>
        <span><span style={{ color: '#ff4d4f' }}>■</span> Failed</span>
      </div>
    </div>
  )
}
