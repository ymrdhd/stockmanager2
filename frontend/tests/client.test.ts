import assert from 'node:assert/strict'
import {test} from 'node:test'
import {api, ApiError, setGuard, WriteAttempt, page, type Fact} from '../src/api/client.ts'
import {fen,money,shanghaiTime,yuan} from '../src/ui.ts'
import {dividendCorrectionForm,dividendForm,openingForm,quoteResolutionForm,reconciliationForm,tradeForm,transferForm,accountForm} from '../src/forms.ts'

test('integer cents and unknown values are displayed without float accounting',()=>{
  assert.equal(fen('1990.01'),199001);assert.equal(fen('-10.05'),-1005);assert.throws(()=>fen('0.001'))
  assert.equal(money(null),'不可用');assert.equal(money(0),'¥0.00');assert.equal(money('9007199254740993'),'¥90,071,992,547,409.93')
  assert.equal(yuan('9007199254740993'),'90071992547409.93');assert.throws(()=>fen('90071992547409.93'))
  assert.equal(shanghaiTime('2026-01-06T09:30'),'2026-01-06T09:30:00+08:00')
})
test('error details and connection ambiguity are surfaced',async()=>{
  const rejected:typeof fetch=async()=>new Response(JSON.stringify({detail:{code:'conflict',message:'balance changed'}}),{status:409})
  await assert.rejects(api('/finance/balances','POST',{},rejected),(e:unknown)=>e instanceof ApiError&&!e.uncertain&&e.message.includes('balance changed'))
  const offline:typeof fetch=async()=>{throw new TypeError('offline')}
  await assert.rejects(api('/trades','POST',{},offline),(e:unknown)=>e instanceof ApiError&&e.uncertain)
  await assert.rejects(api('/trades','GET',undefined,offline),(e:unknown)=>e instanceof ApiError&&!e.uncertain)
})
test('retry of ambiguous write preserves the request ID and exact payload',async()=>{
  const sent:Fact[]=[];let calls=0
  const send:typeof api=async(_p,_m,b)=>{sent.push(structuredClone(b??{}));if(++calls===1)throw new ApiError('timeout',true);return {id:'trade'}}
  const writer=new WriteAttempt(send,()=> 'fixed-key');const body={amount_fen:100}
  await assert.rejects(writer.start('/cash',body));body.amount_fen=200
  await assert.rejects(writer.start('/cash',body),/尚未核实/)
  assert.equal((await writer.retry()).id,'trade');assert.deepEqual(sent,[{amount_fen:100,request_id:'fixed-key'},{amount_fen:100,request_id:'fixed-key'}]);assert.equal(writer.pending,null)
})
test('known rejection does not leave a pending request',async()=>{
  const writer=new WriteAttempt(async()=>{throw new ApiError('conflict',false,409)},()=> 'key')
  await assert.rejects(writer.start('/trades',{}));assert.equal(writer.pending,null)
})
test('opening, dividend and quote resolution forms match the server contracts',()=>{
  const accounts=[{id:'a',name:'证券',kind:'securities'}],instruments=[{id:'i',market:'SH',code:'510300'}]
  const opening=openingForm(accounts,instruments).build({account_id:'a',instrument_id:'i',quantity:'300',cost:'3600.50',fees:'1.20',occurred_at:'2026-01-05T09:30',note:'期初'})
  assert.deepEqual(opening,{account_id:'a',instrument_id:'i',quantity:300,cost_fen:360050,fees_fen:120,occurred_at:'2026-01-05T09:30:00+08:00',note:'期初'})
  assert.throws(()=>openingForm(accounts,instruments).build({account_id:'a',instrument_id:'i',quantity:'1.5',cost:'1',fees:'0',occurred_at:'2026-01-05T09:30',note:'n'}))
  const dividend=dividendForm(accounts,instruments).build({account_id:'a',instrument_id:'i',amount:'88.88',occurred_at:'2026-01-06T10:00',note:'到账'})
  assert.equal(dividend.kind,'dividend');assert.equal(dividend.amount_fen,8888)
  const correction=dividendCorrectionForm({id:'event 1',amount_fen:8888,note:'原'}).build({amount:'99.50',occurred_at:'2026-01-06T11:00',note:'更正'})
  assert.deepEqual(correction,{amount_fen:9950,occurred_at:'2026-01-06T11:00:00+08:00',note:'更正'})
  const resolution=quoteResolutionForm({id:'q1',price:'4.1000',source:'交易所',trading_day:'2026-01-06',observed_at:'2026-01-06T15:00:00+08:00'},7).build({reason:'以交易所公告为准'})
  assert.deepEqual(resolution,{selected_quote_id:'q1',expected_quote_revision:7,reason:'以交易所公告为准'})
})
test('an unconfirmed write survives a page reload with the same request ID',async()=>{
  const reloaded=new WriteAttempt(async()=>{throw new ApiError('timeout',true)},()=> 'reload-key')
  await assert.rejects(reloaded.start('/cash',{amount_fen:1}))
  // A fresh module instance models the browser reload: pending must be restored, not regenerated.
  const after=new WriteAttempt(async(_p,_m,b)=>({id:'restored',request_id:b?.request_id}),()=> 'should-never-be-used')
  assert.equal(after.requestId,'reload-key')
  const result=await after.retry()
  assert.equal(result.id,'restored');assert.equal(result.request_id,'reload-key');assert.equal(after.pending,null)
})
test('a changed database identity blocks retrying the previous request',async()=>{
  setGuard(()=>({identity:['00000000-0000-4000-8000-000000000001',1]}))
  const writer=new WriteAttempt(async()=>{throw new ApiError('timeout',true)},()=> 'identity-key')
  await assert.rejects(writer.start('/cash',{amount_fen:1}))
  setGuard(()=>({identity:['00000000-0000-4000-8000-000000000002',1]}))
  await assert.rejects(writer.retry(),(e:unknown)=>e instanceof ApiError&&e.code==='identity_changed')
  assert.notEqual(writer.pending,null)
  writer.discard();setGuard(()=>({}))
})
test('a request already in the ledger is reported instead of being posted again',async()=>{
  let posted=0
  const writer=new WriteAttempt(async(_p,m)=>{if(m==='GET')return {committed:true};posted++;throw new ApiError('timeout',true)},()=> 'committed-key')
  await assert.rejects(writer.start('/cash',{amount_fen:1}))
  const state=await writer.check()
  assert.equal(state.committed,true)
  assert.equal(posted,1)
  // The page must offer the committed result, not a second POST of the same request.
  writer.discard();assert.equal(writer.pending,null)
})
test('list boundary validates revision and pagination',()=>{
  assert.deepEqual(page({items:[{id:'one'}],ledger_revision:1,total:1,limit:50,offset:0}).items,[{id:'one'}])
  assert.throws(()=>page({items:[],ledger_revision:'1',total:0,limit:50,offset:0}))
  assert.throws(()=>page({items:[null],ledger_revision:1,total:1,limit:50,offset:0}))
})
test('manual transfer uses explicit inclusion and latest balance IDs',()=>{
  const accounts=[{id:'bank',name:'银行',kind:'bank',balance_id:'new'},{id:'broker',name:'证券',kind:'securities'}]
  const form=transferForm(accounts)
  const values={from_account_id:'bank',to_account_id:'broker',amount:'2000',occurred_at:'2026-01-06T10:00',note:'confirmed',from_included:'false',to_included:''}
  const r=form.build(values);assert.equal(r.amount_fen,200000);assert.equal(r.from_included,false);assert.equal(r.from_balance_id,'new');assert.equal(r.to_balance_id,null)
  assert.throws(()=>form.build({...values,from_included:''}))
  const old={id:'old',from_account_id:'bank',to_account_id:'broker',from_balance_id:'before'}
  assert.equal(transferForm(accounts,old).build(values).from_balance_id,'before')
})
test('manual reconciliation sends actual confirmed balance instead of adjusting by transfer amount',()=>{
  const form=reconciliationForm([{id:'bank',name:'银行',kind:'bank',balance_id:'b',balance_fen:800000},{id:'broker',kind:'securities'}],{id:'t',from_account_id:'bank',to_account_id:'broker'})
  const values={replacement:'','balance:bank':'10000',occurred_at:'2026-01-06T11:00',note:'statement confirmed',confirmed:'true'}
  assert.deepEqual(form.build(values),{corrected_amount_fen:null,occurred_at:'2026-01-06T11:00:00+08:00',note:'statement confirmed',confirmed:true,balances:[{account_id:'bank',expected_balance_id:'b',confirmed_fen:1000000}]})
  assert.throws(()=>form.build({...values,confirmed:'false'}))
})
test('trade form keeps exact price and explicit specified batch, account creation needs confirmed time',()=>{
  const form=tradeForm([{id:'a',kind:'securities'}],[{id:'i',market:'SH',code:'510300'}],[{id:'lot',remaining_qty:'100',lot_role:'ordinary'}])
  const values={account_id:'a',instrument_id:'i',side:'sell',quantity:'50',price:'12.3456',fees:'',executed_at:'2026-01-06T10:00',external_id:'',note:'',lot_role:'ordinary',strategy_id:'',signal_date:'',frozen_atr:'',strategy_deviation:'false','lot:lot':'50'}
  const body=form.build(values);assert.equal(body.price,'12.3456');assert.equal(body.fees_fen,null);assert.deepEqual(body.allocations,{lot:50});assert.equal(body.trading_day,'2026-01-06')
  assert.throws(()=>form.build({...values,side:'buy'}));assert.throws(()=>accountForm().build({name:'new',kind:'securities',effective_at:'',included:'true'}))
})
