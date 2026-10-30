'use strict';
const usageState={data:null,period:'today',start:'',end:'',provider:'',model:'',purpose:'',app_id:'',activity_id:'',tariff:'',group:'purpose',error:'',draft:null};
const usageFilterKeys=['period','start','end','provider','model','purpose','app_id','tariff'];
function usageDraft(){return usageState.draft||=Object.fromEntries(usageFilterKeys.map(k=>[k,usageState[k]]));}
const purposeNames={environment:'环境决策',compaction:'经历压缩',activity_summary:'活动摘要',chat_planning:'聊天规划',chat_other:'聊天及其他处理',chat_reasoning:'聊天推理',vision:'识图',chat_reply:'聊天回复',memory:'记忆处理',expression:'表达学习',meme:'表情包处理',unclassified:'未分类'};
const costNames={estimated:'估算费用',unpriced:'未配置单价',unknown_usage:'用量未知',unknown_cache:'缓存用量缺失',unknown_schedule:'日历未核对',unknown_tier:'超出已配置 token 档位'};
const usageNumber=n=>n===null||n===undefined?'未知':Number(n).toLocaleString('zh-CN');
const cacheRate=c=>c?.hit_rate==null?'—':(c.hit_rate*100).toFixed(1)+'%';
const cacheCoverage=c=>!c?'暂无可计算的输入/缓存明细':c.unknown_calls?`已知 ${c.known_calls} 次，另 ${c.unknown_calls} 次明细缺失`:!c.known_calls?'暂无可计算的输入/缓存明细':`${c.known_calls} 次调用 · 按输入 token 加权`;
const money=(costs)=>Object.entries(costs||{}).map(([c,v])=>`${c} ${Number(v).toLocaleString('zh-CN',{maximumFractionDigits:6})}`).join(' / ')||'—';
const usageCost=r=>r.cost_state==='estimated'?money({[r.currency]:r.cost}):costNames[r.cost_state]||r.cost_state;
function usageParams(){const p=new URLSearchParams({period:usageState.period});for(const k of ['start','end','provider','model','purpose','app_id','activity_id','tariff'])if(usageState[k])p.set(k,usageState[k]);return p;}
async function loadUsage(){
 const d=await loadListPage('usage','/api/usage',usageParams(),'id');
 if(!d)return false;
 usageState.data=d;usageState.error='';return true;
}
function usageHome(){const u=state.snapshot.usage_today;if(!u)return '';return `<button class="usage-home" data-page="usage">今日用量 · ${usageNumber(u.calls)} 次调用 · ${usageNumber(u.input_tokens+u.output_tokens)} token <span>估算 ${esc(money(u.costs))}${u.unsettled?' · '+u.unsettled+' 次尚未计价':''} ›</span></button>`;}
function usageSelect(id,label,items,value){return `<label>${label}<select id="usage-${id}"><option value="">全部</option>${items.map(v=>`<option value="${esc(v)}" ${v===value?'selected':''}>${esc(id==='purpose'?purposeNames[v]||v:id==='tariff'?tariffNames[v]||v:v)}</option>`).join('')}</select></label>`;}
function usagePage(){
 const d=usageState.data;if(!d)return empty(usageState.error||'正在加载用量…');
 const t=d.totals, options=d.options,f=usageDraft();
 const summaries=[['调用次数',usageNumber(t.calls),'按已记录的调用统计'],['输入 token',usageNumber(t.input_tokens),'含缓存命中部分'],['输出 token',usageNumber(t.output_tokens),'推理 token 不重复累加'],['缓存命中率',cacheRate(t.cache),cacheCoverage(t.cache)],['缓存命中 token',usageNumber(t.cached_tokens),t.unknown_cache?`${t.unknown_cache} 次缺少缓存明细`:'提供商报告'],['估算费用',money(t.costs),t.unsettled?`${t.unsettled} 次费用尚不完整`:'按各次调用的单价版本计算']];
 const max=Math.max(1,...d.series.map(x=>x.input_tokens+x.output_tokens));
 const daily=d.series.map(x=>`<div class="usage-day"><button class="text-button" data-usage-day="${x.date}">${x.date.slice(5)}</button><meter min="0" max="${max}" value="${x.input_tokens+x.output_tokens}" aria-label="${x.date} token 用量">${usageNumber(x.input_tokens+x.output_tokens)}</meter><span>${usageNumber(x.input_tokens+x.output_tokens)} token</span><span>${esc(money(x.costs))}</span></div>`).join('');
 const tariffTable=panel('计费时段',`<div class="tariff-cards">${(d.groups.tariff||[]).map(g=>`<button class="tariff-card" data-usage-tariff="${esc(g.name)}"><strong>${esc(tariffNames[g.name]||g.name)}</strong><span>${usageNumber(g.calls)} 次 · ${usageNumber(g.input_tokens+g.output_tokens)} token</span><span>${esc(money(g.costs))}${g.unsettled?' · '+g.unsettled+' 次未完成计价':''}</span></button>`).join('')||empty('暂无计费时段记录。')}</div>`,'<button class="text-button" data-page="settings">前往计费设置 ›</button>');
 const group=d.groups[usageState.group]||[];
 const groupTable=group.length?`<div class="table-wrap"><table><thead><tr><th>分类</th><th>调用</th><th>输入 / 输出</th><th>缓存命中率</th><th>估算费用</th></tr></thead><tbody>${group.map(g=>`<tr><td><button class="text-button" data-usage-group="${esc(g.name)}">${esc(usageState.group==='purpose'?purposeNames[g.name]||g.name:g.name)}</button></td><td>${g.calls}</td><td>${usageNumber(g.input_tokens)} / ${usageNumber(g.output_tokens)}</td><td>${esc(cacheRate(g.cache))}${g.cache?.unknown_calls?`<small class="usage-muted">${esc(cacheCoverage(g.cache))}</small>`:''}</td><td>${esc(money(g.costs))}${g.unsettled?`<small class="usage-muted">${g.unsettled} 次尚未计价</small>`:''}</td></tr>`).join('')}</tbody></table></div>`:empty('当前范围没有调用记录。');
 const details=d.items.length?`<div class="table-wrap"><table><thead><tr><th>时间 / 用途</th><th>服务商 / 模型</th><th>输入 / 输出 / 缓存命中</th><th>状态 / 耗时</th><th>费用</th></tr></thead><tbody>${d.items.map(r=>`<tr tabindex="0" data-record="${remember('usage'+r.id,r,'模型调用用量与计价')}" aria-label="查看 ${esc(r.model)} 调用详情"><td>${date(r.created)}<small class="usage-muted">${esc(purposeNames[r.purpose]||r.purpose)} · ${esc(appName(r.app))}</small></td><td>${esc(r.model)}<small class="usage-muted">${esc(r.provider)}</small></td><td>${usageNumber(r.input_tokens)} / ${usageNumber(r.output_tokens)} / ${usageNumber(r.cached_tokens)}<small class="usage-muted">命中率 ${esc(cacheRate(r.cache))}</small></td><td>${esc(statusNames[r.status]||({interrupted:'中断'}[r.status])||r.status)}<small class="usage-muted">${r.duration_ms===null?'耗时未知':r.duration_ms+' ms'}</small></td><td>${esc(usageCost(r))}<small class="usage-muted">${esc(tariffNames[r.tariff]||r.tariff||'未配置规则')}</small></td></tr>`).join('')}</tbody></table></div>`:empty('没有匹配的调用。历史未记录的用量不会补造。');
 return `<div class="toolbar usage-toolbar"><label>时段<select id="usage-period">${[['today','今日'],['month','本月'],['7d','最近 7 天'],['30d','最近 30 天'],['custom','自定义']].map(([v,l])=>`<option value="${v}" ${v===f.period?'selected':''}>${l}</option>`).join('')}</select></label><label class="usage-custom-date ${f.period==='custom'?'':'hidden'}">开始日期<input type="date" id="usage-start" value="${esc(f.start)}"></label><label class="usage-custom-date ${f.period==='custom'?'':'hidden'}">结束日期<input type="date" id="usage-end" value="${esc(f.end)}"></label>${usageSelect('provider','服务商',options.provider,f.provider)}${usageSelect('model','模型',options.model,f.model)}${usageSelect('purpose','用途',options.purpose,f.purpose)}${usageSelect('app_id','应用',options.app,f.app_id)}${usageSelect('tariff','计费时段',['fixed','tiered','peak','off_peak','unknown','unpriced'],f.tariff)}<button id="usage-apply" class="secondary">应用筛选</button><button id="usage-reset" class="secondary">重置</button></div><div id="usage-summary"><p class="usage-note">${esc(d.start)} — ${esc(d.end)} · 北京时间 · ${usageState.activity_id?'已筛选单次活动 · ':''}本地估算，非服务商账单。${t.unknown_usage?`${t.unknown_usage} 次用量未知；`:''}${t.unpriced?`${t.unpriced} 次未配置单价；`:''}不同币种分别统计。</p><p id="usage-error" role="alert">${esc(usageState.error)}</p><div class="usage-metrics">${summaries.map(([l,v,n])=>`<div class="panel usage-metric"><small>${l}</small><strong>${esc(v)}</strong><p>${esc(n)}</p></div>`).join('')}</div><div class="section-gap">${tariffTable}</div></div><div class="usage-grid section-gap"><div id="usage-trend">${panel('每日趋势',`<div class="usage-days">${daily}</div>`,'<span class="caption">点击日期查看明细</span>')}</div>${panel('用量去向','<div id="usage-groups">'+groupTable+'</div>',`<select id="usage-group" aria-label="汇总维度">${[['purpose','按用途'],['model','按模型'],['provider','按服务商'],['app','按应用'],['activity_id','按活动']].map(([k,l])=>`<option value="${k}" ${k===usageState.group?'selected':''}>${l}</option>`).join('')}</select>`)}</div><div id="usage-details" class="section-gap">${panel('调用明细',pageNavigation('usage')+details+pageNavigation('usage'),'<span class="caption">点击查看计价依据与关联记录</span>')}</div>`;
}
function renderUsagePage(){
 const markup=usagePage();
 if(!$('#usage-summary')){$('#content').innerHTML=markup;return;}
 const template=document.createElement('template');template.innerHTML=markup;
 // Only replace results. Native selects and date inputs stay connected, even if
 // a live request finishes while the user is choosing a filter.
 for(const id of ['usage-summary','usage-trend','usage-groups','usage-details']){
  const next=template.content.querySelector('#'+id);
  if(next)$('#'+id).innerHTML=next.innerHTML;
 }
 for(const next of template.content.querySelectorAll('.usage-toolbar select')){
  const current=$('#'+next.id);
  if(current===document.activeElement)continue;
  if(current.innerHTML!==next.innerHTML)current.innerHTML=next.innerHTML;
  if(current.value!==next.value)current.value=next.value;
 }
 const draft=usageDraft();
 document.querySelectorAll('.usage-custom-date').forEach(label=>label.classList.toggle('hidden',draft.period!=='custom'));
 for(const key of ['start','end']){
  const input=$('#usage-'+key);
  if(input!==document.activeElement)input.value=draft[key];
 }
 if($('#usage-group').value!==usageState.group)$('#usage-group').value=usageState.group;
}
function readUsageFilters(){const draft=usageDraft();for(const k of usageFilterKeys){const el=$('#usage-'+k);if(el)draft[k]=el.value;}}
function commitUsageFilters(){Object.assign(usageState,usageDraft());}
async function applyUsage(){resetListPage('usage');try{if(await loadUsage())render();}catch(e){usageState.error=e.message;const el=$('#usage-error');if(el)el.textContent=e.message;}}
document.addEventListener('input',e=>{if(e.target.closest('.usage-toolbar'))readUsageFilters();});
document.addEventListener('change',e=>{
 if(e.target.id==='usage-group'){usageState.group=e.target.value;render();return;}
 if(!e.target.closest('.usage-toolbar'))return;
 readUsageFilters();
 if(e.target.id==='usage-period'){
  const draft=usageDraft();
  if(draft.period==='custom'){draft.start||=usageState.data.start;draft.end||=usageState.data.end;render();}
  else{commitUsageFilters();applyUsage();}
 }
});
document.addEventListener('click',async e=>{
 const b=e.target.closest('#usage-apply,#usage-reset,[data-usage-day],[data-usage-group],[data-usage-activity],[data-usage-tariff]');if(!b)return;
 if(b.id==='usage-apply'){readUsageFilters();commitUsageFilters();return applyUsage();}
 if(b.id==='usage-reset'){Object.assign(usageState,{period:'today',start:'',end:'',provider:'',model:'',purpose:'',app_id:'',activity_id:'',tariff:'',draft:null});return applyUsage();}

 if(b.dataset.usageTariff)usageState.tariff=b.dataset.usageTariff;
 if(b.dataset.usageDay){usageState.period='custom';usageState.start=usageState.end=b.dataset.usageDay;}
 if(b.dataset.usageGroup){const k=usageState.group==='app'?'app_id':usageState.group;usageState[k]=b.dataset.usageGroup==='unclassified'&&k==='activity_id'?'__unclassified__':b.dataset.usageGroup;}
 if(b.dataset.usageActivity){usageState.activity_id=b.dataset.usageActivity;$('#detail').classList.add('hidden');state.page='usage';history.replaceState(null,'','#usage');}
 usageState.draft=null;
 return applyUsage();
});

