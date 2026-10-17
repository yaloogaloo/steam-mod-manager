let M=null;
async function openAssetManagement(){
 if(S.busy||S.readOnly||!S.current)return;
 const asset=S.current,context={game:$('game').value,snapshot:S.snapshot,token:asset.token,internal_id:asset.internal_id};
 try{
  const data=await rpc('asset-management',context);
  if(context.game!==$('game').value||S.current?.internal_id!==asset.internal_id)return;
  M={context,data,tab:'files'};$('asset-management-title').textContent=asset.name;renderAssetManagement();$('asset-management-dialog').showModal();
 }catch(e){$('results').hidden=false;$('results').textContent=e.message;}
}
function renderAssetManagement(){
 if(!M)return;const d=M.data,labels={dependency:'依赖',conflict:'冲突关系',addon:'插件',patch:'补丁'};
 document.querySelectorAll('[data-management-tab]').forEach(b=>b.classList.toggle('active',b.dataset.managementTab===M.tab));
 if(M.tab==='files'){
  $('asset-management-body').innerHTML=`<p class="quiet">使用原有文件选择与角色规则；保存不会自动重新部署。</p><div class="org-input-row">${[['all','全选'],['main','仅主文件'],['clear-optional','清除可选'],['default','恢复默认']].map(([key,label])=>`<button data-file-mode="${key}">${label}</button>`).join('')}</div>`+(d.files.files.length?d.files.files.map(f=>`<section class="management-file" data-file-id="${escape(f.id)}"><label class="management-selection"><input type="checkbox" data-file-select ${f.selected_for_deploy?'checked':''}><strong>${escape(f.display_name||f.name||f.filename)}</strong></label><small class="quiet">${escape(f.file_role||'角色未知')} · ${escape(f.path||f.filename)}</small><details><summary>角色与文件说明</summary><div class="org-input-row"><select data-file-role><option value="">选择角色</option><option value="main">Main</option><option value="source">Source（不部署）</option><option value="other">Other</option></select>${d.platform.toLowerCase()==='nexus'?'<select data-file-category><option value="">选择 Nexus 文件分类</option>'+['Main','Optional','Miscellaneous','汉化','Other'].map(c=>`<option>${c}</option>`).join('')+'</select>':''}</div><label>文件说明<textarea data-file-description maxlength="10000" rows="2">${escape(f.metadata?.description||'')}</textarea></label><button data-description-save>保存说明</button></details></section>`).join(''):'<p class="quiet">没有已登记的文件。可先通过“重新识别内容”读取本地归档。</p>');
 }else if(M.tab==='relations'){
  $('asset-management-body').innerHTML=`<div class="org-input-row"><select id="relation-kind">${Object.entries(labels).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select><select id="relation-peer"><option value="">选择同游戏 Mod</option>${d.peers.map(p=>`<option value="${escape(p.internal_id)}">${escape(p.name)}</option>`).join('')}</select><button id="relation-add">添加关系</button></div><details><summary>通过 Workspace ID 添加依赖</summary><div class="org-input-row"><input id="dependency-workspace" placeholder="Workspace ID"><button id="dependency-add">添加依赖</button></div></details>`+Object.entries(d.relations).map(([group,items])=>`<section class="management-file"><h3>${({dependencies:'依赖',conflicts:'冲突关系',addons:'插件',patches:'补丁'})[group]} · ${items.length}</h3>${items.map(r=>`<div class="org-input-row"><span>${escape(r.title||'身份无法解析')}</span><button data-relation-remove="${r.id}">移除关系</button></div>`).join('')||'<p class="quiet">未设置</p>'}</section>`).join('');
 }else{
  $('asset-management-body').innerHTML=`<p class="quiet">用户标记不会自动推断冲突或更改部署状态。启用/禁用遵守原游戏投影规则。</p><label class="management-selection"><input data-enabled type="checkbox" ${d.enabled?'checked':''}>启用 Mod</label>`+[['invalid','已失效',d.status.invalid],['conflict','存在冲突',d.status.conflict_status==='conflict'],['abandoned','已停更',d.abandoned]].map(([key,label,value])=>`<label class="management-selection"><input data-annotation="${key}" type="checkbox" ${value?'checked':''}>${label}</label>`).join('')+`<p class="quiet">${escape(d.status.invalid_reason||d.status.conflict_note||'')}</p><section class="management-file"><h3>部署目标检查</h3><p class="quiet">读取原有部署清单与冲突关系，不修改用户冲突标记或部署状态。</p><button id="conflict-check">检查当前 Mod</button><div id="conflict-results"></div></section>`;
 }
}
async function managementCommand(action,values){
 if(!M||S.busy)return;const target=M,context=M.context;S.busy=true;
 $('asset-management-message').textContent='正在保存…';$('asset-management-body').querySelectorAll('button,input,select,textarea').forEach(b=>b.disabled=true);
 try{
  const data=await rpc(action,{...context,version:target.data.version,...values},true);
  if(M!==target||context.game!==$('game').value)return;
  M.data=data;if(!await refreshCommandView())return;
  const asset=S.assets.find(a=>a.internal_id===context.internal_id);
  if(!asset){$('asset-management-dialog').close();return;}
  M.context={game:context.game,snapshot:S.snapshot,token:asset.token,internal_id:asset.internal_id};renderAssetManagement();$('asset-management-message').textContent=data.message;
 }catch(e){$('asset-management-message').textContent='保存失败：'+e.message;}
 finally{S.busy=false;syncFavoriteControls();$('asset-management-body').querySelectorAll('button,input,select,textarea').forEach(b=>b.disabled=S.readOnly);}
}
$('asset-management-close').onclick=()=>{if(!S.busy)$('asset-management-dialog').close();};
$('asset-management-dialog').addEventListener('cancel',e=>{if(S.busy)e.preventDefault();});
$('asset-management-dialog').onclick=e=>{
 const tab=e.target.closest('[data-management-tab]');if(tab&&!S.busy){M.tab=tab.dataset.managementTab;renderAssetManagement();return;}
 if(e.target.id==='conflict-check')return checkManagementConflicts();
 const mode=e.target.closest('[data-file-mode]');if(mode)return managementCommand('file-selection',{mode:mode.dataset.fileMode});
 const remove=e.target.closest('[data-relation-remove]');if(remove)return managementCommand('relation-remove',{relationship_id:Number(remove.dataset.relationRemove)});
 if(e.target.id==='relation-add')return managementCommand('relation-add',{kind:$('relation-kind').value,target:$('relation-peer').value});
 if(e.target.id==='dependency-add')return managementCommand('dependency-add',{workspace_id:$('dependency-workspace').value});
 const save=e.target.closest('[data-description-save]');if(save){const row=save.closest('[data-file-id]');return managementCommand('file-description',{file_id:row.dataset.fileId,value:row.querySelector('[data-file-description]').value});}
};
$('asset-management-body').onchange=e=>{
 const t=e.target,row=t.closest('[data-file-id]');
 if(t.matches('[data-file-select]'))managementCommand('file-select',{file_id:row.dataset.fileId,value:t.checked});
 else if(t.matches('[data-file-role]')&&t.value)managementCommand('file-role',{file_id:row.dataset.fileId,value:t.value});
 else if(t.matches('[data-file-category]')&&t.value)managementCommand('file-category',{file_id:row.dataset.fileId,value:t.value});
 else if(t.matches('[data-annotation]'))managementCommand('annotation-set',{field:t.dataset.annotation,value:t.checked});
 else if(t.matches('[data-enabled]'))managementCommand('enabled-set',{value:t.checked});
};

async function checkManagementConflicts(){
 if(!M||S.busy)return;const target=M,context={...M.context},button=$('conflict-check');
 button.disabled=true;$('conflict-results').textContent='正在读取部署清单…';
 try{
  const data=await rpc('conflict-report',context);
  if(M!==target||context.game!==$('game').value||context.snapshot!==S.snapshot||S.current?.internal_id!==context.internal_id)return;
  if(!$('conflict-results'))return;
  $('conflict-results').innerHTML=data.conflicts.length?data.conflicts.map(c=>`<div class="management-file"><strong>${escape(c.type)}</strong><p>${escape(c.file)}</p><small class="quiet">${escape((c.workspace_ids||c.mods).join(' · '))}</small></div>`).join(''):'<p class="quiet">未发现部署目标重叠或已声明的冲突关系。此结果不代表游戏内兼容性保证。</p>';
 }catch(e){if(M===target&&$('conflict-results'))$('conflict-results').textContent='检查失败：'+e.message;}
 finally{if(button.isConnected)button.disabled=false;}
}
