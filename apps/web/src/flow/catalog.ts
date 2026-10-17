import contract from './contract.json'
import type { Flow, Handle, StepType } from './types'

export const LIMITS = contract.limits
export const STEP_ID_RE = new RegExp(contract.patterns.stepId)
export const FLOW_KEY_RE = new RegExp(contract.patterns.flowKey)

export type FieldKind =
  | 'entity' | 'expression' | 'variable' | 'variable-ref' | 'integer' | 'boolean' | 'text'
  | 'values' | 'flow' | 'args'

export interface FieldSpec {
  name: string
  label: string
  kind: FieldKind
  required?: boolean
  help?: string
  min?: number
  max?: number
  /** Only shown when another boolean field is true. */
  showWhen?: string
}

export interface StepMeta {
  label: string
  description: string
  glyph: string
  fields: FieldSpec[]
  defaults: () => Record<string, unknown>
}

export const STEP_META: Record<StepType, StepMeta> = {
  start: { label: 'Start', description: 'Where the flow begins.', glyph: '▶', fields: [], defaults: () => ({}) },
  retrieve: {
    label: 'Retrieve', glyph: '⌕', description: 'Read records into a variable.',
    fields: [
      { name: 'entity', label: 'Entity', kind: 'entity', required: true },
      { name: 'where', label: 'Where', kind: 'expression', help: 'Boolean over the record’s fields and earlier variables.' },
      { name: 'many', label: 'Retrieve a list', kind: 'boolean' },
      { name: 'limit', label: 'Limit', kind: 'integer', min: 1, max: LIMITS.maxRetrieve, showWhen: 'many' },
      { name: 'into', label: 'Store in variable', kind: 'variable', required: true },
    ],
    defaults: () => ({ entity: '', into: '' }),
  },
  create: {
    label: 'Create', glyph: '＋', description: 'Create a record.',
    fields: [
      { name: 'entity', label: 'Entity', kind: 'entity', required: true },
      { name: 'values', label: 'Field values', kind: 'values', help: 'An expression per field; required fields must be set.' },
      { name: 'into', label: 'Store in variable', kind: 'variable' },
    ],
    defaults: () => ({ entity: '', values: {} }),
  },
  change: {
    label: 'Change', glyph: '✎', description: 'Update a record held in a variable.',
    fields: [
      { name: 'target', label: 'Record variable', kind: 'variable-ref', required: true },
      { name: 'values', label: 'Field values', kind: 'values' },
    ],
    defaults: () => ({ target: '', values: {} }),
  },
  delete: {
    label: 'Delete', glyph: '🗑', description: 'Delete a record held in a variable.',
    fields: [{ name: 'target', label: 'Record variable', kind: 'variable-ref', required: true }],
    defaults: () => ({ target: '' }),
  },
  decision: {
    label: 'Decision', glyph: '◇', description: 'Branch on a true/false expression.',
    fields: [{ name: 'expr', label: 'Condition', kind: 'expression', required: true }],
    defaults: () => ({ expr: '' }),
  },
  loop: {
    label: 'Loop', glyph: '↻', description: 'Repeat the body for each record in a list (bounded).',
    fields: [
      { name: 'over', label: 'List variable', kind: 'variable-ref', required: true },
      { name: 'item', label: 'Item variable', kind: 'variable', required: true },
      { name: 'max_iterations', label: 'Maximum iterations', kind: 'integer', required: true, min: 1, max: LIMITS.maxIterations },
    ],
    defaults: () => ({ over: '', item: '', max_iterations: 100 }),
  },
  call_flow: {
    label: 'Call flow', glyph: '⇢', description: 'Run another flow.',
    fields: [
      { name: 'flow', label: 'Flow', kind: 'flow', required: true },
      { name: 'args', label: 'Arguments', kind: 'args' },
    ],
    defaults: () => ({ flow: '', args: {} }),
  },
  call_integration: {
    label: 'Call API', glyph: '⇄', description: 'Call an operation of a consumed REST API (configured integration).',
    fields: [
      { name: 'integration', label: 'Integration', kind: 'text', required: true },
      { name: 'operation', label: 'Operation', kind: 'text', required: true },
      { name: 'args', label: 'Parameters', kind: 'args' },
      { name: 'body', label: 'Request body fields', kind: 'args' },
      { name: 'into', label: 'Store response in', kind: 'variable' },
    ],
    defaults: () => ({ integration: '', operation: '', args: {} }),
  },
  raise_error: {
    label: 'Raise error', glyph: '⚠', description: 'Stop the flow and roll back with a message.',
    fields: [{ name: 'message', label: 'Message', kind: 'text', required: true }],
    defaults: () => ({ message: '' }),
  },
  end: { label: 'End', description: 'Finish the flow successfully.', glyph: '■', fields: [], defaults: () => ({}) },
}

export const HANDLE_LABEL: Record<Handle, string> = {
  out: 'next', true: 'true', false: 'false', body: 'each item', done: 'when done',
}

export function handlesOf(type: StepType): Handle[] {
  return (contract.handles[type] ?? []) as Handle[]
}

export function allowedTypes(kind: Flow['kind']): StepType[] {
  return (kind === 'server' ? contract.serverSteps : contract.clientSteps) as StepType[]
}

/** Types the palette may add: a flow has exactly one start step, created with the flow. */
export function addableTypes(kind: Flow['kind']): StepType[] {
  return allowedTypes(kind).filter((t) => t !== 'start')
}

export function stepTitle(step: { type: StepType; label?: string | null; id: string }): string {
  return step.label?.trim() || STEP_META[step.type].label
}
