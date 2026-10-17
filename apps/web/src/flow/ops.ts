// Pure, immutable edits of a Flow. Every editing gesture (palette, canvas, inspector, keyboard)
// goes through these functions, so the rules are in one place and unit-testable.
import { LIMITS, STEP_META, addableTypes, handlesOf } from './catalog'
import type { Flow, FlowEdge, FlowStep, Handle, Position, StepType } from './types'

export type Result<T> = ({ ok: true } & T) | { ok: false; error: string }

const fail = (error: string): { ok: false; error: string } => ({ ok: false, error })

function nextId(prefix: string, taken: Set<string>): string {
  for (let n = 1; ; n++) {
    const id = `${prefix}_${n}`
    if (!taken.has(id)) return id
  }
}

export function newFlow(key: string, kind: Flow['kind'] = 'server'): Flow {
  return {
    key, kind, params: [], edges: [],
    steps: [{ id: 'start', type: 'start', config: {}, position: { x: 0, y: 0 } }],
  }
}

export function addStep(flow: Flow, type: StepType, position: Position): Result<{ flow: Flow; id: string }> {
  if (!addableTypes(flow.kind).includes(type)) {
    return fail(`A ${flow.kind} flow cannot use a “${STEP_META[type].label}” step.`)
  }
  if (flow.steps.length >= LIMITS.maxSteps) return fail(`A flow can have at most ${LIMITS.maxSteps} steps.`)
  const id = nextId(type, new Set(flow.steps.map((s) => s.id)))
  const step: FlowStep = { id, type, config: STEP_META[type].defaults(), position }
  return { ok: true, flow: { ...flow, steps: [...flow.steps, step] }, id }
}

export function removeSteps(flow: Flow, ids: Iterable<string>): Flow {
  const doomed = new Set(ids)
  doomed.delete(startId(flow) ?? '') // the start step cannot be removed
  if (doomed.size === 0) return flow
  return {
    ...flow,
    steps: flow.steps.filter((s) => !doomed.has(s.id)),
    edges: flow.edges.filter((e) => !doomed.has(e.source) && !doomed.has(e.target)),
  }
}

export function startId(flow: Flow): string | undefined {
  return flow.steps.find((s) => s.type === 'start')?.id
}

export function moveSteps(flow: Flow, positions: Record<string, Position>): Flow {
  let changed = false
  const steps = flow.steps.map((s) => {
    const p = positions[s.id]
    if (!p || (s.position?.x === p.x && s.position?.y === p.y)) return s
    changed = true
    return { ...s, position: { x: Math.round(p.x), y: Math.round(p.y) } }
  })
  return changed ? { ...flow, steps } : flow
}

export function updateStep(flow: Flow, id: string, patch: { label?: string | null; config?: Record<string, unknown> }): Flow {
  return {
    ...flow,
    steps: flow.steps.map((s) => {
      if (s.id !== id) return s
      const label = patch.label === undefined ? s.label : patch.label?.trim() ? patch.label : null
      return { ...s, label: label ?? undefined, config: patch.config ?? s.config }
    }),
  }
}

const isEmpty = (v: unknown) =>
  v === undefined || v === null || v === '' ||
  (typeof v === 'object' && !Array.isArray(v) && Object.keys(v as object).length === 0)

/** Set one configuration field; an empty value removes the key. */
export function setConfig(flow: Flow, id: string, name: string, value: unknown): Flow {
  const step = flow.steps.find((s) => s.id === id)
  if (!step) return flow
  const config = { ...step.config }
  if (isEmpty(value)) delete config[name]
  else config[name] = value
  return updateStep(flow, id, { config })
}

export function connect(flow: Flow, source: string, handle: Handle, target: string): Result<{ flow: Flow; edge: FlowEdge }> {
  const src = flow.steps.find((s) => s.id === source)
  const dst = flow.steps.find((s) => s.id === target)
  if (!src || !dst) return fail('Both steps must exist.')
  if (source === target) return fail('A step cannot connect to itself. Use a loop step to repeat work.')
  if (dst.type === 'start') return fail('Nothing can connect into the start step.')
  if (!handlesOf(src.type).includes(handle)) return fail(`A “${STEP_META[src.type].label}” step has no “${handle}” output.`)
  if (flow.edges.some((e) => e.source === source && e.handle === handle)) {
    return fail(`The “${handle}” output of this step is already connected. Remove that connection first.`)
  }
  if (flow.edges.length >= LIMITS.maxEdges) return fail(`A flow can have at most ${LIMITS.maxEdges} connections.`)
  if (reaches(flow, target, source)) return fail('That connection would create a cycle. Use a loop step to repeat work.')
  const edge: FlowEdge = { id: nextId('e', new Set(flow.edges.map((e) => e.id))), source, target, handle }
  return { ok: true, flow: { ...flow, edges: [...flow.edges, edge] }, edge }
}

/** Is ``to`` reachable from ``from`` by following edges? (cycle check before connecting) */
export function reaches(flow: Flow, from: string, to: string): boolean {
  const seen = new Set<string>([from])
  const stack = [from]
  while (stack.length) {
    const n = stack.pop() as string
    if (n === to) return true
    for (const e of flow.edges) {
      if (e.source === n && !seen.has(e.target)) {
        seen.add(e.target)
        stack.push(e.target)
      }
    }
  }
  return false
}

/** Point an output at ``target`` (replacing any existing connection), or disconnect it with null. */
export function setNext(flow: Flow, source: string, handle: Handle, target: string | null): Result<{ flow: Flow }> {
  const without = { ...flow, edges: flow.edges.filter((e) => !(e.source === source && e.handle === handle)) }
  if (target === null) return { ok: true, flow: without }
  const r = connect(without, source, handle, target)
  return r.ok ? { ok: true, flow: r.flow } : r
}

export function removeEdges(flow: Flow, ids: Iterable<string>): Flow {
  const doomed = new Set(ids)
  return doomed.size === 0 ? flow : { ...flow, edges: flow.edges.filter((e) => !doomed.has(e.id)) }
}

export function updateMeta(flow: Flow, patch: Partial<Pick<Flow, 'description' | 'params' | 'kind'>>): Flow {
  return { ...flow, ...patch }
}

export function outgoing(flow: Flow, id: string, handle: Handle): FlowEdge | undefined {
  return flow.edges.find((e) => e.source === id && e.handle === handle)
}

/** Variables a step could refer to are defined by upstream steps; names for pick-lists. */
export function definedVariables(flow: Flow, before?: string): { name: string; type: StepType; step: string }[] {
  const upstream = before ? ancestors(flow, before) : new Set(flow.steps.map((s) => s.id))
  const out: { name: string; type: StepType; step: string }[] = []
  for (const s of flow.steps) {
    if (!upstream.has(s.id)) continue
    for (const key of ['into', 'item']) {
      const v = s.config[key]
      if (typeof v === 'string' && v) out.push({ name: v, type: s.type, step: s.id })
    }
  }
  return out
}

function ancestors(flow: Flow, id: string): Set<string> {
  const seen = new Set<string>()
  const stack = [id]
  while (stack.length) {
    const n = stack.pop() as string
    for (const e of flow.edges) {
      if (e.target === n && !seen.has(e.source)) {
        seen.add(e.source)
        stack.push(e.source)
      }
    }
  }
  return seen
}
