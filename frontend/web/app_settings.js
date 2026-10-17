let settingsContext=null;
$('app-settings').onclick=async()=>{try{
 settingsContext={game:$('game').value,snapshot:S.snapshot};const d=await rpc('app-settings');
 $('settings-library').value=d.library;$('settings-proxy-mode').value=d.proxy_mode;$('settings-proxy-url').value=d.proxy_url;
 $('settings-cookie').value='';$('settings-clear-cookie').checked=false;$('settings-credentials').textContent=d.has_cookie?'已保存 Steam Cookie；留空保留现有值。':'未配置 Steam Cookie；公开来源不要求填写。';
 $('settings-save').disabled=!d.writable;$('settings-error').textContent='';$('app-settings-dialog').showModal();
 }catch(e){$('results').hidden=false;$('results').textContent=e.message;}};
$('app-settings-close').onclick=()=>$('app-settings-dialog').close();
$('app-settings-form').onsubmit=async e=>{e.preventDefault();$('settings-save').disabled=true;try{
 const a={...settingsContext,proxy_mode:$('settings-proxy-mode').value,proxy_url:$('settings-proxy-url').value};if($('settings-clear-cookie').checked)a.steam_cookie='';else if($('settings-cookie').value)a.steam_cookie=$('settings-cookie').value;
 const d=await rpc('settings-save',a,true);$('settings-error').textContent=d.message;$('settings-cookie').value='';
 }catch(e){$('settings-error').textContent='保存失败：'+e.message;}finally{$('settings-save').disabled=S.readOnly;}};
