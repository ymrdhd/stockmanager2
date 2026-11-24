import assert from 'node:assert/strict'
import {test} from 'node:test'
import {ApiError, setGuard, WriteAttempt, type Fact} from '../src/api/client.ts'

/** A durable store shared by several WriteAttempt instances: models a real browser profile. */
function durable():{store:{getItem(k:string):string|null;setItem(k:string,v:string):void;removeItem(k:string):void};data:Map<string,string>}{
  const data=new Map<string,string>()
  return {data,store:{getItem:k=>data.get(k)??null,setItem:(k,v)=>{data.set(k,v)},removeItem:k=>{data.delete(k)}}}
}
const uuidA='11111111-1111-4111-8111-111111111111'
const uuidB='22222222-2222-4222-8222-222222222222'
const asA={identity:[uuidA,1] as [string,number]}
const timeout=()=>new ApiError('timeout',true)

test('an unconfirmed write outlives the browser session, not just the tab',async()=>{
  const env=durable()
  const first=new WriteAttempt(async()=>{throw timeout()},()=> 'durable-key',env.store)
  await assert.rejects(first.start('/trades',{quantity:100}))
  assert.equal(first.pending?.body.request_id,'durable-key')
  // A brand new process reading the same durable store must restore the same request.
  const second=new WriteAttempt(async(_p,_m,b)=>({ok:true,request_id:b?.request_id}),()=> 'must-not-mint',env.store)
  assert.equal(second.requestId,'durable-key')
  assert.equal((await second.retry()).request_id,'durable-key')
  assert.equal(second.pending,null)
  assert.equal(env.data.size,0)
})

test('the record is written before the request is sent',async()=>{
  const env=durable()
  const seen:string[]=[]
  const writer=new WriteAttempt(async(_p,_m,b)=>{seen.push(String(env.data.get('sm2.pending-write')?'persisted':'missing'));return {ok:true,id:b?.request_id}},()=> 'pre-key',env.store)
  await writer.start('/cash',{amount_fen:1})
  assert.deepEqual(seen,['persisted'])
})

test('a session failure keeps the unconfirmed record',async()=>{
  const env=durable()
  const writer=new WriteAttempt(async()=>{throw new ApiError('HTTP 401: 未认证',false,401,'unauthenticated')},()=> 'auth-key',env.store)
  await assert.rejects(writer.start('/trades',{quantity:1}))
  assert.equal(writer.pending?.body.request_id,'auth-key')
  // A later retry rejected on the same grounds must not erase the evidence either.
  await assert.rejects(writer.retry())
  assert.equal(writer.pending?.body.request_id,'auth-key')
})

test('a rejection after an ambiguous attempt keeps the record',async()=>{
  const env=durable()
  let calls=0
  const writer=new WriteAttempt(async()=>{if(++calls===1)throw timeout();throw new ApiError('HTTP 403: 拒绝',false,403,'database_refused')},()=> 'later-key',env.store)
  await assert.rejects(writer.start('/trades',{quantity:1}))
  await assert.rejects(writer.retry())
  assert.equal(writer.pending?.body.request_id,'later-key')
})

test('a first attempt rejected on business grounds is not left pending',async()=>{
  const env=durable()
  const writer=new WriteAttempt(async()=>{throw new ApiError('HTTP 422: 数量非法',false,422,'invalid_input')},()=> 'bad-key',env.store)
  await assert.rejects(writer.start('/trades',{quantity:0}))
  assert.equal(writer.pending,null)
})

test('check() refuses an answer from a different database',async()=>{
  setGuard(()=>asA)
  const env=durable()
  const writer=new WriteAttempt(async(_p,m)=>m==='GET'?{committed:true,database_uuid:uuidB,generation:1}:Promise.reject(timeout()),()=> 'id-key',env.store)
  await assert.rejects(writer.start('/trades',{quantity:1}))
  await assert.rejects(writer.check(),(e:unknown)=>e instanceof ApiError&&e.code==='identity_changed')
  assert.equal(writer.pending?.body.request_id,'id-key')
  writer.discard();setGuard(()=>({}))
})

test('check() refuses an answer from a newer generation of the same database',async()=>{
  setGuard(()=>asA)
  const env=durable()
  const writer=new WriteAttempt(async(_p,m)=>m==='GET'?{committed:true,database_uuid:uuidA,generation:2}:Promise.reject(timeout()),()=> 'gen-key',env.store)
  await assert.rejects(writer.start('/trades',{quantity:1}))
  await assert.rejects(writer.check(),(e:unknown)=>e instanceof ApiError&&e.code==='identity_changed')
  writer.discard();setGuard(()=>({}))
})

