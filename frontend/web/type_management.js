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
async function openInspectorTypePicker(){
 if(S.readOnly||S.busy||!S.current)return;
 inspectorTypeContext=null;
 const asset=S.current,context={game:$('game').value,snapshot:S.snapshot,token:asset.token,internal_id:asset.internal_id};
 try{
  const data=await rpc('mod-types',context);
  if(context.game!==$('game').value||context.snapshot!==S.snapshot||S.current?.internal_id!==asset.internal_id)return;
  inspectorTypeContext={context,data};fillTypeOptions($('inspector-type-select'),data,S.details?.type_id);
  $('inspector-type-message').textContent='选择后立即保存，不改变用户标签。';
  $('inspector-type-menu').showPopover();positionMenu($('inspector-type-menu'),$('inspector-type-action'));$('inspector-type-select').focus();
 }catch(e){$('results').hidden=false;$('results').textContent=e.message;}
}
async function saveModType(context,data,raw,fromInspector){
 if(S.readOnly||S.busy)return;S.busy=true;
 const message=$(fromInspector?'inspector-type-message':'type-message'),target=editingAsset;
 message.textContent='正在保存类型…';$('inspector-type-select').disabled=$('edit-type').disabled=true;
 try{
  await rpc('type-set',{...context,version:data.version,type_id:raw?Number(raw):null},true);
  if(context.game!==$('game').value||context.snapshot!==S.snapshot)return;
  if(fromInspector)$('inspector-type-menu').hidePopover();
  if(!await refreshCommandView())return;
  if(!fromInspector&&editingAsset===target){
   const asset=S.assets.find(a=>a.internal_id===context.internal_id);
   if(asset){editingAsset={asset,snapshot:S.snapshot,game:context.game,epoch:S.epoch};await loadEditorTypes();}
  }
  message.textContent='类型已保存；用户标签未改变。';
 }catch(e){message.textContent='保存失败：'+e.message;}
 finally{S.busy=false;$('inspector-type-select').disabled=$('edit-type').disabled=false;syncFavoriteControls();if(S.current&&S.details)drawInspector(S.current,S.details);
  if(fromInspector&&S.current?.internal_id===context.internal_id&&!$('inspector-type-menu').matches(':popover-open'))$('inspector-type-action')?.focus({preventScroll:true});}
}
$('inspector-type-select').onchange=()=>{if(inspectorTypeContext)saveModType(inspectorTypeContext.context,inspectorTypeContext.data,$('inspector-type-select').value,true);};
$('inspector-type-menu').addEventListener('toggle',event=>{
 $('inspector-type-action')?.setAttribute('aria-expanded',String(event.newState==='open'));
});
document.addEventListener('keydown',event=>{
 if(event.key==='Escape'&&$('inspector-type-menu').matches(':popover-open')){
  event.preventDefault();event.stopImmediatePropagation();$('inspector-type-menu').hidePopover();
  $('inspector-type-action')?.focus({preventScroll:true});
 }
},true);
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
