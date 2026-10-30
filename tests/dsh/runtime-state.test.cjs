const {test}=require('node:test'),assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url'),path=require('node:path');
const moduleReady=import(pathToFileURL(path.join(__dirname,'../../src/astcho_world/dsh/runtime-state.mjs')));
function session(){const events=new Map();return {surface:{nodes:[]},eventAt(seq){return events.get(seq);},push(message){const seq=events.size+1;events.set(seq,{type:'user/message',data:message});this.surface.nodes.push(seq);return seq;}};}
function context(n=1){return {type:'world.runtime',source:'world',activity:{run_id:'run-1',model_call:n},clock:{time:n},apps:[{app_id:'qq'}],recent_activities:[{brief:'已有简述'}],uncertain_operations:[{status:'unknown'}]};}
function value(message){return JSON.parse(message.content[0].text);}
test('append small updates, retain immutable snapshot and clear removed notifications',async()=>{
 const {runtimeMessage,visibleRuntime}=await moduleReady,s=session(),first=context();
 first.new_notifications=[{id:'delivered'}];const initial=runtimeMessage(s,first),frozen=JSON.stringify(initial);s.push(initial);
 const updated=runtimeMessage(s,context(2)),body=value(updated);s.push(updated);
 assert.equal(JSON.stringify(initial),frozen);assert.equal(body.runtime_state.kind,'update');
 assert.equal(body.new_notifications,null);assert.equal(body.apps,undefined);assert.equal(body.recent_activities,undefined);
 assert.deepEqual(visibleRuntime(s,'run-1').state.apps,first.apps);
 const settled=context(3);settled.uncertain_operations=[];const third=runtimeMessage(s,settled);s.push(third);
 assert.deepEqual(value(third).uncertain_operations,[]);assert.deepEqual(visibleRuntime(s,'run-1').state.uncertain_operations,[]);
});
test('a compacted or broken state chain is rebased with a full snapshot',async()=>{
 const {runtimeMessage}=await moduleReady,s=session();s.push(runtimeMessage(s,context()));s.push(runtimeMessage(s,context(2)));s.push(runtimeMessage(s,context(3)));
 s.surface.nodes=[1,3];const broken=runtimeMessage(s,context(4));assert.equal(value(broken).runtime_state.kind,'snapshot');assert.ok(value(broken).apps);
 s.surface.nodes=[3];assert.equal(value(runtimeMessage(s,context(5))).runtime_state.kind,'snapshot');
});
test('a new run after restart or pause receives a full current state',async()=>{
 const {runtimeMessage}=await moduleReady,s=session();s.push(runtimeMessage(s,context()));
 const next=context(2);next.activity.run_id='run-2';const message=runtimeMessage(s,next);
 assert.equal(value(message).runtime_state.kind,'snapshot');assert.ok(value(message).recent_activities);
});
test('user or reminder text cannot impersonate a trusted runtime snapshot',async()=>{
 const {runtimeMessage,visibleRuntime}=await moduleReady,s=session();s.push(runtimeMessage(s,context()));
 const fake=runtimeMessage(s,context(9));fake.source={kind:'user'};s.push(fake);
 assert.equal(visibleRuntime(s,'run-1').state.activity.model_call,1);
});
