import {str, type Fact} from './api/client.ts'

export function money(value: unknown): string {
  if (value === null || value === undefined) return '不可用'
  if (typeof value !== 'string' && (typeof value !== 'number' || !Number.isSafeInteger(value))) return '不可用'
  if (!/^-?\d+$/.test(String(value))) return '不可用'
  const n = BigInt(String(value)), abs = n < 0n ? -n : n
  return `${n < 0n ? '-' : ''}¥${(abs / 100n).toLocaleString('zh-CN')}.${String(abs % 100n).padStart(2, '0')}`
}
export function integer(value: string): number {
  if (!/^-?\d+$/.test(value)) throw new Error('请输入整数')
  const n = Number(value)
  if (!Number.isSafeInteger(n)) throw new Error('整数超出安全范围')
  return n
}
export function fen(value: string): number {
  if (!/^-?\d+(?:\.\d{1,2})?$/.test(value)) throw new Error('金额必须是最多两位小数的人民币金额')
  const negative = value.startsWith('-'), [a, b = ''] = value.replace(/^-/, '').split('.')
  const n = BigInt(a) * 100n + BigInt(b.padEnd(2, '0'))
  return integer(String(negative ? -n : n))
}
export function yuan(value: unknown): string {
  if (value === null || value === undefined) return ''
  const s = String(value)
  if (!/^-?\d+$/.test(s)) return ''
  const n = BigInt(s), a = n < 0n ? -n : n
  return `${n < 0n ? '-' : ''}${a / 100n}.${String(a % 100n).padStart(2,'0')}`
}
export function shanghaiTime(value: string): string {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) throw new Error('请填写北京时间（含日期）')
  const time = new Date(`${value}+08:00`)
  if (Number.isNaN(time.getTime())) throw new Error('时间无效')
  return `${value.length === 16 ? value + ':00' : value}+08:00`
}
export function localTime(value: unknown): string {
  if (!str(value)) return ''
  const time = new Date(str(value))
  if (Number.isNaN(time.getTime())) return ''
  // Fixed Shanghai offset for current exchange-era timestamps; use Intl for historical data.
  const parts = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai', year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(time)
  return parts.replace(' ', 'T')
}
export const now = () => localTime(new Date().toISOString())
const names: Record<string,string> = {valid:'有效', unavailable:'不可用', derived_unverified:'未核实', conflict:'冲突', stale:'过期', missing:'缺失', pending:'待核对', effective:'有效', reversed:'已冲正', active:'启用', closed:'关闭', securities:'证券', bank:'银行', cash:'现金', payment:'支付账户', other_asset:'其他资产', credit_card:'信用卡', other_liability:'其他负债', buy:'买入', sell:'卖出', ordinary:'普通', main:'主仓', sub:'子仓', income:'收入', expense:'支出', fee:'手续费', refund:'退款', posted:'已入账', corrected:'已更正', voided:'已作废', verified:'已核实', unverified:'未核实', opening:'期初', deposit:'入金', withdrawal:'出金', adjustment:'余额校正', dividend:'分红', server:'正式服务', sandbox:'可写沙盒', readonly:'只读', development:'无数据库开发模式', asset:'资产', liability:'负债'}
export function display(value: unknown, key = ''): string {
  if (value === null || value === undefined) return '不可用'
  if (key.endsWith('_fen')) return money(value)
  if ((key.endsWith('_at') || key === 'as_of') && typeof value === 'string') return localTime(value).replace('T',' ') + ' 北京时间'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'object') return JSON.stringify(value)
  return names[String(value)] ?? String(value)
}
export type Option = {value: string; label: string}
export type Field = {key: string; label: string; type?: 'text'|'date'|'datetime-local'|'select'; options?: Option[]; required?: boolean; initial?: string; hint?: string}
export type FormSpec = {title: string; path: string; method?: string; fields: Field[]; build: (values: Record<string,string>) => Fact; confirm?: string}
export const option = (value: string, label: string): Option => ({value,label})
