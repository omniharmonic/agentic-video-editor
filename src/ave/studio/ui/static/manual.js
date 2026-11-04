// Exact-file delivery and protected human reconciliation share the queue's review.
import { h } from '/app/shared/panels.js';
import { buildPackage, newRequestId, packageHistory, packageState, publicationRequest } from '/app/shared/plan.js';

export function openManualExport(ctx, post, variant = null) {
  const channel=variant?.platform?.replaceAll('_',' ') || 'your channel';
  const dialog=h('dialog.manual-dialog',{'aria-labelledby':'manual-title'});
  const status=h('div.manual-status',{role:'status','aria-live':'polite'}), history=h('section.manual-history',{'aria-label':'Package history'});
  const copyNotice=h('p',{role:'status','aria-live':'polite'});
  let alive=true;
  const close=()=>{alive=false;dialog.close();dialog.remove();};
  dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
  const copy=(label,text)=>h('button',{type:'button',onclick:async()=>{try{await navigator.clipboard.writeText(text||'');copyNotice.replaceChildren(h('span',`${label} copied`));}catch{copyNotice.replaceChildren(h('textarea',{readOnly:true,'aria-label':label,value:text||''}),h('p','Select and copy the text above.'));}}},`Copy ${label.toLowerCase()}`);
  function reportForm(pack, target){
    target.replaceChildren();
    const url=h('input',{type:'url',required:true,placeholder:'https://…','aria-label':'Published URL'});
    const time=h('input',{type:'datetime-local',required:true,'aria-label':'Publication time'});
    const changed=h('textarea',{'aria-label':'Changes made in the platform app',placeholder:'Music, tags, caption edits, or other changes (optional)',maxLength:2000});
    const mismatch=h('input',{'aria-label':'Alternate link explanation',maxLength:500,placeholder:'Only needed for a link outside this platform'});
    const actor=h('input',{required:true,'aria-label':'Reported by',placeholder:'Your name',maxLength:120});
    const message=h('p',{role:'status'}), native=globalThis.aveNative?.recordManualPublication;
    const submit=h('button.primary',{type:'submit',disabled:!native},'Record publication');
    const form=h('form.manual-report',h('h3','I posted this package'),h('p',`Exported revision ${pack.rev} · ${pack.account || channel}. This records your report; it does not independently verify the platform.`),
      !native?h('p.manual-hint','Protected recording requires the native desktop connection. This browser can prepare and download packages; publication recording becomes available when that connection is installed.'):null,
      h('label','Published URL',url),h('label','Publication time',time),h('label','Reported by',actor),h('label','Alternate link explanation',mismatch),h('label','Changes made in the platform app',changed),
      submit,h('button',{type:'button',onclick:()=>target.replaceChildren()},'Cancel'),message);
    const requestId=newRequestId();
    form.addEventListener('submit',async e=>{e.preventDefault();if(!native)return;submit.disabled=true;try{
      const result=await native(publicationRequest({post,pack,url:url.value,at:time.value,by:actor.value,changed:changed.value,mismatch:mismatch.value,requestId}));
      status.replaceChildren(h('p.manual-success',`Published · reported by ${result.by}. The package history below preserves the exact files and your report.`));
      await loadHistory();
    }catch(error){message.textContent=error.message;submit.disabled=false;}});
    target.append(form);url.focus();
  }
  function packageCard(pack, result=null){
    const media=pack.media||[],form=h('div'),{publication,published,label}=packageState(pack,variant);
    const zip=result?.zip_url || pack.zip_url;
    return h('article.manual-package',h('div.manual-package-head',h('strong',label),h('span.st-hint',new Date(pack.created).toLocaleString())),
      h('p',`${media.length} original ${media.length===1?'file':'files'} · revision ${pack.rev} · ${pack.account||channel}`),
      h('a.btn.primary',{href:zip,download:`${pack.package_id}.zip`},'Download full-resolution package'),
      globalThis.aveNative?h('div.manual-actions',h('button',{type:'button',onclick:()=>globalThis.aveNative.sharePackage(pack.package_id).catch(e=>status.textContent=e.message)},'Share / AirDrop…'),h('button',{type:'button',onclick:()=>globalThis.aveNative.revealPackage(pack.package_id).catch(e=>status.textContent=e.message)},'Show in Finder')):null,
      h('div.manual-actions',copy('Caption',pack.caption),copy('First comment',pack.first_comment),copy('Alt text',media.map(m=>`${m.position}. ${m.alt}`).join('\n\n'))),
      h('p.st-hint',pack.cover?'Separate cover included. Choose it as a cover, never an extra slide.':'The package keeps the original selected bytes and slide order.'),
      h('details',h('summary','Files and native steps'),h('ol',media.map(m=>h('li',`${m.position}. ${m.filename}${m.width?` · ${m.width} × ${m.height}`:''}`,globalThis.aveNative?h('button',{type:'button',onclick:()=>globalThis.aveNative.sharePackage(pack.package_id,m.filename).catch(e=>status.textContent=e.message)},'Share original…'):null))),h('ul',(pack.native_instructions||[]).map(x=>h('li',x)))),
      published?h('a',{href:publication.url,target:'_blank',rel:'noopener'},'Open reported publication'):pack.reviewed?h('button',{type:'button',onclick:()=>reportForm(pack,form)},'I posted this'):null,form);
  }
  async function loadHistory(){try{const r=await packageHistory(ctx.api,post,variant);if(alive)history.replaceChildren(h('h3','Package history'),...(r.packages||[]).map(p=>packageCard(p)));}catch(e){if(alive)history.replaceChildren(h('p',e.message));}}
  const controls=h('div.manual-actions');
  async function build(draft){for(const b of controls.querySelectorAll('button'))b.disabled=true;status.textContent='Preparing original files and captions…';try{
    const r=await buildPackage(ctx.api,post,variant,draft);if(!alive)return;
    if(!r.ok){status.textContent=(r.warnings||['Export unavailable']).join(' ');return;}
    status.replaceChildren(h('p.manual-success','Package prepared. Downloading has not published anything. Find the exact files in Package history below.'));await loadHistory();
  }catch(e){if(alive)status.replaceChildren(h('p',{role:'alert'},e.message),h('p','Your post is unchanged. Return to the composer to resolve review or save changes.'));}
  finally{if(alive)for(const b of controls.querySelectorAll('button'))b.disabled=false;}}
  controls.append(h('button.primary',{type:'button',onclick:()=>build(false)},'Export reviewed post'),h('button',{type:'button',onclick:()=>build(true)},'Download unreviewed draft'));
  dialog.append(h('header',h('h2',{id:'manual-title'},`Deliver to ${channel}`),h('button',{type:'button',onclick:close,'aria-label':'Close export'},'Close')),
    h('p','Original files, exact caption, first comment, alt text and credits — ready for the platform app.'),h('p.manual-hint','Download first. After posting, record the URL against the exact reviewed package.'),controls,copyNotice,status,history);
  document.body.append(dialog);dialog.showModal();loadHistory();return {close};
}
