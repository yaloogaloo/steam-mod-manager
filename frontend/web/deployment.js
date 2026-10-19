let deploymentData=null, orderSaving=false, orderDrag=null, orderRead=0, orderFeedbackTimer=null;
let deploymentDetail=null, deploymentDetailRead=0;
$('deployment-nav').onclick=()=>showSpace('deployment');
function orderFeedback(message, transient=false) {
  clearTimeout(orderFeedbackTimer);
  $('deployment-status').textContent=message;
  if(transient){
    const game=$('game').value,epoch=S.epoch;
    orderFeedbackTimer=setTimeout(()=>{
      if(game===$('game').value&&epoch===S.epoch&&!deploymentData?.projection_error&&$('deployment-status').textContent===message)$('deployment-status').textContent='';
    },3500);
  }
}
function closeDeploymentInspector(restoreFocus=true) {
  ++deploymentDetailRead;
  const trigger=deploymentDetail?.trigger;
  const scroll=$('deployment-list').scrollTop,comparisonScroll=deploymentDetail?.comparisonScroll;
  deploymentDetail=null;
  $('deployment-inspector').hidden=true;
  $('deployment-aside').append($('deployment-inspector'));
  document.querySelector('.deployment-help').hidden=false;
  document.querySelector('.deployment-body').classList.remove('inspector-open');
  $('snapshot-comparison').hidden=false;
  $('snapshot-detail-slot').hidden=true;
  $('deployment-list').scrollTop=scroll;
  if(comparisonScroll!==undefined)$('snapshot-comparison').scrollTop=comparisonScroll;
  if(restoreFocus&&trigger?.isConnected)trigger.focus({preventScroll:true});
}
function updateDeploymentCover(asset) {
  if(deploymentDetail?.asset.token===asset.token)$('deployment-inspector-image').innerHTML=coverMarkup(asset,'inspector-cover');
}
function drawDeploymentInspector() {
  const {asset,data}=deploymentDetail;
  $('deployment-inspector').innerHTML=`<div class="inspector-top"><span>资产详情</span><button id="deployment-detail-close" class="icon-button" aria-label="关闭详情侧栏">${icon('close')}</button></div>${inspectorContent(asset,data,'deployment-',true)}`;
  renderInspectorDescription('deployment-inspector-description',data);
  $('deployment-detail-close').onclick=()=>closeDeploymentInspector();
}
async function openDeploymentInspector(iid,trigger) {
  const asset=S.assets.find(a=>a.internal_id===iid);
  if(!asset||S.space!=='deployment')return;
  const scroll=$('deployment-list').scrollTop;
  closeDeploymentInspector(false);
  const context={asset:{...asset,size:null},data:null,game:$('game').value,epoch:S.epoch,view:workbenchView,trigger,read:++deploymentDetailRead,comparisonScroll:$('snapshot-comparison').scrollTop};
  deploymentDetail=context;
  if(workbenchView==='snapshots'){
    $('snapshot-comparison').hidden=true;
    $('snapshot-detail-slot').hidden=false;
    $('snapshot-detail-slot').append($('deployment-inspector'));
  }else{
    document.querySelector('.deployment-help').hidden=true;
    document.querySelector('.deployment-body').classList.add('inspector-open');
  }
  $('deployment-inspector').hidden=false;
  drawDeploymentInspector();queueCover(asset);
  $('deployment-list').scrollTop=scroll;
  try{
    const data=await rpc('detail',{token:asset.token});
    if(deploymentDetail!==context||context.read!==deploymentDetailRead||context.game!==$('game').value||context.epoch!==S.epoch||S.space!=='deployment'||context.view!==workbenchView)return;
    context.data=data;context.asset.size=data.size;
    drawDeploymentInspector();
  }catch(error){
    if(deploymentDetail===context)$('deployment-inspector-description').textContent='详情读取失败：'+error.message;
  }
}
async function refreshDeployment() {
  if(workbenchView==='snapshots')return refreshSnapshots();
  const read=++orderRead,game=$('game').value,epoch=S.epoch;
  try {
    const data=await rpc('deployment',{game,snapshot:S.snapshot});
    if(read!==orderRead||game!==$('game').value||epoch!==S.epoch||S.space!=='deployment'||workbenchView!=='current')return;
    if(!deploymentData||deploymentData.game!==data.game)orderFeedback('');
    deploymentData=data;
    $('summary').textContent=`${data.rows.length} 个已部署 Mod · ${data.backend?(data.enabled?'加载顺序':'排序受限'):'不支持加载顺序'}`;
    document.querySelector('.breadcrumb').innerHTML='工作台 <span>/</span> 部署工作台';
    $('deployment-caption').textContent=data.enabled?'仅显示已部署 Mod · 调整顺序后自动保存':data.reason||'仅显示已部署 Mod';
    $('deployment-caption').title=$('deployment-caption').textContent;
    $('deployment-paths').textContent=`排序：${data.order_path||'未准入'}\n投影：${data.projection_path||'未准入'}`;
    if(data.projection_error)orderFeedback(data.projection_error);
    $('order-retry').hidden=!data.retry_projection;
    $('order-retry').disabled=orderSaving||!data.enabled;
    $('deployment-list').innerHTML=data.rows.map((row,i)=>{
      const asset=S.assets.find(a=>a.internal_id===row.internal_id),disabled=orderSaving||!data.enabled;
      if(asset)queueCover(asset);
      return `<article class="deployment-row" data-order-id="${escape(row.internal_id)}" role="listitem"><button class="drag-handle" draggable="${!disabled}" aria-label="拖动调整顺序" ${disabled?'disabled':''}>⠿</button><span class="order-number">${i+1}</span><div class="order-cover" data-cover="${escape(row.token)}">${asset?coverMarkup(asset):''}</div><div class="order-name"><strong>${escape(row.name)}</strong><small>${escape(row.platform)} · 已部署</small>${row.warning?`<small class="order-warning">${escape(row.warning)}</small>`:''}</div><div class="order-buttons"><button data-order-top="${escape(row.internal_id)}" aria-label="置顶" title="置顶" ${disabled||i===0?'disabled':''}>${icon('move-top')}</button><button data-order-up="${escape(row.internal_id)}" aria-label="上移" ${disabled||i===0?'disabled':''}>↑</button><button data-order-down="${escape(row.internal_id)}" aria-label="下移" ${disabled||i===data.rows.length-1?'disabled':''}>↓</button><button data-order-bottom="${escape(row.internal_id)}" aria-label="置底" title="置底" ${disabled||i===data.rows.length-1?'disabled':''}>${icon('move-bottom')}</button><button data-order-view="${escape(row.internal_id)}">详情</button></div></article>`;
    }).join('')||'<p class="quiet">当前游戏没有已部署 Mod。</p>';
  } catch(error){if(read===orderRead&&game===$('game').value&&epoch===S.epoch&&S.space==='deployment'&&workbenchView==='current')orderFeedback('读取失败：'+error.message);}
}
async function saveOrder(order,retry=false) {
  if(orderSaving||!deploymentData?.enabled)return;
  const context={game:$('game').value,snapshot:S.snapshot,epoch:S.epoch};
  orderSaving=true;
  orderFeedback('正在保存并投影，请稍候…');
  document.querySelectorAll('.deployment-row button,#order-retry').forEach(b=>b.disabled=true);
  try {
    const result=await rpc(retry?'order-retry':'order-save',{...context,version:deploymentData.version,order},true);
    if(context.game===$('game').value&&context.epoch===S.epoch)orderFeedback(result.message,result.success&&!deploymentData.rows.some(row=>row.warning));
  } catch(error) {
    if(context.game===$('game').value&&context.epoch===S.epoch)orderFeedback('操作失败：'+error.message);
  } finally {
    orderSaving=false;
    if(S.space==='deployment')await refreshDeployment();
  }
}
$('order-retry').onclick=()=>saveOrder(deploymentData.order,true);
$('deployment-list').onclick=event=>{
  const button=event.target.closest('button');if(!button)return;
  const extreme=button.dataset.orderTop||button.dataset.orderBottom;
  if(extreme){const order=deploymentData.order.filter(i=>i!==extreme);if(button.dataset.orderTop)order.unshift(extreme);else order.push(extreme);saveOrder(order);return;}
  const iid=button.dataset.orderUp||button.dataset.orderDown;
  if(iid){const order=deploymentData.order.slice(),i=order.indexOf(iid),j=i+(button.dataset.orderUp?-1:1);if(j>=0&&j<order.length){[order[i],order[j]]=[order[j],order[i]];saveOrder(order);}}
  if(button.dataset.orderView)openDeploymentInspector(button.dataset.orderView,button);
};
$('deployment-list').ondragstart=event=>{
  if(orderSaving||!deploymentData?.enabled){event.preventDefault();return;}
  orderDrag=event.target.closest('[data-order-id]')?.dataset.orderId;
  if(orderDrag){event.dataTransfer.setData('text/plain',orderDrag);event.dataTransfer.effectAllowed='move';}
};
$('deployment-list').ondragover=event=>{if(orderDrag&&!orderSaving)event.preventDefault();};
$('deployment-list').ondrop=event=>{
  event.preventDefault();const target=event.target.closest('[data-order-id]')?.dataset.orderId;
  if(orderDrag&&target&&target!==orderDrag&&!orderSaving){const order=deploymentData.order.slice(),from=order.indexOf(orderDrag),to=order.indexOf(target);order.splice(from,1);order.splice(to,0,orderDrag);saveOrder(order);}
  orderDrag=null;
};
$('deployment-list').ondragend=()=>orderDrag=null;
