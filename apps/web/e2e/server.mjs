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

function headersFor(path) {
  if (path === '/sw.js' || path === '/index.html' || path === '/manifest.webmanifest' || path === '/offline.html') return { 'Cache-Control': 'no-cache' }
  if (path.startsWith('/assets/')) return { 'Cache-Control': 'public, max-age=31536000, immutable' }
  return {}
}

const json = (res, status, body, extra = {}) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...extra }); res.end(JSON.stringify(body)) }

createServer((req, res) => {
  const url = new URL(req.url ?? '/', `http://localhost:${port}`)
  const path = url.pathname

  if (path === '/__control/release' && req.method === 'POST') { current = url.searchParams.get('n') === '2' ? 2 : 1; return json(res, 200, { release: current }) }
  if (path === '/__control/counts') return json(res, 200, counts)
  if (path === '/__control/reset') { for (const k of Object.keys(counts)) delete counts[k]; loggedIn = false; current = 1; return json(res, 200, {}) }

  if (path === '/healthz') return json(res, 200, { status: 'ok' })
  if (path.startsWith('/api/')) {
    counts[path] = (counts[path] ?? 0) + 1
    if (path === '/api/v1/auth/login' && req.method === 'POST') { loggedIn = true; return json(res, 200, { user: { id: 'u1', email: 'a@example.com', display_name: 'Ada' }, csrf_token: 'csrf-token' }, { 'Set-Cookie': 'sid=secret; HttpOnly; Path=/' }) }
    if (path === '/api/v1/auth/logout') { loggedIn = false; return json(res, 204, {}) }
    if (path === '/api/v1/me') return loggedIn ? json(res, 200, { user: { id: 'u1', email: 'a@example.com', display_name: 'Ada' } }) : json(res, 401, { error: { code: 'unauthorized', message: 'sign in' } })
    if (path === '/api/v1/organizations') return json(res, 200, { items: [{ id: 'o1', name: 'Acme', slug: 'acme', role: 'owner' }], limit: 200, offset: 0, total: 1 })
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
