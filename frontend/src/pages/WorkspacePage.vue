<script setup lang="ts">
import {computed, onMounted, onBeforeUnmount, ref, watch} from 'vue'
import {api, ApiError, fact, facts, page, str, WriteAttempt, type Fact} from '../api/client'
import {getHealth, type HealthState} from '../api/health'
import * as session from '../api/session'
import {display, localTime, money, shanghaiTime, type FormSpec} from '../ui'
import * as forms from '../forms'
import OperationForm from '../components/OperationForm.vue'
import RecordTable from '../components/RecordTable.vue'
import PagedRecords from '../components/PagedRecords.vue'

const tabs=['总览','投资','交易','财务','系统'] as const
const tab=ref<typeof tabs[number]>('总览')
const health=ref<HealthState>({kind:'checking'}), system=ref<Fact|null>(null), overview=ref<Fact|null>(null), market=ref<Fact|null>(null)
const accounts=ref<Fact[]>([]), instruments=ref<Fact[]>([]), categories=ref<Fact[]>([]), lots=ref<Fact[]>([])
const valuation=ref<Fact|null>(null), strategy=ref<Fact|null>(null), month=ref<Fact|null>(null), historical=ref<Fact|null>(null)
const pool=ref<Fact|null>(null), positionsView=ref<Fact|null>(null), strategyError=ref(''), strategyLoading=ref(false)
const signalWriting=ref(false), signalFeedback=ref('')
const selectedAccount=ref(''), asOf=ref(''), period=ref(''), historyTime=ref('')
const openings=ref<Fact[]>([]), openingsError=ref('')
const backupState=ref<Fact|null>(null), migrationState=ref<Fact|null>(null), maintenanceError=ref(''), maintenanceLoading=ref(false)
const error=ref(''), readError=ref(''), feedback=ref(''), busy=ref(false), loading=ref(false)
const retryPending=ref(false), pendingCorrupt=ref(false), storageError=ref('')
const spec=ref<FormSpec|null>(null), formKey=ref(0), refresh=ref(0)
const signIn=ref(false), userName=ref(''), userPassword=ref(''), authState=ref(''), sessionName=ref('')
const quoteInstrument=ref(''), quoteEvidence=ref<Fact|null>(null), quoteError=ref('')
const writer=new WriteAttempt()
let active=true, sequence=0, valuationSequence=0, quoteSequence=0, poolSequence=0
let pendingIdentity: string|null=null, formIdentity: string|null=null
/** Any unconfirmed (or unreadable) write blocks every new business write, in every tab. */
const blocked=computed(()=>retryPending.value||pendingCorrupt.value)
function identityKey(status:Fact): string {
  if(!status.identity||!status.writable)throw new Error('当前没有已验证的可写数据库')
  const i=fact(status.identity)
  return JSON.stringify([status.mode,i.database_uuid,i.generation,i.schema_version])
}
/** Never throw while polling: an unverified status can simply be re-read later. */
function safeKey(status:Fact|null): string {try{return status?identityKey(status):''}catch{return ''}}
function syncPending() {
  retryPending.value=writer.pending!==null
  pendingCorrupt.value=writer.corrupt
  storageError.value=writer.storageError
  if(!writer.pending&&!writer.corrupt)pendingIdentity=null
}
const writable=computed(()=>health.value.kind==='connected'&&system.value?.writable===true&&overview.value!==null&&!busy.value&&!blocked.value&&!loading.value)
const securities=computed(()=>accounts.value.filter(a=>a.kind==='securities'))
const accountRows=computed(()=>overview.value?facts(overview.value.accounts):[])
const overviewMonth=computed(()=>overview.value?fact(overview.value.finance_month):null)
const positions=computed(()=>valuation.value?facts(valuation.value.positions):[])
const currentLots=computed(()=>positions.value.flatMap(p=>facts(p.lots).map(l=>({...l,code:p.code,market:p.market}))))
const currentRevision=computed(()=>Number(overview.value?.ledger_revision ?? 0))
const cols=(fields:Record<string,string>)=>Object.entries(fields).map(([key,label])=>({key,label}))
const accountColumns=cols({name:'账户',kind:'类型',asset_class:'资产/负债',balance_fen:'余额/权益（元）',balance_at:'余额时点',effective_at:'生效日期',effective_date_quality:'生效日期质量',included:'纳入统计',status:'当前状态',quality:'质量',reasons:'异常原因'})
const cashColumns=cols({id:'流水ID',account_id:'证券账户ID',kind:'类型',amount_fen:'金额',occurred_at:'时间',transfer_id:'内部转账ID',reversal_of:'冲正来源',note:'备注'})
const billColumns=cols({id:'账单ID',account_id:'账户ID',kind:'类型',category_id:'分类',amount_fen:'金额',occurred_at:'日期',status:'状态',note:'备注'})
const transferColumns=cols({id:'转账ID',from_account_id:'转出账户ID',to_account_id:'转入账户ID',amount_fen:'金额',occurred_at:'日期',status:'状态',from_included:'转出已包含',to_included:'转入已包含',note:'备注'})
// Use actual returned IDs and versions for every write; never estimate a balance in the browser.
function openForm(make:()=>FormSpec) {if(!writable.value)return;error.value='';feedback.value='';try{formIdentity=identityKey(system.value??{});spec.value=make();formKey.value++}catch(e){error.value=String(e)}}
function navigate(next:typeof tabs[number]) {tab.value=next;if(!retryPending.value){spec.value=null;error.value='';feedback.value=''}}
async function loadMaster(path:string):Promise<Fact[]> {
  const result=page(await api(`${path}?limit=500&offset=0`))
  const items=[...result.items]
  while(items.length<result.total){
    const next=page(await api(`${path}?limit=500&offset=${items.length}&ledger_revision=${result.ledger_revision}`))
    if(next.ledger_revision!==result.ledger_revision||next.total!==result.total||!next.items.length)throw new Error(`${path} 数据版本变化，请重新读取`)
    items.push(...next.items)
  }
  return items
}
async function reload() {
  const token=++sequence;loading.value=true;readError.value='';valuation.value=null;strategy.value=null;month.value=null;historical.value=null
  system.value=null;overview.value=null;market.value=null;accounts.value=[];instruments.value=[];categories.value=[];lots.value=[]
  session.forgetIdentity()
  try {
    const h=await getHealth();if(!active||token!==sequence)return;health.value=h
    if(h.kind!=='connected')throw new Error('后端连接失败；未切换数据库，旧数据不再显示为当前值。')
    const status=await api('/system/status');if(!active||token!==sequence)return;system.value=status
    session.declare(status)
    if(status.mode==='development'){readError.value='开发健康模式，无业务数据库。请通过正式或隔离沙盒启动。';return}
    const [o,m,a,i,c,l]=await Promise.all([api('/finance/overview'),api('/market/status'),loadMaster('/accounts'),loadMaster('/instruments'),loadMaster('/finance/categories'),loadMaster('/lots')])
    if(!active||token!==sequence)return
    overview.value=o;market.value=m;accounts.value=a;instruments.value=i;categories.value=c;lots.value=l
    if(!securities.value.some(a=>a.id===selectedAccount.value))selectedAccount.value=str(securities.value[0]?.id)
    if(!instruments.value.some(x=>x.id===quoteInstrument.value))quoteInstrument.value=str(instruments.value[0]?.id)
    refresh.value++
    if(selectedAccount.value)await loadInvestment()
    await loadMonth()
  } catch(e) {if(active&&token===sequence)handleRead(e)}
  finally {if(active&&token===sequence)loading.value=false}
}
/** A rejected session must ask for credentials again instead of showing stale data. */
function handleRead(e:unknown) {
  readError.value=String(e)
  if(e instanceof ApiError&&e.status===401){signIn.value=true;sessionName.value='';readError.value='会话已过期或被撤销，请重新登录。';forget()}
}
function forget() {system.value=null;overview.value=null;market.value=null;valuation.value=null;strategy.value=null;month.value=null;historical.value=null;accounts.value=[];instruments.value=[];categories.value=[];lots.value=[];spec.value=null;session.forgetIdentity()}
async function signInSubmit() {
  if(busy.value)return
  error.value='';authState.value='正在登录…';busy.value=true
  try {
    const value=await session.login(userName.value.trim(),userPassword.value)
    sessionName.value=value.username;signIn.value=false;userPassword.value='';authState.value=''
    await reload()
  } catch(e) {authState.value=String(e);error.value=String(e)}
  finally {busy.value=false}
}
async function signOut() {
  busy.value=true
  try {await session.logout()} catch{} 
  finally {sessionName.value='';forget();signIn.value=true;busy.value=false;readError.value='已退出登录。'}
}
async function loadInvestment(){await refreshInvestment(false)}
async function refreshInvestment(quiet:boolean){
  const token=++valuationSequence
  if(!quiet){valuation.value=null;strategy.value=null}
  if(!selectedAccount.value)return
  try {
    const time=asOf.value?shanghaiTime(asOf.value):new Date().toISOString()
    const id=encodeURIComponent(selectedAccount.value), query=`?as_of=${encodeURIComponent(time)}`
    const [v,s]=await Promise.all([api(`/portfolio/${id}${query}`),api(`/strategy/evidence/${id}${query}`)])
    if(active&&token===valuationSequence){valuation.value=v;strategy.value=s}
  }catch(e){if(active&&token===valuationSequence)readError.value=String(e)}
}
async function loadQuotes() {
  quoteError.value='';quoteEvidence.value=null
  if(!quoteInstrument.value){quoteError.value='请选择证券标的';return}
  const token=++quoteSequence
  try {
    const as_of=encodeURIComponent(new Date().toISOString())
    const r=await api(`/quotes/${encodeURIComponent(quoteInstrument.value)}?as_of=${as_of}`)
    if(active&&token===quoteSequence)quoteEvidence.value=r
  }catch(e){if(active&&token===quoteSequence)quoteError.value=String(e)}
}
async function loadMonth(){month.value=null;try{month.value=await api('/finance/month'+(period.value?`?period=${encodeURIComponent(period.value)}`:''))}catch(e){readError.value=String(e)}}
/** The read-only opening list is the source for correcting or voiding a batch. */
async function loadOpenings(){
  openingsError.value='';openings.value=[]
  try{openings.value=page(await api('/openings?limit=200&offset=0')).items}
  catch(e){openingsError.value=String(e)}
}
async function loadHistory(){historical.value=null;try{if(!historyTime.value)throw new Error('请选择历史估值时间');historical.value=await api(`/finance/overview?as_of=${encodeURIComponent(shanghaiTime(historyTime.value))}`)}catch(e){readError.value=String(e)}}
async function submit(values:Record<string,string>) {
  if(!spec.value||!writable.value)return
  error.value='';feedback.value=''
  let body:Fact
  try{body=spec.value.build(values)}catch(e){error.value=String(e);return}
  if(!window.confirm(`${spec.value.title}\n${spec.value.confirm??'请核对真实事实及金额。'}\n确定提交？`))return
  busy.value=true
  try{const expected=identityKey(system.value??{});const checked=await api('/system/status');if(expected!==formIdentity||identityKey(checked)!==expected)throw new Error('数据库身份已变化，拒绝提交；请重新读取并重新打开表单');session.declare(checked);pendingIdentity=expected;await writer.start(spec.value.path,body,spec.value.method);feedback.value='提交已确认。正在重新读取实际账本。';spec.value=null;await reload()}
  catch(e){error.value=String(e)}finally{syncPending();busy.value=false}
}
/**
 * Resolve an unconfirmed write: ask the server about the original request ID first,
 * then resend the identical payload. The record is only cleared once the ledger
 * definitively answered; a rejected retry never erases the evidence of a write that
 * may already have committed.
 */
