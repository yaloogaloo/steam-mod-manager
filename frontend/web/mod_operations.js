'use strict';
async function runInspectorOperation(action){
 if(S.readOnly||S.busy||!S.current)return;
 const asset=S.current,context={game:$('game').value,snapshot:S.snapshot,token:asset.token,internal_id:asset.internal_id},epoch=S.epoch;
 S.busy=true;drawInspector(S.current,S.details);$('results').hidden=false;$('results').textContent='正在执行…';
 try{
  const result=await rpc(action,context,true);
  if(context.game!==$('game').value||epoch!==S.epoch)return;
  if(result.cancelled){$('results').textContent='已取消，未修改';return;}
  if(!['open-source','open-offline'].includes(action)){
   S.covers.clear();await refreshCommandView();
  }
  $('results').textContent=result.message||'已打开离线页面';
 }catch(error){$('results').hidden=false;$('results').textContent='操作失败：'+error.message;}
 finally{S.busy=false;syncFavoriteControls();if(S.current)drawInspector(S.current,S.details);}
}
let deregistration=null;
async function openDeregistration(){
 if(S.readOnly||S.busy||!S.current)return;
 const asset=S.current,context={game:$('game').value,snapshot:S.snapshot,token:asset.token,internal_id:asset.internal_id};
 try{
  const data=await rpc('deregister-preview',context);
  if(context.snapshot!==S.snapshot||S.current?.internal_id!==asset.internal_id)return;
  deregistration={context,data};
  $('deregister-body').innerHTML=`<dl class="facts">${[['Mod',data.name],['游戏',data.game],['来源',data.platform+' · '+data.source_id],['部署状态',({not_deployed:'未部署',deployed:'已部署',failed:'部署失败'})[data.deploy_status]||'状态待确认'],['托管归档',data.path||'无已确认路径']].map(([k,v])=>`<dt>${escape(k)}</dt><dd>${escape(v)}</dd>`).join('')}</dl><p>移除 SMM 注册记录、收藏、标签、关系、合集成员及原有部署记录中的成员关系。</p><label><input type="radio" name="deregister-mode" value="keep" checked>仅注销，保留本地文件</label><p class="quiet">托管归档、侧车、离线页面、封面、manifest、备份和共享 CAS 均不清理。不会取消 Workshop 订阅；再次同步可能重新注册。</p><label><input type="radio" name="deregister-mode" value="cleanup" ${data.cleanup_allowed?'':'disabled'}>注销并清理独占归档</label><p class="quiet">${data.cleanup_allowed?`删除当前 UUID 归档中的 ${data.cleanup_files.length} 个文件（包括该目录内的本体、侧车、封面和离线资源）；共享 CAS、外部备份、Workshop 原文件及游戏目录保留。`:escape(data.cleanup_reason)}</p>`;
  $('deregister-message').textContent=data.reason;$('deregister-confirm').disabled=!data.allowed;$('deregister-dialog').showModal();
  const updateMode=()=>{$('deregister-confirm').textContent=document.querySelector('[name="deregister-mode"]:checked').value==='keep'?'确认注销，保留文件':'确认注销并清理独占归档';};
  $('deregister-body').onchange=updateMode;updateMode();
 }catch(e){$('results').hidden=false;$('results').textContent='无法注销：'+e.message;}
}
$('deregister-close').onclick=()=>{if(!S.busy)$('deregister-dialog').close();};
$('deregister-dialog').addEventListener('cancel',e=>{if(S.busy)e.preventDefault();});
$('deregister-confirm').onclick=async()=>{
 if(!deregistration||S.busy)return;const target=deregistration;S.busy=true;$('deregister-confirm').disabled=true;
 try{
  const result=await rpc('mod-deregister',{...target.context,version:target.data.version,confirm:true,keep_files:document.querySelector('[name="deregister-mode"]:checked').value==='keep'},true);
  if(target.context.game!==$('game').value)return;
  closeInspector();await refreshCommandView();$('deregister-dialog').close();$('results').hidden=false;$('results').textContent=result.message;
 }catch(e){
  $('deregister-message').textContent='操作未完成：'+e.message;
  if(target.context.game===$('game').value){
   try{await refreshCommandView();if(!S.assets.some(a=>a.internal_id===target.context.internal_id)){closeInspector();target.data.allowed=false;}}catch(refreshError){$('deregister-message').textContent+='；列表重新读取失败：'+refreshError.message;}
  }
 }
 finally{S.busy=false;syncFavoriteControls();$('deregister-confirm').disabled=!target.data.allowed;}
};
