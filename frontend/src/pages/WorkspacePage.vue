<script setup lang="ts">
import {computed, onMounted, onBeforeUnmount, ref} from 'vue'
import {api, fact, facts, page, str, WriteAttempt, type Fact} from '../api/client'
import {getHealth, type HealthState} from '../api/health'
import {display, money, shanghaiTime, type FormSpec} from '../ui'
import * as forms from '../forms'
import OperationForm from '../components/OperationForm.vue'
import RecordTable from '../components/RecordTable.vue'
import PagedRecords from '../components/PagedRecords.vue'

const tabs=['总览','投资','交易','财务','系统'] as const
const tab=ref<typeof tabs[number]>('总览')
const health=ref<HealthState>({kind:'checking'}), system=ref<Fact|null>(null), overview=ref<Fact|null>(null), market=ref<Fact|null>(null)
const accounts=ref<Fact[]>([]), instruments=ref<Fact[]>([]), categories=ref<Fact[]>([]), lots=ref<Fact[]>([])
const valuation=ref<Fact|null>(null), strategy=ref<Fact|null>(null), month=ref<Fact|null>(null), historical=ref<Fact|null>(null)
const selectedAccount=ref(''), asOf=ref(''), period=ref(''), historyTime=ref('')
const error=ref(''), readError=ref(''), feedback=ref(''), busy=ref(false), loading=ref(false), retryPending=ref(false)
const spec=ref<FormSpec|null>(null), formKey=ref(0), refresh=ref(0)
const writer=new WriteAttempt()
let active=true, sequence=0, valuationSequence=0
let pendingIdentity: string|null=null, formIdentity: string|null=null
function identityKey(status:Fact): string {
  if(!status.identity||!status.writable)throw new Error('当前没有已验证的可写数据库')
  const i=fact(status.identity)
  return JSON.stringify([status.mode,i.database_uuid,i.generation,i.schema_version])
}
const writable=computed(()=>health.value.kind==='connected'&&system.value?.writable===true&&overview.value!==null&&!busy.value&&!retryPending.value&&!loading.value)
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
  try {
    const h=await getHealth();if(!active||token!==sequence)return;health.value=h
    if(h.kind!=='connected')throw new Error('后端连接失败；未切换数据库，旧数据不再显示为当前值。')
    const status=await api('/system/status');if(!active||token!==sequence)return;system.value=status
    if(status.mode==='development'){readError.value='开发健康模式，无业务数据库。请通过正式或隔离沙盒启动。';return}
    const [o,m,a,i,c,l]=await Promise.all([api('/finance/overview'),api('/market/status'),loadMaster('/accounts'),loadMaster('/instruments'),loadMaster('/finance/categories'),loadMaster('/lots')])
    if(!active||token!==sequence)return
    overview.value=o;market.value=m;accounts.value=a;instruments.value=i;categories.value=c;lots.value=l
    if(!securities.value.some(a=>a.id===selectedAccount.value))selectedAccount.value=str(securities.value[0]?.id)
    refresh.value++
    if(selectedAccount.value)await loadInvestment()
    await loadMonth()
  } catch(e) {if(active&&token===sequence)readError.value=String(e)}
  finally {if(active&&token===sequence)loading.value=false}
}
async function loadInvestment(){
  const token=++valuationSequence;valuation.value=null;strategy.value=null
  if(!selectedAccount.value)return
  try {
    const time=asOf.value?shanghaiTime(asOf.value):new Date().toISOString()
    const id=encodeURIComponent(selectedAccount.value), query=`?as_of=${encodeURIComponent(time)}`
    const [v,s]=await Promise.all([api(`/portfolio/${id}${query}`),api(`/strategy/evidence/${id}${query}`)])
    if(active&&token===valuationSequence){valuation.value=v;strategy.value=s}
  }catch(e){if(active&&token===valuationSequence)readError.value=String(e)}
}
async function loadMonth(){month.value=null;try{month.value=await api('/finance/month'+(period.value?`?period=${encodeURIComponent(period.value)}`:''))}catch(e){readError.value=String(e)}}
async function loadHistory(){historical.value=null;try{if(!historyTime.value)throw new Error('请选择历史估值时间');historical.value=await api(`/finance/overview?as_of=${encodeURIComponent(shanghaiTime(historyTime.value))}`)}catch(e){readError.value=String(e)}}
async function submit(values:Record<string,string>) {
  if(!spec.value||!writable.value)return
  error.value='';feedback.value=''
  let body:Fact
  try{body=spec.value.build(values)}catch(e){error.value=String(e);return}
  if(!window.confirm(`${spec.value.title}\n${spec.value.confirm??'请核对真实事实及金额。'}\n确定提交？`))return
  busy.value=true
  try{const expected=identityKey(system.value??{});const checked=await api('/system/status');if(expected!==formIdentity||identityKey(checked)!==expected)throw new Error('数据库身份已变化，拒绝提交；请重新读取并重新打开表单');pendingIdentity=expected;await writer.start(spec.value.path,body,spec.value.method);feedback.value='提交已确认。正在重新读取实际账本。';spec.value=null;await reload()}
  catch(e){error.value=String(e)}finally{retryPending.value=writer.pending!==null;if(!writer.pending)pendingIdentity=null;busy.value=false}
}
async function retry(){if(busy.value||!writer.pending)return;busy.value=true;error.value='';try{const checked=await api('/system/status');if(identityKey(checked)!==pendingIdentity)throw new Error('数据库身份与原请求不一致，禁止跨库重试');await writer.retry();feedback.value='原请求已确认；未重复提交新请求。';spec.value=null;await reload()}catch(e){error.value=String(e)}finally{retryPending.value=writer.pending!==null;if(!writer.pending)pendingIdentity=null;busy.value=false}}
async function refreshMarket(){if(!writable.value)return;busy.value=true;error.value='';feedback.value='';try{const r=await api('/market/refresh','POST');feedback.value=`实际行情刷新结果：${JSON.stringify(r)}`;await reload()}catch(e){error.value=String(e)}finally{busy.value=false}}
function beforeUnload(e:BeforeUnloadEvent){if(writer.pending||busy.value){e.preventDefault();e.returnValue=''}}
let timer:ReturnType<typeof setInterval>|undefined
onMounted(()=>{void reload();window.addEventListener('beforeunload',beforeUnload);timer=setInterval(async()=>{if(busy.value||loading.value)return;const h=await getHealth();if(!active)return;if(h.kind!=='connected'){health.value=h;system.value=null;overview.value=null;valuation.value=null;market.value=null;month.value=null;historical.value=null;readError.value='后端连接失败；请恢复连接后重新读取。'}else if(health.value.kind!=='connected'){health.value=h;await reload()}},10000)})
onBeforeUnmount(()=>{active=false;sequence++;valuationSequence++;if(timer)clearInterval(timer);window.removeEventListener('beforeunload',beforeUnload)})
const validTransfer=(r:Fact)=>r.status==='effective'
const accountById=(id:unknown)=>accountRows.value.find(a=>a.id===id)
</script>

