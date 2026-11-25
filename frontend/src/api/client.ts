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
/** A failed read that cannot establish the current authenticated service is safe.
 * Ordinary validation/version conflicts remain local to the requesting view. */
export function readConnectionFailure(error: unknown): error is ApiError {
  return error instanceof ApiError && (error.status === undefined || error.status === 401 || error.status === 403 || error.status === 408 || error.status >= 500 || (error.status >= 200 && error.status < 300))
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
const PENDING_KEY = 'sm2.pending-write'
// A single origin-wide write slot. A completed/abandoned slot leaves a durable
// fence, not a payload fingerprint. Old forms cannot reopen it even after reload.
const INTENT_KEY = 'sm2.write-intent-fence'
export type WriteIntent = string | null
const STORAGE_EVENT = 'sm2.pending-write-changed'
const STORAGE_MESSAGE = '浏览器存储不可用或已满，无法可靠记录待核实请求；请改用可持久化的浏览器配置后人工核对账本'
/** Refusing is the only safe answer when two tabs cannot be serialised for real. */
const MUTEX_MESSAGE = '当前浏览器没有可用的同源互斥锁（Web Locks API），无法保证两个标签页不会同时写入；请改用支持该能力的浏览器后再提交'
const memory = new Map<string, string>()
const memoryStore: Storage = {
  getItem: k => memory.get(k) ?? null,
  setItem: (k, v) => { memory.set(k, v) },
  removeItem: k => { memory.delete(k) },
}
/** A browser without durable storage must fail loudly instead of pretending to persist a write. */
const unusableStore: Storage = {
  getItem: () => { throw new Error('no durable storage') },
  setItem: () => { throw new Error('no durable storage') },
  removeItem: () => { throw new Error('no durable storage') },
}
/**
 * The record must survive closing the tab and restarting the browser, so it uses
 * localStorage and is shared by every same-origin tab. Only outside a browser
 * (tests, SSR) may a transient store stand in; a real browser never degrades
 * silently, because a record kept only in memory is lost on reload.
 */
function defaultStore(): Storage {
  let local: Storage|undefined
  try {local=(globalThis as {localStorage?: Storage}).localStorage} catch {return unusableStore}
  if (local) return local
  if (typeof window !== 'undefined') return unusableStore
  const transient = (globalThis as {sessionStorage?: Storage}).sessionStorage
  return transient ?? memoryStore
}
const store = defaultStore()

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
 * A mutual exclusion primitive that really serialises critical sections; null when
 * this runtime cannot offer one.
 */
export type WriteMutex = {request<T>(name: string, options: {mode: 'exclusive'}, callback: () => Promise<T>): Promise<T>}
/**
 * Same-process exclusion for Node tests and SSR. It is one module-level queue, so
 * every instance in this runtime shares it — unlike a per-instance flag, which
 * would let two instances in one process race exactly like two tabs do.
 */
const localQueues = new Map<string, Promise<unknown>>()
const localMutexImpl: WriteMutex = {
  request<T>(name: string, _options: {mode: 'exclusive'}, callback: () => Promise<T>): Promise<T> {
    const previous = localQueues.get(name) ?? Promise.resolve()
    const run = previous.then(callback, callback)
    localQueues.set(name, run.then(() => undefined, () => undefined))
    return run
  },
}
/**
 * Prefer the Web Locks API: it serialises same-origin tabs for real. A browser
 * without it gets null, so writes are refused instead of racing through an
 * optimistic localStorage flag or a timer, which have real race windows.
 */
function defaultMutex(): WriteMutex | null {
  const locks = (globalThis as {navigator?: {locks?: unknown}}).navigator?.locks
  const api = locks as WriteMutex | undefined
  if (api && typeof api.request === 'function') return api
  return typeof window === 'undefined' ? localMutexImpl : null
}

/**
 * Retain the same immutable payload and request ID on an ambiguous write failure,
 * across a page reload and a browser restart, so a retry can never double-post the
 * ledger and an operator can always reconcile the original request ID by hand.
 *
 * Every step that reads or replaces the shared record runs inside a same-origin
 * mutex named after the single origin-wide record key, and re-reads the
 * durable record inside that lock: two tabs can no longer both pass a stale
 * in-memory check and each persist their own write.
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
  private readonly mutex: WriteMutex | null
  private readonly listeners = new Set<() => void>()
  /** True when the durable store refused a read: writes must not be sent blindly. */
  private failed = false
  constructor(send: typeof api = api, newId: () => string = () => crypto.randomUUID(), storage: Storage = store,
    locks: WriteMutex | null | undefined = undefined) {
    this.send = send; this.newId = newId; this.target = storage
    this.mutex = locks === undefined ? defaultMutex() : locks
    const restored = this.restore()
    this.pending = restored.pending
    this.corrupt = restored.corrupt
    if (typeof globalThis.addEventListener === 'function') {
      globalThis.addEventListener('storage', event => {
        if ((event as StorageEvent).key !== null && (event as StorageEvent).key !== PENDING_KEY) return
        // Another tab may have created, retried, replaced or broken the record: follow both fields.
        const next = this.restore()
        this.pending = next.pending
        this.corrupt = next.corrupt
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
  private state(pending: Operation | null, corrupt: boolean): void {
    this.pending = pending
    this.corrupt = corrupt
    this.emit()
  }
  /** Adopt the durable state without notifying listeners; the caller decides when to emit. */
  private quiet(next: {pending: Operation | null; corrupt: boolean}): void {
    this.pending = next.pending
    this.corrupt = next.corrupt
  }
  private restore(): {pending: Operation | null; corrupt: boolean} {
    let raw: string | null = null
    try { raw = this.target.getItem(PENDING_KEY) } catch {
      // A store that cannot be read cannot prove anything about the ledger.
      this.failed = true
      this.storageError = STORAGE_MESSAGE
      return {pending: null, corrupt: false}
    }
    this.failed = false
    if (!raw) return {pending: null, corrupt: false}
    try {
      return {pending: parseOperation(raw), corrupt: false}
    } catch {
      // Never drop an unreadable record: the operator must check the ledger by hand.
      return {pending: null, corrupt: true}
    }
  }
  /** The durable record, or a refusal: never a silently empty state over an unreadable store. */
  private read(): {pending: Operation | null; corrupt: boolean} {
    const next = this.restore()
    if (this.failed) throw new ApiError(this.storageError || STORAGE_MESSAGE, false, undefined, 'storage_unavailable')
    return next
  }
  /** One durable record per origin: every identity shares its lock. */
  private lockName(): string { return PENDING_KEY }
  private requireMutex(): void {
    if (this.mutex === null) throw new ApiError(MUTEX_MESSAGE, false, undefined, 'mutex_unavailable')
  }
  private async locked<T>(work: () => Promise<T>): Promise<T> {
    this.requireMutex()
    return (this.mutex as WriteMutex).request(this.lockName(), {mode: 'exclusive'}, work)
  }
  /** Persist before sending: a record that cannot be stored aborts the business request. */
  private save(op: Operation): void {
    try {
      this.target.setItem(PENDING_KEY, JSON.stringify(op))
    } catch {
      this.storageError = STORAGE_MESSAGE
      throw new ApiError(this.storageError, false, undefined, 'storage_unavailable')
    }
    this.storageError = ''
    this.state(op, false)
  }
  /**
   * Remove the record only when the stored request ID is still the expected one.
   * It runs inside the lock, so a record replaced by another tab is never deleted
   * by mistake; that case returns false and keeps the other tab's evidence.
   */
  private clearLocked(expectedRequestId: string): boolean {
    const current = this.read()
    if (current.corrupt) { this.state(null, true); return false }
    const stored = current.pending === null ? '' : str(current.pending.body.request_id)
    if (current.pending === null || !expectedRequestId || stored !== expectedRequestId) {
      this.state(current.pending, false)
      return false
    }
    try {
      // Publish the terminal fence BEFORE removing the pending record. If either
      // storage operation fails, the original request remains recoverable.
      this.target.setItem(INTENT_KEY, expectedRequestId)
      this.target.removeItem(PENDING_KEY)
    } catch {
      this.state(current.pending, false)
      this.storageError = STORAGE_MESSAGE
      throw new ApiError(this.storageError, false, undefined, 'storage_unavailable')
    }
    this.storageError = ''
    this.state(null, false)
    return true
  }
  get requestId(): string { return this.pending ? str(this.pending.body.request_id) : '' }
  get blocked(): boolean { return this.pending !== null || this.corrupt }
  /**
   * Persist the new request and send it, deciding *inside* the lock: a record
   * written by another tab between this object's last read and now still wins.
   */
  /** Capture at explicit form opening, not after async confirmation or lock acquisition.
   * Two forms opened in the same slot share the intent fence. Starting a new
   * form after completion explicitly authorizes a new slot, even for identical facts.
   */
  beginIntent(): WriteIntent {
    try { return this.target.getItem(INTENT_KEY) } catch {
      throw new ApiError(STORAGE_MESSAGE, false, undefined, 'storage_unavailable')
    }
  }
  async start(path: string, body: Fact, method = 'POST', intent: WriteIntent = this.beginIntent()): Promise<Fact> {
    const payload = structuredClone(body)
    const identity = identityOf(guard().identity)
    return this.locked(async () => {
      const current = this.read()
      this.quiet(current)
      if (current.corrupt) {
        throw new ApiError('本机保留了无法解析的待核实写入记录，必须先人工核对原账本', false, undefined, 'pending_corrupt')
      }
      if (current.pending) {
        throw new ApiError('上一笔写入尚未核实，请先重试', false, undefined, 'pending_exists')
      }
      if (intent !== this.beginIntent()) throw new ApiError('此写入意图已由其他标签页处理或失效；请核对账本，若确有另一笔真实交易，请重新打开表单',false,undefined,'intent_stale')
      if (!sameIdentity(identity, identityOf(guard().identity))) throw new ApiError('数据库身份已变化，未发送新请求',false,undefined,'identity_changed')
      const op: Operation = {path, method, body: {...payload, request_id: this.newId()},
        identity, createdAt: new Date().toISOString()}
      this.save(op)
      return this.attempt(true)
    })
  }
  /**
   * Ask the server whether the unconfirmed request already reached the ledger.
   * Only an answer carrying the *same* database UUID and generation is accepted.
   */
  async check(expected = this.requestId): Promise<Fact> {
    return this.locked(async () => {
      const current = this.read()
      this.quiet(current)
      const op = current.pending
      if (!op||str(op.body.request_id)!==expected) throw new Error('待核实请求已变化，请重新核对')
      if (!sameIdentity(op.identity,identityOf(guard().identity)) && op.identity!==null) throw new ApiError('不能跨数据库核实原记录',false,undefined,'identity_changed')
      const answer = await this.send(`/requests/${encodeURIComponent(str(op.body.request_id))}`, 'GET')
      const currentIdentity = identityOf(guard().identity)
      if (op.identity !== null) {
        const uuid = str(answer.database_uuid)
        const generation = Number(answer.generation)
        if (uuid !== op.identity.uuid || generation !== Number(op.identity.generation)) {
          throw new ApiError(
            `服务端返回的数据库身份（${uuid || '空'} 第 ${generation} 代）与原记录（${identityText(op.identity)}）不一致；`
            + '不能用另一个数据库的结果宣布原操作成功，请人工核对原账本', false, undefined, 'identity_changed')
        }
      } else if (currentIdentity !== null) {
        // The record predates identity binding while a database is active now: the
        // answer may belong to any database, so it cannot settle the original write.
        throw new ApiError('原记录没有绑定数据库身份，无法确认该结果属于当前数据库；请人工核对原账本',
          false, undefined, 'identity_unbound')
      }
      return answer
    })
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
  async retry(expected = this.requestId): Promise<Fact> {
    return this.locked(async () => {
      this.quiet(this.read())
      if (!expected||this.requestId!==expected) throw new Error('待重试请求已变化，请重新核对')
      return this.attempt(false)
    })
  }
  private async attempt(initial: boolean): Promise<Fact> {
    const op = this.pending
    if (!op) throw new Error('没有待重试请求')
    const requestId = str(op.body.request_id)
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
      this.clearLocked(requestId)
      return result
    } catch (error) {
      if (initial && clearable(error)) this.clearLocked(requestId)
      throw error
    }
  }
  /**
   * Explicit operator decision to abandon an unconfirmed write; the ledger still
   * needs a manual check. The durable delete is confirmed inside the lock against
   * the original request ID, and the local state only follows that verdict.
   */
  discard(expected = this.requestId): Promise<boolean> {
    this.requireMutex()
    return this.locked(async () => {
      const current = this.read()
      if (current.pending && !sameIdentity(current.pending.identity, identityOf(guard().identity)))
        throw new ApiError('不能跨数据库身份清除待确认记录', false, undefined, 'identity_changed')
      return this.clearLocked(expected)
    })
  }
  /** Drop an unreadable record after the operator has reconciled the ledger by hand. */
  clearCorrupt(): Promise<boolean> {
    this.requireMutex()
    return this.locked(async () => {
      const current = this.read()
      if (!current.corrupt) { this.state(current.pending, false); return false }
      try {
        this.target.setItem(INTENT_KEY, this.newId())
        this.target.removeItem(PENDING_KEY)
      } catch {
        this.state(null, true)
        this.storageError = STORAGE_MESSAGE
        throw new ApiError(this.storageError, false, undefined, 'storage_unavailable')
      }
      this.storageError = ''
      this.state(null, false)
      return true
    })
  }
}
