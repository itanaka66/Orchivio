import { useState } from 'react'
import { ApiError, type ApiClient } from '../api/client'
import { Danger, Feedback, Field, Loading, Panel, Pill, useAction, useApi, when } from './kit'

interface Branch { id: string; name: string; status: string; head_revision_id: string | null; created_at: string }
interface Revision { id: string; revision_no: number; message?: string | null; created_at: string; branch?: string }
interface Conflict { id?: string; element?: string; path?: string; kind?: string; message?: string; [k: string]: unknown }
interface MergeResult { status: string; revision_no: number | null; conflicts_resolved: number; level: string | null; changes: { kind?: string; path?: string; summary?: string }[] }

const CHOICES = ['ours', 'theirs', 'keep', 'drop']

export function Branches({ client, appId }: { client: ApiClient; appId: string }) {
  const branches = useApi(() => client.request<Branch[]>('GET', `/applications/${appId}/branches`), [client, appId])
  const revisions = useApi(() => client.request<{ items: Revision[] }>('GET', `/applications/${appId}/revisions?limit=50`), [client, appId])
  const { run, error, notice } = useAction(branches.reload)
  const [name, setName] = useState('')
  const [from, setFrom] = useState('')
  const [target, setTarget] = useState<Branch | null>(null)
  const [conflicts, setConflicts] = useState<Conflict[] | null>(null)
  const [choices, setChoices] = useState<Record<string, string>>({})
  const [result, setResult] = useState<MergeResult | null>(null)
  const [acknowledge, setAcknowledge] = useState(false)
  const [mergeError, setMergeError] = useState('')

  const merge = async (b: Branch, dry: boolean) => {
    setMergeError('')
    setResult(null)
    try {
      const r = await client.request<MergeResult>('POST', `/branches/${b.id}/merge`, { into: 'main', dry_run: dry, resolutions: choices, acknowledge_risks: acknowledge })
      setResult(r)
      setConflicts(null)
      if (!dry) branches.reload()
    } catch (e) {
      if (e instanceof ApiError && e.code === 'merge_conflicts' && Array.isArray(e.details)) {
        setConflicts(e.details as Conflict[])
        setMergeError(e.message)
      } else setMergeError(e instanceof Error ? e.message : 'Merge failed.')
    }
  }

  return (
    <>
      <Panel title="Branches">
        <Loading loading={branches.loading} error={branches.error} />
        <ul className="list">
          {branches.data?.map((b) => (
            <li key={b.id} className="row">
              <strong>{b.name}</strong> <Pill value={b.status} /> <small className="hint">created {when(b.created_at)}</small>
              {b.name !== 'main' && b.status === 'open' && (
                <>
                  <button type="button" onClick={() => { setTarget(b); setConflicts(null); setResult(null); setMergeError(''); setChoices({}) }}>Merge into main…</button>
                  <Danger label="Abandon" onConfirm={() => run(() => client.request('DELETE', `/branches/${b.id}`), `Branch ${b.name} abandoned.`)} />
                </>
              )}
            </li>
          ))}
        </ul>
        <Feedback error={error} notice={notice} />
      </Panel>

      <Panel title="New branch">
        <form onSubmit={(e) => {
          e.preventDefault()
          void run(() => client.request('POST', `/applications/${appId}/branches`, { name: name.trim(), ...(from ? { from_revision_id: from } : {}) }), 'Branch created.').then(() => setName(''))
        }}>
          <Field label="Name"><input value={name} maxLength={61} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label="Start from" hint="Defaults to the current revision of main.">
            <select value={from} onChange={(e) => setFrom(e.target.value)}>
              <option value="">Current revision</option>
              {revisions.data?.items.map((r) => <option key={r.id} value={r.id}>r{r.revision_no}{r.message ? ` · ${r.message}` : ''}</option>)}
            </select>
          </Field>
          <button type="submit" className="primary" disabled={!name.trim()}>Create branch</button>
        </form>
      </Panel>

      {target && (
        <Panel title={`Merge ${target.name} into main`}>
          <label className="field check"><input type="checkbox" checked={acknowledge} onChange={(e) => setAcknowledge(e.target.checked)} />
            <span>I have read the risky changes (needed for A3+ changes)</span></label>
          {conflicts && (
            <fieldset>
              <legend>Conflicts need a decision</legend>
              {conflicts.map((c, i) => {
                const id = String(c.id ?? c.element ?? i)
                return (
                  <label key={id} className="field"><span>{c.path ?? c.element ?? id}{c.message ? ` — ${c.message}` : ''}</span>
                    <select aria-label={`Resolution for ${id}`} value={choices[id] ?? ''} onChange={(e) => setChoices((s) => ({ ...s, [id]: e.target.value }))}>
                      <option value="">Choose…</option>{CHOICES.map((x) => <option key={x} value={x}>{x}</option>)}
                    </select></label>
                )
              })}
            </fieldset>
          )}
          <div className="toolbar">
            <button type="button" onClick={() => void merge(target, true)}>Preview merge</button>
            <button type="button" className="primary" onClick={() => void merge(target, false)}>Merge</button>
            <button type="button" onClick={() => setTarget(null)}>Close</button>
          </div>
          {mergeError && <p role="alert" className="sev-error">{mergeError}</p>}
          {result && (
            <div role="status">
              <p><Pill value={result.status} /> {result.revision_no ? `revision ${result.revision_no}` : 'no revision created'} · {result.changes.length} change{result.changes.length === 1 ? '' : 's'}{result.level ? ` · level ${result.level}` : ''}</p>
              <ul className="list">{result.changes.slice(0, 30).map((c, i) => <li key={i}>{c.summary ?? `${c.kind ?? ''} ${c.path ?? ''}`}</li>)}</ul>
            </div>
          )}
        </Panel>
      )}
    </>
  )
}
