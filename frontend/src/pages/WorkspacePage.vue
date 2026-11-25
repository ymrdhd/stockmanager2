<script setup lang="ts">
import {computed, onMounted, onBeforeUnmount, ref, watch} from 'vue'
import {api, ApiError, fact, facts, page, str, WriteAttempt, type Fact, type WriteIntent} from '../api/client'
import {AccountScope} from '../api/accountScope'
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
const reconciliationReport=ref<Fact|null>(null), reconciliationError=ref(''), reconciliationLoading=ref(false)
const pool=ref<Fact|null>(null), positionsView=ref<Fact|null>(null), strategyError=ref(''), strategyLoading=ref(false)
const signalWriting=ref(false), signalFeedback=ref('')
const accountScope=new AccountScope(), tradeScope=new AccountScope()
const investmentLoading=ref(false), investmentError=ref(''), tradeEpoch=ref(0)
let pnlSequence=0, tradeSequence=0, reconciliationSequence=0
const selectedAccount=ref(''), asOf=ref(''), period=ref(''), historyTime=ref(''),holdingSearch=ref('')
const netMonths=ref(12),netTrend=ref<Fact|null>(null),netTrendError=ref('')
async function loadNetTrend(){
  netTrend.value=null;netTrendError.value='';const token=sequence
  try{const result=await api('/finance/net-worth-trend?months='+netMonths.value);if(active&&token===sequence)netTrend.value=result}catch(e){if(active&&token===sequence)netTrendError.value=String(e)}
}
function exportNetTrend(){
  if(!netTrend.value)return
  // Export only server timestamps, fixed-point amounts and quality tokens; unknown amounts stay blank.
  const keys=['as_of','total_assets_fen','total_liabilities_fen','net_worth_fen','liquid_net_worth_fen','liquid_assets_fen','securities_equity_fen','quality']
  const scalar=(x:unknown)=>x==null?'':String(x).replace(/[\r\n,"]/g,'')
  const snapshot=netTrend.value
  const text=['ledger_revision,quote_revision,'+keys.join(','),...facts(snapshot.items).map(r=>[snapshot.ledger_revision,snapshot.quote_revision,...keys.map(k=>r[k])].map(scalar).join(','))].join('\r\n')
  const url=URL.createObjectURL(new Blob(['\uFEFF',text],{type:'text/csv;charset=utf-8'}))
  const link=document.createElement('a');link.href=url;link.download='net-worth-summary.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
}
const billAccount=ref(''),billFrom=ref(''),billTo=ref(''),billPath=ref('/finance/bills'),billReadError=ref('')
function filterBills(){
  billReadError.value=''
  try{const p=new URLSearchParams();if(billAccount.value)p.set('account_id',billAccount.value);if(billFrom.value)p.set('from_at',shanghaiTime(billFrom.value));if(billTo.value)p.set('to_at',shanghaiTime(billTo.value));const query=p.toString();billPath.value='/finance/bills'+(query?'?'+query:'')}
  catch(e){billReadError.value=String(e)}
}
const todo=ref<Fact|null>(null),todoError=ref('')
async function refreshWorkstation(){
  todo.value=null;todoError.value='';const token=sequence,ticket=accountScope.capture()
  try{const result=await api('/pending?limit=1');if(active&&token===sequence)todo.value=result}catch(e){if(active&&token===sequence)todoError.value=String(e)}
  if(active&&token===sequence&&accountScope.current(ticket)&&selectedAccount.value){strategyMode.value='current';resetStrategyMode();await refreshStrategy()}
}
const pnlMonth=ref(''),pnlHistory=ref<Fact|null>(null),pnlError=ref('')
async function loadPnlMonth(){
  pnlHistory.value=null;pnlError.value='';const token=++pnlSequence,ticket=accountScope.capture(),account=ticket.account
  try{
    if(!account||!pnlMonth.value)throw new Error('请选择证券账户与月份')
    const result=await api('/portfolio/'+encodeURIComponent(account)+'/month?month='+pnlMonth.value+'-01')
    if(active&&token===pnlSequence&&accountScope.accepts(ticket,result))pnlHistory.value=result
  }catch(e){if(active&&token===pnlSequence&&accountScope.current(ticket))pnlError.value=String(e)}
}
async function pnlDay(row:Fact){
  if(!pnlHistory.value||str(pnlHistory.value.account_id)!==selectedAccount.value)return
  const snapshot=pnlHistory.value, ticket=accountScope.capture(), token=++valuationSequence
  valuation.value=null;strategy.value=null;investmentLoading.value=true;investmentError.value=''
  try{
    const params=new URLSearchParams({as_of:str(row.as_of),ledger_revision:String(snapshot.ledger_revision),quote_revision:String(snapshot.quote_revision),database_uuid:str(snapshot.database_uuid),generation:String(snapshot.generation)})
    const result=await api('/portfolio/'+encodeURIComponent(ticket.account)+'?'+params.toString())
    if(active&&token===valuationSequence&&accountScope.accepts(ticket,result)&&pnlHistory.value===snapshot){
      if(result.ledger_revision!==snapshot.ledger_revision||result.quote_revision!==snapshot.quote_revision||result.database_uuid!==snapshot.database_uuid||result.generation!==snapshot.generation||result.as_of!==row.as_of)throw new Error('收益版本已变化，请重新查询摘要')
      asOf.value=localTime(row.as_of);valuation.value=result
    }
  }catch(e){if(active&&token===valuationSequence&&accountScope.current(ticket)){investmentError.value=String(e);pnlError.value='无法读取同版本详情，请重新查询月度摘要。';pnlHistory.value=null}}
  finally{if(active&&token===valuationSequence&&accountScope.current(ticket))investmentLoading.value=false}
}
const strategyMode=ref<'current'|'historical'>('current'), strategyDay=ref('')
function strategyQuery(){
  if(strategyMode.value==='current')return '?mode=current'
  if(!strategyDay.value)throw new Error('请选择历史策略截止日期')
  return '?mode=historical&as_of='+encodeURIComponent(strategyDay.value)
}
function resetStrategyMode(){poolSequence++;pool.value=null;positionsView.value=null;strategyLoading.value=false;signalFeedback.value='';strategyError.value=''}

const tradeAccount=ref(''),tradeInstrument=ref(''),tradeFrom=ref(''),tradeTo=ref(''),tradePath=ref('/trades'),tradeDetail=ref<Fact|null>(null),tradeReadError=ref('')
function filterTrades(){
  tradeScope.reset(tradeAccount.value,()=>{tradeSequence++;tradeEpoch.value++;tradeReadError.value='';tradeDetail.value=null;tradePath.value='/trades'+(tradeAccount.value?'?account_id='+encodeURIComponent(tradeAccount.value):'')})
  try{
    const params=new URLSearchParams()
    if(tradeAccount.value)params.set('account_id',tradeAccount.value)
    if(tradeInstrument.value)params.set('instrument_id',tradeInstrument.value)
    if(tradeFrom.value)params.set('from_at',shanghaiTime(tradeFrom.value))
    if(tradeTo.value)params.set('to_at',shanghaiTime(tradeTo.value))
    const query=params.toString();tradePath.value='/trades'+(query?'?'+query:'')
  }catch(e){tradeReadError.value=String(e)}
}
async function readTrade(row:Fact){
  tradeReadError.value='';tradeDetail.value=null
  const token=++tradeSequence,ticket=tradeScope.capture(),expected=str(row.account_id),id=str(row.id)
  try{
    if(!expected||!id||(ticket.account&&ticket.account!==expected))throw new Error('成交不属于当前筛选账户')
    const result=await api('/trades/'+encodeURIComponent(id)+'?include_audit=true')
    if(active&&token===tradeSequence&&tradeScope.current(ticket)){
      if(result.account_id!==expected||result.id!==id)throw new Error('成交身份与查询不一致，拒绝展示')
      tradeDetail.value=result
    }
  }catch(e){if(active&&token===tradeSequence&&tradeScope.current(ticket))tradeReadError.value=String(e)}
}
const financeYear=ref(new Date().getFullYear()),yearResult=ref<Fact|null>(null),yearError=ref('')
async function loadYear(){
  yearError.value='';yearResult.value=null
  const token=sequence
  try{const result=await api('/finance/year/'+encodeURIComponent(String(financeYear.value)));if(active&&token===sequence)yearResult.value=result}catch(e){if(active&&token===sequence)yearError.value=String(e)}
}
const backupState=ref<Fact|null>(null), migrationState=ref<Fact|null>(null), maintenanceError=ref(''), maintenanceLoading=ref(false)
const error=ref(''), readError=ref(''), feedback=ref(''), busy=ref(false), loading=ref(false)
const retryPending=ref(false), pendingCorrupt=ref(false), storageError=ref('')
const spec=ref<FormSpec|null>(null), formKey=ref(0), refresh=ref(0)
const signIn=ref(false), userName=ref(''), userPassword=ref(''), authState=ref(''), sessionName=ref('')
const quoteInstrument=ref(''), quoteEvidence=ref<Fact|null>(null), quoteError=ref('')
const writer=new WriteAttempt()
let active=true, sequence=0, valuationSequence=0, quoteSequence=0, poolSequence=0
let pendingIdentity: string|null=null, formIdentity: string|null=null
let formIntent:WriteIntent=null
/** Any unconfirmed (or unreadable) write blocks every new business write, in every tab. */
const blocked=computed(()=>retryPending.value||pendingCorrupt.value)
function identityKey(status:Fact): string {
  if(!status.identity||!status.writable)throw new Error('当前没有已验证的可写数据库')
  const i=fact(status.identity)
  return JSON.stringify([status.mode,i.database_uuid,i.generation,i.schema_version])
}
/** Never throw while polling: an unverified status can simply be re-read later. */
function safeKey(status:Fact|null): string {try{if(!status?.identity)return '';const i=fact(status.identity);return JSON.stringify([status.mode,i.database_uuid,i.generation,i.schema_version])}catch{return ''}}
function syncPending() {
  retryPending.value=writer.pending!==null
  pendingCorrupt.value=writer.corrupt
  storageError.value=writer.storageError
  if(!writer.pending&&!writer.corrupt)pendingIdentity=null
}
const writable=computed(()=>health.value.kind==='connected'&&system.value?.writable===true&&overview.value!==null&&freshness.value==='live'&&!busy.value&&!blocked.value&&!loading.value)
const securities=computed(()=>accounts.value.filter(a=>a.kind==='securities'))
const accountRows=computed(()=>overview.value?facts(overview.value.accounts):[])
const overviewMonth=computed(()=>overview.value?fact(overview.value.finance_month):null)
const positions=computed(()=>valuation.value?facts(valuation.value.positions):[])
const filteredPositions=computed(()=>{const q=holdingSearch.value.trim().toLowerCase();return positions.value.filter(p=>{const i=instruments.value.find(i=>i.id===p.instrument_id);return !q||[p.code,p.name,i?.name,i?.code].some(v=>str(v).toLowerCase().includes(q))})})
const currentLots=computed(()=>filteredPositions.value.flatMap(p=>facts(p.lots).map(l=>({...l,code:p.code,market:p.market}))))
const currentRevision=computed(()=>Number(overview.value?.ledger_revision ?? 0))
const reconciliationCheckRows=computed(()=>facts(reconciliationReport.value?.checks).map(r=>({...r,status_text:reconciliationStatus(r.status)})))

const cols=(fields:Record<string,string>)=>Object.entries(fields).map(([key,label])=>({key,label}))
const accountColumns=cols({name:'账户',kind:'类型',asset_class:'资产/负债',balance_fen:'余额/权益（元）',balance_at:'余额时点',effective_at:'生效日期',effective_date_quality:'生效日期质量',included:'纳入统计',status:'当前状态',quality:'质量',reasons:'异常原因'})
const cashColumns=cols({id:'流水ID',account_id:'证券账户ID',kind:'类型',amount_fen:'金额',occurred_at:'时间',transfer_id:'内部转账ID',reversal_of:'冲正来源',note:'备注'})
const billColumns=cols({id:'账单ID',account_id:'账户ID',kind:'类型',category_id:'分类',amount_fen:'金额',net_amount_fen:'冲减后金额',related_transaction_id:'原支出',occurred_at:'日期',status:'状态',note:'备注'})
const transferColumns=cols({id:'转账ID',from_account_id:'转出账户ID',to_account_id:'转入账户ID',amount_fen:'金额',occurred_at:'日期',status:'状态',from_included:'转出已包含',to_included:'转入已包含',note:'备注'})
// Use actual returned IDs and versions for every write; never estimate a balance in the browser.
function openForm(make:()=>FormSpec) {if(!writable.value)return;error.value='';feedback.value='';try{formIdentity=identityKey(system.value??{});formIntent=writer.beginIntent();spec.value=make();formKey.value++}catch(e){error.value=String(e)}}
function navigate(next:typeof tabs[number]) {tab.value=next;if(!retryPending.value){spec.value=null;error.value='';feedback.value=''}
  if(next==='投资'&&selectedAccount.value)void loadInvestment()
  if(next==='交易'&&selectedAccount.value)tradeAccount.value=selectedAccount.value
}
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
  const oldIdentity=safeKey(system.value);spec.value=null;backupState.value=null;migrationState.value=null;quoteEvidence.value=null;quoteSequence++
  const token=++sequence;loading.value=true;readError.value='';freshness.value='stale';clearAccountViews();month.value=null;historical.value=null
  clearStrategyView();todo.value=null;netTrend.value=null;tradeDetail.value=null;yearResult.value=null;pnlHistory.value=null;reconciliationReport.value=null;reconciliationError.value='';reconciliationSequence++;system.value=null;overview.value=null;market.value=null;accounts.value=[];instruments.value=[];categories.value=[];lots.value=[]
  session.forgetIdentity()
  try {
    const h=await getHealth();if(!active||token!==sequence)return;health.value=h
    if(h.kind!=='connected')throw new Error('后端连接失败；未切换数据库，旧数据不再显示为当前值。')
    const status=await api('/system/status');if(!active||token!==sequence)return;system.value=status
    if(oldIdentity&&oldIdentity!==safeKey(status)){selectedAccount.value='';tradeAccount.value='';quoteInstrument.value='';feedback.value='数据库身份已变化，旧表单已失效；待核实请求仍保留。'}
    if(status.auth_required===true){const activeSession=await session.stored();if(!active||token!==sequence)return;if(!activeSession)throw new ApiError('会话已失效',false,401);sessionName.value=activeSession.username;signIn.value=false}
    session.declare(status)
    if(status.mode==='development'){readError.value='开发健康模式，无业务数据库。请通过正式或隔离沙盒启动。';return}
    const [o,m,a,i,c,l]=await Promise.all([api('/finance/overview'),api('/market/status'),loadMaster('/accounts'),loadMaster('/instruments'),loadMaster('/finance/categories'),loadMaster('/lots')])
    if(!active||token!==sequence)return
    overview.value=o;market.value=m;accounts.value=a;instruments.value=i;categories.value=c;lots.value=l
    if(!securities.value.some(a=>a.id===selectedAccount.value))selectedAccount.value=str(securities.value[0]?.id)
    if(tradeAccount.value&&!securities.value.some(a=>a.id===tradeAccount.value))tradeAccount.value=''
    if(!instruments.value.some(x=>x.id===quoteInstrument.value))quoteInstrument.value=str(instruments.value[0]?.id)
    refresh.value++
    if(selectedAccount.value)await loadInvestment()
    await loadMonth()
    if(active&&token===sequence&&!readError.value){freshness.value='live';freshnessNote.value=''}
  } catch(e) {if(active&&token===sequence)handleRead(e)}
  finally {if(active&&token===sequence)loading.value=false}
}
/** A rejected session must ask for credentials again instead of showing stale data. */
function handleRead(e:unknown) {
  readError.value=String(e);markStale('读取失败，禁止写入')
  if(e instanceof ApiError&&e.status===401){markStale('会话已失效，禁止写入');signIn.value=true;sessionName.value='';readError.value='会话已过期或被撤销，请重新登录。';forget()}
}
function forget() {sequence++;backupState.value=null;migrationState.value=null;quoteEvidence.value=null;maintenanceError.value='';clearAccountViews();quoteSequence++;clearStrategyView();todo.value=null;netTrend.value=null;tradeDetail.value=null;yearResult.value=null;pnlHistory.value=null;reconciliationReport.value=null;reconciliationError.value='';reconciliationSequence++;system.value=null;overview.value=null;market.value=null;valuation.value=null;strategy.value=null;month.value=null;historical.value=null;accounts.value=[];instruments.value=[];categories.value=[];lots.value=[];spec.value=null;session.forgetIdentity()}
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
  const token=++valuationSequence,ticket=accountScope.capture()
  reconciliationSequence++;reconciliationReport.value=null;reconciliationError.value='';reconciliationLoading.value=false
  if(!quiet){valuation.value=null;strategy.value=null}
  investmentError.value='';investmentLoading.value=!!ticket.account
  if(!ticket.account)return
  try {
    const time=asOf.value?shanghaiTime(asOf.value):new Date().toISOString()
    const id=encodeURIComponent(ticket.account), query=`?as_of=${encodeURIComponent(time)}`
    const [v,s]=await Promise.all([api(`/portfolio/${id}${query}`),api(`/strategy/evidence/${id}${query}`)])
    if(active&&token===valuationSequence&&accountScope.accepts(ticket,v,s)){if(v.ledger_revision!==s.ledger_revision||v.quote_revision!==s.quote_revision||v.database_uuid!==s.database_uuid||v.generation!==s.generation||v.as_of!==s.as_of)throw new Error('持仓与证据版本不一致，请重新读取');valuation.value=v;strategy.value=s}
  }catch(e){if(active&&token===valuationSequence&&accountScope.current(ticket)){valuation.value=null;strategy.value=null;investmentError.value=String(e)}}
  finally{if(active&&token===valuationSequence&&accountScope.current(ticket))investmentLoading.value=false}
}
async function loadReconciliation(){
  reconciliationReport.value=null;reconciliationError.value=''
  const snapshot=valuation.value,ticket=accountScope.capture(),token=++reconciliationSequence
  if(!snapshot||!ticket.account||str(snapshot.account_id)!==ticket.account){
    reconciliationError.value='请先读取当前证券账户持仓，再进行对账预览。';return
  }
  reconciliationLoading.value=true
  try{
    const params=new URLSearchParams({
      as_of:str(snapshot.as_of),ledger_revision:String(snapshot.ledger_revision),
      quote_revision:String(snapshot.quote_revision),database_uuid:str(snapshot.database_uuid),
      generation:String(snapshot.generation),
    })
    const result=await api(`/portfolio/${encodeURIComponent(ticket.account)}/reconciliation?${params.toString()}`)
    if(active&&token===reconciliationSequence&&accountScope.accepts(ticket,result)){
      if(result.account_id!==ticket.account||result.as_of!==snapshot.as_of
        ||result.ledger_revision!==snapshot.ledger_revision||result.quote_revision!==snapshot.quote_revision
        ||result.database_uuid!==snapshot.database_uuid||result.generation!==snapshot.generation){
        throw new Error('对账报告与当前持仓不是同一数据库/账本/行情快照，请重新读取')
      }
      reconciliationReport.value=result
    }
  }catch(e){if(active&&token===reconciliationSequence&&accountScope.current(ticket))reconciliationError.value=String(e)}
  finally{if(active&&token===reconciliationSequence&&accountScope.current(ticket))reconciliationLoading.value=false}
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
async function loadMonth(){month.value=null;const token=sequence;try{const result=await api('/finance/month'+(period.value?`?period=${encodeURIComponent(period.value)}`:''));if(active&&token===sequence)month.value=result}catch(e){if(active&&token===sequence)handleRead(e)}}
async function loadHistory(){historical.value=null;const token=sequence;try{if(!historyTime.value)throw new Error('请选择历史估值时间');const result=await api(`/finance/overview?as_of=${encodeURIComponent(shanghaiTime(historyTime.value))}`);if(active&&token===sequence)historical.value=result}catch(e){if(active&&token===sequence)readError.value=String(e)}}
async function submit(values:Record<string,string>) {
  if(!spec.value||!writable.value)return
  error.value='';feedback.value=''
  const submitted=spec.value,intent=formIntent,originalIdentity=formIdentity
  let body:Fact
  try{body=submitted.build(values)}catch(e){error.value=String(e);return}
  if(!window.confirm(`${spec.value.title}\n${spec.value.confirm??'请核对真实事实及金额。'}\n确定提交？`))return
  busy.value=true
  try{const expected=identityKey(system.value??{});const checked=await api('/system/status');if(spec.value!==submitted||intent!==formIntent||expected!==originalIdentity||identityKey(checked)!==expected)throw new Error('数据库身份已变化，拒绝提交；请重新读取并重新打开表单');session.declare(checked);pendingIdentity=expected;await writer.start(submitted.path,body,submitted.method,intent);feedback.value='提交已确认。正在重新读取实际账本。';spec.value=null;await reload()}
  catch(e){error.value=String(e);if(e instanceof ApiError&&e.status===401)handleRead(e);else if(!(e instanceof ApiError)||e.uncertain||e.status===undefined)markStale('写入结果待核实，请重新读取')}finally{syncPending();busy.value=false}
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
    const state=await writer.check(requestId)
    if(state.committed===true){
      await writer.discard(requestId);spec.value=null
      feedback.value=`原请求 ${requestId} 已在账本中入账，未生成新请求。已按实际账本重新读取。`
      await reload();return
    }
    await writer.retry(requestId);feedback.value=`原请求 ${requestId} 尚未入账，已用同一请求 ID 与相同内容重新提交。`;spec.value=null;await reload()
  }catch(e){error.value=String(e)}
  finally{syncPending();busy.value=false}
}
/** Abandoning an unconfirmed write is an explicit operator decision, never automatic. */
async function discardPending(){
  if(busy.value||!writer.pending)return
  if(!window.confirm('放弃这条待确认写入？之后再提交将生成新的请求 ID，可能与已入账记录重复。请先核对服务器账本中是否存在该请求编号。'))return
  const requestId=writer.requestId
  let removed=true
  try{removed=await writer.discard(requestId)}catch(e){error.value=String(e)}
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
  if(strategyLoading.value||signalWriting.value)return
  strategyError.value='';strategyLoading.value=true
  const token=++poolSequence,ticket=accountScope.capture()
  try{
    if(!ticket.account)throw new Error('请先选择证券账户')
    const id=encodeURIComponent(ticket.account)
    // The read-only path only displays evidence; only POST /strategy/refresh writes signals.
    const query=strategyQuery()
    const [p,pos]=await Promise.all([api(`/strategy/pool/${id}${query}`),api(`/strategy/positions/${id}${query}`)])
    if(active&&token===poolSequence&&accountScope.accepts(ticket,p,pos)){pool.value=p;positionsView.value=pos}
  }catch(e){if(active&&token===poolSequence&&accountScope.current(ticket)){pool.value=null;positionsView.value=null;strategyError.value=String(e)}}
  finally{if(active&&token===poolSequence&&accountScope.current(ticket))strategyLoading.value=false}
}
/** An explicit operator action: this is the only path that records formal signals. */
async function writeStrategySignals(){
  if(signalWriting.value||strategyLoading.value||!writable.value||strategyMode.value!=='current')return
  signalWriting.value=true;strategyError.value='';signalFeedback.value=''
  const token=++poolSequence,ticket=accountScope.capture()
  try{
    if(!ticket.account)throw new Error('请先选择证券账户')
    const id=encodeURIComponent(ticket.account)
    const written=await api(`/strategy/refresh?mode=current&account_id=${id}`,'POST')
    if(!active||token!==poolSequence||!accountScope.accepts(ticket,written))return
    pool.value=written
    signalFeedback.value=facts(written.items).some(row=>!!row.signal_id||!!nested(row.ndx_aux).signal_id)?'已读取服务器保存的信号证据。':'服务端未返回正式信号。'
    const pos=await api(`/strategy/positions/${id}?mode=current`)
    if(active&&token===poolSequence&&accountScope.accepts(ticket,pos))positionsView.value=pos
  }catch(e){if(active&&token===poolSequence&&accountScope.current(ticket)){pool.value=null;positionsView.value=null;strategyError.value=String(e)}}
  finally{if(active&&token===poolSequence&&accountScope.current(ticket))signalWriting.value=false}
}
async function loadMaintenance(){
  maintenanceError.value='';maintenanceLoading.value=true;backupState.value=null;migrationState.value=null;const token=sequence
  try{
    const [b,m]=await Promise.all([api('/maintenance/backups'),api('/maintenance/migration')])
    if(active&&token===sequence){backupState.value=b;migrationState.value=m}
  }catch(e){if(active&&token===sequence){maintenanceError.value=String(e);if(e instanceof ApiError&&e.status===401)handleRead(e)}}
  finally{maintenanceLoading.value=false}
}
async function createManualBackup(){
  if(!writable.value||maintenanceLoading.value)return
  maintenanceLoading.value=true;maintenanceError.value='';feedback.value=''
  try{const result=await api('/maintenance/backups','POST');if(result.ok!==true||result.verified!==true)throw new Error('未取得已验证备份');feedback.value='备份已校验：'+str(result.name)+'，'+str(result.backup_at);await loadMaintenance()}catch(e){maintenanceError.value=String(e)}finally{maintenanceLoading.value=false}
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
  if(!active||busy.value||loading.value||signIn.value)return
  const token=sequence
  if(health.value.kind!=='connected'||freshness.value==='stale'){await reload();return}
  // Check connection and identity even on financial and maintenance pages.
  try{
    const status=await api('/system/status');if(!active||token!==sequence)return
    const key=safeKey(status)
    if(key!==safeKey(system.value)||status.writable!==system.value?.writable){
      forget();readError.value='数据库身份或可写状态已变化；请点击「重新读取」核对后再操作。';markStale('数据库身份已变化，旧数据已清除');return
    }
    system.value=status;session.declare(status)
    if(status.mode==='development'){freshness.value='unavailable';freshnessNote.value='开发健康模式，无业务数据库';return}
    const [o,m]=await Promise.all([api('/finance/overview'),api('/market/status')]);if(!active||token!==sequence)return
    if(overview.value&&Number(o.ledger_revision)!==Number(overview.value.ledger_revision)){refresh.value++;clearStrategyView();todo.value=null;netTrend.value=null;reconciliationSequence++;reconciliationReport.value=null;reconciliationError.value='';reconciliationLoading.value=false}
    const quoteMoved=!market.value||Number(m.quote_revision)!==Number(market.value.quote_revision)
    if(quoteMoved){clearStrategyView();netTrend.value=null;reconciliationSequence++;reconciliationReport.value=null;reconciliationError.value='';reconciliationLoading.value=false}
    overview.value=o;market.value=m
    freshness.value='live';freshnessNote.value='';lastPollAt.value=new Date().toISOString()
    if(tab.value==='投资'&&selectedAccount.value&&!asOf.value&&(quoteMoved||refresh.value))await refreshInvestment(true)
  }catch(e){
    if(!active||token!==sequence)return
    if(e instanceof ApiError&&e.status===401){handleRead(e);markStale('会话已过期，显示的是上次成功读取的值');return}
    // A transport failure proves nothing about the ledger: keep the numbers but label them stale.
    markStale(`自动刷新失败（${String(e)}），下列数字是 ${lastPollAt.value?localTime(lastPollAt.value)+' ':'上一次'}读取的旧值，不是当前值`)
  }
}
function wentOffline(){markStale('网络中断，禁止正式写入；待核实请求仍保留');sequence++;valuationSequence++;quoteSequence++;clearAccountViews();loading.value=false}
function cameOnline(){if(!busy.value)void reload()}
function stopPolling(){if(timer){clearInterval(timer);timer=undefined}}
async function boot(){
  syncPending()
  const existing=await session.stored().catch(()=>null)
  if(!active)return
  sessionName.value=existing?.username ?? ''
  // reload() turns the login panel on by itself when the server answers 401.
  await reload()
  // reload/handleRead is authoritative; do not hide the login panel after a 401.
}
/** Cross-tab: another tab may create, retry or abandon the pending write. */
function onStorage(){syncPending()}
/**
 * Switching account must never leave the previous account's pool, decision table or
 * error text on screen, so every strategy view is dropped before the next load.
 */
