let B=null;
async function openBatchEditor(){
 if(S.readOnly||S.busy||!S.selected.size)return;
 const context=orgContext(),ids=S.assets.filter(a=>S.selected.has(a.token)).map(a=>a.internal_id);
 try{
  const data=await rpc('batch-management',{...context,internal_ids:ids});
  if(context.epoch!==S.epoch||context.game!==$('game').value)return;
  B={context,ids,data,tab:'type'};showOrganization(`批量编辑 · ${ids.length} 个 Mod`,'','batch');renderBatchEditor();
 }catch(e){$('results').hidden=false;$('results').textContent=e.message;}
}
function renderBatchEditor(){
 const tabs=`<div class="workbench-tabs">${[['type','Mod 类型'],['platform','来源平台'],['collections','合集关系']].map(([key,label])=>`<button type="button" data-batch-tab="${key}" class="${B.tab===key?'active':''}">${label}</button>`).join('')}</div>`;
 const labels={steam:'Steam',nexus:'Nexus Mods',github:'GitHub',modio:'mod.io',other:'其他 / 本地'};
 let body='';
 if(B.tab==='type')body=`<p class="quiet">为所选 Mod 绑定当前游戏的 Type ID；分类、标签、来源和部署状态保持不变。</p><label>目标类型<select id="batch-type"><option value="keep">请选择</option><option value="">清除类型绑定</option>${B.data.types.map(t=>`<option value="${t.type_id}">${escape(t.name)}</option>`).join('')}</select></label>`;
 else if(B.tab==='platform')body=`<p class="quiet">沿用原批量编辑合同：只修改平台，锁定名称、描述和来源 URL。不会下载内容或重建身份。</p><label>来源平台<select id="batch-platform"><option value="">请选择</option>${(S.registrationSources?.[B.context.game]||[]).map(p=>`<option value="${p}">${labels[p]||escape(p)}</option>`).join('')}</select></label>`;
 else body=`<p class="quiet">仅修改组织关系，不删除 Mod、不部署、不保存加载顺序。</p><div class="org-list">${B.data.collections.map(c=>`<label><span>${escape(c.name)}</span><select data-batch-collection="${c.id}"><option value="">不变</option><option value="add">加入</option><option value="remove">移出</option></select></label>`).join('')||'<p class="quiet">请先在合集视图创建合集。</p>'}</div>`;
 $('organization-body').innerHTML=tabs+body;
 $('organization-body').onclick=e=>{const button=e.target.closest('[data-batch-tab]');if(button&&!S.busy){B.tab=button.dataset.batchTab;renderBatchEditor();}};
}
async function saveBatchEditor(){
 const target=B;if(!target)return;
 const args={internal_ids:target.ids,version:target.data.version};let action;
 if(target.tab==='type'){if($('batch-type').value==='keep')return;action='batch-type-set';args.type_id=$('batch-type').value?Number($('batch-type').value):null;}
 else if(target.tab==='platform'){if(!$('batch-platform').value)return;action='batch-platform-set';args.platform=$('batch-platform').value;}
 else{action='batch-memberships';args.add=[];args.remove=[];for(const choice of document.querySelectorAll('[data-batch-collection]'))if(choice.value)args[choice.value].push(choice.dataset.batchCollection);if(!args.add.length&&!args.remove.length)return;}
 const result=await organizeCommand(action,args,target.context);
 if(result&&B===target)$('organization').close();
}
$('batch-edit').onclick=openBatchEditor;
