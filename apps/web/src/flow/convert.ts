// Flow <-> React Flow. The Flow is the single source of truth; nodes and edges are a view of it.
// React Flow adds measurement and selection state to nodes, which is kept across re-derivations.
import type { Edge, Node } from '@xyflow/react'
import { HANDLE_LABEL, handlesOf } from './catalog'
import type { Flow, FlowEdge, FlowStep, Issue } from './types'

export interface StepNodeData extends Record<string, unknown> {
  step: FlowStep
  issues: Issue[]
}
export type StepNode = Node<StepNodeData, 'step'>
export type FlowRFEdge = Edge<{ edge: FlowEdge; issues: Issue[] }>

export function stepToNode(step: FlowStep, issues: Issue[], prev?: StepNode): StepNode {
  return {
    ...prev,
    id: step.id,
    type: 'step',
    // While a node is being dragged React Flow owns its position; the flow catches up on drop.
    position: prev?.dragging ? prev.position : (step.position ?? prev?.position ?? { x: 0, y: 0 }),
    data: { step, issues },
    deletable: step.type !== 'start',
  }
}

/** Derive nodes from the flow, preserving React Flow's own state (measurement, selection). */
export function flowToNodes(flow: Flow, issuesByStep: Map<string, Issue[]>, prev: StepNode[] = []): StepNode[] {
  const before = new Map(prev.map((n) => [n.id, n]))
  return flow.steps.map((s) => stepToNode(s, issuesByStep.get(s.id) ?? [], before.get(s.id)))
}

export function flowToEdges(flow: Flow, issuesByEdge: Map<string, Issue[]>, selected: ReadonlySet<string> = new Set()): FlowRFEdge[] {
  const branching = new Map(flow.steps.map((s) => [s.id, handlesOf(s.type).length > 1]))
  return flow.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.handle,
    targetHandle: 'in',
    label: branching.get(e.source) ? HANDLE_LABEL[e.handle] : undefined,
    selected: selected.has(e.id),
    ariaLabel: `Connection from ${e.source} (${HANDLE_LABEL[e.handle]}) to ${e.target}`,
    className: (issuesByEdge.get(e.id) ?? []).some((i) => i.severity === 'error') ? 'edge-error' : undefined,
    data: { edge: e, issues: issuesByEdge.get(e.id) ?? [] },
  }))
}

/** Positions from the nodes back into the flow (used when a drag ends). */
export function nodePositions(nodes: StepNode[]): Record<string, { x: number; y: number }> {
  return Object.fromEntries(nodes.map((n) => [n.id, { x: n.position.x, y: n.position.y }]))
}