<template>
  <header><div><h1>Stock Manager 2.0</h1><p>V1.0 开发版 · 06-ui</p></div><div role="status"><strong>后端：{{health.kind==='connected'?'已连接':health.kind==='checking'?'正在连接':'连接失败'}}</strong> · {{display(system?.mode)}} · {{system?.writable===true?'允许写入':'禁止写入'}} <button :disabled="busy||loading" @click="reload">重新读取</button></div></header>
  <nav aria-label="主导航"><button v-for="name in tabs" :key="name" :aria-current="tab===name?'page':undefined" :class="{selected:tab===name}" @click="navigate(name)">{{name}}</button></nav>
  <main>
    <p v-if="loading" role="status">正在读取服务器真实数据…</p>
    <p v-if="readError" class="error" role="alert">{{readError}}</p>
    <p v-if="error" class="error" role="alert">{{error}}</p>
    <p v-if="feedback" class="notice" role="status">{{feedback}}</p>
    <section v-if="retryPending" class="error" role="alert">写入结果未知。请勿重录或刷新页面；使用原请求 ID 重试（同一内容）。<button :disabled="busy" @click="retry">重试原请求</button></section>
    <OperationForm v-if="spec" :key="formKey" :spec="spec" :busy="busy||retryPending||!writable" @submit="submit" @cancel="spec=null" />
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
        </template><p v-else>请选择证券账户或重新读取。</p>
        <h3>V9/G2 策略</h3><p class="notice">正式策略引擎尚未迁移（旧源码未提供），观察池、正式信号及决策不可用。以下仅显示已经保存的批次证据，不是交易建议。</p><details v-if="strategy"><summary>原批次证据</summary><pre>{{JSON.stringify(strategy,null,2)}}</pre></details>
        <h3>实际行情状态</h3><RecordTable v-if="market" :rows="facts(market.items)" :columns="cols({code:'证券代码',market:'市场',quality:'质量',quote:'报价证据'})"/>
      </section>
      <section v-if="tab==='交易'"><h2>真实成交</h2>
        <div class="toolbar"><button :disabled="!writable" @click="openForm(forms.accountForm)">创建账户</button><button :disabled="!writable" @click="openForm(forms.instrumentForm)">创建证券标的</button><button :disabled="!writable" @click="openForm(()=>forms.cashForm(accounts))">期初及证券现金</button><button :disabled="!writable" @click="openForm(()=>forms.tradeForm(accounts,instruments,lots))">录入买入 / 卖出</button></div>
        <h3>成交历史（包括待核对和冲正）</h3>
        <PagedRecords path="/trades" :refresh="refresh" :columns="cols({id:'成交ID',account_id:'账户ID',instrument_id:'标的ID',side:'方向',quantity:'数量',price:'价格（元）',fees_fen:'费用',amount_fen:'金额',executed_at:'成交时间',status:'状态',pending_reason:'待核对原因',note:'备注'})"><template #actions="{row}"><button v-if="row.status==='pending'" :disabled="!writable" @click="openForm(()=>forms.confirmation(`/trades/${row.id}/confirm`))">确认</button><button v-if="row.status!=='reversed'" :disabled="!writable" @click="openForm(()=>forms.tradeForm(accounts,instruments,lots,row))">更正</button><button v-if="row.status!=='reversed'" :disabled="!writable" @click="openForm(()=>forms.voidForm(`/trades/${row.id}/void`,'作废成交'))">作废</button></template></PagedRecords>
        <h3>证券资金记录</h3><PagedRecords path="/cash" :refresh="refresh" :columns="cashColumns"><template #actions="{row}"><template v-if="!row.transfer_id&&!row.execution_id&&['deposit','withdrawal','adjustment'].includes(String(row.kind))"><button :disabled="!writable" @click="openForm(()=>forms.cashForm(accounts,row))">更正</button><button :disabled="!writable" @click="openForm(()=>forms.voidForm(`/cash/${row.id}/void`,'作废资金记录'))">作废</button></template></template></PagedRecords>
        <details><summary>账户及证券标的 ID 对照</summary><RecordTable :rows="accounts" :columns="cols({id:'账户ID',name:'名称',kind:'类型'})"/><RecordTable :rows="instruments" :columns="cols({id:'标的ID',market:'市场',code:'代码',kind:'类型'})"/></details>
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
    <section v-if="tab==='系统'"><h2>系统与维护</h2><p>运行模式：{{display(system?.mode)}}；{{system?.writable===true?'允许写入':'禁止写入'}}。断线不会切换数据库。</p><details v-if="system?.identity" open><summary>已验证数据库身份</summary><pre>{{JSON.stringify(system.identity,null,2)}}</pre></details><h3>行情及日历</h3><pre v-if="market">{{JSON.stringify({calendar_coverage:market.calendar_coverage,market_phase:market.market_phase,last_refresh:market.last_refresh},null,2)}}</pre><p v-else>行情状态不可用。</p><button :disabled="!writable" @click="refreshMarket">手动刷新行情</button><h3>维护命令（在服务端终端显式执行）</h3><p>正式库身份检查：<code>python start.py check-database</code></p><p>一致性备份：<code>python start.py backup --destination &lt;新备份目录绝对路径&gt;</code></p><p>数据库升级：停止写服务，执行 <code>python start.py upgrade-database --destination &lt;新备份目录绝对路径&gt; --confirm</code>。不从网页自动接管或恢复正式库。</p><p class="notice">正式 V9/G2 策略及真实行情实网验证尚未完成。当前只用于本机开发/隔离测试，不开放公网。</p></section>
  </main>
