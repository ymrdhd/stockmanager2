/** Exercise the actual WorkspacePage setup code with synchronous ref/watch and
 * deterministic delayed API doubles. No Vue renderer, DOM or browser is claimed. */
import assert from 'node:assert/strict'
import {test} from 'node:test'
import {readFileSync} from 'node:fs'
import {stripTypeScriptTypes} from 'node:module'
import {runInNewContext} from 'node:vm'
import {AccountScope} from '../src/api/accountScope.ts'
import {ApiError, fact, facts, page, str, WriteAttempt, type Fact} from '../src/api/client.ts'
import {display, localTime, money, shanghaiTime} from '../src/ui.ts'
type Ref<T>={value:T;listeners:Set<()=>void>}
const viewAccount=(view:Ref<Fact|null>)=>view.value?.account_id
function ref<T>(initial:T):Ref<T>{let v=initial;const listeners=new Set<()=>void>();return {get value(){return v},set value(next:T){if(next!==v){v=next;for(const listener of listeners)listener()}},listeners}}
function watch<T>(source:Ref<T>,callback:()=>void){source.listeners.add(callback)}
function deferred<T>(){let resolve!:(value:T)=>void,reject!:(reason:unknown)=>void;const promise=new Promise<T>((r,j)=>{resolve=r;reject=j});return {promise,resolve,reject}}
const tick=()=>new Promise<void>(r=>setImmediate(r))
type Setup={pnlDay(row:Fact):Promise<void>;loadReconciliation():Promise<void>;reconciliationReport:Ref<Fact|null>;reconciliationError:Ref<string>;reconciliationLoading:Ref<boolean>;wentOffline():void;freshness:Ref<string>;writable:Ref<boolean>;system:Ref<Fact|null>;overview:Ref<Fact|null>;health:Ref<Fact>;selectedAccount:Ref<string>;valuation:Ref<Fact|null>;strategy:Ref<Fact|null>;pool:Ref<Fact|null>;positionsView:Ref<Fact|null>;pnlHistory:Ref<Fact|null>;pnlMonth:Ref<string>;tradeDetail:Ref<Fact|null>;tradeAccount:Ref<string>;tradePath:Ref<string>;investmentError:Ref<string>;investmentLoading:Ref<boolean>;navigate(tab:string):void;refreshInvestment(quiet:boolean):Promise<void>;refreshStrategy():Promise<void>;loadPnlMonth():Promise<void>;readTrade(row:Fact):Promise<void>}
function setup(request:(path:string)=>Promise<Fact>):Setup{
  const raw=readFileSync(process.env.SM2_WORKSPACE_SOURCE || new URL('../src/pages/WorkspacePage.vue',import.meta.url),'utf-8')
  const source=raw.split('<script setup lang="ts">')[1].split('</script>')[0].replace(/^import .*$/gm,'')
  const js=stripTypeScriptTypes(source,{mode:'strip'})
  const context={ref,watch,computed:(get:()=>unknown)=>({get value(){return get()}}),onMounted:()=>{},onBeforeUnmount:()=>{},
    api:request,ApiError,fact,facts,page,str,WriteAttempt,AccountScope,display,localTime,money,shanghaiTime,URLSearchParams,Date,
    harness:null as Setup|null}
  runInNewContext(js+`\nglobalThis.harness={selectedAccount,valuation,strategy,pool,positionsView,pnlHistory,pnlMonth,tradeDetail,tradeAccount,tradePath,reconciliationReport,reconciliationError,reconciliationLoading,loadReconciliation,
  investmentError:typeof investmentError==='undefined'?{value:''}:investmentError,
  investmentLoading:typeof investmentLoading==='undefined'?{value:false}:investmentLoading,
  navigate,refreshInvestment,refreshStrategy,loadPnlMonth,readTrade,pnlDay,wentOffline,freshness,writable,system,overview,health};`,context)
  if(!context.harness)throw new Error('Workspace setup did not load')
  return context.harness
}

test('actual Workspace setup: overview A to B clears state, navigation loads B, late A cannot publish',async()=>{
  let delayA=false,failB=false
  const old:ReturnType<typeof deferred<Fact>>[]=[]
  const calls:string[]=[]
  const request=async(path:string):Promise<Fact>=>{
    calls.push(path)
    const match=path.match(/\/(A|B)(?:[/?]|$)/), account=match?.[1]??(path.includes('trade-A')?'A':'B')
    if(delayA&&account==='A'){const d=deferred<Fact>();old.push(d);return d.promise}
    if(failB&&account==='B')throw new Error('B request failed')
    return {account_id:account,id:'trade-'+account,serial:calls.length}
  }
  const w=setup(request);w.selectedAccount.value='A';w.pnlMonth.value='2026-01'
  await w.refreshInvestment(false);await w.refreshStrategy();await w.loadPnlMonth();await w.readTrade({id:'trade-A',account_id:'A'})
  assert.equal(viewAccount(w.valuation),'A');assert.equal(viewAccount(w.strategy),'A');assert.equal(w.tradeDetail.value?.account_id,'A')
  delayA=true
  const pending=[w.refreshInvestment(true),w.refreshStrategy(),w.loadPnlMonth(),w.readTrade({id:'trade-A',account_id:'A'})]
  w.selectedAccount.value='B'
  assert.equal(w.valuation.value,null,'switch must clear previous account immediately')
  assert.equal(w.strategy.value,null);assert.equal(w.pool.value,null);assert.equal(w.positionsView.value,null);assert.equal(w.pnlHistory.value,null);assert.equal(w.tradeDetail.value,null)
  assert.equal(w.investmentLoading.value,true)
  w.navigate('投资');await tick()
  assert.equal(viewAccount(w.valuation),'B');assert.equal(viewAccount(w.strategy),'B');assert.equal(w.investmentLoading.value,false)
  await w.refreshStrategy();await w.loadPnlMonth();await w.readTrade({id:'trade-B',account_id:'B'})
  for(const d of old)d.resolve({account_id:'A',id:'trade-A',serial:-1})
  await Promise.all(pending)
  for(const view of [w.valuation,w.strategy,w.pool,w.positionsView,w.pnlHistory,w.tradeDetail])assert.equal(view.value?.account_id,'B')
  assert.equal(w.tradeAccount.value,'B');assert.match(w.tradePath.value,/account_id=B/)
  delayA=false;w.selectedAccount.value='A';await tick();assert.equal(viewAccount(w.valuation),'A')
  failB=true;w.selectedAccount.value='B';await tick();assert.equal(w.valuation.value,null);assert.equal(w.strategy.value,null);assert.match(w.investmentError.value,/B request failed/)
})

