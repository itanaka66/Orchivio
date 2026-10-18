import { createHash } from 'node:crypto'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Plugin } from 'vite'

/** Put a build id (a hash of the built index.html, which names every hashed asset) into the service worker so each release changes it. */
export function stampServiceWorker(dir: string): string | null {
  const sw = join(dir, 'sw.js')
  const index = join(dir, 'index.html')
  if (!existsSync(sw) || !existsSync(index)) return null
  const id = createHash('sha256').update(readFileSync(index)).digest('hex').slice(0, 12)
  const text = readFileSync(sw, 'utf8')
  if (!text.includes('__BUILD_ID__')) throw new Error('sw.js has no __BUILD_ID__ placeholder')
  writeFileSync(sw, text.replaceAll('__BUILD_ID__', id))
  return id
}

export function pwa(): Plugin {
  let out = 'dist'
  return {
    name: 'orchivio-pwa',
    apply: 'build',
    configResolved(config) { out = join(config.root, config.build.outDir) },
    closeBundle() { stampServiceWorker(out) },
  }
}