test('check() accepts an answer carrying the original identity',async()=>{
  setGuard(()=>asA)
  const env=durable()
  const writer=new WriteAttempt(async(_p,m)=>m==='GET'?{committed:true,database_uuid:uuidA,generation:1,result:{id:'t1'}}:Promise.reject(timeout()),()=> 'ok-key',env.store)
  await assert.rejects(writer.start('/trades',{quantity:1}))
  const state=await writer.check()
  assert.equal(state.committed,true)
  writer.discard();setGuard(()=>({}))
})

test('an unreadable record blocks writes instead of silently starting over',async()=>{
  const env=durable()
  env.data.set('sm2.pending-write','{"path":"/trades"}')
  const writer=new WriteAttempt(async()=>({ok:true}),()=> 'new-key',env.store)
  assert.equal(writer.corrupt,true)
  assert.equal(writer.blocked,true)
  let sent=0
  const blocked=new WriteAttempt(async()=>{sent++;return {ok:true}},()=> 'new-key',env.store)
  await assert.rejects(blocked.start('/trades',{quantity:1}),(e:unknown)=>e instanceof ApiError&&e.code==='pending_corrupt')
  assert.equal(sent,0)
  writer.clearCorrupt()
  assert.equal(writer.corrupt,false)
})

test('a storage failure sends nothing',async()=>{
  const full={getItem:()=>null,setItem:()=>{throw new Error('QuotaExceededError')},removeItem:()=>{}}
  let sent=0
  const writer=new WriteAttempt(async()=>{sent++;return {ok:true}},()=> 'denied-key',full)
  await assert.rejects(writer.start('/trades',{quantity:1}),(e:unknown)=>e instanceof ApiError&&e.code==='storage_unavailable')
  assert.equal(sent,0)
  assert.equal(writer.pending,null)
  assert.match(writer.storageError,/存储不可用/)
})

test('a second tab sees the pending record and cannot post around it',async()=>{
  const env=durable()
  const tabOne=new WriteAttempt(async()=>{throw timeout()},()=> 'tab-key',env.store)
  await assert.rejects(tabOne.start('/trades',{quantity:1}))
  const changes:string[]=[]
  const tabTwo=new WriteAttempt(async()=>({ok:true}),()=> 'other-key',env.store)
  tabTwo.onChange(()=>changes.push('changed'))
  assert.equal(tabTwo.pending?.body.request_id,'tab-key')
  await assert.rejects(tabTwo.start('/trades',{quantity:2}),/尚未核实/)
  assert.equal(tabTwo.pending?.body.request_id,'tab-key')
})

test('abandoning a record tells the operator to reconcile by request ID',async()=>{
  const env=durable()
  const writer=new WriteAttempt(async()=>{throw timeout()},()=> 'drop-key',env.store)
  await assert.rejects(writer.start('/trades',{quantity:1}))
  writer.discard()
  assert.equal(writer.pending,null)
  assert.equal(env.data.size,0)
})

test('a retry is never sent to a different database',async()=>{
  setGuard(()=>asA)
  const env=durable()
  let sent=0
  const writer=new WriteAttempt(async(_p,m)=>{if(m==='GET')return {committed:false};sent++;throw timeout()},()=> 'cross-key',env.store)
  await assert.rejects(writer.start('/trades',{quantity:1}))
  setGuard(()=>({identity:[uuidB,1]}))
  await assert.rejects(writer.retry(),(e:unknown)=>e instanceof ApiError&&e.code==='identity_changed')
  assert.equal(sent,1)
  assert.equal(writer.pending?.body.request_id,'cross-key')
  writer.discard();setGuard(()=>({}))
})

test('the exact payload is resent byte for byte',async()=>{
  const env=durable()
  const seen:Fact[]=[]
  let first=true
  const writer=new WriteAttempt(async(_p,_m,b)=>{seen.push(structuredClone(b??{}));if(first){first=false;throw timeout()}return {ok:true}},()=> 'same-key',env.store)
  const body={account_id:'a',quantity:300,price:'1.2345',note:'真实成交'}
  await assert.rejects(writer.start('/trades',body))
  body.quantity=999
  await writer.retry()
  assert.deepEqual(seen[0],seen[1])
  assert.equal(seen[1].quantity,300)
})
