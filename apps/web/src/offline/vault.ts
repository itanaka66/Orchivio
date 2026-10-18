import type { Db } from './idb'

// Everything kept on the device for offline work is encrypted with a key derived from a passphrase that only the person knows. The key lives in memory
// while the vault is unlocked; the passphrase is never stored. AES-GCM, a fresh random IV per record, PBKDF2-SHA-256 for the key.

export interface Sealed { iv: string; data: string }

const MIN_PASSPHRASE = 10
const VERIFIER = 'orchivio-vault-v1'
const enc = new TextEncoder()
const dec = new TextDecoder()

const b64 = (bytes: Uint8Array): string => { let s = ''; for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000)); return btoa(s) }
const unb64 = (text: string): Uint8Array<ArrayBuffer> => Uint8Array.from(atob(text), (c) => c.charCodeAt(0))

export class VaultError extends Error {}

interface Header { salt: string; iterations: number; verifier: Sealed }

export class Vault {
  private key: CryptoKey | null = null

  constructor(private readonly db: Db, private readonly iterations = 250_000) {}

  get unlocked(): boolean { return this.key !== null }

  async exists(): Promise<boolean> { return (await this.db.get<Header>('meta', 'vault')) !== undefined }

  private async derive(passphrase: string, salt: Uint8Array<ArrayBuffer>, iterations: number): Promise<CryptoKey> {
    const base = await crypto.subtle.importKey('raw', enc.encode(passphrase), 'PBKDF2', false, ['deriveKey'])
    return crypto.subtle.deriveKey({ name: 'PBKDF2', salt, iterations, hash: 'SHA-256' }, base, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt'])
  }

  /** Set up the vault with a new passphrase and unlock it. Refuses when one exists (disable it first) or the passphrase is short. */
  async create(passphrase: string): Promise<void> {
    if (passphrase.length < MIN_PASSPHRASE) throw new VaultError(`Use a passphrase of at least ${MIN_PASSPHRASE} characters.`)
    if (await this.exists()) throw new VaultError('Offline work is already set up on this device.')
    const salt = crypto.getRandomValues(new Uint8Array(16))
    this.key = await this.derive(passphrase, salt, this.iterations)
    await this.db.put('meta', { salt: b64(salt), iterations: this.iterations, verifier: await this.seal(VERIFIER, 'verifier') } satisfies Header, 'vault')
  }

  /** True and unlocked when the passphrase is right; false (and still locked) when it is not. */
  async unlock(passphrase: string): Promise<boolean> {
    const header = await this.db.get<Header>('meta', 'vault')
    if (!header) throw new VaultError('Offline work is not set up on this device.')
    const key = await this.derive(passphrase, unb64(header.salt), header.iterations)
    try {
      const text = await this.open<string>(header.verifier, 'verifier', key)
      if (text !== VERIFIER) return false
    } catch { return false }
    this.key = key
    return true
  }

  lock(): void { this.key = null }

  /** Delete everything stored for offline work: the vault header, queued requests and saved copies. */
  async wipe(): Promise<void> { this.key = null; await this.db.clear() }

  private need(key: CryptoKey | null = this.key): CryptoKey {
    if (!key) throw new VaultError('Unlock offline work first.')
    return key
  }

  /** Encrypt ``value`` (any JSON). ``purpose`` is bound to the ciphertext, so a record cannot be passed off as another kind. */
  async seal(value: unknown, purpose: string): Promise<Sealed> {
    const iv = crypto.getRandomValues(new Uint8Array(12))
    const data = await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: enc.encode(purpose) }, this.need(), enc.encode(JSON.stringify(value)))
    return { iv: b64(iv), data: b64(new Uint8Array(data)) }
  }

  async open<T>(sealed: Sealed, purpose: string, key: CryptoKey | null = this.key): Promise<T> {
    try {
      const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: unb64(sealed.iv), additionalData: enc.encode(purpose) }, this.need(key), unb64(sealed.data))
      return JSON.parse(dec.decode(plain)) as T
    } catch (e) {
      if (e instanceof VaultError) throw e
      throw new VaultError('Saved data could not be read (wrong passphrase or damaged).')
    }
  }
}
