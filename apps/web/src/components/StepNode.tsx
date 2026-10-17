import { Handle, Position, type NodeProps } from '@xyflow/react'
import { memo } from 'react'
import { HANDLE_LABEL, STEP_META, handlesOf, stepTitle } from '../flow/catalog'
import type { StepNode as StepNodeType } from '../flow/convert'
import { errorCount } from '../flow/issues'

/** One-line summary of a step's configuration, shown under its title. */
export function summary(type: string, c: Record<string, unknown>): string {
  const s = (k: string) => (typeof c[k] === 'string' ? (c[k] as string) : '')
  switch (type) {
    case 'retrieve': return [s('entity') || '?', c.many ? '(list)' : '', s('into') && `→ ${s('into')}`].filter(Boolean).join(' ')
    case 'create': return `${s('entity') || '?'}${s('into') ? ` → ${s('into')}` : ''}`
    case 'change': case 'delete': return s('target') || '?'
    case 'decision': return s('expr') || 'no condition'
    case 'loop': return `${s('item') || '?'} in ${s('over') || '?'} (max ${String(c.max_iterations ?? '?')})`
    case 'call_flow': return s('flow') || '?'
    case 'call_integration': return `${s('integration') || '?'}.${s('operation') || '?'}${s('into') ? ` → ${s('into')}` : ''}`
    case 'raise_error': return s('message') || 'no message'
    default: return ''
  }
}

function StepNodeView({ data, selected }: NodeProps<StepNodeType>) {
  const { step, issues } = data
  const meta = STEP_META[step.type]
  const handles = handlesOf(step.type)
  const errors = errorCount(issues)
  const warnings = issues.length - errors
  const problems = errors ? `${errors} error${errors > 1 ? 's' : ''}` : warnings ? `${warnings} warning${warnings > 1 ? 's' : ''}` : ''
  return (
    <div
      className={`step step-${step.type}${selected ? ' is-selected' : ''}${errors ? ' has-error' : warnings ? ' has-warning' : ''}${data.run ? (data.run.error ? ' ran-error' : ' ran') : ''}`}
      role="group"
      aria-label={`${stepTitle(step)}, ${meta.label} step${problems ? `, ${problems}` : ''}${data.run ? (data.run.error ? ', failed in the last run' : `, ran ${data.run.count} time${data.run.count > 1 ? 's' : ''} in the last run`) : ''}`}
    >
      {step.type !== 'start' && <Handle type="target" position={Position.Top} id="in" aria-label="Input" />}
      <div className="step-head">
        <span className="step-glyph" aria-hidden="true">{meta.glyph}</span>
        <span className="step-title">{stepTitle(step)}</span>
        {data.run && <span className={`badge ${data.run.error ? 'badge-error' : 'badge-ok'}`} title="Last run">{data.run.error ? '✕' : `×${data.run.count}`}</span>}
        {problems && <span className={`badge ${errors ? 'badge-error' : 'badge-warning'}`} title={problems}>{issues.length}</span>}
      </div>
      {summary(step.type, step.config) && <div className="step-summary">{summary(step.type, step.config)}</div>}
      {handles.map((h, i) => (
        <Handle
          key={h}
          type="source"
          position={Position.Bottom}
          id={h}
          aria-label={`Output: ${HANDLE_LABEL[h]}`}
          style={handles.length > 1 ? { left: `${((i + 1) / (handles.length + 1)) * 100}%` } : undefined}
        >
          {handles.length > 1 && <span className="handle-label">{HANDLE_LABEL[h]}</span>}
        </Handle>
      ))}
    </div>
  )
}

export const StepNodeComponent = memo(StepNodeView)
