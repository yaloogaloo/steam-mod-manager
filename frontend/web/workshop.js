const W={data:null,selected:new Set(),page:0,busy:false,game:null,resultItems:[],resultPage:0,resultGame:null};
function resetWorkshopGame(){if(W.game!==$('game').value&&!W.busy){W.game=$('game').value;W.data=null;W.selected.clear();$('workshop-list').replaceChildren();$('workshop-results').replaceChildren();W.resultItems=[];$('workshop-result-pages').hidden=true;$('workshop-summary').textContent='';$('workshop-status').textContent='先扫描，再确认需要注册或同步的项目。';}renderWorkshop();}
function renderWorkshop(){
 const rows=W.data?.rows||[],page=rows.slice(W.page*100,(W.page+1)*100);
 const labels={new:'待注册',existing:'已存在',content_missing:'待补齐归档',source_missing:'订阅本体缺失',invalid:'资源异常',protected:'受保护'};
 $('workshop-summary').textContent=W.data?`发现 ${rows.length} 项 · 待注册 ${rows.filter(r=>r.status==='new').length} · 已有对象 ${rows.filter(r=>['existing','content_missing','source_missing','protected'].includes(r.status)).length} · 可同步 ${rows.filter(r=>r.enabled).length}`:'';
 $('workshop-list').innerHTML=page.map(row=>`<label class="workshop-row"><input type="checkbox" data-workshop-id="${escape(row.id)}" ${W.selected.has(row.id)?'checked':''} ${!row.enabled||W.busy?'disabled':''}><div><strong>${escape(row.name)}</strong><small>${escape(row.id||'身份未知')} · ${escape(row.reason)}${row.files?` · ${row.files} 个文件`:''}</small></div><span class="badge">${escape(labels[row.status]||row.status)}</span></label>`).join('');
 $('workshop-selected').textContent=`已选择 ${W.selected.size} 项`;$('workshop-start').disabled=W.busy||!W.data?.enabled||!W.selected.size;
 $('workshop-scan').disabled=W.busy||S.readOnly;for(const id of ['workshop-all','workshop-none'])$(id).disabled=W.busy||!W.data;
 $('workshop-prev').disabled=W.busy||W.page===0;$('workshop-next').disabled=W.busy||(W.page+1)*100>=rows.length;$('workshop-page').textContent=rows.length?`${W.page+1} / ${Math.ceil(rows.length/100)} 页 · 每页最多 100 项`:'';
}
async function scanWorkshop(){
 if(W.busy||registrationBusy||S.readOnly)return;
 const context={...registrationContext};$('workshop-scan').disabled=true;$('workshop-status').textContent='正在扫描本地目录并核对真实数据库身份…';
 try{const data=await rpc('workshop-scan',context);if(context.game!==$('game').value||context.epoch!==S.epoch)throw Error('游戏已变化，请重新扫描');W.data=data;W.game=data.game;W.page=0;W.selected=new Set(data.rows.filter(r=>r.enabled&&['new','content_missing'].includes(r.status)).map(r=>r.id));$('workshop-status').textContent=data.reason||'请选择需要处理的项目。已有对象按原时间戳规则检查更新，不按名称合并。';renderWorkshop();}
 catch(error){$('workshop-status').textContent='扫描失败：'+error.message;}
 finally{$('workshop-scan').disabled=false;}
}
$('workshop-scan').onclick=scanWorkshop;
$('workshop-all').onclick=()=>{W.selected=new Set(W.data.rows.filter(r=>r.enabled).map(r=>r.id));renderWorkshop();};
$('workshop-none').onclick=()=>{W.selected.clear();renderWorkshop();};
$('workshop-list').onchange=event=>{const id=event.target.dataset.workshopId;if(!id)return;event.target.checked?W.selected.add(id):W.selected.delete(id);$('workshop-selected').textContent=`已选择 ${W.selected.size} 项`;$('workshop-start').disabled=!W.selected.size||W.busy||!W.data.enabled;};
$('workshop-prev').onclick=()=>{W.page--;renderWorkshop();};$('workshop-next').onclick=()=>{W.page++;renderWorkshop();};
const workshopItemMarkup=(items,game)=>items.slice(0,100).map(item=>`<div class="workshop-item-result"><div><strong>${escape(item.name)}</strong><small>${escape(item.id)} · ${escape(item.message)}</small></div><span class="badge">${escape({success:'已归档注册',updated:'已更新归档',skipped:'已存在／跳过',failed:'失败'}[item.state]||item.state)}</span>${item.internal_id?`<button type="button" data-workshop-view="${escape(item.internal_id)}" data-workshop-game="${escape(game)}">查看 Mod</button>`:''}</div>`).join('')+(items.length>100?'<p class="quiet">此处显示前 100 项，完整结果保留在任务数据中。</p>':'');
$('workshop-start').onclick=async()=>{
 if(W.busy||registrationBusy||!W.data?.enabled||!W.selected.size)return;
 const context={...registrationContext};W.busy=true;registrationBusy=true;renderWorkshop();$('workshop-status').textContent='原生同步正在执行。关闭此弹窗不会取消已提交任务。';$('registration-error').textContent='';
 try{const result=await rpc('workshop-sync',{...context,scan:W.data.scan,ids:[...W.selected]},true);if(context.game===$('game').value&&context.epoch===S.epoch){$('workshop-status').textContent=result.message;W.resultItems=result.items;W.resultGame=context.game;W.resultPage=0;renderWorkshopResults();await load();if(context.game===$('game').value){registrationContext={game:context.game,snapshot:S.snapshot,epoch:S.epoch};W.data=null;W.selected.clear();}}}
 catch(error){$('workshop-status').textContent='同步失败：'+error.message;}
 finally{W.busy=false;registrationBusy=false;renderWorkshop();await refreshAcquisition();}
};
const closeRegistration=$('registration-close').onclick;
$('registration-close').onclick=()=>{if(W.busy)$('registration-dialog').close();else closeRegistration();};
$('registration-dialog').addEventListener('cancel',()=>{if(W.busy)$('registration-dialog').close();});
document.addEventListener('click',async event=>{const button=event.target.closest('[data-workshop-view]');if(!button)return;const game=button.dataset.workshopGame;if(W.busy)return;if($('registration-dialog').open)$('registration-dialog').close();$('game').value=game;await load();if($('game').value!==game)return;showSpace('library');const asset=S.assets.find(a=>a.internal_id===button.dataset.workshopView);if(asset)await inspect(asset);});

function renderWorkshopResults(){
 const count=W.resultItems.length;
 $('workshop-results').innerHTML=workshopItemMarkup(W.resultItems.slice(W.resultPage*100,(W.resultPage+1)*100),W.resultGame);
 $('workshop-result-pages').hidden=!count;
 $('workshop-result-page').textContent=`${W.resultPage+1} / ${Math.ceil(count/100)} 页 · 共 ${count} 项结果`;
 $('workshop-result-prev').disabled=W.resultPage===0;
 $('workshop-result-next').disabled=(W.resultPage+1)*100>=count;
}
$('workshop-result-prev').onclick=()=>{W.resultPage--;renderWorkshopResults();};
$('workshop-result-next').onclick=()=>{W.resultPage++;renderWorkshopResults();};
