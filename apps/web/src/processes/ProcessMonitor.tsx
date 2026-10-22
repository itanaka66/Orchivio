import { useState } from 'react'
import { type ApiClient } from '../api/client'
import { Loading, Panel, useApi } from '../admin/kit'

interface StepMetric { step_id: string; count: number; avg_ms: number; median_ms: number; p90_ms: number }
interface FlowMetric { flow_id: string; name: string; total: number; completed: number; failed: number; avg_duration_ms: number }
interface ThroughputPoint { date: string; started: number; completed: number; failed: number }

type Tab = 'throughput' | 'bottlenecks' | 'durations'

export function ProcessMonitor({ client, appId }: { client: ApiClient; appId: string }) {
  const [tab, setTab] = useState<Tab>('throughput')

  const throughput = useApi(() => client.request<{ items: ThroughputPoint[] }>('GET', `/applications/${appId}/process-metrics/throughput?days=30`), [client, appId])
  const bottlenecks = useApi(() => client.request<{ items: StepMetric[] }>('GET', `/applications/${appId}/process-metrics/bottlenecks`), [client, appId])
  const durations = useApi(() => client.request<{ items: FlowMetric[] }>('GET', `/applications/${appId}/process-metrics/durations`), [client, appId])

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
          <ThroughputChart data={throughput.data?.items ?? []} />
        </div>
      )}

      {tab === 'bottlenecks' && (
        <div role="tabpanel">
          <Loading loading={bottlenecks.loading} error={bottlenecks.error} />
          <table className="trace">
            <thead><tr><th>Step</th><th>Executions</th><th>Avg (ms)</th></tr></thead>
            <tbody>
              {(bottlenecks.data?.items ?? []).sort((a, b) => b.count - a.count).map((s) => (
                <tr key={s.step_id}>
                  <td><code>{s.step_id}</code></td>
                  <td>{s.count}</td>
                  <td style={{ background: heatColor(s.avg_ms, bottlenecks.data?.items ?? []) }}>{s.avg_ms.toFixed(0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'durations' && (
        <div role="tabpanel">
          <Loading loading={durations.loading} error={durations.error} />
          <table className="trace">
            <thead><tr><th>Flow</th><th>Total</th><th>Completed</th><th>Failed</th><th>Avg (ms)</th></tr></thead>
            <tbody>
              {(durations.data?.items ?? []).sort((a, b) => b.avg_duration_ms - a.avg_duration_ms).map((f) => (
                <tr key={f.flow_id}>
                  <td>{f.name || f.flow_id}</td>
                  <td>{f.total}</td>
                  <td>{f.completed}</td>
                  <td>{f.failed}</td>
                  <td>{f.avg_duration_ms.toFixed(0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function heatColor(val: number, all: StepMetric[]): string {
  const max = Math.max(...all.map((s) => s.avg_ms), 1)
  const ratio = val / max
  const r = Math.round(ratio * 200)
  return `rgba(${r + 55}, ${255 - r}, 0, 0.25)`
}

function ThroughputChart({ data }: { data: ThroughputPoint[] }) {
  if (!data.length) return <p className="hint">No data for the last 30 days.</p>
  const maxVal = Math.max(...data.flatMap((d) => [d.started, d.completed, d.failed]), 1)
  const barW = 20
  const gap = 4
  const h = 120
  const pad = 24
  const w = data.length * (barW + gap) + pad

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg width={w} height={h + 32} aria-label="Throughput chart" role="img">
        {data.map((d, i) => {
          const x = pad + i * (barW + gap)
          const bH = (d.started / maxVal) * h
          const cH = (d.completed / maxVal) * h
          const fH = (d.failed / maxVal) * h
          const segW = Math.floor(barW / 3)
          return (
            <g key={d.date}>
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
                  {d.date.slice(5)}
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
