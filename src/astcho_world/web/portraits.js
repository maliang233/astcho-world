'use strict';
const portraitState={draft:null,dirty:false,schedule:null,scheduleRevision:null,preview:'base.idle',status:'',error:'',busy:false,current:null,pending:null,timer:null,ready:null,failed:new Set()};
function portraitSource(p){
 const fallback='/assets/astcho-desk.png';
 const requested=p?.image?.url||fallback;
 const target=portraitState.failed.has(requested)?fallback:requested;
 if(!portraitState.current){portraitState.current=target;return target;}
 if(target===portraitState.current){portraitState.pending=null;clearTimeout(portraitState.timer);return target;}
 if(target!==portraitState.pending?.target){
  clearTimeout(portraitState.timer);
  const preload=new Image(),request={target,image:preload};
  portraitState.pending=request;
  preload.onload=async()=>{
   try{await preload.decode();}catch{/* A loaded image can still be displayed if decode is unavailable. */}
   if(portraitState.pending!==request)return;
   portraitState.timer=setTimeout(()=>{
    if(portraitState.pending!==request)return;
    portraitState.current=target;portraitState.ready=preload;portraitState.pending=null;
    if(state.page==='overview')render();
   },1500);
  };
  preload.onerror=()=>{
   portraitState.failed.add(target);
   if(portraitState.pending!==request)return;
   portraitState.current=fallback;portraitState.ready=null;portraitState.pending=null;
   if(state.page==='overview')render();
  };
  preload.src=target;
 }
 return portraitState.current;
}
function updatePortrait(p){
 const source=portraitSource(p),old=$('#presence-image');
 const alt='星回 · '+(p?.label||'安静待着');
 if(old?.getAttribute('src')===source){old.alt=alt;return;}
 // Reuse the decoded image rather than requesting the same no-store asset again.
 const image=portraitState.ready?.getAttribute('src')===source?portraitState.ready:new Image();
 image.id='presence-image';image.alt=alt;
 image.onerror=()=>{
  const fallback='/assets/astcho-desk.png';
  portraitState.failed.add(source);
  if(source===fallback)return;
  portraitState.current=fallback;portraitState.pending=null;portraitState.ready=null;
  clearTimeout(portraitState.timer);
  if(state.page==='overview')updatePortrait(p);
 };
 if(image.getAttribute('src')!==source)image.src=source;
 if(old)old.replaceWith(image);else $('.portrait').prepend(image);
}
function portraitEvidence(p){return p?`<p class="state-time">基础状态：${esc(p.base.label)}${p.base.configured?'':' · 日程未分类，使用默认'}<br>当前活动：${esc(p.activity?.label||'暂无正在执行的活动')}</p><details class="portrait-evidence"><summary>立绘选择依据</summary><pre>${esc(JSON.stringify({base:p.base,activity:p.activity,selected:p.selected,image:p.image},null,2))}</pre></details>`:'';}
function initPortraitDraft(d){if(!portraitState.draft||!portraitState.dirty)portraitState.draft={revision:d.revision,states:settingsClone(d.states),tools:{...d.tool_overrides}};if(!portraitState.schedule){portraitState.schedule=settingsClone(d.schedule);portraitState.scheduleRevision=d.schedule_revision;}}
function portraitOptions(d,selected,empty='自动回退'){return `<option value="">${empty}</option>`+d.assets.map(a=>`<option value="${esc(a.id)}" ${selected===a.id?'selected':''}>${esc(a.label)}</option>`).join('');}
function draftPortraitAsset(d,key){const seen=new Set();while(key&&!seen.has(key)){seen.add(key);const row=portraitState.draft.states[key];if(!row)break;const asset=d.assets.find(a=>a.id===row.asset);if(asset)return asset;key=row.fallback;}return d.assets.find(a=>a.id==='idle');}
function portraitPage(d){
 initPortraitDraft(d);const draft=portraitState.draft,keys=Object.keys(draft.states),row=draft.states[portraitState.preview]||draft.states['base.idle'];
 const preview=draftPortraitAsset(d,portraitState.preview);
 return panel('立绘与状态',`<div class="settings-body"><p>基础状态来自日程，当前活动来自实际工具操作；事件驱动活动更新。通知到达不会直接变成看手机。图像只影响网页展示。</p><p role="status">${esc(portraitState.status)}</p><p role="alert" class="portrait-error">${esc(portraitState.error)}</p><div class="portrait-preview"><img src="${esc(preview?.url||'/assets/astcho-desk.png')}" alt="立绘预览"><div><label>仅预览，不改变星回状态<select id="portrait-preview-state">${keys.map(k=>`<option value="${esc(k)}" ${k===portraitState.preview?'selected':''}>${esc(draft.states[k].label)} · ${esc(k)}</option>`).join('')}</select></label><p>五种姿态：安静待着、上课、思考、睡眠、看手机。上课与写笔记共用图片，状态含义分别保留。</p><label>上传图片并分配给预览状态<input type="file" id="portrait-upload" accept="image/png" ${portraitState.busy?'disabled':''}></label><p>透明 PNG，最多 8 MiB、边长最多 4096。上传后先预览，保存映射才应用；旧图片保留，可随时选回。</p></div></div><div class="portrait-mappings">${keys.map(k=>{const r=draft.states[k],resolved=d.resolved[k];return `<div class="portrait-mapping"><strong>${esc(k.startsWith('base.')?'基础状态':'活动')} · ${esc(k)}</strong><label>显示名称<input data-portrait-label="${esc(k)}" value="${esc(r.label)}" maxlength="100"></label><label>使用立绘<select data-portrait-asset="${esc(k)}">${portraitOptions(d,r.asset)}</select></label><label>无图时回退<select data-portrait-fallback="${esc(k)}"><option value="">默认安静立绘</option>${keys.filter(v=>v!==k).map(v=>`<option value="${esc(v)}" ${r.fallback===v?'selected':''}>${esc(draft.states[v].label)} · ${esc(v)}</option>`).join('')}</select></label>${!r.asset?`<small class="usage-muted">尚无专属立绘 · 当前复用 ${esc(resolved?.chain?.join(' → ')||r.fallback||'base.idle')}</small>`:''}</div>`;}).join('')}</div><details class="section-gap"><summary>新增基础状态或活动</summary><div class="portrait-add"><label>稳定标识<input id="portrait-new-key" placeholder="activity.reading 或 base.rest"></label><label>名称<input id="portrait-new-label" placeholder="阅读"></label><button type="button" class="secondary" id="portrait-add-state">添加到草稿</button></div></details><details class="section-gap"><summary>应用工具与活动映射 · ${d.tools.filter(t=>t.signal?.configured_by==='generic').length} 项使用通用回退</summary><p>应用可在清单声明活动类型，也可以在这里覆盖。账号等管理员工具不参与主体活动。</p><div class="table-wrap"><table><thead><tr><th>应用 / 工具</th><th>应用声明</th><th>本地覆盖</th></tr></thead><tbody>${d.tools.map(t=>{const id=t.app+'__'+t.tool;return `<tr><td>${esc(t.app)} · ${esc(t.tool)}</td><td>${esc(t.signal?.kind||'通用活动')}</td><td><select data-portrait-tool="${esc(id)}"><option value="">沿用应用声明 / 通用回退</option>${keys.filter(k=>k.startsWith('activity.')).map(k=>`<option value="${esc(k)}" ${draft.tools[id]===k?'selected':''}>${esc(draft.states[k].label)}</option>`).join('')}</select></td></tr>`;}).join('')}</tbody></table></div></details><button id="portrait-save" class="primary section-gap" ${portraitState.busy?'disabled':''}>保存立绘映射</button><button id="portrait-reload" class="secondary section-gap">重新载入已保存配置</button></div>`)+`<div class="section-gap">${panel('日程基础状态',`<div class="settings-body"><p>分类保存在日程时段的 presentation.base 字段里，改时间或文案仍保留分类。新增时段需选择类别；未分类时回退为安静待着并在这里提示。</p>${!d.schedule_editable?'<p>当前日程仅可查看；可在当前项目的日程文件中配置显示类别。</p>':''}<div class="table-wrap"><table><thead><tr><th>日程</th><th>时段</th><th>基础状态</th></tr></thead><tbody>${portraitState.schedule.map((b,i)=>`<tr><td>${esc(b.routine)}${!b.configured?'<small class="usage-muted">未分类</small>':''}</td><td>${esc(b.start)} — ${esc(b.end)}</td><td><select data-portrait-schedule="${i}" ${!d.schedule_editable?'disabled':''}>${keys.filter(k=>k.startsWith('base.')).map(k=>`<option value="${esc(k)}" ${b.base===k?'selected':''}>${esc(draft.states[k].label)}</option>`).join('')}</select></td></tr>`).join('')}</tbody></table></div><button id="portrait-save-schedule" class="secondary section-gap" ${!d.schedule_editable||portraitState.busy?'disabled':''}>保存日程显示分类</button></div>`)}</div>`;
}
function portraitRender(){readRateDraft();render();}
document.addEventListener('input',e=>{if(!e.target.dataset.portraitLabel)return;portraitState.draft.states[e.target.dataset.portraitLabel].label=e.target.value;portraitState.dirty=true;});
document.addEventListener('change',async e=>{
 const el=e.target;if(el.id==='portrait-preview-state'){portraitState.preview=el.value;portraitRender();return;}
 if(el.dataset.portraitAsset){portraitState.draft.states[el.dataset.portraitAsset].asset=el.value||null;portraitState.dirty=true;portraitRender();}
 if(el.dataset.portraitFallback){portraitState.draft.states[el.dataset.portraitFallback].fallback=el.value||null;portraitState.dirty=true;portraitRender();}
 if(el.dataset.portraitTool){if(el.value)portraitState.draft.tools[el.dataset.portraitTool]=el.value;else delete portraitState.draft.tools[el.dataset.portraitTool];portraitState.dirty=true;}
 if(el.dataset.portraitSchedule!==undefined){const b=portraitState.schedule[Number(el.dataset.portraitSchedule)];b.base=el.value;b.configured=true;}
 if(el.id!=='portrait-upload'||!el.files?.length)return;const file=el.files[0],target=portraitState.preview;if(file.size>8*1024*1024){portraitState.error='图片不能超过 8 MiB';portraitRender();return;}portraitState.busy=true;portraitRender();try{const bitmap=await createImageBitmap(file);const valid=bitmap.width<=4096&&bitmap.height<=4096;bitmap.close();if(!valid)throw new Error('图片边长不能超过 4096 像素');const r=await api('/api/presentation/assets',{method:'POST',headers:{'Content-Type':'image/png'},body:file});portraitState.draft.states[target].asset=r.asset;portraitState.dirty=true;await loadSettings();portraitState.status='图片已上传并加入草稿，保存映射后应用。';portraitState.error='';}catch(error){portraitState.error=error.message;}finally{portraitState.busy=false;portraitRender();}
});
document.addEventListener('click',async e=>{
 const b=e.target.closest('#portrait-add-state,#portrait-save,#portrait-reload,#portrait-save-schedule');if(!b)return;
 if(b.id==='portrait-add-state'){const key=$('#portrait-new-key').value.trim(),label=$('#portrait-new-label').value.trim();if(!/^(base|activity)\.[a-z][a-z0-9_.-]{0,79}$/.test(key)||!label){portraitState.error='请填写 base. 或 activity. 开头的标识及名称。';}else if(portraitState.draft.states[key])portraitState.error='此标识已存在。';else{portraitState.draft.states[key]={label,asset:null,fallback:'base.idle'};portraitState.dirty=true;portraitState.preview=key;portraitState.error='';}portraitRender();return;}
 if(b.id==='portrait-reload'){portraitState.draft=null;portraitState.dirty=false;portraitState.schedule=null;await loadSettings();portraitRender();return;}
 portraitState.busy=true;portraitRender();try{if(b.id==='portrait-save'){await api('/api/presentation/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(portraitState.draft)});portraitState.dirty=false;portraitState.draft=null;}else{await api('/api/presentation/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:portraitState.scheduleRevision,blocks:portraitState.schedule.map(({routine,index,base})=>({routine,index,base}))})});portraitState.schedule=null;}await loadSettings();portraitState.status='已保存，显示规则即时生效。';portraitState.error='';}catch(error){portraitState.error=error.message;}finally{portraitState.busy=false;portraitRender();}
});
