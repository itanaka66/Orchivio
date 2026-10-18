// Model elements a requirement or story can be traced to, read from a revision's model.
export interface ElementRef { id: string; label: string }

const SKIP = new Set(['element_id'])
const singular = (k: string) => (k.endsWith('ies') ? `${k.slice(0, -3)}y` : k.endsWith('s') ? k.slice(0, -1) : k)

/** Every object in the model that has an `element_id`, labelled with its kind (the property it hangs from) and name or key. */
export function collectElements(model: unknown): ElementRef[] {
  const out: ElementRef[] = []
  const seen = new Set<string>()
  const walk = (node: unknown, kind: string, path: string[]): void => {
    if (Array.isArray(node)) {
      node.forEach((child) => walk(child, kind, path))
      return
    }
    if (node === null || typeof node !== 'object') return
    const rec = node as Record<string, unknown>
    const id = rec.element_id
    const name = typeof rec.name === 'string' ? rec.name : typeof rec.key === 'string' ? rec.key : undefined
    const here = name ? [...path, name] : path
    if (typeof id === 'string' && id && !seen.has(id)) {
      seen.add(id)
      out.push({ id, label: `${kind} ${here.join('.') || id}` })
    }
    for (const [k, v] of Object.entries(rec)) if (!SKIP.has(k)) walk(v, singular(k), here)
  }
  walk(model, 'model', [])
  return out
}