function clearStrategyView():void{
  poolSequence++
  pool.value=null;positionsView.value=null;strategyError.value='';signalFeedback.value='';signalWriting.value=false;strategyLoading.value=false
}
function clearAccountViews():void{
  accountScope.reset(selectedAccount.value,()=>{
    valuationSequence++;pnlSequence++;reconciliationSequence++;reconciliationReport.value=null;reconciliationError.value='';reconciliationLoading.value=false
    valuation.value=null;strategy.value=null;pnlHistory.value=null;pnlError.value='';investmentError.value='';investmentLoading.value=!!selectedAccount.value
    clearStrategyView()
  })
  filterTrades()
}
watch(tradeAccount,filterTrades,{flush:'sync'})
watch(selectedAccount,()=>{
  clearAccountViews();tradeAccount.value=selectedAccount.value
  if(!retryPending.value)spec.value=null
  if(tab.value==='投资'&&!loading.value&&selectedAccount.value)void loadInvestment()
},{flush:'sync'})
onMounted(()=>{void boot();window.addEventListener('beforeunload',beforeUnload);window.addEventListener('storage',onStorage);window.addEventListener('offline',wentOffline);window.addEventListener('online',cameOnline);timer=setInterval(()=>{void poll()},20000)})
onBeforeUnmount(()=>{active=false;accountScope.reset('',()=>{});tradeScope.reset('',()=>{});sequence++;valuationSequence++;quoteSequence++;poolSequence++;stopPolling();window.removeEventListener('beforeunload',beforeUnload);window.removeEventListener('storage',onStorage);window.removeEventListener('offline',wentOffline);window.removeEventListener('online',cameOnline)})
const validTransfer=(r:Fact)=>r.status==='effective'
const accountById=(id:unknown)=>accountRows.value.find(a=>a.id===id)

