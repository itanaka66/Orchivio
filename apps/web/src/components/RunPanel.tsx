import { useState } from 'react'
import type { Flow, RunResult, TraceSpan } from '../flow/types'

interface Props {
  flow: Flow
  roles: string[]
  /** Why running is not possible right now (shown instead of enabling the button). */
  blockedReason?: string
  result: RunResult | null
  onRun: (args: Record<string, unknown>, asUser: string, roles: string[]) => Promise<void>
  onGoTo: (stepId: string) => void
}

/** Parse a text input into the value a parameter of this type expects. */
export function parseArg(type: string, raw: string): unknown {
  if (type === 'integer') return /^-?\d+$/.test(raw.trim()) ? Number(raw) : raw
  if (type === 'boolean') return raw === 'true' ? true : raw === 'false' ? false : raw
  return raw
}

export function RunPanel({ flow, roles, blockedReason, result, onRun, onGoTo }: Props) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [asUser, setAsUser] = useState('preview-user')
  const [asRoles, setAsRoles] = useState(roles.join(', '))
  const [busy, setBusy] = useState(false)
  const [inspected, setInspected] = useState<TraceSpan | null>(null)

  return (
    <section className="run-panel" aria-label="Run">
      <h2>Run in the preview</h2>
      <p className="hint">
        Runs the approved version of this flow as the simulated user below, in one transaction: if any step fails,
        nothing is changed.
      </p>
      {flow.params.map((p) => (
        <label className="field" key={p.name}>
          <span>{p.name} <small>({p.type})</small></span>
          <input value={values[p.name] ?? ''} onChange={(e) => setValues({ ...values, [p.name]: e.target.value })} className="mono" />
        </label>
      ))}
      <label className="field"><span>Run as user</span><input value={asUser} maxLength={64} onChange={(e) => setAsUser(e.target.value)} /></label>
      <label className="field">
        <span>With roles</span>
        <input value={asRoles} onChange={(e) => setAsRoles(e.target.value)} />
        <small className="hint">The flow can do only what these roles allow.</small>
      </label>
      <button
        type="button"
        className="primary"
        disabled={busy || !!blockedReason}
        title={blockedReason}
        onClick={async () => {
          setBusy(true)
          try {
            const args = Object.fromEntries(flow.params.map((p) => [p.name, parseArg(p.type, values[p.name] ?? '')]))
            await onRun(args, asUser.trim() || 'preview-user', asRoles.split(',').map((r) => r.trim()).filter(Boolean))
          } finally {
            setBusy(false)
          }
        }}
      >
        {busy ? 'Running…' : 'Run'}
      </button>
      {blockedReason && <p className="hint" role="note">{blockedReason}</p>}

      {result && (
        <div aria-live="polite">
          <p className={`result-${result.status}`}>
            {result.status === 'succeeded' ? 'Succeeded' : 'Failed'} after {result.steps_executed} step{result.steps_executed === 1 ? '' : 's'}.
          </p>
          {result.error && (
            <p className="sev-error">
              {result.error}
              {result.error_step && <button type="button" className="link" onClick={() => onGoTo(result.error_step as string)}>Go to {result.error_step}</button>}
            </p>
          )}
          <ol aria-label="Steps that ran">
            {result.trace.map((s, i) => (
              <li key={i} className={s.status === 'error' ? 'sev-error' : undefined}>
                <button type="button" className="link" onClick={() => { onGoTo(s.step); setInspected(inspected?.step === s.step && inspected.flow === s.flow ? null : s) }}>
                  <code>{s.step}</code>
                </button>
                {s.iteration.length ? ` [${s.iteration.join('.')}]` : ''}
                {s.detail ? ` — ${s.detail}` : ''}
                {s.status === 'error' && ' (failed)'}
                {inspected?.step === s.step && inspected.flow === s.flow && s.vars !== undefined && (
                  <pre className="hint" style={{ margin: '2px 0 0', fontSize: '0.75em', overflowX: 'auto' }}>
                    {JSON.stringify(s.vars, null, 2)}
                  </pre>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  )
}
