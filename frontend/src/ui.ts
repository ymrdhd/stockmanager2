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
const names: Record<string,string> = {manual_balance_snapshot:'手工余额快照',included_account_projection:'纳入统计账户投影',known_liquidity_classification:'已知流动性分类',effective_cash_ledger:'有效证券现金账本',sqlite_ledger_and_explicit_quotes:'SQLite 账本与明确报价',effective_finance_bill_ledger:'有效财务账单账本',unadjusted_security:'证券未复权价格',valid:'有效', unavailable:'不可用', derived_unverified:'未核实', conflict:'冲突', stale:'过期', missing:'缺失', pending:'待核对', effective:'有效', reversed:'已冲正', active:'启用', closed:'关闭', securities:'证券', bank:'银行', cash:'现金', payment:'支付账户', other_asset:'其他资产', credit_card:'信用卡', other_liability:'其他负债', buy:'买入', sell:'卖出', ordinary:'普通', main:'主仓', sub:'子仓', income:'收入', expense:'支出', fee:'手续费', refund:'退款', posted:'已入账', corrected:'已更正', voided:'已作废', verified:'已核实', unverified:'未核实', opening:'期初', deposit:'入金', withdrawal:'出金', adjustment:'余额校正', dividend:'分红', server:'正式服务', sandbox:'可写沙盒', readonly:'只读', development:'无数据库开发模式', asset:'资产', liability:'负债'}

// These are stable API reason codes. Unknown values are deliberately preserved so a
// newer backend reason is never hidden by an incomplete frontend dictionary.
const reasonNames: Record<string,string> = {
  before_confirmed_position_baseline:'早于已核实的期初持仓基准',
  pending_trade:'存在待核对成交',
  unresolved_company_action:'公司行为尚未核实或应用',
  company_action_invalidated_strategy_scale:'公司行为改变了价格尺度，原冻结 ATR/保护价不可继续使用',
  previous_close_unavailable:'上一交易日有效收盘价不可用',
  opening_position_during_day:'当日新增期初持仓，日收益不可计算',
  trade_on_non_session_day:'成交日期不在可信交易日历的交易时段',
  equity_reconciliation_unavailable:'缺少完成证券权益对账所需的资金或估值',
  equity_reconciliation_difference:'证券权益与真实资金/持仓事实存在差额',
  cumulative_baseline_incomplete:'累计收益的起始基准不完整',
  cumulative_baseline_missing:'缺少已核实的累计收益起始基准',
  not_trading_session:'当前日期不是交易日',
  prior_close_equity_unavailable:'上一交易日收盘权益不可用',
  daily_pnl_unavailable:'当日投资收益不可计算',
  prior_close_equity_nonpositive:'上一交易日收盘权益不是正数',
  account_return_or_denominator_unavailable:'账户收益或收益率分母不可用',
  no_included_account_evidence:'没有可用于统计的账户证据',
  missing_balance:'缺少该账户的已核实余额',
  missing_or_ambiguous_cash_baseline:'缺少唯一证券现金期初记录',
  cash_fact_conflicts_with_baseline:'资金事实与期初基准冲突',
  historical_cash_shortfall:'历史现金余额不足',
  account_existence_unverified:'账户实际生效时间尚未核实',
  transfer_inclusion_snapshot_not_yet_effective:'转账引用的手工余额快照在该历史时点尚未生效',
  transfer_reconciliation_historical_gap:'转账人工核对形成了无法还原的历史余额区间',
  legacy_unpaired_transfer:'旧转账缺少可核对的配对记录',
  balance_or_valuation_unavailable:'账户余额或证券估值不可用',
  unverified_balance_or_equity:'账户余额或证券权益尚未核实',
  signal_link_unverified:'真实成交与正式策略信号的关联尚未核实',
  strategy_deviation:'实际成交偏离策略规则',
  strategy_position_not_recorded:'没有找到对应的正式策略持仓记录',
  frozen_atr_or_signal_date_missing:'缺少冻结 ATR 或原始信号日期',
  security_is_not_a_configured_etf:'该证券不是已配置的策略 ETF',
  ndx_aux_evidence_unverified:'NDX 辅助仓证据尚未核实',
  historical_configuration_not_versioned:'历史策略配置没有完整版本记录',
  ledger_corrections_not_bitemporal:'历史账本更正缺少双时态证据',
  historical_replay_incomplete:'历史策略回放证据不完整',
  cash_unverified:'证券现金余额未核实',
  cash_shortfall:'可用现金不足',
  simultaneous_cash_or_slot_competition:'多个计划同时竞争现金或核心仓位',
  transaction_capacity_refused:'事务内容量检查拒绝该计划',
  signal_already_consumed:'该正式信号已被使用',
  read_only_signal_not_recorded:'只读查询不会写入正式信号',
  calendar_unavailable:'可信交易日历不可用或已超出覆盖范围',
  series_unavailable:'所需行情序列不可用',
}