async function retry(){
  if(busy.value||!writer.pending)return
  busy.value=true;error.value='';feedback.value=''
  const requestId=writer.requestId
  try{
    const checked=await api('/system/status')
    session.declare(checked)
    if(pendingIdentity!==null&&safeKey(checked)!==pendingIdentity)throw new Error('数据库身份与原请求不一致，禁止跨库重试')
    const state=await writer.check()
    if(state.committed===true){
      await writer.discard();spec.value=null
      feedback.value=`原请求 ${requestId} 已在账本中入账，未生成新请求。已按实际账本重新读取。`
      await reload();return
    }
    await writer.retry();feedback.value=`原请求 ${requestId} 尚未入账，已用同一请求 ID 与相同内容重新提交。`;spec.value=null;await reload()
  }catch(e){error.value=String(e)}
  finally{syncPending();busy.value=false}
}
/** Abandoning an unconfirmed write is an explicit operator decision, never automatic. */
async function discardPending(){
  if(busy.value||!writer.pending)return
  if(!window.confirm('放弃这条待确认写入？之后再提交将生成新的请求 ID，可能与已入账记录重复。请先核对服务器账本中是否存在该请求编号。'))return
  const requestId=writer.requestId
  let removed=true
  try{removed=await writer.discard()}catch(e){error.value=String(e)}
  syncPending();spec.value=null
  feedback.value=removed
    ?`已放弃待确认写入 ${requestId}。原请求仍可能已入账，请按该请求编号核对成交历史。`
    :`本机待确认记录已被另一个标签页替换（当前为 ${writer.requestId||'空'}），原来的 ${requestId} 未删除；请按该请求编号核对成交历史。`
}
/** Only after the operator has reconciled the ledger by hand may an unreadable record go away. */
async function clearCorruptPending(){
  if(busy.value||!writer.corrupt)return
  if(!window.confirm('确认已按成交历史人工核对过账本？清除后本机不再保留该待核实记录。'))return
  let removed=false
  try{removed=await writer.clearCorrupt()}catch(e){error.value=String(e)}
  syncPending()
  feedback.value=removed?'已清除无法解析的待核实记录。请按成交历史继续人工核对。':'待核实记录已不是无法解析的状态（可能已被另一个标签页处理），本机未删除任何记录。'
}
async function refreshStrategy(){
  if(strategyLoading.value)return
  strategyError.value='';strategyLoading.value=true
  const token=++poolSequence
  try{
    if(!selectedAccount.value)throw new Error('请先选择证券账户')
    const id=encodeURIComponent(selectedAccount.value)
    // The read-only path only displays evidence; only POST /strategy/refresh writes signals.
    const [p,pos]=await Promise.all([api(`/strategy/pool/${id}`),api(`/strategy/positions/${id}`)])
    if(active&&token===poolSequence){pool.value=p;positionsView.value=pos}
  }catch(e){if(active&&token===poolSequence){pool.value=null;positionsView.value=null;strategyError.value=String(e)}}
  finally{if(active&&token===poolSequence)strategyLoading.value=false}
}
/** An explicit operator action: this is the only path that records formal signals. */
async function writeStrategySignals(){
  if(signalWriting.value||!writable.value)return
  signalWriting.value=true;strategyError.value='';signalFeedback.value=''
  const token=++poolSequence
  try{
    if(!selectedAccount.value)throw new Error('请先选择证券账户')
    const id=encodeURIComponent(selectedAccount.value)
    const written=await api(`/strategy/refresh?account_id=${id}`,'POST')
    if(!active||token!==poolSequence)return
    pool.value=written
    signalFeedback.value=facts(written.items).some(row=>!!row.signal_id)?'已读取服务器保存的信号证据。':'服务端未返回正式信号。'
    const pos=await api(`/strategy/positions/${id}`)
    if(active&&token===poolSequence)positionsView.value=pos
  }catch(e){if(active)strategyError.value=String(e)}
  finally{if(active)signalWriting.value=false}
}
async function loadMaintenance(){
  maintenanceError.value='';maintenanceLoading.value=true;backupState.value=null;migrationState.value=null
  try{
    const [b,m]=await Promise.all([api('/maintenance/backups'),api('/maintenance/migration')])
    backupState.value=b;migrationState.value=m
  }catch(e){maintenanceError.value=String(e)}
  finally{maintenanceLoading.value=false}
}
async function refreshMarket(){if(!writable.value)return;busy.value=true;error.value='';feedback.value='';try{const r=await api('/market/refresh','POST');feedback.value=`实际行情刷新结果：${JSON.stringify(r)}`;await reload()}catch(e){error.value=String(e)}finally{busy.value=false}}
function beforeUnload(e:BeforeUnloadEvent){if(writer.pending||writer.corrupt||busy.value){e.preventDefault();e.returnValue=''}}
let timer:ReturnType<typeof setInterval>|undefined
/** Distinguish a live projection from a stale one; a failed poll never keeps old values marked current. */
type Freshness = 'live'|'stale'|'unavailable'
const freshness=ref<Freshness>('live')
const freshnessNote=ref('')
const lastPollAt=ref('')
function markStale(reason:string){freshness.value='stale';freshnessNote.value=reason}
/**
 * ISS-027: refresh the current projection on a plain HTTP poll. Historical queries and
 * a form still being filled are deliberately left untouched, and a network failure is
 * surfaced as a stale marker instead of silently keeping the old numbers current.
 */
