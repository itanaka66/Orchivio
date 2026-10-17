// Small layered layout (no dependency): steps are placed in rows by their longest distance from
// the start, spread horizontally within a row. Good enough for flows of tens of steps; a
// dedicated layout engine (elkjs) can replace it later without touching the callers.
import type { Flow, Position } from './types'

export const COL_GAP = 220
export const ROW_GAP = 130

export function layout(flow: Flow): Record<string, Position> {
  const depth = new Map<string, number>()
  const indeg = new Map<string, number>(flow.steps.map((s) => [s.id, 0]))
  for (const e of flow.edges) indeg.set(e.target, (indeg.get(e.target) ?? 0) + 1)
  const queue = flow.steps.filter((s) => (indeg.get(s.id) ?? 0) === 0).map((s) => s.id)
  for (const id of queue) depth.set(id, 0)
  // Kahn's algorithm: longest-path depth over the DAG (the validator rejects cycles; stay safe anyway)
  for (let i = 0; i < queue.length; i++) {
    const id = queue[i] as string
    for (const e of flow.edges) {
      if (e.source !== id) continue
      depth.set(e.target, Math.max(depth.get(e.target) ?? 0, (depth.get(id) ?? 0) + 1))
      indeg.set(e.target, (indeg.get(e.target) ?? 1) - 1)
      if (indeg.get(e.target) === 0) queue.push(e.target)
    }
  }
  const rows = new Map<number, string[]>()
  for (const s of flow.steps) {
    const d = depth.get(s.id) ?? 0 // steps inside a cycle keep depth 0
    rows.set(d, [...(rows.get(d) ?? []), s.id])
  }
  const out: Record<string, Position> = {}
  for (const [d, ids] of rows) {
    ids.forEach((id, i) => {
      out[id] = { x: Math.round((i - (ids.length - 1) / 2) * COL_GAP), y: d * ROW_GAP }
    })
  }
  return out
}

/** Give every step without a position one from the layout, leaving placed steps alone. */
export function placeMissing(flow: Flow): Flow {
  if (flow.steps.every((s) => s.position)) return flow
  const auto = layout(flow)
  return { ...flow, steps: flow.steps.map((s) => (s.position ? s : { ...s, position: auto[s.id] ?? { x: 0, y: 0 } })) }
}
