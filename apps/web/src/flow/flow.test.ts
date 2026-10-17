import { describe, expect, it } from 'vitest'
import { addableTypes, allowedTypes, handlesOf } from './catalog'
import contract from './contract.json'
import { flowToEdges, flowToNodes, nodePositions } from './convert'
import { canRedo, canUndo, commit, initial, push, redo, replace, undo } from './history'
import { groupIssues, parseIssuePath } from './issues'
import { layout, placeMissing } from './layout'
import {
  addStep, connect, definedVariables, moveSteps, newFlow, removeEdges, removeSteps, setConfig, setNext, startId,
  updateStep,
} from './ops'
import sample from './sample-flow.json'
import type { Flow, Issue } from './types'

const sampleFlow = sample as unknown as Flow

function built() {
  let f = newFlow('Demo')
  const a = addStep(f, 'decision', { x: 0, y: 100 })
  if (!a.ok) throw new Error(a.error)
  f = a.flow
  const b = addStep(f, 'end', { x: -100, y: 200 })
  if (!b.ok) throw new Error(b.error)
  return { flow: b.flow, decision: a.id, end: b.id }
}

describe('contract', () => {
  it('knows the same step types as the backend', () => {
    expect(allowedTypes('server').length).toBe(contract.serverSteps.length)
    expect(allowedTypes('client')).toEqual(['start', 'decision', 'call_flow', 'end'])
    expect(handlesOf('decision')).toEqual(['true', 'false'])
    expect(handlesOf('end')).toEqual([])
  })
  it('never offers a second start step', () => {
    expect(addableTypes('server')).not.toContain('start')
  })
})

