// One persistent production inventory; three views, one contextual inspector.
import {registerDraft} from './navigation-guard.js';
import {h,clear} from './dom.js';
import {button,select,input,field,dialog,media} from './production-ui.js';
const BASE_STAGES=[['idea','Idea'],['writing','Writing'],['design','Design/editing'],['review','Review'],['ready','Ready'],['blocked','Blocked']];
const PRIORITIES=['low','normal','high','urgent'];
const cap=s=>s.charAt(0).toUpperCase()+s.slice(1);
const day=s=>s?new Date(s+'T12:00:00').toLocaleDateString(undefined,{month:'short',day:'numeric'}):'No date';
const completion=p=>{const ts=p.production.checklist;return `${ts.filter(t=>t.done).length}/${ts.length}`;};
const approval=v=>v.flags?.includes('approval_stale')?'Approval stale':v.flags?.includes('approval_expired')?'Approval expired':v.approval?'Approved':'Not reviewed';
const release=p=>{const vs=p.variants.filter(v=>!v.dropped);if(vs.some(v=>v.flags?.includes('uncertain')))return 'Delivery uncertain';if(vs.some(v=>v.flags?.includes('approval_stale')))return 'Approval stale';if(vs.some(v=>v.flags?.includes('approval_expired')))return 'Approval expired';if(vs.some(v=>v.status==='published'))return 'Published';if(vs.some(v=>v.status==='failed'))return 'Failed';if(vs.some(v=>v.status==='publishing'))return 'Sending';if(vs.some(v=>v.schedule?.utc))return 'Scheduled';return 'Unscheduled';};
export async function mount(root,ctx,route) {
  let alive=true,posts=[],campaignRows=[],people={items:[],rev:0},workflows={items:[],rev:0},views={items:[],rev:0},sources=[];
  let mode='table',group='status',sort='due_date',selected=null,tab='Brief',busy=false,dragId=null,focusReturn=null;
  let filters={campaign:route?.query.get('campaign')||'',q:'',owner:'',reviewer:'',status:'',priority:'',content_type:'',overdue:'',from:'',to:''};
  const drafts=new Map(),auxDrafts=new Map(),pendingSaves=new Set();
  const aux=(id)=>{if(!auxDrafts.has(id))auxDrafts.set(id,{});return auxDrafts.get(id);};
  const unregisterAux=registerDraft({autoFlush:false,dirty:()=>[...auxDrafts.values()].some(d=>d.task||d.comment),flush:async()=>{for(const [id,d] of auxDrafts){let p=posts.find(p=>p.id===id);if(!p)continue;if(d.task){await taskAction(p,'add',{title:d.task});if(d.task)throw Error('Task was not saved');}if(d.comment){p=posts.find(p=>p.id===id);await ctx.api.post(`posts/${id}/comments`,{expected_rev:p.rev,text:d.comment,reply_to:d.reply||null,anchor:d.anchor||{revision:p.rev}});delete d.comment;}}},discard:()=>auxDrafts.clear()});
  const unregister=registerDraft({dirty:()=>pendingSaves.size>0||[...drafts.values()].some(d=>Object.keys(d).length),flush:async()=>{await Promise.all([...pendingSaves]);for(const [id,d] of drafts)if(Object.keys(d).length){const p=posts.find(p=>p.id===id);if(p)await save(p,structuredClone(d));}},discard:()=>drafts.clear()});
  const checked=new Set(),collapsed=new Set(),error=h('div',{class:'prod-error',role:'alert',hidden:true}),status=h('span',{class:'prod-save',role:'status'},'All changes saved');
  const toolbar=h('div',{class:'production-filters'}),tabbar=h('div',{class:'production-tabs'}),content=h('div',{class:'pipeline-content'}),inspector=h('aside',{class:'production-inspector',hidden:true,'aria-label':'Work inspector'});
  const layout=h('div',{class:'production-layout'},content,inspector),count=h('span',{class:'prod-muted'}),bulk=h('div',{class:'production-bulk',hidden:true});
  root.replaceChildren(h('section',{class:'pipeline-workspace'},h('header',{class:'workspace-heading'},h('div',{},h('h1',{},'Content production'),h('p',{},'Creative work, deadlines and people')),h('div',{class:'prod-actions'},button('People',peopleDialog),button('Workflows',workflowDialog),button('+ New item',()=>createItem(), 'primary'))),tabbar,toolbar,status,bulk,error,layout));
  // Mixed workflows follow workspace definition order; first occurrence of a shared
  // stage ID supplies its position/label. A single-workflow view uses its exact order.
  const stages=(rows=filtered())=>{const ids=new Set(rows.map(p=>p.production.workflow));const applicable=workflows.items.filter(w=>ids.size?ids.has(w.id):w.id==='wf_default');const map=new Map();for(const w of applicable)for(const s of w.stages)if(!map.has(s.id))map.set(s.id,s.label);return [...map];};
  const peopleOptions=()=>[['','Unassigned'],...people.items.map(p=>[p.id,p.name])];
  const person=id=>people.items.find(p=>p.id===id)?.name||'Unassigned';
  const campaignOptions=()=>[['','No campaign'],...campaignRows.map(r=>[r.campaign.id,r.campaign.name]),...posts.filter(p=>p.campaign&&!campaignRows.some(r=>r.campaign.id===p.campaign)).map(p=>[p.campaign,p.campaign]).filter((v,i,a)=>a.findIndex(x=>x[0]===v[0])===i)];
  const filtered=()=>posts.filter(p=>{const d=p.production;return (!filters.campaign||p.campaign===filters.campaign)&&(!filters.q||[p.title,p.copy.base].join(' ').toLowerCase().includes(filters.q.toLowerCase()))&&['owner','reviewer','status','priority'].every(k=>!filters[k]||d[k]===filters[k])&&(!filters.content_type||p.variants.some(v=>v.post_type===filters.content_type))&&(!filters.overdue||(d.due_date&&d.due_date<ctx.today&&d.status!=='ready'))&&(!filters.from||(d.due_date&&d.due_date>=filters.from))&&(!filters.to||(d.due_date&&d.due_date<=filters.to));}).sort((a,b)=>sort==='priority'?PRIORITIES.indexOf(b.production.priority)-PRIORITIES.indexOf(a.production.priority):sort==='title'?a.title.localeCompare(b.title):(a.production.due_date||'9999').localeCompare(b.production.due_date||'9999'));
  function acceptLatest(p,e){
    const latest=e.latest?.post||e.latest;if(latest?.id!==p.id)return;
    posts=posts.map(x=>x.id===p.id?{...x,rev:latest.rev,campaign:latest.campaign||null,production:{...x.production,...latest.production},title:latest.production?.title||latest.idea?.text||latest.copy?.base||'Untitled item'}:x);
  }
  function showError(e,retry,discard=()=>{}) {
    error.hidden=false;error.replaceChildren(h('strong',{},e.status===409?'This item changed while you were editing. ':''),e.message);
    if(e.latest&&retry)error.append(h('p',{},'Your proposed change is preserved. Reload to discard it, or apply these fields to the latest revision.'),button('Reload latest',async()=>{discard();error.hidden=true;await load(true);}),button('Apply my fields to latest',async()=>{const latest=e.latest.post||e.latest;await retry(latest.rev).catch(()=>{});}));
    if(!e.latest&&retry)error.append(button('Retry proposed change',()=>retry().catch(()=>{})));
    ctx.announce(e.message);
  }
  async function save(...args){const task=performSave(...args);pendingSaves.add(task);try{return await task;}finally{pendingSaves.delete(task);}}
  async function performSave(p,fields,rev=p.rev) {
    busy=true;status.textContent='Saving…';error.hidden=true;const focused=document.activeElement?.getAttribute('aria-label'),focusScope=inspector.contains(document.activeElement)?inspector:root;
    try {const res=await ctx.api.post(`posts/${p.id}/production`,{expected_rev:rev,fields});posts=posts.map(x=>x.id===p.id?res.post:x);const draft=drafts.get(p.id);if(draft)for(const [k,v]of Object.entries(fields))if(JSON.stringify(draft[k])===JSON.stringify(v))delete draft[k];status.textContent='All changes saved';draw();if(selected===p.id)await inspect(false);if(focused)[...focusScope.querySelectorAll('[aria-label]')].find(el=>el.getAttribute('aria-label')===focused)?.focus();return res.post;}
    catch(e){status.textContent='Changes not saved';acceptLatest(p,e);draw();if(selected===p.id)await inspect(false);if(focused)[...focusScope.querySelectorAll('[aria-label]')].find(el=>el.getAttribute('aria-label')===focused)?.focus();showError(e,n=>save(p,fields,n),()=>drafts.delete(p.id));throw e;}
    finally{busy=false;}
  }
  const edit=(p,fields)=>{drafts.set(p.id,{...(drafts.get(p.id)||{}),...fields});return save(p,fields).catch(()=>{});};
  async function changeStage(p,value){if(value==='blocked'){const reason=input('Blocked reason',p.production.blocked_reason);dialog('Block work',[field('Reason',reason)],()=>save(p,{status:value,blocked_reason:reason.value}));}else await edit(p,{status:value});}
  async function assign(p,cid){try{let target=campaignRows.find(r=>r.campaign.id===(cid||p.campaign))?.campaign;if(!target)throw Error('Choose a campaign from Campaigns first.');await ctx.api.post(`campaigns/${target.id}/members`,{expected_rev:target.rev,post_revs:{[p.id]:p.rev},add:cid?[p.id]:[],remove:cid?[]:[p.id]});await load(true);}catch(e){acceptLatest(p,e);draw();showError(e);}}
  function renderToolbar(){
    clear(tabbar).append(...['table','board','timeline'].map(m=>button(cap(m),()=>{mode=m;draw();},m===mode?'active':'')),count,
      select('Saved view',[['','Saved views'],...views.items.map(v=>[v.id,v.name])],'',id=>{const v=views.items.find(x=>x.id===id);if(v){filters={...filters,...v.filters};mode=v.mode;group=v.group;sort=v.sort;renderToolbar();draw();}}),button('Save view',saveView));
    tabbar.querySelectorAll('button').forEach(b=>{if(['Table','Board','Timeline'].includes(b.textContent))b.setAttribute('aria-pressed',String(b.textContent.toLowerCase()===mode));});
    const search=input('Search pipeline',filters.q,'search');search.placeholder='Search content';search.addEventListener('input',()=>{filters.q=search.value;draw(false);});
    clear(toolbar).append(search,select('Campaign filter',[['','All campaigns'],...campaignOptions().slice(1)],filters.campaign,v=>{filters.campaign=v;draw(false);}),
      select('Owner filter',[['','All owners'],...peopleOptions().slice(1)],filters.owner,v=>{filters.owner=v;draw(false);}),
      select('Stage filter',[['','All stages'],...stages(posts)],filters.status,v=>{filters.status=v;draw(false);}),
      h('details',{},h('summary',{},'More filters'),h('div',{class:'prod-extra-filters'},
        select('Reviewer filter',[['','All reviewers'],...peopleOptions().slice(1)],filters.reviewer,v=>{filters.reviewer=v;draw(false);}),
        select('Priority filter',[['','All priorities'],...PRIORITIES.map(p=>[p,cap(p)])],filters.priority,v=>{filters.priority=v;draw(false);}),
        select('Content type filter',[['','All types'],...['feed','carousel','reel','story','video','text'].map(x=>[x,cap(x)])],filters.content_type,v=>{filters.content_type=v;draw(false);}),
        field('Overdue only',h('input',{type:'checkbox',checked:!!filters.overdue,onchange:e=>{filters.overdue=e.target.checked?'1':'';draw(false);}})),
        ...['from','to'].map(k=>field('Due '+k,h('input',{type:'date',value:filters[k],'aria-label':'Due '+k,onchange:e=>{filters[k]=e.target.value;draw(false);}}))),
        button('Clear filters',()=>{filters=Object.fromEntries(Object.keys(filters).map(k=>[k,'']));renderToolbar();draw(false);}))),
      select('Group by',[['status','Group: Stage'],['campaign','Group: Campaign'],['owner','Group: Owner']],group,v=>{group=v;draw(false);}),
      select('Sort by',[['due_date','Sort: Due date'],['title','Sort: Title'],['priority','Sort: Priority']],sort,v=>{sort=v;draw(false);}));
  }
  function draw(chrome=true){
    if(!alive)return;const scroll=content.querySelector('.production-board')?.scrollLeft||content.querySelector('.production-table-wrap')?.scrollLeft||0;
    if(chrome)renderToolbar();const rows=filtered();count.textContent=`${rows.length} of ${posts.length} items`;
    clear(content);content.append(mode==='table'?table(rows):mode==='board'?board(rows):timeline(rows));
    const scroller=content.querySelector('.production-board,.production-table-wrap');if(scroller)scroller.scrollLeft=scroll;
    if(!rows.length)content.append(h('div',{class:'production-empty'},h('p',{},posts.length?'No content matches these filters.':'Your production workspace is ready for its first idea.'),button('+ New item',()=>createItem())));
    drawBulk(rows);
  }
  function drawBulk(rows){const ids=rows.filter(p=>checked.has(p.id));bulk.hidden=!ids.length;clear(bulk).append(h('strong',{},`${ids.length} selected in this view`),
    select('Bulk owner',[['','Assign owner'],...peopleOptions().slice(1)],'',v=>bulkSave(ids,{owner:v||null})),
    select('Bulk stage',[['','Move to stage'],...stages().filter(([k])=>k!=='blocked')],'',v=>{if(v)bulkSave(ids,{status:v});}),
    select('Bulk priority',[['','Set priority'],...PRIORITIES.map(x=>[x,cap(x)])],'',v=>{if(v)bulkSave(ids,{priority:v});}),button('Clear selection',()=>{checked.clear();draw();}));}
  async function bulkSave(rows,fields){for(const p of rows){try{await save(posts.find(x=>x.id===p.id),fields);}catch{return;}}checked.clear();draw();}
  function row(p){const d=p.production,title=input(`Title for ${p.title}`,p.title);title.addEventListener('input',()=>drafts.set(p.id,{...(drafts.get(p.id)||{}),title:title.value}));title.addEventListener('change',()=>edit(p,{title:title.value}));
    const due=input(`Due date for ${p.title}`,d.due_date,'date');due.addEventListener('change',()=>edit(p,{due_date:due.value||null}));
    return h('tr',{'data-post':p.id,class:selected===p.id?'selected':''},h('td',{},h('input',{type:'checkbox','aria-label':`Select ${p.title}`,checked:checked.has(p.id),onchange:e=>{e.target.checked?checked.add(p.id):checked.delete(p.id);drawBulk(filtered());}})),
      h('td',{class:'prod-title-cell'},h('div',{},button('',()=>open(p), 'prod-image-button'),title,button('Open',()=>open(p),'prod-open'))),
      h('td',{},select(`Owner for ${p.title}`,peopleOptions(),d.owner,v=>edit(p,{owner:v||null}))),
      h('td',{},select(`Reviewer for ${p.title}`,peopleOptions(),d.reviewer,v=>edit(p,{reviewer:v||null}))),
      h('td',{},stageSelect(p)),h('td',{},due),h('td',{},select(`Priority for ${p.title}`,PRIORITIES.map(x=>[x,cap(x)]),d.priority,v=>edit(p,{priority:v}))),
      h('td',{},select(`Campaign for ${p.title}`,campaignOptions(),p.campaign,v=>assign(p,v))),h('td',{},button(completion(p),()=>{tab='Tasks';open(p);},'prod-task-fraction')),h('td',{},workIn(p)),h('td',{},h('span',{class:'prod-publication'},release(p))));
  }
  function workIn(p){const links=p.production.links||[];const add=button('+ Link',()=>{const value=(prompt('Paste a Canva link, or type a Resolve timeline name')||'').trim();if(!value)return;const url=/^https:\/\//.test(value);const canva=url&&/(^|\.)canva\.com$|canva\.link$/.test(new URL(value).hostname);edit(p,{links:[...links,url?{tool:canva?'canva':'other',label:canva?'Canva design':new URL(value).hostname,url:value}:{tool:'resolve',label:value,url:null}]});},'prod-add-link');return h('div',{class:'prod-links'},...links.map(l=>l.url?h('a',{href:l.url,target:'_blank',rel:'noopener noreferrer',class:'prod-link prod-link-'+l.tool},l.label):h('span',{class:'prod-link prod-link-resolve',title:'Resolve timeline'},l.label)),add);}
  function stageSelect(p){const opts=(workflows.items.find(w=>w.id===p.production.workflow)?.stages||[]).map(s=>[s.id,s.label]);const el=select(`Move ${p.title} to stage`,opts,p.production.status,v=>{el.value=p.production.status;changeStage(p,v);});el.className='prod-stage stage-'+p.production.status;return el;}
  function table(rows){
    const body=h('tbody',{}),groups=new Map();
    for(const p of rows){const key=group==='campaign'?p.campaign||'':p.production[group]||'';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(p);}
    if(group==='status')for(const [k]of stages())if(!groups.has(k))groups.set(k,[]);
    const ordered=group==='status'?[...groups].sort((a,b)=>stages().findIndex(s=>s[0]===a[0])-stages().findIndex(s=>s[0]===b[0])):[...groups];
    for(const [key,items]of ordered){const label=group==='status'?(stages().find(s=>s[0]===key)?.[1]||key):group==='owner'?person(key):campaignOptions().find(c=>c[0]===key)?.[1]||key;
      body.append(h('tr',{class:'prod-group'},h('th',{colspan:'11'},button(`${collapsed.has(key)?'▸':'▾'} ${label}  ${items.length}`,()=>{collapsed.has(key)?collapsed.delete(key):collapsed.add(key);draw(false);}),button('+ New item',()=>createItem(group==='status'?key:'idea',group==='campaign'?key:filters.campaign)))));
      if(!collapsed.has(key))for(const p of items){const el=row(p);el.querySelector('.prod-image-button').append(media(p));el.querySelector('.prod-image-button').setAttribute('aria-label',`Inspect ${p.title}`);body.append(el);}
    }
    return h('div',{class:'production-table-wrap'},h('table',{class:'production-table'},h('caption',{class:'sr-only'},`${rows.length} content items`),h('thead',{},h('tr',{},['','Content','Owner','Reviewer','Production','Due date','Priority','Campaign','Tasks','Work in','Publication'].map(x=>h('th',{scope:'col'},x)))),body));
  }
  function board(rows){return h('div',{class:'production-board','aria-label':'Content production board'},stages().map(([key,label])=>{
    const lane=h('section',{class:'production-lane lane-'+key,'data-stage':key,'aria-label':label+' content',ondragover:e=>{if(dragId){e.preventDefault();lane.classList.add('drop-target');}},ondragleave:e=>{if(!lane.contains(e.relatedTarget))lane.classList.remove('drop-target');},ondrop:e=>{e.preventDefault();lane.classList.remove('drop-target');const p=posts.find(p=>p.id===dragId);dragId=null;if(p)changeStage(p,key);}},h('h2',{},label,h('span',{},rows.filter(p=>p.production.status===key).length)));
    for(const p of rows.filter(p=>p.production.status===key)){const card=h('article',{class:'production-card',draggable:'true','data-post':p.id,ondragstart:e=>{if(e.target.closest('button,input,select,a')){e.preventDefault();return;}dragId=p.id;e.dataTransfer.setData('text/plain',p.id);e.dataTransfer.effectAllowed='move';},ondragend:()=>{dragId=null;document.querySelectorAll('.drop-target').forEach(x=>x.classList.remove('drop-target'));}},media(p),button(p.title,()=>open(p),'production-title'),h('div',{class:'prod-card-meta'},h('span',{},person(p.production.owner)),h('span',{class:p.production.due_date&&p.production.due_date<ctx.today?'prod-overdue':''},day(p.production.due_date))),h('div',{class:'prod-card-meta'},h('span',{},`${completion(p)} tasks`),h('span',{},cap(p.production.priority))),stageSelect(p));lane.append(card);}
    lane.append(button('+ New item',()=>createItem(key),'prod-add'));return lane;}));}
  function timeline(rows){
    const dated=rows.filter(p=>p.production.start_date||p.production.due_date),undated=rows.filter(p=>!p.production.start_date&&!p.production.due_date);
    const milestones=campaignRows.filter(r=>!filters.campaign||r.campaign.id===filters.campaign).flatMap(r=>(r.campaign.milestones||[]).filter(m=>m.date).map(m=>({...m,campaign:r.campaign.name})));
    const values=[...dated.flatMap(p=>[p.production.start_date,p.production.due_date]),...milestones.map(m=>m.date)].filter(Boolean);let start=new Date((values.sort()[0]||ctx.today)+'T00:00:00Z');start.setUTCDate(start.getUTCDate()-((start.getUTCDay()+6)%7));
    const end=new Date((values.at(-1)||ctx.today)+'T00:00:00Z');const days=Math.max(14,Math.ceil((end-start)/86400000)+4),width=Math.max(700,days*32),offset=d=>(new Date(d+'T00:00:00Z')-start)/86400000*32;
    const chart=h('div',{class:'production-timeline',style:{width:(width+220)+'px'}},h('div',{class:'prod-time-axis',style:{'margin-left':'220px',width:width+'px'}},Array.from({length:days},(_,i)=>{const d=new Date(+start+i*86400000);return h('span',{style:{width:'32px'},title:d.toISOString().slice(0,10)},d.getUTCDate()===1||i===0?d.toLocaleDateString(undefined,{month:'short',day:'numeric',timeZone:'UTC'}):d.getUTCDate());})));
    if(milestones.length)chart.append(h('div',{class:'prod-time-row prod-milestone-row'},h('span',{class:'prod-time-name'},'Campaign milestones'),h('div',{class:'prod-time-track',style:{width:width+'px'}},milestones.map(m=>h('span',{class:'prod-milestone-marker',style:{left:offset(m.date)+'px'},title:`${m.campaign}: ${m.title} · ${m.date}`},'◇ ',m.title)))));
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');if(milestones.length)svg.style.top='88px';svg.setAttribute('class','prod-dependencies');svg.setAttribute('width',width);svg.setAttribute('height',dated.length*52);svg.setAttribute('aria-label','Dependency connections');
    dated.forEach((p,i)=>{const d=p.production,a=d.start_date||d.due_date,b=d.due_date||d.start_date;const track=h('div',{class:'prod-time-track',style:{width:width+'px'}},button(`${day(a)} – ${day(b)}`,()=>open(p),'prod-time-bar stage-'+d.status));const bar=track.firstChild;bar.style.left=offset(a)+'px';bar.style.width=Math.max(32,offset(b)-offset(a)+32)+'px';
      for(const v of p.variants){const at=v.schedule?.local?.slice(0,10);if(at&&offset(at)>=0&&offset(at)<=width)track.append(h('span',{class:'prod-publish-marker',style:{left:offset(at)+'px'},title:`Publication: ${at}`},'◆'));}
      chart.append(h('div',{class:'prod-time-row'},button(p.title,()=>open(p),'prod-time-name'),track));
      for(const dep of d.depends_on){const j=dated.findIndex(x=>x.id===dep);if(j<0)continue;const other=dated[j].production,x1=offset(other.due_date||other.start_date)+32,x2=offset(a),y1=j*52+26,y2=i*52+26;const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',`M${x1},${y1} H${x1+12} V${y2} H${x2}`);svg.append(path);}
    });chart.append(svg);
    return h('div',{},h('p',{class:'prod-muted'},`Production dates · ${day(start.toISOString().slice(0,10))} onward. ◆ Publication date`),h('div',{class:'prod-timeline-scroll',tabindex:'0'},chart),
      h('details',{},h('summary',{},'Dates and dependencies (accessible list)'),h('ul',{},dated.map(p=>h('li',{},button(p.title,()=>open(p)),` ${p.production.start_date||'No start'} to ${p.production.due_date||'No due date'}; depends on: ${p.production.depends_on.map(id=>posts.find(x=>x.id===id)?.title||id).join(', ')||'none'}`)))),h('h2',{class:'prod-small-title'},`Unscheduled work · ${undated.length}`),h('div',{class:'prod-undated'},undated.map(p=>button(p.title,()=>open(p)))));
  }
  function createItem(stage='idea',campaign=filters.campaign){
    const title=input('Item title'),c=select('New item campaign',campaignOptions(),campaign),reason=input('Blocked reason');let created=null;
    dialog('New production item',[field('Title',title),field('Campaign',c),stage==='blocked'?field('Blocked reason',reason):null],async()=>{
      if(!title.value.trim())throw Error('Give this item a title');
      if(stage==='blocked'&&!reason.value.trim())throw Error('Describe what is blocking this work');
      if(!created)created=(await ctx.api.post('posts',{idea:title.value,campaign:c.value||null})).post;
      await load(true);const p=posts.find(x=>x.id===created.id);
      if(stage!=='idea')await save(p,{status:stage,workflow:workflows.items.find(w=>w.stages.some(s=>s.id===stage))?.id||'wf_default',...(stage==='blocked'?{blocked_reason:reason.value}:{})});
      tab='Brief';open(posts.find(x=>x.id===created.id));
    },{submit:'Create item'});
  }

  function saveView(){const name=input('View name');dialog('Save shared view',[field('Name',name)],async()=>{views=await ctx.api.post('production-views',{expected_rev:views.rev,item:{name:name.value,filters,group,sort,mode}});renderToolbar();},{note:'This view is available to everyone in this workspace.'});}
  function peopleDialog(){const name=input('Person name'),role=input('Role label');dialog('Workspace people',[h('ul',{},people.items.map(p=>h('li',{},p.name,p.local_role_label?' · '+p.local_role_label:''))),field('Name',name),field('Role label',role)],async()=>{people=await ctx.api.post('people',{expected_rev:people.rev,item:{name:name.value,local_role_label:role.value}});draw();if(selected)inspect(false);},{submit:'Add person',note:'Assignment labels help organize work. They do not grant release approval authority.'});}
  function workflowDialog(){const picker=select('Existing workflow',[['','New workflow'],...workflows.items.map(w=>[w.id,w.name])],'');const name=input('Workflow name'),labels=h('textarea',{'aria-label':'Workflow stages',rows:'7'},BASE_STAGES.map(([id,label])=>id+': '+label).join('\n'));let id=null;picker.addEventListener('change',()=>{const w=workflows.items.find(w=>w.id===picker.value);id=w?.id||null;name.value=w?.name||'';labels.value=(w?.stages||BASE_STAGES.map(([id,label])=>({id,label}))).map(s=>s.id+': '+s.label).join('\n');});dialog('Production workflows',[field('Workflow',picker),field('Name',name),field('Stage ID: label (one per line)',labels)],async()=>{const stages=labels.value.split('\n').filter(Boolean).map(line=>{const i=line.indexOf(':');if(i<1)throw Error('Each stage needs an ID and label separated by a colon');return {id:line.slice(0,i).trim(),label:line.slice(i+1).trim()};});workflows=await ctx.api.post('workflows',{expected_rev:workflows.rev,item:{...(id?{id}:{}),name:name.value,stages,readiness:{checklist:true,dependencies:true}}});draw();},{note:'Ready requires completed tasks and ready dependencies. Move work out of a stage before retiring it.'});}
  async function open(p){selected=p.id;focusReturn=document.activeElement;inspector.hidden=false;layout.classList.add('has-inspector');document.body.classList.add('production-inspecting');await inspect(true);}
  function close(){selected=null;inspector.hidden=true;layout.classList.remove('has-inspector');document.body.classList.remove('production-inspecting');if(focusReturn?.isConnected)focusReturn.focus();else content.querySelector('.prod-open,.production-title')?.focus();}
  async function inspect(focus){
    const p=posts.find(x=>x.id===selected);if(!p)return;const draft=drafts.get(p.id)||{};const d={...p.production,...draft};const remember=(key,value)=>{draft[key]=value;drafts.set(p.id,draft);};
    clear(inspector).append(h('header',{},h('h2',{},p.title),button('Close inspector',close)),h('div',{class:'prod-inspector-media'},media(p)),h('a',{href:`#/post/${p.id}`,class:'prod-composer-link'},'Edit media and copy'),h('div',{class:'prod-inspector-tabs'},['Brief','Tasks','Discussion','Activity'].map(t=>button(t,()=>{tab=t;inspect(false);},tab===t?'active':''))));
    const pane=h('div',{class:'prod-inspector-pane'});inspector.append(pane);
    if(tab==='Brief'){
      const form=h('form',{}),controls={};
      controls.owner=select('Inspector owner',peopleOptions(),d.owner);controls.reviewer=select('Inspector reviewer',peopleOptions(),d.reviewer);controls.workflow=select('Item workflow',workflows.items.map(w=>[w.id,w.name]),d.workflow);
      controls.start_date=input('Start date',d.start_date,'date');controls.due_date=input('Production due date',d.due_date,'date');controls.estimate_minutes=input('Estimate minutes',d.estimate_minutes,'number');
      form.append(stageSelect(p),h('div',{class:'prod-field-grid'},...Object.entries(controls).map(([k,c])=>field(({start_date:'Start date',due_date:'Due date',estimate_minutes:'Estimate (minutes)'})[k]||cap(k),c))));
      for(const [k,c]of Object.entries(controls))c.addEventListener('input',()=>remember(k,k==='estimate_minutes'?(c.value===''?null:Number(c.value)):c.value||null));
      const brief={};for(const key of ['audience','objective','message','cta']){brief[key]=h('textarea',{'aria-label':cap(key),rows:key==='message'?'3':'2'},d.brief[key]||'');brief[key].addEventListener('input',()=>remember('brief',{...d.brief,...Object.fromEntries(Object.entries(brief).map(([k,v])=>[k,v.value]))}));form.append(field(key==='cta'?'Call to action':cap(key),brief[key]));}
      const deps=select('Dependencies',posts.filter(x=>x.id!==p.id).map(x=>[x.id,x.title]),'');deps.multiple=true;deps.size=3;for(const o of deps.options)o.selected=d.depends_on.includes(o.value);deps.addEventListener('change',()=>remember('depends_on',[...deps.selectedOptions].map(o=>o.value)));form.append(field('Depends on',deps));
      const refs=structuredClone(d.sources),sourceBox=h('section',{},h('h3',{},'Source material'));
      function drawSources(){clear(sourceBox).append(h('h3',{},'Source material'));
        refs.forEach((ref,index)=>{const src=sources.find(s=>s.source_id===ref.source_id),row=h('div',{class:'prod-source-row'},h('strong',{},src?.label||ref.label||ref.source_id));
          if(src?.production_reference&&src.previews?.[0])row.append(h('p',{},'Production reference • excluded from social delivery'),h('audio',{controls:true,preload:'metadata',src:src.previews[0].url,'aria-label':'Audio production reference'}),h('a',{href:src.previews[0].url,download:''},'Download production audio'));
          const replace=select(index===0?'Source material':`Source material ${index+1}`,sources.filter(s=>s.source_id===ref.source_id||!refs.some(r=>r.source_id===s.source_id)).map(s=>[s.source_id,s.label]),ref.source_id);
          replace.addEventListener('change',()=>{ref.source_id=replace.value;ref.label=sources.find(s=>s.source_id===replace.value)?.label||replace.value;delete ref.locator;remember('sources',structuredClone(refs));drawSources();});row.append(field('Linked source',replace));
          {const start=input(`Source ${index+1} start seconds`,ref.locator?.start??'','number'),end=input(`Source ${index+1} end seconds`,ref.locator?.end??'','number');start.min=end.min='0';start.step=end.step='0.1';
            const change=()=>{if(start.value===''&&end.value==='')delete ref.locator;else ref.locator={start:start.value===''?null:Number(start.value),end:end.value===''?null:Number(end.value)};remember('sources',structuredClone(refs));};start.addEventListener('input',change);end.addEventListener('input',change);row.append(h('div',{class:'prod-field-grid'},field('Start (seconds)',start),field('End (seconds)',end)));}
          row.append(button(`Remove source ${index+1}`,()=>{refs.splice(index,1);remember('sources',structuredClone(refs));drawSources();}));sourceBox.append(row);});
        const pick=select('Add source material',[['','Choose source'],...sources.filter(s=>!refs.some(r=>r.source_id===s.source_id)).map(s=>[s.source_id,s.label])],'');sourceBox.append(field('Link another source',pick),button('Add source',()=>{const src=sources.find(s=>s.source_id===pick.value);if(!src)return;refs.push({source_id:src.source_id,label:src.label});remember('sources',structuredClone(refs));drawSources();}),h('a',{href:'#/workbench'},'Import source or produce an artifact'));
      }drawSources();form.append(sourceBox);
      form.append(h('button',{type:'submit',class:'primary'},'Save brief'));
      form.addEventListener('submit',async e=>{e.preventDefault();try{await save(p,structuredClone(drafts.get(p.id)||{}));}catch{}});pane.append(form);
    }else if(tab==='Tasks'){
      pane.append(h('p',{class:'prod-muted'},`${completion(p)} complete`));
      for(const t of d.checklist){const checkbox=h('input',{type:'checkbox',checked:t.done,'aria-label':`Complete ${t.title}`,onchange:()=>taskAction(p,'edit',{id:t.id,done:checkbox.checked})});pane.append(h('div',{class:'prod-task'},h('label',{},checkbox,t.title),select(`Task owner for ${t.title}`,peopleOptions(),t.owner,v=>taskAction(p,'edit',{id:t.id,owner:v||null})),h('input',{type:'date',value:t.due_date||'','aria-label':`Task due date for ${t.title}`,onchange:e=>taskAction(p,'edit',{id:t.id,due_date:e.target.value||null})}),button('Remove',()=>taskAction(p,'remove',{id:t.id}))));}
      const title=input('Task title',aux(p.id).task||'');title.addEventListener('input',()=>{aux(p.id).task=title.value;});pane.append(h('form',{onsubmit:e=>{e.preventDefault();taskAction(p,'add',{title:title.value});}},field('New task',title),h('button',{type:'submit'},'Add task')));
    }else if(tab==='Discussion'){
      const list=h('div',{},'Loading discussion…');pane.append(list);const text=h('textarea',{'aria-label':'Comment',rows:3},aux(p.id).comment||''),reply=select('Reply to',[['','New thread']],aux(p.id).reply||'');text.addEventListener('input',()=>{aux(p.id).comment=text.value;});reply.addEventListener('change',()=>{aux(p.id).reply=reply.value;});const anchors=select('Discussion anchor',[['revision','This revision'],...d.checklist.map(t=>['task:'+t.id,'Task: '+t.title]),...[...new Map([...(p.media||[]),...p.variants.flatMap(v=>v.media||[])].map(m=>[m.asset,m])).values()].map((m,i)=>['media:'+m.asset,(m.role==='cover'?'Cover':'Media '+(i+1))+': '+(m.alt||'Attached image or video')])],aux(p.id).anchorChoice||'revision');anchors.addEventListener('change',()=>{aux(p.id).anchorChoice=anchors.value;aux(p.id).anchor={revision:p.rev,...(anchors.value.startsWith('task:')?{task:anchors.value.slice(5)}:anchors.value.startsWith('media:')?{media:anchors.value.slice(6)}:{})};});pane.append(h('form',{onsubmit:async e=>{e.preventDefault();try{await ctx.api.post(`posts/${p.id}/comments`,{expected_rev:p.rev,text:text.value,reply_to:reply.value||null,anchor:{revision:p.rev,...(anchors.value.startsWith('task:')?{task:anchors.value.slice(5)}:anchors.value.startsWith('media:')?{media:anchors.value.slice(6)}:{})}});delete aux(p.id).comment;await inspect(false);}catch(err){showError(err);}}},field('Discussion',text),field('About',anchors),reply,h('button',{type:'submit'},'Add comment')));
      try{const data=await ctx.api.get(`posts/${p.id}/comments`);if(selected!==p.id||tab!=='Discussion')return;clear(list);for(const c of data.comments){reply.append(h('option',{value:c.id},c.text.slice(0,45)));list.append(h('article',{class:'prod-comment'+(c.reply_to?' reply':'')},h('div',{},h('strong',{},c.actor.name),h('time',{},new Date(c.at).toLocaleString())),h('p',{},c.text),c.anchor?h('small',{},`Revision ${c.anchor.revision||'—'}${c.anchor.task?' · Task '+(p.production.checklist.find(t=>t.id===c.anchor.task)?.title||'from this revision'):''}${c.anchor.media?' · Media '+c.anchor.media:''}`):null,button(c.resolved?'Reopen thread':'Resolve thread',async()=>{try{await ctx.api.post(`posts/${p.id}/comment-resolve`,{expected_rev:p.rev,comment_id:c.id,resolved:!c.resolved});inspect(false);}catch(err){showError(err);}})));}if(!data.comments.length)list.append(h('p',{class:'prod-muted'},'Start a discussion about this work.'));}catch(e){showError(e);}
    }else{pane.append(h('p',{},`Revision ${p.rev}`),h('p',{class:'prod-muted'},`Updated ${new Date(p.updated).toLocaleString()}`));try{const data=await ctx.api.get('changes',{cursor:'ev:0'});if(selected===p.id&&tab==='Activity')pane.append(h('ol',{class:'prod-activity'},data.events.filter(e=>e.post===p.id).slice(-30).reverse().map(e=>h('li',{},h('strong',{},e.op.replaceAll('.',' ')),` · ${e.actor.name}`,h('small',{},new Date(e.ts).toLocaleString())))));}catch(e){showError(e);}}
    pane.append(h('section',{class:'prod-release'},h('h3',{},'Publication'),h('p',{},release(p)),p.variants.map(v=>h('p',{},`${v.account}: ${v.flags?.includes('uncertain')?'Delivery uncertain · ':''}${approval(v)}${v.schedule?.local?' · '+v.schedule.local:''}`)),h('a',{href:`#/post/${p.id}`},'Open release review')));
    if(p.review_feedback?.length)pane.append(h('section',{class:'prod-review-feedback'},h('h3',{},'Review feedback'),p.review_feedback.map(f=>h('p',{},f.note||f.kind)),h('a',{href:`#/post/${p.id}`},'Review requested changes')));
    if(focus)inspector.querySelector('button')?.focus();
  }
  async function taskAction(p,op,task,rev=p.rev){busy=true;try{const r=await ctx.api.post(`posts/${p.id}/tasks`,{expected_rev:rev,op,task});posts=posts.map(x=>x.id===p.id?r.post:x);if(op==='add')delete aux(p.id).task;draw();await inspect(false);}catch(e){acceptLatest(p,e);draw();await inspect(false);showError(e,n=>taskAction(p,op,task,n));}finally{busy=false;}}
  async function load(force=false){if(!alive||busy||dragId||(!force&&root.contains(document.activeElement)&&document.activeElement.matches('input,textarea,select')))return;
    try{const [inventory,c,pe,w,v]=await Promise.all([ctx.api.get('production'),ctx.api.get('campaigns'),ctx.api.get('people'),ctx.api.get('workflows'),ctx.api.get('production-views')]);if(!alive)return;posts=inventory.posts;sources=inventory.sources;campaignRows=c.campaigns;people=pe;workflows=w;views=v;draw();if(selected&&!inspector.contains(document.activeElement))inspect(false);}catch(e){showError(e);}}
  const escape=e=>{if(e.key==='Tab'&&selected&&innerWidth<1200&&!document.querySelector('dialog[open]')){const nodes=[...inspector.querySelectorAll('button,input,select,textarea,a[href]')].filter(el=>!el.disabled);const a=nodes[0],b=nodes.at(-1);if(e.shiftKey&&document.activeElement===a){e.preventDefault();b?.focus();}else if(!e.shiftKey&&document.activeElement===b){e.preventDefault();a?.focus();}}if(e.key==='Escape'&&selected&&!document.querySelector('dialog[open]'))close();};document.addEventListener('keydown',escape);
  await load(true);return {refresh:()=>load(),unmount(){unregister();unregisterAux();alive=false;document.body.classList.remove('production-inspecting');document.removeEventListener('keydown',escape);}};
}
