import {h,clear} from '/app/shared/dom.js';

const stages=[['sources','Recordings'],['speakers','Speakers'],['mapping','Sync & cameras'],['bounds','Session bounds'],['assembly','Assembly'],['rough','Rough cut'],['sequence','Opening & closing'],['final','Final cut'],['clips','Clips & shorts'],['release','Release']];
const seconds=n=>{n=Number(n)||0;return `${Math.floor(n/3600)?Math.floor(n/3600)+':':''}${String(Math.floor(n/60)%60).padStart(2,'0')}:${(n%60).toFixed(1).padStart(4,'0')}`;};

const durationLabel=n=>n>0&&n<1?`${(n*1000).toFixed(3).replace(/\.?0+$/,'')} ms`:seconds(n);
const omittedInside=c=>Math.max(0,(c?.omitted_inside_seconds||0)-(c?.boundary_rounding||[]).reduce((n,r)=>n+r.end-r.start,0));

export async function openEpisode(ctx,{episodeId=null}={}){
  const previous=document.activeElement,dialog=h('dialog',{class:'st media-workshop episode-workshop','aria-label':'Media · Episode'});
  let id=episodeId,data=null,stage='sources',session='main',selected=null,busy=false,timer=null,closed=false,drafts={},captureDraft=()=>{},refreshGeneration=0;
  const title=h('h2',{},'Episodes'),state=h('p',{class:'mw-status',role:'status'},'Loading episodes…'),error=h('p',{class:'mw-error',role:'alert',hidden:true});
  const rail=h('nav',{class:'ew-stages','aria-label':'Episode stages'}),playerSlot=h('div',{class:'ew-player'}),transcript=h('section',{class:'ew-transcript','aria-label':'Transcript evidence'}),inspector=h('aside',{class:'ew-inspector'}),primary=h('footer',{class:'ew-next'}),sourceTabs=h('nav',{class:'ew-sources','aria-label':'Recording previews'});
  const main=h('div',{class:'ew-main'},h('section',{class:'ew-viewer'},sourceTabs,playerSlot,transcript),inspector);
  dialog.append(h('header',{class:'mw-heading'},h('div',{},title,state),button('Close',()=>dialog.close())),error,rail,main,primary);document.body.append(dialog);dialog.showModal();
  dialog.addEventListener('focusin',e=>{if(inspector.contains(e.target))requestAnimationFrame(()=>{if(document.activeElement!==e.target)return;const r=e.target.getBoundingClientRect(),m=main.getBoundingClientRect();if(r.bottom>m.bottom-24)main.scrollTop+=r.bottom-m.bottom+24;if(r.top<m.top+18)main.scrollTop-=m.top-r.top+18;});});
  dialog.addEventListener('close',()=>{closed=true;clearTimeout(timer);dialog.remove();previous?.focus();});
  function fail(e){error.textContent=e.message;error.hidden=false;}
  function button(label,fn,important=false){return h('button',{type:'button',class:important?'primary':'',onclick:async()=>{error.hidden=true;try{await fn();}catch(e){fail(e);}}},label);}
  function field(label,input){return h('label',{class:'mw-field'},h('span',{},label),input);}
  function choose(label,values,value){const node=h('select',{'aria-label':label},values.map(v=>h('option',{value:v.id},v.label)));node.value=value||values[0]?.id;return node;}
  function recorded(state){return ['reviewed exact','review recorded'].includes(state);}
  function done(kind){return recorded(data?.checkpoints[kind]?.state);}
  function next(){if(data.adoption){if(!done('assembly')||!data.roughs.length)return 'assembly';if(!done('rough')||!data.sequences.length)return 'rough';if(!done('sequence')||!data.finals.length)return 'sequence';return 'final';}if(!data.media||!data.transcript||data.sources.some(s=>!s.preview))return 'sources';if(!done('speakers'))return 'speakers';if(session!=='combined'&&!done('mapping'))return 'mapping';if(session!=='combined'&&!done('bounds'))return 'bounds';if(!done('assembly')||!data.roughs.length)return 'assembly';if(!done('rough')||!data.sequences.length)return 'rough';if(!done('sequence')||!data.finals.length)return 'sequence';return 'final';}
  async function refresh(auto=false){
    const target={id,session},generation=++refreshGeneration,current=()=>!closed&&generation===refreshGeneration&&id===target.id&&session===target.session;let loaded;
    try{loaded=await ctx.api.get(`episodes/${target.id}/view?session=${encodeURIComponent(target.session)}`);}catch(e){if(current())throw e;return;}
    if(!current())return;drafts={};captureDraft=()=>{};data=loaded;if(auto)stage=next();if(!selected||!data.sources.some(s=>s.id===selected))selected=data.sources[0]?.id;draw();
  }
  async function catalog(){
    refreshGeneration++;
    title.textContent='Episodes';state.textContent='Choose the recording session you want to work on.';clear(rail);clear(sourceTabs);clear(playerSlot);delete playerSlot.dataset.artifact;clear(transcript);clear(inspector);clear(primary);
    const list=await ctx.api.get('episodes');playerSlot.className='ew-player ew-empty';
    if(!list.episodes.length)playerSlot.append(h('h3',{},'Bring the conversation into the studio'),h('p',{},'Start from your recordings, or open an existing episode folder on this Mac. Originals stay intact.'));
    for(const row of list.episodes)playerSlot.append(button(row.label,async()=>{id=row.id;await refresh(true);}),h('p',{},row.status==='available'?(row.ownership==='external_reference'?'Linked episode folder':'Managed recording copies'):row.reason),row.status!=='available'&&window.aveNative?.reconnectEpisode?button('Reconnect '+row.label,async()=>{await window.aveNative.reconnectEpisode(row.id);id=row.id;await refresh(true);}):null);
    primary.append(button('Create from recordings',intake,true));
    if(window.aveNative?.selectEpisode)primary.append(button('Open project folder',async()=>{const row=await window.aveNative.selectEpisode();id=row.id;await refresh(true);}));
    else inspector.append(h('p',{},'To link an existing folder, open this workspace in the native app.'));
  }
  async function intake(){
    clear(playerSlot);clear(inspector);clear(primary);clear(transcript);state.textContent='Choose recordings that belong to the same episode.';
    const name=h('input',{type:'text','aria-label':'Episode title',placeholder:'Episode title',maxlength:200}),choices=h('div',{class:'ew-intake-sources'}),selectedSources=new Set();let importing=false;
    playerSlot.append(field('Episode title',name),choices);
    async function rows(){clear(choices);const library=await ctx.api.get('workbench');for(const s of library.sources.filter(s=>['audio','video'].includes(s.kind))){const check=h('input',{type:'checkbox','aria-label':s.label,onchange:e=>e.target.checked?selectedSources.add(s.source_id):selectedSources.delete(s.source_id)});check.checked=selectedSources.has(s.source_id);choices.append(h('label',{class:'ew-check'},check,h('span',{},s.label)));}}
    const upload=h('input',{type:'file',multiple:true,accept:'audio/*,video/*','aria-label':'Import episode recordings'});
    upload.addEventListener('change',async()=>{importing=true;try{for(const file of upload.files){state.textContent='Importing '+file.name;const response=await fetch('/api/studio/workbench/upload?name='+encodeURIComponent(file.name),{method:'PUT',body:file});const result=await response.json();if(!response.ok)throw Error(result.error);selectedSources.add(result.source_id);if(result.job)await waitFor(result.job.id);}await rows();state.textContent='Recordings imported. Choose the episode title and create.';}catch(e){fail(e);}finally{importing=false;}});
    const choose=window.aveNative?button('Choose recordings',async()=>{importing=true;try{await window.aveNative.importFiles();await rows();}finally{importing=false;}}):field('Add recordings',upload);
    inspector.append(choose,h('p',{},'These files are copied into a managed episode. Copying does not establish ownership or usage rights.'));
    primary.append(button('Create episode',async()=>{if(importing)throw Error('Wait for all selected recordings to finish importing.');if(!name.value.trim()||!selectedSources.size)throw Error('Enter a title and choose at least one recording.');await run('episode-create',{title:name.value,sources:[...selectedSources]});},true),button('Back to episodes',catalog));await rows();
  }
  async function waitFor(jid){for(;;){if(closed)throw Error('Episode window closed');const j=await ctx.api.get('workbench/jobs/'+jid);state.textContent=j.phase||j.status;if(['done','failed','interrupted','cancelled'].includes(j.status)){if(j.status!=='done')throw Error(j.diagnostics||j.status);return j;}await new Promise(resolve=>setTimeout(resolve,500));}}
  function lockSubmission(){
    const controls=[...dialog.querySelectorAll('.ew-stages button,.ew-stages select,.ew-inspector input,.ew-inspector select,.ew-inspector textarea,.ew-inspector button,.ew-next button,.ew-main button:not(.ew-transcript button)')];
    const prior=controls.map(node=>[node,node.disabled]);for(const [node] of prior)node.disabled=true;
    return ()=>{for(const [node,disabled] of prior)node.disabled=disabled;};
  }
  async function submit(pending,retry=false){
    if(busy||closed)return;
    busy=true;const unlock=lockSubmission();clear(primary);
    state.textContent='Checking the selected inputs…';
    primary.append(h('span',{role:'status'},'Checking sources and permissions. Playback remains available; this request can take several minutes for large recordings.'));
    let completed=null,failure=null;
    try{
      const active=await ctx.api.post(retry?'workbench/retry':'workbench/'+pending.op,retry?{job_id:pending.job.id}:pending.args);
      pending.job=active;
      if(closed)return;
      clear(primary);const progress=h('span',{role:'status'},'Working locally. You can close this window and return later.');
      const cancel=button('Cancel job',async()=>{cancel.disabled=true;progress.textContent='Stopping the job. Completed outputs are preserved; retry becomes available after it stops.';try{await ctx.api.post('workbench/cancel-media',{job_id:active.id});}catch(error){cancel.disabled=false;progress.textContent='Cancellation was not confirmed. Try again or wait for the current result.';fail(error);}});
      primary.append(progress,cancel);
      completed=await waitFor(active.id);
    }catch(e){failure=e;}
    finally{busy=false;unlock();}
    if(closed)return;
    // Navigation is held while submitting/polling; nevertheless never apply a
    // result to a different episode/session if another caller changed context.
    if(id!==pending.episode||session!==pending.session)return;
    if(failure){
      fail(failure);clear(primary);
      primary.append(button(pending.job?'Retry this job':'Retry submission',()=>submit(pending,!!pending.job),true),button('Return to episode',()=>id?refresh():catalog()));
      return;
    }
    if(pending.op==='episode-create')id=completed.result.episode;
    const resultEpisode=id,resultSession=session,auto=!['episode-select','episode-short','episode-package','episode-handoff'].includes(pending.op);
    try{await refresh(auto);}catch(e){if(closed||id!==resultEpisode||session!==resultSession)return;state.textContent='Completed output saved · view could not refresh';clear(primary);primary.append(button('Reload completed result',()=>{if(!closed&&id===resultEpisode&&session===resultSession)return refresh(auto);},true),button('Return to episodes',catalog));throw e;}
  }
  async function run(op,extras={}){
    if(busy||closed)return;
    const args=op==='episode-create'?extras:{episode:id,grant_revision:data.grant_revision,input_sha256:data.inputs[op],session,...extras};
    // Retrying an uncertain POST uses these same captured bytes/request ID.
    await submit({op,args:{...args,request_id:crypto.randomUUID()},episode:id,session,job:null});
  }
  function source(){return data.sources.find(s=>s.id===selected)||data.sources[0];}
  function playArtifact(artifact,mode,label){if(artifact&&playerSlot.dataset.artifact===artifact.id&&playerSlot.querySelector('video,audio')){const caption=playerSlot.querySelector('.ew-player-label');if(caption)caption.textContent=label;return;}playerSlot.dataset.artifact=artifact?.id||'';clear(playerSlot);playerSlot.className='ew-player '+(mode==='audio'?'ew-audio':'');if(!artifact){playerSlot.append(h('p',{},'Prepare recordings to see a playable preview here.'));return;}
    const player=h(mode==='audio'?'audio':'video',{controls:true,preload:'auto',src:artifact.url,'aria-label':label});playerSlot.append(h('p',{class:'ew-player-label'},label),player);
    if(mode==='audio'){const wave=artifact.details?.waveform?.peaks||[];const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 900 140');svg.setAttribute('aria-label','Decoded source waveform');svg.setAttribute('role','img');const stride=Math.max(1,Math.ceil(wave.length/200));for(let i=0;i<wave.length;i+=stride){const line=document.createElementNS(svg.namespaceURI,'line'),height=Math.max(1,wave[i]*130);line.setAttribute('x1',String(i/wave.length*900));line.setAttribute('x2',String(i/wave.length*900));line.setAttribute('y1',String(70-height/2));line.setAttribute('y2',String(70+height/2));svg.append(line);}if(wave.length)playerSlot.insertBefore(svg,player);}
    player.addEventListener('error',()=>fail(Error('Preview could not be decoded. Refresh the episode to recheck its registered bytes.')));
  }
  function seek(t){const player=playerSlot.querySelector('video,audio');if(player){player.currentTime=Math.max(0,t);player.play().catch(fail);}}
  let transcriptGeneration=0;
  async function transcriptView(page=0){
    const generation=++transcriptGeneration;clear(transcript);const tr=data.transcript;if(!tr)return;
    if(['assembly','rough','sequence','final','clips','release'].includes(stage)){transcript.append(h('p',{class:'mw-help'},stage==='sequence'?'Sequence previews play the selected opening or closing. Excerpt ranges use rough-cut output time.':stage==='final'?'Final playback includes the reviewed opening and closing. Caption times follow the complete programme.':'Programme playback uses output time. Use a decision’s source context to hear the original recording.'));return;}
    try{const result=await ctx.api.get(`episodes/${id}/transcript?source=${encodeURIComponent(selected)}&page=${page}`);if(generation!==transcriptGeneration||closed)return;
      transcript.append(h('h3',{},'Conversation'),h('p',{class:'mw-help'},`${tr.word_count} transcribed words. ${tr.engine==='generated-ground-truth'?'Synthetic ground-truth fixture':(typeof tr.engine==='object'?'Local '+(tr.engine.asr==='fluidaudio'?'Parakeet':tr.engine.asr||'ASR')+' transcript':'Local '+tr.engine+' transcript')}; unconfirmed wording and speaker labels.`));
      if(result.reason)transcript.append(h('p',{},result.reason));
      for(const u of result.utterances)transcript.append(button(`${seconds(u.source_start)}  ${u.speaker||''}  ${u.text}`,()=>seek(u.source_start)));
      if(result.pages>1)transcript.append(h('div',{},page?button('Previous passage',()=>transcriptView(page-1)):null,h('span',{},`Passage ${page+1} of ${result.pages}`),page+1<result.pages?button('Next passage',()=>transcriptView(page+1)):null));
    }catch(e){if(generation===transcriptGeneration)fail(e);}
  }

  function draw(){
    refreshGeneration++;
    captureDraft();captureDraft=()=>{};playerSlot.querySelector('.ew-audition-return')?.remove();dialog.scrollTop=0;main.scrollTop=0;transcript.classList.toggle('ew-final-transcript',stage==='final');transcript.classList.toggle('ew-release-transcript',['clips','release'].includes(stage));if(stage==='bounds')selected=data.sources.find(s=>s.file===data.transcript?.source)?.id||selected;title.textContent=data.label+(session==='combined'?' · Combined sessions':session!=='main'?` · ${session}`:'');state.textContent=data.ownership==='external_reference'?'Linked episode · originals read-only':'Managed episode · originals preserved';clear(rail);clear(inspector);clear(primary);clear(sourceTabs);if(data.view_provenance)inspector.append(h('p',{class:'mw-help ew-view-note'},data.view_provenance.note));
    rail.append(button('All episodes',catalog));
    const sessions=[{id:'main',label:'Full recording'},...data.sessions.filter(s=>s.id!=='main').map(s=>({id:s.id,label:s.label||s.id}))];
    if(data.combined_choices?.length>=2||session==='combined')sessions.push({id:'combined',label:'Combined sessions'});
    if(sessions.length>1){const select=choose('Session',sessions,session);select.addEventListener('change',async()=>{
      if(busy||closed){select.value=session;return;}
      const previousSession=session;session=select.value;busy=true;const unlock=lockSubmission();state.textContent='Loading selected session…';
      try{await refresh(true);}catch(e){if(!closed){session=previousSession;draw();fail(e);}}
      finally{busy=false;unlock();}
    });rail.append(select);}
    for(const [key,label] of stages.filter(([key])=>!data.adoption||!['sources','speakers','mapping','bounds'].includes(key)))rail.append(h('button',{type:'button','aria-current':stage===key?'step':null,onclick:()=>{stage=key;draw();}},label,done(key)?h('span',{'aria-label':'review recorded'},' ✓'):null));
    requestAnimationFrame(()=>{const active=rail.querySelector('[aria-current=step]');if(active){const r=rail.getBoundingClientRect(),a=active.getBoundingClientRect();if(a.right>r.right)rail.scrollLeft+=a.right-r.right+12;if(a.left<r.left)rail.scrollLeft-=r.left-a.left+12;}});
    const src=source();for(const s of data.sources)sourceTabs.append(h('button',{type:'button','aria-pressed':s.id===selected?'true':'false',onclick:()=>{selected=s.id;draw();}},s.label));
    sourceTabs.hidden=['assembly','rough','sequence','final','clips','release'].includes(stage);if(!sourceTabs.hidden)playArtifact(src?.preview,src?.probe?.video?'audiovisual':'audio',src?src.label+' · source preview':'Recording');transcriptView();
    if(stage==='sources')recordings();if(stage==='speakers')speakers();if(stage==='mapping')mapping();if(stage==='bounds')bounds();if(stage==='assembly')assembly();if(stage==='rough')rough();if(stage==='sequence')sequence();if(stage==='final')finalCut();if(['clips','release'].includes(stage))releaseDesk().catch(fail);
    if(data.checkpoints[stage]?.state==='stale')inspector.prepend(h('p',{class:'ew-warning'},'Needs a fresh review: '+data.checkpoints[stage].reason));
    if(done(stage))inspector.append(h('details',{},h('summary',{},'Review history'),h('p',{},data.checkpoints[stage].reason),button('Withdraw this confirmation',()=>review(stage,data.checkpoints[stage].payload,stage==='speakers'?'main':session,'revoke'))));
    inspector.append(h('details',{},h('summary',{},'Source identity & access'),h('p',{},data.ownership==='external_reference'?'Files remain owned and managed in the selected folder.':'These are managed copies of the imported recordings.'),h('p',{class:'ew-hash'},src?.sha256||''),window.aveNative?.revokeEpisode?button('Disconnect episode',async()=>{await window.aveNative.revokeEpisode(id);await catalog();}):null));
  }
  function referencePicker(){const ref=data.sources.find(s=>s.file===data.transcript?.source)||data.sources.find(s=>s.probe?.audio&&!s.probe?.video)||data.sources[0];return choose('Reference recording',data.sources.filter(s=>!s.probe||s.probe.audio).map(s=>({id:s.id,label:s.label})),ref?.id);}
  function recordings(){
    inspector.append(h('h3',{},'Recordings'),h('p',{},'Listen to the recording and inspect its channel evidence before naming speakers.'));
    for(const s of data.sources)inspector.append(h('details',{open:s.id===selected},h('summary',{},s.label),s.probe?h('div',{},h('p',{},`${seconds(s.probe.duration)} · ${s.probe.video?'Video + audio':'Audio'} · ${s.probe.channel_layout||'Channel evidence unavailable'}`),h('p',{},JSON.stringify(s.probe.channel_evidence||{}))):h('p',{},'Analysis has not been generated.')));
    for(const warning of data.media?.warnings||[])inspector.append(h('p',{class:'ew-warning'},warning));
    if(!data.media||data.sources.some(s=>!s.preview)){primary.append(button('Prepare recordings',()=>run('episode-intake'),true));return;}
    const ref=referencePicker(),count=h('input',{type:'number',min:1,max:16,value:2,'aria-label':'Number of speakers'});inspector.append(field('Dialogue reference',ref),field('Number of speakers',count));
    primary.append(button(data.transcript?'Transcribe again':'Transcribe conversation',()=>run('episode-transcribe',{source_id:ref.value,speakers:Number(count.value)}),true));
    if(data.transcript)primary.append(button('Continue to speakers',()=>{stage='speakers';draw();}));
  }
  function speakers(){
    inspector.append(h('h3',{},'Who is speaking?'),h('p',{},'Listen to each voice, then name the transcript IDs. Labels are local to this episode.'));
    if(!data.transcript){primary.append(button('Prepare transcript',()=>{stage='sources';draw();},true));return;}
    const fields={};for(const sid of data.transcript.speakers){const input=h('input',{type:'text',value:drafts.speakers?.names?.[sid]??data.checkpoints.speakers.payload?.names?.[sid]??'',maxlength:100,'aria-label':'Name '+sid});fields[sid]=input;inspector.append(field(sid,input));}
    captureDraft=()=>{drafts.speakers={names:Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value]))};};
    primary.append(button(done('speakers')?'Review speaker changes':'Review speaker names',()=>review('speakers',{names:Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value]))},'main'),true));
  }
  function mapping(){
    const videos=data.sources.some(s=>s.probe?.video);inspector.append(h('h3',{},videos?'Sources, cameras & sync':'Sources & session'),h('p',{},videos?'Check the camera previews and measured alignment. A warning is evidence to inspect, never a confirmed match.':'Audio sessions use the recorder and its channels. No camera mapping is required.'));
    if(!data.transcript||!done('speakers')){primary.append(button('Review speakers first',()=>{stage='speakers';draw();},true));return;}
    if(!data.sync){const ref=referencePicker();inspector.append(field('Sync reference',ref));primary.append(button(videos?'Measure source alignment':'Set reference clock',()=>run('episode-sync',{source_id:ref.value,points:5}),true));return;}
    const mapping=drafts.mapping||data.checkpoints.mapping.payload,ref=referencePicker(),selectedSources=new Set(mapping?.sources||data.sources.map(s=>s.id)),cameras={};if(mapping?.reference)ref.value=mapping.reference;inspector.append(field('Dialogue reference',ref));
    for(const s of data.sources){const check=h('input',{type:'checkbox','aria-label':'Include '+s.label,onchange:e=>e.target.checked?selectedSources.add(s.id):selectedSources.delete(s.id)});check.checked=selectedSources.has(s.id);inspector.append(h('label',{class:'ew-check'},check,h('span',{},s.label)));
      const clock=data.sync.files?.[s.file];inspector.append(h('p',{class:'mw-help'},clock?`Offset ${Number(clock.start_offset).toFixed(3)} s; drift ${Number(clock.drift_ppm||0).toFixed(1)} ppm; ${clock.sync_map?.length?'fitted-knot residual':'fit residual'} ${clock.residual_ms??'unknown'} ms; confidence PSR ${clock.psr??'reference'}.${clock.sync_map?.length?` Piecewise clock: ${clock.sync_map.length} measured points; prior linear residual ${clock.linear_residual_ms??'unknown'} ms. Inspect speech between points.`:''}`:'No alignment evidence'));
      if(s.probe?.video){const role=choose('Camera role '+s.label,[{id:'speaker',label:'Speaker camera'},{id:'wide',label:'Wide view'}],mapping?.cameras?.[s.id]?.role||'speaker');const who=choose('Speaker in '+s.label,[{id:'',label:'Choose a speaker'},...data.transcript.speakers.map(id=>({id,label:data.checkpoints.speakers.payload.names[id]||id}))],mapping?.cameras?.[s.id]?.speakers?.[0]||'');who.disabled=role.value==='wide';role.addEventListener('change',()=>{who.disabled=role.value==='wide';if(who.disabled)who.value='';});cameras[s.id]={role,who};inspector.append(field('Camera role',role),field('Visible speaker',who));}}
    captureDraft=()=>{drafts.mapping={reference:ref.value,sources:[...selectedSources],cameras:Object.fromEntries(Object.entries(cameras).filter(([sid])=>selectedSources.has(sid)).map(([sid,c])=>[sid,{role:c.role.value,speakers:c.role.value==='wide'?[]:c.who.value?[c.who.value]:[]}]))};};
    for(const warning of data.sync.warnings||[])inspector.append(h('p',{class:'ew-warning'},warning));
    inspector.append(h('details',{},h('summary',{},'Remeasure alignment'),button('Measure again',()=>run('episode-sync',{source_id:ref.value,points:9}))));
    primary.append(button(videos?'Review source & camera mapping':'Review audio session',()=>review('mapping',{reference:ref.value,sources:[...selectedSources],cameras:Object.fromEntries(Object.entries(cameras).filter(([sid])=>selectedSources.has(sid)).map(([sid,c])=>[sid,{role:c.role.value,speakers:c.role.value==='wide'?[]:c.who.value?[c.who.value]:[]}]))}),true));
  }
  function bounds(){
    inspector.append(h('h3',{},'Where does the session begin?'),h('p',{},'Play the surrounding conversation. These are proposed episode-time boundaries until you confirm them in the native sheet.'));
    if(!done('mapping')){primary.append(button('Review the session sources',()=>{stage='mapping';draw();},true));return;}
    const reference=data.sources.find(s=>s.file===data.transcript.source),saved=data.checkpoints.bounds.payload,proposal=drafts.bounds||saved;
    const lo=h('input',{type:'number',step:.001,min:0,max:reference.probe.duration,value:proposal?.in??data.suggested_bounds?.head?.proposed_in??0,'aria-label':'Session start seconds'}),hi=h('input',{type:'number',step:.001,min:0,max:reference.probe.duration,value:proposal?.out??data.suggested_bounds?.tail?.proposed_out??reference.probe.duration,'aria-label':'Session end seconds'});
    inspector.append(field('Proposed start · seconds',lo),button('Listen around start',()=>seek(Number(lo.value)-3)),field('Proposed end · seconds',hi),button('Listen around end',()=>seek(Number(hi.value)-3)),h('p',{},saved?`Confirmed: ${seconds(saved.in)} to ${seconds(saved.out)}`:'No exact bounds confirmed yet.'));
    captureDraft=()=>{drafts.bounds={in:Number(lo.value),out:Number(hi.value)};};
    if(data.suggested_bounds)inspector.append(h('details',{},h('summary',{},'Boundary suggestion evidence'),h('p',{},`${data.suggested_bounds.head.reason} (confidence ${data.suggested_bounds.head.confidence})`),h('p',{},`${data.suggested_bounds.tail.reason} (confidence ${data.suggested_bounds.tail.confidence})`)));
    primary.append(button('Review session bounds',()=>review('bounds',{in:Number(lo.value),out:Number(hi.value)}),true));
  }
  function assembly(){
    inspector.append(h('h3',{},'Review the assembly'),h('p',{},'Build from the confirmed bounds and selected source coverage. Check the retained conversation and any omitted intervals before review.'));
    if(!data.adoption&&session!=='combined'&&!done('bounds')){primary.append(button('Review session bounds first',()=>{stage='bounds';draw();},true));return;}
    const latest=data.assemblies.at(-1),selectedAssembly=data.assemblies.find(a=>a.version===drafts.combinedVersion)||latest;
    let ordered=[...(drafts.combined||selectedAssembly?.constituents?.map(c=>c.identity.name)||data.combined_choices.map(c=>c.session))];
    const buildCombined=()=>run('episode-combined',{constituents:ordered.map(name=>data.combined_choices.find(c=>c.session===name)?.identity).filter(Boolean)});
    if(session==='combined'){
      const list=h('section',{},h('h4',{},'Included sessions in order'));
      for(const [i,name] of ordered.entries()){const item=data.combined_choices.find(c=>c.session===name);if(!item)continue;const move=button('Move '+name+' earlier',()=>{if(i){[ordered[i-1],ordered[i]]=[ordered[i],ordered[i-1]];drafts.combined=ordered;draw();}});move.disabled=!i;list.append(h('div',{},h('p',{},`${i+1}. ${name} · ${seconds(item.duration)}${item.trim?` · episode ${seconds(item.trim.in)}–${seconds(item.trim.out)}`:''}`),move,button('Exclude '+name,()=>{ordered.splice(i,1);drafts.combined=ordered;draw();}))); }
      for(const c of data.combined_choices.filter(c=>!ordered.includes(c.session)))list.append(button('Include '+c.session,()=>{ordered.push(c.session);drafts.combined=ordered;draw();}));
      inspector.append(list,h('p',{class:'mw-help'},'Each session retains its own confirmed bounds and omissions. The combined render needs its own review.'));
    }
    const build=()=>session==='combined'?buildCombined():run('episode-assembly');
    if(!latest||!latest.render||!latest.saved_inputs_match){primary.append(button('Build & render assembly',build,true));return;}

    const selector=choose('Assembly version',data.assemblies.map(a=>({id:String(a.version),label:`Assembly v${a.version} · ${seconds(a.duration)}`})),String(selectedAssembly.version));inspector.append(field('Rendered version',selector));
    function show(){const a=data.assemblies.find(x=>String(x.version)===selector.value);playArtifact(a.render,a.mode,`Assembly v${a.version} · ${seconds(a.duration)} · rendered review`);clear(primary);const pending=session==='combined'&&JSON.stringify(ordered)!==JSON.stringify(a.constituents.map(c=>c.identity.name));const confirmed=!pending&&done('assembly')&&data.checkpoints.assembly.payload.version===a.version;state.textContent=confirmed?'Assembly review recorded':'Generated assembly · needs exact review';inspector.querySelector('.ew-coverage')?.remove();inspector.append(coverageEvidence(a));if(a.constituents?.length)inspector.append(h('section',{class:'ew-coverage'},h('h4',{},'Rendered order and coverage'),...a.constituents.map((c,i)=>h('div',{},h('p',{},`${i+1}. ${c.identity.name} · ${seconds(c.duration)}${c.trim?` · confirmed ${seconds(c.trim.in)}–${seconds(c.trim.out)}`:''}`),...c.excluded.map(x=>h('p',{},`Excluded episode ${seconds(x[0])}–${seconds(x[1])}`))))));inspector.querySelector('.ew-render-evidence')?.remove();inspector.append(h('details',{class:'ew-render-evidence'},h('summary',{},'Rendered evidence'),h('p',{},`${seconds(a.duration)} · ${a.render?.details?.report?.lufs??'—'} LUFS`),h('p',{class:'ew-hash'},a.sha256),h('p',{},a.render?.details?.report?.warnings?.join(' ')||'No compiler warnings.')));for(const warning of a.render?.details?.report?.warnings||[])primary.append(h('p',{class:'ew-review-warning','data-render-version':String(a.version)},warning));if(omittedInside(a.coverage)>1e-9)primary.append(h('p',{class:'ew-review-warning'},`${durationLabel(omittedInside(a.coverage))} omitted inside confirmed bounds`));if(pending){primary.append(h('p',{class:'ew-review-warning'},'Session order has unapplied changes. Build a new assembly before review.'),button('Discard order changes',()=>{drafts.combined=a.constituents.map(c=>c.identity.name);draw();}));}else if(confirmed)primary.append(button('Continue to rough cut',()=>{stage='rough';draw();},true));else if(a.render)primary.append(button('Review this exact assembly',()=>review('assembly',{stage:'assembly',name:session,version:a.version,sha256:a.sha256,artifact_id:a.render.id}),true));primary.append(button('Build a new assembly',build));}
    selector.addEventListener('change',()=>{if(session==='combined'){drafts.combinedVersion=Number(selector.value);drafts.combined=data.assemblies.find(a=>a.version===drafts.combinedVersion).constituents.map(c=>c.identity.name);draw();}else show();});show();
  }
  function coverageEvidence(assembly){
    const c=assembly.coverage,box=h('section',{class:'ew-coverage','aria-label':'Assembly coverage'});if(!c)return box;
    const range=(a,b)=>`${seconds(a)}–${seconds(b)}`;
    box.append(h('h3',{},'Assembly coverage'),h('p',{},`${seconds(c.output_duration)} output from ${seconds(c.confirmed_duration)} confirmed · ${seconds(c.reference_duration)} reference recording`));
    const rounding=c.boundary_rounding||[],omitted=omittedInside(c),gaps=c.excluded_inside_bounds.filter(g=>!rounding.some(r=>r.start===g.start&&r.end===g.end));
    box.append(h('p',{class:omitted>1e-9?'ew-warning':'mw-help'},omitted>1e-9?`${durationLabel(omitted)} inside the confirmed bounds is omitted.`:rounding.length?`Retained within ${durationLabel(c.boundary_precision_seconds)} boundary precision.`:'The entire confirmed span is retained.'));
    for(const gap of gaps)box.append(h('p',{},`Omitted episode ${gap.end-gap.start<1?`${gap.start.toFixed(6)}–${gap.end.toFixed(6)} s`:range(gap.start,gap.end)} · ${durationLabel(gap.end-gap.start)} · ${gap.reason}.`));
    if(rounding.length)box.append(h('details',{class:'ew-boundary-rounding'},h('summary',{},'Boundary rounding details'),h('p',{},'Recorded bounds are unchanged. These edge differences are within the EditDoc boundary precision.'),h('pre',{},JSON.stringify({confirmed_bounds:c.confirmed_bounds,precision_seconds:c.boundary_precision_seconds,edge_differences:rounding},null,2))));
    box.append(h('h4',{},'Retained · episode → output'));
    for(const span of c.retained)box.append(h('p',{class:'ew-interval'},`${range(span.episode_start,span.episode_end)} → ${range(span.output_start,span.output_end)}`));
    if(c.outside_confirmed_bounds.length)box.append(h('details',{},h('summary',{},'Outside confirmed bounds'),c.outside_confirmed_bounds.map(span=>h('p',{},`${range(span.start,span.end)} · ${span.reason}.`))));
    return box;
  }
  function rough(){
    inspector.append(h('h3',{},'Shape the conversation'),h('p',{},'Compare the assembly with the cleanup. Listen around each suggested cut; restore anything that belongs in the conversation.'));
    if(!done('assembly')){primary.append(button('Review assembly first',()=>{stage='assembly';draw();},true));return;}
    const assemblyPayload=data.checkpoints.assembly.payload,assembly=data.assemblies.find(a=>a.version===assemblyPayload.version&&a.sha256===assemblyPayload.sha256);
    if(!assembly?.render){inspector.append(h('p',{class:'ew-warning'},'The reviewed assembly render is unavailable.'));return;}
    const latest=data.roughs.at(-1),usable=latest?.saved_inputs_match&&latest.render;
    const policy=choose('Cleanup approach',[{id:'balanced',label:'Balanced cleanup'},{id:'gentle',label:'Gentle cleanup'}],drafts.rough?.policy||(usable?latest.report.policy:'balanced'));
    inspector.append(field('Cleanup approach',policy));
    if(!usable){playArtifact(assembly.render,assembly.mode,'Reviewed assembly');primary.append(button('Analyze and render rough cut',()=>run('episode-rough',{assembly:assemblyPayload,policy:policy.value,restore:[]}),true));return;}
    const report=latest.report,restore=new Set(drafts.rough?.restore??report.restored),list=h('section',{class:'ew-decisions'}),dirtyNote=h('p',{class:'ew-warning',hidden:true},'Changes are not in the current render. Render the revision before review.');let reviewButton=null;
    const refreshReview=()=>{const dirty=policy.value!==report.policy||JSON.stringify([...restore].sort())!==JSON.stringify([...report.restored].sort());if(reviewButton)reviewButton.disabled=dirty;dirtyNote.hidden=!dirty;list.querySelectorAll('input[type=checkbox]').forEach(el=>{el.disabled=policy.value!==report.policy;});};
    policy.addEventListener('change',refreshReview);
    captureDraft=()=>{drafts.rough={policy:policy.value,restore:[...restore]};};
    playArtifact(latest.render,latest.mode,`Rough cut v${latest.version} · rendered review`);
    inspector.append(h('div',{},button('Play assembly',()=>playArtifact(assembly.render,assembly.mode,'Reviewed assembly')),button('Play rough cut',()=>playArtifact(latest.render,latest.mode,`Rough cut v${latest.version} · rendered review`))),h('p',{},`${seconds(assembly.duration)} assembly → ${seconds(latest.duration)} rough · ${report.cuts.length} ${report.cuts.length===1?'cut':'cuts'}`),dirtyNote,list);
    function auditionCut(artifact,mode,label,time,cut){
      const scroll=main.scrollTop,anchor=document.activeElement;
      playArtifact(artifact,mode,label);seek(time);playerSlot.querySelector('.ew-audition-return')?.remove();
      const back=button('Return to decision '+cut.id,()=>{if(closed||stage!=='rough'||!anchor?.isConnected)return;main.scrollTop=scroll;anchor.focus({preventScroll:true});});back.classList.add('ew-audition-return');playerSlot.append(back);
      if(window.matchMedia('(max-width:700px)').matches){back.focus({preventScroll:true});main.scrollTop=0;}
    }
    for(const cut of report.detected_cuts){
      const decisions=report.decisions.filter(d=>cut.decisions.includes(d.id)||d.cut===cut.id);
      const check=h('input',{type:'checkbox','aria-label':'Restore cut '+cut.id,onchange:e=>{e.target.checked?restore.add(cut.id):restore.delete(cut.id);refreshReview();}});check.checked=restore.has(cut.id);
      const d=decisions[0],context=d?.context,source=data.sources.find(s=>s.id===context?.source_id);
      list.append(h('div',{class:'ew-decision'},h('label',{class:'ew-check'},check,h('span',{},`Restore ${seconds(cut.e0)}–${seconds(cut.e1)} · ${cut.comparison?.input_clock||'episode source'} time`)),cut.comparison?button('Before · assembly',()=>auditionCut(assembly.render,assembly.mode,'Assembly · before cut',Math.max(0,cut.comparison.assembly_output_s-2),cut)):null,cut.comparison?button('After · rough seam',()=>auditionCut(latest.render,latest.mode,'Rough cut · after cut',Math.max(0,cut.comparison.rough_output_s-2),cut)):null,...decisions.map(x=>h('p',{},`${x.text||x.type}: ${x.reason} · confidence ${x.confidence}`)),source?.preview?button('Listen in original',()=>auditionCut(source.preview,source.probe?.video?'audiovisual':'audio',source.label+' · original context',Math.max(0,context.source_seconds-3),cut)):null));
    }
    for(const d of report.decisions.filter(d=>d.review&&!d.cut))list.append(h('p',{class:'ew-warning'},`${d.text||d.type}: ${d.reason} · confidence ${d.confidence}`));
    for(const warning of report.warnings||[])list.append(h('p',{class:'ew-warning'},warning));
    const selected={stage:'rough',name:session,version:latest.version,sha256:latest.sha256,artifact_id:latest.render.id};
    if(done('rough')&&data.checkpoints.rough.payload.version===latest.version)primary.append(h('p',{},'Rough cut reviewed.'),button('Continue to opening & closing',()=>{stage='sequence';draw();},true));
    else {reviewButton=button('Review this exact rough cut',()=>{playArtifact(latest.render,latest.mode,`Rough cut v${latest.version} · rendered review`);return review('rough',selected);},true);primary.append(reviewButton);}
    primary.append(button('Render revised cleanup',()=>run('episode-rough',{assembly:assemblyPayload,policy:policy.value,restore:policy.value===report.policy?[...restore]:[]})));
    refreshReview();
  }
  function captionPhraseEditor({rough,range=()=>null,corrections,onChange,audition,target=null,component=false,excerpt=false}){
    const node=h('div',{class:'ew-phrase-editor'}),desk=h('div',{}),words=h('div',{}),pages=new Map(),context={id,session,stage};node.append(desk,words);let page=0,serial=0,size=40;
    const scope=()=>!closed&&node.isConnected&&id===context.id&&session===context.session&&stage===context.stage;
    const same=(a,b)=>a.occurrence_id===b.occurrence_id&&a.transcript_sha256===b.transcript_sha256&&a.continuity_id===b.continuity_id;
    const allowed=w=>{const r=range();return w&&!w.partial_source_word&&(!r||(w.start>=r[0]-1e-8&&w.end<=r[1]+1e-8));};
    async function load(n){if(pages.has(n))return pages.get(n);const ticket=serial,value=await ctx.api.get(`episodes/${context.id}/view/caption-words?session=${encodeURIComponent(context.session)}&page=${n}`);if(!scope()||ticket!==serial)return null;if(value.rough.sha256!==rough.sha256){serial++;clear(desk);clear(words);throw Error('The reviewed rough cut changed. Reload before correcting captions.');}pages.set(n,value);if(n===0&&value.pages===1)size=value.words.length||40;return value;}
    async function at(index){if(index<0)return null;const first=await load(0);if(!first||Math.floor(index/size)>=first.pages)return null;const value=await load(Math.floor(index/size));return value?.words[index%size]||null;}
    async function locate(word){for(const [n,value] of pages){const found=value.words.findIndex(w=>w.word_id===word.word_id&&w.occurrence_id===word.occurrence_id&&w.transcript_sha256===word.transcript_sha256);if(found>=0)return n*size+found;}const first=await load(0);if(!first)return null;let lo=0,hi=first.pages-1;const time=word.rough_start??word.start;while(lo<=hi){const n=Math.floor((lo+hi)/2),value=await load(n);if(!value)return null;const found=value.words.findIndex(w=>w.word_id===word.word_id&&w.occurrence_id===word.occurrence_id&&w.transcript_sha256===word.transcript_sha256);if(found>=0)return n*size+found;if(time<(value.words[0]?.start??Infinity))hi=n-1;else lo=n+1;}throw Error('This caption word is no longer in the selected rough cut.');}
    async function select(word){const ticket=++serial,bounds=JSON.stringify(range()),alive=()=>scope()&&ticket===serial&&JSON.stringify(range())===bounds;let first=await locate(word);if(!alive()||first===null)return;
      const current=corrections().find(c=>c.occurrence_id===word.occurrence_id&&c.transcript_sha256===word.transcript_sha256&&c.word_ids.includes(word.word_id));
      if(current)for(let i=0;i<49&&first>0;i++){const previous=await at(first-1);if(!alive())return;if(!previous||!same(await at(first),previous)||!current.word_ids.includes(previous.word_id))break;first--;}
      const selected=[];for(let i=0;i<(current?.word_ids.length||1);i++){const value=await at(first+i);if(!alive())return;if(!allowed(value)||(i&&!same(selected[0],value))||(current&&!current.word_ids.includes(value.word_id)))throw Error('The saved correction range changed. Choose a continuous phrase again.');selected.push(value);}
      clear(desk);audition(selected[0].start,selected.at(-1).end);
      const original=h('p',{class:'mw-help'}),input=h('input',{type:'text',maxlength:500,value:current?.text||selected.map(w=>w.w).join(' '),'aria-label':component?'Correct component word':excerpt?'Correct excerpt word':'Corrected word'}),start=h('input',{type:'number',step:.05,value:current?.start_offset_s??0,'aria-label':component?'Component word start adjustment':'Word start adjustment seconds'}),end=h('input',{type:'number',step:.05,value:current?.end_offset_s??0,'aria-label':component?'Component word end adjustment':'Word end adjustment seconds'});let edited=!!current;
      input.addEventListener('input',()=>{edited=true;});
      const more=button('Include next word',async()=>{more.disabled=true;const next=await at(first+selected.length);if(!alive())return;if(selected.length>=50||!allowed(next)||!same(selected[0],next))return update();selected.push(next);if(!edited)input.value=selected.map(w=>w.w).join(' ');await update();}),less=button('Remove last word',async()=>{if(selected.length>1)selected.pop();if(!edited)input.value=selected.map(w=>w.w).join(' ');await update();});
      async function update(){const next=await at(first+selected.length);if(!alive())return;original.textContent='Original: '+selected.map(w=>w.w).join(' ');more.disabled=selected.length>=50||!allowed(next)||!same(selected[0],next);less.disabled=selected.length===1;}
      const replace=()=>corrections().filter(c=>!(c.occurrence_id===selected[0].occurrence_id&&c.transcript_sha256===selected[0].transcript_sha256&&c.word_ids.some(ref=>selected.some(w=>w.word_id===ref))));
      desk.append(original,button(component?'Listen to original word in rough cut':'Listen to original phrase',()=>audition(selected[0].start,selected.at(-1).end)),h('div',{},more,less),field('Replace selected phrase',input),field('Move start · seconds',start),field('Move end · seconds',end),h('p',{class:'mw-help'},'Changes affect this occurrence only. Include adjacent words to remove an extra word; original source text stays unchanged.'),button(component?'Apply component correction':excerpt?'Save excerpt correction':'Apply correction',async()=>{if(!alive())return;if(!input.value.trim())throw Error('Enter replacement text, or include an adjacent word to remove an extra word.');const next=replace();if(input.value.trim()!==selected.map(w=>w.w).join(' ')||Number(start.value)||Number(end.value))next.push({occurrence_id:selected[0].occurrence_id,transcript_sha256:selected[0].transcript_sha256,word_ids:selected.map(w=>w.word_id),text:input.value.trim(),start_offset_s:Number(start.value),end_offset_s:Number(end.value)});onChange(next);await show(page);}),button('Restore original phrase',async()=>{if(!alive())return;onChange(replace());await show(page);}));await update();
    }
    async function show(n=0){const ticket=++serial;page=n;const value=await load(n);if(!scope()||ticket!==serial||!value)return;clear(words);clear(desk);for(const word of value.words.filter(allowed)){const correction=corrections().find(c=>c.occurrence_id===word.occurrence_id&&c.transcript_sha256===word.transcript_sha256&&c.word_ids.includes(word.word_id));words.append(button(`${seconds(word.start)} ${correction?(correction.word_ids[0]===word.word_id?correction.text:word.w+' · in corrected phrase'):word.w}`,()=>select(word)));}if(value.pages>1)words.append(h('div',{},n?button(excerpt?'Previous excerpt words':'Previous caption words',()=>show(n-1)):null,h('span',{},` ${n+1} of ${value.pages} `),n+1<value.pages?button(excerpt?'Next excerpt words':'Next caption words',()=>show(n+1)):null));}
    return {node,open:async()=>{await show();if(scope()&&target)await select(target);},select};
  }
  function sequence(){
    inspector.append(h('h3',{},'Opening & closing'),h('p',{},'Choose the show details and the material that frames this conversation. Preview the exact plan before review.'));
    if(!done('rough')){primary.append(button('Review rough cut first',()=>{stage='rough';draw();},true));return;}
    const roughPayload=data.checkpoints.rough.payload,roughCut=data.roughs.find(r=>r.version===roughPayload.version&&r.sha256===roughPayload.sha256);
    if(!roughCut?.render){inspector.append(h('p',{class:'ew-warning'},'The reviewed rough render is unavailable.'));return;}
    const latest=data.sequences.at(-1),current=latest?.saved_inputs_match&&Object.values(latest.components).every(Boolean),editing=drafts.sequenceEditing||!current;
    if(!editing){
      const args=latest.args,played=new Set(),required=Object.keys(latest.components),note=h('p',{class:'mw-help'},required.length?'Play each enabled preview before preparing review.':'Both sequences are disabled.');
      inspector.append(h('p',{},`${args.show} · ${args.episode_title}${args.number?' · '+args.number:''}`),h('p',{},args.hosts.length?'Hosts: '+args.hosts.map(x=>x.name+(x.title?' ('+x.title+')':'')).join(', '):'No hosts listed.'),note);
      if(latest.mode==='audio')inspector.append(h('p',{},'Show and host text is plan information. Only the selected recordings and excerpts are heard.'));
      for(const tag of ['opening','closing']){
        inspector.append(h('h4',{},tag==='opening'?'Opening':'Closing'));
        if(!args[tag].enabled){inspector.append(h('p',{},'None'));continue;}
        if(latest.mode==='audio')for(const [i,item] of args[tag].components.entries()){
          const part=h('section',{class:'ew-sequence-item'},h('h5',{},`Component ${i+1}`));
          if(item.kind==='excerpt'){
            part.append(h('p',{},`Rough cut excerpt · ${seconds(item.start)}–${seconds(item.end)} · ${seconds(item.end-item.start)}`),latest.excerpts[`${tag}:${i}`]?h('p',{},'Complete transcript words: '+latest.excerpts[`${tag}:${i}`]):h('p',{class:'mw-help'},'No complete transcript words in this excerpt.'));
            if(latest.partial_words[`${tag}:${i}`]?.length)part.append(h('p',{class:'ew-warning'},'Boundary cuts through: '+latest.partial_words[`${tag}:${i}`].join(', ')+'. Listen and adjust the range.'));
          }else part.append(h('p',{},`${latest.asset_labels[item.id]||'Licensed recording'} · from ${seconds(item.start)} · ${seconds(item.duration)}; inspect any speech for captions.`));
          inspector.append(part);
        }
        else {for(const item of latest.plan?.[tag]?.items||[])inspector.append(h('p',{},`${item.kind.replaceAll('_',' ')} · ${seconds(item.duration)}${item.source?.excerpt?' · '+item.source.excerpt:''}`));for(const item of args[tag].components)inspector.append(h('p',{},`Music: ${latest.asset_labels[item.id]||'Licensed recording'} · from ${seconds(item.start)} · ${seconds(item.duration)} available`));}
      }
      for(const warning of latest.plan?.warnings||[]){if(!args.closing.enabled&&(warning.includes('editor name')||warning.includes('credits')))continue;if(!args.opening.enabled&&(warning.includes('hosts to introduce')||warning.includes('host has no title')))continue;const readable=warning.includes('no hosts to introduce')?'Add host names to include a host card.':warning.includes('host has no title')?'Add a host title in Change plan to show each host’s role.':warning.includes('no editor name')?'Add an editor name in Change plan to include an editor credit.':warning.includes('nothing to put in the credits')?'No credits will be shown.':warning;inspector.append(h('p',{class:'ew-warning'},readable));}
      let reviewButton=null;
      for(const tag of required){const art=latest.components[tag];inspector.append(button('Play '+tag,()=>{playArtifact(art,latest.mode,tag+' · rendered preview');const player=playerSlot.querySelector('video,audio');player.addEventListener('playing',()=>{played.add(tag);if(reviewButton&&played.size===required.length){reviewButton.disabled=false;note.textContent='The plan and selected previews are ready for exact review.';}},{once:true});return player.play();}));if(art.details?.caption)inspector.append(h('details',{},h('summary',{},tag+' planned captions'),h('p',{},art.details.caption.status),art.details.caption.cues.map(c=>h('p',{},`${seconds(c.start)} ${c.text}`))));for(const warning of art.details?.report?.warnings||[])inspector.append(h('p',{class:'ew-warning'},warning));}
      if(required.length)playArtifact(latest.components[required[0]],latest.mode,required[0]+' · rendered preview');else playArtifact(roughCut.render,roughCut.mode,'Reviewed rough cut');
      if(done('sequence')&&data.checkpoints.sequence.payload.job_id===latest.job_id)primary.append(h('p',{},'Opening and closing reviewed.'),button('Continue to final cut',()=>{stage='final';draw();},true));
      else {reviewButton=button('Review this exact plan',()=>review('sequence',{job_id:latest.job_id,plan_sha256:latest.plan_sha256,artifacts:Object.fromEntries(required.map(tag=>[tag,latest.components[tag].id]))}),true);reviewButton.disabled=required.length>0;primary.append(reviewButton);}
      primary.append(button('Change plan',()=>{drafts.sequenceEditing=true;draw();}));return;
    }
    playArtifact(roughCut.render,roughCut.mode,'Reviewed rough cut · choose context');
    const initial=drafts.sequence||latest?.args||{},show=h('input',{type:'text',maxlength:120,value:initial.show||data.label,'aria-label':'Show name'}),episode=h('input',{type:'text',maxlength:200,value:initial.episode_title||'','aria-label':'Episode title'}),number=h('input',{type:'text',maxlength:30,value:initial.number||'','aria-label':'Episode number'}),hosts=h('input',{type:'text',value:(initial.hosts||[]).map(x=>x.name).join(', '),'aria-label':'Host names'}),hostTitle=h('input',{type:'text',maxlength:100,value:initial.hosts?.[0]?.title||'','aria-label':'Host title'}),editor=h('input',{type:'text',maxlength:100,value:initial.editor||'','aria-label':'Editor name'});
    inspector.append(field('Show name',show),field('Episode title',episode),field('Episode number',number),field('Hosts · comma separated',hosts),field('Host title · optional',hostTitle),field('Editor name · optional',editor));
    const coldCheck=h('input',{type:'checkbox','aria-label':'Include cold open'});coldCheck.checked=!!initial.cold_open;
    const coldStart=h('input',{type:'number',min:0,step:.001,value:initial.cold_open?.start??0,'aria-label':'Cold open start'}),coldEnd=h('input',{type:'number',min:0,step:.001,value:initial.cold_open?.end??0,'aria-label':'Cold open end'});
    if(roughCut.mode!=='audio')inspector.append(h('label',{class:'ew-check'},coldCheck,h('span',{},'Include cold open from this rough cut')),field('Cold open start · output seconds',coldStart),field('Cold open end · output seconds',coldEnd));
    let coldCorrections=structuredClone(initial.cold_open?.corrections||[]);const coldTarget=drafts.sequenceTarget;
    if(roughCut.mode!=='audio')inspector.append(excerptCaptionPicker('cold open',()=>[Number(coldStart.value),Number(coldEnd.value)],()=>coldCorrections,value=>{coldCorrections=value;},coldTarget?.component_index===-1?coldTarget:null));
    function excerptCaptionPicker(label,range,getCorrections,setCorrections,target=null){
      const box=h('div',{});let editor=null;
      const open=button('Correct '+label+' captions',async()=>{clear(box);editor=captionPhraseEditor({rough:roughPayload,range,corrections:getCorrections,onChange:setCorrections,audition:(a,b)=>auditionExcerpt(Math.max(range()[0],a-1),Math.min(range()[1],b+1)),target,component:!!target,excerpt:!target});box.append(editor.node);await editor.open();});
      const result=h('div',{},open,box);if(target)requestAnimationFrame(()=>{if(result.isConnected)open.click();});return result;
    }
    function auditionExcerpt(a,b){playArtifact(roughCut.render,roughCut.mode,'Reviewed rough cut · original excerpt (unapplied draft)');const player=playerSlot.querySelector('video,audio');const stop=()=>{if(player.currentTime>=b){player.pause();player.removeEventListener('timeupdate',stop);}};player.addEventListener('timeupdate',stop);seek(Math.max(0,a));}
    const sections={};let library=[];
    for(const tag of ['opening','closing']){
      const saved=initial[tag]||{},enabled=h('input',{type:'checkbox','aria-label':'Enable '+tag});enabled.checked=!!saved.enabled;
      const list=h('div',{}),section={enabled,rows:[],list,next:0};sections[tag]=section;
      function add(component={}){
        if(section.rows.length>=data.sequence_max_components)throw Error(`Choose at most ${data.sequence_max_components} components.`);
        const index=++section.next,suffix=index===1?'':` ${index}`,options=roughCut.mode==='audio'?[{id:'',label:'Choose audio'},{id:'excerpt',label:'Rough cut excerpt'},{id:'asset',label:'Licensed library recording'}]:[{id:'',label:'No music'},{id:'asset',label:'Licensed music bed'}];
        const kind=choose(tag+' audio component'+suffix,options,component.kind||''),start=h('input',{type:'number',min:0,step:.001,value:component.start??0,'aria-label':tag+' audio start'+suffix}),end=h('input',{type:'number',min:0,step:.001,value:component.end??component.duration??0,'aria-label':tag+' audio end or duration'+suffix}),assets=h('select',{'aria-label':tag+' licensed recording'+suffix},h('option',{value:''},'Choose licensed recording'));
        const row={kind,start,end,assets,saved:component},endField=field('End · rough-cut seconds',end),endLabel=endField.querySelector('span'),audition=button('Listen to original '+tag+' excerpt '+index,()=>auditionExcerpt(Number(start.value),Number(end.value))),move=button(`Move ${tag} component ${index} earlier`,()=>{const at=section.rows.indexOf(row);if(at>0){[section.rows[at-1],section.rows[at]]=[section.rows[at],section.rows[at-1]];list.insertBefore(card,section.rows[at].card);refreshMoves();}}),card=h('div',{class:'ew-decision'},h('h4',{},`${tag} component ${index}`),field('Audio',kind),field('Start · seconds',start),endField,field('Licensed recording',assets),audition,move,button(`Remove ${tag} component ${index}`,()=>{section.rows.splice(section.rows.indexOf(row),1);card.remove();refreshMoves();}));row.card=card;row.move=move;section.rows.push(row);list.append(card);refreshMoves();
        function refreshMoves(){section.rows.forEach((r,i)=>{r.move.disabled=i===0;});}
        for(const a of library)assets.append(h('option',{value:a.id},a.alt||'Licensed recording'));assets.value=component.id||'';
        const target=drafts.sequenceTarget;
        const picker=excerptCaptionPicker(`${tag} component ${index}`,()=>[Number(start.value),Number(end.value)],()=>row.saved.corrections||[],value=>{row.saved={...row.saved,kind:'excerpt',corrections:value};},target?.component_tag===tag&&target.component_index===index-1?target:null);card.append(picker);
        let previousKind=kind.value;const update=()=>{assets.closest('label').hidden=kind.value!=='asset';picker.hidden=kind.value!=='excerpt';audition.hidden=kind.value!=='excerpt';if(endLabel)endLabel.textContent=kind.value==='asset'?'Duration · seconds':'End · rough-cut seconds';};kind.addEventListener('change',()=>{if(kind.value!==previousKind){row.saved={};assets.value='';previousKind=kind.value;}update();});update();
      }
      section.add=add;const addButton=button('Add '+tag+' component',()=>add());const update=()=>{if(enabled.checked&&!section.rows.length&&roughCut.mode==='audio')add();list.hidden=!enabled.checked;addButton.hidden=!enabled.checked;};enabled.addEventListener('change',update);inspector.append(h('label',{class:'ew-check'},enabled,h('span',{},'Enable '+tag)),list,addButton);for(const component of saved.components||[])add(component);if(enabled.checked&&!section.rows.length)add();update();
    }
    ctx.api.get('library?kind=audio').then(value=>{library=(value.assets||[]).filter(a=>a.kind==='audio'&&a.license_ok);for(const sec of Object.values(sections))for(const row of sec.rows){for(const a of library)row.assets.append(h('option',{value:a.id},a.alt||'Licensed recording'));row.assets.value=row.saved.id||'';}}).catch(fail);
    function values(strict=false){const out={rough:roughPayload,show:show.value,episode_title:episode.value,number:number.value,hosts:hosts.value.split(',').map(x=>x.trim()).filter(Boolean).map(name=>({name,title:hostTitle.value})),editor:editor.value,cold_open:roughCut.mode!=='audio'&&coldCheck.checked?{start:Number(coldStart.value),end:Number(coldEnd.value),...(coldCorrections.length?{corrections:coldCorrections}:{})}:null};
      for(const [tag,sec] of Object.entries(sections)){const components=[];if(sec.enabled.checked)for(const row of sec.rows){if(row.kind.value==='excerpt')components.push({kind:'excerpt',start:Number(row.start.value),end:Number(row.end.value),...(row.saved.kind==='excerpt'&&row.saved.corrections?.length?{corrections:row.saved.corrections}:{})});else if(row.kind.value==='asset'){const a=library.find(a=>a.id===row.assets.value);if(a)components.push({kind:'asset',id:a.id,sha256:a.sha256,start:Number(row.start.value),duration:Number(row.end.value)});else if(!library.length&&row.saved.id)components.push(row.saved);else if(strict)throw Error('Choose a licensed recording for '+tag+'.');}}if(strict&&roughCut.mode==='audio'&&sec.enabled.checked&&!components.length)throw Error('Choose real audio for '+tag+'.');out[tag]={enabled:sec.enabled.checked,components};}return out;}

    captureDraft=()=>{drafts.sequence=values();drafts.sequenceEditing=true;};
    primary.append(button('Preview opening & closing',()=>run('episode-sequence',values(true)),true));
  }
  function finalCut(){
    inspector.append(h('h3',{},'Finish the programme'),h('p',{},'Listen to the complete programme, check its sound and read the planned captions before exact review.'));
    if(!done('rough')||!done('sequence')){primary.append(button('Review opening & closing first',()=>{stage='sequence';draw();},true));return;}
    const roughPayload=data.checkpoints.rough.payload,roughCut=data.roughs.find(r=>r.version===roughPayload.version&&r.sha256===roughPayload.sha256),sequencePayload=data.checkpoints.sequence.payload;
    if(!roughCut?.render){inspector.append(h('p',{class:'ew-warning'},'The reviewed rough render is unavailable.'));return;}
    const latest=data.finals.at(-1),current=latest?.saved_inputs_match&&latest.render,editing=drafts.finalEditing||!current;
    const seq=data.sequences.find(s=>s.job_id===sequencePayload.job_id),opening=seq?.components?.opening?.details?.report,openingEstimate=opening?.samples?opening.samples/48000:(opening?.duration||0),openingOffset=latest?.render?.details?.report?.joins_s?.[0]??openingEstimate;
    const placementTime=t=>`main conversation ${seconds(t)} · programme ${seconds(openingOffset+Number(t))}${latest?'':' (estimated)'}`;
    if(!editing){
      const art=latest.render,report=art.details.report,caption=art.details.caption;
      playArtifact(art,latest.mode,`Final cut v${latest.version} · ${seconds(report.duration)}`);
      inspector.append(h('p',{},`${seconds(report.duration)} programme · ${report.lufs} LUFS · ${report.true_peak_db} dB true peak`),h('p',{},`Dialogue adjustment ${latest.spec.gain_db} dB · ${latest.spec.burn?'captions burned into picture':'caption sidecar'}`));
      inspector.append(h('p',{},latest.spec.music?`Music: ${latest.asset_labels[latest.spec.music.id]||'Licensed recording'} · ${latest.spec.music.gain_db} dB · duck ${latest.spec.music.duck_db} dB`:'Music: none'));
      for(const m of latest.spec.motion||[])inspector.append(h('p',{},`Name title: ${m.name}${m.title?' · '+m.title:''} · ${placementTime(m.start)} for ${seconds(m.duration)}`));
      for(const b of latest.spec.broll||[])inspector.append(h('p',{},`B-roll: ${latest.asset_labels[b.id]||'Licensed visual'} · ${placementTime(b.start)} for ${seconds(b.duration)}`));
      for(const warning of report.warnings||[])inspector.append(h('p',{class:'ew-warning'},warning));
      for(const credit of report.credits||[])inspector.append(h('p',{},'Required credit: '+credit));
      const capbox=h('details',{},h('summary',{},'Selected caption sidecar · '+caption.cues.length+' cues'),h('p',{},caption.status),(caption.problems||[]).map(p=>h('p',{class:'ew-warning'},`Cue ${p.cue}: ${p.detail}`)),(caption.boundary_adjustments||[]).map(a=>h('p',{class:'ew-warning'},`Caption crossed the ${a.component||'programme'} boundary: “${a.text}”${a.clipped_words?.length?' · clipped words: '+a.clipped_words.join(', '):' · word timing unavailable or unchanged'}. Listen at the boundary.`)));inspector.append(capbox);
      const editWord=word=>{if(word.component_tag){drafts.sequence=structuredClone(seq.args);drafts.sequenceEditing=true;drafts.sequenceTarget=word;stage='sequence';draw();}else{drafts.finalEditing=true;drafts.finalTarget=word;draw();}},allWords=caption.cues.flatMap(c=>c.words||[]).filter(w=>w.word_id);
      clear(transcript);transcript.append(h('h3',{},'Caption review'),h('p',{class:'mw-help'},'Select a caption to listen. Open a line to adjust a word and its timing.'));
      for(const cue of caption.cues){const row=h('details',{class:'ew-caption-row'},h('summary',{},`${seconds(cue.start)}  ${cue.text}`),button('Listen to this caption',()=>seek(cue.start)),...(cue.words||[]).filter(w=>w.word_id).map(w=>button('Edit '+w.w,()=>editWord(w))));transcript.append(row);}
      const comparison=report.asr_comparison,compare=h('details',{open:comparison.discrepancies>0},h('summary',{},`Final audio comparison · ${comparison.discrepancies} ${comparison.discrepancies===1?'difference':'differences'}`),art.details.asr?.configuration?.engine==='synthetic-test-only'?h('p',{},'Synthetic ASR fixture; this is not a real transcription.'):null,h('p',{},comparison.note));
      for(const d of comparison.items){const at=d.asr_start??d.planned_start??0,joins=report.joins_s||(report.joins_samples||[]).map(n=>n/report.sample_rate),component=seq?.args?.opening?.enabled&&at<(joins[0]??0)?'opening':seq?.args?.closing?.enabled&&at>=(joins.at(-1)??Infinity)?'closing':null,candidates=allWords.filter(w=>(w.component_tag||null)===component),nearest=candidates.reduce((best,w)=>!best||Math.abs(w.start-at)<Math.abs(best.start-at)?w:best,null);compare.append(h('div',{class:'ew-difference'},h('p',{},d.kind==='timing'?`Timing: caption ${seconds(d.planned_start)} “${d.planned}”; audio ASR ${seconds(d.asr_start)}.`:`${seconds(at)} · captions: ${d.planned||'(none)'} · audio ASR: ${d.asr||'(none)'}`),button('Listen to difference',()=>seek(Math.max(0,at-1))),nearest?button('Edit nearby word',()=>editWord(nearest)):component?button('Revise '+component+' captions',()=>{drafts.sequence=structuredClone(seq.args);drafts.sequenceEditing=true;stage='sequence';draw();}):null));}
      if(comparison.truncated)compare.append(h('p',{},'Additional differences are retained in the evidence.'));transcript.append(compare);
      const payload={stage:'final',name:session,version:latest.version,sha256:latest.sha256,artifact_id:art.id};
      if(done('final')&&data.checkpoints.final.payload.version===latest.version)primary.append(h('p',{},'Final cut reviewed.'));
      else {const reviewButton=button('Review this exact final cut',()=>review('final',payload),true);reviewButton.disabled=true;primary.append(reviewButton);playerSlot.querySelector('video,audio')?.addEventListener('playing',()=>{reviewButton.disabled=false;},{once:true});}
      primary.append(button('Revise sound & captions',()=>{drafts.finalEditing=true;draw();}));return;
    }
    playArtifact(roughCut.render,roughCut.mode,'Reviewed rough cut');
    const initial=drafts.final||latest?.spec||{},gain=h('input',{type:'number',min:-24,max:12,step:.5,value:initial.gain_db??0,'aria-label':'Dialogue gain dB'}),burn=h('input',{type:'checkbox','aria-label':'Burn captions'});burn.checked=initial.burn??false;
    inspector.append(field('Dialogue adjustment · dB',gain),h('p',{class:'mw-help'},'The finished programme is measured after loudness processing. Captions are generated from the source transcript and need wording review.'));
    const music=h('select',{'aria-label':'Music bed'},h('option',{value:''},'None')),musicStart=h('input',{type:'number',min:0,step:.1,value:initial.music?.start??0,'aria-label':'Music start seconds'}),musicGain=h('input',{type:'number',min:-60,max:0,step:1,value:initial.music?.gain_db??-20,'aria-label':'Music gain dB'}),duck=h('input',{type:'number',min:-40,max:0,step:1,value:initial.music?.duck_db??-8,'aria-label':'Music duck dB'}),fadeIn=h('input',{type:'number',min:0,max:30,step:.1,value:initial.music?.fade_in_s??1,'aria-label':'Music fade in'}),fadeOut=h('input',{type:'number',min:0,max:30,step:.1,value:initial.music?.fade_out_s??1,'aria-label':'Music fade out'});let library=[],libraryLoaded=false;
    const musicFields=h('div',{hidden:true},h('p',{class:'mw-help'},`Music plays throughout the main conversation, starting at programme ${seconds(openingOffset)}${latest?'':' (estimated)'}. The source start selects where to begin within the music recording.`),field('Music source start · seconds',musicStart),field('Music gain · dB',musicGain),field('Duck under dialogue · dB',duck),field('Fade in · seconds',fadeIn),field('Fade out · seconds',fadeOut));
    music.addEventListener('change',()=>{musicFields.hidden=!music.value;});inspector.append(field('Licensed music bed',music),musicFields);
    const brollRows=[],motionRows=[];let brollSerial=0,motionSerial=0;const brollBox=h('div',{}),motionBox=h('div',{});
    function addBroll(item={}){
      if(brollRows.length>=8)throw Error('Choose at most eight B-roll placements.');
      const index=++brollSerial,suffix=index===1?'':` ${index}`,select=h('select',{'aria-label':'B-roll'+suffix},h('option',{value:''},'Choose licensed visual'));
      const start=h('input',{type:'number',min:0,step:.1,value:item.start??0,'aria-label':'B-roll start'+suffix}),duration=h('input',{type:'number',min:.05,step:.1,value:item.duration??3,'aria-label':'B-roll duration'+suffix}),sourceIn=h('input',{type:'number',min:0,step:.1,value:item.source_in??0,'aria-label':'B-roll source in'+suffix}),move=h('input',{type:'checkbox','aria-label':'Move still image'+suffix});move.checked=!!item.ken_burns;
      const projected=h('p',{class:'mw-help'}),refresh=()=>{projected.textContent=placementTime(start.value);};start.addEventListener('input',refresh);refresh();const row={select,start,duration,sourceIn,move,saved:item};const card=h('div',{class:'ew-decision'},h('h4',{},`B-roll ${index}`),field('Licensed B-roll',select),field('Start · main-conversation seconds',start),projected,button('Listen at placement',()=>{playArtifact(roughCut.render,roughCut.mode,'Reviewed rough cut · B-roll context');seek(Number(start.value));}),field('Duration · seconds',duration),field('Video source in · seconds',sourceIn),h('label',{class:'ew-check'},move,h('span',{},'Slow movement on a still')),button('Remove B-roll '+index,()=>{brollRows.splice(brollRows.indexOf(row),1);card.remove();}));row.card=card;brollRows.push(row);brollBox.append(card);
      for(const a of library.filter(a=>['image','video'].includes(a.kind)))select.append(h('option',{value:a.id},a.alt||'Licensed visual'));select.value=item.id||'';
      const update=()=>{const visual=library.find(a=>a.id===select.value);sourceIn.disabled=visual?.kind!=='video';move.disabled=visual?.kind!=='image';if(sourceIn.disabled)sourceIn.value='0';if(move.disabled)move.checked=false;};row.update=update;select.addEventListener('change',update);if(libraryLoaded)update();
    }
    function addMotion(item={}){
      if(motionRows.length>=8)throw Error('Choose at most eight name titles.');
      const index=++motionSerial,suffix=index===1?'':` ${index}`,name=h('input',{type:'text',maxlength:40,value:item.name||'','aria-label':'Name title text'+suffix}),title=h('input',{type:'text',maxlength:60,value:item.title||'','aria-label':'Name title role'+suffix}),side=choose('Name title side'+suffix,[{id:'left',label:'Left'},{id:'right',label:'Right'}],item.side||'left'),start=h('input',{type:'number',min:0,step:.1,value:item.start??0,'aria-label':'Name title start'+suffix}),duration=h('input',{type:'number',min:data.final_layout.title_min_duration_s,max:30,step:.1,value:item.duration??4,'aria-label':'Name title duration'+suffix});
      const projected=h('p',{class:'mw-help'}),refresh=()=>{projected.textContent=placementTime(start.value);};start.addEventListener('input',refresh);refresh();const row={name,title,side,start,duration};const card=h('div',{class:'ew-decision'},h('h4',{},`Name title ${index}`),field('Name',name),field('Role · optional',title),field('Side',side),field('Start · main-conversation seconds',start),projected,button('Listen at title',()=>{playArtifact(roughCut.render,roughCut.mode,'Reviewed rough cut · title context');seek(Number(start.value));}),field('Duration · seconds',duration),button('Remove name title '+index,()=>{motionRows.splice(motionRows.indexOf(row),1);card.remove();}));motionRows.push(row);motionBox.append(card);
    }
    if(roughCut.mode!=='audio'){
      inspector.append(h('h4',{},'B-roll placements'),brollBox,button('Add B-roll',()=>addBroll()),h('label',{class:'ew-check'},burn,h('span',{},'Burn captions into picture')),h('h4',{},'Name titles'),motionBox,button('Add name title',()=>addMotion()));
      for(const item of initial.broll||[])addBroll(item);for(const item of initial.motion||[])addMotion(item);
    }else inspector.append(h('p',{},'Audio captions are a separate sidecar.'));
    ctx.api.get('library').then(value=>{library=(value.assets||[]).filter(a=>a.license_ok);for(const a of library)if(a.kind==='audio')music.append(h('option',{value:a.id},a.alt||'Licensed recording'));music.value=initial.music?.id||'';musicFields.hidden=!music.value;for(const row of brollRows){for(const a of library.filter(a=>['image','video'].includes(a.kind)))row.select.append(h('option',{value:a.id},a.alt||'Licensed visual'));row.select.value=row.saved.id||'';row.update();}libraryLoaded=true;}).catch(fail);
    let corrections=structuredClone(initial.corrections||roughCut.caption_corrections||[]);
    const correctionPanel=h('details',{},h('summary',{},'Correct caption wording'),h('p',{class:'mw-help'},'Choose a word, then include adjacent words to correct a phrase in this final caption version.'));
    const target=drafts.finalTarget;delete drafts.finalTarget;
    const editor=captionPhraseEditor({rough:roughPayload,corrections:()=>corrections,onChange:value=>{corrections=value;},target,audition:(a,b)=>{playArtifact(roughCut.render,roughCut.mode,'Reviewed rough cut · original caption context');const player=playerSlot.querySelector('video,audio');const stop=()=>{if(player.currentTime>=b+1){player.pause();player.removeEventListener('timeupdate',stop);}};player.addEventListener('timeupdate',stop);seek(Math.max(0,a-1));}});
    correctionPanel.append(editor.node);inspector.append(correctionPanel);if(target)correctionPanel.open=true;editor.open().catch(fail);
    const exportQuality=choose('Final export quality',[{id:'episode_draft',label:'Review copy · 540p · faster'},{id:'episode_hd',label:'Full HD · 1080p'},{id:'episode_uhd',label:'Ultra HD · 2160p'},{id:'master',label:'Authored canvas size'}],initial.preset||'episode_hd');if(roughCut.mode!=='audio')inspector.append(field('Final export quality',exportQuality),h('p',{class:'mw-help'},'Choose the picture size for this finished version. Sources below that size are upscaled; original-byte copies are available in the release package.'));
    const values=(strict=false)=>{const chosen=library.find(a=>a.id===music.value);return {rough:roughPayload,sequence:sequencePayload,preset:exportQuality.value,gain_db:Number(gain.value),caption_style:'podcast',burn:roughCut.mode==='audio'?false:burn.checked,corrections,motion:motionRows.map(m=>({name:m.name.value,title:m.title.value,side:m.side.value,start:Number(m.start.value),duration:Number(m.duration.value)})),broll:brollRows.map(b=>{const asset=library.find(a=>a.id===b.select.value);if(!asset){if(!libraryLoaded&&b.saved.id)return b.saved;if(strict)throw Error('Choose a licensed visual for each B-roll placement.');return {...b.saved,id:b.select.value,start:Number(b.start.value),duration:Number(b.duration.value),source_in:Number(b.sourceIn.value),ken_burns:b.move.checked};}return {id:asset.id,sha256:asset.sha256,start:Number(b.start.value),duration:Number(b.duration.value),source_in:Number(b.sourceIn.value),ken_burns:b.move.checked};}),music:chosen?{id:chosen.id,sha256:chosen.sha256,start:Number(musicStart.value),gain_db:Number(musicGain.value),duck_db:Number(duck.value),fade_in_s:Number(fadeIn.value),fade_out_s:Number(fadeOut.value)}:(!libraryLoaded?initial.music||null:null)};};
    captureDraft=()=>{drafts.final=values();drafts.finalEditing=true;};
    primary.append(button('Render final programme',()=>run('episode-final',values(true)),true));
  }
  async function releaseDesk(){
    const mode=stage,generation=id+':'+session+':'+mode;
    inspector.append(h('h3',{},mode==='clips'?'Keep the complete thought':'Prepare the release'));
    if(!done('final')){primary.append(button('Review the final cut first',()=>{stage='final';draw();},true));return;}
    const payload=data.checkpoints.final.payload,final=data.finals.find(f=>f.sha256===payload.sha256),art=final.render;
    playArtifact(art,final.mode,`Saved final v${payload.version} · ${seconds(art.details.report.duration)}`);
    inspector.append(h('p',{},'Working from this saved final version. Current reviews, sources and licenses are checked before creating a selection.'));
    const releases=await ctx.api.get(`episodes/${id}/view/release?session=${encodeURIComponent(session)}`);
    if(generation!==id+':'+session+':'+stage)return;
    if(mode==='clips'){
      const proposal=await ctx.api.get(`episodes/${id}/view/candidates?session=${encodeURIComponent(session)}`);
      if(generation!==id+':'+session+':'+stage)return;
      const offset=art.details.report.joins_s?.[0]??((art.details.report.joins_samples?.[0]||0)/(art.details.report.sample_rate||48000));
      const name=h('input',{type:'text',maxlength:120,'aria-label':'Selection title',placeholder:'A complete thought'}),start=h('input',{type:'number',min:0,max:proposal.duration,step:.001,value:0,'aria-label':'Selection start seconds'}),end=h('input',{type:'number',min:0,max:proposal.duration,step:.001,value:proposal.duration,'aria-label':'Selection end seconds'});
      inspector.append(h('h3',{},'New selection'),field('Clip title',name),field('Start · main-conversation seconds',start),field('End · main-conversation seconds',end),button('Listen in context',()=>{playArtifact(art,final.mode,'Final programme · selection context');seek(offset+Number(start.value)-5);}),h('p',{class:'mw-help'},proposal.note));
      let page=0;const list=h('div',{class:'ew-candidate-list'});clear(transcript);transcript.append(h('h3',{},'Statement candidates'),list);
      function showCandidates(){clear(list);const items=proposal.candidates.slice(page*30,(page+1)*30);for(const c of items)list.append(button(`${seconds(c.start)} · ${c.text}`,()=>{start.value=c.start;end.value=c.end;name.value=c.text.slice(0,120);playArtifact(art,final.mode,'Final programme · statement context');seek(offset+c.start-5);}));list.append(h('div',{},page?button('Previous statements',()=>{page--;showCandidates();}):null,h('span',{},` ${page*30+1}–${Math.min((page+1)*30,proposal.candidates.length)} of ${proposal.candidates.length} `),(page+1)*30<proposal.candidates.length?button('Next statements',()=>{page++;showCandidates();}):null));}showCandidates();
      primary.append(button('Render selection for review',()=>run('episode-select',{final:payload,title:name.value,start:Number(start.value),end:Number(end.value)}),true));
      const collection=h('section',{class:'ew-release-collection'},h('h3',{},'Your selections'));if(releases.clips.length)transcript.prepend(collection);
      const newest=releases.clips.at(-1);if(newest)playArtifact(newest.artifact,newest.artifact.details.mode,newest.title);
      for(const clip of [...releases.clips].reverse()){
        const card=h('details',{class:'ew-decision',open:clip===newest},h('summary',{},clip.title+' · '+(recorded(clip.keep.state)?'Kept · final v'+payload.version:clip.keep.state==='stale'?'Needs a new review':'Ready to review')),h('p',{},clip.selection?.text||''),h('p',{class:'mw-help'},'Before: '+(clip.selection?.before||'(beginning)')),h('p',{class:'mw-help'},'After: '+(clip.selection?.after||'(end)')),button('Play selection',()=>playArtifact(clip.artifact,clip.artifact.details.mode,clip.title)),h('a',{href:clip.artifact.url,download:''},'Download this selection'));
        if(!recorded(clip.keep.state)){
          const complete=h('input',{type:'checkbox','aria-label':'Complete statement '+clip.title}),context=h('input',{type:'checkbox','aria-label':'Context reviewed '+clip.title});
          card.append(h('label',{class:'ew-check'},complete,h('span',{},'This is a complete statement')),h('label',{class:'ew-check'},context,h('span',{},'I listened to the surrounding context')),button('Review keep: '+clip.title,()=>{if(!complete.checked||!context.checked)throw Error('Review the complete statement and surrounding context first.');return review('keep',{...clip.edit,artifact_id:clip.artifact.id,complete_statement:true,context_reviewed:true},clip.edit.name);}));
        }else{
          card.append(button('Unkeep: '+clip.title,()=>review('keep',clip.keep.payload,clip.edit.name,'revoke')),button('Open kept clips in Media',async()=>{dialog.close();const {openVideo}=await import('./video-workshop.js');await openVideo(ctx);}));
          if(clip.artifact.details.mode!=='audio'){
            const title=h('input',{type:'text',maxlength:releases.short_title_limit,value:clip.title,'aria-label':'Short title '+clip.title}),show=h('input',{type:'text',maxlength:40,value:releases.short_show||'','aria-label':'Short show label '+clip.title}),a=h('input',{type:'number',min:0,step:.001,value:0,'aria-label':'Short start '+clip.title}),b=h('input',{type:'number',min:0,step:.001,value:clip.duration,'aria-label':'Short end '+clip.title}),captions=h('input',{type:'checkbox','aria-label':'Burn short captions '+clip.title});captions.checked=true;
            const crops={},cropBox=h('div',{});for(const source of clip.sources){const custom=h('input',{type:'checkbox','aria-label':'Custom crop '+source.label}),controls={},box=h('div',{hidden:true});for(const [key,value] of Object.entries({x:0,y:0,w:1,h:1})){controls[key]=h('input',{type:'number',min:0,max:1,step:.01,value,'aria-label':'Crop '+key+' '+source.label});box.append(field(key+' · fraction of source',controls[key]));}custom.addEventListener('change',()=>box.hidden=!custom.checked);cropBox.append(h('label',{class:'ew-check'},custom,h('span',{},'Frame '+source.label)),box);crops[source.id]={custom,controls};}
            card.append(h('h4',{},'Portrait short'),field('Opening title · up to '+releases.short_title_limit+' characters',title),field('Show label · up to 40 characters',show),field('Recut start · clip seconds',a),field('Recut end · clip seconds',b),h('label',{class:'ew-check'},captions,h('span',{},'Burn exact captions')),h('p',{class:'mw-help'},'Set each camera’s crop separately. Unchecked cameras use a centred portrait crop; inspect every angle after rendering.'),cropBox,button('Render short: '+clip.title,()=>run('episode-short',{keep:clip.keep.payload,title:title.value,show:show.value,start:Number(a.value),end:Number(b.value),layout:'crop',captions:captions.checked,reframe:null,reframes:Object.fromEntries(Object.entries(crops).filter(([,c])=>c.custom.checked).map(([id,c])=>[id,Object.fromEntries(Object.entries(c.controls).map(([k,v])=>[k,Number(v.value)]))]))})));
          }
        }collection.append(card);
      }
      for(const short of releases.shorts)collection.append(h('details',{class:'ew-decision'},h('summary',{},'Short · '+short.title),button('Play short '+short.title,()=>{playArtifact(short.artifact,'audiovisual',short.title);playerSlot.scrollIntoView({block:'start'});}),h('a',{href:short.artifact.url,download:''},'Download portrait short'),h('p',{},'Rendered draft · inspect framing, title, speech and captions.')));
      return;
    }
    const selectedPackage=[...releases.packages].reverse().find(p=>p.manifest.input.binding.edit.sha256===payload.sha256);
    clear(transcript);const completed=h('section',{class:'ew-release-collection'}),chapterEditor=h('section',{});transcript.append(completed,chapterEditor);transcript.append(h('h3',{},'Chapter suggestions'),h('p',{class:'mw-help'},'Choose a spoken occurrence to anchor a chapter. Chapter times use the complete final programme, including the opening.'));
    const chapters=[],chapterBox=h('div',{});let serial=0,updatePackageDraft=()=>{};
    function addChapter(value={start:0,title:''}){const n=++serial,start=h('input',{type:'number',min:0,max:art.details.report.duration,step:.001,value:value.start,'aria-label':'Chapter '+n+' start'}),title=h('input',{type:'text',maxlength:200,value:value.title,'aria-label':'Chapter '+n+' title'}),row={start,title,locator:value.locator};const box=h('div',{class:'ew-chapter'},field('Programme seconds',start),field('Chapter title',title),button('Listen to chapter '+n,()=>{playArtifact(art,final.mode,'Final programme · chapter');seek(Number(start.value));}),button('Remove chapter '+n,()=>{chapters.splice(chapters.indexOf(row),1);box.remove();updatePackageDraft();}));start.addEventListener('input',()=>row.locator=null);chapters.push(row);chapterBox.append(box);updatePackageDraft();}
    for(const chapter of selectedPackage?.manifest.input.chapters.chapters||[{start:0,title:'Opening'}])addChapter(chapter);const cues=art.details.caption.cues||[];let chapterPage=0;const suggestionList=h('div',{});transcript.append(suggestionList);
    function chapterSuggestions(){clear(suggestionList);for(const cue of cues.slice(chapterPage*25,(chapterPage+1)*25)){const word=cue.words?.find(w=>w.occurrence_id&&w.word_id&&w.transcript_sha256);if(!word)continue;suggestionList.append(button(`${seconds(word.start)} · ${cue.text}`,()=>addChapter({start:word.start,title:cue.text.slice(0,200),locator:{occurrence_id:word.occurrence_id,transcript_sha256:word.transcript_sha256,word_id:word.word_id}})));}if(chapterPage)suggestionList.append(button('Previous chapter suggestions',()=>{chapterPage--;chapterSuggestions();}));if((chapterPage+1)*25<cues.length)suggestionList.append(button('Next chapter suggestions',()=>{chapterPage++;chapterSuggestions();}));}chapterSuggestions();
    const originals=h('input',{type:'checkbox','aria-label':'Include full-quality original recordings'}),mp3=h('input',{type:'checkbox','aria-label':'MP3 podcast audio'}),aac=h('input',{type:'checkbox','aria-label':'AAC podcast audio'}),copy=h('textarea',{'aria-label':'Release description',rows:5,maxlength:20000});mp3.checked=selectedPackage?selectedPackage.manifest.audio.some(a=>a.format==='mp3'):true;aac.checked=selectedPackage?selectedPackage.manifest.audio.some(a=>a.format==='aac'):true;originals.checked=!!selectedPackage?.files.some(f=>f.role==='original_source');copy.value=selectedPackage?.manifest.input.copy||'';
    inspector.append(h('h4',{},'Delivery files'),h('p',{},`Selected final v${payload.version} · ${seconds(art.details.report.duration)} · ${final.mode==='audio'?'audio master':'finished video'}`),h('p',{class:'mw-help'},'The selected final and caption bytes are preserved. MP3 and AAC include measured podcast loudness and chapters. Original recordings are optional full-quality copies.'),h('label',{class:'ew-check'},mp3,h('span',{},'MP3 podcast audio')),h('label',{class:'ew-check'},aac,h('span',{},'AAC podcast audio')),h('label',{class:'ew-check'},originals,h('span',{},'Include full-quality original recordings')));if(selectedPackage){
      const draftNote=h('p',{class:'mw-help'});inspector.append(draftNote);
      updatePackageDraft=()=>{const same=originals.checked===!!selectedPackage.files.some(f=>f.role==='original_source')&&copy.value===(selectedPackage.manifest.input.copy||'')&&JSON.stringify([...(mp3.checked?['mp3']:[]),...(aac.checked?['aac']:[])].sort())===JSON.stringify(selectedPackage.manifest.audio.map(a=>a.format).sort())&&JSON.stringify(chapters.map(c=>[Number(c.start.value),c.title.value,c.locator?.occurrence_id||null,c.locator?.transcript_sha256||null,c.locator?.word_id||null]))===JSON.stringify(selectedPackage.manifest.input.chapters.chapters.map(c=>[c.start,c.title,c.locator?.occurrence_id||null,c.locator?.transcript_sha256||null,c.locator?.word_id||null]));draftNote.textContent=same?'These choices match the selected package. Changes create a new package.':'Unbuilt changes. The selected package still contains its saved settings.';};
      for(const node of [inspector,chapterEditor])for(const event of ['input','change'])node.addEventListener(event,updatePackageDraft);updatePackageDraft();
    }
    chapterEditor.append(h('h3',{},'Chapters'),chapterBox,button('Add chapter',()=>addChapter()),field('Release description',copy));
    primary.append(button('Build release package',()=>run('episode-package',{final:payload,chapters:chapters.map(c=>({start:Number(c.start.value),title:c.title.value,...(c.locator?{locator:c.locator}:{})})).sort((a,b)=>a.start-b.start),formats:[...(mp3.checked?['mp3']:[]),...(aac.checked?['aac']:[])],originals:originals.checked,copy:copy.value}),true));
    for(const pack of [...releases.packages].reverse()){
      const selected=pack===selectedPackage,originalCount=pack.files.filter(f=>f.role==='original_source').length;
      completed.append(h('details',{class:'ew-package-result',open:selected},h('summary',{},`${selected?'Ready to download':'Earlier package'} · final v${pack.manifest.input.binding.edit.version}`),h('p',{},`${pack.manifest.input.render.quality.video?('MP4 video · '+pack.manifest.input.render.quality.video.width+'×'+pack.manifest.input.render.quality.video.height+' · '):'Audio master · '}${pack.manifest.audio.map(a=>a.format.toUpperCase()).join(' + ')} · ${originalCount?originalCount+' original recording'+(originalCount===1?'':'s')+' included':'Original recordings excluded'} · ${pack.manifest.input.chapters.chapters.length} chapter${pack.manifest.input.chapters.chapters.length===1?'':'s'}`),h('div',{class:'ew-release-actions'},h('a',{href:pack.preview_url,target:'_blank',rel:'noopener'},'Play package'),h('a',{href:pack.url,download:'',class:'primary'},'Download release ZIP')),h('p',{},'Ready for your publishing workflow. Nothing has been queued or sent.'),h('details',{},h('summary',{},'Files & selected version'),h('p',{class:'ew-hash'},pack.manifest.input.binding.edit.sha256),h('div',{class:'ew-release-files'},pack.files.map(f=>h('a',{href:f.url,download:''},f.file))))));
    }
    const timeline=h('input',{type:'text',maxlength:120,value:data.label+' · final v'+payload.version,'aria-label':'Resolve timeline name'}),subtitles=h('input',{type:'checkbox','aria-label':'Try supported Resolve subtitle import'}),sources=h('input',{type:'checkbox','aria-label':'Create source-body reference timeline'}),layers=h('input',{type:'checkbox','aria-label':'Include alternate reference layers'}),proxies=h('input',{type:'checkbox','aria-label':'Link verified Resolve proxies'});
    const resolveBox=h('details',{},h('summary',{},'Review in DaVinci Resolve'),h('p',{},'Start Resolve and its bridge, then create a timeline from this finished master. Its audio mix and picture effects stay baked. Success requires a verified Resolve response.'),field('New timeline name',timeline),h('label',{class:'ew-check'},subtitles,h('span',{},'Try subtitle import if supported')));
    if(final.mode!=='audio')resolveBox.append(h('label',{class:'ew-check'},sources,h('span',{},'Also create a source-body reference timeline')),h('label',{class:'ew-check'},layers,h('span',{},'Include alternate camera and editable dialogue reference layers')),h('label',{class:'ew-check'},proxies,h('span',{},'Link existing verified Resolve proxies')),h('p',{class:'mw-help'},'Reference only: the companion excludes opening/closing components and cannot reproduce drift retiming, fades, ducking, gain envelopes or all graphic effects. Compare it with the finished master. Missing proxies use originals.'));
    resolveBox.append(button('Send to Resolve',()=>run('episode-handoff',{final:payload,name:timeline.value,subtitles:subtitles.checked,sources:sources.checked,layers:layers.checked,proxies:proxies.checked})));
    for(const r of releases.handoffs){const result=h('section',{},h('p',{},'Finished master: '+r.report.timeline));if(r.companion)result.append(h('p',{},'Source companion: '+r.companion.status+(r.companion.report?' · '+r.companion.report.timeline:'')),r.companion.reason?h('p',{},r.companion.reason):null,h('details',{},h('summary',{},'Reference limitations'),r.companion.limitations.map(x=>h('p',{},x))));resolveBox.append(result);}inspector.append(resolveBox);
  }

  async function review(kind,payload,scope=session,decision='confirm'){
    if(busy||closed)return;
    const context={episode:id,session,stage},body={episode:id,grant_revision:data.grant_revision,kind,session:scope,payload:structuredClone(payload),decision};
    const current=()=>!closed&&id===context.episode&&session===context.session&&stage===context.stage;
    busy=true;const unlock=lockSubmission();clear(primary);
    state.textContent='Checking recorded sources and current permissions before review…';
    primary.append(h('span',{role:'status'},'Preparing this review. Playback remains available while sources and permissions are checked.'));
    let prepared,failure=null;
    try{
      prepared=await ctx.api.post('episodes/prepare',body);
      if(!current())return;
      const req=prepared.request;
      await ctx.api.post('episodes/view',{token:prepared.token,payload_sha256:req.payload_sha256,preview_sha256:req.dependencies.preview_sha256??null});
    }catch(e){failure=e;}
    finally{busy=false;unlock();}
    if(!current())return;
    if(failure){fail(failure);clear(primary);primary.append(button('Retry preparing review',()=>review(kind,body.payload,scope,decision),true),button('Change proposal',draw));return;}
    clear(primary);
    if(kind==='assembly'){
      const a=data.assemblies.find(a=>a.version===body.payload.version&&a.sha256===body.payload.sha256);
      for(const warning of a?.render?.details?.report?.warnings||[])primary.append(h('p',{class:'ew-review-warning','data-render-version':String(a.version)},warning));
      if(omittedInside(a?.coverage)>1e-9)primary.append(h('p',{class:'ew-review-warning'},`${durationLabel(omittedInside(a.coverage))} omitted inside confirmed bounds`));
    }
    inspector.append(h('section',{class:'ew-decision'},h('h3',{},'Prepared decision'),h('p',{},`${decision==='revoke'?'Withdraw confirmation: ':''}${kind} · ${scope}`),h('pre',{},JSON.stringify(body.payload,null,2)),h('p',{},'Inspect this exact evidence and playback. Confirmation opens a native sheet and device-owner authentication.')));
    state.textContent='Exact evidence ready for native review';
    if(window.aveNative?.confirmEpisode)primary.append(button('Confirm in native review',async()=>{
      if(busy||!current())return;
      busy=true;const unlockConfirm=lockSubmission();state.textContent='Confirming this reviewed version…';let failure=null;
      try{await window.aveNative.confirmEpisode(prepared.token);}catch(e){failure=e;}
      finally{busy=false;unlockConfirm();}
      if(!current())return;
      if(failure){state.textContent='Confirmation needs attention';fail(failure);return;}
      await refresh(kind!=='keep');
    },true));
    else primary.append(h('p',{},'Open this workspace in the native app to confirm this exact decision.'));
    primary.append(button('Change proposal',draw));
  }
  try{if(id)await refresh(true);else await catalog();}catch(e){fail(e);}
}