describe('ops', () => {
  it('new flows have exactly a start step', () => {
    const f = newFlow('X')
    expect(f.steps.map((s) => s.type)).toEqual(['start'])
    expect(startId(f)).toBe('start')
  })

  it('adds steps with unique ids and default config', () => {
    let f = newFlow('X')
    for (let i = 0; i < 3; i++) {
      const r = addStep(f, 'retrieve', { x: 0, y: i })
      if (!r.ok) throw new Error(r.error)
      f = r.flow
    }
    expect(new Set(f.steps.map((s) => s.id)).size).toBe(4)
    expect(f.steps[1]?.id).toBe('retrieve_1')
    expect(f.steps[1]?.config).toEqual({ entity: '', into: '' })
  })

  it('refuses steps a client flow may not use', () => {
    const r = addStep(newFlow('C', 'client'), 'create', { x: 0, y: 0 })
    expect(r.ok).toBe(false)
  })

  it('connects, rejecting the same invalid connections as the server', () => {
    const { flow, decision, end } = built()
    expect(connect(flow, 'start', 'out', 'start').ok).toBe(false) // self
    expect(connect(flow, decision, 'out', end).ok).toBe(false) // decisions have true/false, not out
    expect(connect(flow, end, 'out', decision).ok).toBe(false) // end has no output
    expect(connect(flow, decision, 'true', 'start').ok).toBe(false) // into start
    const a = connect(flow, 'start', 'out', decision)
    expect(a.ok).toBe(true)
    if (!a.ok) return
    expect(connect(a.flow, 'start', 'out', end).ok).toBe(false) // output already used
    const b = connect(a.flow, decision, 'true', end)
    if (!b.ok) throw new Error(b.error)
    expect(b.flow.edges.map((e) => e.id)).toEqual(['e_1', 'e_2'])
  })

  it('refuses a connection that would close a cycle', () => {
    const { flow, decision } = built()
    const r1 = addStep(flow, 'decision', { x: 0, y: 300 })
    if (!r1.ok) throw new Error(r1.error)
    let f = r1.flow
    for (const [s, h, t] of [['start', 'out', decision], [decision, 'true', r1.id]] as const) {
      const c = connect(f, s, h, t)
      if (!c.ok) throw new Error(c.error)
      f = c.flow
    }
    const back = connect(f, r1.id, 'true', decision)
    expect(back.ok).toBe(false)
    if (!back.ok) expect(back.error).toMatch(/cycle/)
  })

  it('setNext replaces or clears an output', () => {
    const { flow, decision, end } = built()
    const c = connect(flow, 'start', 'out', decision)
    if (!c.ok) throw new Error(c.error)
    const moved = setNext(c.flow, 'start', 'out', end)
    expect(moved.ok && moved.flow.edges.map((e) => e.target)).toEqual([end])
    const cleared = setNext(c.flow, 'start', 'out', null)
    expect(cleared.ok && cleared.flow.edges).toEqual([])
  })

  it('removing a step removes its connections but never the start step', () => {
    const { flow, decision, end } = built()
    const c1 = connect(flow, 'start', 'out', decision)
    if (!c1.ok) throw new Error(c1.error)
    const c2 = connect(c1.flow, decision, 'true', end)
    if (!c2.ok) throw new Error(c2.error)
    expect(removeSteps(c2.flow, [decision]).edges).toEqual([])
    expect(removeSteps(c2.flow, ['start']).steps.length).toBe(c2.flow.steps.length)
    expect(removeEdges(c2.flow, [c2.edge.id]).edges.length).toBe(1)
  })

  it('moving steps rounds positions and keeps identity when nothing changes', () => {
    const { flow } = built()
    expect(moveSteps(flow, { start: { x: 0, y: 0 } })).toBe(flow)
    const moved = moveSteps(flow, { start: { x: 10.4, y: 20.6 } })
    expect(moved.steps[0]?.position).toEqual({ x: 10, y: 21 })
  })

  it('setConfig removes empty values and trims labels', () => {
    const { flow, decision } = built()
    const set = setConfig(flow, decision, 'expr', 'a > 1')
    expect(set.steps.find((s) => s.id === decision)?.config).toEqual({ expr: 'a > 1' })
    const cleared = setConfig(set, decision, 'expr', '')
    expect(cleared.steps.find((s) => s.id === decision)?.config).toEqual({})
    const labelled = updateStep(flow, decision, { label: '  ' })
    expect(labelled.steps.find((s) => s.id === decision)?.label).toBeUndefined()
  })

  it('lists variables defined upstream of a step', () => {
    const names = definedVariables(sampleFlow, 'bump').map((v) => v.name)
    expect(names).toEqual(expect.arrayContaining(['low', 'product']))
    expect(definedVariables(sampleFlow, 'find').map((v) => v.name)).toEqual([])
  })
})

describe('history', () => {
  it('undoes, redoes and drops redo history on a new change', () => {
    let h = initial(1)
    h = push(push(h, 2), 3)
    expect([canUndo(h), canRedo(h)]).toEqual([true, false])
    h = undo(undo(h))
    expect(h.present).toBe(1)
    h = redo(h)
    expect(h.present).toBe(2)
    h = push(h, 9)
    expect(canRedo(h)).toBe(false)
    expect(undo(undo(undo(h))).present).toBe(1)
  })
  it('commits a series of replaced values as one undo step', () => {
    let h = initial('a')
    const base = h.present
    h = replace(replace(h, 'b'), 'c')
    h = commit(h, base)
    expect(h.past).toEqual(['a'])
    expect(undo(h).present).toBe('a')
  })
  it('ignores no-op pushes and bounds the history', () => {
    let h = initial(0)
    expect(push(h, 0)).toBe(h)
    for (let i = 1; i <= 150; i++) h = push(h, i)
    expect(h.past.length).toBe(100)
  })
})

