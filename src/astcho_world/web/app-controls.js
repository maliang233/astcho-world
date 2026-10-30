'use strict';
const controlResults=new Map(),controlBusy=new Set(),controlErrors=new Map();
let accountPollTimer=null,accountPollFor=null;
const pendingAccountStates=new Set(['scan_required','confirmation_required','verifying']);
const accountLabels={scan_required:'等待手机扫码',confirmation_required:'已扫码，请在手机上确认登录',verifying:'正在获取并校验登录凭证',logged_in:'已登录',expired:'二维码已过期，请重新生成',cancelled:'已取消扫码',not_started:'尚未开始扫码',not_logged_in:'未登录',superseded:'二维码已被更新，请刷新登录状态',login_failed:'登录未完成，请重新扫码'};
function currentAccountApp(){return state.page==='app'?state.snapshot?.apps.find(a=>a.app_id===state.selectedApp&&a.admin_tools?.length):null;}
function appControls(a){return a.admin_tools?.length?`<section class="panel" id="app-account-controls" data-app="${esc(a.app_id)}"><div class="panel-heading"><h2>账号与应用状态</h2></div><div class="account-status" aria-live="polite"></div><div class="account-qr"></div><div class="account-actions"></div></section>`:'';}
function updateAccountPanel(a){
 const panel=$('#app-account-controls');if(!panel||panel.dataset.app!==a.app_id)return;
 const result=controlResults.get(a.app_id)||a.availability?.data||{},busy=controlBusy.has(a.app_id),pending=pendingAccountStates.has(result.status);
 const label=accountLabels[result.status]||(result.logged_in?'已登录':'尚未检查登录状态');
 const remaining=result.expires_at?Math.max(0,Math.ceil(result.expires_at-Date.now()/1000)):null;
 const text=`<p><strong>${esc(label)}</strong>${result.name?' · '+esc(result.name):''}</p>${pending&&result.status!=='verifying'?`<p>请用哔哩哔哩手机 App 扫码并确认。页面会自动检查${remaining!==null?'，二维码剩余约 '+remaining+' 秒':''}。</p>`:''}${result.status==='verifying'?'<p>手机确认已收到，正在完成登录，请稍等。</p>':''}${result.error?'<p class="notice">'+esc(result.error)+(controlErrors.get(a.app_id)>=3?' 自动检查已暂停，请手动重试。':'')+'</p>':''}${result.reason?'<p>'+esc({incomplete_login_credential:'B站未返回完整凭证，请重新扫码。',session_rejected:'B站拒绝了本次登录，请重新扫码。',missing_session:'尚未保存有效登录凭证。',session_expired:'登录已失效，请重新扫码。'}[result.reason]||result.reason)+'</p>':''}`;
 const status=panel.querySelector('.account-status');if(status.innerHTML!==text)status.innerHTML=text;
 const qr=panel.querySelector('.account-qr'),image=result.qrcode_image;
 if(pending&&image?.startsWith('data:image/png;base64,')){
  let img=qr.querySelector('img');if(!img){img=document.createElement('img');img.width=220;img.height=220;img.alt='B站登录二维码';qr.append(img);}
  if(img.getAttribute('src')!==image)img.src=image;
 }else qr.replaceChildren();
 const actions=a.admin_tools.flatMap(name=>{const t=a.tools.find(t=>t.name===name);return(t?.input_schema?.properties?.action?.enum||[]).filter(action=>action!=='check'||pending||result.error).filter(action=>action!=='cancel'||pending).map(action=>`<button class="secondary" data-control-app="${esc(a.app_id)}" data-control-tool="${esc(name)}" data-control-action="${esc(action)}" ${busy?'disabled':''}>${esc({status:'刷新登录状态',login:pending?'重新生成二维码':'扫码登录',check:'立即检查',cancel:'取消扫码',logout:'退出登录'}[action]||action)}</button>`);}).join('');
 const buttons=panel.querySelector('.account-actions');if(buttons.innerHTML!==actions)buttons.innerHTML=actions;
}
function syncAccountPolling(){
 const a=currentAccountApp();
 if(!a||document.hidden||controlBusy.has(a.app_id)){
  clearTimeout(accountPollTimer);accountPollTimer=null;accountPollFor=null;return;
 }
 const result=controlResults.get(a.app_id);
 const action=!result?'status':pendingAccountStates.has(result.status)&&(controlErrors.get(a.app_id)||0)<3?'check':null;
 if(accountPollTimer&&accountPollFor===a.app_id&&action)return;
 clearTimeout(accountPollTimer);accountPollTimer=null;accountPollFor=null;
 if(action){accountPollFor=a.app_id;accountPollTimer=setTimeout(()=>{accountPollTimer=null;accountPollFor=null;accountAction(a.app_id,a.admin_tools[0],action,true);},action==='status'?0:2500);}
}
async function accountAction(appId,tool,action,automatic=false){
 if(controlBusy.has(appId))return;
 clearTimeout(accountPollTimer);accountPollTimer=null;accountPollFor=null;controlBusy.add(appId);
 if(!automatic)controlErrors.set(appId,0);
 const previous=controlResults.get(appId)||{},args={action};
 if(['check','cancel'].includes(action)&&previous.login_id)args.login_id=previous.login_id;
 const app=currentAccountApp();if(app?.app_id===appId)updateAccountPanel(app);
 try{
  const result=await api('/api/apps/'+encodeURIComponent(appId)+'/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tool,arguments:args})});
  controlErrors.set(appId,0);controlResults.set(appId,result);await refresh();
 }catch(error){controlErrors.set(appId,(controlErrors.get(appId)||0)+1);controlResults.set(appId,{...previous,error:error.message});}
 finally{controlBusy.delete(appId);const current=currentAccountApp();if(current?.app_id===appId)updateAccountPanel(current);syncAccountPolling();}
}
document.addEventListener('click',e=>{const button=e.target.closest('[data-control-app]');if(button)accountAction(button.dataset.controlApp,button.dataset.controlTool,button.dataset.controlAction);});
document.addEventListener('visibilitychange',syncAccountPolling);
