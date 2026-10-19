let workbenchView='current',snapshotData=null,snapshotSelected=null,snapshotRead=0,snapshotBusy=false,snapshotEdit=null;
function selectWorkbench(view){closeDeploymentInspector(false);workbenchView=view;$('current-deployment-view').hidden=view!=='current';$('snapshot-view').hidden=view!=='snapshots';$('current-deployment-tab').classList.toggle('active',view==='current');$('snapshot-tab').classList.toggle('active',view==='snapshots');refreshDeployment();}
$('current-deployment-tab').onclick=()=>selectWorkbench('current');
$('snapshot-tab').onclick=()=>selectWorkbench('snapshots');
async function refreshSnapshots(){
 const serial=++snapshotRead,game=$('game').value,epoch=S.epoch;
 if(snapshotData?.game!==game){snapshotSelected=null;snapshotData=null;$('snapshot-status').textContent='';if($('snapshot-dialog').open)$('snapshot-dialog').close();}
 try{
  const data=await rpc('deployment-snapshots',{game,snapshot:S.snapshot,...(snapshotSelected===null?{}:{record_id:snapshotSelected})});
  if(serial!==snapshotRead||game!==$('game').value||epoch!==S.epoch||S.space!=='deployment'||workbenchView!=='snapshots')return;
  snapshotData=data;document.querySelector('.breadcrumb').innerHTML='工作台 <span>/</span> 部署工作台';$('summary').textContent=`${data.current_count} 个已部署 Mod · ${data.records.length} 个快照`;
  $('snapshot-create').disabled=snapshotBusy||!data.enabled;$('snapshot-create').title=data.reason;
  $('snapshot-list').innerHTML=data.records.map(r=>`<article class="snapshot-record ${r.id===snapshotSelected?'active':''}"><button data-snapshot-open="${r.id}"><strong>${escape(r.name)}</strong><small>${r.count} 个成员</small></button><div><button data-snapshot-rename="${r.id}" ${!data.enabled||snapshotBusy?'disabled':''}>重命名</button><button data-snapshot-delete="${r.id}" ${!data.enabled||snapshotBusy?'disabled':''}>删除</button></div></article>`).join('')||'<p class="quiet">还没有部署快照。保存当前成员集合，便于日后比较。</p>';
  const groups=[['common','共同成员'],['snapshot_only','快照有 · 当前无'],['current_only','当前有 · 快照无']];
  $('snapshot-comparison').innerHTML=data.comparison?groups.map(([key,label])=>`<section class="snapshot-group"><h3>${label}<span>${data.comparison[key].length}</span></h3>${data.comparison[key].map(row=>`<div class="snapshot-member"><div><strong>${escape(row.name)}</strong><small>${escape(row.platform)}</small></div>${row.internal_id?`<button data-snapshot-detail="${escape(row.internal_id)}">详情</button>`:'<span class="quiet">历史成员</span>'}</div>`).join('')||'<p class="quiet">无成员</p>'}</section>`).join(''):'<p class="quiet">选择一个快照，查看成员与当前部署的差异。</p>';
  if(data.comparison&&!data.comparison.snapshot_only.length&&!data.comparison.current_only.length)$('snapshot-comparison').insertAdjacentHTML('afterbegin','<p class="snapshot-equal">成员完全一致 · 不比较加载顺序</p>');
 }catch(error){if(snapshotSelected!==null&&error.message.includes('快照不存在')&&serial===snapshotRead){snapshotSelected=null;return refreshSnapshots();}if(serial===snapshotRead&&game===$('game').value&&epoch===S.epoch)$('snapshot-status').textContent='读取失败：'+error.message;}
}
function openSnapshotEditor(action,id=null){
 if(snapshotBusy||!snapshotData?.enabled)return;
 const record=snapshotData.records.find(r=>r.id===id);
 snapshotEdit={action,record_id:id,game:$('game').value,snapshot:S.snapshot,epoch:S.epoch,version:snapshotData.version};
 $('snapshot-dialog-title').textContent=action==='snapshot-delete'?'删除部署快照':action==='snapshot-rename'?'重命名快照':'保存当前部署快照';
 $('snapshot-dialog-note').textContent=action==='snapshot-delete'?`确认删除「${record.name}」？只删除快照及其成员关系，不删除 Mod，不改变部署状态或加载顺序。`:`当前有 ${snapshotData.current_count} 个已部署 Mod。只保存成员集合，不保存顺序。`;
 $('snapshot-name-label').hidden=action==='snapshot-delete';$('snapshot-name').required=action!=='snapshot-delete';$('snapshot-name').value=action==='snapshot-rename'?record.name:'';
 $('snapshot-overwrite-label').hidden=action!=='snapshot-save';$('snapshot-overwrite').checked=false;$('snapshot-error').textContent='';$('snapshot-submit').textContent=action==='snapshot-delete'?'确认删除':'保存';$('snapshot-submit').disabled=false;$('snapshot-dialog').showModal();
}
$('snapshot-create').onclick=()=>openSnapshotEditor('snapshot-save');
$('snapshot-cancel').onclick=()=>{if(!snapshotBusy)$('snapshot-dialog').close();};
$('snapshot-dialog').addEventListener('cancel',event=>{if(snapshotBusy)event.preventDefault();});
$('snapshot-list').onclick=event=>{const b=event.target.closest('button');if(!b)return;if(b.dataset.snapshotOpen){closeDeploymentInspector(false);snapshotSelected=Number(b.dataset.snapshotOpen);refreshSnapshots();}if(b.dataset.snapshotRename)openSnapshotEditor('snapshot-rename',Number(b.dataset.snapshotRename));if(b.dataset.snapshotDelete)openSnapshotEditor('snapshot-delete',Number(b.dataset.snapshotDelete));};
$('snapshot-comparison').onclick=event=>{const button=event.target.closest('[data-snapshot-detail]');if(button)openDeploymentInspector(button.dataset.snapshotDetail,button);};
$('snapshot-form').onsubmit=async event=>{
 event.preventDefault();if(snapshotBusy||!snapshotEdit)return;
 const context={...snapshotEdit};snapshotBusy=true;$('snapshot-submit').disabled=true;$('snapshot-cancel').disabled=true;$('snapshot-error').textContent='正在保存…';
 try{
  const result=await rpc(context.action,{...context,name:$('snapshot-name').value,overwrite:$('snapshot-overwrite').checked},true);
  if(context.game===$('game').value&&context.epoch===S.epoch){snapshotSelected=context.action==='snapshot-delete'?null:result.record_id;$('snapshot-status').textContent=result.message;$('snapshot-dialog').close();}
 }catch(error){if(context.game===$('game').value&&context.epoch===S.epoch)$('snapshot-error').textContent='操作失败：'+error.message;}
 finally{snapshotBusy=false;$('snapshot-submit').disabled=false;$('snapshot-cancel').disabled=false;if(S.space==='deployment'&&workbenchView==='snapshots')await refreshSnapshots();}
};
