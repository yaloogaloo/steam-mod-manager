let contentArchivePaths=[];
let registrationDraft=null,registrationContext=null,registrationBusy=false,registrationTarget=null;
function setRegistrationStage(stage){
 $('registration-dialog').dataset.stage=stage;
 $('registration-back').hidden=stage!=='preview';
 for(const el of document.querySelectorAll('[data-registration-step]'))el.setAttribute('aria-current',el.dataset.registrationStep===stage?'step':'false');
}
$('registration-back').onclick=()=>{if(!registrationBusy){setRegistrationStage('input');invalidateRegistration();}};
function updateRegistrationFields(){const kind=$('registration-kind').value;registrationDraft=null;$('registration-preview').hidden=true;$('registration-confirm').hidden=true;$('registration-url-fields').hidden=kind!=='url';$('registration-offline-fields').hidden=kind!=='offline';$('registration-local-fields').hidden=kind!=='local';$('workshop-fields').hidden=kind!=='workshop';$('import-start').hidden=['workshop','offline'].includes(kind);if(kind==='offline')resetOfflineGame();if(kind==='workshop')resetWorkshopGame();$('import-source').required=kind==='local';$('import-title').required=false;updateLocalPlatform();}
function openRegistration(target=null){if(S.readOnly||registrationBusy)return;setRegistrationStage('input');for(const id of ['registration-url','registration-name','local-url','import-source','import-title','import-workshop'])$(id).value='';localArchivePaths=[];contentArchivePaths=[];$('content-package-list').textContent='';$('registration-content-source').value='';const allowed=S.registrationSources?.[$('game').value]||[];for(const option of $('registration-platform').options)option.disabled=!allowed.includes(({Steam:'steam',Nexus:'nexus','mod.io':'modio',GitHub:'github',Other:'other'})[option.value]);$('registration-platform').value=[...$('registration-platform').options].find(o=>!o.disabled)?.value||'Other';$('registration-platform').onchange();$('registration-kind').querySelector('[value=local]').disabled=!allowed.length;for(const option of $('local-platform').options)option.disabled=!allowed.includes(({Steam:'steam',Nexus:'nexus',GitHub:'github','mod.io':'modio',Other:'other'})[option.value]);$('local-platform').value=[...$('local-platform').options].find(o=>!o.disabled)?.value||'Steam';$('registration-kind').querySelector('[value=workshop]').disabled=!allowed.includes('steam');$('local-cover').value='';$('local-offline').value='';registrationTarget=target;registrationDraft=null;registrationContext={game:$('game').value,snapshot:S.snapshot,epoch:S.epoch};$('registration-heading').textContent=target?'为同一 Mod 补齐内容':'添加 Mod';$('registration-game').textContent=$('game').selectedOptions[0].text+(target?' · '+target.name:'');$('registration-kind-label').hidden=!!target;$('registration-kind').value='url';updateRegistrationFields();$('registration-url-fields').hidden=!!target;$('registration-source-note').textContent='所有游戏均可手工注册各平台 URL / ID，无需 API Key；自动抓取与 Workshop 扫描另按官方游戏身份校验。';$('registration-content-fields').hidden=!target;$('import-start').textContent=target?'确认补齐并识别':'预览注册';$('registration-error').textContent='';if(!target)restoreRegistrationChoices();$('registration-dialog').showModal();refreshAcquisition();}
$('library-add').onclick=$('acquisition-add').onclick=()=>openRegistration();
$('registration-kind').onchange=updateRegistrationFields;
$('registration-platform').onchange=()=>{
 const platform=$('registration-platform').value;
 $('registration-url-label').textContent=platform==='Steam'?'Workshop ID / 来源链接':platform==='Other'?'来源链接（可选）':'来源 URL / ID';
 $('registration-url').placeholder=({Steam:'Workshop ID 或 Steam 创意工坊链接',Nexus:'https://www.nexusmods.com/游戏/mods/123','mod.io':'https://mod.io/g/游戏/m/标识',GitHub:'https://github.com/owner/repository',Other:'来源链接可选；没有本体时只登记本地身份'})[platform];
 invalidateRegistration();
};
$('registration-close').onclick=()=>{if(!registrationBusy)$('registration-dialog').close();};
$('registration-dialog').addEventListener('cancel',e=>{if(registrationBusy)e.preventDefault();});
setInterval(()=>{for(const id of ['library-add','acquisition-add'])$(id).disabled=S.readOnly||registrationBusy;},350);
async function registrationOperation(action,args){
 if(registrationBusy)return;registrationBusy=true;$('content-cancel').hidden=action!=='content-add';$('import-start').disabled=$('registration-confirm').disabled=true;$('registration-error').textContent='正在执行，请等待…';
 const context=registrationContext;
 try{const result=await rpc(action,{...context,...args},true);if(context.game===$('game').value&&context.epoch===S.epoch){$('registration-error').textContent=result.message;if(action==='content-open'){$('game-caption').textContent=result.message;return result;}await load();if(context.game!==$('game').value||S.epoch!==context.epoch+1)return result;const asset=S.assets.find(a=>a.internal_id===result.internal_id);if(asset){showSpace('library');await inspect(asset);}$('registration-dialog').close();$('acquisition-message').textContent=result.message;$('game-caption').textContent=result.message;}return result;}
 catch(error){if(context.game===$('game').value){$('registration-error').textContent='操作失败：'+error.message;$('game-caption').textContent='操作失败：'+error.message;}}
 finally{$('content-cancel').hidden=true;registrationBusy=false;for(const id of ['library-add','acquisition-add'])$(id).disabled=S.readOnly;$('import-start').disabled=$('registration-confirm').disabled=false;await refreshAcquisition();}
}
$('import-form').onsubmit=async event=>{
 event.preventDefault();if(registrationBusy||S.readOnly)return;if($('registration-kind').value==='workshop')return scanWorkshop();if($('registration-kind').value==='offline')return parseOfflinePages();
 if(registrationTarget){return registrationOperation('content-add',{token:registrationTarget.token,internal_id:registrationTarget.internal_id,source:$('registration-content-source').value,archive_paths:contentArchivePaths});}
 registrationDraft=null;$('registration-confirm').hidden=true;const kind=$('registration-kind').value,context=registrationContext;
 registrationBusy=true;$('import-start').disabled=true;$('registration-error').textContent='正在读取输入并核对重复对象…';
 try{
 const args={...context,kind,platform:kind==='local'?$('local-platform').value:$('registration-platform').value,url:kind==='local'?$('local-url').value:$('registration-url').value,title:kind==='local'?($('import-kind').value==='batch'?'':$('import-title').value):kind==='offline'?'':$('registration-name').value,source:kind==='offline'?$('registration-offline').value:$('import-source').value,format:$('import-kind').value,...(kind==='local'?{cover_source:$('local-cover').value,offline_html_path:$('local-offline').value}:{}),...(kind==='local'&&$('import-kind').value==='zip'&&localArchivePaths.length?{archive_paths:localArchivePaths}:{}),workshop_id:$('import-workshop').value};
 const preview=await rpc('registration-preview',args);
 if(context.game!==$('game').value||context.epoch!==S.epoch)throw Error('游戏已变化，请重新打开添加流程');
 registrationDraft={preview,args};setRegistrationStage('preview');$('registration-preview').hidden=false;$('registration-preview').innerHTML=`<h3>确认注册信息</h3><dl class="facts"><dt>目标游戏</dt><dd>${escape($('game').selectedOptions[0].text)}</dd><dt>来源 / 身份</dt><dd>${escape(preview.platform)} · ${escape(preview.external_id||'注册时分配本地身份')}</dd><dt>名称</dt><dd>${escape(preview.title||'由原来源规则自动命名')}</dd><dt>本体</dt><dd>${preview.has_content?'有本地内容':'无本体 · 待补齐内容'}</dd><dt>封面</dt><dd>${escape(preview.attachments?.cover_source||'未选择；不会覆盖已有对象的用户封面')}</dd><dt>附加离线网页</dt><dd>${escape(preview.attachments?.offline_html_path||'未选择')}</dd><dt>重复检查</dt><dd>${preview.duplicate?'已有对象：仅关联，不覆盖':'未发现重复对象'}</dd></dl><p>${escape(preview.operation)}</p><p class="quiet">${escape(preview.metadata_note)}</p><div class="registration-description">${escape(preview.description||'未获取来源描述；不会伪造元数据。')}</div>${preview.batch_candidates?.length?`<h3>发现 ${preview.batch_candidates.length} 个子目录</h3><div class="org-list">${preview.batch_candidates.slice(0,100).map(p=>`<p>${escape(p.name)}</p>`).join('')}${preview.batch_candidates.length>100?'<p>其余项目将在原 Worker 中继续处理，任务列表提供逐项结果。</p>':''}</div>`:''}`;$('registration-confirm').hidden=false;$('registration-confirm').textContent=preview.duplicate?'查看已有 Mod':'确认注册';$('registration-error').textContent='';
 }catch(error){$('registration-error').textContent='预览失败：'+error.message;$('acquisition-message').textContent='操作失败：'+error.message;}
 finally{$('content-cancel').hidden=true;registrationBusy=false;for(const id of ['library-add','acquisition-add'])$(id).disabled=S.readOnly;$('import-start').disabled=false;}
};
$('registration-confirm').onclick=()=>{if(!registrationDraft)return;const {preview,args}=registrationDraft;registrationOperation(args.kind==='local'?(args.format==='zip'?'import-zip':'import-folder'):'registration-save',args.kind==='local'?{source:args.source,title:args.title,batch_directory:args.format==='batch',workshop_id:args.workshop_id,platform:args.platform,url:args.url,archive_paths:args.archive_paths,cover_source:args.cover_source,offline_html_path:args.offline_html_path,registration_draft:preview.draft}:{draft:preview.draft,title:preview.title});};
async function registrationAssetAction(action){const asset=S.current;if(!asset)return;if(action==='content-add')return openRegistration(asset);registrationContext={game:$('game').value,snapshot:S.snapshot,epoch:S.epoch};return registrationOperation(action,{token:asset.token,internal_id:asset.internal_id});}

