const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
function harness(count=41){
 const rows=Array.from({length:count},(_,i)=>({seq:count-i}));
 const requests=[];
 const context=vm.createContext({URLSearchParams,document:{addEventListener(){},querySelectorAll(){return [];}},state:{page:'activity'},esc:String,$:()=>({classList:{add(){}},scrollIntoView(){}}),render(){},api:async url=>{
  const q=new URL('http://fixture'+url).searchParams;requests.push(q);
  return {items:rows.filter(r=>!q.has('before')||r.seq<Number(q.get('before'))).slice(0,Number(q.get('limit')))};
 }});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../../src/astcho_world/web/pagination.js'),'utf8'),context);
 vm.runInContext("var shown=[];async function loadEvents(){const data=await loadListPage('activity','/api/events');if(!data)return false;shown=data.items;return true;}",context);
 return {context,rows,requests,run:code=>vm.runInContext(code,context),ids:()=>JSON.parse(vm.runInContext('JSON.stringify(shown.map(r=>r.seq))',context))};
}
test('pages replace rows, reach the exact end, go back and return to latest',async()=>{
 const h=harness();await h.run('loadEvents()');const first=h.ids();assert.equal(first.length,20);
 await h.run("turnListPage('activity','next')");const second=h.ids();assert.equal(second.length,20);
 assert.equal(first.some(id=>second.includes(id)),false);
 await h.run("turnListPage('activity','next')");assert.deepEqual(h.ids(),[1]);assert.equal(h.run("getListPage('activity').next"),null);
 await h.run("turnListPage('activity','prev')");assert.deepEqual(h.ids(),second);
 h.rows.unshift({seq:42});await h.run('loadEvents()');assert.deepEqual(h.ids(),second);assert.equal(h.run('historicalPage()'),true);
 await h.run("turnListPage('activity','first')");assert.equal(h.ids()[0],42);assert.equal(h.ids().length,20);assert.equal(h.run('historicalPage()'),false);
 assert.equal(h.requests[0].get('limit'),'21');assert.equal(h.requests[1].get('before'),String(first.at(-1)));
});
test('an exact multiple has no empty extra page',async()=>{
 const h=harness(40);await h.run('loadEvents()');await h.run("turnListPage('activity','next')");
 assert.equal(h.ids().length,20);assert.equal(h.run("getListPage('activity').next"),null);
 await h.run("turnListPage('activity','next')");assert.equal(h.run("getListPage('activity').index"),1);
});
test('a failed page restores the previous page and can be retried',async()=>{
 const h=harness();await h.run('loadEvents()');const first=h.ids(),api=h.context.api;
 h.context.api=async()=>{throw new Error('synthetic failure');};await h.run("turnListPage('activity','next')");
 assert.equal(h.run("getListPage('activity').index"),0);assert.deepEqual(h.ids(),first);assert.equal(h.run("getListPage('activity').busy"),false);
 h.context.api=api;await h.run("turnListPage('activity','next')");assert.equal(h.run("getListPage('activity').index"),1);
});
test('a request issued before a filter reset cannot overwrite the new page',async()=>{
 const h=harness();let resolve;h.context.api=()=>new Promise(r=>resolve=r);
 const old=h.run("loadListPage('activity','/api/events',{q:'old'})");h.run("resetListPage('activity')");
 h.context.api=async()=>({items:[{seq:99}]});const fresh=await h.run("loadListPage('activity','/api/events',{q:'new'})");
 resolve({items:Array.from({length:21},(_,i)=>({seq:50-i}))});assert.equal(await old,null);
 assert.equal(fresh.items[0].seq,99);assert.equal(h.run("getListPage('activity').count"),1);assert.equal(h.run("getListPage('activity').next"),null);
});
test('a page click supersedes an in-flight live refresh instead of being ignored',async()=>{
 const h=harness();await h.run('loadEvents()');const first=h.ids();
 let finishBackground;const realApi=h.context.api;
 h.context.api=()=>new Promise(resolve=>finishBackground=resolve);
 const background=h.run('loadEvents()');assert.equal(h.run("getListPage('activity').busy"),true);
 h.context.api=realApi;await h.run("turnListPage('activity','next')");
 assert.equal(h.run("getListPage('activity').index"),1);assert.equal(h.ids().some(id=>first.includes(id)),false);
 const second=h.ids();finishBackground({items:h.rows.slice(0,21)});assert.equal(await background,false);
 assert.deepEqual(h.ids(),second);assert.equal(h.run("getListPage('activity').turning"),false);
});
