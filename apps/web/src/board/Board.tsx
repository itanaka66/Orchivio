import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ApiError, type ApiClient, type FeedbackItem, type Requirement, type RequirementStatus, type Story, type StoryStatus,
  type TraceLink, type Traceability,
} from '../api/client'
import { useOffline } from '../offline/OfflineProvider'
import type { OutboxItem } from '../offline/outbox'
import type { ElementRef } from './elements'

export const COLUMNS: { status: StoryStatus; title: string }[] = [
  { status: 'proposed', title: 'Proposed' }, { status: 'backlog', title: 'Backlog' }, { status: 'ready', title: 'Ready' },
  { status: 'in_progress', title: 'In progress' }, { status: 'done', title: 'Done' }, { status: 'rejected', title: 'Rejected' },
]
const REQUIREMENT_STATUSES: RequirementStatus[] = ['draft', 'accepted', 'implemented', 'rejected']

type Tab = 'board' | 'requirements' | 'feedback' | 'traceability'

interface Props {
  client: ApiClient
  projectId: string
  applicationId?: string
  elements: ElementRef[]
  canEdit: boolean
}

/** Apply the changes waiting on this device to a list loaded from the server (or from the saved copy). */
function overlay<T extends { id: string }>(items: T[], changes: OutboxItem[], prefix: string): T[] {
  if (changes.length === 0) return items
  const by = new Map(changes.map((c) => [c.path.slice(prefix.length), c]))
  return items.filter((x) => by.get(x.id)?.kind.endsWith('_delete') !== true).map((x) => {
    const c = by.get(x.id)
    return c ? { ...x, ...(c.body as object) } : x
  })
}

const message = (e: unknown) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : 'Something went wrong.')