export function reasonText(value: unknown): string {
  if (value === null || value === undefined) return '未提供原因'
  if (typeof value !== 'string') return String(value)
  const raw = value.trim()
  if (!raw) return '未提供原因'
  if (raw.startsWith('liquidity_classification_unknown:')) {
    return `账户 ${raw.slice('liquidity_classification_unknown:'.length)}：流动性分类待确认（受限/不受限），暂不计入流动资产`
  }
  const separator = raw.indexOf(':')
  if (separator > 0) {
    const scope = raw.slice(0, separator), code = raw.slice(separator + 1)
    if (reasonNames[code]) return `${scope}：${reasonNames[code]}`
    if (code.startsWith('current_quote_')) {
      const quality = code.slice('current_quote_'.length)
      return `${scope}：当前报价${quality === 'stale' ? '已过期' : quality === 'unavailable' ? '不可用' : `质量为 ${quality}`}`
    }
  }
  return reasonNames[raw] ?? names[raw] ?? raw
}

const reasonListKeys = new Set(['reason','reasons','reason_codes','liquidity_reasons','warnings','pending_reason','daily_return_reason','related_reasons'])

export function auditDetails(value: unknown): string {
  if (value === null || value === undefined || value === '') return '无附加审计详情'
  let parsed: unknown = value
  if (typeof value === 'string') {
    try { parsed = JSON.parse(value) as unknown } catch { return value }
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return String(parsed)
  const labels: Record<string,string> = {
    related_transaction_id:'关联原支出', category_id:'账单分类', note:'备注', id:'记录编号', before:'更正前', after:'更正后',
    from_account_id:'转出账户', to_account_id:'转入账户', before_fen:'原余额', after_fen:'核实余额',
    reason:'原因', strategy_deviation:'策略偏离', correction_of:'更正原记录', reversal_id:'冲正记录',
    request_id:'请求编号', signal_id:'正式信号', signal_date:'信号日期', frozen_atr:'冻结 ATR',
    amount_fen:'金额', fees_fen:'费用', occurred_at:'发生时间', executed_at:'成交时间',
    status:'状态', pending_reason:'待核对原因', source:'来源', kind:'类型', account_id:'账户',
    instrument_id:'证券', external_id:'券商成交编号', confirmed:'已核实',
  }
  const formatValue = (key: string, item: unknown): string => {
    if (item === null || item === undefined) return '未提供'
    if (key.endsWith('_fen')) return money(item)
    if ((key.endsWith('_at') || key === 'as_of') && typeof item === 'string') return display(item, key)
    if (key === 'reason' || key === 'pending_reason') return reasonText(item)
    if (typeof item === 'boolean') return item ? '是' : '否'
    if (Array.isArray(item)) return item.map(part => typeof part === 'string' ? reasonText(part) : JSON.stringify(part)).join('、')
    if (typeof item === 'object') return JSON.stringify(item)
    return names[String(item)] ?? String(item)
  }
  return Object.entries(parsed as Record<string, unknown>)
    .map(([key, item]) => `${labels[key] ?? key}：${formatValue(key, item)}`)
    .join('；') || '无附加审计详情'
}

export function display(value: unknown, key = ''): string {
  if (value === null || value === undefined) return '不可用'
  if (key.endsWith('_fen')) return money(value)
  if ((key.endsWith('_at') || key === 'as_of') && typeof value === 'string') return localTime(value).replace('T',' ') + ' 北京时间'
  if (reasonListKeys.has(key)) {
    if (Array.isArray(value)) return value.map(reasonText).join('；') || '无异常原因'
    return reasonText(value)
  }
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'object') return JSON.stringify(value)
  return names[String(value)] ?? String(value)
}
export type Option = {value: string; label: string}
export type Field = {key: string; label: string; type?: 'text'|'date'|'datetime-local'|'select'; options?: Option[]; required?: boolean; initial?: string; hint?: string}
export type FormSpec = {title: string; path: string; method?: string; fields: Field[]; build: (values: Record<string,string>) => Fact; confirm?: string}
export const option = (value: string, label: string): Option => ({value,label})
