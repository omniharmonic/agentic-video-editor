import {h,clear} from './dom.js';
export async function mount(root,ctx){
  let alive=true,data=null,chosen=new Set(),view=null,busy=false;
  const status=h('p',{'role':'status','aria-live':'polite'}),body=h('div');
  clear(root).append(h('section',{class:'st-page collaboration'},h('h1',{},'Collaborate'),h('p',{},'Share selected drafts. Review incoming changes here before applying them.'),status,body));
  const button=(text,action,disabled=false)=>h('button',{type:'button',disabled,onclick:()=>run(action)},text);
  async function run(fn){if(busy)return;busy=true;try{await fn();status.textContent='';}catch(e){status.textContent=e.message;}finally{busy=false;}}
  async function native(action){if(!window.aveNative)throw Error('Open this workspace in the desktop app to use its native sharing sheets.');status.textContent='Continue in the native sharing sheet.';await window.aveNative.connectionAction(action,{});await load();}
  async function load(){data=await ctx.api.get('collaboration');if(alive)draw();}
  const words=x=>String(x||'').replaceAll('_',' ');
  function draw(){
    clear(body);body.append(h('p',{class:'connections-notice'},data.scope),h('p',{},data.enabled?`Sharing enabled on ${data.interface}`:'Sharing is stopped. Queued work is retained.'),h('div',{class:'connection-actions'},button(data.enabled?'Stop sharing':'Enable sharing',()=>native(data.enabled?'exchangeStop':'exchangeEnable')),button('Invite collaborator',()=>native('exchangeInvite'),!data.enabled||!!data.peer),button('Add collaborator',()=>native('exchangeJoin'),!data.enabled||!!data.peer)));
    if(data.error)body.append(h('p',{role:'alert'},words(data.error)));
    if(data.peer)body.append(h('section',{class:'collab-peer'},h('h2',{},'Paired collaborator'),h('p',{},`Device ${data.peer.identity.node} · User ${data.peer.identity.user}`),h('p',{},`Studio ${data.peer.studio}`),button('Revoke collaborator',()=>native('exchangeRevoke'))));
    if(data.pairing)body.append(h('section',{class:'collab-pairing'},h('h2',{},'Compare before pairing'),h('strong',{class:'collab-code'},data.pairing.code),h('p',{},`Device ${data.pairing.peer.identity.node} · Studio ${data.pairing.peer.studio}`),h('p',{},'Compare the code and identities through your trusted channel. Both people must confirm.'),button('Confirm exact pairing',()=>native('exchangeConfirm')),button('Cancel pairing',()=>native('exchangeCancelPair'))));
    const choices=h('fieldset',{},h('legend',{},'Choose drafts to share'));
    for(const p of data.posts){const input=h('input',{type:'checkbox',checked:chosen.has(p.id),onchange:()=>input.checked?chosen.add(p.id):chosen.delete(p.id)});choices.append(h('label',{class:'collab-choice'},input,h('span',{},p.text||p.id)));}
    choices.append(button('Preview outgoing bundle',async()=>{view=await ctx.api.post('collaboration/prepare-send',{posts:[...chosen],visuals:[],media_ids:[]});outgoing(view);},!data.peer));body.append(choices);
    body.append(h('h2',{},'Outbox'));
    if(!data.outbox.length)body.append(h('p',{},'No bundles sent yet.'));
    for(const row of data.outbox)body.append(h('article',{class:'collab-transfer'},h('strong',{},words(row.state)),h('p',{},`Bundle ${row.bundle_id.slice(0,12)} · Attempt ${row.attempts}`),row.error?h('p',{},words(row.error)):null,!['received','cancelled'].includes(row.state)?button('Cancel future attempts',async()=>{await ctx.api.post('collaboration/cancel',{digest:row.sha256});await load();}):null));
    body.append(h('h2',{},'Large files'),h('div',{class:'connection-actions'},button('Send a file',()=>native('exchangeSendFile'),!data.peer)));
    for(const row of data.files)body.append(h('article',{class:'collab-transfer'},h('strong',{},words(row.state)),h('p',{},`${row.name} · ${Math.round(100*(row.offset||0)/row.bytes)}% of ${row.bytes.toLocaleString()} bytes`),row.error?h('p',{},words(row.error)):null));
    for(const row of data.received_files)body.append(h('article',{class:'collab-transfer'},h('strong',{},row.state==='complete'?'Received and verified':words(row.state)),h('p',{},`${row.name||row.sha256.slice(0,12)} · ${row.bytes.toLocaleString()} bytes`),row.path?h('p',{},`In the exchange folder: ${row.path}`):null));
    body.append(h('h2',{},'Inbox'));
    if(!data.inbox.length)body.append(h('p',{},'No incoming bundles.'));
    for(const row of data.inbox)body.append(h('article',{class:'collab-transfer'},h('strong',{},words(row.review_state)),h('p',{},`Studio ${row.source} · ${row.bytes.toLocaleString()} bytes`),button('Review changes',async()=>incoming(await ctx.api.post('collaboration/preview',{digest:row.transport_sha256})))));
    for(const c of data.conflicts)body.append(h('article',{},h('h3',{},c.post),h('p',{},c.reason),button('Resolve conflict',async()=>conflict(await ctx.api.get('collaboration/conflict/'+encodeURIComponent(c.id))))));
  }
  function surface(title){const section=h('section',{class:'collab-review',tabindex:-1},h('h2',{},title));body.append(section);section.focus();section.scrollIntoView({block:'start'});return section;}
  function content(value){return h('pre',{},value?JSON.stringify(value,null,2):'No local draft');}
  function columns(row){return h('div',{class:'collab-columns'},...['base','local','incoming'].map(k=>h('section',{},h('h3',{},words(k)),content(row[k]))));}
  function outgoing(v){const box=surface('Review outgoing bundle');box.append(h('p',{},`This sends ${v.drafts.length} drafts and ${v.media_bytes.toLocaleString()} selected media bytes to studio ${v.peer.studio}.`),h('p',{},'No approvals, accounts, schedules, licenses or raw recordings are included.'),...v.drafts.map(p=>h('details',{},h('summary',{},p.id),content(p.content))),button('Send bundle',async()=>{await ctx.api.post('collaboration/send',{token:v.token});view=null;await load();}),button('Back',()=>{view=null;draw();}));}
  function incoming(v){view=v;const box=surface('Review incoming changes');box.append(h('p',{},'Apply creates or updates unambiguous drafts and retains conflicts for your decision. Received media remains untrusted.'));
    for(const row of v.comparisons)box.append(h('h3',{},row.post),row.protected?h('p',{},'Publishing history is protected. Create a separate draft to use incoming work.'):null,columns(row));
    for(const dep of v.result.dependencies||[])box.append(h('p',{},`${dep.asset||dep.document.id}: ${dep.missing.length?dep.missing.join('; '):'Exact dependencies present; local licensing/review still required'}`));
    box.append(button('Apply reviewed bundle',async()=>{await ctx.api.post('collaboration/apply',{token:v.token});view=null;await load();}),button('Back',()=>{view=null;draw();}));}
  function conflict(c){view=c;const box=surface('Resolve '+c.post);box.append(h('p',{},c.reason),columns({...c,local:c.current}));const resolve=async(take,text)=>{await ctx.api.post('collaboration/resolve',{id:c.id,take,expected_rev:c.current_rev,...(text===undefined?{}:{text})});view=null;await load();};box.append(button('Keep local',()=>resolve('local')));
    if(c.protected)box.append(button('Create separate draft from incoming',()=>resolve('separate')));
    else{const label=h('label',{},'Merged draft copy'),input=h('textarea',{'aria-label':'Merged draft copy',rows:8,value:c.current.copy.base||''});label.append(input);box.append(button('Use incoming',()=>resolve('incoming')),label,button('Save merged draft',()=>resolve('merge',input.value)));}
  }
  await load();const timer=setInterval(()=>{if(!busy&&!view)load().catch(e=>status.textContent=e.message);},2500);
  return {unmount(){alive=false;clearInterval(timer);},refresh(){if(!view)return load();}};
}