export function Board({ client, projectId, applicationId, elements, canEdit }: Props) {
  const [tab, setTab] = useState<Tab>('board')
  const [stories, setStories] = useState<Story[]>([])
  const [requirements, setRequirements] = useState<Requirement[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const off = useOffline()
  const offRef = useRef(off)
  offRef.current = off
  const connected = off ? off.online : true
  const editable = canEdit && (connected || (!!off && off.enabled && off.unlocked)) // offline, changes are kept on this device (when offline work is unlocked)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const reload = useCallback(async () => {
    const load = async () => {
      const [s, r] = await Promise.all([client.stories(projectId), client.requirements(projectId)])
      return { stories: s.items, requirements: r.items }
    }
    try {
      const o = offRef.current
      const res = o ? await o.readThrough(`board:${projectId}`, load) : { data: await load(), stale: false as const, savedAt: undefined }
      setStories(res.data.stories)
      setRequirements(res.data.requirements)
      setSavedAt(res.stale ? res.savedAt ?? '' : null)
      setError('')
    } catch (e) { setError(message(e)) }
  }, [client, projectId])
  // reload when the number of unsent changes goes down: they were sent, so the server's lists have changed
  useEffect(() => { void reload() }, [reload, off?.pending, off?.unlocked, off?.online])

  /** Create a story or requirement: sent now, or kept on this device until the server can be reached. */
  const create = useCallback(async (kind: 'story' | 'requirement', title: string, body = '') => {
    const o = offRef.current
    const send = (): Promise<unknown> => (kind === 'story' ? client.createStory(projectId, title) : client.createRequirement(projectId, title, body))
    if (!o) return guardRef.current(send)
    try {
      const r = await o.submit({ kind, path: `/projects/${projectId}/${kind === 'story' ? 'stories' : 'requirements'}`, body: kind === 'story' ? { title } : { title, body }, label: title, project: projectId }, send)
      setError('')
      setNotice(r.queued ? 'Saved on this device. It will be sent when you are online.' : '')
      await reload()
    } catch (e) { setError(message(e)) }
  }, [client, projectId, reload])

  /** Change or delete an existing item: sent now with the version the person saw, or kept on this device. */
  const change = useCallback(async (what: 'story' | 'requirement', item: Story | Requirement, body: Record<string, unknown> | null, label: string) => {
    const o = offRef.current
    const path = `/projects/${projectId}/${what === 'story' ? 'stories' : 'requirements'}/${item.id}`
    const ifMatch = item.version
    const v: [number] | [] = ifMatch === undefined ? [] : [ifMatch]
    const send = (): Promise<unknown> => what === 'story'
      ? (body ? client.updateStory(projectId, item.id, body, ...v) : client.deleteStory(projectId, item.id, ...v))
      : (body ? client.updateRequirement(projectId, item.id, body, ...v) : client.deleteRequirement(projectId, item.id, ...v))
    if (!o) return guardRef.current(send)
    const base = body ? Object.fromEntries(Object.keys(body).map((k) => [k, (item as unknown as Record<string, unknown>)[k]])) : undefined
    try {
      const r = await o.submit({ kind: `${what}_${body ? 'edit' : 'delete'}` as 'story_edit', path, body: body ?? {}, label, project: projectId, ifMatch, base }, send)
      setError('')
      setNotice(r.queued ? 'Saved on this device. It will be sent when you are online.' : '')
      await reload()
    } catch (e) {
      setError(e instanceof ApiError && e.code === 'version_conflict' ? 'Someone else changed this item. The board was reloaded; try again.' : message(e))
      if (e instanceof ApiError && e.code === 'version_conflict') await reload()
    }
  }, [client, projectId, reload])

  // what is waiting on this device is shown as if it had been done
  const pendingChanges = (off?.items ?? []).filter((i) => i.project === projectId && (i.kind.endsWith('_edit') || i.kind.endsWith('_delete')) && i.status !== 'conflict')
  const shownStories = useMemo(() => overlay(stories, pendingChanges.filter((i) => i.kind.startsWith('story')), `/projects/${projectId}/stories/`), [stories, pendingChanges, projectId]) // eslint-disable-line react-hooks/exhaustive-deps
  const shownRequirements = useMemo(() => overlay(requirements, pendingChanges.filter((i) => i.kind.startsWith('requirement')), `/projects/${projectId}/requirements/`), [requirements, pendingChanges, projectId]) // eslint-disable-line react-hooks/exhaustive-deps
  const conflicts = (off?.items ?? []).filter((i) => i.project === projectId && i.status === 'conflict')

  const guard = useCallback(async (work: () => Promise<unknown>, done?: string) => {
    try { await work(); setError(''); if (done !== undefined) setNotice(done); await reload() } catch (e) { setError(message(e)) }
  }, [reload])
  const guardRef = useRef(guard)
  guardRef.current = guard
  const waiting = (off?.items ?? []).filter((i) => i.project === projectId && (i.kind === 'story' || i.kind === 'requirement'))

  return (
    <section className="board" aria-label="Story board">
      <div role="tablist" className="tabs" aria-label="Board sections">
        {(['board', 'requirements', 'feedback', 'traceability'] as Tab[]).map((t) => (
          <button key={t} type="button" role="tab" aria-selected={tab === t} className={tab === t ? 'primary' : ''} onClick={() => setTab(t)}>
            {t[0]?.toUpperCase()}{t.slice(1)}
          </button>
        ))}
      </div>
      {error && <p role="alert" className="sev-error">{error}</p>}
      {notice && <p role="status" className="hint">{notice}</p>}
      {savedAt !== null && <p role="status" className="sev-warning">You are looking at the copy saved {savedAt ? new Date(savedAt).toLocaleString() : 'earlier'}: the server cannot be reached. Your changes wait on this device and are sent when you are online; if someone else changed the same item you will be asked which version to keep.</p>}
      {off && !connected && <p className="hint">Offline: changes to requirements and stories wait on this device and are sent when you are back online.</p>}
      {conflicts.length > 0 && <p role="alert" className="sev-warning">{conflicts.length} change{conflicts.length === 1 ? '' : 's'} could not be applied because someone else changed the item. Decide in “Offline work”.</p>}
      {tab === 'board' && <Columns stories={shownStories} requirements={shownRequirements} canEdit={editable} canCreate={canEdit} waiting={waiting.filter((w) => w.kind === 'story')}
        onAdd={(title) => create('story', title)}
        onMove={(s, status) => change('story', stories.find((x) => x.id === s.id) ?? s, { status, position: Math.max(0, ...stories.map((x) => x.position)) + 1 }, s.title)}
        onEdit={(s, patch) => change('story', stories.find((x) => x.id === s.id) ?? s, patch, patch.title ?? s.title)}
        onDelete={(s) => change('story', stories.find((x) => x.id === s.id) ?? s, null, s.title)} />}
      {tab === 'requirements' && <Requirements requirements={shownRequirements} canEdit={editable} canCreate={canEdit} waiting={waiting.filter((w) => w.kind === 'requirement')} canExtract={editable && !!applicationId}
        onAdd={(title, body) => create('requirement', title, body)}
        onStatus={(r, status) => change('requirement', requirements.find((x) => x.id === r.id) ?? r, { status }, r.title)}
        onDelete={(r) => change('requirement', requirements.find((x) => x.id === r.id) ?? r, null, r.title)}
        onExtract={(text) => guard(async () => {
          const r = await client.extractRequirements(applicationId!, text)
          setNotice(`${r.requirements.length} draft requirement${r.requirements.length === 1 ? '' : 's'} added${r.skipped_duplicates ? `, ${r.skipped_duplicates} duplicate${r.skipped_duplicates === 1 ? '' : 's'} skipped` : ''}.`)
        })} />}
      {tab === 'feedback' && (!connected ? <p className="hint">Reviewing feedback needs a connection.</p> : applicationId
        ? <Feedback client={client} applicationId={applicationId} canEdit={canEdit} onChanged={reload} setNotice={setNotice} setError={setError} />
        : <p className="hint">Choose an application to see its preview feedback.</p>)}
      {tab === 'traceability' && (!connected ? <p className="hint">Traceability needs a connection.</p> : <Trace client={client} projectId={projectId} stories={shownStories} requirements={shownRequirements} elements={elements} canEdit={canEdit} />)}
    </section>
  )
}

