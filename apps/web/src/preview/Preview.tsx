import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError, type ApiClient, type PreviewActor } from '../api/client'
import { FeedbackWidget } from '../board/FeedbackWidget'

type Json = Record<string, unknown>
interface Field { name: string; type: string; element_id?: string; required?: boolean; primary_key?: boolean; generated?: boolean; computed?: unknown; max_length?: number }
interface Entity { name: string; display_name?: string; element_id?: string; fields: Field[] }
interface PageDef { key: string; path: string; type: 'list' | 'form' | 'detail'; entity: string; mode?: 'create' | 'edit'; element_id?: string; columns?: string[]; actions?: (string | { key: string })[] }
interface Enumeration { name: string; values: string[] }
interface Module { key: string; entities?: Entity[]; pages?: PageDef[]; enumerations?: Enumeration[] }

export interface ResolvedPage { module: Module; page: PageDef; entity: Entity }

/** All pages of the model with the entity they show, in model order. Pages whose entity is missing are left out. */
export function resolvePages(model: Json): ResolvedPage[] {
  const out: ResolvedPage[] = []
  for (const module of ((model.modules ?? []) as Module[])) {
    for (const page of module.pages ?? []) {
      const entity = module.entities?.find((e) => e.name === page.entity)
      if (entity) out.push({ module, page, entity })
    }
  }
  return out
}

/** Fields a person types in: not generated, computed or the key. */
export const editable = (e: Entity): Field[] => e.fields.filter((f) => !f.generated && !f.computed && !f.primary_key)
const message = (e: unknown) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : 'Something went wrong.')

interface Props { client: ApiClient; applicationId: string; model: Json; roles: string[]; canEdit: boolean }

/**
 * The application as its end users would see it, rendered from the model and backed by the preview database. Elements carry
 * `data-orchivio-page` / `data-orchivio-element` so the feedback widget can say exactly what a comment is about.
 */
export function Preview({ client, applicationId, model, roles: modelRoles, canEdit }: Props) {
  const pages = useMemo(() => resolvePages(model), [model])
  const [key, setKey] = useState(pages[0]?.page.key ?? '')
  const [user, setUser] = useState('preview-user')
  const [roleText, setRoleText] = useState(modelRoles[0] ?? '')
  const [editing, setEditing] = useState<{ id?: string; values: Record<string, unknown> } | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const current = pages.find((p) => p.page.key === key) ?? pages[0]
  const actor: PreviewActor = useMemo(() => ({ user: user.trim() || 'preview-user', roles: roleText.split(',').map((r) => r.trim()).filter(Boolean) }), [user, roleText])

  if (!current) return <p className="hint">The model has no pages yet.</p>
  const formFor = (entity: string, mode: 'create' | 'edit') => pages.find((p) => p.page.entity === entity && p.page.type === 'form' && p.page.mode === mode)

  return (
    <div className="preview">
      <div className="toolbar" role="toolbar" aria-label="Preview as">
        <label>User <input value={user} maxLength={64} onChange={(e) => setUser(e.target.value)} aria-label="Preview user" /></label>
        <label>Roles <input value={roleText} onChange={(e) => setRoleText(e.target.value)} aria-label="Preview roles" list="preview-roles" />
          <datalist id="preview-roles">{modelRoles.map((r) => <option key={r} value={r} />)}</datalist></label>
        {canEdit && <button type="button" onClick={async () => {
          try { await client.previewReset(applicationId); setNotice('Preview data was reset with sample rows.'); setError(''); setEditing(null) } catch (e) { setError(message(e)) }
        }}>Reset sample data</button>}
      </div>
      <nav aria-label="Pages" className="tabs">
        {pages.filter((p) => p.page.type !== 'form').map((p) => (
          <button key={p.page.key} type="button" aria-current={p.page.key === current.page.key ? 'page' : undefined} className={p.page.key === current.page.key ? 'primary' : ''}
            onClick={() => { setKey(p.page.key); setEditing(null) }}>{p.entity.display_name ?? p.entity.name} · {p.page.type}</button>
        ))}
      </nav>
      {error && <p role="alert" className="sev-error">{error}</p>}
      {notice && <p role="status" className="hint">{notice}</p>}
      <section data-orchivio-page={current.page.key} data-orchivio-element={current.page.element_id} aria-label={`Page ${current.page.key}`} className="card">
        {editing ? (
          <RowForm resolved={formFor(current.entity.name, editing.id ? 'edit' : 'create') ?? current} enums={current.module.enumerations ?? []} editing={editing}
            onCancel={() => setEditing(null)}
            onSave={async (values) => {
              const entity = `${current.module.key}.${current.entity.name}`
              try {
                if (editing.id) await client.previewUpdate(applicationId, entity, editing.id, values, actor)
                else await client.previewCreate(applicationId, entity, values, actor)
                setEditing(null); setError(''); setNotice('Saved.')
              } catch (e) { setError(message(e)) }
            }} />
        ) : current.page.type === 'list'
          ? <RowList client={client} applicationId={applicationId} resolved={current} actor={actor} setError={setError}
              onCreate={canEdit && formFor(current.entity.name, 'create') ? () => setEditing({ values: {} }) : undefined}
              onEdit={canEdit && formFor(current.entity.name, 'edit') ? (row) => setEditing({ id: String(row.id), values: row }) : undefined} />
          : <p className="hint">Open a record from its list.</p>}
      </section>
      <FeedbackWidget client={client} applicationId={applicationId} page={current.page.key} />
    </div>
  )
}

