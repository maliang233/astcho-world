'use strict';
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {snapshot:null, contexts:[], notifications:[], events:[], decisions:[], page:'overview', selectedApp:null, paused:false, appFilter:'', search:'', activity:'', stream:null, generation:0, records:new Map()};
const names = {settings:'设置',usage:'用量与费用',context:'模型上下文',overview:'星回',life:'日程与状态',notifications:'通知中心',activity:'活动记录',reminders:'稍后提醒',apps:'应用空间',app:'应用详情'};
const phases = {idle:'此刻，安静地待着。',thinking:'正在选择下一步。',executing:'正在使用应用。',observer:'观察台已就绪。',error:'活动遇到了问题。',budget_exhausted:'本轮已达到步数上限。',offline:'尚未开始运行。'};
const statusNames = {pending:'待送达',delivered:'已送达',ignored:'已忽略',succeeded:'已完成',running:'执行中',failed:'失败',unknown:'结果未知',cancelled:'已取消',ready:'可用 · 尚未启动',stopped:'已停止'};
const appState = (s) => s==='running'?'已启动':statusNames[s]||s;
const appName = (id) => state.snapshot?.apps.find(a=>a.app_id===id)?.dashboard.name || (id==='world'?'World':id);
const clock = (t) => t ? new Date(t*1000).toLocaleTimeString('zh-CN',{hour12:false}) : '—';
const date = (t) => t ? new Date(t*1000).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}) : '—';
const empty = (text) => `<div class="empty"><span class="empty-symbol">✧</span>${esc(text)}</div>`;
function remember(key, record, title){state.records.set(key,{record,title});return esc(key);}
async function api(path, options){const r=await fetch(path,options); if(r.status===401){$('#login').classList.remove('hidden');throw new Error('需要连接观察台');}if(!r.ok){const error=await r.json().catch(()=>({}));throw new Error(typeof error.detail==='string'?error.detail:`请求失败 (${r.status})`);}return r.json();}
function connected(ok){document.body.classList.toggle('web-disconnected',!ok);if(!ok){clearTimeout(portraitState.timer);portraitState.pending=null;portraitState.current='/assets/astcho-desk.png';if($('#presence-image'))$('#presence-image').src=portraitState.current;}$('#connection').innerHTML=`<i class="dot"></i>${ok?'实时连接':'连接中断 · 自动重连'}`;$('#connection').classList.toggle('offline',!ok);}
function eventIcon(e){if(e.kind.endsWith('failed')||e.data?.status==='unknown'||e.data?.status==='failed')return ['!','red'];if(e.kind.startsWith('notification'))return ['▱',''];if(e.kind.startsWith('reminder'))return ['◷','amber'];if(e.kind.startsWith('model'))return ['✧',''];if(e.kind.startsWith('decision'))return ['⑂',''];return ['↗',''];}
function eventMarkup(e){const [icon,color]=eventIcon(e);let text=e.data?.text||e.data?.message||e.data?.content||e.data?.tool||e.operation_id||e.kind;if(typeof text==='object') text=JSON.stringify(text);return `<button class="event" data-record="${remember('e'+e.seq,e,e.summary)}"><span class="event-icon ${color}">${icon}</span><span class="event-body"><span class="event-top"><strong>${esc(e.summary)}</strong><span class="mini-tag">${esc(appName(e.app))}</span><time>${clock(e.created)}</time></span><p>${esc(text)}</p></span></button>`;}
function notificationsMarkup(items){return items.length?items.map(n=>`<button class="notification" data-record="${remember('n'+n.id,n,'通知详情')}"><span class="notification-top"><strong>${esc(n.source)}</strong><span class="status-tag">${esc(statusNames[n.state]||n.state)}</span></span><p>${esc(n.message)}</p><small>${esc(appName(n.app))} · ${date(n.created)}</small></button>`).join(''):empty('还没有通知。新的通知会出现在这里。');}
function appRow(a){return `<button class="app-row" data-app="${esc(a.app_id)}"><span class="app-dot">${esc(a.dashboard.icon)}</span><span><strong>${esc(a.dashboard.name)}</strong><small>${a.tools.length} 项工具 · v${esc(a.version)}</small></span><span class="state">${esc(appState(a.state))} ↗</span></button>`;}
function panel(title,body,extra=''){return `<section class="panel"><div class="panel-heading"><h2>${title}</h2>${extra}</div>${body}</section>`;}
function decisionTitle(e){
  const d=e.data||{},a=d.arguments||{};
  const titles={world_apps:'查看有哪些应用',world_tools:`查看 ${appName(a.app_id)||'应用'} 的可用工具`,world_notifications:'查看通知',world_ignore:'忽略一条通知',world_open:'打开通知，读取上下文',world_remind:'给稍后的自己设置提醒',world_skills:'查看可用知识',world_read_skill:'读取使用知识',world_recall:'回看已经经历的记录',world_finish:'结束这轮活动'};
  if(e.kind==='activity.failed')return '这轮活动遇到了问题';
  if(titles[d.tool])return titles[d.tool];
  const [app,tool]=(d.tool||'').split('__');
  return tool?`使用 ${appName(app)} · ${tool}`:e.summary;
}
function decisionMarkup(e){
  const result=state.decisions.find(r=>r.kind==='decision.result'&&r.activity_id===e.activity_id&&r.data?.call_id&&r.data.call_id===e.data?.call_id);
  const value=result?.data?.result;
  const outcome=e.kind==='activity.failed'?'活动中断':!result?'尚无关联结果':value?.error?'执行出错':statusNames[value?.status]||(value?.status==='idle'?'已结束':'已返回结果');
  const detail={...e,execution_result:result||null};
  return `<button class="decision-row" data-record="${remember('choice'+e.seq,detail,decisionTitle(e))}"><time>${clock(e.created)}</time><span><strong>${esc(decisionTitle(e))}</strong><small>${esc(outcome)}${e.data?.arguments?.text?' · '+esc(e.data.arguments.text):''}</small></span><span aria-hidden="true">›</span></button>`;
}
function presenceCopy(){
  const s=state.snapshot,r=s.runtime,p=s.presentation;
  const phaseText={idle:'安静待着',thinking:'正在思考下一步',executing:'正在行动',observer:'星回尚未启动',error:'活动遇到问题',budget_exhausted:'本轮达到步数上限',offline:'尚未开始运行'}[r.phase]||r.phase;
  const sub=r.phase==='observer'?'Web 已连接，可以查看已有记录。':r.phase==='executing'?`正在调用 ${r.tool||'工具'}，结果尚未返回。`:r.phase==='thinking'?'正在等待模型返回选择。':r.phase==='idle'?'本轮活动已经结束，等待下一次唤醒。':'以实际运行记录为准。';
  return `<span class="eyebrow">此刻</span><h2>${esc(p?.available&&r.phase==='idle'?p.label:phaseText)}</h2>${portraitEvidence(p)}<p>${esc(sub)}</p><p class="state-time">${r.updated?'状态更新于 '+date(r.updated):'暂无状态时间'}</p>${r.phase==='observer'?'<p class="state-note">当前是观察模式，立绘不代表模型正在运行。</p>':''}<div class="presence-inbox"><strong>${s.counts.pending} 条通知待送达</strong><button class="text-button" data-page="notifications">查看通知 ›</button></div>`;
}
function homeSections(){
  const s=state.snapshot;
  const decisions=state.decisions.filter(e=>e.kind==='decision.tool'||e.kind==='activity.failed').slice(0,8);
  return `${activitySummary()}${lifeSummary()}${usageHome()}<div class="home-history">${panel('最近做了什么',decisions.map(decisionMarkup).join('')||empty('暂时还没有决策记录。星回开始活动后，会在这里留下选择和结果。'),'<button class="text-button" data-page="activity">全部活动 ›</button>')}${panel('最近收到的通知',notificationsMarkup(s.notifications.slice(0,3)),'<button class="text-button" data-page="notifications">全部通知 ›</button>')}</div>`;
}
function renderOverview(){
  // Keep the portrait connected during live refreshes; detaching it restarts painting/loading.
  if(!$('#home-sections'))$('#content').innerHTML='<section class="room"><div class="portrait"><span class="portrait-note">星回</span></div><div class="presence-copy"></div></section><div id="home-sections"></div>';
  const evidenceOpen=$('.portrait-evidence')?.open||false;
  $('.presence-copy').innerHTML=presenceCopy();
  if($('.portrait-evidence'))$('.portrait-evidence').open=evidenceOpen;
  $('#home-sections').innerHTML=homeSections();
  updatePortrait(state.snapshot.presentation);
}
const connectionNames={connected:'已连接',disconnected:'等待连接',not_running:'接入未运行'};
function lifeSummary(){
  const life=state.snapshot.life;if(!life)return '';
  const schedule=life.schedule,current=schedule.current,connection=life.connection;
  return `<div class="life-summary"><button class="panel life-card" data-page="life"><span>当前日程</span><strong>${esc(schedule.configured?current.routine:'默认日程')}</strong><p>${esc(current.mood)}</p></button><button class="panel life-card" data-page="life"><span>日程说话系数</span><strong>${esc(current.talk_value)} <small>/ 100</small></strong><p>沿用原规则，影响通知概率</p></button><button class="panel life-card" data-page="life"><span>QQ · ${connection.transport==='http'?'HTTP':'反向 WebSocket'}</span><strong>${esc(connectionNames[connection.state]||connection.state)}</strong><p>${connection.state==='not_running'?'当前观察模式未开启 QQ 接入':connection.transport==='http'?'HTTP 事件接收模式':connection.state==='connected'?'同一连接接收消息与调用接口':'等待 NapCat 主动连接'}</p></button></div>`;
}
function lifePage(){
  const life=state.snapshot.life;if(!life)return empty('当前没有日程与状态数据。');
  const schedule=life.schedule,current=schedule.current,cognition=life.cognition;
  const blocks=schedule.today.blocks;
  const table=blocks.length?`<div class="table-wrap"><table><thead><tr><th>时段</th><th>日程状态</th><th>说话系数</th><th>生效状态</th></tr></thead><tbody>${blocks.map(b=>`<tr class="${b.active?'schedule-active':''}"><td>${esc(b.start)} — ${esc(b.end)}</td><td>${esc(b.mood)}</td><td>${esc(b.talk_value)} / 100</td><td>${b.active?'当前时段':'—'}</td></tr>`).join('')}</tbody></table></div>`:empty('未找到配置的时段，当前使用原服务默认值。');
  const moods=cognition.moods||[];
  const moodTable=moods.length?`<div class="table-wrap"><table><thead><tr><th>群</th><th>兴奋度</th><th>害羞度</th><th>原状态更新时间</th></tr></thead><tbody>${moods.map(m=>`<tr><td>${esc(m.group_id)}</td><td>${Number(m.excitement).toFixed(2)}</td><td>${Number(m.shyness).toFixed(2)}</td><td>${date(m.updated_at)}</td></tr>`).join('')}</tbody></table></div>`:empty('原聊天运行时尚未上报情绪数据，不显示推测值。');
  return lifeSummary()+`<div class="section-gap">${panel('今日日程',`<div class="life-description"><strong>${esc(schedule.today.routine)}${current.overridden?' · 临时覆盖中':''}</strong><p>${esc(schedule.date)} · ${esc(schedule.timezone)} · ${esc(schedule.source||'未配置日程文件')}</p><p>当前：${esc(current.mood)}</p>${schedule.today.mood_append?`<p>特殊日期附加：${esc(schedule.today.mood_append)}</p>`:''}</div>${table}`,'<span class="caption">原日程服务 · 只读</span>')}</div><div class="section-gap">${panel('原版能力接入情况',`<div class="component-list">${life.components.map(c=>`<div class="component-row"><span><strong>${esc(c.name)}</strong><p>${esc(c.detail)}</p></span><span class="status-tag">${esc(c.status)}</span></div>`).join('')}</div>`)}</div><div class="section-gap">${panel('情绪与记忆上报',`<div class="life-description"><p>运行时：${cognition.initialized?'已初始化':'尚未初始化或进程未运行'} · 记忆数量：${cognition.memory_count===null?'尚无实时数据':esc(cognition.memory_count)}</p><p>最近上报：${date(cognition.last_report)}。浏览本页不会启动聊天、检索记忆或标记消息已读。</p></div>${moodTable}`)}</div>`;
}
function toolbar(){return `<div class="toolbar"><input id="search" aria-label="搜索记录" placeholder="搜索活动内容、工具或结果…" value="${esc(state.search)}"><select id="app-filter" aria-label="按应用筛选"><option value="">所有应用</option><option value="world" ${state.appFilter==='world'?'selected':''}>World</option>${state.snapshot.apps.map(a=>`<option value="${esc(a.app_id)}" ${state.appFilter===a.app_id?'selected':''}>${esc(a.dashboard.name)}</option>`).join('')}</select><button id="pause" class="secondary">${state.paused?'继续实时更新':'暂停实时更新'}</button>${state.activity?'<button id="clear-activity" class="secondary">退出单次活动</button>':''}</div>`;}
function activity(){return toolbar()+'<div id="activity-results">'+panel(state.activity?'单次活动轨迹':'活动时间线',`${pageNavigation('activity')}<div class="event-list">${state.events.map(eventMarkup).join('')||empty('没有匹配的记录。')}</div>${pageNavigation('activity')}`,`<span class="live">${historicalPage()?'历史页':state.paused?'已暂停':'实时更新'}</span>`)+'</div>';}
function renderActivityPage(){
 const markup=activity();
 if(!$('#activity-results')){$('#content').innerHTML=markup;return;}
 const template=document.createElement('template');template.innerHTML=markup;
 $('#activity-results').innerHTML=template.content.querySelector('#activity-results').innerHTML;
 for(const id of ['search','app-filter']){
  const current=$('#'+id),next=template.content.querySelector('#'+id);
  if(current===document.activeElement)continue;
  if(id==='app-filter'&&current.innerHTML!==next.innerHTML)current.innerHTML=next.innerHTML;
  if(current.value!==next.value)current.value=next.value;
 }
 $('#pause').textContent=template.content.querySelector('#pause').textContent;
 const clear=template.content.querySelector('#clear-activity');
 if(!clear)$('#clear-activity')?.remove();else if(!$('#clear-activity'))$('#content .toolbar').append(clear);
}

