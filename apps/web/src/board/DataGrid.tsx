import { useState } from 'react'
import { type ApiClient } from '../api/client'
import { Feedback, Loading, Pager, useAction, useApi, type Page } from '../admin/kit'

interface Column { name: string; type: string; nullable: boolean }
interface EntityMeta { name: string; columns: Column[] }

const LIMIT = 50

export function DataGrid({ client, appId, entity }: { client: ApiClient; appId: string; entity: string }) {
  const [offset, setOffset] = useState(0)
  const [sort, setSort] = useState<{ col: string; dir: 'asc' | 'desc' } | null>(null)
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [editCell, setEditCell] = useState<{ rowId: string; col: string; value: string } | null>(null)

  const meta = useApi(() => client.request<EntityMeta>('GET', `/applications/${appId}/preview/entities/${entity}`), [client, appId, entity])

  const params = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) })
  if (sort) { params.set('order_by', sort.col); params.set('order', sort.dir) }
  Object.entries(filters).forEach(([k, v]) => { if (v) params.set(`filter_${k}`, v) })

  const rows = useApi(() => client.request<Page<Record<string, unknown>>>('GET', `/applications/${appId}/preview/data/${entity}?${params}`), [client, appId, entity, offset, sort, filters])
  const { run, error, notice } = useAction(rows.reload)

  const cols = meta.data?.columns ?? []
  const idCol = cols.find((c) => c.name === 'id')?.name ?? cols[0]?.name

  const toggleSort = (col: string) => {
    setSort((s) => s?.col === col ? (s.dir === 'asc' ? { col, dir: 'desc' } : null) : { col, dir: 'asc' })
    setOffset(0)
  }

  const saveCell = async () => {
    if (!editCell) return
    const patch: Record<string, unknown> = { [editCell.col]: editCell.value }
    await run(() => client.request('PATCH', `/applications/${appId}/preview/data/${entity}/${editCell.rowId}`, patch), 'Saved.')
    setEditCell(null)
  }

  return (
    <div className="datagrid">
      <Feedback error={error} notice={notice} />
      <Loading loading={meta.loading || rows.loading} error={meta.error || rows.error} />

      {/* Filter row */}
      {cols.length > 0 && (
        <div className="row filter-row">
          {cols.map((c) => (
            <input key={c.name} placeholder={`Filter ${c.name}…`} value={filters[c.name] ?? ''} className="filter-input"
              onChange={(e) => { setFilters((f) => ({ ...f, [c.name]: e.target.value })); setOffset(0) }} />
          ))}
          <button type="button" className="link" onClick={() => { setFilters({}); setOffset(0) }}>Clear</button>
        </div>
      )}

      <div style={{ overflowX: 'auto' }}>
        <table className="trace">
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c.name} onClick={() => toggleSort(c.name)} style={{ cursor: 'pointer', userSelect: 'none' }}>
                  {c.name}
                  {sort?.col === c.name ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : ''}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.data?.items.map((row, ri) => {
              const rowId = String(idCol ? row[idCol] : ri)
              return (
                <tr key={rowId}>
                  {cols.map((c) => {
                    const cellVal = String(row[c.name] ?? '')
                    const isEditing = editCell?.rowId === rowId && editCell?.col === c.name
                    return (
                      <td key={c.name} onDoubleClick={() => setEditCell({ rowId, col: c.name, value: cellVal })}>
                        {isEditing ? (
                          <input autoFocus value={editCell!.value} onChange={(e) => setEditCell({ ...editCell!, value: e.target.value })}
                            onBlur={() => void saveCell()} onKeyDown={(e) => { if (e.key === 'Enter') void saveCell(); if (e.key === 'Escape') setEditCell(null) }} />
                        ) : (
                          <span title={cellVal}>{cellVal.length > 60 ? cellVal.slice(0, 60) + '…' : cellVal}</span>
                        )}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <Pager page={rows.data} offset={offset} limit={LIMIT} onOffset={setOffset} />
    </div>
  )
}