// -- V9/G2 read models -------------------------------------------------------------
const statusLabels:Record<string,string>={SIGNAL:'正式信号（突破成立且闸门通过）',NO_SIGNAL:'未突破（无信号）',DATA_UNAVAILABLE:'数据不可用',FACTOR_BLOCKED:'因子阻止',FACTOR_UNVERIFIED:'因子未核实（严格模式）',COOLDOWN:'冷却期内',POSITION_OPEN:'持仓中',PLAN_PENDING:'已有待执行计划',PORTFOLIO_FULL:'指数仓已达 8 个',CASH_BLOCKED:'现金或权益不足，未生成计划',AWAITING_CONFIRMATION:'次日开盘价已通过条件，待核对真实成交'}
const actionLabels:Record<string,string>={HOLD:'继续持有',STOP:'触发普通止损',GAP_STOP:'跳空止损',EXIT:'到期或规则退出',CLOSED:'已清仓',ADD_STOP:'子仓触发止损',ADD_GAP_STOP:'子仓跳空止损'}
const factorLabels:Record<string,string>={PASS:'通过',FAIL:'不通过',UNVERIFIED:'未核实',DATA_UNAVAILABLE:'数据不足',NOT_REQUIRED:'无额外因子',DISABLED:'未配置组合'}
const num=(v:unknown,d=4)=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(d):display(v)
const nested=(v:unknown)=>v&&typeof v==='object'&&!Array.isArray(v)?v as Fact:{}
const statusText=(v:unknown)=>statusLabels[str(v)]??display(v)
const reconciliationStatus=(v:unknown)=>({pass:'通过',attention:'需要核对',unavailable:'不可计算'} as Record<string,string>)[str(v)]??display(v)
function ndxText(aux:Fact):string{
  if(!aux.status)return '—'
  return `${str(aux.status)}：${str(aux.detail)}；目标 ${aux.target_ratio==null?'—':num(Number(aux.target_ratio)*100,1)+'%'} / ${aux.target_amount==null?'—':num(aux.target_amount,2)+' 元'}；实际 ${aux.actual_quantity??'—'} 股；当前比例 ${aux.current_ratio==null?'—':num(Number(aux.current_ratio)*100,1)+'%'}；实际现金 ${money(aux.actual_cash_fen)}；现金缓冲 25 万元；不预留资金${aux.next_etf_session?'；适用 '+str(aux.next_etf_session):''}`
}
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
    ndx_text:ndxText(nested(item.ndx_aux)),
    factor_text:`因子 ${factorLabels[str(factor.status)]??'—'}；组合 ${factorLabels[str(combo.status)]??'—'}`,
    cooldown_text:nested(item.cooldown).active?str(nested(item.cooldown).note)||'冷却中':'无冷却限制',
    data_text:`${str(series.source)||'无来源'}；${series.bars??0} 根；末日 ${str(series.last_date)||'—'}${str(series.warning)?`；${str(series.warning)}`:''}`,
  }}):[])