async function poll(){
  if(!active||busy.value||loading.value||blocked.value||spec.value||signIn.value)return
  if(health.value.kind!=='connected')return
  if(tab.value!=='总览'&&tab.value!=='投资')return
  try{
    const status=await api('/system/status');if(!active)return
    const key=safeKey(status)
    if(key&&safeKey(system.value)&&key!==safeKey(system.value)){
      readError.value='数据库身份或可写状态已变化；已停止自动更新，请点击「重新读取」核对后再操作。';stopPolling();markStale('数据库身份已变化，显示的是上一次成功读取的值');return
    }
    system.value=status;session.declare(status)
    if(status.mode==='development'){freshness.value='unavailable';freshnessNote.value='开发健康模式，无业务数据库';return}
    const [o,m]=await Promise.all([api('/finance/overview'),api('/market/status')]);if(!active)return
    if(overview.value&&Number(o.ledger_revision)!==Number(overview.value.ledger_revision))refresh.value++
    const quoteMoved=!market.value||Number(m.quote_revision)!==Number(market.value.quote_revision)
    overview.value=o;market.value=m
    freshness.value='live';freshnessNote.value='';lastPollAt.value=new Date().toISOString()
    if(tab.value==='投资'&&selectedAccount.value&&!asOf.value&&(quoteMoved||refresh.value))await refreshInvestment(true)
  }catch(e){
    if(!active)return
    if(e instanceof ApiError&&e.status===401){handleRead(e);markStale('会话已过期，显示的是上次成功读取的值');return}
    // A transport failure proves nothing about the ledger: keep the numbers but label them stale.
    markStale(`自动刷新失败（${String(e)}），下列数字是 ${lastPollAt.value?localTime(lastPollAt.value)+' ':'上一次'}读取的旧值，不是当前值`)
  }
}
function stopPolling(){if(timer){clearInterval(timer);timer=undefined}}
async function boot(){
  syncPending()
  const existing=await session.stored()
  if(!active)return
  sessionName.value=existing?.username ?? ''
  // reload() turns the login panel on by itself when the server answers 401.
  await reload()
  if(existing!==null&&health.value.kind==='connected')signIn.value=false
}
/** Cross-tab: another tab may create, retry or abandon the pending write. */
function onStorage(){syncPending()}
/**
 * Switching account must never leave the previous account's pool, decision table or
 * error text on screen, so every strategy view is dropped before the next load.
 */
function clearStrategyView():void{
  poolSequence++
  pool.value=null;positionsView.value=null;strategyError.value='';signalFeedback.value='';signalWriting.value=false
}
watch(selectedAccount,()=>{clearStrategyView()})
onMounted(()=>{void boot();window.addEventListener('beforeunload',beforeUnload);window.addEventListener('storage',onStorage);timer=setInterval(()=>{void poll()},20000)})
onBeforeUnmount(()=>{active=false;sequence++;valuationSequence++;quoteSequence++;poolSequence++;stopPolling();window.removeEventListener('beforeunload',beforeUnload);window.removeEventListener('storage',onStorage)})
const validTransfer=(r:Fact)=>r.status==='effective'
const accountById=(id:unknown)=>accountRows.value.find(a=>a.id===id)

