/** Browser runtime for client flows (FR-053).
 *
 * Client flows run entirely in the browser: start, decision, call_flow, end.
 * Expressions are evaluated with a Function constructor (expressions are admin-authored model code, not user input).
 */
import type { Flow, FlowStep, RunResult, TraceSpan } from './types'

const MAX_STEPS = 500
const MAX_DEPTH = 10

class ClientFlowError extends Error {
  constructor(message: string, public readonly stepId: string | null = null) {
    super(message)
  }
}

function evalBool(expr: string, vars: Record<string, unknown>): boolean {
  const names = Object.keys(vars)
  const values = names.map((k) => vars[k])
  // eslint-disable-next-line no-new-func, @typescript-eslint/no-implied-eval
  return Boolean(new Function(...names, `return !!(${expr})`)(...values))
}

function evalValue(expr: string, vars: Record<string, unknown>): unknown {
  const names = Object.keys(vars)
  const values = names.map((k) => vars[k])
  // eslint-disable-next-line no-new-func, @typescript-eslint/no-implied-eval
  return new Function(...names, `return (${expr})`)(...values)
}

function followFlow(
  flowKey: string,
  args: Record<string, unknown>,
  flowMap: ReadonlyMap<string, Flow>,
  trace: TraceSpan[],
  depth: number,
  parentIteration: number[],
): void {
  if (depth > MAX_DEPTH) throw new ClientFlowError('maximum call depth exceeded')
  const flow = flowMap.get(flowKey)
  if (!flow) throw new ClientFlowError(`flow '${flowKey}' not found`)

  const stepMap = new Map(flow.steps.map((s) => [s.id, s]))
  const edgesBySource = new Map<string, typeof flow.edges>()
  for (const e of flow.edges) {
    const list = edgesBySource.get(e.source)
    if (list) list.push(e)
    else edgesBySource.set(e.source, [e])
  }

  const vars = { ...args }
  const start = flow.steps.find((s) => s.type === 'start')
  if (!start) throw new ClientFlowError(`flow '${flowKey}' has no start step`)

  let current: FlowStep = start
  let stepsRun = 0

  while (current.type !== 'end') {
    if (trace.length + ++stepsRun > MAX_STEPS) {
      throw new ClientFlowError('flow ran too many steps', current.id)
    }

    const t0 = Date.now()
    let handle: string = 'out'
    let detail = ''

    const snapVars = { ...vars }  // snapshot before this step runs

    try {
      if (current.type === 'start') {
        handle = 'out'
      } else if (current.type === 'decision') {
        const expr = String(current.config.expr ?? '')
        const result = evalBool(expr, vars)
        handle = result ? 'true' : 'false'
        detail = String(result)
      } else if (current.type === 'call_flow') {
        const callee = String(current.config.flow ?? '')
        const argExprs = (current.config.args ?? {}) as Record<string, string>
        const callArgs: Record<string, unknown> = {}
        for (const [k, expr] of Object.entries(argExprs)) {
          callArgs[k] = evalValue(expr, vars)
        }
        followFlow(callee, callArgs, flowMap, trace, depth + 1, parentIteration)
        detail = `→ ${callee}`
        handle = 'out'
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      trace.push({ flow: flowKey, step: current.id, type: current.type, status: 'error', ms: Date.now() - t0, iteration: parentIteration, detail: msg, vars: snapVars })
      throw e instanceof ClientFlowError ? e : new ClientFlowError(msg, current.id)
    }

    trace.push({ flow: flowKey, step: current.id, type: current.type, status: 'ok', ms: Date.now() - t0, iteration: parentIteration, detail, vars: snapVars })

    const edges = edgesBySource.get(current.id) ?? []
    const edge = edges.find((e) => e.handle === handle)
    if (!edge) throw new ClientFlowError(`step '${current.id}' has no '${handle}' edge`, current.id)
    const next = stepMap.get(edge.target)
    if (!next) throw new ClientFlowError(`edge points to unknown step '${edge.target}'`, current.id)
    current = next
  }

  trace.push({ flow: flowKey, step: current.id, type: 'end', status: 'ok', ms: 0, iteration: parentIteration, detail: '' })
}

/** Run a client flow entirely in the browser. Pass `allFlows` so `call_flow` steps resolve. */
export function runClientFlow(
  flowKey: string,
  args: Record<string, unknown>,
  allFlows: ReadonlyMap<string, Flow>,
): RunResult {
  const trace: TraceSpan[] = []
  try {
    followFlow(flowKey, args, allFlows, trace, 0, [])
    return { flow: flowKey, status: 'succeeded', error: null, error_step: null, steps_executed: trace.length, trace }
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    const errorStep = e instanceof ClientFlowError ? e.stepId : null
    return { flow: flowKey, status: 'failed', error: msg, error_step: errorStep, steps_executed: trace.length, trace }
  }
}
