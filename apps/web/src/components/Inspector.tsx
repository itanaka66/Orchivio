import type { ReactElement } from 'react'
import { HANDLE_LABEL, STEP_META, handlesOf, stepTitle, type FieldSpec } from '../flow/catalog'
import { parseIssuePath } from '../flow/issues'
import { entityOfVariable } from '../flow/model'
import { definedVariables, outgoing } from '../flow/ops'
import type { Flow, FlowParam, FlowStep, Handle, Issue, ModelView } from '../flow/types'

const SCALAR_TYPES = ['string', 'text', 'integer', 'decimal', 'boolean', 'date', 'datetime', 'uuid']

interface StepProps {
  flow: Flow
  step: FlowStep
  model: ModelView
  issues: Issue[]
  readOnly?: boolean
  onLabel: (label: string) => void
  onConfig: (name: string, value: unknown) => void
  /** Change several settings as one edit (one undo step). */
  onConfigs: (patch: Record<string, unknown>) => void
  onNext: (handle: Handle, target: string | null) => void
  onDelete: () => void
}

export function StepInspector({ flow, step, model, issues, readOnly, onLabel, onConfig, onConfigs, onNext, onDelete }: StepProps) {
  const meta = STEP_META[step.type]
  const others = flow.steps.filter((s) => s.id !== step.id && s.type !== 'start')
  const fieldIssues = (name: string) => issues.filter((i) => parseIssuePath(i.path).field === name)
  const general = issues.filter((i) => !parseIssuePath(i.path).field)
  return (
    <section className="inspector" aria-label={`Edit ${stepTitle(step)}`}>
      <h2>{meta.label} <code>{step.id}</code></h2>
      <p className="hint">{meta.description}</p>
      <label className="field">
        <span>Label</span>
        <input value={step.label ?? ''} disabled={readOnly} maxLength={100} placeholder={meta.label}
               onChange={(e) => onLabel(e.target.value)} />
      </label>

      {meta.fields.filter((f) => !f.showWhen || step.config[f.showWhen] === true).map((f) => (
        <Field key={f.name} spec={f} flow={flow} step={step} model={model} issues={fieldIssues(f.name)}
               readOnly={readOnly} onChange={(v) => onConfig(f.name, v)} onMany={onConfigs} />
      ))}

      {handlesOf(step.type).length > 0 && (
        <fieldset className="next">
          <legend>What happens next</legend>
          {handlesOf(step.type).map((h) => (
            <label className="field" key={h}>
              <span>{handlesOf(step.type).length > 1 ? `When “${HANDLE_LABEL[h]}”` : 'Next step'}</span>
              <select
                disabled={readOnly}
                value={outgoing(flow, step.id, h)?.target ?? ''}
                onChange={(e) => onNext(h, e.target.value || null)}
              >
                <option value="">(nothing: the flow ends here)</option>
                {others.map((s) => <option key={s.id} value={s.id}>{stepTitle(s)} ({s.id})</option>)}
              </select>
            </label>
          ))}
        </fieldset>
      )}

      {general.length > 0 && <IssueList issues={general} />}
      {step.type !== 'start' && !readOnly && (
        <button type="button" className="danger" onClick={onDelete}>Delete this step</button>
      )}
    </section>
  )
}

function IssueList({ issues }: { issues: Issue[] }) {
  return (
    <ul className="issues-inline" role="alert">
      {issues.map((i, n) => <li key={n} className={`sev-${i.severity}`}>{i.message}</li>)}
    </ul>
  )
}

interface FieldProps {
  spec: FieldSpec
  flow: Flow
  step: FlowStep
  model: ModelView
  issues: Issue[]
  readOnly?: boolean
  onChange: (v: unknown) => void
  onMany: (patch: Record<string, unknown>) => void
}