// -- V9/G2 read models -------------------------------------------------------------
const statusLabels:Record<string,string>={SIGNAL:'正式信号（突破成立且闸门通过）',NO_SIGNAL:'未突破（无信号）',DATA_UNAVAILABLE:'数据不可用',FACTOR_BLOCKED:'因子阻止',FACTOR_UNVERIFIED:'因子未核实（严格模式）',COOLDOWN:'冷却期内',POSITION_OPEN:'持仓中',PLAN_PENDING:'已有待执行计划',PORTFOLIO_FULL:'指数仓已达 8 个',CASH_BLOCKED:'现金或权益不足，未生成计划',AWAITING_CONFIRMATION:'次日开盘价已通过条件，待核对真实成交'}
const actionLabels:Record<string,string>={HOLD:'继续持有',STOP:'触发普通止损',GAP_STOP:'跳空止损',EXIT:'到期或规则退出',CLOSED:'已清仓',ADD_STOP:'子仓触发止损',ADD_GAP_STOP:'子仓跳空止损'}
const factorLabels:Record<string,string>={PASS:'通过',FAIL:'不通过',UNVERIFIED:'未核实',DATA_UNAVAILABLE:'数据不足',NOT_REQUIRED:'无额外因子',DISABLED:'未配置组合'}
const num=(v:unknown,d=4)=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(d):display(v)
const nested=(v:unknown)=>v&&typeof v==='object'&&!Array.isArray(v)?v as Fact:{}
const statusText=(v:unknown)=>statusLabels[str(v)]??display(v)
/** Which price series a row was computed on; the two are never mixed in one number. */
const basisLabels:Record<string,string>={index_level:'指数点位',etf_unadjusted:'ETF 未复权',etf_adjusted:'ETF 复权价'}
const basisText=(v:unknown)=>basisLabels[str(v)]??(str(v)||'未标注')
/** Flatten the pool item into display values; no numbers are recomputed in the browser. */
const strategyRows=computed(()=>pool.value?facts(pool.value.items).map(item=>{
  const gap=nested(item.gap), factor=nested(item.factor), combo=nested(item.combo), series=nested(item.series), base=nested(item.regime)
  return {...item,
    id:`${str(item.index_id)}`,
    label:`${str(item.index_id)} ${str(item.name)} → ${str(item.etf_code)||'无对应场内标的'}（${str(item.track)}）`,
    regime_text:`${str(base.label)||'未知'}`,
    er60_text:base.er60==null?'--':num(base.er60,4),
    price_basis_text:`${num(item.signal_close)} ${str(item.price_unit)}`,
    threshold_text:`${num(item.threshold_high)} ${str(item.price_unit)}`,
    buy_max_text:`${num(item.buy_max)} ${str(item.price_unit)}`,
    gap_text:gap.atr_multiple==null?'--':`${num(gap.abs,4)} / ${num(gap.atr_multiple,2)} ATR`,
    basis_text:basisText(item.price_basis),
    input_version_text:str(item.input_version)||'—',
    tradable_text:item.tradable===true?'可预填真实成交':`不可预填：${str(item.tradable_block_reason)||'标的价格口径不是未复权 ETF 价'}`,
    status_text:statusText(item.status),
    factor_text:`因子 ${factorLabels[str(factor.status)]??'—'}；组合 ${factorLabels[str(combo.status)]??'—'}`,
    cooldown_text:nested(item.cooldown).active?str(nested(item.cooldown).note)||'冷却中':'无冷却限制',
    data_text:`${str(series.source)||'无来源'}；${series.bars??0} 根；末日 ${str(series.last_date)||'—'}${str(series.warning)?`；${str(series.warning)}`:''}`,
  }}):[])
const strategyColumns=computed(()=>cols({label:'指数与交易标的',regime_text:'G2 市场状态',er60_text:'ER60',signal_date:'最近正式信号日',price_basis_text:'信号收盘',basis_text:'价格口径',threshold_text:'突破位',buy_max_text:'买入上限',atr14:'ATR14',gap_text:'距突破位 / ATR 倍数',status_text:'信号状态',factor_text:'因子与准入',cooldown_text:'冷却剩余',data_text:'数据来源与质量',input_version_text:'数据版本',tradable_text:'可否预填成交'}).concat([{key:'detail',label:'状态原因'}]))
const positionRows=computed(()=>positionsView.value?facts(positionsView.value.items).map(item=>{
  const p=nested(item.position), pyramid=nested(item.pyramid), aux=nested(item.ndx_aux), cooldown=nested(item.cooldown)
  return {...item,
    id:`pos-${str(item.index_id)}`,
    label:`${str(item.index_id)} ${str(item.name)} → ${str(item.etf_code)}`,
    entry_text:`${str(p.entry_date)||'—'} @ ${num(p.entry_price)} ${str(item.price_unit)}`,
    atr_text:`${num(p.signal_atr)}（冻结）`,
    stop_text:`${num(p.stop_price)} ${str(item.price_unit)}${p.pending_stop==null?'':`；下一交易日生效 ${num(p.pending_stop)}`}`,
    trail_text:`触发 ${num(p.trail_trigger)}；阶段 ${p.stage??'—'}；峰值 ${num(p.highest_price)}`,
    term_text:`第 ${p.holding_days??'—'}/${p.exit_after_bars??'—'} 日；续期 ${p.renewals??0} 次；E2a 延持 ${p.e2a_extended_days??0}/10${p.extended?'（延持中）':''}`,
    pyramid_text:pyramid.reason?`${str(pyramid.status)}：${str(pyramid.reason)}`:'—',
    ndx_text:aux.status?`${str(aux.status)}：${str(aux.detail)}`:'—',
    action_text:`【模型建议，非实际成交】${actionLabels[str(p.action)]??display(p.action)}${p.action_price==null?'':` @ ${num(p.action_price)}`}${str(p.action_reason)?`；${str(p.action_reason)}`:''}`,
    live_text:`策略记录 ${p.add_executed?'已加仓':'主仓'}；账本剩余 ${item.live_quantity??0} 股`,
    cooldown_text:cooldown.active?str(cooldown.note)||'冷却中':'无冷却限制',
  }}):[])
