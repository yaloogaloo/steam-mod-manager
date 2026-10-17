let acquisitionData=null, acquisitionSubmitting=false, acquisitionSignature=null,localArchivePaths=[];
const acquisitionPages=new Map();
function showSpace(space) {
  if($('library-filter-menu').matches(':popover-open'))$('library-filter-menu').hidePopover();
  S.space=space;
  if(bridge?.remember_session&&S.snapshot)bridge.remember_session($('game').value,space);
  for(const selector of ['.browse-tabs','.querybar','.workspace'])document.querySelector(selector).hidden=space!=='library';
  $('collection-tools').hidden=space!=='library'||S.browse!=='collections';
  $('acquisition-space').hidden=space!=='acquire';
  $('deployment-space').hidden=space!=='deployment';
  document.querySelector('h1').textContent=space==='deployment'?'部署工作台':space==='acquire'?'获取与同步':'资产库';
  document.querySelector('.breadcrumb').innerHTML='工作台 <span>/</span> '+({acquire:'获取与同步',deployment:'部署工作台',library:'资产库'})[space];
  document.querySelector('.eyebrow').textContent=({acquire:'MOD SOURCES',deployment:'DEPLOYMENT WORKBENCH',library:'YOUR MOD LIBRARY'})[space];
  $('library-add').hidden=space!=='library';
  document.querySelector('.rail-note').textContent=({acquire:'从本地 Workshop 同步内容，或从已有资源登记 Mod。',deployment:'查看当前部署、调整顺序并比较成员快照。',library:'按游戏整理 Mod，查看资源与部署状态。'})[space];
  $('library-nav').classList.toggle('active',space==='library');
  $('deployment-nav').classList.toggle('active',space==='deployment');
  $('acquisition-nav').classList.toggle('active',space==='acquire');
  if(space==='acquire')refreshAcquisition();
  else if(space==='deployment')refreshDeployment();
  else $('summary').textContent=`${S.assets.length} 个 Mod · ${S.assets.filter(a=>a.deployed).length} 已部署`;
}
$('acquisition-nav').onclick=()=>showSpace('acquire');
$('library-nav').onclick=()=>{showSpace('library');$('search').focus();};
$('refresh').onclick=()=>load().then(()=>{if(S.space==='deployment')refreshDeployment();else if(S.space==='acquire')refreshAcquisition();});
async function refreshAcquisition() {
  const context={game:$('game').value,epoch:S.epoch};
  try {
    const data=await rpc('acquisition');
    if(context.game!==$('game').value||context.epoch!==S.epoch||data.game!==context.game)return;
    acquisitionData=data;
    $('import-root').textContent=data.archive_root?'归档到 '+data.archive_root:'当前为只读，未配置可写资源环境';
    if(S.space==='acquire')$('summary').textContent='统一注册与内容归档 · Steam 创意工坊增量同步';
    $('workshop-source').textContent=data.workshop_path||'未配置 Workshop 目录：请从游戏菜单管理当前游戏';
    $('import-start').disabled=$('import-pick').disabled=!data.import_allowed||acquisitionSubmitting;
    $('sync-start').disabled=!data.sync_allowed||acquisitionSubmitting||!data.workshop_path;
    if(data.import_allowed) {
      document.querySelector('.readonly-note').textContent='资源保存在现有归档库';
    }
    $('sync-note').textContent=data.sync_allowed?'扫描当前游戏全部本地订阅内容；原服务按 Steam 时间戳处理新增、更新与跳过，保留受保护资源。':'当前只读或未配置来源服务';
    const signature=JSON.stringify([data.tasks,[...acquisitionPages]]);
    if(signature!==acquisitionSignature){
    const expanded=new Set([...$('acquisition-tasks').querySelectorAll('details[open]')].map(d=>d.dataset.taskDetail));
    acquisitionSignature=signature;
    $('acquisition-tasks').innerHTML=data.tasks.slice().reverse().map(job=>`<article class="acquisition-task"><div><span class="badge ${job.state==='success'?'deployed':''}">${escape({running:'执行中',success:'成功',failed:'失败',duplicate:'已存在',partial:'部分完成'}[job.state]||job.state)}</span><strong>${escape(job.name)}</strong><small>${escape(job.game_name)} · ${({'import-folder':'目录导入','import-zip':'ZIP 内容导入','registration-save':'来源注册','content-add':'补齐本体','content-rescan':'重新识别内容','source-sync':'Steam 来源元数据同步','workshop-sync':'Workshop 原生同步','offline-register':'Nexus 离线网页批量注册'}[job.action]||job.action)}</small></div><p>${escape(job.message)}${job.state==='running'&&Number.isFinite(job.percent)?` · ${job.percent}%`:''}</p>${job.items?.length?`<details data-task-detail="${escape(job.id)}" ${expanded.has(job.id)?'open':''}><summary>逐项结果 · ${job.items.length}</summary>${workshopItemMarkup(job.items.slice((acquisitionPages.get(job.id)||0)*100,((acquisitionPages.get(job.id)||0)+1)*100),job.game)}${job.items.length>100?`<div class="task-pages"><button data-result-page="${escape(job.id)}" data-delta="-1" ${(acquisitionPages.get(job.id)||0)===0?'disabled':''}>上一页</button><span>${(acquisitionPages.get(job.id)||0)+1} / ${Math.ceil(job.items.length/100)}</span><button data-result-page="${escape(job.id)}" data-delta="1" ${((acquisitionPages.get(job.id)||0)+1)*100>=job.items.length?'disabled':''}>下一页</button></div>`:''}</details>`:''}${job.action==='offline-register'&&job.items?.length?`<button data-offline-result="${escape(job.id)}">查看完整注册结果</button>`:''}${job.internal_id&&job.state!=='failed'?`<button data-task-view="${escape(job.id)}">在资产库查看</button>`:''}</article>`).join('')||'<p class="quiet">尚无提交的任务。导入或同步完成后，结果会保留在这里。</p>';
    }
  } catch(error){$('acquisition-message').textContent='读取任务失败：'+error.message;}
}
setInterval(()=>{if(S.space==='acquire')refreshAcquisition();},350);
async function submitAcquisition(action,args) {
  if(acquisitionSubmitting || S.readOnly)return;
  const context={game:$('game').value,snapshot:S.snapshot,epoch:S.epoch};
  acquisitionSubmitting=true;
  $('acquisition-message').textContent='任务执行中，请等待。离开此页不会取消任务。';
  refreshAcquisition();
  try {
    const result=await rpc(action,{...args,...context},true);
    if(context.game===$('game').value) {
      if(action==='pick-import-folder'||action==='pick-import-zip') {
        if(!result.cancelled){if(typeof invalidateRegistration==='function')invalidateRegistration();$('import-source').value=result.source;localArchivePaths=result.sources||[result.source];}
        $('acquisition-message').textContent=result.cancelled?'已取消目录选择，未执行导入':'已选择目录';
      } else {
        await refreshCommandView();
        $('acquisition-message').textContent=result.message;
      }
    }
  } catch(error) {
    if(context.game===$('game').value)$('acquisition-message').textContent='操作失败：'+error.message;
  } finally {
    acquisitionSubmitting=false;
    if(context.game!==$('game').value)$('acquisition-message').textContent='任务已结束，结果保留在原游戏的任务记录中。';
    refreshAcquisition();
  }
}
$('import-form').onsubmit=event=>{event.preventDefault();submitAcquisition($('import-kind').value==='zip'?'import-zip':'import-folder',{
  source:$('import-source').value,workshop_id:$('import-workshop').value.trim(),title:$('import-title').value.trim()});};
