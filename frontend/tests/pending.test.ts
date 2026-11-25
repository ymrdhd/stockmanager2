import assert from 'node:assert/strict'
import {test} from 'node:test'
import {api, ApiError, setGuard, WriteAttempt, type Fact, type Operation, type WriteMutex} from '../src/api/client.ts'

/** A durable store shared by several WriteAttempt instances: models a real browser profile. */
function durable():{store:{getItem(k:string):string|null;setItem(k:string,v:string):void;removeItem(k:string):void};data:Map<string,string>}{
  const data=new Map<string,string>()
  return {data,store:{getItem:k=>data.get(k)??null,setItem:(k,v)=>{data.set(k,v)},removeItem:k=>{data.delete(k)}}}
}
const uuidA='11111111-1111-4111-8111-111111111111'
const uuidB='22222222-2222-4222-8222-222222222222'
const asA={identity:[uuidA,1] as [string,number]}
const timeout=()=>new ApiError('timeout',true)

/**
 * A stand-in for `navigator.locks`: one queue per lock name, shared by every
 * instance handed the same object, which is exactly what two tabs see.
 */
function sharedLocks():WriteMutex&{names:string[]}{
  const queues=new Map<string,Promise<unknown>>()
  const names:string[]=[]
  return {
    names,
    request<T>(name:string,_options:{mode:'exclusive'},callback:()=>Promise<T>):Promise<T>{
      names.push(name)
      const previous=queues.get(name)??Promise.resolve()
      const run=previous.then(callback,callback)
      queues.set(name,run.then(()=>undefined,()=>undefined))
      return run
    },
  }
}

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
  setGuard(()=>({}))
})

test('check() refuses an answer from a newer generation of the same database',async()=>{
  setGuard(()=>asA)
  const env=durable()
  const writer=new WriteAttempt(async(_p,m)=>m==='GET'?{committed:true,database_uuid:uuidA,generation:2}:Promise.reject(timeout()),()=> 'gen-key',env.store)
  await assert.rejects(writer.start('/trades',{quantity:1}))
  await assert.rejects(writer.check(),(e:unknown)=>e instanceof ApiError&&e.code==='identity_changed')
  setGuard(()=>({}))
})

