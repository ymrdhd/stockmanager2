/** Shared JSON boundary. All monetary projections originate on the server. */
export type Fact = Record<string, unknown>
export type Page = { items: Fact[]; ledger_revision: number; total: number; limit: number; offset: number }
export function fact(value: unknown): Fact {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('服务端响应不是对象')
  return value as Fact
}
export function facts(value: unknown): Fact[] {
  if (!Array.isArray(value)) throw new Error('服务端响应不是列表')
  return value.map(fact)
}
export function str(value: unknown): string { return typeof value === 'string' ? value : '' }
export function page(value: unknown): Page {
  const r = fact(value)
  for (const key of ['ledger_revision', 'total', 'limit', 'offset']) {
    if (typeof r[key] !== 'number' || !Number.isSafeInteger(r[key]) || r[key] < 0) throw new Error('列表版本或分页响应无效')
  }
  return {items: facts(r.items), ledger_revision: Number(r.ledger_revision), total: Number(r.total), limit: Number(r.limit), offset: Number(r.offset)}
}
export class ApiError extends Error {
  readonly uncertain: boolean
  readonly status?: number
  readonly code: string
  constructor(message: string, uncertain: boolean, status?: number, code = '') {
    super(message); this.uncertain = uncertain; this.status = status; this.code = code
  }
}
function detail(body: unknown): string {
  try {
    const d = fact(body).detail
    if (typeof d === 'string') return d
    if (Array.isArray(d)) return d.map(e => {
      const v = fact(e)
      return `${Array.isArray(v.loc) ? v.loc.join('.') : ''}: ${str(v.msg)}`
    }).join('; ')
    return str(fact(d).message) || '请求失败'
  } catch { return '请求失败' }
}
function code(body: unknown): string {
  try { return str(fact(fact(body).detail).code) } catch { return '' }
}
/** Database identity a write is allowed to touch, and the CSRF token of the session. */
export type Guard = { csrf?: string; identity?: [string, number] }
const noGuard: Guard = {}
let guard: () => Guard = () => noGuard
export function setGuard(next: () => Guard): void { guard = next }
function writeHeaders(current: Guard, withJson: boolean): Record<string, string> {
  const headers: Record<string, string> = {}
  if (withJson) headers['Content-Type'] = 'application/json'
  if (current.csrf) headers['x-csrf-token'] = current.csrf
  if (current.identity) {
    headers['x-database-uuid'] = current.identity[0]
    headers['x-database-generation'] = String(current.identity[1])
  }
  return headers
}
export async function api(path: string, method = 'GET', body?: Fact, request: typeof fetch = fetch): Promise<Fact> {
  let response: Response
  const headers = method === 'GET' ? {} : writeHeaders(guard(), body !== undefined)
  try {
    response = await request(`/api/v1${path}`, {method, cache: 'no-store', signal: AbortSignal.timeout(path === '/market/refresh' ? 120000 : 15000),
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
      ...(Object.keys(headers).length === 0 ? {} : {headers})})
  } catch { throw new ApiError('连接失败或请求超时；写入结果可能未知', method !== 'GET') }
  let data: unknown
  try { data = await response.json() } catch { throw new ApiError(`服务端返回无效响应 (HTTP ${response.status})`, method !== 'GET', response.status) }
  if (!response.ok) throw new ApiError(`HTTP ${response.status}: ${detail(data)}`, method !== 'GET' && response.status >= 500, response.status, code(data))
  try { return fact(data) } catch { throw new ApiError('服务端返回无效响应', method !== 'GET') }
}

/** The database identity a pending write was created against. */
export type PendingIdentity = { uuid: string; generation: number }
/**
 * A write that may already have reached the ledger. It is persisted before the
 * request is sent and is only removed once the server has definitively answered
 * for the *same* database identity, or the operator explicitly abandons it.
 */
export type Operation = {
  path: string
  method: string
  body: Fact
  identity: PendingIdentity | null
  createdAt: string
}
type Storage = {getItem(key: string): string | null; setItem(key: string, value: string): void; removeItem(key: string): void}
const memory = new Map<string, string>()
const memoryStore: Storage = {
  getItem: k => memory.get(k) ?? null,
  setItem: (k, v) => { memory.set(k, v) },
  removeItem: k => { memory.delete(k) },
}
/**
 * The record must survive closing the tab and restarting the browser, so it uses
 * localStorage and is shared by every same-origin tab. sessionStorage or memory is
 * only a last resort when no durable store exists (tests, private lockdown modes).
 */
