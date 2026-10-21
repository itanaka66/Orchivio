import { useState } from 'react'
import type { ApiClient } from '../api/client'

type Json = Record<string, unknown>
interface Field { name: string; type: string; primary_key?: boolean; generated?: boolean; computed?: unknown }
interface Entity { name: string; display_name?: string; fields: Field[] }
interface PageDef {
  key: string; path: string; type: 'list' | 'form' | 'detail'; entity: string
  mode?: 'create' | 'edit'; columns?: string[]; actions?: string[]
}
interface Module { key: string; entities?: Entity[]; pages?: unknown[] }

const STD_ACTIONS = ['create', 'edit', 'delete', 'view'] as const

function withPages(model: Json, moduleKey: string, pages: PageDef[]): Json {
  return {
    ...model,
    modules: ((model.modules ?? []) as Module[]).map((m) => m.key === moduleKey ? { ...m, pages } : m),
  }
}

interface Props { client: ApiClient; applicationId: string; model: Json; canEdit: boolean }

export function PageDesigner({ client, applicationId, model, canEdit }: Props) {
  const modules = (model.modules ?? []) as Module[]
  const mod = modules.find((m) => (m.entities ?? []).length > 0) ?? modules[0]
  const entities = (mod?.entities ?? []) as Entity[]
  // normalize each action to a string; object-form actions (flow bindings) are kept verbatim
  const rawPages = (mod?.pages ?? []) as Array<PageDef & { actions?: (string | { key: string })[] }>
  const pages: PageDef[] = rawPages.map((p) => ({
    ...p,
    actions: (p.actions ?? []).map((a): string => (typeof a === 'string' ? a : (a as { key: string }).key)),
  }))

  const [draft, setDraft] = useState<PageDef | null>(null)
  const [isNew, setIsNew] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  if (!mod) return <p className="hint">No modules found in this model.</p>

  const submit = async (newPages: PageDef[], title: string) => {
    setBusy(true); setError('')
    try {
      await client.createProposal(applicationId, withPages(model, mod.key, newPages), title)
      setNotice(`Proposal submitted: "${title}"`)
      setDraft(null)
    } catch (e) { setError(e instanceof Error ? e.message : 'Failed to submit proposal.') }
    finally { setBusy(false) }
  }

  const startNew = () => {
    setDraft({ key: '', path: '', type: 'list', entity: entities[0]?.name ?? '', columns: [], actions: ['create', 'edit', 'delete'] })
    setIsNew(true); setNotice(''); setError('')
  }

  const startEdit = (p: PageDef) => {
    setDraft({ ...p })
    setIsNew(false); setNotice(''); setError('')
  }

  const deletePage = (p: PageDef) => {
    if (!window.confirm(`Delete page "${p.key}"? A proposal will be submitted.`)) return
    void submit(pages.filter((x) => x.key !== p.key), `Delete page ${p.key}`)
  }

  const save = () => {
    if (!draft) return
    if (!draft.key.trim()) { setError('Page key is required.'); return }
    if (!draft.path.trim()) { setError('Path is required.'); return }
    if (!draft.entity) { setError('Entity is required.'); return }
    if (isNew && pages.some((p) => p.key === draft.key)) { setError(`A page named "${draft.key}" already exists.`); return }
    const newPages = isNew ? [...pages, draft] : pages.map((p) => (p.key === draft.key ? draft : p))
    void submit(newPages, `${isNew ? 'Add' : 'Edit'} page ${draft.key}`)
  }

  const draftEntity = draft ? entities.find((e) => e.name === draft.entity) : null
  const fields = draftEntity?.fields ?? []

  return (
    <div className="page-designer">
      <div className="designer-list card">
        <div className="panel-head"><h2>Pages</h2></div>
        {pages.length === 0 && <p className="hint">No pages yet. Create one below.</p>}
        <ul className="list">
          {pages.map((p) => (
            <li key={p.key}>
              <button type="button" className="link" aria-current={draft?.key === p.key && !isNew ? true : undefined}
                onClick={() => startEdit(p)}>
                <span className="badge">{p.type}</span>{' '}{p.key}
                <small> · {p.entity}</small>
              </button>
              {canEdit && (
                <button type="button" className="link" aria-label={`Delete page ${p.key}`}
                  onClick={() => deletePage(p)}>Delete</button>
              )}
            </li>
          ))}
        </ul>
        {canEdit && <p><button type="button" className="primary" onClick={startNew}>New page</button></p>}
      </div>

      <div className="designer-edit">
        {error && <p role="alert" className="sev-error">{error}</p>}
        {notice && <p role="status" className="hint">{notice}</p>}
        {draft ? (
          <section className="card" aria-label={isNew ? 'New page' : `Edit page ${draft.key}`}>
            <div className="panel-head"><h2>{isNew ? 'New page' : `Edit ${draft.key}`}</h2></div>
            <form onSubmit={(e) => { e.preventDefault(); save() }}>
              {isNew ? (
                <>
                  <label className="field"><span>Key</span>
                    <input required value={draft.key} placeholder="product-list"
                      onChange={(e) => setDraft({ ...draft, key: e.target.value })} /></label>
                  <label className="field"><span>Path</span>
                    <input required value={draft.path} placeholder="/products"
                      onChange={(e) => setDraft({ ...draft, path: e.target.value })} /></label>
                  <label className="field"><span>Type</span>
                    <select value={draft.type} onChange={(e) => setDraft({ ...draft, type: e.target.value as PageDef['type'] })}>
                      <option value="list">list</option>
                      <option value="form">form</option>
                      <option value="detail">detail</option>
                    </select></label>
                  <label className="field"><span>Entity</span>
                    <select value={draft.entity} onChange={(e) => setDraft({ ...draft, entity: e.target.value })}>
                      {entities.map((ent) => <option key={ent.name} value={ent.name}>{ent.display_name ?? ent.name}</option>)}
                    </select></label>
                  {draft.type === 'form' && (
                    <label className="field"><span>Mode</span>
                      <select value={draft.mode ?? 'create'} onChange={(e) => setDraft({ ...draft, mode: e.target.value as 'create' | 'edit' })}>
                        <option value="create">create</option>
                        <option value="edit">edit</option>
                      </select></label>
                  )}
                </>
              ) : (
                <p className="hint">
                  <span className="badge">{draft.type}</span>{' '}
                  {draft.entity}{draft.mode ? ` (${draft.mode})` : ''} · {draft.path}
                </p>
              )}

              {draft.type !== 'form' && (
                <fieldset>
                  <legend>Columns{draft.columns?.length ? ` (${draft.columns.length} selected)` : ''}</legend>
                  {fields.length === 0 && <p className="hint">No fields.</p>}
                  {fields.map((f) => (
                    <label key={f.name} className="checkbox">
                      <input type="checkbox"
                        checked={(draft.columns ?? []).includes(f.name)}
                        onChange={(ev) => {
                          const cols = draft.columns ?? []
                          setDraft({ ...draft, columns: ev.target.checked ? [...cols, f.name] : cols.filter((c) => c !== f.name) })
                        }} />
                      {' '}{f.name} <small>({f.type})</small>
                    </label>
                  ))}
                </fieldset>
              )}

              {draft.type === 'list' && (
                <fieldset>
                  <legend>Actions</legend>
                  {STD_ACTIONS.map((a) => (
                    <label key={a} className="checkbox">
                      <input type="checkbox"
                        checked={(draft.actions ?? []).includes(a)}
                        onChange={(ev) => {
                          const acts = draft.actions ?? []
                          setDraft({ ...draft, actions: ev.target.checked ? [...acts, a] : acts.filter((x) => x !== a) })
                        }} />
                      {' '}{a}
                    </label>
                  ))}
                </fieldset>
              )}

              <div className="toolbar">
                <button type="submit" className="primary" disabled={busy || !canEdit}>
                  {busy ? 'Submitting…' : 'Submit as proposal'}
                </button>
                <button type="button" onClick={() => { setDraft(null); setError('') }}>Cancel</button>
              </div>
            </form>
          </section>
        ) : (
          <p className="hint">Select a page from the list to edit it, or create a new one.</p>
        )}
      </div>
    </div>
  )
}
