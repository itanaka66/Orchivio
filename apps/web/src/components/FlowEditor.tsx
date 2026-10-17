import {
  Background, Controls, MiniMap, ReactFlow, ReactFlowProvider, applyNodeChanges, useReactFlow,
  type Connection, type EdgeChange, type NodeChange,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent } from 'react'
import { STEP_META, stepTitle } from '../flow/catalog'
import { flowToEdges, flowToNodes, type StepNode } from '../flow/convert'
import { canRedo, canUndo, commit, initial, push, redo, replace, undo, type History } from '../flow/history'
import { errorCount, groupIssues } from '../flow/issues'
import { layout, placeMissing } from '../flow/layout'
import {
  addStep, connect, moveSteps, removeEdges, removeSteps, setConfig, setNext, updateMeta, updateStep,
} from '../flow/ops'
import type { Flow, FlowParam, Handle, Issue, ModelView, StepType } from '../flow/types'
import { FlowInspector, StepInspector } from './Inspector'
import { DRAG_TYPE, Palette } from './Palette'
import { StepNodeComponent } from './StepNode'

const nodeTypes = { step: StepNodeComponent }
const TEXT_EDIT_WINDOW_MS = 1000

export interface FlowEditorProps {
  flow: Flow
  model: ModelView
  readOnly?: boolean
  /** Server-side validation of the whole flow; omit to edit without live validation. */
  validate?: (flow: Flow) => Promise<Issue[]>
  /** Called by the Save button. May return a message to show. */
  onSave?: (flow: Flow) => Promise<string | void> | string | void
  saveLabel?: string
}

type Validation = 'off' | 'pending' | 'done' | 'unavailable'

export function FlowEditor(props: FlowEditorProps) {
  return (
    <ReactFlowProvider>
      <Editor {...props} />
    </ReactFlowProvider>
  )
}

const semantic = (f: Flow) => JSON.stringify({ ...f, steps: f.steps.map(({ position: _p, ...s }) => s) })