const positionColumns=computed(()=>cols({label:'指数与交易标的',entry_text:'建仓事实（策略证据）',atr_text:'信号日 ATR',stop_text:'当前保护价',trail_text:'跟踪止盈',term_text:'时间退出',pyramid_text:'盈利加仓条件',ndx_text:'NDX 辅助仓',action_text:'策略建议',live_text:'实际持仓对照',cooldown_text:'冷却'}).concat([{key:'warnings',label:'未核对与提示'}]))
/** Only a recorded formal signal on the unadjusted ETF series may prefill a trade form. */
function canFill(row:Fact):boolean{
  const status=str(row.status)
  if(status!=='SIGNAL'&&status!=='AWAITING_CONFIRMATION'&&status!=='PLAN_PENDING')return false
  // A number derived from an index level or an adjusted series is not a fillable ETF price.
  if(row.tradable!==true||str(row.price_basis)!=='etf_unadjusted'||!str(row.signal_id)||typeof row.atr14!=='number'||Number(num(row.atr14))<=0)return false
  const code=str(row.etf_code)
  return !!code&&instruments.value.some(i=>str(i.code)===code)
}
function fillBlockReason(row:Fact):string{
  if(row.tradable===true&&str(row.price_basis)==='etf_unadjusted')return ''
  return str(row.tradable_block_reason)||`价格口径为 ${basisText(row.price_basis)}，不能预填真实成交`
}
function openTradeFromSignal(row:Fact){
  if(!writable.value||!canFill(row))return
  const instrument=instruments.value.find(i=>str(i.code)===str(row.etf_code))
  const fill=nested(row.fill)
  // atr14 is already in yuan on the unadjusted ETF series, so it is the frozen ATR to keep.
  const note=`策略信号证据：${str(row.index_id)} 信号日 ${str(row.signal_date)}，冻结 ATR ${num(row.atr14)}（ETF 未复权口径，元），`
    +`突破位 ${num(row.threshold_high)} ${str(row.price_unit)}，买入上限 ${num(row.buy_max)} ${str(row.price_unit)}`
    +(fill.entry_price==null?'':`，次日开盘价 ${num(fill.entry_price)} 已通过价格条件`)
  openForm(()=>forms.tradeForm(accounts.value,instruments.value,lots.value,undefined,{
    account_id:selectedAccount.value,instrument_id:str(instrument?.id),side:'buy',lot_role:'main',
    strategy_id:'INDEX_STRATEGY_ATR',signal_id:str(row.signal_id),signal_date:str(row.signal_date),frozen_atr:num(row.atr14),note,
  }))
}
</script>