test('actual Workspace setup: A-B-A late response with equal account id is discarded',async()=>{
  const old:ReturnType<typeof deferred<Fact>>[]=[]
  let delay=true,serial=0
  const w=setup(async path=>{
    const account=path.match(/\/(A|B)(?:[/?]|$)/)?.[1]??'A'
    if(delay&&account==='A'){const d=deferred<Fact>();old.push(d);return d.promise}
    return {account_id:account,serial:++serial}
  })
  w.selectedAccount.value='A';w.navigate('投资');assert.equal(old.length,2)
  w.selectedAccount.value='B';await tick();assert.equal(viewAccount(w.valuation),'B')
  delay=false;w.selectedAccount.value='A';await tick();const newSerial=w.valuation.value?.serial
  assert.equal(viewAccount(w.valuation),'A')
  for(const d of old)d.resolve({account_id:'A',serial:-1})
  await tick();assert.equal(w.valuation.value?.serial,newSerial);assert.notEqual(newSerial,-1)
})


test('Workspace offline event fences writes and clears prior investment',async()=>{
  const w=setup(async path=>({account_id:path.includes('/A')?'A':'B'}))
  w.health.value={kind:'connected'};w.system.value={writable:true};w.overview.value={ledger_revision:1};w.selectedAccount.value='A'
  await w.refreshInvestment(false);assert.equal(w.writable.value,true)
  w.wentOffline();assert.equal(w.writable.value,false);assert.equal(w.freshness.value,'stale');assert.equal(w.valuation.value,null)
})

test('Workspace refuses different version of month detail',async()=>{
  const row={as_of:'2026-01-06T07:00:00+00:00'}
  const snap={account_id:'A',ledger_revision:5,quote_revision:3,database_uuid:'demo',generation:1,items:[row]}
  const w=setup(async path=>path.includes('/month?')?snap:{...snap,ledger_revision:6,as_of:row.as_of})
  w.selectedAccount.value='A';w.pnlMonth.value='2026-01';await w.loadPnlMonth();await w.pnlDay(row)
  assert.equal(w.valuation.value,null);assert.equal(w.pnlHistory.value,null);assert.match(w.investmentError.value,/版本/)
})


test('Workspace reconciliation preview is bound to the displayed account and exact valuation snapshot',async()=>{
  const snapshot={account_id:'A',as_of:'2026-01-06T08:00:00+00:00',ledger_revision:7,quote_revision:4,database_uuid:'db-a',generation:2}
  let called=''
  const w=setup(async path=>{
    called=path
    return {...snapshot,source:'sqlite_ledger_read_snapshot',quality:'valid',summary:{effective_trade_count:1},checks:[],exceptions:[]}
  })
  w.selectedAccount.value='A';w.valuation.value=snapshot
  await w.loadReconciliation()
  assert.match(called,/\/portfolio\/A\/reconciliation\?/)
  for(const value of ['as_of=2026-01-06T08%3A00%3A00%2B00%3A00','ledger_revision=7','quote_revision=4','database_uuid=db-a','generation=2'])assert.ok(called.includes(value),value)
  assert.equal(w.reconciliationReport.value?.account_id,'A')
  assert.equal(w.reconciliationLoading.value,false)
  w.selectedAccount.value='B'
  assert.equal(w.reconciliationReport.value,null,'account change must clear the prior account reconciliation immediately')
})

test('Workspace rejects reconciliation returned for another snapshot',async()=>{
  const snapshot={account_id:'A',as_of:'2026-01-06T08:00:00+00:00',ledger_revision:7,quote_revision:4,database_uuid:'db-a',generation:2}
  const w=setup(async()=>({...snapshot,ledger_revision:8,source:'sqlite_ledger_read_snapshot'}))
  w.selectedAccount.value='A';w.valuation.value=snapshot
  await w.loadReconciliation()
  assert.equal(w.reconciliationReport.value,null)
  assert.match(w.reconciliationError.value,/同一数据库/)
})
