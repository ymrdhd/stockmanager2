import assert from 'node:assert/strict'
import {test} from 'node:test'
import {AccountScope} from '../src/api/accountScope.ts'
import type {Fact} from '../src/api/client.ts'
function deferred<T>(){let resolve!:(value:T)=>void,reject!:(reason:unknown)=>void;const promise=new Promise<T>((r,j)=>{resolve=r;reject=j});return {promise,resolve,reject}}

test('09D-003 scope clears all projections synchronously and rejects late A, including A-B-A',async()=>{
  const scope=new AccountScope()
  let view:Fact|null=null, evidence:Fact|null=null, pnl:Fact|null=null, loading=false, error=''
  const select=(id:string)=>scope.reset(id,()=>{view=null;evidence=null;pnl=null;loading=true;error=''})
  async function load(response:Promise<Fact[]>){
    const ticket=scope.capture()
    try{const [v,e,p]=await response;if(scope.accepts(ticket,v,e,p)){view=v;evidence=e;pnl=p}}
    catch(e){if(scope.current(ticket)){view=null;evidence=null;pnl=null;error=String(e)}}
    finally{if(scope.current(ticket))loading=false}
  }
  const data=(account_id:string,serial:number)=>Array.from({length:3},()=>({account_id,serial}))
  select('A');await load(Promise.resolve(data('A',0)))
  assert.ok(view);const a=deferred<Fact[]>(),aLoad=load(a.promise)
  select('B');assert.deepEqual([view,evidence,pnl,loading],[null,null,null,true])
  const b=deferred<Fact[]>(),bLoad=load(b.promise)
  b.resolve(data('B',1));await bLoad
  a.resolve(data('A',2));await aLoad
  assert.deepEqual([view,evidence,pnl],data('B',1))
  const oldB=deferred<Fact[]>(),oldBLoad=load(oldB.promise)
  select('A');const oldA=deferred<Fact[]>(),oldALoad=load(oldA.promise)
  select('B');select('A');await load(Promise.resolve(data('A',3)))
  oldB.resolve(data('B',4));oldA.resolve(data('A',5));await Promise.all([oldBLoad,oldALoad])
  assert.deepEqual([view,evidence,pnl],data('A',3));assert.equal(loading,false)
  select('B');await load(Promise.reject(new Error('B failed')))
  assert.deepEqual([view,evidence,pnl,loading],[null,null,null,false]);assert.match(error,/B failed/)
})

test('current response must match requested account for every projection',()=>{
  const scope=new AccountScope();scope.reset('B',()=>{});const ticket=scope.capture()
  assert.throws(()=>scope.accepts(ticket,{account_id:'B'},{account_id:'A'}),/不一致/)
  assert.throws(()=>scope.accepts(ticket,{}),/不一致/)
  assert.equal(scope.accepts(ticket,{account_id:'B'},{account_id:'B'}),true)
  scope.reset('A',()=>{});assert.equal(scope.accepts(ticket,{account_id:'B'}),false)
})
