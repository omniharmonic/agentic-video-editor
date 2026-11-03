import {openMedia} from './media-workshop.js';
import {openAssetPicker} from './library.js';
// Deterministic, local production. All untrusted source text uses DOM text nodes.
import {registerDraft} from './navigation-guard.js';
import {h, clear} from './dom.js';

export async function mount(root, ctx) {
  let alive=true, data={sources:[],jobs:[]}, selected='', sentence='', polling=null, xhr=null, loading=false, uploadOpen=false, activeJobId=null, jobsSignature='';
  const error=h('p',{class:'wb-error',role:'alert',hidden:true}),notice=h('p',{class:'wb-notice',role:'status'});
  const sourceSelect=h('select',{'aria-label':'Source material'}), choices=h('div',{class:'wb-sentences'});
  const style=h('select',{'aria-label':'Visual style'},['paper','ink','clay'].map(v=>h('option',{value:v},v[0].toUpperCase()+v.slice(1))));
  const format=h('select',{'aria-label':'Image format'},['4x5','1x1','9x16'].map(v=>h('option',{value:v},v.replace('x',':'))));
  const speakers=h('input',{type:'number',min:1,max:16,value:1,'aria-label':'Speaker count'});
  const renderButton=h('button',{type:'button',class:'primary',onclick:()=>run('render',{source_id:selected,sentence_id:sentence,recipe:'quote-post',style:style.value,format:format.value})},'Render quote image');
  const transcribeButton=h('button',{type:'button',class:'primary',onclick:()=>run('transcribe',{source_id:selected,speakers:Number(speakers.value)})},'Transcribe locally');
  const quoteTools=h('div',{class:'wb-options'},field('Visual style',style),field('Image format',format),renderButton);
  const recordingTools=h('div',{class:'wb-recording',hidden:true},field('Expected speakers',speakers),h('p',{},'One speaker skips diarization. Names remain unconfirmed.'),transcribeButton);
  const results=h('section',{class:'wb-results','aria-label':'Production results'}),warnings=h('div',{class:'wb-warnings'});
  const uploadInput=h('input',{type:'file',accept:'.md,.markdown,.txt,.docx,.pdf,.wav,.mp3,.m4a,.aac,.flac,.ogg,.aif,.aiff,.mp4,.mov,.mkv,.m4v,.webm,.avi','aria-label':'Upload source file'});
  const progress=h('progress',{max:100,value:0,hidden:true,'aria-label':'Upload progress'});
  const uploadButton=h('button',{type:'submit'},'Import source');
  const filename=h('span',{class:'wb-filename'});
  const uploadForm=h('form',{class:'wb-upload',onsubmit:e=>{e.preventDefault();upload();}},field('Add writing or a recording',uploadInput),uploadButton,progress);
  const mobileTabs=h('div',{class:'wb-mobile-tabs','aria-label':'Workbench panels'});
  function panel(name){root.dataset.wbPanel=name;for(const b of mobileTabs.children)b.setAttribute('aria-pressed',String(b.dataset.panel===name));}
  for(const name of ['source','results'])mobileTabs.append(h('button',{type:'button','data-panel':name,onclick:()=>panel(name)},name==='source'?'Source & options':'Results'));
  panel('source');
  const addSource=h('button',{type:'button',class:'wb-add-source','aria-expanded':'false',onclick:()=>{if(window.aveNative?.importFiles){window.aveNative.importFiles().then(()=>refreshSources(true)).catch(fail);return;}uploadOpen=!uploadOpen;uploadForm.hidden=!uploadOpen;addSource.setAttribute('aria-expanded',String(uploadOpen));if(uploadOpen)uploadInput.focus();}},'Add source');
  root.append(h('header',{class:'wb-heading'},h('div',{},h('h1',{},'Sources'),h('p',{},'Turn your source material into a draft you can inspect.')),h('div',{class:'wb-heading-actions'},window.aveNative?.selectEpisode?h('button',{type:'button',onclick:()=>window.aveNative.selectEpisode().then(()=>openMedia(ctx,{mediaKind:'episode'})).catch(fail)},'Open project folder'):null,addSource,h('button',{type:'button',onclick:()=>openMedia(ctx,{mediaKind:'video'})},'Edit a video'),h('button',{type:'button',onclick:()=>openMedia(ctx,{mediaKind:'episode'})},'Episodes'),h('a',{href:'#/desk'},'Source overview'),h('a',{href:'#/pipeline'},'Open production'))),filename,
    error,notice,warnings,uploadForm,mobileTabs,h('div',{class:'wb-layout'},h('section',{class:'wb-source'},h('h2',{},'Choose your source'),field('Source material',sourceSelect),choices,quoteTools,recordingTools),results));
  function field(label,node){return h('label',{class:'wb-field'},h('span',{},label),node);}
  function fail(e){if(!alive)return;error.textContent=e.message;error.hidden=false;}
  function active(){return data.jobs.some(j=>['queued','running'].includes(j.status));}
  function updateButtons(){const src=data.sources.find(s=>s.source_id===selected);renderButton.disabled=data.read_only||loading||active()||!sentence;transcribeButton.disabled=data.read_only||loading||active()||!src?.ready;uploadButton.disabled=data.read_only||loading||active();}
  function drawSelection(){
    const src=data.sources.find(s=>s.source_id===selected);clear(choices);quoteTools.hidden=!src||src.kind!=='writing';recordingTools.hidden=!src||['writing','document'].includes(src.kind)||src.production_reference;
    if(!src){choices.append(h('p',{class:'wb-muted'},'Import a UTF-8 essay or an audio/video recording to begin.'));updateButtons();return;}
    if(src.kind==='writing'){
      choices.append(h('p',{class:'wb-muted'},'Select a complete, verbatim sentence (12–240 characters). You choose the quote.'));
      for(const s of src.sentences){const length=[...s.text].length,valid=length>=12&&length<=240;const radio=h('input',{type:'radio',name:'quote-sentence',value:s.id,checked:sentence===s.id,disabled:!valid,'aria-label':`Select ${s.id}`,onchange:()=>{sentence=s.id;updateButtons();}});choices.append(h('label',{class:'wb-sentence'+(!valid?' wb-ineligible':'')},radio,h('span',{},s.text,h('small',{},`${length} characters${valid?'':' · outside quote length'}`))));}
      choices.append(h('details',{class:'wb-details'},h('summary',{},'Source references'),h('p',{},src.source_id),h('ul',{},src.sentences.map(s=>h('li',{},s.id)))));
    }else if(src.kind==='document'){
      drawManuscript(src,0);
    }else{
      choices.append(h('p',{},src.label),h('p',{class:'wb-muted'},src.ready?`${src.duration.toFixed(2)} seconds · ${src.has_audio===false?'silent video preview ready':'playback ready'}`:'Probe and audio extraction pending. The original upload is preserved.'));
      for(const preview of src.previews||[])choices.append(h('figure',{class:'wb-source-player'},h(preview.kind,{controls:true,preload:'metadata',src:preview.url,'aria-label':preview.label}),h('figcaption',{},preview.label)));
      if(src.production_reference)choices.append(h('a',{href:src.previews[0].url,download:''},'Download production audio'),h('button',{type:'button',onclick:()=>openMedia(ctx,{mediaKind:'audio',asset:src.asset})},'Open saved programme'),h('p',{},'Production reference • excluded from social delivery'));else choices.append(h('button',{type:'button',onclick:()=>openMedia(ctx,{mediaKind:src.kind,sourceId:src.source_id})},'Edit in Media'),h('p',{class:'wb-muted'},'After transcription: confirm speaker names and camera mapping before assembly or cuts.'));
    }
    updateButtons();
  }
  async function drawManuscript(src,n){
    clear(choices);choices.append(h('p',{},src.ready?'Original retained. Browse extracted sections without loading the whole manuscript.':'Extraction pending or needs attention. See the job diagnostics; the complete original is retained.'));
    if(!src.ready)return;
    try{const data=await ctx.api.get(`workbench/manuscripts/${src.source_id}?page=${n}`);if(selected!==src.source_id)return;
      if(data.warnings?.length)choices.append(h('div',{class:'wb-source-warnings',role:'status'},h('strong',{},'Partial text extraction — original retained'),...data.warnings.map(w=>h('p',{},w))));
      if(data.locator_basis)choices.append(h('p',{},'Character ranges refer to '+data.locator_basis+'.'));
      const jump=h('input',{type:'number',min:1,max:data.pages,value:n+1,'aria-label':'Manuscript section page'});
      choices.append(h('div',{class:'wb-options'},h('button',{type:'button',disabled:n===0,onclick:()=>drawManuscript(src,n-1)},'Previous sections'),field(`Section page of ${data.pages}`,jump),h('button',{type:'button',onclick:()=>drawManuscript(src,Number(jump.value)-1)},'Go to sections'),h('button',{type:'button',disabled:n+1===data.pages,onclick:()=>drawManuscript(src,n+1)},'Next sections')));
      for(const section of data.sections)choices.append(h('article',{class:'wb-sentence'},h('div',{},h('small',{},`${section.id} · ${section.page?'PDF page '+section.page+' · ':''}characters ${section.start}–${section.end}`),h('p',{},section.text),h('button',{type:'button',onclick:()=>openAssetPicker(ctx,{title:'Choose a design image',kind:'image',allowDraft:true,onPick:a=>openMedia(ctx,{asset:a.id,source:{...section,source_id:src.source_id}})})},'Design from this section'))));
    }catch(e){fail(e);}
  }
  sourceSelect.addEventListener('change',()=>{selected=sourceSelect.value;sentence='';drawSelection();});
  async function refreshSources(force=false){
    const old=JSON.stringify(data.sources);data=await ctx.api.get('workbench');if(!alive)return;
    if(force||old!==JSON.stringify(data.sources)){
      sourceSelect.replaceChildren(h('option',{value:''},'Choose a source'),...data.sources.map(s=>h('option',{value:s.source_id},s.label)));
      if(!selected&&data.sources.length)selected=data.sources[0].source_id;
      sourceSelect.value=selected;drawSelection();
    }
    clear(warnings);for(const message of data.warnings||[])warnings.append(h('p',{},message));
    uploadForm.hidden=data.sources.length>0&&!uploadOpen;
    addSource.setAttribute('aria-expanded',String(!uploadForm.hidden));
    if(activeJobId){const job=data.jobs.find(j=>j.id===activeJobId);if(job&&!['queued','running'].includes(job.status)){notice.textContent='';activeJobId=null;}}
    drawJobs();updateButtons();
  }
  function drawJobs(){
    const signature=JSON.stringify(data.jobs);if(signature===jobsSignature)return;jobsSignature=signature;
    clear(results).append(h('h2',{},'Results'));
    if(!data.jobs.length)results.append(h('div',{class:'wb-empty'},h('p',{},'Your rendered image or transcript will appear here.'),h('p',{class:'wb-muted'},'Rendering creates an editable draft. Publishing review stays separate.')));
    for(const j of data.jobs.slice(0,12)){
      const labels={render:'Quote image',import:'Recording import',transcribe:'Local transcript'};
      const card=h('article',{class:'wb-job','data-job':j.id},h('header',{},h('h3',{},labels[j.operation]),h('span',{class:`wb-state wb-${j.status}`},j.status==='done'?({render:'Rendered',transcribe:'Transcribed',import:'Imported'}[j.operation]||'Completed'):j.status)));
      if(j.diagnostics)card.append(h('pre',{class:'wb-diagnostic'},j.diagnostics));
      if(['failed','interrupted'].includes(j.status))card.append(h('button',{type:'button',disabled:active()||loading,onclick:()=>run('retry',{job_id:j.id})},'Retry job'));
      const r=j.result;
      if(r){
        if(r.assets?.length){for(const asset of r.assets){if(['video','audio'].includes(asset.kind))card.append(h(asset.kind,{class:'wb-artifact',src:asset.url,controls:true,preload:'metadata','aria-label':'Finished '+asset.kind}),h('a',{href:asset.url,download:''},'Download finished '+asset.kind));else card.append(h('a',{href:asset.url,target:'_blank',rel:'noopener'},h('img',{class:'wb-artifact',src:asset.url,alt:asset.alt||'Rendered quote image',width:asset.w,height:asset.h})));}}
        if(r.critic)card.append(h('p',{},r.critic.ok?'Visual checks passed':'Visual checks need review'),h('p',{class:'wb-muted'},`Contrast ${r.critic.contrast??'not measured'}`),...(r.critic.notes||[]).map(n=>h('p',{},typeof n==='string'?n:JSON.stringify(n))));
        if(r.note)card.append(h('p',{class:r.attachment_conflict?'wb-error':''},r.note));
        if(r.utterances){const transcript=h('div',{class:'wb-transcript'});for(const u of r.utterances)transcript.append(h('p',{},h('strong',{},`${u.start.toFixed(1)}s · ${u.speaker||'Unassigned'} `),u.text));card.append(transcript,h('details',{class:'wb-details'},h('summary',{},'Transcript details'),h('p',{},`${r.words} words · ${r.engine?.asr||'local transcription'}`),h('a',{href:r.transcript_url,target:'_blank',rel:'noopener'},'Open transcript JSON')));}
        if(r.preview_truncated)card.append(h('p',{class:'wb-muted'},'This preview shows up to100 utterances. Open this recording in Media to browse bounded transcript pages.'));
        if(r.next)card.append(h('p',{class:'wb-muted'},r.next));
      }
      if(j.post)card.append(h('a',{href:`#/post/${j.post}`,class:'wb-open-draft'},'Open draft'));
      results.append(card);
    }
  }
  async function run(op,body){loading=true;error.hidden=true;updateButtons();try{const job=await ctx.api.post(`workbench/${op}`,body);if(!alive)return;activeJobId=job.id;panel('results');notice.textContent=`${job.operation} started. You can keep this page open or return later.`;await refreshSources();}catch(e){fail(e);}finally{loading=false;if(alive)updateButtons();}}
  async function upload(){const file=uploadInput.files[0];if(!file){fail(new Error('Choose a source file first.'));return;}loading=true;error.hidden=true;progress.hidden=false;progress.value=0;updateButtons();
    try{const response=await new Promise((resolve,reject)=>{xhr=new XMLHttpRequest();xhr.open('PUT',`/api/studio/workbench/upload?name=${encodeURIComponent(file.name)}`);xhr.upload.onprogress=e=>{if(alive&&e.lengthComputable)progress.value=e.loaded/e.total*100;};xhr.onload=()=>{let body;try{body=JSON.parse(xhr.responseText);}catch{reject(new Error('The upload returned an invalid response. Your selected file is preserved.'));return;}xhr.status>=200&&xhr.status<300?resolve(body):reject(new Error(body.error||'Upload failed'));};xhr.onerror=()=>reject(new Error('Upload connection failed. Retry with the selected file.'));xhr.onabort=()=>reject(new Error('Upload cancelled'));xhr.send(file);});if(!alive)return;selected=response.source_id;uploadInput.value='';sentence='';uploadOpen=false;filename.textContent=`Imported ${file.name}`;if(response.job)activeJobId=response.job.id;notice.textContent=response.job?'Source preserved. Probe and audio extraction started.':'Source imported. Select a sentence below.';await refreshSources(true);
    }catch(e){fail(e);}finally{loading=false;if(alive){progress.hidden=true;updateButtons();}}}
  async function poll(){if(!alive)return;try{await refreshSources();}catch(e){fail(e);}finally{if(alive)polling=setTimeout(poll,1500);}}
  const unregister=registerDraft({autoFlush:false,dirty:()=>!!uploadInput.files.length||loading,flush:async()=>{if(loading)throw Error('Wait for the current upload to finish.');await upload();},discard:()=>{uploadInput.value='';}});
  await refreshSources(true);polling=setTimeout(poll,1500);
  return {refresh:async()=>{},unmount(){unregister();alive=false;clearTimeout(polling);if(xhr&&xhr.readyState!==4)xhr.abort();}};
}
