import {h} from './dom.js';
import {button,field,input,select,dialog} from './production-ui.js';
import {openAssetPicker} from './library.js';

export async function editProfile(ctx, id=null, done=()=>{}) {
 const data=await ctx.api.get('profiles');const row=data.profiles.find(p=>p.id===id);const p=row?.profile||{};
 const name=input('Profile display name',p.display_name||row?.label||''),handle=input('Profile handle',p.handle||''),bio=h('textarea',{'aria-label':'Profile bio',rows:3},p.bio||''),link=input('Profile website',p.url||'');
 const platform=select('Platform',['instagram','linkedin','facebook','threads','youtube','tiktok','bluesky','pinterest','x'].map(v=>[v,v[0].toUpperCase()+v.slice(1)]),row?.platform||'instagram');platform.disabled=!!row;
 let avatar=p.avatar||null;const avatarButton=button(avatar?'Change avatar':'Choose avatar',()=>openAssetPicker(ctx,{kind:"image",allowDraft:true,onPick:a=>{avatar=a.id;avatarButton.textContent='Avatar selected';}}));
 const nodes=[field('Display name',name),field('Handle',handle),field('Bio',bio),field('Website',link),field('Platform',platform),avatarButton];
 if(row)nodes.push(button('Remove unused local profile',async()=>{try{await ctx.api.post(`profiles/${id}/remove`,{expected_rev:row.revision});dlg.close();await ctx.refreshChrome?.();done(null);}catch(e){nodes[0].append(h('p',{role:'alert'},e.message));}}));
 const dlg=dialog(row?'Edit local profile':'New local profile',nodes,async()=>{const r=await ctx.api.post(row?`profiles/${id}/edit`:'profiles',{expected_rev:row?.revision||null,platform:platform.value,profile:{display_name:name.value,handle:handle.value,bio:bio.value,url:link.value,avatar}});await ctx.refreshChrome?.();await done(r.profile);},{submit:row?'Save profile':'Create profile',note:'Plan locally with your own images and writing. No social login is needed.'});
}

export async function writingLibrary(ctx,onInsert){
 let data=await ctx.api.get('copy-groups'),chosen=null;const list=h('div',{class:'writing-list'}),preview=h('blockquote',{class:'writing-preview'},'Select saved writing to preview it.'),search=input('Find saved writing');
 const insert=button('Insert a copy',async()=>{if(chosen){await onInsert(chosen);dlg.close();}},'primary');insert.disabled=true;
 const refresh=()=>{list.replaceChildren();for(const row of data.items.filter(r=>(r.name+' '+r.text).toLowerCase().includes(search.value.toLowerCase())))list.append(button(row.name,()=>{chosen=row;preview.textContent=row.text;insert.disabled=false;drawActions();}));};
 const actions=h('div');function drawActions(){actions.replaceChildren(button('Edit saved writing',()=>edit(chosen)),button('Remove saved writing',async()=>{data=await ctx.api.post('copy-groups',{expected_rev:data.rev,remove:chosen.id});chosen=null;preview.textContent='Select saved writing to preview it.';insert.disabled=true;actions.replaceChildren();refresh();}));}
 function edit(row=null){const name=input('Writing name',row?.name||''),text=h('textarea',{'aria-label':'Saved writing text',rows:6},row?.text||''),kind=select('Writing kind',[['caption','Caption'],['hook','Hook'],['hashtags','Hashtags']],row?.kind||'caption');dialog(row?'Edit saved writing':'Save reusable writing',[field('Name',name),field('Kind',kind),field('Text',text)],async()=>{data=await ctx.api.post('copy-groups',{expected_rev:data.rev,item:{...(row?{id:row.id}:{}),name:name.value,text:text.value,kind:kind.value}});if(chosen&&row?.id===chosen.id){chosen=data.items.find(i=>i.id===row.id);preview.textContent=chosen.text;}refresh();});}
 const dlg=dialog('Your writing library',[search,button('New saved writing',()=>edit()),list,preview,actions,insert],()=>{}, {submit:'Done',note:'Insertion copies this version. Later changes to saved writing never change a post.'});search.addEventListener('input',refresh);refresh();
}
