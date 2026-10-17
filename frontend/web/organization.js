// User category tags and per-game collections. No deployment or order actions.
let O = null;
const orgContext = () => ({game:$('game').value, snapshot:S.snapshot, epoch:S.epoch});
const currentCollection = () => S.collections.find(c=>c.id===$('collection').value);

function showOrganization(title, body, mode, target = null) {
  if (S.readOnly || !S.organizeCommands || S.busy) return;
  O = {mode, target, context:orgContext()};
  $('organization-title').textContent=title;
  $('organization-body').innerHTML=body;
  $('organization-message').textContent='';
  $('organization-save').hidden=mode==='tags';
  $('organization-save').disabled=false;
  $('organization-save').textContent=mode==='delete'?'删除合集':mode==='members'?'添加成员':'保存';
  if (!$('organization').open) $('organization').showModal();
  $('organization-body').querySelector('input')?.focus();
}
$('organization-close').onclick=$('organization-cancel').onclick=()=>$('organization').close();
$('organization').addEventListener('cancel', event=>{ if(S.busy)event.preventDefault(); });

async function organizeCommand(action, values, context = orgContext()) {
  if (S.readOnly || !S.organizeCommands || S.busy) return null;
  S.busy=true;
  const dialog=O;
  $('organization-save').disabled=true;
  $('organization-body').querySelectorAll('button,input').forEach(e=>e.disabled=true);
  $('organization-message').textContent='正在保存…';
  try {
    const result=await rpc(action,{...values,game:context.game,snapshot:context.snapshot},true);
    if(context.epoch!==S.epoch || context.game!==$('game').value) return null;
    if(result.cancelled){$('organization-message').textContent='已取消，未修改';return null;}
    if(!await refreshCommandView()) return null;
    $('results').hidden=false;
    $('results').textContent=result.changed===0?'内容已存在或已移除，无需重复操作':'已保存';
    if(O && O===dialog) O.context=orgContext();
    return result;
  } catch(error) {
    if(context.game===$('game').value) {
      $('results').hidden=false;$('results').textContent='操作失败：'+error.message;
      if(O===dialog) $('organization-message').textContent='操作失败：'+error.message;
    }
    return null;
  } finally {
    S.busy=false;syncFavoriteControls();
    $('organization-save').disabled=false;
    $('organization-body').querySelectorAll('button,input').forEach(e=>e.disabled=false);
    if(S.current && S.details)drawInspector(S.current,S.details);
    if(S.browse==='collections') renderCollectionTools();
  }
}

async function openTags() {
  if(!S.current || !S.details) return;
  const asset=S.current, context=orgContext();
  try {
    const tags=await rpc('tags',{...context,token:asset.token,internal_id:asset.internal_id});
    if(context.epoch!==S.epoch || S.current?.internal_id!==asset.internal_id) return;
    showOrganization('管理标签', '', 'tags',asset.internal_id);
    O.tags=tags;
    drawTagManager();
  } catch(error) { $('results').hidden=false;$('results').textContent='标签读取失败：'+error.message; }
}
function drawTagManager(query='') {
  const tags=O.tags;
  $('organization-body').innerHTML=`<p class="quiet">用户标签与 Mod 类型分别保存。添加和移除后立即保存。</p><h3>当前标签</h3><div class="tag-chips">${tags.tags.map(t=>`<button type="button" data-remove-tag="${escape(t)}" title="移除 ${escape(t)}">${escape(t)} <span>×</span></button>`).join('')||'<span class="quiet">尚无用户标签</span>'}</div><label>查找或新建标签<div class="org-input-row"><input id="tag-query" maxlength="80" placeholder="例如 Gameplay、Fix、Graphics" value="${escape(query)}"><button type="button" id="tag-add">添加标签</button></div></label><div id="tag-catalog" class="org-list"></div>`;
  const drawCatalog=()=>{
    const query=$('tag-query').value.trim();
    $('tag-catalog').innerHTML=tags.available.filter(t=>t.toLocaleLowerCase().includes(query.toLocaleLowerCase())).map(t=>`<button type="button" data-add-tag="${escape(t)}" ${tags.tags.includes(t)?'disabled':''}>${icon('tags')}<span>${escape(t)}</span><small>${tags.tags.includes(t)?'已添加':'添加'}</small></button>`).join('')||'<p class="quiet">没有匹配标签，可用上方输入新建。</p>';
  };
  $('tag-query').oninput=drawCatalog;
  $('tag-query').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();$('tag-add').click();}};
  $('tag-add').onclick=()=>changeTag('tag-add',$('tag-query').value);
  $('organization-body').onclick=event=>{
    const remove=event.target.closest('[data-remove-tag]'), add=event.target.closest('[data-add-tag]');
    if(remove)changeTag('tag-remove',remove.dataset.removeTag);
    else if(add && !add.disabled)changeTag('tag-add',add.dataset.addTag);
  };
  drawCatalog();
  $('tag-query').focus();
}
async function changeTag(action,label) {
  const target=O?.target, asset=S.assets.find(a=>a.internal_id===target);
  if(!asset || O.mode!=='tags')return;
  const result=await organizeCommand(action,{token:asset.token,internal_id:target,version:O.tags.version,label},O.context);
  if(result && O?.mode==='tags' && O.target===target && $('organization').open) {
    O.tags=result;
    drawTagManager();
    $('organization-message').textContent=result.changed===0?'已有该标签，无需重复添加':'已保存';
  }
}

