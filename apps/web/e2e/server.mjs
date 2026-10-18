// A tiny stand-in for "the reverse proxy in front of the Studio" for browser tests: serves the built app from dist/ with the cache headers that
// docs/PWA.md recommends, a minimal fake of the API (so we can see which API requests really reach the network), and a control endpoint that
// switches to a second "release" to exercise the update flow. Not used outside tests.
import { createServer } from 'node:http'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { extname, join, resolve } from 'node:path'

const dist = resolve(process.argv[2] ?? 'dist')
const port = Number(process.env.PORT ?? 4173)
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.webmanifest': 'application/manifest+json', '.png': 'image/png', '.svg': 'image/svg+xml' }

function walk(dir, base = '') {
  const out = {}
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) Object.assign(out, walk(full, `${base}/${name}`))
    else out[`${base}/${name}`] = readFileSync(full)
  }
  return out
}

const release1 = walk(dist)
// release 2 is the same build deployed again with a change: a different index.html (so a different build id once stamped) and worker bytes
const release2 = { ...release1 }
release2['/index.html'] = Buffer.from(release1['/index.html'].toString('utf8').replace('</body>', '<!-- release 2 --></body>'))
release2['/sw.js'] = Buffer.from(release1['/sw.js'].toString('utf8').replace(/BUILD = '[^']*'/, "BUILD = 'release2'"))
const releases = { 1: release1, 2: release2 }
let current = 1

const counts = {}
let loggedIn = false

// A small versioned board, to see offline changes arrive (and conflict) for real. Mirrors the API's If-Match / Idempotency-Key behaviour.
const P = '11111111-1111-4111-8111-111111111111'
const board = { stories: [], requirements: [], keys: new Map(), seq: 0, log: [] }
const uid = () => `00000000-0000-4000-8000-${String(++board.seq).padStart(12, '0')}`
const resetBoard = () => { board.stories = [{ id: uid(), title: 'Pay invoice', body: '', acceptance: '', status: 'backlog', source: 'manual', position: 1, requirement_id: null, version: 1 }]; board.requirements = []; board.keys = new Map(); board.log = [] }
resetBoard()
const readBody = (req) => new Promise((resolve) => { let b = ''; req.on('data', (c) => { b += c }); req.on('end', () => { try { resolve(b ? JSON.parse(b) : {}) } catch { resolve({}) } }) })

function headersFor(path) {
  if (path === '/sw.js' || path === '/index.html' || path === '/manifest.webmanifest' || path === '/offline.html') return { 'Cache-Control': 'no-cache' }
  if (path.startsWith('/assets/')) return { 'Cache-Control': 'public, max-age=31536000, immutable' }
  return {}
}

const json = (res, status, body, extra = {}) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...extra }); res.end(JSON.stringify(body)) }

createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://localhost:${port}`)
  const path = url.pathname

  if (path === '/__control/release' && req.method === 'POST') { current = url.searchParams.get('n') === '2' ? 2 : 1; return json(res, 200, { release: current }) }
  if (path === '/__control/counts') return json(res, 200, counts)
  if (path === '/__control/reset') { for (const k of Object.keys(counts)) delete counts[k]; loggedIn = false; current = 1; resetBoard(); return json(res, 200, {}) }
  if (path === '/__control/board') return json(res, 200, board)
  if (path === '/__control/rename' && req.method === 'POST') { const st = board.stories[0]; st.title = url.searchParams.get('title') ?? st.title; if (url.searchParams.get('status')) st.status = url.searchParams.get('status'); st.version += 1; return json(res, 200, st) }

  if (path === '/healthz') return json(res, 200, { status: 'ok' })
  if (path.startsWith('/api/')) {
    counts[path] = (counts[path] ?? 0) + 1
    if (path === '/api/v1/auth/login' && req.method === 'POST') { loggedIn = true; return json(res, 200, { user: { id: 'u1', email: 'a@example.com', display_name: 'Ada' }, csrf_token: 'csrf-token' }, { 'Set-Cookie': 'sid=secret; HttpOnly; Path=/' }) }
    if (path === '/api/v1/auth/logout') { loggedIn = false; return json(res, 204, {}) }
    if (path === '/api/v1/me') return loggedIn ? json(res, 200, { user: { id: 'u1', email: 'a@example.com', display_name: 'Ada' } }) : json(res, 401, { error: { code: 'unauthorized', message: 'sign in' } })
    if (path === '/api/v1/organizations') return json(res, 200, { items: [{ id: 'o1', name: 'Acme', slug: 'acme', role: 'owner' }], limit: 200, offset: 0, total: 1 })
    if (path === '/api/v1/organizations/o1/projects') return json(res, 200, { items: [{ id: P, organization_id: 'o1', name: 'Inventory', slug: 'inventory' }], limit: 200, offset: 0, total: 1 })
    if (path === `/api/v1/projects/${P}/applications`) return json(res, 200, { items: [], limit: 200, offset: 0, total: 0 })
    const m = path.match(new RegExp(`^/api/v1/projects/${P}/(stories|requirements)(?:/([0-9a-f-]{36}))?$`))
    if (m) {
      if (!loggedIn) return json(res, 401, { error: { code: 'unauthorized', message: 'sign in' } })
      const list = board[m[1]]
      if (req.method === 'GET' && !m[2]) return json(res, 200, { items: list })
      const body = await readBody(req)
      if (req.method === 'POST' && !m[2]) {
        const key = req.headers['idempotency-key']
        if (key && board.keys.has(key)) return json(res, 201, board.keys.get(key), { 'Idempotency-Replayed': 'true' })
        const item = { id: uid(), title: body.title, body: body.body ?? '', acceptance: '', status: m[1] === 'stories' ? 'backlog' : 'draft', source: 'manual', position: list.length + 1, requirement_id: null, version: 1 }
        list.push(item)
        if (key) board.keys.set(key, item)
        board.log.push(`POST ${m[1]} ${body.title}`)
        return json(res, 201, item)
      }
      const item = list.find((x) => x.id === m[2])
      if (!item) return json(res, 404, { error: { code: 'not_found', message: 'not found' } })
      const ifMatch = req.headers['if-match']
      if (ifMatch !== undefined && Number(String(ifMatch).replace(/"/g, '')) !== item.version) return json(res, 409, { error: { code: 'version_conflict', message: 'the item was changed by someone else', details: { current: item } } })
      if (req.method === 'PATCH') { Object.assign(item, body); item.version += 1; board.log.push(`PATCH ${item.title} ${JSON.stringify(body)}`); return json(res, 200, item) }
      if (req.method === 'DELETE') { board[m[1]] = list.filter((x) => x !== item); board.log.push(`DELETE ${item.title}`); res.writeHead(204); return res.end() }
    }
    if (path === '/api/v1/ping') return json(res, 200, { pong: counts[path], secret: 'private-data-that-must-never-be-cached' })
    return json(res, 404, { error: { code: 'not_found', message: 'not found' } })
  }

  const files = releases[current]
  let key = path
  if (!files[key]) key = !extname(path) ? '/index.html' : key // single-page app fallback for navigations
  const body = files[key]
  if (!body) { res.writeHead(404); return res.end('not found') }
  res.writeHead(200, { 'Content-Type': types[extname(key)] ?? 'application/octet-stream', ...headersFor(key) })
  res.end(body)
}).listen(port, () => console.log(`e2e server on ${port}, serving ${dist}`))
