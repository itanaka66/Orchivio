// Mirrors src/orchivio/model/schema.py (FlowDef). The allowed step types, handles and limits come
// from contract.json, which is generated from the validator.

export type StepType =
  | 'start' | 'retrieve' | 'create' | 'change' | 'delete'
  | 'decision' | 'loop' | 'call_flow' | 'raise_error' | 'end'

export type Handle = 'out' | 'true' | 'false' | 'body' | 'done'

export interface Position { x: number; y: number }

export interface FlowParam { name: string; type: string }

export interface FlowStep {
  id: string
  type: StepType
  label?: string | null
  config: Record<string, unknown>
  position?: Position | null
}

export interface FlowEdge { id: string; source: string; target: string; handle: Handle }

export interface Flow {
  element_id?: string | null
  key: string
  kind: 'server' | 'client'
  description?: string | null
  params: FlowParam[]
  steps: FlowStep[]
  edges: FlowEdge[]
  /** Declarative tests live in the model; the editor carries them through untouched. */
  tests?: unknown[]
}

export interface Issue {
  severity: 'error' | 'warning'
  code: string
  path: string
  message: string
  element_id?: string | null
}

/** Minimal view of the application model that the editor needs for choices. */
export interface ModelView {
  roles: string[]
  entities: { qualified: string; name: string; fields: { name: string; type: string; editable: boolean }[] }[]
  flows: { key: string; kind: 'server' | 'client'; params: FlowParam[] }[]
}

export interface TraceSpan {
  flow: string
  step: string
  type: string
  status: 'ok' | 'error'
  ms: number
  iteration: number[]
  detail: string
}

export interface RunResult {
  flow: string
  status: 'succeeded' | 'failed'
  error: string | null
  error_step: string | null
  steps_executed: number
  trace: TraceSpan[]
}