interface Waiting { id: number; label: string; status: string }

function WaitingList({ items, what }: { items: Waiting[]; what: string }) {
  if (items.length === 0) return null
  return (
    <ul className="list waiting" aria-label={`${what} waiting to be sent`}>
      {items.map((w) => <li key={w.id} className="card slim"><strong>{w.label}</strong> <small className="hint">{w.status === 'rejected' ? 'refused by the server (see Offline work)' : 'waiting to be sent'}</small></li>)}
    </ul>
  )
}

function StoryEditor({ story, onSave, onClose }: {
  story: Story
  onSave: (patch: Partial<Pick<Story, 'title' | 'body' | 'acceptance'>>) => void
  onClose: () => void
}) {
  const [title, setTitle] = useState(story.title)
  const [body, setBody] = useState(story.body ?? '')
  const [acceptance, setAcceptance] = useState(story.acceptance ?? '')
  const dirty = title !== story.title || body !== (story.body ?? '') || acceptance !== (story.acceptance ?? '')
  return (
    <form role=”dialog” aria-label=”Edit story” className=”card story-editor” onSubmit={(e) => {
      e.preventDefault()
      const patch: Partial<Pick<Story, 'title' | 'body' | 'acceptance'>> = {}
      if (title !== story.title) patch.title = title
      if (body !== (story.body ?? '')) patch.body = body
      if (acceptance !== (story.acceptance ?? '')) patch.acceptance = acceptance
      onSave(patch)
      onClose()
    }}>
      <h3>Edit story</h3>
      <label className=”field”><span>Title</span>
        <input required maxLength={300} value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <label className=”field”><span>User story</span>
        <textarea rows={4} maxLength={10000} placeholder=”As a … I want … so that …” value={body} onChange={(e) => setBody(e.target.value)} />
      </label>
      <label className=”field”><span>Acceptance criteria</span>
        <textarea rows={4} maxLength={10000} placeholder=”Given … when … then …” value={acceptance} onChange={(e) => setAcceptance(e.target.value)} />
      </label>
      <div className=”toolbar”>
        <button type=”submit” className=”primary” disabled={!dirty || !title.trim()}>Save</button>
        <button type=”button” onClick={onClose}>Cancel</button>
      </div>
    </form>
  )
}

