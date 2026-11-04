// Shared small forms for production and campaign planning. All text stays data.
import { h } from '/app/shared/dom.js';
export const button = (label, fn, cls='') => h('button', {type:'button', class:cls, onclick:fn}, label);
export function select(label, values, value, change) {
  const el=h('select', {'aria-label':label}, values.map(([id,name])=>h('option',{value:id},name)));
  el.value=value ?? '';
  if(change) {
    el.addEventListener('change',()=>change(el.value));
    // Explicit keyboard movement also works when the host webview does not open
    // the OS select popup. One key changes one value and retains native labeling.
    el.addEventListener('keydown',e=>{
      if(e.altKey||e.ctrlKey||e.metaKey||el.multiple)return;
      const next={ArrowDown:Math.min(el.options.length-1,el.selectedIndex+1),ArrowUp:Math.max(0,el.selectedIndex-1),Home:0,End:el.options.length-1}[e.key];
      if(next===undefined)return;e.preventDefault();
      if(next!==el.selectedIndex){el.selectedIndex=next;el.dispatchEvent(new Event('change',{bubbles:true}));}
    });
  }
  return el;
}
export function input(label, value='', type='text') {return h('input',{'aria-label':label,type,value:value ?? ''});}
export function field(label, control) {return h('label',{class:'prod-field'},h('span',{},label),control);}
export function dialog(title, fields, save, {submit='Save', note=''}={}) {
  const previous=document.activeElement, error=h('div',{role:'alert',class:'prod-error'});
  const form=h('form',{}, h('header',{},h('h2',{},title),button('Close',()=>dlg.close())),note?h('p',{class:'prod-muted'},note):null,fields,error);
  const send=h('button',{type:'submit',class:'primary'},submit);
  form.append(h('footer',{},button('Cancel',()=>dlg.close()),send));
  const dlg=h('dialog',{class:'production-dialog'},form); document.body.append(dlg);
  form.addEventListener('submit',async e=>{e.preventDefault();send.disabled=true;error.textContent='';try{await save();dlg.close();}catch(err){error.textContent=err.message;if(err.latest)error.append(h('p',{},'Someone else changed this record. Your input is preserved. Close and reopen to compare the latest version.'));}finally{send.disabled=false;}});
  dlg.addEventListener('close',()=>{dlg.remove();previous?.focus();});dlg.showModal();return dlg;
}
export function media(p, cls='') {
  const t=p.thumbnail||{};
  if(t.url && t.kind==='image')return h('img',{class:'prod-thumb '+cls,src:t.url,alt:t.alt||'',loading:'lazy'});
  if(t.url && t.kind==='video')return h('video',{class:'prod-thumb '+cls,src:t.url,muted:true,preload:'metadata','aria-label':t.alt||'Video'});
  return h('span',{class:'prod-thumb prod-no-media '+cls,'aria-label':t.kind==='missing'?'Media unavailable':'Text content'},t.kind==='missing'?'Missing':'Aa');
}