function RowList({ client, applicationId, resolved, actor, setError, onCreate, onEdit }: {
  client: ApiClient; applicationId: string; resolved: ResolvedPage; actor: PreviewActor; setError: (m: string) => void
  onCreate?: () => void; onEdit?: (row: Json) => void
}) {
  const { module, page, entity } = resolved
  const [rows, setRows] = useState<Json[] | null>(null)
  const columns = (page.columns?.length ? page.columns : entity.fields.slice(0, 5).map((f) => f.name))
    .map((n) => entity.fields.find((f) => f.name === n)).filter((f): f is Field => !!f)
  const qualified = `${module.key}.${entity.name}`
  const load = useCallback(async () => {
    setRows(null)
    try { setRows((await client.previewList(applicationId, qualified, actor)).items); setError('') } catch (e) { setRows([]); setError(message(e)) }
  }, [client, applicationId, qualified, actor, setError])
  useEffect(() => { void load() }, [load])
  return (
    <>
      {onCreate && <button type="button" className="primary" onClick={onCreate}>New {entity.display_name ?? entity.name}</button>}
      {rows === null && <p>Loading…</p>}
      {rows?.length === 0 && <p className="hint">No records.</p>}
      {rows && rows.length > 0 && (
        <table className="trace" data-orchivio-element={entity.element_id}>
          <thead><tr>{columns.map((c) => <th key={c.name} data-orchivio-element={c.element_id}>{c.name}</th>)}{onEdit && <th><span className="sr-only">Actions</span></th>}</tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={String(r.id ?? i)}>
                {columns.map((c) => <td key={c.name} data-orchivio-element={c.element_id}>{show(r[c.name])}</td>)}
                {onEdit && <td><button type="button" className="link" aria-label={`Edit row ${i + 1}`} onClick={() => onEdit(r)}>Edit</button></td>}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

const show = (v: unknown): string => (v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v))

function RowForm({ resolved, enums, editing, onSave, onCancel }: {
  resolved: ResolvedPage; enums: Enumeration[]; editing: { id?: string; values: Record<string, unknown> }
  onSave: (values: Record<string, unknown>) => void; onCancel: () => void
}) {
  const { entity, page } = resolved
  const fields = editable(entity)
  const [values, setValues] = useState<Record<string, unknown>>(() => Object.fromEntries(fields.map((f) => [f.name, editing.values[f.name] ?? ''])))
  return (
    <form data-orchivio-element={page.element_id} onSubmit={(e) => {
      e.preventDefault()
      const out: Record<string, unknown> = {}
      for (const f of fields) {
        const v = values[f.name]
        if (v === '' || v === undefined) continue
        out[f.name] = /^(integer|number|decimal)/.test(f.type) ? Number(v) : v
      }
      onSave(out)
    }}>
      <h2>{editing.id ? 'Edit' : 'New'} {entity.display_name ?? entity.name}</h2>
      {fields.map((f) => {
        const en = f.type.startsWith('enum:') ? enums.find((x) => x.name === f.type.slice(5)) : undefined
        const set = (v: unknown) => setValues((s) => ({ ...s, [f.name]: v }))
        return (
          <label key={f.name} className={f.type === 'boolean' ? 'field check' : 'field'} data-orchivio-element={f.element_id}>
            <span>{f.name}{f.required ? ' *' : ''}</span>
            {f.type === 'boolean'
              ? <input type="checkbox" checked={values[f.name] === true} onChange={(e) => set(e.target.checked)} />
              : en
                ? <select value={String(values[f.name] ?? '')} onChange={(e) => set(e.target.value)}><option value="">—</option>{en.values.map((v) => <option key={v} value={v}>{v}</option>)}</select>
                : <input value={String(values[f.name] ?? '')} maxLength={f.max_length} inputMode={/^(integer|number|decimal)/.test(f.type) ? 'numeric' : undefined}
                    required={f.required && !f.type.startsWith('ref:')} onChange={(e) => set(e.target.value)} />}
          </label>
        )
      })}
      <div className="toolbar"><button type="submit" className="primary">Save</button><button type="button" onClick={onCancel}>Cancel</button></div>
    </form>
  )
}
