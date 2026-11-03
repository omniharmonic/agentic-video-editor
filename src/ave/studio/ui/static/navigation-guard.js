import {h} from './dom.js';
const editors=new Set();
let pending=null;
export function registerDraft(controller){editors.add(controller);return ()=>editors.delete(controller);}
export const hasUnsaved=()=>[...editors].some(c=>c.dirty());
window.addEventListener('beforeunload',e=>{if(hasUnsaved()){e.preventDefault();e.returnValue='';}});
export async function leaveSafely(){
 if(pending)return pending;
 const dirty=[...editors].filter(c=>c.dirty());if(!dirty.length)return true;
 for(const c of dirty){if(c.autoFlush===false)continue;try{await c.flush?.();}catch{}}
 if(!dirty.some(c=>c.dirty()))return true;
 pending=new Promise(resolve=>{
  const error=h('p',{role:'alert'}),dlg=h('dialog',{class:'production-dialog'},h('h2',{},'Keep your changes?'),h('p',{},'Your changes have not been saved. Stay here, save them, or discard them before leaving.'),error);
  const finish=value=>{dlg.close();resolve(value);pending=null;};
  dlg.append(h('footer',{},h('button',{type:'button',onclick:()=>finish(false)},'Cancel'),h('button',{type:'button',onclick:async()=>{try{for(const c of dirty)await c.discard?.();finish(true);}catch(e){error.textContent='Could not load the saved version: '+e.message;}}},'Discard changes'),h('button',{type:'button',class:'primary',onclick:async()=>{try{for(const c of dirty)await c.flush?.();if(dirty.some(c=>c.dirty())){error.textContent='Changes are still not saved. Your text is preserved; resolve the error and try again.';return;}finish(true);}catch(e){error.textContent=e.message;}}},'Save and continue')));
  dlg.addEventListener('cancel',e=>{e.preventDefault();finish(false);});dlg.addEventListener('close',()=>dlg.remove());document.body.append(dlg);dlg.showModal();
 });return pending;
}
