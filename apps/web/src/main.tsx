import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'
import { registerServiceWorker } from './pwa/register'

const root = document.getElementById('root')
if (!root) throw new Error('missing #root')
void registerServiceWorker().catch(() => undefined) // installability and the offline shell; never blocks the app
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