$('import-form').addEventListener('input',()=>{registrationDraft=null;$('registration-preview').hidden=true;$('registration-confirm').hidden=true;});

for(const [id,action] of [['content-pick-folder','pick-import-folder'],['content-pick-zip','pick-import-zip']]){
 $(id).onclick=async()=>{
  $(id).disabled=true;
  try{const result=await rpc(action,{...registrationContext,multiple:action==='pick-import-zip',completion:true},true);if(!result.cancelled){contentArchivePaths=action==='pick-import-zip'?result.sources:[];$('registration-content-source').value=result.source;$('content-package-list').textContent=contentArchivePaths.length?`${contentArchivePaths.length} 个包：${contentArchivePaths.map(p=>p.split(/[\\/]/).pop()).join('、')}`:'已选择单个目录';}}
  catch(error){$('registration-error').textContent=error.message;}
  finally{$(id).disabled=false;}
 };
}

function updateLocalPlatform(){const steam=$('local-platform').value==='Steam';$('import-kind').querySelector('[value=batch]').disabled=steam;if(steam&&$('import-kind').value==='batch')$('import-kind').value='folder';const batch=$('import-kind').value==='batch';$('import-title').parentElement.hidden=batch;$('import-title').required=false;$('import-kind').querySelector('[value=zip]').textContent=steam?'ZIP 压缩包':'ZIP / 7z / RAR 压缩包';$('import-workshop').parentElement.hidden=!steam;$('import-workshop').required=$('registration-kind').value==='local'&&steam;$('local-url-label').hidden=steam||batch;$('local-attachments').hidden=batch;$('local-offline-label').hidden=$('local-platform').value!=='Nexus';if(batch){$('local-cover').value='';$('local-offline').value='';}else if($('local-platform').value!=='Nexus')$('local-offline').value='';}
$('local-platform').onchange=updateLocalPlatform;

