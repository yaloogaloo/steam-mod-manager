'use strict';
let gameConfiguration=null, gameConfigContext=null, gameCreating=false;
$('game-settings').onclick=async()=>{
  try{
    gameCreating=false;$('game-create-fields').hidden=false;$('new-game-id').readOnly=true;
    gameConfigContext={game:$('game').value,snapshot:S.snapshot};
    gameConfiguration=await rpc('game-config',gameConfigContext);
    const c=gameConfiguration.config;
    $('new-game-name').value=c.name;$('new-game-id').value=c.app_id;$('new-game-strategy').value=c.deploy_type;
    $('game-config-name').textContent=`${c.name} · ${c.app_id} · ${c.deploy_type}`;
    const labels={install_path:'游戏安装目录',mod_path:'Mod 部署目录',workshop_path:'Workshop 内容目录'};
    $('game-config-fields').innerHTML=Object.entries(labels).map(([key,label])=>`<label>${label}<div class="org-input-row"><input id="game-path-${key}" readonly value="${escape(c[key])}"><button type="button" data-game-path="${key}" ${gameConfiguration.writable?'':'disabled'}>选择目录</button></div></label>`).join('');
    $('game-config-status').textContent='沿用当前游戏部署策略；修改路径不会移动任何 Mod。';
    $('game-config-save').disabled=!gameConfiguration.writable;
    $('game-config-dialog').showModal();
  }catch(e){alert(e.message);}
};
$('game-config-close').onclick=()=>$('game-config-dialog').close();
$('game-config-fields').onclick=async event=>{
  const button=event.target.closest('[data-game-path]');if(!button)return;
  button.disabled=true;
  try{
    const result=await rpc('game-directory-pick',{...gameConfigContext,field:button.dataset.gamePath,creating:gameCreating},true);
    if(!result.cancelled)$('game-path-'+button.dataset.gamePath).value=result.path;
  }catch(e){$('game-config-status').textContent=e.message;}
  finally{button.disabled=false;}
};
$('game-config-save').onclick=async()=>{
  $('game-config-save').disabled=true;
  try{
    const paths=Object.fromEntries(['install_path','mod_path','workshop_path'].map(k=>[k,$('game-path-'+k).value]));
    gameConfiguration=await rpc(gameCreating?'game-create':'game-config-save',{...gameConfigContext,version:gameConfiguration?.version,paths,app_id:$('new-game-id').value.trim(),name:$('new-game-name').value,deploy_type:$('new-game-strategy').value},true);
    if(gameCreating){const created=gameConfiguration.game;const bootstrap=await rpc('bootstrap');S.registrationSources=bootstrap.registrationSources;$('game').replaceChildren(...bootstrap.games.map(g=>new Option(g.name,g.key)));$('game').value=created;syncGamePicker();await load();$('game-config-dialog').close();return;}
    $('game-config-status').textContent=gameConfiguration.message;
    const currentGame=$('game').value,bootstrap=await rpc('bootstrap');
    $('game').replaceChildren(...bootstrap.games.map(g=>new Option(g.name,g.key)));$('game').value=currentGame;syncGamePicker();
  }catch(e){$('game-config-status').textContent=e.message;}
  finally{$('game-config-save').disabled=false;}
};

function openCreateGame(){
 if(S.readOnly)return;
 closeGameMenu(false);gameCreating=true;gameConfiguration=null;
 gameConfigContext={game:$('game').value,snapshot:S.snapshot};
 $('game-create-fields').hidden=false;$('new-game-id').readOnly=false;$('new-game-id').value='';$('new-game-name').value='';
 $('game-config-name').textContent='沿用旧版正整数 AppID 身份；不要求已有部署 Backend。';
 $('game-config-fields').innerHTML=Object.entries({install_path:'游戏安装目录（可选）',mod_path:'Mod 部署目录（可选）',workshop_path:'Workshop 目录（可选）'}).map(([key,label])=>`<label>${label}<div class="org-input-row"><input id="game-path-${key}" readonly><button type="button" data-game-path="${key}">选择目录</button></div></label>`).join('');
 $('game-config-status').textContent='创建后即可登记和管理 Mod；无部署配置时不会开放部署。';$('game-config-save').disabled=false;
 $('game-config-dialog').showModal();$('new-game-name').focus();
}

$('game-path-check').onclick=async()=>{try{const result=await rpc('game-path-check',{...gameConfigContext,creating:gameCreating,paths:{install_path:$('game-path-install_path').value,mod_path:$('game-path-mod_path').value}});$('game-config-status').textContent=result.errors.join('；')||'安装与部署目录均存在；未写入或移动任何文件。';}catch(e){$('game-config-status').textContent=e.message;}};