function Field({ spec, flow, step, model, issues, readOnly, onChange, onMany }: FieldProps) {
  const value = step.config[spec.name]
  const id = `f-${step.id}-${spec.name}`
  const invalid = issues.some((i) => i.severity === 'error')
  const common = { id, disabled: readOnly, 'aria-invalid': invalid || undefined, 'aria-describedby': issues.length ? `${id}-err` : undefined }
  let control: ReactElement
  switch (spec.kind) {
    case 'entity':
      control = (
        <select {...common} value={typeof value === 'string' ? value : ''} onChange={(e) => onChange(e.target.value)}>
          <option value="">Choose an entity…</option>
          {model.entities.map((e) => <option key={e.qualified} value={e.qualified}>{e.name}</option>)}
        </select>
      )
      break
    case 'expression':
      control = <input {...common} className="mono" spellCheck={false} value={typeof value === 'string' ? value : ''}
                       onChange={(e) => onChange(e.target.value)} />
      break
    case 'variable':
      control = <input {...common} className="mono" spellCheck={false} maxLength={31} pattern="[a-z][a-z0-9_]*"
                       value={typeof value === 'string' ? value : ''} onChange={(e) => onChange(e.target.value)} />
      break
    case 'variable-ref': {
      const vars = definedVariables(flow, step.id).map((v) => v.name)
      const current = typeof value === 'string' ? value : ''
      control = (
        <select {...common} value={current} onChange={(e) => onChange(e.target.value)}>
          <option value="">Choose a variable…</option>
          {[...new Set(current && !vars.includes(current) ? [...vars, current] : vars)].map((v) => <option key={v} value={v}>{v}</option>)}
        </select>
      )
      break
    }
    case 'integer':
      control = <input {...common} type="number" min={spec.min} max={spec.max} value={typeof value === 'number' ? value : ''}
                       onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))} />
      break
    case 'boolean':
      return (
        <label className="field check">
          <input id={id} type="checkbox" disabled={readOnly} checked={value === true} onChange={(e) => onChange(e.target.checked)} />
          <span>{spec.label}</span>
        </label>
      )
    case 'text':
      control = <input {...common} maxLength={200} value={typeof value === 'string' ? value : ''} onChange={(e) => onChange(e.target.value)} />
      break
    case 'flow':
      control = (
        <select {...common} value={typeof value === 'string' ? value : ''}
                onChange={(e) => {
                  const callee = model.flows.find((f) => f.key === e.target.value)
                  // choosing a flow pre-fills the argument list with that flow's parameters
                  onMany({ flow: e.target.value, args: Object.fromEntries((callee?.params ?? []).map((p: FlowParam) => [p.name, ''])) })
                }}>
          <option value="">Choose a flow…</option>
          {model.flows.filter((f) => f.key !== flow.key && (flow.kind === 'client' || f.kind === 'server')).map((f) => (
            <option key={f.key} value={f.key}>{f.key} ({f.kind})</option>
          ))}
        </select>
      )
      break
    case 'values': {
      const entityName = step.type === 'create'
        ? (step.config.entity as string | undefined)
        : entityOfVariable(flow, String(step.config.target ?? ''))
      const entity = model.entities.find((e) => e.qualified === entityName || e.name === entityName)
      return <KeyValues spec={spec} id={id} value={value} readOnly={readOnly} issues={issues}
                        keys={entity?.fields.filter((f) => f.editable).map((f) => f.name) ?? []} onChange={onChange} />
    }
    case 'args': {
      const callee = model.flows.find((f) => f.key === step.config.flow)
      return <KeyValues spec={spec} id={id} value={value} readOnly={readOnly} issues={issues}
                        keys={callee?.params.map((p) => p.name) ?? []} onChange={onChange} />
    }
  }
  return (
    <div className="field">
      <label htmlFor={id}>{spec.label}{spec.required && <abbr title="required"> *</abbr>}</label>
      {control}
      {spec.help && <small className="hint">{spec.help}</small>}
      {issues.length > 0 && <div id={`${id}-err`}><IssueList issues={issues} /></div>}
    </div>
  )
}

