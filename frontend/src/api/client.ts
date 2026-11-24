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
  constructor(message: string, uncertain: boolean, status?: number) { super(message); this.uncertain=uncertain; this.status=status }
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
export async function api(path: string, method = 'GET', body?: Fact, request: typeof fetch = fetch): Promise<Fact> {
  let response: Response
  try {
    response = await request(`/api/v1${path}`, {method, cache: 'no-store', signal: AbortSignal.timeout(path === '/market/refresh' ? 120000 : 15000),
      ...(body === undefined ? {} : {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})})
  } catch { throw new ApiError('连接失败或请求超时；写入结果可能未知', method !== 'GET') }
  let data: unknown
  try { data = await response.json() } catch { throw new ApiError(`服务端返回无效响应 (HTTP ${response.status})`, method !== 'GET', response.status) }
  if (!response.ok) throw new ApiError(`HTTP ${response.status}: ${detail(data)}`, method !== 'GET' && response.status >= 500, response.status)
  try { return fact(data) } catch { throw new ApiError('服务端返回无效响应', method !== 'GET') }
}
export type Operation = {path: string; method: string; body: Fact}
/** Retain the same immutable payload and request ID on an ambiguous write failure. */
export class WriteAttempt {
  pending: Operation | null = null
  private readonly send: typeof api
  private readonly newId: () => string
  constructor(send = api, newId = () => crypto.randomUUID()) { this.send=send; this.newId=newId }
  async start(path: string, body: Fact, method = 'POST'): Promise<Fact> {
    if (this.pending) throw new Error('上一笔写入尚未核实，请先重试')
    this.pending = {path, method, body: structuredClone({...body, request_id: this.newId()})}
    return this.retry()
  }
  async retry(): Promise<Fact> {
    const op = this.pending
    if (!op) throw new Error('没有待重试请求')
    try {
      const result = await this.send(op.path, op.method, op.body)
      this.pending = null
      return result
    } catch (error) {
      if (error instanceof ApiError && !error.uncertain) this.pending = null
      throw error
    }
  }
}