function Columns({ stories, requirements, canEdit, canCreate, waiting, onAdd, onMove, onEdit, onDelete }: {
  stories: Story[]; requirements: Requirement[]; canEdit: boolean; canCreate: boolean; waiting: Waiting[]
  onAdd: (title: string) => void; onMove: (s: Story, status: StoryStatus) => void
  onEdit: (s: Story, patch: Partial<Pick<Story, 'title' | 'body' | 'acceptance'>>) => void
  onDelete: (s: Story) => void
}) {
  const [title, setTitle] = useState('')
  const [editing, setEditing] = useState<Story | null>(null)
  const reqTitle = useMemo(() => new Map(requirements.map((r) => [r.id, r.title])), [requirements])
  return (
    <>
      {canCreate && (
        <form className=”inline” onSubmit={(e) => { e.preventDefault(); if (title.trim()) { onAdd(title.trim()); setTitle('') } }}>
          <label className=”field”><span>New story</span><input value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} placeholder=”As a clerk I want …” /></label>
          <button type=”submit” disabled={!title.trim()}>Add story</button>
        </form>
      )}
      <WaitingList items={waiting} what=”Stories” />
      {editing && (
        <StoryEditor story={editing} onSave={(patch) => onEdit(editing, patch)} onClose={() => setEditing(null)} />
      )}
      <div className=”columns”>
        {COLUMNS.map((col) => {
          const here = stories.filter((s) => s.status === col.status)
          return (
            <section key={col.status} className=”column” aria-label={col.title}
              onDragOver={(e) => { if (canEdit) e.preventDefault() }}
              onDrop={(e) => {
                const id = e.dataTransfer.getData('text/plain')
                const s = stories.find((x) => x.id === id)
                if (canEdit && s && s.status !== col.status) onMove(s, col.status)
              }}>
              <h3>{col.title} <span className=”count”>{here.length}</span></h3>
              <ul className=”list”>
                {here.map((s) => (
                  <li key={s.id} className=”story” draggable={canEdit} onDragStart={(e) => e.dataTransfer.setData('text/plain', s.id)}>
                    <button type=”button” className=”link story-title” onClick={() => setEditing(s)}><strong>{s.title}</strong></button>
                    {s.body && <p className=”story-body hint”>{s.body.slice(0, 120)}{s.body.length > 120 ? '…' : ''}</p>}
                    {s.requirement_id && <small className=”hint”> · {reqTitle.get(s.requirement_id) ?? 'requirement'}</small>}
                    {s.source !== 'manual' && <small className=”badge badge-warning”>{s.source}</small>}
                    {canEdit && (
                      <div className=”story-actions”>
                        <button type=”button” className=”link” aria-label={`Edit story ${s.title}`} onClick={() => setEditing(s)}>Edit</button>
                        <label><span className=”sr-only”>Move “{s.title}” to</span>
                          <select value={s.status} onChange={(e) => onMove(s, e.target.value as StoryStatus)}>
                            {COLUMNS.map((c) => <option key={c.status} value={c.status}>{c.title}</option>)}
                          </select>
                        </label>
                        <button type=”button” className=”link” aria-label={`Delete story ${s.title}`} onClick={() => onDelete(s)}>Delete</button>
                      </div>
                    )}
                    {!canEdit && s.acceptance && (
                      <details className=”story-acceptance”><summary className=”hint”>Acceptance criteria</summary><p>{s.acceptance}</p></details>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )
        })}
      </div>
    </>
  )
}

function Requirements({ requirements, canEdit, canCreate, waiting, canExtract, onAdd, onStatus, onDelete, onExtract }: {
  requirements: Requirement[]; canEdit: boolean; canCreate: boolean; waiting: Waiting[]; canExtract: boolean
  onAdd: (title: string, body: string) => void; onStatus: (r: Requirement, s: RequirementStatus) => void
  onDelete: (r: Requirement) => void; onExtract: (text: string) => void
}) {
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [doc, setDoc] = useState('')
  return (
    <>
      <ul className="list">
        {requirements.length === 0 && <li className="hint">No requirements yet.</li>}
        {requirements.map((r) => (
          <li key={r.id} className="card">
            <strong>{r.title}</strong> {r.source === 'ai' && <small className="badge badge-warning">AI draft</small>}
            {r.body && <p className="hint">{r.body}</p>}
            {canEdit && (
              <div className="story-actions">
                <label><span className="sr-only">Status of “{r.title}”</span>
                  <select value={r.status} onChange={(e) => onStatus(r, e.target.value as RequirementStatus)}>
                    {REQUIREMENT_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                </label>
                <button type="button" className="link" aria-label={`Delete requirement ${r.title}`} onClick={() => onDelete(r)}>Delete</button>
              </div>
            )}
          </li>
        ))}
      </ul>
      <WaitingList items={waiting} what="Requirements" />
      {canCreate && (
        <form className="card" onSubmit={(e) => { e.preventDefault(); if (title.trim()) { onAdd(title.trim(), body); setTitle(''); setBody('') } }}>
          <h2>Add a requirement</h2>
          <label className="field"><span>Title</span><input value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} /></label>
          <label className="field"><span>Details</span><textarea rows={3} value={body} maxLength={10000} onChange={(e) => setBody(e.target.value)} /></label>
          <button type="submit" disabled={!title.trim()}>Add requirement</button>
        </form>
      )}
      {canExtract && (
        <form className="card" onSubmit={(e) => { e.preventDefault(); if (doc.trim()) { onExtract(doc); setDoc('') } }}>
          <h2>Extract from a document</h2>
          <p className="hint">Paste notes or a brief. The AI adds <em>draft</em> requirements; nothing is applied to the application.</p>
          <label className="field"><span>Document</span><textarea rows={6} value={doc} maxLength={20000} onChange={(e) => setDoc(e.target.value)} /></label>
          <button type="submit" disabled={!doc.trim()}>Extract requirements</button>
        </form>
      )}
    </>
  )
}

function Feedback({ client, applicationId, canEdit, onChanged, setNotice, setError }: {
  client: ApiClient; applicationId: string; canEdit: boolean; onChanged: () => void
  setNotice: (s: string) => void; setError: (s: string) => void
}) {
  const [items, setItems] = useState<FeedbackItem[] | null>(null)
  const load = useCallback(async () => {
    try { setItems((await client.feedback(applicationId, 'new')).items) } catch (e) { setItems([]); setError(message(e)) }
  }, [client, applicationId, setError])
  useEffect(() => { void load() }, [load])
  if (!canEdit) return <p className="hint">Only editors can review feedback.</p>
  return (
    <>
      <div className="toolbar">
        <button type="button" className="primary" disabled={!items?.length} onClick={async () => {
          try {
            const r = await client.clusterFeedback(applicationId)
            setNotice(`${r.stories.length} story proposal${r.stories.length === 1 ? '' : 's'} created; ${r.unclustered} item${r.unclustered === 1 ? '' : 's'} left over.`)
            setError('')
            await load()
            onChanged()
          } catch (e) { setError(message(e)) }
        }}>Group into proposed stories</button>
      </div>
      {items === null && <p>Loading…</p>}
      {items?.length === 0 && <p className="hint">No new feedback.</p>}
      <ul className="list">
        {items?.map((f) => (
          <li key={f.id} className="card">
            <p>{f.body}</p>
            <small className="hint">{f.page_key}{f.element_id ? ` · element ${f.element_id.slice(0, 8)}` : ''}</small>
            {f.has_screenshot && <> <a href={client.screenshotUrl(applicationId, f.id)} target="_blank" rel="noreferrer noopener">Screenshot</a></>}
            <button type="button" className="link" aria-label={`Dismiss feedback ${f.body.slice(0, 30)}`}
              onClick={async () => { try { await client.dismissFeedback(applicationId, f.id); await load() } catch (e) { setError(message(e)) } }}>Dismiss</button>
          </li>
        ))}
      </ul>
    </>
  )
}

function Trace({ client, projectId, stories, requirements, elements, canEdit }: {
  client: ApiClient; projectId: string; stories: Story[]; requirements: Requirement[]; elements: ElementRef[]; canEdit: boolean
}) {
  const [doc, setDoc] = useState<Traceability | null>(null)
  const [links, setLinks] = useState<TraceLink[]>([])
  const [error, setError] = useState('')
  const [from, setFrom] = useState('')
  const [kind, setKind] = useState<'element' | 'test'>('element')
  const [target, setTarget] = useState('')
  const [query, setQuery] = useState('')
  const [tests, setTests] = useState<{ id: string; title: string }[]>([])

  const load = useCallback(async () => {
    try {
      const [t, l] = await Promise.all([client.traceability(projectId), client.traceLinks(projectId)])
      setDoc(t)
      setLinks(l.items)
      setError('')
    } catch (e) { setError(message(e)) }
  }, [client, projectId])
  useEffect(() => { void load() }, [load, stories.length, requirements.length])
  useEffect(() => {
    if (kind !== 'test') return
    let live = true
    client.testCases(projectId, query).then((r) => live && setTests(r.items), () => undefined)
    return () => { live = false }
  }, [client, projectId, kind, query])

  const options = useMemo(() => [
    ...requirements.map((r) => ({ key: `requirement:${r.id}`, label: `Requirement · ${r.title}` })),
    ...stories.map((s) => ({ key: `story:${s.id}`, label: `Story · ${s.title}` })),
  ], [requirements, stories])
  const names = useMemo(() => new Map([...requirements.map((r) => [r.id, r.title] as const), ...stories.map((x) => [x.id, x.title] as const)]), [requirements, stories])
  const elementNames = useMemo(() => new Map(elements.map((x) => [x.id, x.label] as const)), [elements])
  const c = doc?.coverage
  const pct = (n: number) => (c && c.requirements ? `${n}/${c.requirements}` : '0/0')

  return (
    <>
      {error && <p role="alert" className="sev-error">{error}</p>}
      {c && (
        <dl className="coverage" aria-label="Coverage">
          <div><dt>With stories</dt><dd>{pct(c.with_stories)}</dd></div>
          <div><dt>With elements</dt><dd>{pct(c.with_elements)}</dd></div>
          <div><dt>With tests</dt><dd>{pct(c.with_tests)}</dd></div>
          <div><dt>Broken links</dt><dd className={c.dangling_elements + c.dangling_tests ? 'sev-error' : ''}>{c.dangling_elements + c.dangling_tests}</dd></div>
        </dl>
      )}
      <table className="trace">
        <thead><tr><th>Item</th><th>Status</th><th>Elements</th><th>Tests</th></tr></thead>
        <tbody>
          {[...(doc?.requirements.map((r) => ({ ...r, kind: 'Requirement' })) ?? []), ...(doc?.stories.map((s) => ({ ...s, kind: 'Story' })) ?? [])].map((r) => (
            <tr key={`${r.kind}${r.id}`}>
              <td><small>{r.kind}</small> {r.title}</td>
              <td>{r.status}</td>
              <td>{r.elements.map((e) => <span key={e.id} className={e.exists ? 'chip' : 'chip sev-error'}>{e.exists ? e.name : 'missing element'}</span>)}</td>
              <td>{r.tests.map((t) => <span key={t.id} className={t.exists ? 'chip' : 'chip sev-error'}>{t.exists ? t.title : 'missing test'}</span>)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {canEdit && (
        <form className="card" onSubmit={async (e) => {
          e.preventDefault()
          const [type, id] = from.split(':') as ['requirement' | 'story', string]
          try { await client.createTraceLink(projectId, { from_type: type, from_id: id, to_type: kind, to_id: target }); setTarget(''); await load() }
          catch (err) { setError(message(err)) }
        }}>
          <h2>Add a trace link</h2>
          <label className="field"><span>From</span>
            <select value={from} onChange={(e) => setFrom(e.target.value)}><option value="">Choose…</option>{options.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}</select></label>
          <label className="field"><span>To</span>
            <select value={kind} onChange={(e) => { setKind(e.target.value as 'element' | 'test'); setTarget('') }}><option value="element">Model element</option><option value="test">Generated test case</option></select></label>
          {kind === 'test' && <label className="field"><span>Search test cases</span><input value={query} onChange={(e) => setQuery(e.target.value)} /></label>}
          <label className="field"><span>Target</span>
            <select value={target} onChange={(e) => setTarget(e.target.value)}><option value="">Choose…</option>
              {(kind === 'element' ? elements.map((x) => ({ id: x.id, label: x.label })) : tests.map((x) => ({ id: x.id, label: x.title }))).map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
            </select></label>
          <button type="submit" disabled={!from || !target}>Link</button>
        </form>
      )}
      {canEdit && links.length > 0 && (
        <section aria-label="Existing links">
          <h2>Links</h2>
          <ul className="list">
            {links.map((l) => (
              <li key={l.id}>
                {names.get(l.from_id) ?? l.from_id.slice(0, 8)} → {l.to_type} {names.get(l.to_id) ?? elementNames.get(l.to_id) ?? l.to_id.slice(0, 40)}
                <button type="button" className="link" aria-label={`Remove link from ${names.get(l.from_id) ?? l.from_id}`}
                  onClick={async () => { try { await client.deleteTraceLink(projectId, l.id); await load() } catch (err) { setError(message(err)) } }}>Remove</button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  )
}
