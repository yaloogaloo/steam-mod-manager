/* Read-only production performance scenario; never invokes a business command. */
globalThis.perf = {stage:'startup', frames:[], longTasks:[], mutations:[], calls:[], rpc:[], images:[], inputs:[], errors:[], samples:[]};
const pause = ms => new Promise(resolve=>setTimeout(resolve,ms));
const until = async test => {const start=performance.now();while(!test()){if(performance.now()-start>60000)throw Error('Read timeout in '+perf.stage);await pause(25);}};
const paint = () => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
let lastFrame=performance.now();
function perfFrame(now){if(['finished','failed'].includes(perf.stage))return;perf.frames.push([perf.stage,now-lastFrame]);lastFrame=now;requestAnimationFrame(perfFrame);}
requestAnimationFrame(perfFrame);
new PerformanceObserver(list=>{for(const e of list.getEntries())perf.longTasks.push([perf.stage,e.startTime,e.duration]);}).observe({entryTypes:['longtask']});
new MutationObserver(records=>{
 let added=0,removed=0,cardsAdded=0,cardsRemoved=0;
 for(const r of records){for(const [nodes,sign] of [[r.addedNodes,1],[r.removedNodes,-1]])for(const n of nodes){const count=1+(n.querySelectorAll?.('*').length||0);if(sign>0){added+=count;cardsAdded+=n.matches?.('.card')?1:0;}else{removed+=count;cardsRemoved+=n.matches?.('.card')?1:0;}}}
 if(added||removed)perf.mutations.push([perf.stage,added,removed,cardsAdded,cardsRemoved]);
}).observe(document.body,{subtree:true,childList:true});
for(const name of ['renderGrid','reflow','filter','drawInspector','drawDeploymentInspector']){
 const original=globalThis[name];if(!original)continue;
 globalThis[name]=function(...args){const t=performance.now();try{return original.apply(this,args);}finally{perf.calls.push([perf.stage,name,performance.now()-t]);}};
}
const originalRpc=rpc;
rpc=async function(op,args={},write=false){
 const start=performance.now(),stage=perf.stage;
 if(write)throw Error('Performance scenario attempted production write: '+op);
 try{return await originalRpc(op,args,write);}finally{perf.rpc.push([stage,op,performance.now()-start]);}
};
document.addEventListener('load',e=>{if(e.target.tagName==='IMG')perf.images.push([perf.stage,performance.now(),e.target.naturalWidth,e.target.naturalHeight]);},true);
addEventListener('error',e=>perf.errors.push(e.message));
addEventListener('unhandledrejection',e=>perf.errors.push(String(e.reason)));
async function action(name,fn){perf.stage=name;const t=performance.now();await fn();await paint();perf.inputs.push([name,performance.now()-t]);perf.samples.push({stage:name,game:$('game').value,count:S.assets.length,nodes:document.querySelectorAll('*').length,cards:document.querySelectorAll('.card').length,dpr:devicePixelRatio,viewport:[innerWidth,innerHeight],columns:S.columns,width:S.width,cell:S.cell,overflow:document.documentElement.scrollWidth>innerWidth});}
async function game(id){showSpace('library');$('search').value='';$('status').value='all';$('type').value='';$('game').value=id;await load();await until(()=>!$('refresh').disabled);}
async function scroll(id,duration,speed){const el=$(id),start=performance.now();let direction=1,previous=start;await new Promise(resolve=>{function step(now){const dt=now-previous;previous=now;const max=el.scrollHeight-el.clientHeight;if(el.scrollTop>=max-2)direction=-1;if(el.scrollTop<=1)direction=1;el.scrollTop+=direction*speed*dt/1000;if(now-start<duration)requestAnimationFrame(step);else resolve();}requestAnimationFrame(step);});}
async function cycle(index){
 await action('stellaris-load',()=>game('281990'));
 await until(()=>!S.coverActive&&!S.coverQueue.length);
 await action('stellaris-scroll',()=>scroll('viewport',4000,900));
 await action('stellaris-fast-scroll',()=>scroll('viewport',3000,4800));
 await action('selection-burst',async()=>{for(let i=0;i<6;i++){const t=performance.now();inspect(S.rows[(i*7+index)%S.rows.length]);paint().then(()=>perf.inputs.push(['selection-first-paint',performance.now()-t]));await pause(70);}await until(()=>S.details!==null);});
 await action('search-filter',async()=>{closeInspector();for(const q of ['舰','UI','']){$('search').value=q;filter();await paint();}$('status').value='deployed';filter();await paint();$('status').value='all';filter();});
 await action('density',async()=>{$('compact').click();await paint();$('standard').click();});
 await action('larger-library-load',()=>game('431960'));
 await until(()=>!S.coverActive&&!S.coverQueue.length);
 await action('larger-library-scroll',()=>scroll('viewport',4000,1100));
 await action('larger-library-fast-scroll',()=>scroll('viewport',3000,4800));
 await action('return-stellaris',()=>game('281990'));
 await action('deployment',async()=>{$('deployment-nav').click();await until(()=>deploymentData?.game==='281990'&&deploymentData.rows.length===18);await scroll('deployment-list',1600,1200);for(const n of [0,14,1]){document.querySelectorAll('[data-order-view]')[n].click();await until(()=>deploymentDetail?.data);}closeDeploymentInspector(false);});
 await action('snapshots',async()=>{$('snapshot-tab').click();await until(()=>snapshotData?.game==='281990');for(let i=0;i<2;i++){document.querySelector('[data-snapshot-open]')?.click();await until(()=>snapshotData.comparison);document.querySelector('[data-snapshot-detail]')?.click();await until(()=>deploymentDetail?.data);closeDeploymentInspector(false);}$('current-deployment-tab').click();});
 await action('window',async()=>{globalThis.perfWindowRequest=true;await until(()=>perfWindowRequest===false);});
}
globalThis.startPerformance = async (duration) => {
 try{await until(()=>typeof S!=='undefined'&&S.snapshot);const start=performance.now();let i=0;do{await cycle(i++);}while(performance.now()-start<duration*1000);perf.elapsed=performance.now()-start;perf.cycles=i;perf.stage='finished';}
 catch(e){perf.errors.push(e.stack||String(e));perf.stage='failed';}
};