function notificationsPage(){return panel('通知中心',`${pageNavigation('notifications')}${notificationsMarkup(state.notifications)}${pageNavigation('notifications')}`);}
function apps(){return `<div class="cards">${state.snapshot.apps.map(a=>`<button class="panel app-card" data-app="${esc(a.app_id)}"><span class="app-dot">${esc(a.dashboard.icon)}</span><h2>${esc(a.dashboard.name)} ↗</h2><p>${esc(a.description)}</p><small>${a.tools.length} 项工具 · ${esc(appState(a.state))} · v${esc(a.version)}</small></button>`).join('')||empty('还没有接入应用。')}</div>`;}
function reminders(){const items=state.snapshot.reminders;return items.length?`<div class="cards">${items.map(r=>`<button class="panel app-card" data-record="${remember('r'+r.id,r,'自设提醒')}"><span class="mini-tag">${r.fired?'已触发':'等待到期'}</span><div class="reminder-date">${new Date(r.due*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false})}</div><p>${esc(r.text)}</p><small>${date(r.due)}</small></button>`).join('')}</div>`:empty('星回还没有设置提醒。');}
function appPage(){const a=state.snapshot.apps.find(a=>a.app_id===state.selectedApp);if(!a)return empty('这个应用目前没有注册。历史记录仍然保留。');return `<div id="app-content"><div class="app-title"><span class="brand-mark">${esc(a.dashboard.icon)}</span><div><h2>${esc(a.dashboard.name)}</h2><p>${esc(a.description)} · v${esc(a.version)}</p></div></div>${panel('可用能力',`<div class="tool-grid">${a.tools.map(t=>`<div class="tool"><strong>${esc(t.name)}</strong> <span class="mini-tag">${t.effect==='write'?'写入操作':'读取工具'}</span><p>${esc(t.description)}</p><details><summary>参数结构 · ${t.timeout}s 超时</summary><pre>${esc(JSON.stringify(t.input_schema,null,2))}</pre></details></div>`).join('')}</div>`)}<div class="section-gap">${panel('最近操作',`<div class="event-list">${state.events.filter(e=>e.app===a.app_id).slice(0,12).map(eventMarkup).join('')||empty('这个应用尚无活动记录。')}</div>`,`<button class="text-button" data-app-history="${esc(a.app_id)}">完整记录 ↗</button>`)}</div></div>${appControls(a)}<div id="plugin-views"></div>`;}
function renderAppPage(){
 const a=currentAccountApp(),panel=$('#app-account-controls');
 if(a&&panel?.dataset.app===a.app_id&&$('#app-content')){
  const template=document.createElement('template');template.innerHTML=appPage();
  $('#app-content').innerHTML=template.content.querySelector('#app-content').innerHTML;
  $('#plugin-views').replaceChildren();
 }else $('#content').innerHTML=appPage();
 if(a)updateAccountPanel(a);
}
function contextPage(){
  const list=state.contexts||[];
  return panel('实际模型输入',`<p class="list-description">查看每次调用的消息、工具和提供商报告用量。没有新增固定输入预算；字符数不等于 token 数。输入为 DSH 调用时记录，尚未转换成提供商协议。</p>${pageNavigation('context')}<div class="table-wrap"><table><thead><tr><th>时间 / 用途</th><th>模型</th><th>消息 / 工具</th><th>用量</th></tr></thead><tbody>${list.map(r=>`<tr tabindex="0" data-context="${r.seq}"><td>${date(r.created)}<br>${r.purpose==='compaction'?'经历压缩':r.purpose==='activity_summary'?'活动摘要':'环境选择'}${r.compaction?'<br>'+esc(statusNames[r.compaction.status]||r.compaction.status):''}</td><td>${esc(r.model)}</td><td>${r.message_count} / ${r.tool_count}</td><td>${r.usage?esc(JSON.stringify(r.usage)):'未记录提供商用量'}</td></tr>`).join('')}</tbody></table></div>${list.length?'':empty('尚未记录模型输入。旧记录不会补造。')}${pageNavigation('context')}`);
}
function readPath(value,path){return path.split('.').reduce((v,k)=>v?.[k],value);}
async function pluginViews(generation){const a=state.snapshot.apps.find(a=>a.app_id===state.selectedApp);if(!a)return;const target=$('#plugin-views');for(const v of a.dashboard.views){try{const data=await api(`/api/apps/${encodeURIComponent(a.app_id)}/views/${encodeURIComponent(v.id)}`);if(generation!==state.generation)return;let body;if(!data.items.length)body=empty('尚无记录。这里展示已保存的历史，不会主动读取应用。');else if(v.component==='cards')body=`<div class="tool-grid">${data.items.map((row,i)=>`<button class="tool" data-record="${remember(`v${a.app_id}${v.id}${i}`,row,v.title)}">${Object.entries(v.columns).map(([key,label])=>`<p>${esc(label)}：${esc(formatValue(readPath(row,key)))}</p>`).join('')}</button>`).join('')}</div>`;else{const cols=Object.entries(v.columns).length?Object.entries(v.columns):[['id','记录'],['status','状态']];body=`<div class="table-wrap"><table><thead><tr>${cols.map(([,l])=>`<th>${esc(l)}</th>`).join('')}</tr></thead><tbody>${data.items.map((row,i)=>`<tr tabindex="0" data-record="${remember(`v${a.app_id}${v.id}${i}`,row,v.title)}">${cols.map(([key])=>`<td>${esc(formatValue(readPath(row,key)))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;}target.insertAdjacentHTML('beforeend',`<div class="section-gap">${panel(esc(v.title),body,'<span class="caption">APP VIEW · v1</span>')}</div>`);}catch(e){if(generation===state.generation)target.insertAdjacentHTML('beforeend',`<div class="notice section-gap">${esc(v.title)}：${esc(e.message)}</div>`);}}}
function formatValue(v){return v===undefined?'—':typeof v==='object'?JSON.stringify(v):v;}
function render(){if(!state.snapshot||(pagedNames[state.page]&&getListPage(state.page).turning))return;const generation=++state.generation;state.records.clear();const s=state.snapshot;$('#breadcrumb').textContent=names[state.page]||'应用';$('#page-title').textContent=state.page==='overview'?'星回':names[state.page];$('#page-subtitle').textContent={overview:'看看他现在在做什么。',life:'沿用原日程，查看真实配置与已上报的状态。',notifications:'通知送达，不等于打开应用；打开应用，也不等于形成记忆。',activity:'沿着实际记录，回看每一次感知与选择。',settings:'按类别查看和管理当前配置。',usage:'查看实际用量、估算费用与每次调用的计价依据。',context:'查看实际送入环境模型的内容与压缩调用。',reminders:'留给稍后的自己，一些还想继续的事。',apps:'每个应用都是一扇独立的窗。',app:'查看应用能力与记录。浏览本页不会调用星回的工具。'}[state.page];$('#eyebrow').textContent=state.page==='overview'?'ASTCHO WORLD':'ASTCHO WORLD / '+state.page.toUpperCase();$('#mode').textContent={demo:'合成数据演示',observer:'只读观察模式',live:'星回运行中'}[s.mode]||s.mode;$('#mode').classList.toggle('demo',s.mode==='demo');$('#nav-count').textContent=s.counts.pending;$('#app-count').textContent=s.apps.length;$('#app-nav').innerHTML=s.apps.map(a=>`<button data-app="${esc(a.app_id)}" class="nav-item ${state.selectedApp===a.app_id&&state.page==='app'?'active':''}"><span class="app-dot">${esc(a.dashboard.icon)}</span>${esc(a.dashboard.name)}</button>`).join('');document.querySelectorAll('.sidebar [data-page]').forEach(b=>b.classList.toggle('active',b.dataset.page===state.page));const notices=[...(s.mode==='demo'?['当前为合成数据演示，未连接真实模型或 QQ。']:[]),...s.issues];$('#notice').textContent=notices.join(' · ');$('#notice').classList.toggle('hidden',!notices.length);if(state.page==='overview')renderOverview();else if(state.page==='usage')renderUsagePage();else if(state.page==='activity')renderActivityPage();else if(state.page==='app')renderAppPage();else $('#content').innerHTML=state.page==='notifications'?notificationsPage():state.page==='apps'?apps():state.page==='reminders'?reminders():state.page==='life'?lifePage():state.page==='context'?contextPage():state.page==='settings'?settingsPage():appPage();$('#updated').textContent='同步于 '+clock(s.server_time);if(!['overview','life','settings'].includes(state.page))$('#developer-nav').open=true;if(state.page==='app')pluginViews(generation);syncAccountPolling();if($('#debug-panel').open)loadDebug();}
async function loadEvents(){
 const p=new URLSearchParams();
 if(state.page==='activity'){
  if(state.appFilter)p.set('app_id',state.appFilter);if(state.search)p.set('q',state.search);if(state.activity)p.set('activity_id',state.activity);
  const result=await loadListPage('activity','/api/events',p);if(!result)return false;
  state.events=result.items;return true;
 }
 if(state.page==='app')p.set('app_id',state.selectedApp);
 p.set('limit','50');state.events=(await api('/api/events?'+p)).items;return true;
}
async function loadContexts(){const data=await loadListPage('context','/api/context');if(!data)return false;state.contexts=data.items;return true;}
async function loadNotifications(){const data=await loadListPage('notifications','/api/notifications',{},'cursor');if(!data)return false;state.notifications=data.items;return true;}
let refreshing=false, queued=false;
async function refresh(){if(refreshing){queued=true;return;}refreshing=true;try{state.snapshot=await api('/api/snapshot');if(state.page==='overview')state.decisions=(await api('/api/events?decisions=true&limit=100')).items;if(['activity','app'].includes(state.page))await loadEvents();if(state.page==='usage')await loadUsage();if(state.page==='settings')await loadSettings();if(state.page==='context')await loadContexts();if(state.page==='notifications')await loadNotifications();if(state.page!=='settings'||!document.activeElement?.closest('#content input,#content select,#content textarea'))render();connected(true);}catch(e){connected(false);}finally{refreshing=false;if(queued){queued=false;refresh();}}}
async function navigate(page,app){if(!['overview','life','settings'].includes(page))$('#developer-nav').open=true;state.page=page;if(pagedNames[page])resetListPage(page);state.selectedApp=app||null;state.activity='';state.search='';state.appFilter='';$('#detail').classList.add('hidden');history.replaceState(null,'','#'+(app?'app/'+encodeURIComponent(app):page));await refresh();}
function showDetail(key){const entry=state.records.get(key);if(!entry)return;const r=entry.record;$('#detail-content').innerHTML=`<span class="eyebrow">OBSERVED RECORD</span><h2>${esc(entry.title)}</h2>${usageDetail(r)}<dl>${[['应用',r.app?appName(r.app):null],['时间',r.created?date(r.created):null],['状态',r.status?statusNames[r.status]||r.status:r.state?statusNames[r.state]||r.state:null],['活动 ID',r.activity_id],['操作 ID',r.operation_id]].filter(([,v])=>v).map(([l,v])=>`<dt>${l}</dt><dd>${esc(v)}</dd>`).join('')}</dl>${r.context_seq?`<button class="secondary" data-context="${r.context_seq}">查看实际模型上下文</button>`:''}${r.activity_id?`<button class="secondary" data-usage-activity="${esc(r.activity_id)}">查看本次活动用量</button>`:''}${r.activity_id?`<button class="primary" data-activity="${esc(r.activity_id)}">查看这次活动的完整轨迹 ↗</button>`:''}<p>以下是已保存的实际记录。没有记录的决策理由不作推测。</p>${r.cost_state?'<details><summary>原始记录与计价快照</summary>':''}<pre>${esc(JSON.stringify(r,null,2))}</pre>${r.cost_state?'</details>':''}`;$('#detail').classList.remove('hidden');}
async function start(){const fragment=location.hash.slice(1);if(fragment.startsWith('token=')){const token=new URLSearchParams(fragment).get('token');history.replaceState(null,'',location.pathname);try{await api('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});}catch(e){$('#login-error').textContent=e.message;}}else if(fragment.startsWith('app/')){state.page='app';state.selectedApp=decodeURIComponent(fragment.slice(4));}else if(names[fragment])state.page=fragment;await refresh();if(state.snapshot)connectStream();}
function connectStream(){state.stream?.close();const stream=new EventSource('/api/stream');state.stream=stream;stream.addEventListener('refresh',()=>{connected(true);if(!state.paused&&!historicalPage()&&!getListPage(state.page).busy&&!document.activeElement?.matches('input,select')&&state.page!=='settings')refresh();if($('#debug-panel').open)loadDebug();});stream.onerror=()=>connected(false);stream.onopen=()=>connected(true);}
$('#login-form').addEventListener('submit',async(e)=>{e.preventDefault();try{await api('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:$('#token').value})});$('#token').value='';$('#login').classList.add('hidden');await refresh();connectStream();}catch(e){$('#login-error').textContent=e.message;}});
$('#refresh').onclick=()=>refresh();$('#close-detail').onclick=()=>$('#detail').classList.add('hidden');document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.matches('tr[data-context]'))e.target.click();if(e.key==='Escape')$('#detail').classList.add('hidden');if(e.key==='Enter'&&e.target.matches('tr[data-record]'))showDetail(e.target.dataset.record);});
document.addEventListener('click',async e=>{const t=e.target.closest('[data-page],[data-app],[data-record],[data-activity],[data-app-history],[data-context],#pause,#clear-activity');if(!t)return;if(t.dataset.context){const c=await api('/api/context?seq='+encodeURIComponent(t.dataset.context));if(c.items[0]){const key='context'+t.dataset.context;remember(key,c.items[0],'实际模型上下文');showDetail(key);}return;}if(t.dataset.page)return navigate(t.dataset.page);if(t.dataset.app)return navigate('app',t.dataset.app);if(t.dataset.record)return showDetail(t.dataset.record);if(t.dataset.activity||t.dataset.appHistory){state.page='activity';state.activity=t.dataset.activity||'';state.appFilter=t.dataset.appHistory||'';state.search='';resetListPage('activity');$('#detail').classList.add('hidden');return refresh();}if(t.id==='pause'){state.paused=!state.paused;return state.paused?render():refresh();}if(t.id==='clear-activity'){state.activity='';resetListPage('activity');return refresh();}});
let activitySearchTimer=null;
document.addEventListener('input',e=>{if(e.target.id!=='search')return;state.search=e.target.value;clearTimeout(activitySearchTimer);activitySearchTimer=setTimeout(()=>{if(state.page==='activity'){resetListPage('activity');refresh();}},250);});
document.addEventListener('change',e=>{if(e.target.id==='app-filter'){state.appFilter=e.target.value;resetListPage('activity');refresh();}if(e.target.id==='search'){clearTimeout(activitySearchTimer);state.search=e.target.value;resetListPage('activity');refresh();}});
document.querySelectorAll('#nav button').forEach(b=>b.title=b.textContent.trim());
const now=new Date();$('#today').innerHTML=`${now.toLocaleDateString('zh-CN',{weekday:'long'})}<strong>${now.toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'}).replace('/',' / ')}</strong>${now.getFullYear()}`;start();

let debugText='',debugLoading=false;
async function loadDebug(){
  if(debugLoading)return;debugLoading=true;
  const source=$('#log-source').value;
  try{
    const logs=await api('/api/logs?source='+encodeURIComponent(source==='events'?'server':source));
    const select=$('#log-source');
    for(const name of logs.sources){if(![...select.options].some(o=>o.value===name)){const option=document.createElement('option');option.value=name;option.textContent=name==='server'?'服务日志':appName(name)+' · 进程日志';select.append(option);}}
    if(source==='events'){
      const params=new URLSearchParams({limit:'100',include_debug:'true'});if(state.activity)params.set('activity_id',state.activity);if(state.appFilter)params.set('app_id',state.appFilter);
      const data=await api('/api/events?'+params);
      debugText=data.items.reverse().map(e=>`${date(e.created)} [${e.kind}] activity=${e.activity_id||'-'} operation=${e.operation_id||'-'}\n${e.summary}\n${JSON.stringify(e.data,null,2)}`).join('\n\n');
      $('#debug-status').textContent='最近 100 条实际事件 · 按当前活动/应用筛选';
    }else{debugText=logs.text;$('#debug-status').textContent=logs.truncated?'仅显示文件末尾 128 KiB':logs.exists?'当前文件内容':'尚未生成日志文件';}
    const pre=$('#debug-output'),scroll=pre.scrollTop;pre.textContent=debugText||'暂无日志。旧记录不会补造请求或堆栈。';pre.scrollTop=scroll;
  }catch(e){$('#debug-status').textContent=e.message;}finally{debugLoading=false;}
}
$('#debug-panel').addEventListener('toggle',()=>{if($('#debug-panel').open)loadDebug();});
$('#debug-refresh').onclick=loadDebug;$('#log-source').onchange=loadDebug;
$('#debug-download').onclick=()=>{const url=URL.createObjectURL(new Blob([debugText],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='astcho-debug.log';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};

function activitySummary(){
  const s=state.snapshot,a=s.logical_activity,w=s.idle_wake||{};
  const status=a?{running:'进行中',waiting:'等待明确事件',suspended:'暂停，可在下次机会继续'}[a.state]||a.state:'暂无持续活动';
  const wake=w.sleeping?'日程睡眠，不进行周期唤醒':w.remaining_seconds!=null?'下次空闲机会约 '+Math.max(0,Math.ceil(w.remaining_seconds))+' 秒后':'当前不排周期唤醒';
  return `<div class="activity-summary">${panel('持续活动',`<div class="activity-summary-body"><p class="activity-note">${a?esc(a.resume_note||'暂无活动说明'): '当前没有继续中的活动'}</p>${a?.wait?`<dl class="activity-wait"><div><dt>等待范围</dt><dd>${esc(a.wait.scope||'选定时间')}</dd></div><div><dt>到期时间</dt><dd>${date(a.wait.expires_at)}</dd></div></dl>`:''}<p class="activity-wake">${esc(wake)}</p>${s.archive_error?'<p class="notice">归档未完成，原历史保留：'+esc(s.archive_error)+'</p>':''}</div>`,`<span class="status-tag">${esc(status)}</span>`)}</div>`;
}