const store: Storage = typeof globalThis.localStorage === 'undefined'
  ? (typeof globalThis.sessionStorage === 'undefined' ? memoryStore : globalThis.sessionStorage)
  : globalThis.localStorage
const PENDING_KEY = 'sm2.pending-write'
const STORAGE_EVENT = 'sm2.pending-write-changed'

function identityOf(current: Guard['identity']): PendingIdentity | null {
  return current ? {uuid: current[0], generation: Number(current[1])} : null
}
function sameIdentity(a: PendingIdentity | null, b: PendingIdentity | null): boolean {
  if (a === null || b === null) return a === b
  return a.uuid === b.uuid && Number(a.generation) === Number(b.generation)
}
function identityText(identity: PendingIdentity | null): string {
  return identity === null ? '未绑定数据库身份' : `${identity.uuid} 第 ${identity.generation} 代`
}
/**
 * Whether a failed first attempt proves the ledger did not accept the write, so
 * the record can be dropped. Session, permission, identity and network answers
 * are excluded on purpose: none of them proves the earlier attempt did not post.
 */
function clearable(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  if (error.uncertain || error.status === undefined) return false
  if (error.code === 'storage_unavailable') return false
  if (error.status === 401 || error.status === 403) return false
  if (error.status === 409) return error.code !== 'identity_mismatch' && error.code !== 'identity_required'
  return error.status >= 400 && error.status < 500 && error.status !== 408
}
/** Parse whatever an older build may have written; a bad record is never guessed away. */
function parseOperation(raw: string): Operation {
  const value = fact(JSON.parse(raw))
  if (typeof value.path !== 'string' || !value.path) throw new Error('invalid path')
  if (typeof value.method !== 'string' || !value.method) throw new Error('invalid method')
  const body = fact(value.body)
  if (typeof body.request_id !== 'string' || !body.request_id) throw new Error('invalid request id')
  let identity: PendingIdentity | null = null
  if (typeof value.identity === 'string' && value.identity) {
    const pair = JSON.parse(value.identity)
    if (!Array.isArray(pair) || pair.length !== 2) throw new Error('invalid identity')
    identity = {uuid: String(pair[0]), generation: Number(pair[1])}
  } else if (value.identity !== null && value.identity !== undefined) {
    const record = fact(value.identity)
    identity = {uuid: str(record.uuid), generation: Number(record.generation)}
    if (!identity.uuid || !Number.isSafeInteger(identity.generation)) throw new Error('invalid identity')
  }
  return {path: value.path, method: value.method, body, identity,
    createdAt: typeof value.createdAt === 'string' ? value.createdAt : ''}
}

/**
 * Retain the same immutable payload and request ID on an ambiguous write failure,
 * across a page reload and a browser restart, so a retry can never double-post the
 * ledger and an operator can always reconcile the original request ID by hand.
 */