function usageDetail(r){
 if(!r.cost_state)return '';
 const p=r.pricing;
 return `<section class="usage-detail">${p?.backfill?`<p class="backfill-label">${esc(p.backfill.label)} · ${date(p.backfill.at)}<br>补算批次 ${esc(p.backfill.batch_id)}</p>`:''}<h3>${esc(usageCost(r))} · ${esc(tariffNames[r.tariff]||r.tariff||'未配置规则')}</h3>${p?.reason?`<p>${esc(p.reason)}${p.local_time?' · '+esc(p.local_time):''}${p.timezone?' · '+esc(p.timezone):''}</p>`:''}${tierSelection(p)}<p>输入 ${usageNumber(r.input_tokens)} token，其中缓存命中 ${usageNumber(r.cached_tokens)}；输出 ${usageNumber(r.output_tokens)} token。缓存命中率 ${esc(cacheRate(r.cache))}。</p><p>命中率 = 有完整缓存明细的调用中，缓存命中 token 总量 ÷ 输入 token 总量。${esc(cacheCoverage(r.cache))}；未报告明细的调用不按零命中计。</p>${p&&p.input_price!==undefined?`<p>单价版本 #${p.rate_id??p.id} · ${esc(p.currency)} / 百万 token<br>普通输入 ${esc(p.input_price)} · 缓存命中 ${esc(p.cached_price??p.input_price)} · 输出 ${esc(p.output_price)}</p><p>费用 =（普通输入量 × 普通输入单价 + 缓存命中量 × 缓存单价 + 输出量 × 输出单价）÷ 1,000,000。推理量已包含在提供商输出口径中，不额外累加。</p>`:'<p>此调用缺少可用单价或已核对的日历，详情见计价快照；后续配置不会自动补算历史。</p>'}<p>服务商：${esc(r.provider)} · 模型：${esc(r.model)}<br>用途：${esc(purposeNames[r.purpose]||r.purpose)} · 耗时：${r.duration_ms===null?'未知':r.duration_ms+' ms'}</p></section>`;
}