$('import-pick').onclick=()=>submitAcquisition($('import-kind').value==='zip'?'pick-import-zip':'pick-import-folder',{platform:$('local-platform').value,multiple:$('import-kind').value==='zip'});
$('import-kind').onchange=()=>{localArchivePaths=[];$('import-source').value='';$('import-pick').textContent=$('import-kind').value==='zip'?'选择压缩包（可多选）':'选择目录';if(typeof updateLocalPlatform==='function')updateLocalPlatform();};
$('import-source').oninput=()=>{localArchivePaths=[];};
$('sync-form').onsubmit=event=>{
  event.preventDefault();
  submitAcquisition('workshop-sync-all',{});
};
$('acquisition-tasks').onclick=async event=>{
  const pager=event.target.closest('[data-result-page]');if(pager){event.preventDefault();const id=pager.dataset.resultPage;acquisitionPages.set(id,Math.max(0,(acquisitionPages.get(id)||0)+Number(pager.dataset.delta)));refreshAcquisition();return;}
  const button=event.target.closest('[data-task-view]');
  const job=acquisitionData?.tasks.find(t=>t.id===button?.dataset.taskView);
  if(!job)return;
  if($('game').value!==job.game)$('game').value=job.game;
  await load();
  showSpace('library');
  const asset=S.assets.find(a=>a.internal_id===job.internal_id);
  if(asset)inspect(asset);
};

$('sync-game-settings').onclick=()=>$('game-settings').click();