function KeyValues({ spec, id, value, keys, issues, readOnly, onChange }: {
  spec: FieldSpec; id: string; value: unknown; keys: string[]; issues: Issue[]; readOnly?: boolean; onChange: (v: unknown) => void
}) {
  const map = (value && typeof value === 'object' ? value : {}) as Record<string, string>
  const rows = Object.entries(map)
  const set = (next: Record<string, string>) => onChange(next)
  const unused = keys.filter((k) => !(k in map))
  return (
    <fieldset className="field kv" aria-describedby={issues.length ? `${id}-err` : undefined}>
      <legend>{spec.label}</legend>
      {rows.map(([k, v], i) => (
        <div className="kv-row" key={`${i}-${k}`}>
          <input aria-label={`${spec.label}: field name ${i + 1}`} list={`${id}-keys`} className="mono" value={k} disabled={readOnly}
                 onChange={(e) => set(Object.fromEntries(rows.map(([kk, vv], j) => (j === i ? [e.target.value, vv] : [kk, vv]))))} />
          <input aria-label={`${spec.label}: expression for ${k || 'field'}`} className="mono" spellCheck={false} value={String(v)} disabled={readOnly}
                 onChange={(e) => set({ ...map, [k]: e.target.value })} />
          {!readOnly && <button type="button" aria-label={`Remove ${k || 'row'}`} onClick={() => set(Object.fromEntries(rows.filter((_, j) => j !== i)))}>✕</button>}
        </div>
      ))}
      <datalist id={`${id}-keys`}>{keys.map((k) => <option key={k} value={k} />)}</datalist>
      {!readOnly && (
        <button type="button" onClick={() => set({ ...map, [unused[0] ?? `field_${rows.length + 1}`]: '' })}>Add {spec.name === 'args' ? 'argument' : 'field'}</button>
      )}
      {spec.help && <small className="hint">{spec.help}</small>}
      {issues.length > 0 && <div id={`${id}-err`}><IssueList issues={issues} /></div>}
    </fieldset>
  )
}

interface MetaProps {
  flow: Flow
  readOnly?: boolean
  onDescription: (d: string) => void
  onParams: (p: FlowParam[]) => void
}

export function FlowInspector({ flow, readOnly, onDescription, onParams }: MetaProps) {
  const set = (i: number, patch: Partial<FlowParam>) => onParams(flow.params.map((p, j) => (j === i ? { ...p, ...patch } : p)))
  return (
    <section className="inspector" aria-label="Flow settings">
      <h2>{flow.key} <small>({flow.kind} flow)</small></h2>
      <label className="field">
        <span>Description</span>
        <textarea rows={3} maxLength={500} disabled={readOnly} value={flow.description ?? ''} onChange={(e) => onDescription(e.target.value)} />
      </label>
      <fieldset className="field kv">
        <legend>Parameters</legend>
        {flow.params.map((p, i) => (
          <div className="kv-row" key={i}>
            <input aria-label={`Parameter ${i + 1} name`} className="mono" value={p.name} disabled={readOnly} pattern="[a-z][a-z0-9_]*"
                   onChange={(e) => set(i, { name: e.target.value })} />
            <input aria-label={`Parameter ${i + 1} type`} className="mono" list="param-types" value={p.type} disabled={readOnly}
                   onChange={(e) => set(i, { type: e.target.value })} />
            {!readOnly && <button type="button" aria-label={`Remove parameter ${p.name || i + 1}`} onClick={() => onParams(flow.params.filter((_, j) => j !== i))}>✕</button>}
          </div>
        ))}
        <datalist id="param-types">{SCALAR_TYPES.map((t) => <option key={t} value={t} />)}</datalist>
        {!readOnly && <button type="button" onClick={() => onParams([...flow.params, { name: `param_${flow.params.length + 1}`, type: 'string' }])}>Add parameter</button>}
        <small className="hint">Types: string, integer, …, enum:Name or ref:Entity.</small>
      </fieldset>
    </section>
  )
}
