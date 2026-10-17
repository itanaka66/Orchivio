import type { Issue } from './types'

export interface ParsedPath {
  stepId?: string
  edgeId?: string
  /** First config key after ``config/`` (for example ``where`` or ``values``). */
  field?: string
}

/** Server issue paths look like ``/modules/0/flows/Key/steps/<id>/config/<field>[/<sub>]``. */
export function parseIssuePath(path: string): ParsedPath {
  const step = /\/steps\/([a-z][a-z0-9_]*)/.exec(path)?.[1]
  const edge = /\/edges\/([a-z][a-z0-9_]*)/.exec(path)?.[1]
  const field = /\/config\/([A-Za-z0-9_]+)/.exec(path)?.[1]
  return { stepId: step, edgeId: edge, field }
}

export interface GroupedIssues {
  byStep: Map<string, Issue[]>
  byEdge: Map<string, Issue[]>
  general: Issue[]
}

export function groupIssues(issues: Issue[]): GroupedIssues {
  const out: GroupedIssues = { byStep: new Map(), byEdge: new Map(), general: [] }
  for (const i of issues) {
    const { stepId, edgeId } = parseIssuePath(i.path)
    const bucket = stepId ? out.byStep.get(stepId) : edgeId ? out.byEdge.get(edgeId) : undefined
    if (stepId) out.byStep.set(stepId, [...(bucket ?? []), i])
    else if (edgeId) out.byEdge.set(edgeId, [...(bucket ?? []), i])
    else out.general.push(i)
  }
  return out
}

export const errorCount = (issues: Issue[]) => issues.filter((i) => i.severity === 'error').length
