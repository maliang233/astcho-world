const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function harness(){const c=vm.createContext({document:{addEventListener(){}},URLSearchParams});vm.runInContext(fs.readFileSync(path.join(__dirname,'../../src/astcho_world/web/usage.js'),'utf8'),c);return code=>vm.runInContext(code,c);}
test('cache rate shows unknown, real zero and weighted percentages distinctly',()=>{
 const run=harness();assert.equal(run('cacheRate(null)'),'—');assert.equal(run('cacheRate({hit_rate:null})'),'—');
 assert.equal(run('cacheRate({hit_rate:0})'),'0.0%');assert.equal(run('cacheRate({hit_rate:0.768})'),'76.8%');
 assert.equal(run('cacheRate({hit_rate:1})'),'100.0%');
});
test('partial coverage is visible and activity summaries have their own label',()=>{
 const run=harness();assert.match(run('cacheCoverage({known_calls:2,unknown_calls:3})'),/已知 2 次，另 3 次/);
 assert.match(run('cacheCoverage({known_calls:0,unknown_calls:3})'),/已知 0 次，另 3 次/);
 assert.equal(run("purposeNames.activity_summary"),'活动摘要');
});