<template>
  <header><div><h1>Stock Manager 2.0</h1><p>V1.0 开发版 · 08-strategy</p></div><div role="status"><strong>后端：{{health.kind==='connected'?'已连接':health.kind==='checking'?'正在连接':'连接失败'}}</strong> · {{display(system?.mode)}} · {{system?.writable===true?'允许写入':'禁止写入'}}<template v-if="sessionName"> · 已登录：{{sessionName}} <button :disabled="busy" @click="signOut">退出登录</button></template> <button :disabled="busy||loading" @click="reload">重新读取</button></div></header>
  <nav aria-label="主导航"><button v-for="name in tabs" :key="name" :aria-current="tab===name?'page':undefined" :class="{selected:tab===name}" @click="navigate(name)">{{name}}</button></nav>
  <main>
    <section v-if="signIn" class="operation" aria-label="登录">
      <h2>登录</h2>
      <p>这是单用户私有服务。所有业务数据与写入都需要先通过应用认证；未认证请求一律被拒绝。</p>
      <form @submit.prevent="signInSubmit">
        <div class="fields">
          <label><span>用户名</span><input v-model="userName" autocomplete="username" :disabled="busy"/></label>
          <label><span>密码</span><input v-model="userPassword" type="password" autocomplete="current-password" :disabled="busy"/></label>
        </div>
        <button type="submit" :disabled="busy">登录</button>
      </form>
      <p v-if="authState" class="notice" role="status">{{authState}}</p>
    </section>
    <p v-if="freshness!=='live'" class="error" role="alert">{{freshnessNote||'当前数字可能不是最新值'}}</p>
    <p v-if="loading" role="status">正在读取服务器真实数据…</p>
    <p v-if="readError" class="error" role="alert">{{readError}}</p>
    <p v-if="error" class="error" role="alert">{{error}}</p>
    <p v-if="feedback" class="notice" role="status">{{feedback}}</p>
    <section v-if="retryPending" class="error" role="alert">
      <p>写入结果未知，原请求编号 <code>{{writer.requestId}}</code>。请勿重录或刷新页面：先向服务器核实该编号，再用同一内容重试。核实前所有新写入都被阻止。</p>
      <button :disabled="busy" @click="retry">核实并重试原请求</button><button :disabled="busy" @click="discardPending">放弃待确认写入</button>
    </section>
    <section v-if="pendingCorrupt" class="error" role="alert">
      <p>本机保留了无法解析的待核实写入记录，已阻止新的业务写入。请按成交历史核对账本后，再明确清除该记录。</p>
      <button :disabled="busy" @click="clearCorruptPending">已人工核对，清除该记录</button>
    </section>
    <p v-if="storageError" class="error" role="alert">{{storageError}}</p>
    <OperationForm v-if="spec" :key="formKey" :spec="spec" :busy="busy||blocked||!writable" @submit="submit" @cancel="spec=null" />
    <template v-if="health.kind==='connected'&&system&&system.mode!=='development'">
      <section v-if="tab==='总览'">
        <h2>个人资产总览</h2>
        <template v-if="overview">
          <dl class="metrics"><div v-for="item in cols({net_worth_fen:'净资产',total_assets_fen:'总资产',total_liabilities_fen:'总负债',securities_equity_fen:'证券权益',securities_cash_fen:'证券现金',securities_market_value_fen:'证券市值',investment_daily_pnl_fen:'证券当日收益'})" :key="item.key"><dt>{{item.label}}</dt><dd>{{money(overview[item.key])}}</dd></div></dl>
          <p>质量：{{display(overview.quality)}}；{{display(overview.as_of,'as_of')}}；账本版本 {{overview.ledger_revision}} · 行情版本 {{overview.quote_revision}}</p>
          <p v-if="Array.isArray(overview.reasons)&&overview.reasons.length" class="notice">{{display(overview.reasons)}}</p>
          <h3>本财务月（23日至次月22日）</h3><RecordTable v-if="overviewMonth" :rows="[overviewMonth]" :columns="cols({period:'周期开始',income_fen:'收入',expense_fen:'支出',fees_fen:'手续费',net_fen:'结余'})"/>
          <h3>资产负债明细（含排除统计账户）</h3><RecordTable :rows="accountRows" :columns="accountColumns"/>
        </template>
        <p v-else>总览尚未取得。</p>
      </section>
      <section v-if="tab==='投资'">
        <h2>持仓与行情</h2>
        <div class="toolbar"><label>证券账户 <select v-model="selectedAccount" @change="loadInvestment"><option value="">选择账户</option><option v-for="a in securities" :key="String(a.id)" :value="a.id">{{a.name}}</option></select></label><label>历史截止时间（北京时间，留空为当前） <input v-model="asOf" type="datetime-local" step="1"/></label><button @click="loadInvestment">查询持仓</button><button :disabled="!writable" @click="refreshMarket">刷新真实行情</button><button :disabled="!writable" @click="openForm(()=>forms.quoteForm(instruments))">录入核实价格</button></div>
        <template v-if="valuation"><p>数据时点 {{display(valuation.as_of,'as_of')}}；账本 {{valuation.ledger_revision}} / 行情 {{valuation.quote_revision}}；质量 {{display(valuation.quality)}}；{{display(valuation.reasons)}}</p>
          <dl class="metrics"><div v-for="item in cols({cash_fen:'证券现金',market_value_fen:'市值',equity_fen:'账户权益',daily_pnl_fen:'自然日收益',latest_trading_day_pnl_fen:'最近交易日收益',realized_fen:'已实现收益',unrealized_fen:'未实现收益',cumulative_pnl_fen:'累计收益'})" :key="item.key"><dt>{{item.label}}</dt><dd>{{money(valuation[item.key])}}</dd></div></dl>
          <p>累计起算：{{display(valuation.cumulative_from)}}；最近完成交易日：{{display(valuation.latest_completed_trading_day)}}</p>
          <RecordTable :rows="positions" :columns="cols({code:'代码',market:'市场',quantity:'持仓股数',remaining_cost_fen:'剩余本金',remaining_fees_fen:'剩余费用',holding_cost_fen:'持仓成本',price:'现价（元）',market_value_fen:'市值',daily_pnl_fen:'当日收益',realized_fen:'已实现',unrealized_fen:'未实现',quality:'质量',quote:'报价时点及来源'})"/>
          <h3>当前/历史有效批次</h3><RecordTable :rows="currentLots" :columns="cols({lot_id:'批次ID',code:'证券代码',lot_role:'角色',quantity:'数量',cost_fen:'剩余本金',fees_fen:'剩余费用',frozen_atr:'冻结ATR',signal_date:'信号日',opened_at:'开启时间'})"/>
        </template>
        <template v-else><p>请选择证券账户或重新读取。</p></template>
        <h3>V9/G2 策略观察池</h3>
        <p class="notice">默认 <b>ETF 模式</b>下，V9 突破位、ATR 与 BuyMax 全部用<b>ETF 未复权人民币日线</b>计算；<b>G2 基准</b>用 <b>14 只指数真实点位</b>判定。两套价格各自成体系、不混算，也不做点位折算；真实市值用未复权价。表内「信号收盘」列标明每一行用的是哪套价格。策略建议不等于真实成交：真实主仓/子仓/退出只能经「录入真实成交」确认。</p>
        <div class="toolbar"><button :disabled="strategyLoading||!selectedAccount" @click="refreshStrategy">{{strategyLoading?'正在刷新策略…':'刷新策略观察池（只读）'}}</button><button :disabled="signalWriting||!writable||!selectedAccount" @click="writeStrategySignals">{{signalWriting?'正在写入…':'记录正式信号（写入）'}}</button><span v-if="pool">数据日期 {{display(pool.as_of)}}；规则 {{display(pool.strategy_version)}}；基准 {{display(nested(pool.regime).label)}} / ER60 {{display(nested(pool.regime).er60)}}；权益 {{money(pool.equity_fen)}}（{{display(pool.equity_quality)}}）；基础仓 {{money(pool.base_amount_fen)}}；指数仓 {{pool.core_positions}}/{{pool.max_positions}}；可用现金 {{money(pool.cash_fen)}}</span></div>
        <p class="notice">刷新只读取证据、不写库；只有「记录正式信号（写入）」会把信号与数据版本落到账本。</p>
        <p v-if="signalFeedback" class="notice">{{signalFeedback}}</p>
        <p v-if="strategyError" class="error" role="alert">{{strategyError}}</p>
        <p v-if="pool&&Array.isArray(nested(pool.regime).warning)===false&&nested(pool.regime).warning" class="notice">G2 基准提示：{{display(nested(pool.regime).warning)}}</p>
        <p v-if="pool&&Array.isArray(pool.equity_reasons)&&pool.equity_reasons.length" class="notice">权益口径：{{display(pool.equity_reasons)}}</p>
        <RecordTable v-if="pool" :rows="strategyRows" :columns="strategyColumns">
          <template #actions="{row}">
            <button v-if="canFill(row)" :disabled="!writable" @click="openTradeFromSignal(row)">按此信号录入真实成交</button>
            <small v-else-if="fillBlockReason(row)">{{fillBlockReason(row)}}</small>
          </template>
        </RecordTable>
        <p v-else-if="!strategyLoading">尚未读取策略观察池。选择证券账户后点击「刷新策略观察池」。</p>
        <h3>持仓决策（策略建议 vs 实际持仓）</h3>
        <p class="notice">主仓与子仓各自使用其真实成交价与信号日冻结 ATR 计算止损；此处不会修改真实持仓。下表「决策」列是<b>模型建议</b>，不代表任何实际成交。</p>
        <p v-if="positionsView&&positionsView.model_only===true" class="notice">此处不释放仓位、不启动冷却；只有真实卖出成交才会。</p>
        <template v-if="positionsView">
          <RecordTable :rows="positionRows" :columns="positionColumns"/>
          <template v-if="facts(positionsView.unrealized_model_actions).length">
            <h4>未实现的模型动作（仅建议，不影响真实持仓）</h4>
            <RecordTable :rows="facts(positionsView.unrealized_model_actions)" :columns="cols({index_id:'指数',etf_code:'ETF 代码',action:'建议动作',action_price:'参考价（元）',action_reason:'原因',price_basis:'价格口径'})"/>
          </template>
          <h4>真实账本中的策略批次（核对依据）</h4>
          <RecordTable v-if="facts(positionsView.custody).length" :rows="facts(positionsView.custody)" :columns="cols({lot_id:'批次ID',lot_role:'角色',strategy_id:'策略标识',signal_date:'信号日',frozen_atr:'冻结ATR',code:'证券代码',market:'市场',kind:'类型',remaining:'剩余数量'})"/>
          <p v-else class="notice">账本中没有带策略标记的主仓/子仓批次；策略持仓状态若存在则属于未核对证据。</p>
        </template>
        <p v-else class="notice">尚未读取持仓决策。</p>
        <details v-if="strategy"><summary>原批次证据（历史接口）</summary><pre>{{JSON.stringify(strategy,null,2)}}</pre></details>
        <h3>实际行情状态</h3><RecordTable v-if="market" :rows="facts(market.items)" :columns="cols({code:'证券代码',market:'市场',quality:'质量',quote:'报价证据'})"/>
        <h3>冲突收盘价核实</h3>
        <p class="notice">同一交易日存在互相矛盾的已核实收盘价时，必须由你明确指定采用哪一条。核实会绑定当前行情版本，版本变化后需要重新核实。</p>
        <div class="toolbar"><label>证券标的 <select v-model="quoteInstrument"><option value="">选择标的</option><option v-for="i in instruments" :key="String(i.id)" :value="i.id">{{i.market}} {{i.code}}</option></select></label><button @click="loadQuotes">查询报价证据及冲突</button></div>
        <p v-if="quoteError" class="error" role="alert">{{quoteError}}</p>
        <template v-if="quoteEvidence">
          <p>行情版本 {{quoteEvidence.quote_revision}}；证据 {{facts(quoteEvidence.items).length}} 条；已核实冲突 {{facts(quoteEvidence.resolutions).length}} 条。</p>
          <RecordTable :rows="facts(quoteEvidence.items)" :columns="cols({id:'报价ID',price:'价格（元）',kind:'类型',quality:'质量',source:'来源',observed_at:'观测时间',trading_day:'交易日',quote_revision:'行情版本'})"><template #actions="{row}"><button v-if="row.quality==='conflict'" :disabled="!writable" @click="openForm(()=>forms.quoteResolutionForm(row,Number(quoteEvidence?.quote_revision??0)))">核实采用此价格</button></template></RecordTable>
          <h4>已核实记录</h4><RecordTable :rows="facts(quoteEvidence.resolutions)" :columns="cols({id:'核实ID',instrument_id:'标的',selected_quote_id:'采用报价',quote_revision:'核实后版本',reviewed_quote_ids:'覆盖的冲突报价',reason:'核实依据',resolved_at:'核实时间'})"/>
        </template>
      </section>
      <section v-if="tab==='交易'"><h2>真实成交</h2>
        <div class="toolbar"><button :disabled="!writable" @click="openForm(forms.accountForm)">创建账户</button><button :disabled="!writable" @click="openForm(forms.instrumentForm)">创建证券标的</button><button :disabled="!writable" @click="openForm(()=>forms.cashForm(accounts))">期初及证券现金</button><button :disabled="!writable||!securities.length||!instruments.length" @click="openForm(()=>forms.openingForm(accounts,instruments))">期初持仓录入</button><button :disabled="!writable" @click="openForm(()=>forms.tradeForm(accounts,instruments,lots))">录入买入 / 卖出</button></div>
        <h3>成交历史（包括待核对和冲正）</h3>
        <PagedRecords path="/trades" :refresh="refresh" :columns="cols({id:'成交ID',account_id:'账户ID',instrument_id:'标的ID',side:'方向',quantity:'数量',price:'价格（元）',fees_fen:'费用',amount_fen:'金额',executed_at:'成交时间',status:'状态',pending_reason:'待核对原因',note:'备注'})"><template #actions="{row}"><button v-if="row.status==='pending'" :disabled="!writable" @click="openForm(()=>forms.confirmation(`/trades/${row.id}/confirm`))">确认</button><button v-if="row.status!=='reversed'" :disabled="!writable" @click="openForm(()=>forms.tradeForm(accounts,instruments,lots,row))">更正</button><button v-if="row.status!=='reversed'" :disabled="!writable" @click="openForm(()=>forms.voidForm(`/trades/${row.id}/void`,'作废成交'))">作废</button></template></PagedRecords>
        <h3>证券资金记录</h3><PagedRecords path="/cash" :refresh="refresh" :columns="cashColumns"><template #actions="{row}"><template v-if="!row.transfer_id&&!row.execution_id&&['opening','deposit','withdrawal','adjustment'].includes(String(row.kind))"><button :disabled="!writable" @click="openForm(()=>forms.cashForm(accounts,row))">更正</button><button :disabled="!writable" @click="openForm(()=>forms.voidForm(`/cash/${row.id}/void`,'作废资金记录'))">作废</button></template></template></PagedRecords>
        <details><summary>账户及证券标的 ID 对照</summary><RecordTable :rows="accounts" :columns="cols({id:'账户ID',name:'名称',kind:'类型'})"/><RecordTable :rows="instruments" :columns="cols({id:'标的ID',market:'市场',code:'代码',kind:'类型'})"/></details>
        <h3>期初持仓批次（可更正 / 作废）</h3>
        <p class="notice">更正与作废都保留审计痕迹：作废不会删除原批次，只会写入一条冲正记录。这里只列出期初批次，不改动真实成交历史。</p>
        <div class="toolbar"><button @click="loadOpenings">{{openings.length?'重新读取期初批次':'读取期初批次'}}</button></div>
        <p v-if="openingsError" class="error" role="alert">{{openingsError}}</p>
        <RecordTable v-if="openings.length" :rows="openings" :columns="cols({id:'期初批次ID',account_id:'账户ID',instrument_id:'标的ID',quantity:'数量',cost_fen:'成本',fees_fen:'费用',occurred_at:'发生时间',status:'状态',note:'备注'})"><template #actions="{row}"><button :disabled="!writable" @click="openForm(()=>forms.openingCorrectionForm(row))">更正</button><button :disabled="!writable" @click="openForm(()=>forms.openingVoidForm(row))">作废</button></template></RecordTable>
        <h3>现金分红</h3>
        <p class="notice">现金分红是外部收益，进入证券账户现金，不改变持仓股数。拆分与合并仅录入已核实的实际比例及真实尾差；复杂配股尚不支持数量调整。</p>
        <div class="toolbar"><button :disabled="!writable||!securities.length||!instruments.length" @click="openForm(()=>forms.dividendForm(accounts,instruments))">录入现金分红</button><button :disabled="!writable||!securities.length||!instruments.length" @click="openForm(()=>forms.companyForm(accounts,instruments))">核实拆分 / 合并</button></div>
        <PagedRecords path="/company-events" :refresh="refresh" :columns="cols({id:'公司行为ID',account_id:'账户ID',instrument_id:'标的ID',kind:'类型',occurred_at:'发生时间',cash_movement_id:'对应现金流水',note:'备注'})"><template #actions="{row}"><button v-if="row.kind==='dividend'" :disabled="!writable" @click="openForm(()=>forms.dividendCorrectionForm(row))">更正金额</button><button v-if="row.status!=='reversed'" :disabled="!writable" @click="openForm(()=>forms.voidForm(row.kind==='dividend'?`/company-events/${row.id}/void`:`/portfolio/company-events/${row.id}/void`,row.kind==='dividend'?'作废现金分红':'作废公司行为'))">作废</button></template></PagedRecords>
      </section>
      <section v-if="tab==='财务'"><h2>个人财务</h2><div class="toolbar"><button :disabled="!writable" @click="openForm(forms.accountForm)">创建账户</button><button :disabled="!writable" @click="openForm(forms.categoryForm)">新增分类</button><button :disabled="!writable" @click="openForm(()=>forms.billForm(accounts,categories))">新增收入 / 支出 / 手续费</button><button :disabled="!writable||!overview" @click="openForm(()=>forms.transferForm(accountRows))">内部转账</button></div>
        <h3>账户及余额</h3><RecordTable :rows="accountRows" :columns="accountColumns"><template #actions="{row}"><button v-if="row.kind!=='securities'&&row.status==='active'" :disabled="!writable" @click="openForm(()=>forms.balanceForm(row))">余额核实</button><button :disabled="!writable" @click="openForm(()=>forms.accountUpdate(row,currentRevision))">账户状态</button><button :disabled="!writable" @click="openForm(()=>forms.scopeForm(row,currentRevision))">统计范围</button><button v-if="!row.effective_at" :disabled="!writable" @click="openForm(()=>forms.effectiveForm(row,currentRevision))">核实生效时间</button></template></RecordTable>
        <h3>普通收支账单</h3><PagedRecords path="/finance/bills" :refresh="refresh" :columns="billColumns"><template #actions="{row}"><template v-if="row.status==='effective'"><button :disabled="!writable" @click="openForm(()=>forms.billForm(accounts,categories,row))">更正</button><button :disabled="!writable" @click="openForm(()=>forms.voidForm(`/finance/bills/${row.id}/void`,'作废账单'))">作废</button></template></template></PagedRecords>
        <h3>内部转账</h3><PagedRecords path="/finance/transfers" :refresh="refresh" :columns="transferColumns"><template #actions="{row}"><template v-if="validTransfer(row)"><button v-if="!row.from_included&&!row.to_included" :disabled="!writable" @click="openForm(()=>forms.transferForm(accountRows,row))">更正</button><button v-if="!row.from_included&&!row.to_included" :disabled="!writable" @click="openForm(()=>forms.voidForm(`/finance/transfers/${row.id}/void`,'作废转账'))">作废</button><button v-if="accountById(row.from_account_id)?.kind!=='securities'||accountById(row.to_account_id)?.kind!=='securities'" :disabled="!writable" @click="openForm(()=>forms.reconciliationForm(accountRows,row))">人工核对更正 / 作废</button></template></template></PagedRecords>
        <h3>财务月</h3><label>周期开始日期（23日，留空为当前） <input type="date" v-model="period"/></label><button @click="loadMonth">查询</button><template v-if="month"><RecordTable :rows="[month]" :columns="cols({period:'周期开始',start_at:'开始时点',end_exclusive:'结束（不含）',income_fen:'收入',expense_fen:'支出',fees_fen:'手续费',net_fen:'结余',ledger_revision:'账本版本'})"/><RecordTable :rows="facts(month.items)" :columns="billColumns"/></template>
        <h3>历史余额与净资产</h3><label>截止时间（北京时间） <input type="datetime-local" step="1" v-model="historyTime"/></label><button @click="loadHistory">查询历史</button><template v-if="historical"><RecordTable :rows="[historical]" :columns="cols({as_of:'估值时点',net_worth_fen:'净资产',total_assets_fen:'总资产',total_liabilities_fen:'总负债',quality:'质量',ledger_revision:'账本版本',quote_revision:'报价版本',reasons:'原因'})"/><RecordTable :rows="facts(historical.accounts)" :columns="accountColumns"/></template>
        <details><summary>手工余额历史</summary><PagedRecords path="/finance/balances" :refresh="refresh" :columns="cols({account_id:'账户ID',before_fen:'原余额',after_fen:'新余额',occurred_at:'生效时间',reason:'原因',status:'状态'})"/></details>
      </section>
    </template>
    <section v-if="tab==='系统'"><h2>系统与维护</h2><p>运行模式：{{display(system?.mode)}}；{{system?.writable===true?'允许写入':'禁止写入'}}。断线不会切换数据库。</p><details v-if="system?.identity" open><summary>已验证数据库身份</summary><pre>{{JSON.stringify(system.identity,null,2)}}</pre></details><h3>行情及日历</h3><pre v-if="market">{{JSON.stringify({calendar_coverage:market.calendar_coverage,market_phase:market.market_phase,last_refresh:market.last_refresh},null,2)}}</pre><p v-else>行情状态不可用。</p><button :disabled="!writable" @click="refreshMarket">手动刷新行情</button><h3>维护命令（在服务端终端显式执行）</h3><p>正式库身份检查：<code>python start.py check-database</code></p><p>一致性备份：<code>python start.py backup --destination &lt;新备份目录绝对路径&gt;</code></p><p>数据库升级：停止写服务，执行 <code>python start.py upgrade-database --destination &lt;新备份目录绝对路径&gt; --confirm</code>。不从网页自动接管或恢复正式库。</p><h3>备份与迁移对账</h3><p class="notice">查询只读取服务端现状；请通过上述服务端维护命令创建备份，网页端不会自动接管、还原或删除任何备份。</p><div class="toolbar"><button :disabled="maintenanceLoading" @click="loadMaintenance">{{maintenanceLoading?'正在读取…':'查询备份状态与迁移对账'}}</button></div><p v-if="maintenanceError" class="error" role="alert">{{maintenanceError}}</p><template v-if="backupState"><h4>备份状态</h4><RecordTable :rows="facts(backupState.items).length?facts(backupState.items):[backupState]" :columns="cols({name:'备份名称',path:'位置',backup_at:'创建时间',file_size:'体积（字节）',verified:'已校验',status:'状态'})"/><p v-if="facts(backupState.items).length===0" class="notice">服务端尚未返回任何备份条目。</p></template><template v-if="migrationState"><h4>迁移对账报告</h4><pre>{{JSON.stringify(migrationState,null,2)}}</pre></template><p class="notice">V9/G2 集成待审查，证券实网验证尚未完成。当前只用于本机开发/隔离测试，不开放公网。</p></section>
  </main>
