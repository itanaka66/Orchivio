/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { pwa } from './pwa-plugin'

// The API is same-origin in production (served behind one reverse proxy); in development the
// dev server proxies it so the session cookie works without CORS.
export default defineConfig({
  plugins: [react(), pwa()],
  server: { proxy: { '/api': 'http://127.0.0.1:8000', '/healthz': 'http://127.0.0.1:8000' } },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    css: false,
  },
})
