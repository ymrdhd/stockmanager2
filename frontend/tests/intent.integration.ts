/** Real API/SQLite integration driven by Python TestClient over JSON lines.
 * Transport and mutex are simulated; this is not a browser test. */
import assert from 'node:assert/strict'
import {createInterface} from 'node:readline'
import {api, ApiError, WriteAttempt, setGuard, type Fact, type WriteMutex} from '../src/api/client.ts'
const input=createInterface({input:process.stdin})
const answers=input[Symbol.asyncIterator]()
const timer=setTimeout(()=>{console.error('integration transport timeout');process.exit(1)},25000)
const [account,instrument,other,uuid]=process.argv.slice(2)
setGuard(()=>({identity:[uuid,1]}))
const data=new Map<string,string>()
const storage={getItem:(k:string)=>data.get(k)??null,setItem:(k:string,v:string)=>{data.set(k,v)},removeItem:(k:string)=>{data.delete(k)}}
let queue:Promise<unknown>=Promise.resolve()
const locks:WriteMutex={request<T>(_name:string,_opts:{mode:'exclusive'},work:()=>Promise<T>):Promise<T>{
  const next=queue.then(work,work);queue=next.then(()=>undefined,()=>undefined);return next
}}
async function exchange(message:Fact):Promise<Fact>{
  process.stdout.write(JSON.stringify(message)+'\n')
  const next=await answers.next();if(next.done)throw new Error('bridge closed')
  return JSON.parse(next.value) as Fact
}
let lose=false, posts=0
const transport:typeof fetch=async(path,init)=>{
  const result=await exchange({kind:'http',path:String(path),method:init?.method,body:init?.body?JSON.parse(String(init.body)):null,headers:init?.headers})
  if(init?.method==='POST'){posts++;if(lose){lose=false;throw new TypeError('response lost after commit')}}
  return new Response(JSON.stringify(result.body),{status:Number(result.status)})
}
const send:typeof api=(path,method='GET',body)=>api(path,method,body,transport)
const body={account_id:account,instrument_id:instrument,side:'buy',quantity:100,price:'3.4567',fees_fen:5,executed_at:'2026-01-05T09:30:00+08:00',trading_day:'2026-01-05'}
try{
  const a=new WriteAttempt(send,()=> 'intent-first',storage,locks),b=new WriteAttempt(send,()=> 'intent-duplicate',storage,locks)
  const slot=a.beginIntent(),otherSlot=b.beginIntent();assert.equal(slot,otherSlot)
  const both=await Promise.allSettled([a.start('/trades',body,'POST',slot),b.start('/trades',body,'POST',otherSlot)])
  assert.equal(both[0].status,'fulfilled');assert.equal(both[1].status,'rejected');assert.equal(posts,1)
  await exchange({kind:'checkpoint',phase:'first'})
  const c=new WriteAttempt(send,()=> 'intent-second',storage,locks)
  await c.start('/trades',body,'POST',c.beginIntent());assert.equal(posts,2)
  await exchange({kind:'checkpoint',phase:'second'})
  const d=new WriteAttempt(send,()=> 'intent-other',storage,locks)
  lose=true;const lostSlot=d.beginIntent()
  await assert.rejects(d.start('/trades',{...body,account_id:other},'POST',lostSlot),(e:unknown)=>e instanceof ApiError&&e.uncertain)
  const reopen=new WriteAttempt(send,()=> 'must-not-mint',storage,locks)
  assert.equal(reopen.requestId,'intent-other')
  const state=await reopen.check('intent-other');assert.equal(state.committed,true)
  await reopen.retry('intent-other') // same ID: real API replay, not another trade
  await assert.rejects(reopen.start('/trades',{...body,account_id:other},'POST',lostSlot),(e:unknown)=>e instanceof ApiError&&e.code==='intent_stale')
  assert.equal(posts,4)
  await exchange({kind:'checkpoint',phase:'final'})
}finally{clearTimeout(timer);input.close();process.stdin.pause()}
