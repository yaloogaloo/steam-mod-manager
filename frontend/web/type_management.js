let typeContext=null,typeData=null,inspectorTypeContext=null;
function fillTypeOptions(select,data,selected){
 select.replaceChildren(new Option('未设置类型',''),...data.types.map(t=>new Option(t.name,String(t.type_id))));
 select.value=selected==null?'':String(selected);
}
async function loadEditorTypes(){
 const target=editingAsset;if(!target)return;
 typeContext=null;typeData=null;$('edit-type').disabled=true;document.querySelector('[data-type-action=type-set]').disabled=true;
 const context={game:target.game,snapshot:target.snapshot,token:target.asset.token,internal_id:target.asset.internal_id};
 try{
  const data=await rpc('mod-types',context);
  if(editingAsset!==target||!$('editor').open)return;
  typeContext=context;typeData=data;fillTypeOptions($('edit-type'),data,S.details.type_id);$('type-message').textContent='';
 }catch(e){$('type-message').textContent=e.message;}
 finally{if(editingAsset===target){$('edit-type').disabled=!typeContext;document.querySelector('[data-type-action=type-set]').disabled=!typeContext||S.readOnly;}}
}
function closeInspectorTypeMenu(restoreFocus = true) {
  const menu = $('inspector-type-menu');
  if (!menu?.matches(':popover-open')) return;
  menu.hidePopover();
  $('inspector-type-action')?.setAttribute('aria-expanded', 'false');
  if (restoreFocus) $('inspector-type-action')?.focus({preventScroll: true});
}
async function openInspectorTypePicker(){
 if(S.readOnly||S.busy||!S.current)return;
 const menu=$('inspector-type-menu'),trigger=$('inspector-type-action');
 if(menu.matches(':popover-open'))return closeInspectorTypeMenu();
 if(menu.dataset.pending)return;
 menu.dataset.pending='1';
 closeFileTypeMenu(false);
 const asset=S.current,context={game:$('game').value,snapshot:S.snapshot,token:asset.token,internal_id:asset.internal_id};
 try{
  const data=await rpc('mod-types',context);
  if(context.game!==$('game').value||context.snapshot!==S.snapshot||S.current?.internal_id!==asset.internal_id||trigger!==$('inspector-type-action'))return;
  inspectorTypeContext={context,data};
  const selected=S.details?.type_id==null?'':String(S.details.type_id);
  const options=[['', '未设置类型'],...data.types.map(type=>[String(type.type_id), type.name])];
  menu.innerHTML=options.map(([value,text])=>{
   const on=value===selected;
   const mark=on?icon('check'):'<span class="file-type-check-space"></span>';
   return `<button type="button" role="option" tabindex="-1" data-value="${escape(value)}" aria-selected="${on}"><span>${escape(text)}</span>${mark}</button>`;
  }).join('');
  menu.onclick=event=>{
   const option=event.target.closest('[role="option"]');
   if(!option||!inspectorTypeContext)return;
   const value=option.dataset.value;
   if(option.getAttribute('aria-selected')==='true')return closeInspectorTypeMenu();
   saveModType(inspectorTypeContext.context,inspectorTypeContext.data,value,true);
  };
  trigger.setAttribute('aria-expanded','true');
  menu.showPopover();
  placeFileTypeMenu(menu,trigger);
  (menu.querySelector('[aria-selected="true"]')||menu.querySelector('[role="option"]'))?.focus({preventScroll:true});
 }catch(e){$('results').hidden=false;$('results').textContent=e.message;}
 finally{delete menu.dataset.pending;}
}
async function saveModType(context,data,raw,fromInspector){
 if(S.readOnly||S.busy)return;S.busy=true;
 const message=fromInspector?null:$('type-message'),target=editingAsset;
 if(message)message.textContent='正在保存类型…';
 if($('edit-type'))$('edit-type').disabled=true;
 if(fromInspector)closeInspectorTypeMenu(false);
 try{
  await rpc('type-set',{...context,version:data.version,type_id:raw?Number(raw):null},true);
  if(context.game!==$('game').value||context.snapshot!==S.snapshot)return;
  if(!await refreshCommandView())return;
  if(!fromInspector&&editingAsset===target){
   const asset=S.assets.find(a=>a.internal_id===context.internal_id);
   if(asset){editingAsset={asset,snapshot:S.snapshot,game:context.game,epoch:S.epoch};await loadEditorTypes();}
  }
  if(message)message.textContent='类型已保存；用户标签未改变。';
 }catch(e){
  if(fromInspector){$('results').hidden=false;$('results').textContent='保存失败：'+e.message;}
  else if(message)message.textContent='保存失败：'+e.message;
 }
 finally{S.busy=false;if($('edit-type'))$('edit-type').disabled=false;syncFavoriteControls();if(S.current&&S.details)drawInspector(S.current,S.details);
  if(fromInspector&&S.current?.internal_id===context.internal_id)$('inspector-type-action')?.focus({preventScroll:true});}
}
document.addEventListener('click',event=>{
 if(event.target.closest('[data-type-action="type-set"]')&&typeContext&&typeData)saveModType(typeContext,typeData,$('edit-type').value,false);
});

