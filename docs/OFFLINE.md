# Offline data and sync (FR-129, P2)

The Studio keeps working without a connection for a limited, safe set of things. Everything stored on the device is encrypted with a passphrase that only the
person knows; it is never sent anywhere and never stored.

## What works offline

* **Reading** the story board and a small project index opened before (encrypted snapshots, shown with a "saved copy" banner).
* **Adding** requirements, stories, preview feedback and flow-change proposals. They wait in an encrypted outbox and are sent when the connection returns and the
  person is signed in. An `Idempotency-Key` makes re-sending safe.
* **Editing and deleting** requirements and stories. Each edit carries the version the person saw (`If-Match`). If a concurrent change happened, the server
  answers 409: fields the person did not touch are rebased automatically; fields that conflict show a **Keep mine / Keep theirs** prompt in the outbox panel.
* Background sync: the service worker wakes the Studio when the browser reports the connection is back; visibility-change also triggers a sync attempt.
* Storage management: shows quota and usage; warns and blocks new offline changes when the browser reports the origin is almost full; `requestPersistent()` asks
  the browser to protect the data under storage pressure.
* AI extraction, feedback review and traceability **need a connection**.

## How it is protected

* IndexedDB + WebCrypto: PBKDF2 (SHA-256) derives an AES-GCM key from the passphrase. Wrong passphrase = nothing opens. Lose it = the data is lost.
* Data is bound to one account; another account on the same browser cannot open or send it and can only delete it.
* Signing out asks for confirmation when changes are unsent, and locks the vault.
* Each queued request carries an `Idempotency-Key`. The server (`api/idempotency.py`, table `idempotency_keys`, migration 0019) replays the stored answer
  for a repeated key instead of creating a duplicate, so a retry after a lost response is safe. A key reused with a different body is refused.
* A change the server refuses stays in the outbox with the reason; a person chooses to retry or discard it. Nothing is dropped silently.

## Not built / not verified

* Component and unit tests (jsdom + fake-indexeddb) cover the vault, outbox, provider, panel, board, feedback widget and the offline start.
* **Playwright offline scenarios** cover the happy path and conflict resolution in Chromium and Firefox (`offline.e2e.ts`); WebKit is skipped because
  Playwright's offline emulation does not reach it. Nothing was tried on real physical devices.
* No background sync while the Studio tab is closed (only when the tab becomes visible again or the SW's sync event fires while the tab is open).
* Generated applications: the appshell (`/apps/{id}/shell/`) caches its own static files (HTML, script, styles, icons, manifest) and serves the offline page when there is no connection. It never caches API data, rows or sessions; offline it opens and says so. Data operations need a connection.