function invalidateRegistration(){registrationDraft=null;$('registration-preview').hidden=true;$('registration-confirm').hidden=true;}
$('registration-local-fields').addEventListener('click',async e=>{
 const pick=e.target.closest('[data-local-attachment]'),clear=e.target.closest('[data-clear-attachment]');
 if(clear){$('local-'+clear.dataset.clearAttachment).value='';invalidateRegistration();return;}
 if(!pick)return;pick.disabled=true;invalidateRegistration();
 try{const result=await rpc('pick-import-'+pick.dataset.localAttachment,registrationContext,true);if(!result.cancelled)$('local-'+pick.dataset.localAttachment).value=result.source;}
 catch(error){$('registration-error').textContent=error.message;}
 finally{pick.disabled=false;}
});

$('content-cancel').onclick=async()=>{try{const result=await rpc('content-cancel',{...registrationContext,token:registrationTarget.token,internal_id:registrationTarget.internal_id},true);$('registration-error').textContent=result.message;}catch(error){$('registration-error').textContent=error.message;}};

$('registration-content-source').oninput=()=>{contentArchivePaths=[];$('content-package-list').textContent='使用手动路径；多包请重新选择';};

function registrationChoices(){return {kind:$('registration-kind').value,platform:$('registration-kind').value==='offline'?'Nexus':$('registration-platform').value,localPlatform:$('local-platform').value,importKind:$('import-kind').value};}
function restoreRegistrationChoices(){
 const saved=S.registrationPreferences?.[$('game').value]||{};
 const restore=(id,value)=>{const option=[...$(id).options].find(o=>o.value===value&&!o.disabled);if(option)$(id).value=option.value;};
 restore('registration-platform',saved.platform);$('registration-platform').onchange();
 restore('registration-kind',saved.kind==='offline'&&$('registration-platform').querySelector('[value=Nexus]').disabled?'url':saved.kind);restore('local-platform',saved.localPlatform);
 updateRegistrationFields();restore('import-kind',saved.importKind);updateLocalPlatform();
}
function rememberRegistrationChoices(){
 if(registrationTarget||registrationBusy||S.readOnly)return;
 const game=$('game').value,choices=registrationChoices();S.registrationPreferences||={};S.registrationPreferences[game]=choices;
 if(typeof bridge?.remember_registration==='function')bridge.remember_registration(game,JSON.stringify(choices));
}
for(const id of ['registration-kind','registration-platform','local-platform','import-kind'])$(id).addEventListener('change',rememberRegistrationChoices);