const strategyColumns=computed(()=>cols({label:'指数与交易标的',regime_text:'G2 市场状态',er60_text:'ER60',signal_date:'最近正式信号日',price_basis_text:'信号收盘',basis_text:'价格口径',threshold_text:'突破位',buy_max_text:'买入上限',atr14:'ATR14',gap_text:'距突破位 / ATR 倍数',status_text:'信号状态',factor_text:'因子与准入',cooldown_text:'冷却剩余',data_text:'数据来源与质量',input_version_text:'数据版本',tradable_text:'可否预填成交',ndx_text:'NDX T5 辅助仓'}).concat([{key:'detail',label:'状态原因'}]))
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
    ndx_text:ndxText(aux),
    near_stop_text:(Array.isArray(item.near_stop)?facts(item.near_stop):[]).map(r=>`${display(r.role)}：${r.status==='protection_reached'?'报价达到保护价':'接近止损'}（报价 ${r.quote_price}，保护价 ${r.stop_price}，${display(r.quote_at,'quote_at')}，${r.quote_source}）`).join('；')||'无已核实提示',
    action_text:`【模型建议，非实际成交】${actionLabels[str(p.action)]??display(p.action)}${p.action_price==null?'':` @ ${num(p.action_price)}`}${str(p.action_reason)?`；${str(p.action_reason)}`:''}`,
    live_text:item.live_quantity==null?'真实数量不可核实':`策略记录 ${p.add_executed?'已加仓':'主仓'}；账本剩余 ${item.live_quantity} 股`,
    cooldown_text:cooldown.active==null?'冷却状态不可核实':cooldown.active?str(cooldown.note)||'冷却中':'无冷却限制',
  }}):[])