function renderCollectionTools() {
  const active=S.browse==='collections', c=currentCollection();
  $('collection-tools').hidden=!active;
  if(!active)return;
  const canWrite=!S.readOnly && S.organizeCommands && !S.busy;
  $('collection-tools').innerHTML=c?`<button id="collection-back">← 所有合集</button><strong title="${escape(c.name)}">${escape(c.name)}</strong><span class="quiet">${c.tokens.length} 个 Mod</span><div class="collection-actions"><button id="collection-add-members" ${canWrite?'':'disabled'}>添加成员</button><button id="collection-remove-members" ${canWrite&&S.selected.size?'':'disabled'}>移出所选 ${S.selected.size||''}</button><button id="collection-cover" ${canWrite?'':'disabled'}>封面</button><button id="collection-rename" ${canWrite?'':'disabled'}>重命名</button><button id="collection-delete" ${canWrite?'':'disabled'}>删除合集</button></div>`:`<strong>我的合集</strong><span class="quiet">按游戏整理 · 不参与部署或加载顺序</span><button id="collection-new" class="primary" ${canWrite?'':'disabled'}>新建合集</button>`;
  if(c) {
    $('collection-back').onclick=()=>{$('collection').value='';closeInspector();filter();};
    $('collection-add-members').onclick=()=>openCollectionMembers(c);
    $('collection-remove-members').onclick=async()=>{
      await organizeCommand('collection-remove',{collection_id:c.id,version:c.version,
        internal_ids:S.assets.filter(a=>S.selected.has(a.token)).map(a=>a.internal_id)});
    };
    $('collection-rename').onclick=()=>openCollectionName(c);
    $('collection-cover').onclick=()=>openCollectionCover(c);
    $('collection-delete').onclick=()=>showOrganization('删除合集',`<p>删除「${escape(c.name)}」？</p><p class="quiet">仅删除合集及其组织关系，不删除 Mod，不改变部署状态、manifest 或加载顺序。</p>`,'delete',c);
  } else $('collection-new').onclick=()=>openCollectionName();
}
function renderCollectionMode() {
  renderCollectionTools();
  const list=S.browse==='collections' && !$('collection').value;
  $('collection-list').hidden=!list;
  $('grid').hidden=list;
  if(!list)return false;
  S.rows=[];
  S.selected.clear();
  closeInspector();
  selectionUI();
  $('empty').hidden=true;
  $('results').hidden=true;
  const query=$('search').value.trim().toLocaleLowerCase();
  $('collection-list').innerHTML=S.collections.filter(c=>c.name.toLocaleLowerCase().includes(query)).map(c=>`<button class="collection-tile" draggable="${!S.readOnly}" data-open-collection="${c.id}">${c.cover?`<img class="collection-cover" src="${c.cover}" alt="">`:icon('layers')}<strong>${escape(c.name)}</strong><span>${c.tokens.length} 个 Mod</span><small>打开合集 →</small></button>`).join('')||'<div class="collection-empty"><h2>尚无匹配的合集</h2><p class="quiet">创建合集，把当前游戏的 Mod 整理在一起。</p></div>';
  $('collection-list').onclick=event=>{
    const tile=event.target.closest('[data-open-collection]');
    if(tile){$('collection').value=tile.dataset.openCollection;filter();}
  };
  $('collection-list').ondragstart=e=>{const tile=e.target.closest('[data-open-collection]');if(tile&&!S.readOnly)e.dataTransfer.setData('application/x-smm-collection',tile.dataset.openCollection);};
  $('collection-list').ondragover=e=>{if(!S.readOnly)e.preventDefault();};
  $('collection-list').ondrop=e=>{e.preventDefault();const tile=e.target.closest('[data-open-collection]'),source=e.dataTransfer.getData('application/x-smm-collection');if(tile&&source&&source!==tile.dataset.openCollection)organizeCommand('collection-move',{source,target:tile.dataset.openCollection,versions:S.collections.map(c=>[c.id,c.version])});};
  return true;
}
function openCollectionName(c=null) {
  showOrganization(c?'重命名合集':'新建合集',`<label>合集名称<input id="collection-name" maxlength="100" placeholder="例如：常用体验增强" value="${escape(c?.name||'')}"></label><p class="quiet">仅用于整理当前游戏的 Mod，不包含部署或成员排序。</p>`,c?'rename':'create',c);
}
function openCollectionMembers(c) {
  const candidates=S.assets.filter(a=>!c.tokens.includes(a.token));
  showOrganization('添加合集成员',`<p class="quiet">${escape(c.name)} · 仅列出当前游戏尚未加入的 Mod</p><input id="member-query" placeholder="搜索可添加的 Mod"><div id="member-candidates" class="org-list">${candidates.map(a=>`<label data-member-name="${escape(a.name.toLocaleLowerCase())}"><input type="checkbox" name="member" value="${a.internal_id}"><span>${escape(a.name)}<small>${escape(a.platform)} · ${escape(a.workspace_id)}</small></span></label>`).join('')||'<p class="quiet">当前游戏的所有 Mod 都已加入。</p>'}</div>`,'members',c);
  $('member-query').oninput=()=>{
    const query=$('member-query').value.toLocaleLowerCase();
    document.querySelectorAll('[data-member-name]').forEach(e=>e.hidden=!e.dataset.memberName.includes(query));
  };
}
$('organization-form').onsubmit=async event=>{
  event.preventDefault();
  if(!O || O.mode==='tags')return;
  if(O.mode==='batch')return saveBatchEditor();
  if(O.mode==='cover'){const chosen=$('collection-cover-member').value;if(!chosen){$('organization-message').textContent='请选择有封面的合集成员，或使用本地图片';return;}const result=await organizeCommand('collection-cover-member',{collection_id:O.target.id,version:O.target.version,internal_id:chosen},O.context);if(result)$('organization').close();return;}
  const state=O, c=state.target;
  const action={create:'collection-create',rename:'collection-rename',delete:'collection-delete',members:'collection-add'}[state.mode];
  const values=state.mode==='create'?{name:$('collection-name').value}:
    {collection_id:c.id,version:c.version,...(state.mode==='rename'?{name:$('collection-name').value}:state.mode==='delete'?{confirm:true}:{internal_ids:[...document.querySelectorAll('input[name=member]:checked')].map(e=>e.value)})};
  const result=await organizeCommand(action,values,state.context);
  if(result && O===state){
    $('organization').close();
    if(state.mode==='create'){$('collection').value=result.collection_id;filter();}
  }
};

function openCollectionCover(c){
 const members=S.assets.filter(a=>c.tokens.includes(a.token));
 showOrganization('合集封面',`<p class="quiet">只写入此合集的独立封面；不修改成员 Mod 封面。</p><button type="button" id="collection-cover-local">选择本地图片</button><label>或使用合集成员封面<select id="collection-cover-member"><option value="">选择成员</option>${members.map(a=>`<option value="${escape(a.internal_id)}">${escape(a.name)}</option>`).join('')}</select></label>`,'cover',c);
 $('collection-cover-local').onclick=async()=>{const result=await organizeCommand('collection-cover-pick',{collection_id:c.id,version:c.version},O.context);if(result)$('organization').close();};
}