describe('layout', () => {
  it('places every step, parents above children, without overlap', () => {
    const f = { ...sampleFlow, steps: sampleFlow.steps.map((s) => ({ ...s, position: undefined })) }
    const placed = layout(f)
    expect(Object.keys(placed).sort()).toEqual(f.steps.map((s) => s.id).sort())
    for (const e of f.edges) expect((placed[e.target]?.y ?? 0)).toBeGreaterThan(placed[e.source]?.y ?? 0)
    const spots = new Set(Object.values(placed).map((p) => `${p.x},${p.y}`))
    expect(spots.size).toBe(f.steps.length)
  })
  it('only fills in missing positions', () => {
    const f = placeMissing({ ...sampleFlow, steps: sampleFlow.steps.map((s, i) => (i === 0 ? s : { ...s, position: undefined })) })
    expect(f.steps[0]?.position).toEqual(sampleFlow.steps[0]?.position)
    expect(f.steps.every((s) => s.position)).toBe(true)
    expect(placeMissing(sampleFlow)).toBe(sampleFlow)
  })
})

describe('issues', () => {
  const at = (path: string, severity: Issue['severity'] = 'error'): Issue => ({ severity, code: 'x', path, message: 'm' })
  it('maps server paths to steps, edges and fields', () => {
    expect(parseIssuePath('/modules/0/flows/Key/steps/any/config/expr')).toEqual({ stepId: 'any', edgeId: undefined, field: 'expr' })
    expect(parseIssuePath('/modules/0/flows/Key/edges/e4')).toEqual({ stepId: undefined, edgeId: 'e4', field: undefined })
    expect(parseIssuePath('/modules/0/flows/Key/steps').stepId).toBeUndefined()
  })
  it('groups issues by where they belong', () => {
    const g = groupIssues([at('/m/flows/K/steps/a/config/x'), at('/m/flows/K/steps/a'), at('/m/flows/K/edges/e1'), at('/m/flows/K/steps')])
    expect(g.byStep.get('a')?.length).toBe(2)
    expect(g.byEdge.get('e1')?.length).toBe(1)
    expect(g.general.length).toBe(1)
  })
})

describe('conversion', () => {
  it('derives one node per step and one edge per connection, losslessly', () => {
    const nodes = flowToNodes(sampleFlow, new Map())
    const edges = flowToEdges(sampleFlow, new Map())
    expect(nodes.map((n) => n.id)).toEqual(sampleFlow.steps.map((s) => s.id))
    expect(nodes.every((n) => n.type === 'step')).toBe(true)
    expect(nodes.find((n) => n.id === 'start')?.deletable).toBe(false)
    expect(edges.map((e) => [e.source, e.sourceHandle, e.target])).toEqual(sampleFlow.edges.map((e) => [e.source, e.handle, e.target]))
    expect(edges.find((e) => e.id === 'e3')?.label).toBe('true') // branching outputs are labelled
    expect(edges.find((e) => e.id === 'e1')?.label).toBeUndefined()
    expect(nodes.map((n) => n.data.step)).toEqual(sampleFlow.steps) // the step objects are carried, not copied
    expect(nodePositions(nodes)['find']).toEqual(sampleFlow.steps.find((s) => s.id === 'find')?.position)
  })
  it('keeps React Flow state (measurement, selection) when re-deriving', () => {
    const first = flowToNodes(sampleFlow, new Map())
    const measured = first.map((n) => ({ ...n, measured: { width: 160, height: 60 }, selected: n.id === 'find' }))
    const again = flowToNodes({ ...sampleFlow }, new Map(), measured)
    expect(again.find((n) => n.id === 'find')?.selected).toBe(true)
    expect(again.find((n) => n.id === 'find')?.measured).toEqual({ width: 160, height: 60 })
  })
})

describe('run marks', () => {
  it('counts how often each of this flow’s steps ran and flags failures', async () => {
    const { runMarks } = await import('./convert')
    const span = (step: string, status = 'ok', flow = 'core.ReorderLowStock') => ({ flow, step, status })
    const marks = runMarks(sampleFlow, [span('find'), span('bump'), span('bump'), span('none', 'error'), span('find', 'ok', 'core.Other')])
    expect(marks.get('bump')).toEqual({ count: 2, error: false })
    expect(marks.get('none')).toEqual({ count: 1, error: true })
    expect(marks.get('find')).toEqual({ count: 1, error: false }) // a step with the same id in another flow is ignored
    expect(marks.has('done')).toBe(false)
  })
})
