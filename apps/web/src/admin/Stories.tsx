import { useState } from 'react'
import type { ApiClient, Requirement, Story } from '../api/client'
import { Danger, Feedback, Loading, Panel, Pill, useAction, useApi } from './kit'

type ReqStatus = Requirement['status']
type StoryStatus = Story['status']

const REQ_STATUSES: ReqStatus[] = ['draft', 'accepted', 'implemented', 'rejected']
const STORY_STATUSES: StoryStatus[] = ['proposed', 'backlog', 'ready', 'in_progress', 'done', 'rejected']

function statusLabel(s: string) {
  return s.replace(/_/g, ' ')
}

/** Story and requirements board (FR-098). */
export function Stories({ client, projectId }: { client: ApiClient; projectId: string }) {
  const reqs = useApi(() => client.requirements(projectId), [client, projectId])
  const stories = useApi(() => client.stories(projectId), [client, projectId])

  const reloadAll = () => { reqs.reload(); stories.reload() }
  const { run, error, notice } = useAction(reloadAll)

  // ---- requirements ----
  const [newReqTitle, setNewReqTitle] = useState('')
  const [editReq, setEditReq] = useState<Requirement | null>(null)
  const [editReqTitle, setEditReqTitle] = useState('')
  const [editReqBody, setEditReqBody] = useState('')
  const [reqFilter, setReqFilter] = useState<ReqStatus | ''>('')

  // ---- stories ----
  const [newStoryTitle, setNewStoryTitle] = useState('')
  const [newStoryReq, setNewStoryReq] = useState('')
  const [editStory, setEditStory] = useState<Story | null>(null)
  const [editStoryTitle, setEditStoryTitle] = useState('')
  const [editStoryBody, setEditStoryBody] = useState('')
  const [editStoryAcceptance, setEditStoryAcceptance] = useState('')
  const [storyFilter, setStoryFilter] = useState<StoryStatus | ''>('')

  const startEditReq = (r: Requirement) => {
    setEditReq(r)
    setEditReqTitle(r.title)
    setEditReqBody(r.body)
  }

  const saveReq = () => {
    if (!editReq) return Promise.resolve()
    return client.updateRequirement(projectId, editReq.id, { title: editReqTitle, body: editReqBody }, editReq.version)
  }

  const startEditStory = (s: Story) => {
    setEditStory(s)
    setEditStoryTitle(s.title)
    setEditStoryBody(s.body)
    setEditStoryAcceptance(s.acceptance)
  }

  const saveStory = () => {
    if (!editStory) return Promise.resolve()
    return client.updateStory(projectId, editStory.id, {
      title: editStoryTitle,
      body: editStoryBody,
      acceptance: editStoryAcceptance,
    }, editStory.version)
  }

  const visibleReqs = (reqs.data?.items ?? []).filter((r) => !reqFilter || r.status === reqFilter)
  const visibleStories = (stories.data?.items ?? []).filter((s) => !storyFilter || s.status === storyFilter)

  return (
    <>
      {/* ---- Requirements panel ---- */}
      <Panel
        title="Requirements"
        actions={
          <form
            className="inline"
            onSubmit={(e) => {
              e.preventDefault()
              if (!newReqTitle.trim()) return
              void run(
                () => client.createRequirement(projectId, newReqTitle.trim()),
                'Requirement created.',
              ).then(() => setNewReqTitle(''))
            }}
          >
            <input
              value={newReqTitle}
              placeholder="Requirement title"
              onChange={(e) => setNewReqTitle(e.target.value)}
            />
            <button type="submit" disabled={!newReqTitle.trim()}>
              Add
            </button>
          </form>
        }
      >
        <p className="hint">High-level requirements linked to stories. Status: draft → accepted → implemented.</p>
        <Feedback error={error} notice={notice} />
        <div className="toolbar">
          <label>
            Status{' '}
            <select
              aria-label="Filter requirements"
              value={reqFilter}
              onChange={(e) => setReqFilter(e.target.value as ReqStatus | '')}
            >
              <option value="">All</option>
              {REQ_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {statusLabel(s)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <Loading loading={reqs.loading} error={reqs.error} />
        {!reqs.loading && visibleReqs.length === 0 && <p className="hint">No requirements.</p>}
        <ul className="list">
          {visibleReqs.map((r) =>
            editReq?.id === r.id ? (
              <li key={r.id} className="card">
                <form
                  onSubmit={(e) => {
                    e.preventDefault()
                    void run(saveReq, 'Requirement updated.').then(() => setEditReq(null))
                  }}
                >
                  <input
                    className="field"
                    value={editReqTitle}
                    onChange={(e) => setEditReqTitle(e.target.value)}
                    autoFocus
                  />
                  <textarea
                    className="field"
                    rows={3}
                    value={editReqBody}
                    placeholder="Description (optional)"
                    onChange={(e) => setEditReqBody(e.target.value)}
                  />
                  <div className="toolbar">
                    <button type="submit" className="primary" disabled={!editReqTitle.trim()}>
                      Save
                    </button>
                    <button type="button" onClick={() => setEditReq(null)}>
                      Cancel
                    </button>
                  </div>
                </form>
              </li>
            ) : (
              <li key={r.id} className="card slim">
                <div className="row">
                  <strong>{r.title}</strong>
                  <Pill value={r.status} />
                  <div className="row">
                    {REQ_STATUSES.filter((s) => s !== r.status).map((s) => (
                      <button
                        key={s}
                        type="button"
                        className="link"
                        onClick={() =>
                          void run(() => client.updateRequirement(projectId, r.id, { status: s }, r.version))
                        }
                      >
                        → {statusLabel(s)}
                      </button>
                    ))}
                    <button type="button" className="link" onClick={() => startEditReq(r)}>
                      Edit
                    </button>
                    <Danger
                      label="Delete"
                      onConfirm={() => void run(() => client.deleteRequirement(projectId, r.id, r.version))}
                    />
                  </div>
                </div>
                {r.body && <p className="hint">{r.body}</p>}
              </li>
            ),
          )}
        </ul>
      </Panel>

      {/* ---- Stories panel ---- */}
      <Panel
        title="Stories"
        actions={
          <form
            className="inline"
            onSubmit={(e) => {
              e.preventDefault()
              if (!newStoryTitle.trim()) return
              void run(
                () => client.createStory(projectId, newStoryTitle.trim(), newStoryReq || undefined),
                'Story created.',
              ).then(() => { setNewStoryTitle(''); setNewStoryReq('') })
            }}
          >
            <input
              value={newStoryTitle}
              placeholder="Story title"
              onChange={(e) => setNewStoryTitle(e.target.value)}
            />
            <select
              aria-label="Requirement"
              value={newStoryReq}
              onChange={(e) => setNewStoryReq(e.target.value)}
            >
              <option value="">No requirement</option>
              {(reqs.data?.items ?? []).map((r) => (
                <option key={r.id} value={r.id}>
                  {r.title}
                </option>
              ))}
            </select>
            <button type="submit" disabled={!newStoryTitle.trim()}>
              Add
            </button>
          </form>
        }
      >
        <p className="hint">User stories with acceptance criteria. Drag through statuses to track progress.</p>
        <div className="toolbar">
          <label>
            Status{' '}
            <select
              aria-label="Filter stories"
              value={storyFilter}
              onChange={(e) => setStoryFilter(e.target.value as StoryStatus | '')}
            >
              <option value="">All</option>
              {STORY_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {statusLabel(s)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <Loading loading={stories.loading} error={stories.error} />
        {!stories.loading && visibleStories.length === 0 && <p className="hint">No stories.</p>}
        <ul className="list">
          {visibleStories.map((s) =>
            editStory?.id === s.id ? (
              <li key={s.id} className="card">
                <form
                  onSubmit={(e) => {
                    e.preventDefault()
                    void run(saveStory, 'Story updated.').then(() => setEditStory(null))
                  }}
                >
                  <input
                    className="field"
                    value={editStoryTitle}
                    onChange={(e) => setEditStoryTitle(e.target.value)}
                    autoFocus
                  />
                  <textarea
                    className="field"
                    rows={3}
                    value={editStoryBody}
                    placeholder="As a … I want … so that …"
                    onChange={(e) => setEditStoryBody(e.target.value)}
                  />
                  <textarea
                    className="field"
                    rows={3}
                    value={editStoryAcceptance}
                    placeholder="Acceptance criteria"
                    onChange={(e) => setEditStoryAcceptance(e.target.value)}
                  />
                  <div className="toolbar">
                    <button type="submit" className="primary" disabled={!editStoryTitle.trim()}>
                      Save
                    </button>
                    <button type="button" onClick={() => setEditStory(null)}>
                      Cancel
                    </button>
                  </div>
                </form>
              </li>
            ) : (
              <li key={s.id} className="card slim">
                <div className="row">
                  <strong>{s.title}</strong>
                  <Pill value={s.status} />
                  {s.requirement_id && (
                    <small className="hint">
                      req: {(reqs.data?.items ?? []).find((r) => r.id === s.requirement_id)?.title ?? s.requirement_id.slice(0, 8)}
                    </small>
                  )}
                  <div className="row">
                    {STORY_STATUSES.filter((st) => st !== s.status).slice(0, 3).map((st) => (
                      <button
                        key={st}
                        type="button"
                        className="link"
                        onClick={() => void run(() => client.updateStory(projectId, s.id, { status: st }, s.version))}
                      >
                        → {statusLabel(st)}
                      </button>
                    ))}
                    <button type="button" className="link" onClick={() => startEditStory(s)}>
                      Edit
                    </button>
                    <Danger
                      label="Delete"
                      onConfirm={() => void run(() => client.deleteStory(projectId, s.id, s.version))}
                    />
                  </div>
                </div>
                {s.body && <p className="hint">{s.body}</p>}
                {s.acceptance && <p className="hint"><em>Acceptance: {s.acceptance}</em></p>}
              </li>
            ),
          )}
        </ul>
      </Panel>
    </>
  )
}