</template>

<style>
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#182536;background:#f7f8fa;line-height:1.5;font-size:15px}*{box-sizing:border-box}body{margin:0}header{padding:18px 24px;background:#fff;border-bottom:1px solid #cbd5e1;display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap}h1{font-size:1.5rem;margin:0}header p{margin:3px 0;color:#526477}nav{display:flex;gap:8px;padding:12px 24px;background:#edf1f5;flex-wrap:wrap}button{font:inherit;cursor:pointer;border:1px solid #a6b5c5;background:#fff;color:#20344c;border-radius:5px;padding:7px 12px;margin:3px}button.selected{background:#274d75;color:white}button:disabled{cursor:not-allowed;opacity:.5}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #276da2;outline-offset:2px}main{max-width:1500px;margin:auto;padding:20px 24px}section{background:white;border:1px solid #d9e0e7;border-radius:7px;padding:16px;margin-bottom:16px}h2{font-size:1.25rem;margin:0 0 14px}h3{font-size:1.05rem;margin:20px 0 8px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}.metrics div{background:#eef3f7;border-radius:5px;padding:12px}.metrics dt{color:#4c5b70}.metrics dd{font-size:1.4rem;font-weight:600;margin:3px 0;font-variant-numeric:tabular-nums}.table-scroll{overflow:auto;max-width:100%}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{padding:8px;text-align:left;border-bottom:1px solid #dde4eb;vertical-align:top;max-width:400px;overflow-wrap:anywhere}th{background:#edf1f5;white-space:nowrap}.toolbar{display:flex;flex-wrap:wrap;align-items:end;gap:8px}.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin-bottom:12px}label{display:flex;flex-direction:column;gap:3px}input,select{padding:8px;font:inherit;min-width:0;border:1px solid #9daebd;border-radius:4px;background:white}small{color:#526477}.operation{border:2px solid #809cba}.error{background:#fff1f0;color:#8b2621;padding:10px;border-left:4px solid #a5322c;white-space:pre-wrap;overflow-wrap:anywhere}.notice{background:#f0f4f9;padding:10px;border-left:4px solid #4779a4;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto}code{overflow-wrap:anywhere}details{margin-top:16px}
</style>
