const NB={data:null,selected:new Set(),page:0,busy:false,game:null,result:null};
function resetOfflineGame(){if(NB.game!==$('game').value&&!NB.busy){NB.game=$('game').value;NB.data=null;NB.result=null;NB.selected.clear();NB.page=0;$('offline-paths').value='';$('offline-status').textContent='先选择文件，解析后确认来源身份与资源。';}renderOffline();}
function renderOffline(){
 $('registration-offline-fields').classList.toggle('has-preview',!!NB.data||!!NB.result);
 $('offline-file-count').textContent=`已选择 ${$('offline-paths').value.split(/\r?\n/).filter(s=>s.trim()).length} 个网页文件`;
 const rows=NB.result?.items||NB.data?.rows||[],pages=Math.ceil(rows.length/50),current=rows.slice(NB.page*50,(NB.page+1)*50);
 $('offline-pick').disabled=$('offline-parse').disabled=$('offline-paths').disabled=NB.busy;
 $('offline-controls').hidden=!NB.data||!!NB.result;
 $('offline-submit').disabled=NB.busy||!NB.selected.size;
 $('offline-submit').textContent=`注册所选项目 · ${NB.selected.size}`;
 $('offline-retry').hidden=!NB.result?.items.some(i=>i.state==='failed');
 $('offline-list').innerHTML=NB.result?workshopItemMarkup(current,NB.game):current.map(row=>`<div class="offline-preview-row"><label><input type="checkbox" data-offline-id="${escape(row.id)}" ${NB.selected.has(row.id)?'checked':''} ${!row.enabled||NB.busy?'disabled':''}><div><strong>${escape(row.name||row.file)}</strong><small>${escape(row.file)} · ${escape(row.format||'格式未知')}</small><small>${escape(row.url||'来源未确认')}</small><small>${row.name?`名称／来源已识别 · ${row.description?'有描述':'描述缺失'} · ${row.resources||0} 个离线资源 · ${row.cover?'含候选图片':'未识别封面'} · 无本体 · `:''}${escape(row.message)}</small></div></label></div>`).join('');
 $('offline-pagination').hidden=!rows.length;$('offline-page').textContent=`${NB.page+1} / ${pages} 页 · ${rows.length} 项`;
 $('offline-prev').disabled=NB.page===0;$('offline-next').disabled=NB.page+1>=pages;
}
async function parseOfflinePages(){
 if(NB.busy||registrationBusy||S.readOnly)return;
 const context={...registrationContext},paths=$('offline-paths').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
 if(!paths.length){$('offline-status').textContent='请先选择网页文件。';return;}
 NB.busy=true;NB.data=null;NB.result=null;renderOffline();$('offline-status').textContent='正在解析所选网页，核实来源及本地资源…';
 try{const data=await rpc('offline-preview',{...context,paths});if(context.game!==$('game').value||context.epoch!==S.epoch)throw Error('游戏已切换，请重新预览');NB.data=data;NB.game=context.game;NB.page=0;NB.selected=new Set(data.rows.filter(r=>r.state==='new'&&r.enabled).map(r=>r.id));$('offline-status').textContent=`共 ${data.rows.length} 项 · 可新注册 ${NB.selected.size} · 已存在 ${data.rows.filter(r=>r.state==='existing').length} · 重复／异常 ${data.rows.filter(r=>!r.enabled||r.state==='repeated').length}`;}
 catch(error){$('offline-status').textContent='解析失败：'+error.message;}
 finally{NB.busy=false;renderOffline();}
}
$('offline-pick').onclick=async()=>{if(NB.busy||registrationBusy)return;try{const value=await rpc('pick-offline-pages',registrationContext,true);if(!value.cancelled){$('offline-paths').value=value.paths.join('\n');NB.data=null;NB.result=null;renderOffline();}}catch(error){$('offline-status').textContent=error.message;}};
$('offline-parse').onclick=parseOfflinePages;
$('offline-all').onclick=()=>{const seen=new Set();NB.selected=new Set(NB.data.rows.filter(r=>{if(!r.enabled||seen.has(r.url))return false;seen.add(r.url);return true;}).map(r=>r.id));renderOffline();};
$('offline-none').onclick=()=>{NB.selected.clear();renderOffline();};
$('offline-list').onchange=event=>{const id=event.target.dataset.offlineId;if(id===undefined)return;if(event.target.checked){const row=NB.data.rows.find(r=>r.id===id);for(const peer of NB.data.rows)if(peer.url===row.url)NB.selected.delete(peer.id);NB.selected.add(id);}else NB.selected.delete(id);renderOffline();};
$('offline-prev').onclick=()=>{NB.page--;renderOffline();};$('offline-next').onclick=()=>{NB.page++;renderOffline();};
$('offline-submit').onclick=async()=>{
 if(NB.busy||registrationBusy||!NB.data||!NB.selected.size)return;
 const context={...registrationContext};NB.busy=true;registrationBusy=true;renderOffline();$('offline-status').textContent='正在按原生批量流程注册并归档网页资源；不下载或部署 Mod。';
 try{const result=await rpc('offline-register',{...context,preview:NB.data.preview,ids:[...NB.selected]},true);NB.result=result;NB.page=0;if(context.game===$('game').value&&context.epoch===S.epoch){$('offline-status').textContent=result.message;await load();registrationContext={game:context.game,snapshot:S.snapshot,epoch:S.epoch};}}
 catch(error){$('offline-status').textContent='注册失败：'+error.message;}
 finally{NB.busy=false;registrationBusy=false;renderOffline();await refreshAcquisition();}
};
$('offline-retry').onclick=()=>{$('offline-paths').value=NB.result.items.filter(i=>i.state==='failed').map(i=>i.source).join('\n');parseOfflinePages();};
const previousOfflineClose=$('registration-close').onclick;
$('registration-close').onclick=()=>{if(NB.busy)$('registration-dialog').close();else previousOfflineClose();};
$('registration-dialog').addEventListener('cancel',()=>{if(NB.busy)$('registration-dialog').close();});

$('offline-paths').oninput=()=>{NB.data=null;NB.result=null;NB.selected.clear();renderOffline();};

document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-offline-result]');if(!button||NB.busy||registrationBusy)return;
 const job=acquisitionData?.tasks.find(t=>t.id===button.dataset.offlineResult);if(!job)return;
 $('game').value=job.game;await load();if($('game').value!==job.game)return;
 openRegistration();$('registration-kind').value='offline';updateRegistrationFields();
 NB.game=job.game;NB.result=job;NB.data=null;NB.page=0;$('offline-status').textContent=job.message;renderOffline();
});