const positionColumns=computed(()=>cols({label:'指数与交易标的',entry_text:'建仓事实（策略证据）',atr_text:'信号日 ATR',stop_text:'当前保护价',near_stop_text:'报价风险提示（非策略指令）',trail_text:'跟踪止盈',term_text:'时间退出',pyramid_text:'盈利加仓条件',ndx_text:'NDX 辅助仓',action_text:'策略建议',live_text:'实际持仓对照',cooldown_text:'冷却'}).concat([{key:'warnings',label:'未核对与提示'}]))
/** Only a recorded formal signal on the unadjusted ETF series may prefill a trade form. */
function canFill(row:Fact):boolean{
  const status=str(row.status)
  if(status!=='SIGNAL'&&status!=='AWAITING_CONFIRMATION'&&status!=='PLAN_PENDING')return false
  // A number derived from an index level or an adjusted series is not a fillable ETF price.
  if(strategyMode.value!=='current'||row.tradable!==true||str(row.price_basis)!=='etf_unadjusted'||!str(row.signal_id)||typeof row.atr14!=='number'||Number(num(row.atr14))<=0)return false
  const code=str(row.etf_code)
  return !!code&&instruments.value.some(i=>str(i.code)===code)
}
function canFillNdx(row:Fact):boolean{
  const aux=nested(row.ndx_aux)
  return strategyMode.value==='current'&&aux.actionable===true&&aux.status==='ALLOW'&&!!str(aux.signal_id)&&str(row.etf_code)==='159941'&&instruments.value.some(i=>str(i.code)===str(row.etf_code))
}
function openTradeFromNdx(row:Fact){
  if(!writable.value||!canFillNdx(row))return
  const aux=nested(row.ndx_aux), instrument=instruments.value.find(i=>str(i.code)===str(row.etf_code))
  openForm(()=>forms.tradeForm(accounts.value,instruments.value,lots.value,undefined,{
    account_id:selectedAccount.value,instrument_id:str(instrument?.id),side:'buy',lot_role:str(aux.lot_role),strategy_id:'NDX_AUX_T5',
    signal_id:str(aux.signal_id),signal_date:str(aux.signal_date),note:'T5 辅助仓建议证据（不预留资金、无冻结 ATR）；仅按券商真实成交录入。分笔成交填写各自券商成交编号。',
  }))
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
  <header><div><h1>Stock Manager 2.0</h1><p>V1.0 候选开发版 · 09-RC（未发布）</p></div><div role="status"><strong>后端：{{health.kind==='connected'?'已连接':health.kind==='checking'?'正在连接':'连接失败'}}</strong> · {{display(system?.mode)}} · {{system?.writable===true?'允许写入':'禁止写入'}}<template v-if="sessionName"> · 已登录：{{sessionName}} <button :disabled="busy" @click="signOut">退出登录</button></template> <button :disabled="busy||loading" @click="reload">重新读取</button></div></header>
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
          <dl class="metrics"><div v-for="item in cols({net_worth_fen:'净资产',liquid_net_worth_fen:'流动净资产',liquid_assets_fen:'流动资产',total_assets_fen:'总资产',total_liabilities_fen:'总负债',securities_equity_fen:'证券权益',securities_cash_fen:'证券现金',securities_market_value_fen:'证券市值',investment_daily_pnl_fen:'证券当日收益'})" :key="item.key"><dt>{{item.label}}</dt><dd>{{money(overview[item.key])}}</dd></div></dl>
          <p>基础投资收益率（上一交易日收盘权益）：{{overview.investment_daily_return_pct==null?'不可计算':String(overview.investment_daily_return_pct)+'%'}}；{{display(overview.investment_daily_return_reason)}}。流动净资产口径：短期负债，其他资产/负债分类未知时不计算；{{display(overview.liquidity_reasons)}}</p>
          <p>质量：{{display(overview.quality)}}；{{display(overview.as_of,'as_of')}}；账本版本 {{overview.ledger_revision}} · 行情版本 {{overview.quote_revision}}</p>
          <p v-if="Array.isArray(overview.reasons)&&overview.reasons.length" class="notice">{{display(overview.reasons)}}</p>
          <h3>本财务月（23日至次月22日）</h3><RecordTable v-if="overviewMonth" :rows="[overviewMonth]" :columns="cols({period:'周期开始',income_fen:'收入',expense_fen:'支出',fees_fen:'手续费',refund_received_fen:'实际收到退款',refund_against_original_fen:'冲减本期原支出',bill_cash_flow_fen:'账单现金流',net_fen:'结余'})"/>
          <h3>风险、信号与待核对</h3><div class="toolbar"><label>证券账户 <select v-model="selectedAccount"><option value="">选择账户</option><option v-for="a in securities" :key="str(a.id)" :value="str(a.id)">{{a.name}}</option></select></label><button :disabled="strategyLoading||signalWriting" @click="refreshWorkstation">只读刷新风险及待办</button><button @click="navigate('投资')">投资详情</button><button @click="navigate('交易')">待核对成交详情</button></div><p v-if="todo">全库待核对成交 {{todo.total}} 条；账本 {{todo.ledger_revision}}，时点 {{display(todo.as_of)}}</p><p v-else>待核对数量尚未读取。</p><p v-if="todoError" class="error">{{todoError}}</p><p v-if="strategyError" class="error">{{strategyError}}</p><template v-if="pool&&strategyMode==='current'"><p>策略行情日 {{display(pool.as_of)}}，账本截止 {{display(pool.ledger_cutoff)}}；只读建议不等于真实成交。</p><RecordTable :rows="strategyRows.filter(r=>r.status==='SIGNAL'||r.status==='AWAITING_CONFIRMATION'||r.status==='PLAN_PENDING'||nested(r.ndx_aux).actionable===true)" :columns="cols({label:'标的',status_text:'状态',detail:'原因',ndx_text:'T5'})"/></template><template v-if="positionsView&&strategyMode==='current'"><h4>实际策略持仓的风险参考（未核实不得执行）</h4><RecordTable :rows="positionRows" :columns="cols({label:'标的',stop_text:'保护价',near_stop_text:'接近保护价提示',action_text:'模型建议',term_text:'期限',ndx_text:'T5',warnings:'证据提示'})"/></template><p v-if="!positionsView">持仓风险尚未读取，不代表没有风险。</p><h3>资产结构</h3><RecordTable :rows="Object.entries(nested(overview.asset_categories)).map(([kind,value])=>({kind,...nested(value)}))" :columns="cols({kind:'类别',asset_class:'资产/负债',amount_fen:'金额',known_fen:'已知部分',quality:'质量'})"/><h3>资产负债明细（含排除统计账户）</h3><RecordTable :rows="accountRows" :columns="accountColumns"/>
        </template>
        <p v-else>总览尚未取得。</p>
      </section>
      <section v-if="tab==='投资'">
        <h2>持仓与行情</h2>
        <div class="toolbar"><label>证券账户 <select v-model="selectedAccount"><option value="">选择账户</option><option v-for="a in securities" :key="String(a.id)" :value="a.id">{{a.name}}</option></select></label><label>历史截止时间（北京时间，留空为当前） <input v-model="asOf" type="datetime-local" step="1"/></label><button @click="loadInvestment">查询持仓</button><button :disabled="!writable" @click="refreshMarket">刷新真实行情</button><button :disabled="!writable" @click="openForm(()=>forms.quoteForm(instruments))">录入核实价格</button></div>
        <p v-if="investmentLoading" role="status">正在读取选定证券账户…</p><p v-if="investmentError" class="error" role="alert">{{investmentError}}</p>
        <template v-if="valuation"><p>数据时点 {{display(valuation.as_of,'as_of')}}；账本 {{valuation.ledger_revision}} / 行情 {{valuation.quote_revision}}；质量 {{display(valuation.quality)}}；{{display(valuation.reasons)}}</p>
          <dl class="metrics"><div v-for="item in cols({cash_fen:'证券现金',market_value_fen:'市值',equity_fen:'账户权益',daily_pnl_fen:'自然日收益',latest_trading_day_pnl_fen:'最近交易日收益',realized_fen:'已实现收益',unrealized_fen:'未实现收益',cumulative_pnl_fen:'累计收益'})" :key="item.key"><dt>{{item.label}}</dt><dd>{{money(valuation[item.key])}}</dd></div></dl>
          <p>基础当日收益率：{{valuation.daily_return_pct==null?'不可计算':String(valuation.daily_return_pct)+'%'}}；上一交易日收盘权益 {{money(valuation.previous_close_equity_fen)}}（{{display(valuation.previous_close_as_of,'as_of')}}）；{{display(valuation.daily_return_reason)}}</p>
          <p>权益对账差额（不是可自动调整金额）：{{money(valuation.reconciliation_difference_fen)}}；{{display(valuation.reasons)}}。</p><button v-if="accountById(selectedAccount)" :disabled="!writable||!!asOf" @click="openForm(()=>forms.equityAdjustmentForm(accountById(selectedAccount)!,valuation!))">核对证券现金差额并校正</button><button :disabled="reconciliationLoading||!selectedAccount||!valuation" @click="loadReconciliation">{{reconciliationLoading?'正在对账…':'生成内部账本对账预览'}}</button><p>累计起算：{{display(valuation.cumulative_from)}}；最近完成交易日：{{display(valuation.latest_completed_trading_day)}}</p>
          <p v-if="reconciliationError" class="error" role="alert">{{reconciliationError}}</p>
          <template v-if="reconciliationReport">
            <h3>内部账本对账预览</h3>
            <p class="notice">只核对本系统已记录的成交、批次、持仓、现金和收益；不是券商对账单或旧库外部对账。此报告只读，不会自动修改任何账本记录。</p>
            <p>报告质量：{{display(reconciliationReport.quality)}}；账本 {{reconciliationReport.ledger_revision}} / 行情 {{reconciliationReport.quote_revision}}；{{display(reconciliationReport.as_of,'as_of')}}</p>
            <RecordTable :rows="facts([reconciliationReport.summary])" :columns="cols({total_trade_count:'成交总数',effective_trade_count:'有效成交',buy_count:'买入笔数',sell_count:'卖出笔数',pending_trade_count:'待核对成交',sale_allocation_mismatch_count:'卖出分配差异',open_lot_count:'未清批次数',open_quantity:'剩余股数',open_cost_fen:'剩余成本及费用',cash_fen:'证券现金',cash_quality:'现金质量',market_value_fen:'证券市值',equity_fen:'证券权益',daily_pnl_fen:'当日收益',equity_pnl_fen:'权益变动收益',reconciliation_difference_fen:'收益勾稽差额',quality:'整体质量'})"/>
            <RecordTable :rows="reconciliationCheckRows" :columns="cols({code:'检查项目',status_text:'结果',message:'说明',checked_count:'检查数量',mismatch_count:'差异数量',count:'待处理数量',difference_fen:'勾稽差额',quality:'质量'})"/>
            <h4>卖出成交批次分配明细</h4>
            <RecordTable :rows="facts(reconciliationReport.allocation_details)" :columns="cols({sell_execution_id:'卖出成交ID',lot_id:'被扣减批次ID',instrument_id:'证券标的',quantity:'扣减股数',cost_fen:'分配本金',buy_fees_fen:'分配买入费用',method:'分配方式',executed_at:'卖出时间'})"/>
            <p v-if="reconciliationReport.allocation_details_truncated" class="notice">批次分配明细超过 100 条，仅显示前 100 条；可按成交历史分页复核。</p>
            <RecordTable v-if="Number(reconciliationReport.exception_count)>0" :rows="facts(reconciliationReport.exceptions)" :columns="cols({kind:'异常类型',record_id:'记录ID',instrument_id:'证券标的',occurred_at:'发生时间',expected:'预期数量',actual:'实际数量',reason:'原因',message:'说明'})"/>
            <p v-if="reconciliationReport.exceptions_truncated" class="notice">异常超过展示上限，仅显示前 100 条；请按成交及批次记录分页复核。</p>
            <p v-else-if="Number(reconciliationReport.exception_count)===0" class="notice">当前快照未发现内部账本数量或收益勾稽差异；这不代表已经与券商或旧库完成外部核对。</p>
          </template>
          <label>搜索持仓代码或名称 <input v-model="holdingSearch"/></label><RecordTable :rows="filteredPositions" :columns="cols({code:'代码',name:'名称',name_quality:'名称质量',market:'市场',quantity:'持仓股数',remaining_cost_fen:'剩余本金',remaining_fees_fen:'剩余费用',holding_cost_fen:'持仓成本',price:'现价（元）',market_value_fen:'市值',daily_pnl_fen:'当日收益',realized_fen:'已实现',unrealized_fen:'未实现',quality:'质量',quote:'报价时点及来源'})"/>
          <h3>当前/历史有效批次</h3><RecordTable :rows="currentLots" :columns="cols({lot_id:'批次ID',open_execution_id:'来源成交ID',opening_id:'期初记录ID',code:'证券代码',lot_role:'角色',strategy_id:'策略',quantity:'数量',cost_fen:'剩余本金',fees_fen:'剩余费用',frozen_atr:'冻结ATR',signal_date:'信号日',opened_at:'开启时间'})"/>
        </template>
        <template v-else><p>请选择证券账户或重新读取。</p></template>
        <h3>月度投资盈亏（日级明细）</h3><p class="notice">基于当前账本与行情版本重算，不表示当时已知的历史证据；未核实金额不显示为零。</p><div class="toolbar"><label>月份 <input type="month" v-model="pnlMonth"/></label><button @click="loadPnlMonth">查询</button></div><p v-if="pnlError" class="error" role="alert">{{pnlError}}</p><template v-if="pnlHistory"><p>账本 {{pnlHistory.ledger_revision}} / 行情 {{pnlHistory.quote_revision}}；查询时间 {{display(pnlHistory.as_of)}}</p><RecordTable :rows="facts(pnlHistory.items)" :columns="cols({day:'日期',is_session:'交易日',daily_pnl_fen:'自然日投资收益',daily_return_pct:'基础收益率（%）',daily_return_reason:'收益率原因',equity_fen:'证券权益',quality:'质量',reasons:'原因',reason:'不可用原因'})"><template #actions="{row}"><button @click="pnlDay(row)">查看该日持仓</button></template></RecordTable></template><h3>V9/G2 策略观察池</h3>
        <p class="notice">默认 <b>ETF 模式</b>下，V9 突破位、ATR 与 BuyMax 全部用<b>ETF 未复权人民币日线</b>计算；<b>G2 基准</b>用 <b>14 只指数真实点位</b>判定。两套价格各自成体系、不混算，也不做点位折算；真实市值用未复权价。表内「信号收盘」列标明每一行用的是哪套价格。策略建议不等于真实成交：真实主仓/子仓/退出只能经「录入真实成交」确认。</p>
        <div class="toolbar"><label>策略模式 <select v-model="strategyMode" :disabled="signalWriting" @change="resetStrategyMode"><option value="current">当前决策</option><option value="historical">历史回放（只读）</option></select></label><label v-if="strategyMode==='historical'">截止交易日 <input type="date" v-model="strategyDay" @change="resetStrategyMode"/></label><button :disabled="strategyLoading||!selectedAccount" @click="refreshStrategy">{{strategyLoading?'正在刷新策略…':'刷新策略观察池（只读）'}}</button><button :disabled="signalWriting||!writable||!selectedAccount||strategyMode!=='current'" @click="writeStrategySignals">{{signalWriting?'正在写入…':'记录正式信号（写入）'}}</button><span v-if="pool">行情日期 {{display(pool.as_of)}}；账本截止 {{display(pool.ledger_cutoff)}}；规则 {{display(pool.strategy_version)}}；基准 {{display(nested(pool.regime).label)}} / ER60 {{display(nested(pool.regime).er60)}}；权益 {{money(pool.equity_fen)}}（{{display(pool.equity_quality)}}）；基础仓 {{money(pool.base_amount_fen)}}；指数仓 {{pool.core_positions}}/{{pool.max_positions}}；可用现金 {{money(pool.cash_fen)}}</span></div>
        <p class="notice">刷新只读取证据、不写库；只有「记录正式信号（写入）」会把信号与数据版本落到账本。</p>
        <p v-if="pool?.historical_replay_complete===false" class="notice">历史配置和更正时点证据不完整：仅供研究，不可执行，不生成保护指令。</p>
        <p v-if="signalFeedback" class="notice">{{signalFeedback}}</p>
        <p v-if="strategyError" class="error" role="alert">{{strategyError}}</p>
        <p v-if="pool&&Array.isArray(nested(pool.regime).warning)===false&&nested(pool.regime).warning" class="notice">G2 基准提示：{{display(nested(pool.regime).warning)}}</p>
        <p v-if="pool&&Array.isArray(pool.equity_reasons)&&pool.equity_reasons.length" class="notice">权益口径：{{display(pool.equity_reasons)}}</p>
        <RecordTable v-if="pool" :rows="strategyRows" :columns="strategyColumns">
          <template #actions="{row}">
            <button v-if="canFill(row)" :disabled="!writable" @click="openTradeFromSignal(row)">按此信号录入真实成交</button>
            <button v-else-if="canFillNdx(row)" :disabled="!writable" @click="openTradeFromNdx(row)">按 T5 建/增仓证据录入真实成交</button>
            <small v-else-if="fillBlockReason(row)">{{fillBlockReason(row)}}</small>
          </template>
        </RecordTable>
        <p v-else-if="!strategyLoading">尚未读取策略观察池。选择证券账户后点击「刷新策略观察池」。</p>
        <h3>持仓决策（策略建议 vs 实际持仓）</h3>
        <p class="notice">ATR 主仓与子仓各自使用真实成交价与信号日冻结 ATR 计算止损；T5 辅助仓独立按实际仓位及 SMA 规则给出目标，不预留资金，减仓/退出须按真实卖出及批次录入；此处不会修改真实持仓。下表「决策」列是<b>模型建议</b>，不代表任何实际成交。</p>
        <p v-if="positionsView&&positionsView.model_only===true" class="notice">此处不释放仓位、不启动冷却；只有真实卖出成交才会。</p>
        <template v-if="positionsView">
          <RecordTable :rows="positionRows" :columns="positionColumns"/>
          <template v-if="facts(positionsView.unrealized_model_actions).length">
            <h4>未实现的模型动作（仅建议，不影响真实持仓）</h4>
            <RecordTable :rows="facts(positionsView.unrealized_model_actions)" :columns="cols({index_id:'指数',etf_code:'ETF 代码',action:'建议动作',action_price:'参考价（元）',action_reason:'原因',price_basis:'价格口径'})"/>
          </template>
          <h4>真实账本中的策略批次（核对依据）</h4>
          <RecordTable v-if="facts(positionsView.custody).length" :rows="facts(positionsView.custody)" :columns="cols({lot_id:'批次ID',lot_role:'角色',strategy_id:'策略标识',strategy_deviation:'偏离策略',deviation_note:'偏离依据',signal_verified:'信号核实',signal_date:'信号日',frozen_atr:'冻结ATR',code:'证券代码',market:'市场',kind:'类型',remaining:'剩余数量'})"/>
          <p v-else class="notice">{{positionsView.custody_quality==='historical_unavailable'?'历史账本版本不可完整核验，不能据此判断持仓为零。':'账本中没有带策略标记的主仓/子仓批次；策略持仓状态若存在则属于未核对证据。'}}</p>
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
        <div class="toolbar"><label>证券账户 <select v-model="tradeAccount"><option value="">全部</option><option v-for="a in securities" :key="str(a.id)" :value="str(a.id)">{{a.name}}</option></select></label><label>证券 <select v-model="tradeInstrument"><option value="">全部</option><option v-for="i in instruments" :key="str(i.id)" :value="str(i.id)">{{i.code}}</option></select></label><label>起始（北京时间，含） <input type="datetime-local" v-model="tradeFrom"/></label><label>结束（北京时间，不含） <input type="datetime-local" v-model="tradeTo"/></label><button @click="filterTrades">筛选成交</button></div>
        <p v-if="tradeReadError" class="error" role="alert">{{tradeReadError}}</p>
        <PagedRecords :key="tradeEpoch" :path="tradePath" :account-id="tradeAccount" :refresh="refresh" :columns="cols({id:'成交ID',account_id:'账户ID',instrument_id:'标的ID',side:'方向',quantity:'数量',price:'价格（元）',fees_fen:'费用',amount_fen:'金额',executed_at:'成交时间',status:'状态',pending_reason:'待核对原因',note:'备注'})"><template #actions="{row}"><button @click="readTrade(row)">查看事实与审计</button><button v-if="row.status==='pending'" :disabled="!writable" @click="openForm(()=>forms.confirmation(`/trades/${row.id}/confirm`))">确认</button><button v-if="row.status!=='reversed'" :disabled="!writable" @click="openForm(()=>forms.tradeForm(accounts,instruments,lots,row))">更正</button><button v-if="row.status!=='reversed'" :disabled="!writable" @click="openForm(()=>forms.voidForm(`/trades/${row.id}/void`,'作废成交'))">作废</button></template></PagedRecords>
        <details v-if="tradeDetail" open><summary>成交事实与审计记录</summary><RecordTable :rows="[tradeDetail]" :columns="cols({id:'成交ID',account_id:'账户',side:'方向',quantity:'数量',status:'状态',strategy_deviation:'策略偏离',note:'说明'})"/><RecordTable :rows="facts(tradeDetail.audit_events)" :columns="cols({event:'事件',created_at:'记录时间',reason:'原因',entity_id:'对象'})"/><details><summary>原始审计证据</summary><pre>{{JSON.stringify(tradeDetail,null,2)}}</pre></details></details>
        <h3>证券资金记录</h3><PagedRecords path="/cash" :refresh="refresh" :columns="cashColumns"><template #actions="{row}"><template v-if="!row.transfer_id&&!row.execution_id&&['opening','deposit','withdrawal','adjustment'].includes(String(row.kind))"><button :disabled="!writable" @click="openForm(()=>forms.cashForm(accounts,row))">更正</button><button :disabled="!writable" @click="openForm(()=>forms.voidForm(`/cash/${row.id}/void`,'作废资金记录'))">作废</button></template></template></PagedRecords>
        <details><summary>账户及证券标的 ID 对照</summary><RecordTable :rows="accounts" :columns="cols({id:'账户ID',name:'名称',kind:'类型'})"/><RecordTable :rows="instruments" :columns="cols({id:'标的ID',market:'市场',code:'代码',kind:'类型'})"/></details>
        <h3>期初持仓批次（可更正 / 作废）</h3>
        <p class="notice">更正与作废都保留审计痕迹：作废不会删除原批次，只会写入一条冲正记录。这里只列出期初批次，不改动真实成交历史。</p>
        <PagedRecords path="/openings" :refresh="refresh" :columns="cols({id:'期初批次ID',account_id:'账户ID',instrument_id:'标的ID',quantity:'数量',cost_fen:'成本',fees_fen:'费用',occurred_at:'发生时间',status:'状态',note:'备注'})"><template #actions="{row}"><button :disabled="!writable" @click="openForm(()=>forms.openingCorrectionForm(row))">更正</button><button :disabled="!writable" @click="openForm(()=>forms.openingVoidForm(row))">作废</button></template></PagedRecords>
        <h3>现金分红</h3>
        <p class="notice">现金分红是外部收益，进入证券账户现金，不改变持仓股数。拆分与合并仅录入已核实的实际比例及真实尾差；复杂配股尚不支持数量调整。</p>
        <div class="toolbar"><button :disabled="!writable||!securities.length||!instruments.length" @click="openForm(()=>forms.dividendForm(accounts,instruments))">录入现金分红</button><button :disabled="!writable||!securities.length||!instruments.length" @click="openForm(()=>forms.companyForm(accounts,instruments))">核实拆分 / 合并</button></div>
        <PagedRecords path="/company-events" :refresh="refresh" :columns="cols({id:'公司行为ID',account_id:'账户ID',instrument_id:'标的ID',kind:'类型',occurred_at:'发生时间',cash_movement_id:'对应现金流水',note:'备注'})"><template #actions="{row}"><button v-if="row.kind==='dividend'" :disabled="!writable" @click="openForm(()=>forms.dividendCorrectionForm(row))">更正金额</button><button v-if="row.status!=='reversed'" :disabled="!writable" @click="openForm(()=>forms.voidForm(row.kind==='dividend'?`/company-events/${row.id}/void`:`/portfolio/company-events/${row.id}/void`,row.kind==='dividend'?'作废现金分红':'作废公司行为'))">作废</button></template></PagedRecords>
      </section>
      <section v-if="tab==='财务'"><h2>个人财务</h2><div class="toolbar"><button :disabled="!writable" @click="openForm(forms.accountForm)">创建账户</button><button :disabled="!writable" @click="openForm(forms.categoryForm)">新增分类</button><button :disabled="!writable" @click="openForm(()=>forms.billForm(accounts,categories))">新增收入 / 支出 / 手续费</button><button :disabled="!writable||!overview" @click="openForm(()=>forms.transferForm(accountRows))">内部转账</button></div>
        <h3>账户及余额</h3><RecordTable :rows="accountRows" :columns="accountColumns"><template #actions="{row}"><button v-if="row.kind!=='securities'&&row.status==='active'" :disabled="!writable" @click="openForm(()=>forms.balanceForm(row))">余额核实</button><button :disabled="!writable" @click="openForm(()=>forms.accountUpdate(row,currentRevision))">账户状态</button><button :disabled="!writable" @click="openForm(()=>forms.scopeForm(row,currentRevision))">统计范围</button><button v-if="!row.effective_at" :disabled="!writable" @click="openForm(()=>forms.effectiveForm(row,currentRevision))">核实生效时间</button></template></RecordTable>
        <h3>普通收支账单</h3><div class="toolbar"><label>账户 <select v-model="billAccount"><option value="">全部</option><option v-for="a in accounts" :key="str(a.id)" :value="str(a.id)">{{a.name}}</option></select></label><label>起始（北京时间，含） <input type="datetime-local" v-model="billFrom"/></label><label>结束（北京时间，不含） <input type="datetime-local" v-model="billTo"/></label><button @click="filterBills">筛选账单</button></div><p v-if="billReadError" class="error">{{billReadError}}</p><PagedRecords :path="billPath" :refresh="refresh" :columns="billColumns"><template #actions="{row}"><template v-if="row.status==='effective'"><button :disabled="!writable" @click="openForm(()=>row.kind==='refund'?forms.refundForm(accounts,row,row):forms.billForm(accounts,categories,row))">更正</button><button v-if="['expense','fee'].includes(String(row.kind))" :disabled="!writable" @click="openForm(()=>forms.refundForm(accounts,row))">录入退款</button><button :disabled="!writable" @click="openForm(()=>forms.voidForm(`/finance/bills/${row.id}/void`,'作废账单'))">作废</button></template></template></PagedRecords>
        <h3>内部转账</h3><PagedRecords path="/finance/transfers" :refresh="refresh" :columns="transferColumns"><template #actions="{row}"><template v-if="validTransfer(row)"><button v-if="!row.from_included&&!row.to_included" :disabled="!writable" @click="openForm(()=>forms.transferForm(accountRows,row))">更正</button><button v-if="!row.from_included&&!row.to_included" :disabled="!writable" @click="openForm(()=>forms.voidForm(`/finance/transfers/${row.id}/void`,'作废转账'))">作废</button><button v-if="accountById(row.from_account_id)?.kind!=='securities'||accountById(row.to_account_id)?.kind!=='securities'" :disabled="!writable" @click="openForm(()=>forms.reconciliationForm(accountRows,row))">人工核对更正 / 作废</button></template></template></PagedRecords>
        <h3>财务月</h3><label>周期开始日期（23日，留空为当前） <input type="date" v-model="period"/></label><button @click="loadMonth">查询</button><template v-if="month"><RecordTable :rows="[month]" :columns="cols({period:'周期开始',start_at:'开始时点',end_exclusive:'结束（不含）',income_fen:'收入',expense_fen:'支出',fees_fen:'手续费',refund_received_fen:'实际收到退款',refund_against_original_fen:'冲减本期原支出',bill_cash_flow_fen:'账单现金流',net_fen:'结余',ledger_revision:'账本版本'})"/><RecordTable :rows="Object.entries(nested(month.categories)).map(([category_id,amount_fen])=>({category_id,amount_fen}))" :columns="cols({category_id:'分类',amount_fen:'分类金额'})"/><RecordTable :rows="facts(month.items)" :columns="billColumns"/></template>
        <h3>按年查询财务月</h3><label>年份 <input type="number" min="1900" max="9997" v-model.number="financeYear"/></label><button @click="loadYear">查询年度财务月</button><p v-if="yearError" class="error" role="alert">{{yearError}}</p><RecordTable v-if="yearResult" :rows="facts(yearResult.items)" :columns="cols({period:'周期开始',income_fen:'收入',expense_fen:'支出',fees_fen:'手续费',refund_received_fen:'实际收到退款',refund_against_original_fen:'冲减本期原支出',bill_cash_flow_fen:'账单现金流',net_fen:'结余'})"/><h3>基础净资产趋势（按月末，当前月截至查询）</h3><p>当前账本/行情版本重算，非历史知识回放，未知值保留空缺。</p><label>月份数 <select v-model.number="netMonths"><option :value="1">1</option><option :value="3">3</option><option :value="6">6</option><option :value="12">12</option></select></label><button @click="loadNetTrend">查询净资产趋势</button><button :disabled="!netTrend" @click="exportNetTrend">导出摘要 CSV（分）</button><p v-if="netTrendError" class="error">{{netTrendError}}</p><template v-if="netTrend"><p>账本 {{netTrend.ledger_revision}} / 行情 {{netTrend.quote_revision}}</p><RecordTable :rows="facts(netTrend.items)" :columns="cols({as_of:'估值时点',total_assets_fen:'总资产',total_liabilities_fen:'总负债',net_worth_fen:'净资产',liquid_net_worth_fen:'流动净资产',liquidity_reasons:'流动性原因',securities_equity_fen:'证券权益',quality:'质量',reasons:'原因'})"/></template><h3>历史余额与净资产</h3><label>截止时间（北京时间） <input type="datetime-local" step="1" v-model="historyTime"/></label><button @click="loadHistory">查询历史</button><template v-if="historical"><RecordTable :rows="[historical]" :columns="cols({as_of:'估值时点',net_worth_fen:'净资产',liquid_net_worth_fen:'流动净资产',liquid_assets_fen:'流动资产',total_assets_fen:'总资产',total_liabilities_fen:'总负债',quality:'质量',ledger_revision:'账本版本',quote_revision:'报价版本',reasons:'原因'})"/><RecordTable :rows="facts(historical.accounts)" :columns="accountColumns"/></template>
        <details><summary>手工余额历史</summary><PagedRecords path="/finance/balances" :refresh="refresh" :columns="cols({account_id:'账户ID',before_fen:'原余额',after_fen:'新余额',occurred_at:'生效时间',reason:'原因',status:'状态'})"/></details>
      </section>
    </template>
    <section v-if="tab==='系统'"><h2>系统与维护</h2><p>运行模式：{{display(system?.mode)}}；{{system?.writable===true?'允许写入':'禁止写入'}}。断线不会切换数据库。服务监听端口：{{display(system?.listen_port)}}；端口修改只在停止服务、修改受控配置并重新启动后生效，网页不执行重启。</p><details v-if="system?.identity" open><summary>已验证数据库身份</summary><pre>{{JSON.stringify(system.identity,null,2)}}</pre></details><h3>行情及日历</h3><p v-if="market && market.calendar_coverage" class="notice">交易日历：{{display((market.calendar_coverage as Fact).start)}} 至 {{display((market.calendar_coverage as Fact).end)}}；剩余 {{display((market.calendar_coverage as Fact).days_remaining)}} 天。</p><p v-if="market && market.calendar_coverage && (market.calendar_coverage as Fact).status !== 'valid'" class="error" role="alert">交易日历即将到期或已超出可信范围。请按本地验收说明核实交易所公告并更新锁定日历依赖并执行 tools/verify_calendar.py 审查；超出范围不生成正式交易建议。</p><pre v-if="market">{{JSON.stringify({calendar_coverage:market.calendar_coverage,calendar_release:market.calendar_release,market_phase:market.market_phase,last_refresh:market.last_refresh},null,2)}}</pre><p v-else>行情状态不可用。</p><button :disabled="!writable" @click="refreshMarket">手动刷新行情</button><h3>维护命令（在服务端终端显式执行）</h3><p>正式库身份检查：<code>python start.py check-database</code></p><p>一致性备份：<code>python start.py backup --destination &lt;新备份目录绝对路径&gt;</code></p><p>数据库升级：停止写服务，执行 <code>python start.py upgrade-database --destination &lt;新备份目录绝对路径&gt; --confirm</code>。不从网页自动接管或恢复正式库。</p><h3>备份与迁移对账</h3><p class="notice">一键备份由服务端一致性快照完成；网页端不会自动接管、还原或删除任何备份。</p><div class="toolbar"><button :disabled="!writable||maintenanceLoading" @click="createManualBackup">一键备份</button><button :disabled="maintenanceLoading" @click="loadMaintenance">{{maintenanceLoading?'正在读取…':'查询备份状态与迁移对账'}}</button></div><p v-if="maintenanceError" class="error" role="alert">{{maintenanceError}}</p><template v-if="backupState"><h4>备份状态</h4><RecordTable :rows="facts(backupState.items).length?facts(backupState.items):[backupState]" :columns="cols({name:'备份名称',path:'位置',backup_at:'创建时间',file_size:'体积（字节）',verified:'已校验',status:'状态'})"/><p v-if="facts(backupState.items).length===0" class="notice">服务端尚未返回任何备份条目。</p></template><template v-if="migrationState"><h4>迁移对账报告</h4><pre>{{JSON.stringify(migrationState,null,2)}}</pre></template><p class="notice">V9/G2 集成待审查，证券实网验证尚未完成。当前只用于本机开发/隔离测试，不开放公网。</p></section>
  </main>
</template>

<style>
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#182536;background:#f7f8fa;line-height:1.5;font-size:15px}*{box-sizing:border-box}body{margin:0}header{padding:18px 24px;background:#fff;border-bottom:1px solid #cbd5e1;display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap}h1{font-size:1.5rem;margin:0}header p{margin:3px 0;color:#526477}nav{display:flex;gap:8px;padding:12px 24px;background:#edf1f5;flex-wrap:wrap}button{font:inherit;cursor:pointer;border:1px solid #a6b5c5;background:#fff;color:#20344c;border-radius:5px;padding:7px 12px;margin:3px}button.selected{background:#274d75;color:white}button:disabled{cursor:not-allowed;opacity:.5}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #276da2;outline-offset:2px}main{max-width:1500px;margin:auto;padding:20px 24px}section{background:white;border:1px solid #d9e0e7;border-radius:7px;padding:16px;margin-bottom:16px}h2{font-size:1.25rem;margin:0 0 14px}h3{font-size:1.05rem;margin:20px 0 8px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}.metrics div{background:#eef3f7;border-radius:5px;padding:12px}.metrics dt{color:#4c5b70}.metrics dd{font-size:1.4rem;font-weight:600;margin:3px 0;font-variant-numeric:tabular-nums}.table-scroll{overflow:auto;max-width:100%}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{padding:8px;text-align:left;border-bottom:1px solid #dde4eb;vertical-align:top;max-width:400px;overflow-wrap:anywhere}th{background:#edf1f5;white-space:nowrap}.toolbar{display:flex;flex-wrap:wrap;align-items:end;gap:8px}.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin-bottom:12px}label{display:flex;flex-direction:column;gap:3px}input,select{padding:8px;font:inherit;min-width:0;border:1px solid #9daebd;border-radius:4px;background:white}small{color:#526477}.operation{border:2px solid #809cba}.error{background:#fff1f0;color:#8b2621;padding:10px;border-left:4px solid #a5322c;white-space:pre-wrap;overflow-wrap:anywhere}.notice{background:#f0f4f9;padding:10px;border-left:4px solid #4779a4;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto}code{overflow-wrap:anywhere}details{margin-top:16px}
</style>
