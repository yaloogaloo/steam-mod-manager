let editingAdvanced=null;
function loadAdvancedEditor(){
 editingAdvanced=S.details.advanced||{};
 const labels={steam:'Steam',nexus:'Nexus Mods',github:'GitHub',modio:'mod.io',other:'其他 / 本地'};
 $('edit-platform').replaceChildren(...(S.registrationSources?.[$('game').value]||[]).map(p=>new Option(labels[p]||p,p)));
 $('edit-platform').value=String(editingAdvanced.platform||'').toLowerCase().replace('mod.io','modio');
 $('edit-source-url').value=editingAdvanced.source_url||'';
 $('edit-custom-path').value=editingAdvanced.custom_deploy_path||'';
 $('edit-game-version-label').hidden=!editingAdvanced.witcher3;
 $('edit-game-version').value=editingAdvanced.game_version||'next_gen';
 // Source editing is a primary existing operation, not a hidden migration option.
 $('edit-advanced').open=true;
}
function advancedEditorValues(){
 if(!editingAdvanced)return{};const fields={};
 const oldPlatform=String(editingAdvanced.platform||'').toLowerCase().replace('mod.io','modio');
 if($('edit-platform').value!==oldPlatform)fields.platform=$('edit-platform').value;
 if($('edit-source-url').value!==editingAdvanced.source_url)fields.source_url=$('edit-source-url').value;
 if($('edit-custom-path').value!==editingAdvanced.custom_deploy_path)fields.custom_deploy_path=$('edit-custom-path').value;
 if(editingAdvanced.witcher3&&$('edit-game-version').value!==editingAdvanced.game_version)fields.game_version=$('edit-game-version').value;
 return fields;
}
$('edit-custom-pick').onclick=async()=>{
 const target=editingAsset;if(!target||S.busy)return;$('edit-custom-pick').disabled=true;
 try{const result=await rpc('custom-deploy-pick',{game:target.game,snapshot:target.snapshot,token:target.asset.token,internal_id:target.asset.internal_id},true);if(editingAsset===target&&!result.cancelled)$('edit-custom-path').value=result.path;}
 catch(e){$('edit-error').textContent=e.message;}
 finally{$('edit-custom-pick').disabled=false;}
};
$('edit-custom-clear').onclick=()=>$('edit-custom-path').value='';