export class WriteAttempt {
  pending: Operation | null = null
  /** True when storage held a record that cannot be parsed: writes stay blocked. */
  corrupt = false
  /** Reason the last persistence attempt failed, for an explicit manual-check notice. */
  storageError = ''
  private readonly send: typeof api
  private readonly newId: () => string
  private readonly target: Storage
  private readonly listeners = new Set<() => void>()
  constructor(send: typeof api = api, newId: () => string = () => crypto.randomUUID(), storage: Storage = store) {
    this.send = send; this.newId = newId; this.target = storage
    const restored = this.restore()
    this.pending = restored.pending
    this.corrupt = restored.corrupt
    if (typeof globalThis.addEventListener === 'function') {
      globalThis.addEventListener('storage', event => {
        if ((event as StorageEvent).key !== null && (event as StorageEvent).key !== PENDING_KEY) return
        const next = this.restore()
        this.pending = next.pending
        this.emit()
      })
    }
  }
  /** Subscribe to changes so the page and other tabs stay consistent. */
  onChange(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  private emit(): void {
    for (const listener of this.listeners) listener()
    if (typeof globalThis.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
      globalThis.dispatchEvent(new CustomEvent(STORAGE_EVENT))
    }
  }
  private restore(): {pending: Operation | null; corrupt: boolean} {
    let raw: string | null = null
    try { raw = this.target.getItem(PENDING_KEY) } catch { return {pending: null, corrupt: false} }
    if (!raw) return {pending: null, corrupt: false}
    try {
      return {pending: parseOperation(raw), corrupt: false}
    } catch {
      // Never drop an unreadable record: the operator must check the ledger by hand.
      return {pending: null, corrupt: true}
    }
  }
  private persist(op: Operation | null): void {
    if (op === null) {
      this.target.removeItem(PENDING_KEY)
      return
    }
    // A failed write of the record must abort the business request, never proceed blindly.
    this.target.setItem(PENDING_KEY, JSON.stringify(op))
  }
  private save(op: Operation | null): void {
    try {
      this.persist(op)
    } catch {
      this.storageError = '浏览器存储不可用或已满，无法可靠记录待核实请求；请改用可持久化的浏览器配置后人工核对账本'
      throw new ApiError(this.storageError, false, undefined, 'storage_unavailable')
    }
    this.pending = op
    if (op === null) this.storageError = ''
    this.emit()
  }
  get requestId(): string { return this.pending ? str(this.pending.body.request_id) : '' }
  get blocked(): boolean { return this.pending !== null || this.corrupt }
  async start(path: string, body: Fact, method = 'POST'): Promise<Fact> {
    if (this.corrupt) throw new ApiError('本机保留了无法解析的待核实写入记录，必须先人工核对原账本', false, undefined, 'pending_corrupt')
    if (this.pending) throw new Error('上一笔写入尚未核实，请先重试')
    // Persist first: if the record cannot be stored, the write is never sent.
    this.save({path, method, body: structuredClone({...body, request_id: this.newId()}),
      identity: identityOf(guard().identity), createdAt: new Date().toISOString()})
    return this.attempt(true)
  }
  /**
   * Ask the server whether the unconfirmed request already reached the ledger.
   * Only an answer carrying the *same* database UUID and generation is accepted.
   */
  async check(): Promise<Fact> {
    const op = this.pending
    if (!op) throw new Error('没有待核实的请求')
    const answer = await this.send(`/requests/${encodeURIComponent(str(op.body.request_id))}`, 'GET')
    const current = identityOf(guard().identity)
    if (op.identity !== null) {
      const uuid = str(answer.database_uuid)
      const generation = Number(answer.generation)
      if (uuid !== op.identity.uuid || generation !== Number(op.identity.generation)) {
        throw new ApiError(
          `服务端返回的数据库身份（${uuid || '空'} 第 ${generation} 代）与原记录（${identityText(op.identity)}）不一致；`
          + '不能用另一个数据库的结果宣布原操作成功，请人工核对原账本', false, undefined, 'identity_changed')
      }
    } else if (current !== null) {
      // The record predates identity binding while a database is active now: the
      // answer may belong to any database, so it cannot settle the original write.
      throw new ApiError('原记录没有绑定数据库身份，无法确认该结果属于当前数据库；请人工核对原账本',
        false, undefined, 'identity_unbound')
    }
    return answer
  }
  /**
   * Resend the identical payload under the identical request ID.
   *
   * The record is cleared only when the ledger definitively answered *this*
   * request: a first attempt rejected on business grounds (conflict, invalid
   * input, not found). Everything else keeps it — in particular a 401/403/CSRF
   * failure or a later retry rejection never proves the original write did not
   * commit, so the operator can still reconcile it by request ID.
   */
  async retry(): Promise<Fact> { return this.attempt(false) }
  private async attempt(initial: boolean): Promise<Fact> {
    const op = this.pending
    if (!op) throw new Error('没有待重试请求')
    const current = identityOf(guard().identity)
    if (!sameIdentity(op.identity, current)) {
      // Another database (or another generation) is active: the old request may
      // already be committed somewhere this session can no longer see.
      throw new ApiError(
        `数据库身份已变化（原记录 ${identityText(op.identity)}，当前 ${identityText(current)}），`
        + '不能跨数据库或跨代重试；请先核实原请求状态或放弃待确认记录',
        false, undefined, 'identity_changed')
    }
    try {
      const result = await this.send(op.path, op.method, op.body)
      this.save(null)
      return result
    } catch (error) {
      if (initial && clearable(error)) this.save(null)
      throw error
    }
  }
  /** Explicit operator decision to abandon an unconfirmed write; the ledger still needs a manual check. */
  discard(): void { this.save(null) }
  /** Drop an unreadable record after the operator has reconciled the ledger by hand. */
  clearCorrupt(): void {
    try { this.target.removeItem(PENDING_KEY) } catch { /* nothing else can be done */ }
    this.pending = null
    this.corrupt = false
    this.emit()
  }
}