</template>

<style>
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#182536;background:#f7f8fa;line-height:1.5;font-size:15px}*{box-sizing:border-box}body{margin:0}header{padding:18px 24px;background:#fff;border-bottom:1px solid #cbd5e1;display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap}h1{font-size:1.5rem;margin:0}header p{margin:3px 0;color:#526477}nav{display:flex;gap:8px;padding:12px 24px;background:#edf1f5;flex-wrap:wrap}button{font:inherit;cursor:pointer;border:1px solid #a6b5c5;background:#fff;color:#20344c;border-radius:5px;padding:7px 12px;margin:3px}button.selected{background:#274d75;color:white}button:disabled{cursor:not-allowed;opacity:.5}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #276da2;outline-offset:2px}main{max-width:1500px;margin:auto;padding:20px 24px}section{background:white;border:1px solid #d9e0e7;border-radius:7px;padding:16px;margin-bottom:16px}h2{font-size:1.25rem;margin:0 0 14px}h3{font-size:1.05rem;margin:20px 0 8px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}.metrics div{background:#eef3f7;border-radius:5px;padding:12px}.metrics dt{color:#4c5b70}.metrics dd{font-size:1.4rem;font-weight:600;margin:3px 0;font-variant-numeric:tabular-nums}.table-scroll{overflow:auto;max-width:100%}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{padding:8px;text-align:left;border-bottom:1px solid #dde4eb;vertical-align:top;max-width:400px;overflow-wrap:anywhere}th{background:#edf1f5;white-space:nowrap}.toolbar{display:flex;flex-wrap:wrap;align-items:end;gap:8px}.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin-bottom:12px}label{display:flex;flex-direction:column;gap:3px}input,select{padding:8px;font:inherit;min-width:0;border:1px solid #9daebd;border-radius:4px;background:white}small{color:#526477}.operation{border:2px solid #809cba}.error{background:#fff1f0;color:#8b2621;padding:10px;border-left:4px solid #a5322c;white-space:pre-wrap;overflow-wrap:anywhere}.notice{background:#f0f4f9;padding:10px;border-left:4px solid #4779a4;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto}code{overflow-wrap:anywhere}details{margin-top:16px}
</style>
