const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function harness(){
 let id=0;const timers=new Map(),requests=[],responses=[];
 const context=vm.createContext({Map,Set,Date,JSON,encodeURIComponent,console,
  document:{hidden:false,addEventListener(){}},$:()=>null,esc:String,
  state:{page:'app',selectedApp:'bilibili',snapshot:{apps:[{app_id:'bilibili',admin_tools:['account']}]}},
  setTimeout(fn,ms){const key=++id;timers.set(key,{fn,ms});return key;},clearTimeout(key){timers.delete(key);},
  refresh:async()=>{},api:async(url,options)=>{requests.push(JSON.parse(options.body));const next=responses.shift();if(next instanceof Error)throw next;return next;}
 });
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../../src/astcho_world/web/app-controls.js'),'utf8'),context);
 return {context,timers,requests,responses,run:code=>vm.runInContext(code,context),
  async fire(){const [key,timer]=timers.entries().next().value;timers.delete(key);await timer.fn();await new Promise(r=>setImmediate(r));}
 };
}
test('live refresh does not keep postponing the same QR poll; stable login ID is sent',async()=>{
 const h=harness();h.run("controlResults.set('bilibili',{status:'scan_required',login_id:'one'});syncAccountPolling()");
 const timer=[...h.timers.keys()][0];for(let i=0;i<10;i++)h.run('syncAccountPolling()');
 assert.deepEqual([...h.timers.keys()],[timer]);
 h.responses.push({status:'confirmation_required',login_id:'one'});await h.fire();
 assert.deepEqual(h.requests[0].arguments,{action:'check',login_id:'one'});
 assert.equal(h.run("controlResults.get('bilibili').status"),'confirmation_required');
 assert.equal(h.timers.size,1);
 h.responses.push({status:'logged_in',logged_in:true,name:'合成账号'});await h.fire();
 assert.equal(h.timers.size,0);
});
test('three transient failures retain the QR and stop auto retry, manual retry can recover',async()=>{
 const h=harness();h.run("controlResults.set('bilibili',{status:'scan_required',login_id:'one',qrcode_image:'data:image/png;base64,abc'});syncAccountPolling()");
 for(let i=0;i<3;i++){h.responses.push(new Error('temporary'));await h.fire();}
 assert.equal(h.requests.length,3);assert.equal(h.timers.size,0);
 assert.equal(h.run("controlResults.get('bilibili').qrcode_image"),'data:image/png;base64,abc');
 h.responses.push({status:'logged_in',logged_in:true});await h.run("accountAction('bilibili','account','check')");
 assert.equal(h.run("controlErrors.get('bilibili')"),0);
});
test('leaving the app or hiding the page stops checks; return resumes a pending flow',()=>{
 const h=harness();h.run("controlResults.set('bilibili',{status:'scan_required'});syncAccountPolling()");
 assert.equal(h.timers.size,1);h.run("state.page='overview';syncAccountPolling()");assert.equal(h.timers.size,0);
 h.run("state.page='app';syncAccountPolling()");assert.equal(h.timers.size,1);
 h.run('document.hidden=true;syncAccountPolling()');assert.equal(h.timers.size,0);
 h.run('document.hidden=false;syncAccountPolling()');assert.equal(h.timers.size,1);
});
test('status recovers pending QR on page load, expiry and cancellation stop checks',async()=>{
 const h=harness();h.run('syncAccountPolling()');h.responses.push({status:'scan_required',login_id:'restored'});await h.fire();
 assert.deepEqual(h.requests[0].arguments,{action:'status'});assert.equal(h.timers.size,1);
 h.responses.push({status:'expired',login_id:'restored'});await h.fire();assert.equal(h.timers.size,0);
 h.responses.push({status:'cancelled',logged_in:false});await h.run("accountAction('bilibili','account','cancel')");
 assert.equal(h.timers.size,0);
});
test('in-flight automatic check cannot be duplicated by manual click',async()=>{
 const h=harness();let finish;h.context.api=()=>new Promise(resolve=>finish=resolve);
 const first=h.run("accountAction('bilibili','account','check')");await h.run("accountAction('bilibili','account','check')");
 assert.equal(h.run("controlBusy.has('bilibili')"),true);finish({status:'not_logged_in'});await first;
 assert.equal(h.run("controlBusy.has('bilibili')"),false);
});