function Editor({ flow: initialFlow, model, readOnly, validate, onSave, saveLabel = 'Save as proposal' }: FlowEditorProps) {
  const rf = useReactFlow<StepNode>()
  const [hist, setHist] = useState<History<Flow>>(() => initial(placeMissing(initialFlow)))
  const flow = hist.present
  const flowRef = useRef(flow)
  flowRef.current = flow
  const savedKey = useRef(JSON.stringify(hist.present))
  const [issues, setIssues] = useState<Issue[]>([])
  const [validation, setValidation] = useState<Validation>(validate ? 'pending' : 'off')
  const [selectedSteps, setSelectedSteps] = useState<ReadonlySet<string>>(new Set())
  const [selectedEdges, setSelectedEdges] = useState<ReadonlySet<string>>(new Set())
  const [status, setStatus] = useState('')
  const [saving, setSaving] = useState(false)
  const wrapper = useRef<HTMLDivElement>(null)
  const lastText = useRef<{ key: string; at: number } | null>(null)
  const dragBase = useRef<Flow | null>(null)

  const grouped = useMemo(() => groupIssues(issues), [issues])
  const edges = useMemo(() => flowToEdges(flow, grouped.byEdge, selectedEdges), [flow, grouped, selectedEdges])
  const [nodes, setNodes] = useState<StepNode[]>(() => flowToNodes(flow, grouped.byStep))

  // The flow is the source of truth: re-derive nodes whenever it, the issues or the selection change,
  // keeping React Flow's own state (measurement) for nodes that already exist.
  useEffect(() => {
    setNodes((prev) => flowToNodes(flow, grouped.byStep, prev).map((n) => ({ ...n, selected: selectedSteps.has(n.id) })))
  }, [flow, grouped, selectedSteps])

  // --- live validation -------------------------------------------------------------------------
  const seq = useRef(0)
  const key = useMemo(() => semantic(flow), [flow]) // layout changes do not need a server round-trip
  useEffect(() => {
    if (!validate) return
    const mine = ++seq.current
    setValidation('pending')
    const timer = setTimeout(() => {
      validate(flowRef.current).then(
        (found) => { if (mine === seq.current) { setIssues(found); setValidation('done') } },
        () => { if (mine === seq.current) setValidation('unavailable') },
      )
    }, 400)
    return () => clearTimeout(timer)
  }, [key, validate])

  // --- editing helpers -------------------------------------------------------------------------
  const say = useCallback((m: string) => setStatus(m), [])
  const change = useCallback((next: Flow, message?: string) => {
    setHist((h) => push(h, next))
    lastText.current = null
    if (message) say(message)
  }, [say])

  const centre = useCallback((offset: number) => {
    const box = wrapper.current?.getBoundingClientRect()
    return rf.screenToFlowPosition({ x: (box?.left ?? 0) + (box?.width ?? 400) / 2 + offset, y: (box?.top ?? 0) + (box?.height ?? 300) / 2 + offset })
  }, [rf])

  const add = useCallback((type: StepType, at?: { x: number; y: number }) => {
    const r = addStep(flowRef.current, type, at ?? centre((flowRef.current.steps.length % 6) * 24))
    if (!r.ok) return say(r.error)
    change(r.flow, `Added ${STEP_META[type].label} step ${r.id}. Choose what comes before and after it in the panel on the right.`)
    setSelectedSteps(new Set([r.id]))
  }, [centre, change, say])

  const doConnect = useCallback((source: string, handle: Handle, target: string) => {
    const r = connect(flowRef.current, source, handle, target)
    if (!r.ok) return say(r.error)
    change(r.flow, `Connected ${source} (${handle}) to ${target}.`)
  }, [change, say])

  const onConnect = useCallback((c: Connection) => {
    if (c.source && c.target && c.sourceHandle) doConnect(c.source, c.sourceHandle as Handle, c.target)
  }, [doConnect])

  const isValidConnection = useCallback((c: Connection | { source: string; target: string; sourceHandle?: string | null }) => (
    !!c.sourceHandle && connect(flowRef.current, c.source, c.sourceHandle as Handle, c.target).ok
  ), [])

  const commitPositions = useCallback((positions: Record<string, { x: number; y: number }>) => {
    setHist((h) => push(h, moveSteps(h.present, positions)))
  }, [])

  const onNodesChange = useCallback((changes: NodeChange<StepNode>[]) => {
    setNodes((ns) => applyNodeChanges(changes.filter((c) => c.type !== 'remove'), ns))
    const removed = changes.flatMap((c) => (c.type === 'remove' ? [c.id] : []))
    if (removed.length) {
      change(removeSteps(flowRef.current, removed), `Removed ${removed.length} step${removed.length > 1 ? 's' : ''}.`)
      setSelectedSteps((s) => new Set([...s].filter((id) => !removed.includes(id))))
    }
    const picks = changes.flatMap((c) => (c.type === 'select' ? [c] : []))
    if (picks.length) {
      setSelectedSteps((s) => {
        const next = new Set(s)
        for (const c of picks) c.selected ? next.add(c.id) : next.delete(c.id)
        return next
      })
    }
    const placed = changes.flatMap((c) => (c.type === 'position' && c.dragging !== true && c.position ? [[c.id, c.position] as const] : []))
    if (placed.length) commitPositions(Object.fromEntries(placed))
  }, [change, commitPositions])

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    const removed = changes.flatMap((c) => (c.type === 'remove' ? [c.id] : []))
    if (removed.length) change(removeEdges(flowRef.current, removed), `Removed ${removed.length} connection${removed.length > 1 ? 's' : ''}.`)
    const picks = changes.flatMap((c) => (c.type === 'select' ? [c] : []))
    if (picks.length) {
      setSelectedEdges((s) => {
        const next = new Set(s)
        for (const c of picks) c.selected ? next.add(c.id) : next.delete(c.id)
        return next
      })
    }
  }, [change])

  const onDrop = useCallback((e: DragEvent) => {
    e.preventDefault()
    const type = e.dataTransfer.getData(DRAG_TYPE) as StepType
    if (type && STEP_META[type]) add(type, rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }))
  }, [add, rf])

  const selectedId = selectedSteps.size === 1 ? [...selectedSteps][0] : undefined
  const selected = flow.steps.find((s) => s.id === selectedId)
  const errors = errorCount(issues)
  const dirty = JSON.stringify(flow) !== savedKey.current

  /** Text edits within a short window collapse into one undo step. */
  const typed = useCallback((id: string, name: string, make: (f: Flow) => Flow) => {
    const k = `${id}.${name}`
    const now = Date.now()
    const merge = lastText.current?.key === k && now - lastText.current.at < TEXT_EDIT_WINDOW_MS
    setHist((h) => (merge ? replace(h, make(h.present)) : push(h, make(h.present))))
    lastText.current = { key: k, at: now }
  }, [])

  const save = async () => {
    if (!onSave) return
    setSaving(true)
    try {
      const msg = await onSave(flow)
      savedKey.current = JSON.stringify(flow)
      say(typeof msg === 'string' ? msg : 'Saved.')
    } catch (e) {
      say(e instanceof Error ? `Could not save: ${e.message}` : 'Could not save.')
    } finally {
      setSaving(false)
    }
  }

  const onKeyDown = (e: KeyboardEvent) => {
    const t = e.target as HTMLElement
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName)) return // keep the browser's own text undo
    const mod = e.ctrlKey || e.metaKey
    if (mod && e.key.toLowerCase() === 'z') {
      e.preventDefault()
      setHist((h) => (e.shiftKey ? redo(h) : undo(h)))
    } else if (mod && e.key.toLowerCase() === 'y') {
      e.preventDefault()
      setHist(redo)
    }
  }

  const goTo = (id: string) => {
    setSelectedSteps(new Set([id]))
    void rf.fitView({ nodes: [{ id }], duration: 200, maxZoom: 1.2 })
  }

  const download = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(flow, null, 2)], { type: 'application/json' }))
    const a = Object.assign(document.createElement('a'), { href: url, download: `${flow.key}.flow.json` })
    a.click()
    URL.revokeObjectURL(url)
  }

  const validationText = {
    off: 'Live validation is not connected.',
    pending: 'Checking…',
    unavailable: 'Validation is unavailable; the server will check when you save.',
    done: errors ? `${errors} error${errors > 1 ? 's' : ''} to fix.` : issues.length ? `No errors, ${issues.length} warning${issues.length > 1 ? 's' : ''}.` : 'No problems found.',
  }[validation]

  return (
    <div className="editor" onKeyDown={onKeyDown}>
      <div className="toolbar" role="toolbar" aria-label="Flow actions">
        <strong className="flow-name">{flow.key}</strong>
        <button type="button" onClick={() => setHist(undo)} disabled={readOnly || !canUndo(hist)}>Undo</button>
        <button type="button" onClick={() => setHist(redo)} disabled={readOnly || !canRedo(hist)}>Redo</button>
        <button type="button" disabled={readOnly} onClick={() => { change(moveSteps(flow, layout(flow)), 'Arranged the steps.'); setTimeout(() => void rf.fitView({ duration: 200 }), 0) }}>Auto-arrange</button>
        <button type="button" onClick={() => void rf.fitView({ duration: 200 })}>Fit to screen</button>
        <button type="button" onClick={download}>Download JSON</button>
        <span className="spacer" />
        <span className={`validation validation-${validation}${errors ? ' has-errors' : ''}`}>{validationText}</span>
        {onSave && (
          <button type="button" className="primary" onClick={save}
                  disabled={readOnly || saving || !dirty || errors > 0 || validation === 'pending'}
                  title={errors ? 'Fix the errors first' : !dirty ? 'No changes to save' : undefined}>
            {saving ? 'Saving…' : saveLabel}
          </button>
        )}
      </div>

      <div className="editor-body">
        <Palette kind={flow.kind} onAdd={(t) => add(t)} disabled={readOnly} />

        <div className="canvas" ref={wrapper} onDrop={onDrop} onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }}>
          <ReactFlow
            aria-label={`Flow diagram for ${flow.key}`}
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            isValidConnection={isValidConnection}
            onNodeDragStart={() => { dragBase.current = flowRef.current }}
            onNodeDragStop={() => {
              const base = dragBase.current
              dragBase.current = null
              if (!base) return
              const moved: Record<string, { x: number; y: number }> = {}
              for (const n of rf.getNodes()) moved[n.id] = n.position
              setHist((h) => commit(replace(h, moveSteps(h.present, moved)), base))
            }}
            fitView
            minZoom={0.2}
            nodesDraggable={!readOnly}
            nodesConnectable={!readOnly}
            deleteKeyCode={readOnly ? null : ['Backspace', 'Delete']}
            nodesFocusable
            edgesFocusable
            proOptions={{ hideAttribution: false }}
          >
            <Background gap={20} />
            <Controls />
            <MiniMap pannable zoomable ariaLabel="Overview of the flow" />
          </ReactFlow>
        </div>

        <aside className="side">
          {selected ? (
            <StepInspector
              flow={flow} step={selected} model={model} readOnly={readOnly}
              issues={grouped.byStep.get(selected.id) ?? []}
              onLabel={(l) => typed(selected.id, 'label', (f) => updateStep(f, selected.id, { label: l }))}
              onConfig={(name, v) => typed(selected.id, name, (f) => setConfig(f, selected.id, name, v))}
              onConfigs={(patch) => change(Object.entries(patch).reduce((f, [k, v]) => setConfig(f, selected.id, k, v), flow))}
              onNext={(h, target) => {
                const r = setNext(flow, selected.id, h, target)
                if (r.ok) change(r.flow, target ? `${selected.id} (${h}) now continues to ${target}.` : `${selected.id} (${h}) no longer continues anywhere.`)
                else say(r.error)
              }}
              onDelete={() => { change(removeSteps(flow, [selected.id]), `Deleted ${stepTitle(selected)}.`); setSelectedSteps(new Set()) }}
            />
          ) : (
            <FlowInspector
              flow={flow} readOnly={readOnly}
              onDescription={(d) => typed('flow', 'description', (f) => updateMeta(f, { description: d }))}
              onParams={(p: FlowParam[]) => change(updateMeta(flow, { params: p }))}
            />
          )}

          <section className="problems" aria-label="Problems">
            <h2>Problems <span className="count">{issues.length}</span></h2>
            {issues.length === 0 ? <p className="hint">{validationText}</p> : (
              <ul>
                {issues.map((i, n) => {
                  const sid = /\/steps\/([a-z][a-z0-9_]*)/.exec(i.path)?.[1]
                  return (
                    <li key={n} className={`sev-${i.severity}`}>
                      <span className="sev">{i.severity}</span> {i.message}
                      {sid && flow.steps.some((s) => s.id === sid) && <button type="button" className="link" onClick={() => goTo(sid)}>Go to {sid}</button>}
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </aside>
      </div>

      <div className="status" role="status" aria-live="polite">{status}</div>
      <p className="sr-only">
        Keyboard: Tab moves between steps; arrow keys move the selected step; Backspace or Delete removes it.
        Use the panel on the right to choose what each step does and where it continues.
        Press Ctrl+Z to undo and Ctrl+Shift+Z to redo.
      </p>
    </div>
  )
}
