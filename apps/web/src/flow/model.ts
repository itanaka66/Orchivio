// Helpers over the application model JSON as returned by the API (a stored revision).
import type { Flow, ModelView } from './types'

type Json = Record<string, any> // eslint-disable-line @typescript-eslint/no-explicit-any

export interface FlowRef { module: string; flow: Flow }

export function listFlows(model: Json): FlowRef[] {
  return (model.modules ?? []).flatMap((m: Json) => (m.flows ?? []).map((f: Flow) => ({ module: m.key as string, flow: f })))
}

/** Entities and flows, in the shape the inspector's pick-lists need. */
export function viewOf(model: Json, currentModule: string): ModelView {
  const entities: ModelView['entities'] = []
  const flows: ModelView['flows'] = []
  for (const m of (model.modules ?? []) as Json[]) {
    for (const e of (m.entities ?? []) as Json[]) {
      if (e.transient) continue
      entities.push({
        qualified: m.key === currentModule ? e.name : `${m.key}.${e.name}`,
        name: e.name,
        fields: (e.fields ?? []).map((f: Json) => ({
          name: f.name, type: f.type, editable: !(f.generated || f.computed || f.primary_key),
        })),
      })
    }
    for (const f of (m.flows ?? []) as Json[]) {
      flows.push({ key: m.key === currentModule ? f.key : `${m.key}.${f.key}`, kind: f.kind ?? 'server', params: f.params ?? [] })
    }
  }
  return { entities, flows }
}

/** A copy of ``model`` with ``flow`` stored in ``module`` (replacing the flow with the same key). */
export function withFlow(model: Json, module: string, flow: Flow): Json {
  const modules = (model.modules ?? []) as Json[]
  if (!modules.some((m) => m.key === module)) throw new Error(`unknown module ${module}`)
  return {
    ...model,
    modules: modules.map((m) => {
      if (m.key !== module) return m
      const flows = (m.flows ?? []) as Flow[]
      const i = flows.findIndex((f) => f.key === flow.key)
      return { ...m, flows: i === -1 ? [...flows, flow] : flows.map((f, j) => (j === i ? flow : f)) }
    }),
  }
}

/** Which entity does the record (or list) variable ``name`` hold? Follows loops back to retrieves. */
export function entityOfVariable(flow: Flow, name: string): string | undefined {
  for (const s of flow.steps) {
    if ((s.type === 'retrieve' || s.type === 'create') && s.config.into === name && typeof s.config.entity === 'string') {
      return s.config.entity
    }
    if (s.type === 'loop' && s.config.item === name && typeof s.config.over === 'string') {
      return entityOfVariable(flow, s.config.over)
    }
  }
  return undefined
}