test('check() accepts an answer carrying the original identity',async()=>{
  setGuard(()=>asA)
  const env=durable()
  const writer=new WriteAttempt(async(_p,m)=>m==='GET'?{committed:true,database_uuid:uuidA,generation:1,result:{id:'t1'}}:Promise.reject(timeout()),()=> 'ok-key',env.store)
  await assert.rejects(writer.start('/trades',{quantity:1}))
  const state=await writer.check()
  assert.equal(state.committed,true)
  setGuard(()=>({}))
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
  assert.equal(await writer.clearCorrupt(),true)
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
  const writer=new WriteAttempt(async()=>{throw timeout()},()=> 'drop-key',env.store,sharedLocks())
  await assert.rejects(writer.start('/trades',{quantity:1}))
  assert.equal(await writer.discard(),true)
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
  await assert.rejects(writer.discard(),(e:unknown)=>e instanceof ApiError&&e.code==='identity_changed')
  assert.equal(writer.pending?.body.request_id,'cross-key')
  setGuard(()=>({}))
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

// -- ISS-025: the check and the write must happen inside one shared lock ----------
test('two tabs cannot both pass the check: only one write is persisted and sent',async()=>{
  const env=durable()
  const locks=sharedLocks()
  const sent:string[]=[]
  const send:typeof api=async(_p,_m,b)=>{sent.push(String(b?.request_id));throw timeout()}
  const firstKey=()=> 'tab-one-key', secondKey=()=> 'tab-two-key'
  const tabOne=new WriteAttempt(send,firstKey,env.store,locks)
  const tabTwo=new WriteAttempt(send,secondKey,env.store,locks)
  // Both tabs saw no record when they were constructed; both now try to post.
  assert.equal(tabOne.pending,null);assert.equal(tabTwo.pending,null)
  const results=await Promise.allSettled([tabOne.start('/trades',{quantity:1}),tabTwo.start('/trades',{quantity:2})])
  assert.equal(results[0].status,'rejected')
  assert.equal(results[1].status,'rejected')
  const codes=results.map(r=>(r as PromiseRejectedResult).reason).filter((e:unknown)=>e instanceof ApiError).map((e)=>((e as ApiError).code))
  // One side posts (its error is the simulated timeout), the other is refused locally.
  assert.deepEqual(codes.sort(),['','pending_exists'],'the loser must be refused with pending_exists, never with a network error')
  // Exactly one business request left the browser, under one single request ID.
  assert.equal(sent.length,1)
  const stored=JSON.parse(env.data.get('sm2.pending-write')??'{}') as Operation
  assert.equal(stored.body.request_id,sent[0])
  assert.equal([...sent].filter(id=>id===stored.body.request_id).length,1)
  // All database identities share the same origin-wide pending-record lock.
  assert.ok(locks.names.length>=2&&new Set(locks.names).size===1&&locks.names.every(n=>n.startsWith('sm2.pending-write')))
})

test('a lost response keeps the record and the retry posts it exactly once',async()=>{
  const env=durable()
  const locks=sharedLocks()
  let calls=0
  const attempts:string[]=[]
  const send:typeof api=async(_p,_m,b)=>{
    attempts.push(String(b?.request_id))
    if(++calls===1)throw timeout()
    return {ok:true,request_id:b?.request_id}
  }
  const writer=new WriteAttempt(send,()=> 'once-key',env.store,locks)
  await assert.rejects(writer.start('/trades',{quantity:1}))
  assert.equal(writer.pending?.body.request_id,'once-key')
  // While that record is unconfirmed no second write may start.
  const second=new WriteAttempt(send,()=> 'other-key',env.store,locks)
  await assert.rejects(second.start('/trades',{quantity:2}),(e:unknown)=>e instanceof ApiError&&e.code==='pending_exists')
  assert.equal(calls,1)
  const result=await writer.retry()
  assert.equal(result.request_id,'once-key')
  assert.deepEqual(attempts,['once-key','once-key'],'the retry must reuse the original request ID, not mint a new one')
  assert.equal(new Set(attempts).size,1)
  assert.equal(writer.pending,null)
  assert.equal(env.data.size,0)
})

test('an unparseable record is reported as corrupt, blocks writes, and can be cleared',async()=>{
  const env=durable()
  const locks=sharedLocks()
  env.data.set('sm2.pending-write','{"path":"/trades"')
  let sent=0
  const writer=new WriteAttempt(async()=>{sent++;return {ok:true}},()=> 'never-key',env.store,locks)
  assert.equal(writer.corrupt,true)
  assert.equal(writer.blocked,true)
  await assert.rejects(writer.start('/trades',{quantity:1}),(e:unknown)=>e instanceof ApiError&&e.code==='pending_corrupt')
  assert.equal(sent,0)
  assert.equal(env.data.has('sm2.pending-write'),true,'an unreadable record is never deleted silently')
  assert.equal(await writer.clearCorrupt(),true)
  assert.equal(writer.corrupt,false)
  assert.equal(env.data.size,0)
  // Writing works again only after the operator reconciled and cleared it.
  await writer.start('/trades',{quantity:1})
  assert.equal(sent,1)
})

test('without navigator.locks a write is refused instead of racing',async()=>{
  const env=durable()
  let sent=0
  const writer=new WriteAttempt(async()=>{sent++;return {ok:true}},()=> 'no-lock-key',env.store,null)
  await assert.rejects(writer.start('/trades',{quantity:1}),(e:unknown)=>e instanceof ApiError&&e.code==='mutex_unavailable')
  assert.equal(sent,0)
  assert.equal(env.data.size,0,'nothing may be persisted when the write is refused')
})