// Game catalog management reuses the original Type ID service.
let catalogContext=null,catalogData=null,catalogBusy=false;
function drawTypeCatalog(){
 const readOnly=S.readOnly||catalogBusy;
 $('type-catalog-list').innerHTML=catalogData.types.map(t=>`<div class="org-input-row" style="margin:10px 0"><input data-catalog-name="${t.type_id}" value="${escape(t.name)}" maxlength="100" ${readOnly?'disabled':''}><button type="button" data-catalog-rename="${t.type_id}" ${readOnly?'disabled':''}>重命名</button><button type="button" data-catalog-delete="${t.type_id}" ${readOnly?'disabled':''}>删除</button></div>`).join('');
 $('type-catalog-add').disabled=$('type-catalog-name').disabled=readOnly;
}
$('type-catalog-close').onclick=()=>{if(!catalogBusy)$('type-catalog-dialog').close();};
$('type-catalog-dialog').addEventListener('cancel',e=>{if(catalogBusy)e.preventDefault();});
async function mutateTypeCatalog(action,fields){
 if(catalogBusy||S.busy||S.readOnly||!catalogContext)return;
 if(catalogContext.epoch!==S.epoch||catalogContext.game!==$('game').value){$('type-catalog-status').textContent='游戏已变化，请重新打开类型目录';return;}
 catalogBusy=true;S.busy=true;drawTypeCatalog();
 try{
  catalogData=await rpc(action,{...catalogContext,version:catalogData.version,...fields},true);
  if(!await refreshCommandView())return;
  catalogContext={game:$('game').value,snapshot:S.snapshot,epoch:S.epoch};
  $('type-catalog-status').textContent='目录已保存；用户标签未改变';$('type-catalog-name').value='';
 }catch(e){$('type-catalog-status').textContent='保存失败：'+e.message;}
 finally{catalogBusy=false;S.busy=false;drawTypeCatalog();syncFavoriteControls();if(S.current&&S.details)drawInspector(S.current,S.details);}
}
$('type-catalog-add').onclick=()=>mutateTypeCatalog('type-create',{name:$('type-catalog-name').value});
$('type-catalog-list').onclick=e=>{
 const rename=e.target.closest('[data-catalog-rename]'),remove=e.target.closest('[data-catalog-delete]');
 if(rename){const id=Number(rename.dataset.catalogRename);mutateTypeCatalog('type-rename',{type_id:id,name:document.querySelector(`[data-catalog-name="${id}"]`).value});}
 if(remove&&confirm('删除此类型并解除当前游戏的对应绑定？用户标签不受影响。'))mutateTypeCatalog('type-delete',{type_id:Number(remove.dataset.catalogDelete),confirm:true});
};
