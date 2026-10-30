'use strict';
// Cursor pages keep newly arriving records ahead of the historical page boundary.
const listPages=new Map();
const pagedNames={activity:'活动记录',context:'模型上下文',usage:'调用明细',notifications:'通知中心'};
function getListPage(key){if(!listPages.has(key))resetListPage(key);return listPages.get(key);}
function resetListPage(key){listPages.set(key,{index:0,cursors:[null],next:null,count:0,size:20,request:0,busy:false,turning:false,error:''});}
function historicalPage(){return Boolean(pagedNames[state.page]&&getListPage(state.page).index>0);}
function pageNavigation(key){
 const p=getListPage(key);
 return `<nav class="pagination" data-pager="${key}" aria-label="${pagedNames[key]}分页"><div class="pagination-actions"><button class="secondary" data-list="${key}" data-turn="first" ${p.index===0||(p.busy||p.turning)?'disabled':''}>返回最新</button><button class="secondary" data-list="${key}" data-turn="prev" ${p.index===0||(p.busy||p.turning)?'disabled':''}>上一页</button><span class="pagination-position" aria-live="polite">第 ${p.index+1} 页 · 本页 ${p.count} 条</span><button class="secondary" data-list="${key}" data-turn="next" ${p.next===null||(p.busy||p.turning)?'disabled':''}>下一页</button></div><span class="pagination-note">${p.error?esc(p.error):p.index>0?'历史页保持不动，返回最新查看新记录':`每页 ${p.size} 条${p.next===null?' · 已到末页':''}`}</span></nav>`;
}
async function loadListPage(key,path,parameters={},cursorKey='seq'){
 const p=getListPage(key),request=++p.request,index=p.index;
 const query=new URLSearchParams(parameters);query.set('limit',p.size+1);
 const before=p.cursors[index];if(before!==null)query.set('before',before);else query.delete('before');
 p.busy=true;
 try{
  const data=await api(path+'?'+query);
  if(listPages.get(key)!==p||p.request!==request||p.index!==index)return null;
  const items=data.items.slice(0,p.size);
  p.next=data.items.length>p.size?items.at(-1)[cursorKey]:null;
  p.count=items.length;p.error='';
  // A refreshed first page may have a new boundary; discard older forward cursors.
  p.cursors.length=index+1;
  return {...data,items};
 }catch(error){if(listPages.get(key)!==p||p.request!==request)return null;throw error;}
 finally{if(p.request===request)p.busy=false;}
}
async function turnListPage(key,direction){
 const p=getListPage(key);if(p.turning)return;
 const previous={index:p.index,cursors:[...p.cursors],next:p.next,count:p.count};
 if(direction==='next'&&p.next!==null){p.cursors[p.index+1]=p.next;p.index++;}
 else if(direction==='prev'&&p.index>0)p.index--;
 else if(direction==='first'&&p.index>0){p.index=0;p.cursors=[null];}
 else return;
 // A user turn supersedes an in-flight background refresh instead of eating the click.
 p.turning=true;
 document.querySelectorAll(`.pagination[data-pager="${key}"] button`).forEach(b=>b.disabled=true);
 $('#detail').classList.add('hidden');
 let loaded=false;
 try{
  loaded=await (key==='usage'?loadUsage():key==='context'?loadContexts():key==='notifications'?loadNotifications():loadEvents());
 }catch(error){
  if(listPages.get(key)===p)Object.assign(p,previous,{error:error.message,busy:false});
 }finally{
  p.turning=false;
  if(listPages.get(key)===p&&state.page===key&&!p.busy){
   render();
   if(loaded){const target=key==='usage'?$('#usage-details'):$('#content');target?.scrollIntoView({block:'start'});}
  }
 }
}
document.addEventListener('click',e=>{const b=e.target.closest('[data-list][data-turn]');if(b)turnListPage(b.dataset.list,b.dataset.turn);});
