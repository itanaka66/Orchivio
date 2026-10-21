// Thin reverse proxy for the Studio E2E tests: serves the built Studio SPA from dist/
// and forwards /api/, /healthz and /readyz to the real Orchivio API server.
// Usage: node e2e/studio_proxy.mjs <dist-dir> <api-port>  (defaults: dist, 4190; PORT env sets listen port, default 4191)
import { createServer, request as httpRequest } from 'node:http'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { extname, join, resolve } from 'node:path'

const dist = resolve(process.argv[2] ?? 'dist')
const apiPort = Number(process.argv[3] ?? 4190)
const port = Number(process.env.PORT ?? 4191)

const types = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.webmanifest': 'application/manifest+json',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
}

function walk(dir, base = '') {
  const out = {}
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) Object.assign(out, walk(full, `${base}/${name}`))
    else out[`${base}/${name}`] = readFileSync(full)
  }
  return out
}

const files = walk(dist)

function proxy(req, res) {
  const headers = { ...req.headers, host: `127.0.0.1:${apiPort}` }
  const options = { host: '127.0.0.1', port: apiPort, path: req.url, method: req.method, headers }
  const pr = httpRequest(options, (pr2) => {
    res.writeHead(pr2.statusCode ?? 500, pr2.headers)
    pr2.pipe(res)
  })
  pr.on('error', () => { res.writeHead(502); res.end('bad gateway') })
  req.pipe(pr)
}

createServer((req, res) => {
  const path = new URL(req.url ?? '/', `http://localhost:${port}`).pathname

  if (path.startsWith('/api/') || path === '/healthz' || path === '/readyz') {
    return proxy(req, res)
  }

  let key = path
  if (!files[key]) key = extname(path) ? key : '/index.html' // SPA fallback
  const body = files[key]
  if (!body) { res.writeHead(404); return res.end('not found') }
  res.writeHead(200, { 'Content-Type': types[extname(key)] ?? 'application/octet-stream', 'Cache-Control': 'no-cache' })
  res.end(body)
}).listen(port, () => console.log(`studio proxy on ${port}, api on ${apiPort}`))
