"""Registered episode adapters on the shared workbench, engines and library."""
from __future__ import annotations
from ..atomic import publish as publish_atomic
import copy
import json
import os
from pathlib import Path
import re
import secrets
from .. import editdoc, media, library
from ..episode import write_json
from ..publish.safety import sha256_file
from . import episode_registry as R, episode_checkpoints as C, production, ledger, seal
from .workbench import safe, read, write, _copy_immutable, _own_stage, _publish_staged
from .video_workshop import number, request, proxy, compiled_waveform, input_source, safe_dir

_ASR_FINGERPRINT_CACHE={}

OPS=('episode-create','episode-intake','episode-transcribe','episode-sync','episode-assembly','episode-combined','episode-rough','episode-sequence','episode-final','episode-select','episode-short','episode-package','episode-handoff','episode-adopt')


def adoption_base(ep,row,session):
    if session!='main':raise ValueError('Adopted programme uses its own main assembly scope')
    path=R.regular(editdoc.path_for(ep,'assembly',1,'main'))
    if sha256_file(path)!=row['adopted_assembly_sha256']:raise ValueError('Adopted immutable assembly changed')
    for item in row['adopted_files']:
        if sha256_file(R.regular(ep.root/item['file']))!=item['sha256']:raise ValueError('Adopted source or dependent bytes changed')
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is None:licensed_asset_closure(__import__('ave.studio.service',fromlist=['open_studio']).open_studio(row['workspace']),row['adopted_assets'])
    return {'root_identity':row['binding']['identity'],'adoption':row['adoption'],'adopted_assets':row['adopted_assets'],
            'adopted_assembly_sha256':row['adopted_assembly_sha256'],'adopted_files':row['adopted_files']}


def base_inputs(studio,ep,row,session):
    if row.get('adoption'):return adoption_base(ep,row,session)
    mapping=C.required(studio,ep,row,'mapping',session)
    bounds=C.required(studio,ep,row,'bounds',session)
    result=C.inputs(studio,ep,row,'bounds',session,bounds['payload'])
    return {**result,'bounds_event':bounds['checkpoint_id']}


def state_hash(ep,row,op,session):
    if op in ('episode-select','episode-short','episode-package','episode-handoff'):op='episode-final'
    names={'episode-intake':[], 'episode-transcribe':['media.json'], 'episode-sync':['media.json','transcript.json'],
           'episode-assembly':['media.json','transcript.json','sync.json'], 'episode-combined':['media.json','transcript.json','sync.json'], 'episode-rough':['media.json','transcript.json','sync.json'],
           'episode-sequence':['media.json','transcript.json','sync.json'], 'episode-final':['media.json','transcript.json','sync.json']}[op]
    value={'sources':row['source_fingerprint'],'grant_revision':row['grant_revision'],'files':{n:sha256_file(R.regular(ep.artifact(n))) if ep.artifact(n).exists() else None for n in names}}
    if op=='episode-rough':
        from .. import cleanup
        value['cleanup_policy']=sha256_file(cleanup.DEFAULT_POLICY)
        value['choices']=R.policy()['cleanup_choices']
    if op in ('episode-sequence','episode-final'):
        from .. import sequence,captions
        value['sequence_policy']=sha256_file(sequence.DEFAULT_POLICY)
        value['caption_style']=sha256_file(captions.REPO_CONFIG)
        value['sequence_limits']={k:v for k,v in R.policy().items() if k.startswith('sequence_')}
        if op=='episode-final':
            value['caption_alignment_tolerance_s']=R.policy()['caption_alignment_tolerance_s']
            from ..resources import home
            value['export_presets']=sha256_file(home()/'config'/'episode-exports.yaml')
    return R.digest(value)


def validate_operation(sp,op,args):
    from .service import open_studio
    studio=open_studio(sp.root,publishers={},host=None)
    from . import episode_release as release
    if op in release.OPS:return release.validate(studio,op,args),None
    if op=='episode-create':
        production.only(args,{'title','sources','request_id'});request(args.get('request_id'));production.text(args.get('title'),'Episode title',200,True)
        if not isinstance(args.get('sources'),list) or not 1<=len(args['sources'])<=200 or len(set(args['sources']))!=len(args['sources']):raise ValueError('Choose 1–200 distinct recordings')
        for sid in args['sources']:input_source(sp,sid)
        return {},None
    allowed={'episode','grant_revision','input_sha256','request_id','session'}
    if op=='episode-transcribe':allowed|={'source_id','speakers'}
    if op=='episode-sync':allowed|={'source_id','points'}
    if op=='episode-combined':allowed|={'constituents'}
    if op=='episode-rough':allowed|={'assembly','policy','restore'}
    if op=='episode-sequence':allowed|={'rough','show','episode_title','number','hosts','editor','cold_open','opening','closing'}
    if op=='episode-final':allowed|={'rough','sequence','gain_db','caption_style','burn','music','corrections','motion','broll','preset'}
    production.only(args,allowed);request(args.get('request_id'));number(args.get('grant_revision'),1,1000000,'Grant revision',True)
    if not isinstance(args.get('session'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',args['session']):raise ValueError('Choose a session')
    with R.resolve(studio,args.get('episode'),args['grant_revision'],verify_sources=True) as (ep,row):
        if state_hash(ep,row,op,args['session'])!=args.get('input_sha256'):raise ValueError('Episode inputs changed; reload before starting this job')
        if op in ('episode-transcribe','episode-sync') and args.get('source_id') not in {s['id'] for s in row['sources']}:raise ValueError('Choose a registered reference recording')
        if op=='episode-transcribe':number(args.get('speakers'),1,16,'Speaker count',True)
        if op=='episode-sync':number(args.get('points'),3,15,'Sync points',True)
        if op=='episode-assembly':base_inputs(studio,ep,row,args['session'])
        if op=='episode-combined':combined_inputs(studio,ep,row,args)
        if op=='episode-rough':
            if args.get('policy') not in R.policy()['cleanup_choices']:raise ValueError('Choose a supported cleanup policy')
            restore=args.get('restore')
            if not isinstance(restore,list) or len(restore)>1000 or len(set(restore))!=len(restore) or any(not isinstance(x,str) or not re.fullmatch(r'k[0-9]{3,6}',x) for x in restore):raise ValueError('Choose valid cuts to restore')
            rough_inputs(studio,ep,row,args)
        if op=='episode-sequence':sequence_inputs(studio,ep,row,args)
        if op=='episode-final':final_inputs(studio,ep,row,args)
        return row,None


def validate_programme_duration(durations):
    import math
    if any(type(d) not in (int,float) or not math.isfinite(d) or d<0 for d in durations):raise ValueError('Invalid programme duration')
    total=sum(durations)
    if total>R.policy()['max_session_seconds']+1e-8:raise ValueError('Complete programme exceeds the episode duration limit')
    return total


def combined_inputs(studio,ep,row,args):
    """Ordered exact reviewed session assemblies; no inherited confirmation."""
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is not None:return bridge.CLIENT.call('combined-inputs')
    if args.get('session')!='combined':raise ValueError('Combined assembly requires the combined scope')
    selected=args.get('constituents')
    if not isinstance(selected,list) or not 2<=len(selected)<=20:raise ValueError('Choose 2–20 reviewed session assemblies')
    rows=[];seen=set()
    for item in selected:
        if not isinstance(item,dict) or set(item)!={'stage','name','version','sha256','artifact_id'} or item.get('stage')!='assembly' or item.get('name') in seen or item.get('name')=='combined':raise ValueError('Choose distinct exact session assemblies')
        seen.add(item['name']);event=C.required(studio,ep,row,'assembly',item['name'])
        if item!=event['payload']:raise ValueError('A selected session assembly is no longer reviewed exact')
        _,doc=exact(ep,item);art=artifact(studio,row['id'],item['artifact_id'])
        asset_id=art['details'].get('asset')
        if not asset_id:raise ValueError('Session render has no provenance asset')
        from ..library import derive
        records=library.read_all(studio.sp);order,unknown=derive.closure(records,[asset_id])
        if unknown or len(order)>200:raise ValueError('Session provenance closure is missing or too large')
        closure=[]
        for aid in order:
            rec=records[aid];path=safe(studio.sp,studio.sp.root/rec['file'])
            if R._verified_owned_provenance_sha(studio,path,rec['sha256'])!=rec['sha256']:raise ValueError('Session provenance bytes changed')
            closure.append({'id':aid,'sha256':rec['sha256'],'derived_from':rec.get('derived_from',[])})
        if records[asset_id]['sha256']!=art['sha256']:raise ValueError('Session render provenance differs')
        rows.append({'identity':copy.deepcopy(item),'event':event['checkpoint_id'],'dependency_sha256':event['dependency_sha256'],'asset':asset_id,'asset_closure':closure,'duration':doc['duration'],'trim':copy.deepcopy(doc.get('trim')),'excluded':copy.deepcopy(doc.get('excluded',[]))})
    validate_programme_duration([r['duration'] for r in rows])
    return {'constituents':rows}


def compose_assemblies(ep,deps):
    """Join exact existing EditDoc tracks in creator order, retaining source clocks."""
    docs=[exact(ep,r['identity'])[1] for r in deps['constituents']];first=docs[0]
    for doc in docs:
        if editdoc.effective_mode(doc)!=editdoc.effective_mode(first) or doc['canvas']!=first['canvas'] or doc['fps']!=first['fps']:raise ValueError('Session picture formats must match before combining')
        if doc['tracks'].get('music') or doc['tracks'].get('overlays') or doc['tracks'].get('room_tone'):raise ValueError('Assembly combination cannot discard dependent tracks')
    validate_programme_duration([d['duration'] for d in docs])
    combined=editdoc.clone(first);combined['sources']={};combined['tracks']['video']=[];combined['tracks']['dialogue']=[];combined['tracks']['music']=[];combined['tracks']['overlays']=[];combined['tracks']['room_tone']=None
    offset=0.
    for i,doc in enumerate(docs):
        keys={k:f's{i+1}_{k}' for k in doc['sources']}
        for key,src in doc['sources'].items():combined['sources'][keys[key]]=copy.deepcopy(src)
        for kind in ('video','dialogue'):
            for seg in doc['tracks'][kind]:
                seg=copy.deepcopy(seg);seg['id']=f's{i+1}_{seg["id"]}';seg['src']=keys[seg['src']];seg['o0']+=offset;seg['o1']+=offset;seg['occurrence_id']=f'session{i+1}_{seg.get("occurrence_id",seg["id"])}';combined['tracks'][kind].append(seg)
        offset+=doc['duration']
    combined['duration']=offset;combined['session']='combined';combined['trim']=None;combined['excluded']=[];combined['combined_constituents']=copy.deepcopy(deps['constituents']);combined['episode_inputs']=copy.deepcopy(deps)
    combined['captions']={'burn':False};combined['history']=[]
    problems=editdoc.validate(combined)
    if problems:raise ValueError('; '.join(problems))
    return combined


def write_inherited_candidate(ep,job,doc):
    path=R.regular(ep.work/'native-inherited'/job['id']/'candidate.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if R.json_file(path)!=doc:raise ValueError('Inherited programme candidate changed on retry')
    else:write_json(path,doc)


def validate_inherited_candidate(studio,ep,row,job,doc):
    from .video_workshop import recut_output
    if not row.get('adoption'):raise ValueError('Inherited gate requires a captured adopted programme')
    args=job['args'];op=job['operation']
    if op=='episode-assembly':
        expected=R.json_file(editdoc.path_for(ep,'assembly',1,'main'))
        if doc.get('episode_inputs')!=base_inputs(studio,ep,row,args['session']):raise ValueError('Adopted assembly dependencies changed')
    elif op=='episode-rough':
        _,base=exact(ep,args['assembly']);report=doc.get('rough_report') or {}
        if doc.get('rough_inputs')!=rough_inputs(studio,ep,row,args) or report.get('policy')!=args['policy'] or report.get('restored')!=args['restore']:raise ValueError('Adopted cleanup identity changed')
        from .. import cleanup
        policy=cleanup._deep_merge(cleanup.load_policy(),R.policy()['cleanup_choices'][args['policy']])
        if report.get('resolved_policy')!=policy:raise ValueError('Adopted cleanup policy changed')
        cuts=report.get('cuts',[])
        if not isinstance(cuts,list) or len(cuts)>10000:raise ValueError('Invalid adopted cleanup cuts')
        expected=recut_output(base,[{'id':c['id'],'o0':c['e0'],'o1':c['e1'],'xfade_ms':c['xfade_ms']} for c in cuts])
    else:raise ValueError('Inherited gate is outside this operation')
    for key in ('sources','tracks','clips','duration','canvas','fps','media_mode','caption_edit','caption_snapshot','independent_project'):
        if doc.get(key)!=expected.get(key):raise ValueError('Inherited programme differs from exact source construction: '+key)
    licensed_asset_closure(studio,row['adopted_assets'])
    return True


def rough_inputs(studio,ep,row,args):
    """Only the exact reviewed assembly selected by this job is eligible."""
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is not None:
        return bridge.CLIENT.call('rough-inputs')
    event=C.required(studio,ep,row,'assembly',args['session'])
    chosen=args.get('assembly')
    if chosen!=event['payload']:raise ValueError('Select the exact reviewed assembly render')
    path,doc=exact(ep,chosen)
    if doc.get('independent_project') or doc.get('combined_constituents'):
        from .video_workshop import recut_output
        recut_output(doc,[])
    else:editdoc.require_monotonic(doc,'Episode cleanup')
    result=artifact(studio,row['id'],chosen['artifact_id'])
    return {'assembly_event':event['checkpoint_id'],'assembly':copy.deepcopy(chosen),
            'assembly_dependencies':event['dependency_sha256'],'assembly_asset':result['details'].get('asset')}


def licensed_asset_closure(studio,selected):
    """Parent-only exact selection and attested derivative closure for a job.

    Selected identities come from an already authenticated job/plan, never from
    a bridge request. The response contains bounded public metadata, not ledger
    secrets or bookmarks.
    """
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is not None:raise ValueError('Licensed asset closure requires parent verification')
    from ..library import derive,registry
    if not isinstance(selected,list) or len(selected)>20:raise ValueError('Choose at most 20 licensed assets')
    R.control(studio.sp)  # verifies the protected ledger before coverage reads events
    problems=registry.license_log(studio.sp).verify()
    if problems:raise ValueError('License ledger verification failed: '+'; '.join(problems))
    records=library.read_all(studio.sp);ids=[]
    for item in selected:
        if not isinstance(item,dict) or set(item)!={'id','sha256'}:raise ValueError('Each asset requires an exact identity')
        rec=records.get(item['id'])
        if not rec or rec['sha256']!=item['sha256'] or item['id'] in ids:raise ValueError('Selected asset identity changed or was repeated')
        ids.append(item['id'])
    order,unknown=derive.closure(records,ids)
    if unknown or len(order)>200:raise ValueError('Asset provenance closure is missing or exceeds its budget')
    closure=[]
    for aid in order:
        rec=records[aid];path=safe(studio.sp,studio.sp.root/rec['file'])
        if not path.is_file() or R._verified_owned_provenance_sha(studio,path,rec['sha256'])!=rec['sha256']:raise ValueError('Licensed asset bytes changed: '+aid)
        closure.append({**{k:copy.deepcopy(rec.get(k)) for k in ('id','sha256','file','kind','derived_from','license')},
                        'label':rec.get('alt') or (rec.get('source') or {}).get('title') or 'Licensed recording'})
    coverage=library.coverage(studio.sp,ids)
    if not coverage['ok']:raise ValueError('Asset license or human attestation is missing: '+str(coverage['missing']))
    return {'selected':copy.deepcopy(selected),'closure':closure,'coverage':coverage}


def validate_excerpt_corrections(ep,row,doc,a,b,corrections):
    if not isinstance(corrections,list) or len(corrections)>100:raise ValueError('Choose at most 100 excerpt caption corrections')
    if not corrections:return
    from ..occurrences import mapped_words,correction_words,apply_correction
    from ..captions import timing_adjustment_limit
    projection=editdoc.clone(doc);projection['native_source_hashes']={s['file']:s['sha256'] for s in row['sources']};projection['caption_edit']={'style':'podcast','burn':False,'corrections':[]}
    words=mapped_words(ep,projection);used=set();limit=timing_adjustment_limit(ep)
    for correction in corrections:
        production.only(correction,{'occurrence_id','transcript_sha256','word_ids','text','start_offset_s','end_offset_s'});production.text(correction.get('text'),'Caption correction',500,True)
        refs=correction.get('word_ids')
        if not isinstance(refs,list) or not 1<=len(refs)<=50:raise ValueError('Choose excerpt words to correct')
        chosen=correction_words(words,correction)
        if len(chosen)!=len(refs) or any(w['start']<a-1e-8 or w['end']>b+1e-8 for w in chosen):raise ValueError('Caption correction is outside the selected excerpt')
        keys={(w['occurrence_id'],w['word_id']) for w in chosen}
        if keys&used:raise ValueError('Excerpt corrections cannot overlap')
        used|=keys;words=apply_correction(words,correction,limit)
        revised=next((w for w in words if w['occurrence_id']==correction['occurrence_id'] and w['word_id']==refs[0]),None)
        if not revised or revised['start']<a-1e-8 or revised['end']>b+1e-8:raise ValueError('Corrected timing must stay inside the excerpt')


def native_cold_words(ep,row,doc,cold):
    if not cold:return None
    from .. import sequence
    projection=editdoc.clone(doc);projection['native_source_hashes']={s['file']:s['sha256'] for s in row['sources']};projection['caption_edit']={'style':'podcast','burn':False,'corrections':[]}
    end=cold['start']+sequence._whole_frames_slice(doc,cold['start'],cold['end'],doc['fps'])['duration']
    words,partial=sequence.excerpt_words(ep,projection,cold['start'],end,tag='opening',index=-1,corrections=cold.get('corrections',[]))
    if partial:raise ValueError('Cold open boundaries cut through a transcript word; choose a word gap')
    return words


def sequence_inputs(studio,ep,row,args):
    """Validate a typed, exact sequence request and its licensed asset closure."""
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is not None:return bridge.CLIENT.call('sequence-inputs')
    event=C.required(studio,ep,row,'rough',args['session'])
    if args.get('rough')!=event['payload']:raise ValueError('Select the exact reviewed rough render')
    _,doc=exact(ep,args['rough']);mode=editdoc.effective_mode(doc)
    for field,limit in (('show',120),('episode_title',200),('number',30),('editor',100)):
        production.text(args.get(field,''),field,limit,field not in ('number','editor'))
    hosts=args.get('hosts')
    if not isinstance(hosts,list) or len(hosts)>4:raise ValueError('Choose up to four hosts')
    for host in hosts:
        production.only(host,{'name','title'});production.text(host.get('name'),'Host name',100,True);production.text(host.get('title',''),'Host title',100,False)
    cold=args.get('cold_open')
    if cold is not None:
        if mode=='audio':raise ValueError('Choose the audio cold open as an excerpt component')
        production.only(cold,{'start','end','corrections'});a=number(cold.get('start'),0,doc['duration'],'Cold open start');b=number(cold.get('end'),0,doc['duration'],'Cold open end')
        if b<=a:raise ValueError('Cold open end must follow its start')
        from ..occurrences import mapped_words
        from .. import sequence
        rendered_end=a+sequence._whole_frames_slice(doc,a,b,doc['fps'])['duration']
        if any(w['start']<rendered_end and w['end']>a and (w['start']<a-1e-8 or w['end']>rendered_end+1e-8) for w in mapped_words(ep,doc)):
            raise ValueError('Cold open boundaries cut through a transcript word; choose a word gap')
        validate_excerpt_corrections(ep,row,doc,a,rendered_end,cold.get('corrections',[]))
    selected={};limits=R.policy()
    for name in ('opening','closing'):
        section=args.get(name);production.only(section,{'enabled','components'})
        if type(section.get('enabled')) is not bool or not isinstance(section.get('components'),list) or len(section['components'])>limits['sequence_max_components']:raise ValueError('Invalid sequence components')
        if not section['enabled'] and section['components']:raise ValueError('Disabled sequences cannot have components')
        if mode=='audio' and section['enabled'] and not section['components']:raise ValueError('An enabled audio sequence needs a real audio component')
        if mode!='audio' and (len(section['components'])>1 or any(c.get('kind')!='asset' for c in section['components'])):raise ValueError('Choose at most one licensed music bed for each audiovisual sequence')
        for item in section['components']:
            kind=item.get('kind')
            if kind=='excerpt':
                production.only(item,{'kind','start','end','corrections'});a=number(item.get('start'),0,doc['duration'],'Excerpt start');b=number(item.get('end'),0,doc['duration'],'Excerpt end')
                if b<=a or b-a>limits['sequence_component_max_seconds']:raise ValueError('Excerpt range is empty or too long')
                validate_excerpt_corrections(ep,row,doc,a,b,item.get('corrections',[]))
            elif kind=='asset':
                production.only(item,{'kind','id','sha256','start','duration'});number(item.get('start'),0,limits['max_source_seconds'],'Recording start');number(item.get('duration'),.001,limits['sequence_component_max_seconds'],'Recording duration')
                if not isinstance(item.get('id'),str) or not isinstance(item.get('sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',item['sha256']):raise ValueError('Choose an exact licensed audio recording')
                if item['id'] in selected and selected[item['id']]!=item['sha256']:raise ValueError('Asset identity differs between components')
                selected[item['id']]=item['sha256']
            else:raise ValueError('Choose an excerpt or licensed audio recording')
    closure=licensed_asset_closure(studio,[{'id':k,'sha256':v} for k,v in sorted(selected.items())])
    by_id={r['id']:r for r in closure['closure']}
    for name in ('opening','closing'):
        for item in args[name]['components']:
            if item['kind']=='asset':
                rec=by_id[item['id']];probe=media.probe_file(safe(studio.sp,studio.sp.root/rec['file']),studio.sp.root)
                if not probe['audio'] or item['start']+item['duration']>probe['duration']+1e-6:raise ValueError('Selected audio range exceeds the licensed recording')
    return {'rough_event':event['checkpoint_id'],'rough_dependencies':event['dependency_sha256'],
            'rough':copy.deepcopy(args['rough']),'rough_asset':artifact(studio,row['id'],args['rough']['artifact_id'])['details'].get('asset'),
            'mode':mode,'assets':closure}


def make_sequence_plan(studio,ep,args,deps):
    from .. import sequence
    _,doc=exact(ep,args['rough'])
    pol=sequence.load_policy();pol['opening']['hosts']['default_title']='';pol['closing']['credits']['editor']=args.get('editor') or None
    meta={'show':{'name':args['show'],'hosts':copy.deepcopy(args['hosts'])},'number':args['number'],'episode_title':args['episode_title']}
    if args['cold_open']:
        meta['sequences']={'cold_open':{'range':{'stage':'rough','name':args['session'],'o0':args['cold_open']['start'],'o1':args['cold_open']['end']}}}
    if deps['mode']=='audio':
        # Visual card/credits warnings have no meaning in an audio programme.
        # The text remains descriptive; only selected components are audible.
        identity=sequence.identity(meta,pol);identity.pop('warnings',None)
        for entry in identity.values():
            if isinstance(entry,dict) and isinstance(entry.get('source'),str):entry['source']=entry['source'].replace('episode.json','sequence plan')
        plan={'identity':identity,'_bdoc':doc}
        public={'version':1,'identity':identity,'mode':'audio','opening':copy.deepcopy(args['opening']),
                'closing':copy.deepcopy(args['closing']),'warnings':[],'approval':{'approved':False}}
    else:
        verified={}
        for tag in ('opening','closing'):
            components=args[tag]['components']
            if not components:continue
            item=components[0];rec=next(r for r in deps['assets']['closure'] if r['id']==item['id']);path=str(safe(studio.sp,studio.sp.root/rec['file']))
            attrs=(rec.get('license') or {}).get('attribution') or {};covered=deps['assets']['coverage']['covered']
            verified[path]={'sha256':rec['sha256'],'status':'licensed','license':(covered[0]['license'] if covered else 'verified library closure'),
                            'source':'verified studio library','attribution_required':bool(attrs.get('required')),'attribution_text':attrs.get('text') or '',
                            'title':rec.get('label') or '', 'proof_sha256':R.digest(deps['assets'])}
            pol[tag]['music']['file']=path;pol[tag]['music']['in']=item['start'];pol[tag]['music']['source_limit_s']=item['duration']
        plan=sequence.plan(ep,name=args['session'],doc=doc,write=False,metadata=meta,policy_data=pol,base_identity=args['rough'],verified_assets=verified)
        if plan['errors']:raise ValueError('Sequence plan: '+'; '.join(plan['errors']))
        public={k:v for k,v in plan.items() if not k.startswith('_')}
    snapshot={'schema':'episode_sequence_plan.v1','args':copy.deepcopy(args),'dependencies':deps,'policy':pol,'plan':public,'mode':deps['mode']}
    return plan,snapshot


def sequence_record(studio,ep,row,job_id,plan_sha):
    if not isinstance(job_id,str) or not re.fullmatch(r'job_[a-f0-9]{24}',job_id):raise ValueError('Choose an exact sequence job')
    p=safe(studio.sp,studio.sp.work/'production'/'episode-renders'/row['id']/job_id/'sequence-plan.json')
    if sha256_file(p)!=plan_sha:raise ValueError('Sequence plan bytes changed')
    record=read(studio.sp,p);job={'id':job_id,'operation':record['operation'],'args':record['args'],'episode_descriptor':record['descriptor']}
    if job['operation']!='episode-sequence' or job['args']['episode']!=row['id'] or job['args']['grant_revision']!=row['grant_revision']:raise ValueError('Sequence plan belongs to a different episode or grant')
    check_descriptor(studio,job)
    deps=sequence_inputs(studio,ep,row,job['args']);_,snapshot=make_sequence_plan(studio,ep,job['args'],deps)
    if snapshot!=record['snapshot']:raise ValueError('Sequence plan or dependencies changed; render a new preview')
    return record


def final_inputs(studio,ep,row,args):
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is not None:return bridge.CLIENT.call('final-inputs')
    rough=C.required(studio,ep,row,'rough',args['session'])
    if args.get('rough')!=rough['payload']:raise ValueError('Choose the exact reviewed rough cut')
    sequence_event=C.required(studio,ep,row,'sequence',args['session'])
    if args.get('sequence')!=sequence_event['payload']:raise ValueError('Choose the exact reviewed opening and closing plan')
    sequence_plan=sequence_record(studio,ep,row,args['sequence']['job_id'],args['sequence']['plan_sha256'])
    if sequence_plan['args']['rough']!=args['rough']:raise ValueError('Sequence plan belongs to a different rough cut')
    from .. import presets
    chosen_preset=args.get('preset','master')
    if chosen_preset not in ('master','preview','episode_hd','episode_uhd','episode_draft'):raise ValueError('Choose a supported final export quality')
    export_preset=presets.get(chosen_preset)
    number(args.get('gain_db'),-24,12,'Dialogue gain')
    if args.get('caption_style')!='podcast':raise ValueError('Choose the podcast caption style')
    if type(args.get('burn')) is not bool:raise ValueError('Choose whether to burn captions')
    _,doc=exact(ep,args['rough']);mode=editdoc.effective_mode(doc)
    if mode=='audio' and args['burn']:raise ValueError('Audio captions are sidecars; audio has no picture')
    motion_items=args.get('motion',[])
    if not isinstance(motion_items,list) or len(motion_items)>8:raise ValueError('Choose at most eight motion titles')
    if mode=='audio' and motion_items:raise ValueError('Audio has no picture for a motion title')
    layout=R.policy()['final_layout'];motion_y=round(doc['canvas']['h']*layout['title_y_fraction'])
    motion_specs=[]
    if motion_items:
        from .. import motion
        for item in motion_items:
            production.only(item,{'name','title','side','start','duration'})
            number(item.get('start'),0,doc['duration'],'Title start');number(item.get('duration'),layout['title_min_duration_s'],30,'Title duration')
            if item['start']+item['duration']>doc['duration']+1e-6:raise ValueError('Motion title exceeds the rough cut')
            spec=motion.normalize_spec({'template':'lower-third','props':{'name':item.get('name'),'title':item.get('title',''),'side':item.get('side','left')},'duration':item['duration'],'fps':doc['fps'],'canvas':doc['canvas']})
            motion_specs.append({'spec':json.loads(json.dumps(spec)),'hash':motion.spec_hash(spec),'start':item['start'],'y':motion_y})
    corrections=args.get('corrections',[])
    if not isinstance(corrections,list) or len(corrections)>500:raise ValueError('Choose at most 500 caption corrections')
    if corrections:
        from ..occurrences import mapped_words,correction_words
        projection=editdoc.clone(doc);projection['native_source_hashes']={s['file']:s['sha256'] for s in row['sources']};projection['caption_edit']={'style':'podcast','burn':False,'corrections':[]}
        words=mapped_words(ep,projection);used=set()
        for correction in corrections:
            production.only(correction,{'occurrence_id','transcript_sha256','word_ids','text','start_offset_s','end_offset_s'})
            production.text(correction.get('text'),'Caption correction',500,True)
            from .. import captions
            timing_limit=captions.timing_adjustment_limit(ep)
            for key in ('start_offset_s','end_offset_s'):
                if key in correction:number(correction[key],-timing_limit,timing_limit,'Caption timing adjustment')
            if not isinstance(correction.get('word_ids'),list) or not 1<=len(correction['word_ids'])<=50:raise ValueError('Choose caption words to correct')
            selected=correction_words(words,correction)
            if len(selected)!=len(correction['word_ids']):raise ValueError('Caption correction refers to stale or missing words')
            keys={(w['occurrence_id'],w['word_id']) for w in selected}
            if used&keys:raise ValueError('Caption corrections cannot overlap')
            used|=keys
        projection['caption_edit']['corrections']=copy.deepcopy(corrections);mapped_words(ep,projection)
    music=args.get('music');selected={}
    inherited={'music':copy.deepcopy(doc['tracks'].get('music',[])),'overlays':copy.deepcopy(doc['tracks'].get('overlays',[]))}
    for kind,items in inherited.items():
        for item in items:
            ref=item.get('library') or {};aid=item.get('asset') or ref.get('asset');sha=item.get('sha256') or ref.get('sha256')
            if not sha or sha256_file(R.regular(ep.root/item['file']))!=sha:raise ValueError('Inherited dependent media lacks a matching immutable identity')
            if aid:selected[aid]=sha
    if music is not None:
        production.only(music,{'id','sha256','start','gain_db','duck_db','fade_in_s','fade_out_s'})
        if not isinstance(music.get('id'),str) or not isinstance(music.get('sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',music['sha256']):raise ValueError('Choose an exact registered music asset')
        number(music.get('start'),0,R.policy()['max_source_seconds'],'Music start')
        number(music.get('gain_db'),-60,0,'Music gain');number(music.get('duck_db'),-40,0,'Music ducking')
        number(music.get('fade_in_s'),0,30,'Music fade in');number(music.get('fade_out_s'),0,30,'Music fade out')
        selected[music.get('id')]=music.get('sha256')
    broll=args.get('broll',[])
    if not isinstance(broll,list) or len(broll)>8:raise ValueError('Choose at most eight B-roll placements')
    if mode=='audio' and broll:raise ValueError('Audio has no picture for B-roll')
    for item in broll:
        production.only(item,{'id','sha256','start','duration','source_in','ken_burns'})
        if not isinstance(item.get('id'),str) or not isinstance(item.get('sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',item['sha256']):raise ValueError('Choose an exact registered B-roll asset')
        number(item.get('start'),0,doc['duration'],'B-roll start');number(item.get('duration'),.05,doc['duration'],'B-roll duration');number(item.get('source_in'),0,R.policy()['max_source_seconds'],'B-roll source start')
        if type(item.get('ken_burns')) is not bool:raise ValueError('Choose whether the still moves')
        if item['id'] in selected and selected[item['id']]!=item['sha256']:raise ValueError('Asset identity differs between placements')
        selected[item['id']]=item['sha256']
    assets=licensed_asset_closure(studio,[{'id':k,'sha256':v} for k,v in sorted(selected.items())])
    if music:
        rec=next(r for r in assets['closure'] if r['id']==music['id']);probe=media.probe_file(safe(studio.sp,studio.sp.root/rec['file']),studio.sp.root)
        if not probe['audio'] or music['start']+doc['duration']>probe['duration']+1e-6:raise ValueError('Music must cover the selected rough cut')
        if max(music['fade_in_s'],music['fade_out_s'])>doc['duration']:raise ValueError('Music fades cannot exceed the rough cut duration')
    proposals=[];broll_config=None
    if broll:
        from ..broll import place
        base_path,base=exact(ep,args['rough']);working=editdoc.clone(base);broll_config=place.load_config(ep);broll_config['credit']['position_landscape']=copy.deepcopy(layout['credit_position_landscape']);broll_config['credit']['position_portrait']=copy.deepcopy(layout['credit_position_portrait'])
        for item in broll:
            proposal=place.propose(ep,asset=item['id'],at=item['start'],dur=item['duration'],mode='cutaway',stage='rough',name=args['session'],studio=studio.sp.root,
                                   src_in=item['source_in'],audio='mute',ken_burns='push_in' if item['ken_burns'] else None,by='native-creator-draft',
                                   explicit_base=(base_path,working,args['rough']['version']),write=False)
            rec=next(r for r in assets['closure'] if r['id']==item['id']);suffix=Path(rec['file']).suffix.lower()
            proposal['placement']['file']=ep.rel(ep.work/'episode-assets'/(rec['sha256']+suffix))
            proposals.append(proposal);working['tracks']['overlays'].append(copy.deepcopy(proposal['placement']))
    components={}
    for tag,aid in args['sequence']['artifacts'].items():
        rec=artifact(studio,row['id'],aid)
        components[tag]={k:copy.deepcopy(v) for k,v in rec.items() if k!='signature'}
    component_durations=[]
    for rec in components.values():
        report=rec['details']['report'];component_durations.append(report['samples']/48000 if mode=='audio' else report['duration'])
    from .. import ffutil
    if mode=='audio':body_duration=round(doc['duration']*48000)/48000
    else:
        fps=ffutil.fps_fraction(doc['fps']);body_duration=ffutil.frame_of(doc['duration'],fps)/float(fps)
    validate_programme_duration([body_duration,*component_durations])
    rough_artifact=artifact(studio,row['id'],args['rough']['artifact_id'])
    from .. import captions
    style=captions.get_style(args['caption_style']);font=captions.resolve_font(style['fonts'],bold=bool(style.get('bold')))
    if not font.get('file') or not font.get('bundled'):raise ValueError('Caption rendering requires the bundled font')
    return {'rough_event':rough['checkpoint_id'],'rough_dependencies':rough['dependency_sha256'],
            'sequence_event':sequence_event['checkpoint_id'],'sequence_dependencies':sequence_event['dependency_sha256'],
            'sequence_plan':sequence_plan['snapshot'],'sequence_args':sequence_plan['args'],'components':components,
            'rough_asset':rough_artifact['details'].get('asset'),'mode':mode,'export_preset':{'name':chosen_preset,'settings':export_preset},
            'caption_profile':{'style':style,'font_sha256':sha256_file(Path(font['file'])),'glossary':captions.episode_glossary(ep),'timing_limit_s':captions.timing_adjustment_limit(ep)},
            'source_hashes':{s['file']:s['sha256'] for s in row['sources']},'asr_configuration':final_asr_configuration(),'assets':assets,'motion_specs':motion_specs,
            'broll_proposals':proposals,'broll_config':broll_config,'inherited_tracks':inherited}


def final_asr_configuration():
    """Pin the local v3 model and executable; no network model fallback."""
    from .. import config,desktop_runtime
    cli=config.fluidaudio_cli()
    if not cli or not Path(cli).is_file():raise ValueError('Final caption verification needs the installed FluidAudio executable')
    model=desktop_runtime.root()/'asr' if desktop_runtime.frozen() else Path.home()/'Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3'
    if not model.is_dir():raise ValueError('Final caption verification needs the local Parakeet v3 model; no download is attempted')
    files=[]
    for path in sorted(model.rglob('*')):
        if path.is_symlink():raise ValueError('ASR model contains a symlink')
        if path.is_file():
            st=path.stat();files.append((path.relative_to(model).as_posix(),st.st_size,st.st_mtime_ns,st.st_ino))
    if not files or len(files)>5000:raise ValueError('Local ASR model inventory is empty or too large')
    stat_key=R.digest(files);cache_key=(str(model),stat_key)
    if cache_key not in _ASR_FINGERPRINT_CACHE:
        if desktop_runtime.frozen():desktop_runtime.component('asr')
        _ASR_FINGERPRINT_CACHE.clear();_ASR_FINGERPRINT_CACHE[cache_key]=R.digest([(name,sha256_file(model/name)) for name,_,_,_ in files])
    return {'engine':'fluidaudio-parakeet-v3','binary_sha256':sha256_file(Path(cli)),
            'model_sha256':_ASR_FINGERPRINT_CACHE[cache_key],'model_dir':str(model),'language':None,'diarize':False}


def final_asr(ep,media_path,work_dir,configuration):
    from .. import captions
    if configuration['engine']!='fluidaudio-parakeet-v3':raise ValueError('Unsupported final transcription engine')
    return captions.retranscribe(ep,media_path,asr='fluidaudio',diarize=False,work_dir=work_dir,local_model_dir=Path(configuration['model_dir']))


def compare_final_captions(cues,words):
    """Bounded lexical evidence; ASR remains a hypothesis, not correction authority."""
    import difflib
    def tokens(text):return re.findall(r"[\w']+",text.lower())
    planned=[(token,c['start'],c['end']) for c in cues for token in tokens(c['text'])]
    heard=[(token,w['start'],w['end']) for w in words for token in tokens(w.get('w',''))]
    if len(planned)>1000000 or len(heard)>1000000:raise ValueError('Caption comparison exceeds its bounded word budget')
    diff=[];changed=0;tolerance=R.policy()['caption_alignment_tolerance_s']
    for tag,a,b,c,d in difflib.SequenceMatcher(None,[x[0] for x in planned],[x[0] for x in heard],autojunk=True).get_opcodes():
        if tag=='equal':
            for left,right in zip(planned[a:b],heard[c:d]):
                midpoint=(right[1]+right[2])/2
                if midpoint<left[1]-tolerance or midpoint>left[2]+tolerance:
                    changed+=1
                    if len(diff)<1000:diff.append({'kind':'timing','planned':left[0],'asr':right[0],'planned_start':left[1],'asr_start':right[1]})
            continue
        changed+=1
        if len(diff)<1000:
            diff.append({'kind':tag,'planned':' '.join(x[0] for x in planned[a:b]),'asr':' '.join(x[0] for x in heard[c:d]),
                         'planned_start':planned[a][1] if a<len(planned) else None,'asr_start':heard[c][1] if c<len(heard) else None})
    return {'planned_words':len(planned),'asr_words':len(heard),'discrepancies':changed,'items':diff,'truncated':changed>len(diff),'timing_tolerance_s':tolerance,
            'note':'ASR is independent evidence; listen and correct discrepancies before approval.'}


def bound_program_cues(cues,duration):
    """Keep SRT times inside the actual output and disclose boundary clipping."""
    import math
    limit=math.floor(duration*1000+1e-8)/1000
    kept=[];adjustments=[]
    for original in cues:
        cue=copy.deepcopy(original)
        if cue.end<=0 or cue.start>=limit:
            adjustments.append({'kind':'outside','start':cue.start,'end':cue.end,'text':cue.text})
            continue
        before=(cue.start,cue.end);cue.start=max(0,cue.start);cue.end=min(limit,cue.end)
        if (cue.start,cue.end)!=before:
            clipped=[w.get('w','') for w in cue.words if w.get('start',0)<cue.start or w.get('end',0)>cue.end]
            adjustments.append({'kind':'clipped','start':before[0],'end':before[1],'bounded_start':cue.start,'bounded_end':cue.end,
                                'text':cue.text,'clipped_words':clipped,'word_evidence':'mapped source words' if cue.words else 'unavailable'})
        if cue.end>cue.start:kept.append(cue)
    return kept,adjustments


def bound_component_cues(cues,start,end,component):
    """Shift local cues and bound them to the exact component on the SRT clock."""
    import math
    low=math.ceil(start*1000-1e-8)/1000;high=math.floor(end*1000+1e-8)/1000
    kept=[];adjustments=[]
    for original in cues:
        cue=copy.deepcopy(original);cue.start+=start;cue.end+=start
        for word in cue.words:word['start']+=start;word['end']+=start
        before=(cue.start,cue.end)
        if cue.end<=low or cue.start>=high:
            adjustments.append({'component':component,'kind':'outside','start':before[0],'end':before[1],'text':cue.text});continue
        cue.start=max(low,cue.start);cue.end=min(high,cue.end)
        if (cue.start,cue.end)!=before:
            clipped=[w.get('w','') for w in cue.words if w.get('start',0)<cue.start or w.get('end',0)>cue.end]
            adjustments.append({'component':component,'kind':'clipped','start':before[0],'end':before[1],'bounded_start':cue.start,'bounded_end':cue.end,'text':cue.text,'clipped_words':clipped,'word_evidence':'mapped source words' if cue.words else 'unavailable'})
        if cue.end>cue.start:kept.append(cue)
    return kept,adjustments


def validate_programme_caption_boundaries(cues,report):
    """Reject sidecars that leave the programme or cross an actual join."""
    import math
    duration=report.get('expected_duration',report.get('duration'))
    if not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<=0:raise ValueError('Final programme duration is invalid')
    joins=[n/report['sample_rate'] for n in report['joins_samples']] if 'joins_samples' in report else report.get('joins_s',[])
    for cue in cues:
        start,end=cue['start'],cue['end']
        if not all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) or start<0 or end<=start or end>duration+1e-8:raise ValueError('Caption leaves the rendered programme')
        if any(start<join-1e-8 and end>join+1e-8 for join in joins):raise ValueError('Caption crosses a programme component join')


def descriptor(studio,op,args):
    value={'operation':op,'args':copy.deepcopy(args),'workspace':str(studio.sp.root)}
    return {'value':value,'signature':seal.mac('episode-job.v1',R.digest(value))}


def check_descriptor(studio,job):
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is not None:
        bridge.CLIENT.call('snapshot');return
    expected={'operation':job['operation'],'args':job['args'],'workspace':str(studio.sp.root)}
    d=job.get('episode_descriptor') or {}
    if d.get('value')!=expected or not seal.verify_mac(d.get('signature'),'episode-job.v1',R.digest(expected)):raise ValueError('Episode job descriptor is absent or invalid')


def create(studio,args):
    sp=studio.sp;eid='episode_'+R.digest({'request':args['request_id']})[:24]
    old=R.records(sp).get(eid)
    if old:
        if old.get('create')!=args:raise ValueError('Episode request ID belongs to different intake')
        return {'episode':eid}
    root=safe_dir(sp,sp.work/'production'/'episodes'/eid)
    stage=safe_dir(sp,sp.work/'production'/'staging'/'episodes'/eid)
    _own_stage(sp,stage,stage.with_suffix('.owner.json'),{'schema':'episode-intake.v1','args':args})
    if not root.exists():
        from ..episode import Episode
        ep=Episode(stage)
        if not ep.meta_path.exists():write(sp,ep.meta_path,{'title':args['title'],'layout':'flat','registered_copy':eid,'speakers':{},'cameras':{},'stages':{}})
        elif read(sp,ep.meta_path).get('registered_copy')!=eid:raise ValueError('Intake staging ownership changed')
        ep.create()
        labels={}
        for n,sid in enumerate(args['sources']):
            row,path=input_source(sp,sid);filename=f'{n+1:03d}_'+Path(row['label']).name
            _copy_immutable(sp,path,ep.raw/filename,row['sha256'])
            label=row['label'] if not re.fullmatch(r'[a-f0-9]{64}\.[a-z0-9]+',row['label']) else f'Recording {n+1}'
            labels[ep.rel(ep.raw/filename)]=label
        meta=read(sp,ep.meta_path);meta['source_labels']=labels;write(sp,ep.meta_path,meta)
        root.parent.mkdir(parents=True,exist_ok=True);_publish_staged(stage,root)
    meta=R.json_file(root/'episode.json')
    if meta.get('registered_copy')!=eid:raise ValueError('Managed episode ownership mismatch')
    for n,sid in enumerate(args['sources']):
        original,path=input_source(sp,sid);destination=R.regular(root/'raw'/(f'{n+1:03d}_'+Path(original['label']).name))
        if sha256_file(destination)!=original['sha256']:raise ValueError('Managed intake source identity changed; originals preserved')
    spec,paths=R.layout_paths(root,meta);sources=R.scan(root,paths)
    with ledger.ledger_lock(sp):
        if eid not in R.records(sp):
            R.control(sp).append_locked({'schema':'registered_episode.v1','id':eid,'workspace':str(sp.root),'ownership':'managed_copy','label':args['title'],
                'root_hint':sp.rel(root),'binding':{'identity':[root.stat().st_dev,root.stat().st_ino]},'additional':{},'layout':spec,'sources':sources,
                'source_fingerprint':R.digest(sources),'grant_revision':1,'derived_consent':True,'create':args})
    return {'episode':eid}


def _artifact_record(studio,eid,aid):
    if not isinstance(aid,str) or not re.fullmatch(r'artifact_[a-f0-9]{32}',aid):raise ValueError('Choose a registered episode preview')
    rec=read(studio.sp,studio.sp.work/'production'/'episode-artifacts'/(aid+'.json'))
    if rec['episode']!=eid:raise ValueError('Artifact belongs to another episode')
    if not seal.verify_mac(rec.get('signature'),'episode-artifact.v1',R.digest({k:v for k,v in rec.items() if k!='signature'})):raise ValueError('Artifact binding was changed')
    return rec


def artifact(studio,eid,aid):
    rec=_artifact_record(studio,eid,aid)
    p=safe(studio.sp,studio.sp.root/rec['file'])
    if sha256_file(p)!=rec['sha256']:raise ValueError('Episode preview bytes changed')
    caption=(rec.get('details') or {}).get('caption')
    if caption:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+\.srt',caption.get('filename','')):raise ValueError('Invalid sequence caption identity')
        cp=safe(studio.sp,p.parent/caption['filename'])
        if sha256_file(cp)!=caption.get('sha256'):raise ValueError('Sequence caption bytes changed')
    evidence=(rec.get('details') or {}).get('asr')
    if evidence:
        if not re.fullmatch(r'asr-evidence-[a-f0-9]{64}\.json',evidence.get('filename','')):raise ValueError('Invalid final transcription evidence identity')
        evidence_path=safe(studio.sp,p.parent/evidence['filename'])
        if sha256_file(evidence_path)!=evidence.get('sha256') or evidence.get('media_sha256')!=rec['sha256']:raise ValueError('Final transcription evidence or media changed')
    return rec


def register_artifact(studio,row,path,*,kind,source=None,edit=None,details=None,verified_sha256=None,before_publish=None):
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is not None:return bridge.CLIENT.call('artifact',file=studio.sp.rel(path),kind=kind,source=source,edit=edit,details=details or {})
    sp=studio.sp;output_sha=verified_sha256 or sha256_file(path)
    aid='artifact_'+R.digest({'episode':row['id'],'grant_revision':row['grant_revision'],'file':sp.rel(path),'kind':kind,'source':source,'edit':edit,'sha256':output_sha})[:32]
    rec={'id':aid,'episode':row['id'],'grant_revision':row['grant_revision'],'kind':kind,'sha256':output_sha,
         'file':sp.rel(path),'source':source,'edit':edit,'details':details or {}}
    from .episode_view import _stat
    rec['file_stats']=_stat(path)
    rec['signature']=seal.mac('episode-artifact.v1',R.digest(rec));destination=sp.work/'production'/'episode-artifacts'/(aid+'.json')
    exists=destination.exists()
    if exists:
        previous=artifact(studio,row['id'],aid)
        comparable=rec if 'file_stats' in previous else {k:v for k,v in rec.items() if k!='file_stats'}
        if previous!=comparable:raise ValueError('Artifact identity already has different metadata')
    if before_publish:
        # All media hashing is complete. Keep the current-state guard and the
        # artifact's atomic JSON publication inside one short control boundary.
        with ledger.ledger_lock(sp):
            before_publish()
            if not exists:write(sp,destination,rec)
    elif not exists:write(sp,destination,rec)
    return public_artifact(previous if exists else rec)


def public_artifact(rec):return {**{k:v for k,v in rec.items() if k not in ('file','signature')},'url':'/api/studio/episodes/'+rec['episode']+'/artifacts/'+rec['id']}


def copy_episode_output(studio,ep,source,destination):
    """Copy a verified generated file from a granted episode to the workspace."""
    import shutil,tempfile
    source=R.regular(source);destination=safe(studio.sp,destination)
    if not any(source.is_relative_to(R.regular(base,directory=True)) for base in (ep.work,ep.captions)):raise ValueError('Generated output is outside the granted work or captions folder')
    expected=sha256_file(source);destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():
        if sha256_file(destination)!=expected:raise ValueError('Existing preview differs; refusing replacement')
        return
    with tempfile.NamedTemporaryFile(dir=destination.parent,delete=False) as f:
        temporary=Path(f.name)
        with source.open('rb') as inp:shutil.copyfileobj(inp,f)
        f.flush();os.fsync(f.fileno())
    try:
        if sha256_file(temporary)!=expected or sha256_file(source)!=expected:raise ValueError('Sequence source changed during copy')
        try:publish_atomic(temporary,destination)
        except FileExistsError:
            if sha256_file(destination)!=expected:raise ValueError('Existing preview differs; refusing replacement')
    finally:temporary.unlink(missing_ok=True)


def write_caption_once(ep,path,writer):
    """Publish generated sidecar bytes without replacing any earlier result."""
    import tempfile
    path=R.regular(path);base=R.regular(ep.captions,directory=True)
    if not path.is_relative_to(base):raise ValueError('Caption leaves granted captions folder')
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,suffix=path.suffix,delete=False) as f:temporary=Path(f.name)
    try:
        writer(temporary);digest=sha256_file(temporary)
        try:publish_atomic(temporary,path)
        except FileExistsError:
            if sha256_file(R.regular(path))!=digest:raise ValueError('Existing caption bytes differ; refusing replacement')
    finally:temporary.unlink(missing_ok=True)
    return path


def copy_library_to_episode(studio,ep,rec):
    """Copy selected library bytes into granted derived work, never raw."""
    import shutil,tempfile
    source=safe(studio.sp,studio.sp.root/rec['file']);suffix=source.suffix.lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,8}',suffix):raise ValueError('Unsupported licensed asset extension')
    dest=R.regular(ep.work/'episode-assets'/(rec['sha256']+suffix));dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists():
        if sha256_file(dest)!=rec['sha256']:raise ValueError('Existing licensed asset copy differs')
        return dest
    with tempfile.NamedTemporaryFile(dir=dest.parent,delete=False) as f:
        tmp=Path(f.name)
        with source.open('rb') as inp:shutil.copyfileobj(inp,f)
        f.flush();os.fsync(f.fileno())
    try:
        if sha256_file(tmp)!=rec['sha256'] or sha256_file(source)!=rec['sha256']:raise ValueError('Licensed asset changed during copy')
        try:publish_atomic(tmp,dest)
        except FileExistsError:
            if sha256_file(R.regular(dest))!=rec['sha256']:raise ValueError('Existing licensed asset copy differs')
    finally:tmp.unlink(missing_ok=True)
    return dest


def native_final_license_gate(studio,ep,doc,args,deps):
    """Use parent-verified library closure and the unchanged legacy gate."""
    from .. import final
    projected=editdoc.clone(doc)
    for track in projected['tracks'].get('music',[]):
        identity={'id':track.get('asset'),'sha256':track.get('sha256')}
        if identity not in deps['assets']['selected'] or not deps['assets']['coverage']['ok']:raise ValueError('Music license closure differs from the job')
        if sha256_file(R.regular(ep.root/track['file']))!=identity['sha256']:raise ValueError('Music copy changed')
    projected['tracks']['music']=[]
    return final._license_gate(ep,projected,False)


def selected_music_track(ep,args,deps):
    music=args.get('music')
    inherited=copy.deepcopy(deps.get('inherited_tracks',{}).get('music',[]))
    if not music:return inherited
    rec=next(r for r in deps['assets']['closure'] if r['id']==music['id'])
    suffix=Path(rec['file']).suffix.lower();rel=ep.rel(ep.work/'episode-assets'/(rec['sha256']+suffix))
    return inherited+[{'id':'final-music','file':rel,'asset':music['id'],'sha256':music['sha256'],'o0':0.0,'o1':exact(ep,args['rough'])[1]['duration'],
             'in':music['start'],'gain_db':music['gain_db'],'duck_db':music['duck_db'],'fade_in_s':music['fade_in_s'],'fade_out_s':music['fade_out_s']}]


def validate_native_motion(ep,doc,deps):
    inherited=deps.get('inherited_tracks',{}).get('overlays',[])
    if doc['tracks'].get('overlays',[])[:len(inherited)]!=inherited:raise ValueError('Inherited overlays changed')
    overlays=[o for o in doc['tracks'].get('overlays',[])[len(inherited):] if o.get('kind')=='motion']
    if len(overlays)!=len(deps['motion_specs']):raise ValueError('Motion titles differ from selected controls')
    for i,(overlay,item) in enumerate(zip(overlays,deps['motion_specs']),1+sum(o.get('kind')=='motion' for o in inherited)):
        spec=item['spec'];start=round(item['start'],3);path=R.regular(ep.work/'motion'/('lower-third-'+item['hash']+'.mov'))
        expected={'id':f'm{i}','file':ep.rel(path),'o0':start,'o1':round(start+spec['duration'],3),'x':0,'y':item['y'],
                  'w':doc['canvas']['w'],'h':doc['canvas']['h'],'layer':i,'kind':'motion','alpha':True,'template':'lower-third','props':spec['props']}
        if {k:v for k,v in overlay.items() if k!='sha256'}!=expected or sha256_file(path)!=overlay.get('sha256'):raise ValueError('Motion title content or identity changed')


def validate_native_broll(ep,doc,deps):
    inherited=deps.get('inherited_tracks',{}).get('overlays',[])
    if doc['tracks'].get('overlays',[])[:len(inherited)]!=inherited:raise ValueError('Inherited overlays changed')
    for o in inherited:
        if sha256_file(R.regular(ep.root/o['file']))!=(o.get('sha256') or o.get('library',{}).get('sha256')):raise ValueError('Inherited overlay bytes changed')
    overlays=doc['tracks'].get('overlays',[])[len(inherited):]
    if any(o.get('kind') not in ('motion','broll','credit') for o in overlays):raise ValueError('Unsupported final overlay')
    placed=[o for o in overlays if o.get('kind')=='broll'];credits=[o for o in overlays if o.get('kind')=='credit']
    if len(placed)!=len(deps['broll_proposals']):raise ValueError('B-roll placements differ from selected controls')
    expected_credit_ids=[]
    for overlay,proposal in zip(placed,deps['broll_proposals']):
        expected=copy.deepcopy(proposal['placement']);credit=proposal.get('credit')
        if credit:expected['credit_id']=expected['id']+'c';expected_credit_ids.append(expected['credit_id'])
        if overlay!=expected or sha256_file(R.regular(ep.root/overlay['file']))!=overlay['library']['sha256']:raise ValueError('B-roll placement or bytes changed')
        if credit:
            actual=next((c for c in credits if c.get('id')==expected['credit_id']),None)
            if not actual or actual.get('text')!=credit['text'] or actual.get('credit_for')!=expected['id'] or actual.get('o0')!=credit['o0'] or actual.get('o1')!=min(credit['o1'],doc['duration']):raise ValueError('Required B-roll credit changed')
            path=R.regular(ep.root/actual['file'])
            if not path.is_relative_to(R.regular(ep.work/'broll'/'credits',directory=True)) or sha256_file(path)!=actual.get('sha256'):raise ValueError('B-roll credit bytes changed')
    if sorted(c.get('id') for c in credits)!=sorted(expected_credit_ids):raise ValueError('B-roll credit set changed')


def validate_final_candidate(ep,doc,args,deps):
    _,rough=exact(ep,args['rough']);expected=copy.deepcopy(rough['tracks']['dialogue'])
    for segment in expected:segment['gain_db']=float(segment.get('gain_db',0))+args['gain_db']
    if doc.get('stage')!='final' or doc.get('final_spec')!=args or doc.get('final_inputs')!=deps:raise ValueError('Final candidate identity changed')
    if doc.get('sources')!=rough.get('sources') or doc.get('duration')!=rough.get('duration') or doc['tracks']['video']!=rough['tracks']['video'] or doc['tracks']['dialogue']!=expected:raise ValueError('Final programme differs from the reviewed rough cut')
    if doc['tracks'].get('room_tone')!=rough['tracks'].get('room_tone') or doc.get('native_source_hashes')!=deps['source_hashes']:raise ValueError('Final source or room-tone identity changed')
    if doc['tracks'].get('music')!=selected_music_track(ep,args,deps):raise ValueError('Final music differs from selected controls')
    if doc.get('caption_edit')!={'style':args['caption_style'],'burn':args['burn'],'corrections':args.get('corrections',rough.get('caption_edit',{}).get('corrections',[]))}:raise ValueError('Final caption choices differ from selected controls')
    for key,hash_key in (('srt','body_srt_sha256'),('ass','body_ass_sha256')):
        path=R.regular(ep.root/doc['captions'][key])
        if not path.is_relative_to(R.regular(ep.captions,directory=True)) or sha256_file(path)!=doc.get(hash_key):raise ValueError('Final body caption identity changed')
    validate_native_motion(ep,doc,deps);validate_native_broll(ep,doc,deps)


def exact(ep,payload):
    if payload.get('stage') not in ('assembly','rough','final','select','short') or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',str(payload.get('name',''))):raise ValueError('Choose an exact episode edit')
    number(payload.get('version'),1,1000000,'Version',True)
    p=R.regular(editdoc.path_for(ep,payload['stage'],payload['version'],payload['name']))
    if sha256_file(p)!=payload.get('sha256'):raise ValueError('EditDoc bytes changed')
    return p,R.json_file(p)


def rough_comparison_points(assembly,rough,report):
    """Map every detected decision to actual assembly and rough output clocks."""
    def episode_to_output(doc,t):
        segments=doc['tracks']['dialogue']
        for seg in segments:
            a=seg['e0'];b=a+seg['o1']-seg['o0']
            if a-1e-7<=t<b-1e-7:return seg['o0']+max(0,t-a)
        return None
    combined=bool(assembly.get('combined_constituents') or assembly.get('independent_project'));applied=report.get('cuts',[]);result={}
    for cut in report.get('detected_cuts',[]):
        before=cut['e0'] if combined else episode_to_output(assembly,cut['e0'])
        if before is None:continue
        if combined:
            after=before-sum(max(0,min(before,c['e1'])-c['e0']) for c in applied if c['e0']<before)
        else:
            kept=not any(c['id']==cut['id'] for c in applied)
            after=episode_to_output(rough,cut['e0'] if kept else cut['e1'])
            if after is None:
                after=next((s['o0'] for s in rough['tracks']['dialogue'] if s['e0']>=cut['e1']-1e-6),rough['duration'])
        result[cut['id']]={'assembly_output_s':max(0,before),'rough_output_s':max(0,after),'input_clock':'combined assembly output' if combined else 'episode source'}
    return result


def load(studio,eid,session='main'):
    return _construct_load(studio,eid,session)


def _construct_load(studio,eid,session,*,reader=None):
    """Shared view construction; only explicit read endpoints supply a Reader."""
    if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',session):raise ValueError('Invalid session')
    with R.resolve(studio,eid,verify_sources=False) as (ep,row):
        if reader:reader.bind(ep,row)
        status=reader.status if reader else C.status
        get_artifact=reader.artifact if reader else artifact
        ev=C.evidence(ep,row);probes={p['file']:p for p in (ev['media'] or {}).get('files',[])}
        results=[]
        directory=safe_dir(studio.sp,studio.sp.work/'production'/'episode-artifacts')
        for p in sorted(directory.glob('artifact_*.json')):
            rec=read(studio.sp,p)
            if rec['episode']==eid:
                try:results.append(public_artifact(get_artifact(studio,eid,rec['id'])))
                except (ValueError,OSError):continue
        sources=[]
        labels=R.json_file(ep.meta_path).get('source_labels',{})
        for source in row['sources']:
            preview=next((a for a in reversed(results) if a['kind']=='source' and a['source']['id']==source['id'] and a['grant_revision']==row['grant_revision']),None)
            sources.append({**source,'label':labels.get(source['file'],Path(source['file']).name),'probe':probes.get(source['file']),'preview':preview})
        transcript=ev['transcript'] or {};words=transcript.get('words',[])
        states={kind:status(studio,ep,row,C.latest(studio,eid,kind,'main' if kind=='speakers' else session)) for kind in C.KINDS if kind!='keep'}
        assemblies=[]
        for v in editdoc.list_versions(ep,'assembly',session):
            p=editdoc.path_for(ep,'assembly',v,session);doc=R.json_file(p)
            identity={'stage':'assembly','name':session,'version':v,'sha256':sha256_file(p)}
            render=next((a for a in reversed(results) if a.get('edit')==identity and a.get('kind')=='assembly'),None)
            current=reader.dependencies(doc.get('episode_inputs')) if reader else doc.get('episode_inputs')==_current_inputs(studio,ep,row,session,doc)
            assemblies.append({**identity,'duration':doc['duration'],'coverage':None if doc.get('combined_constituents') else assembly_coverage(doc,probes), 'constituents':copy.deepcopy(doc.get('combined_constituents',[])), 'excluded':doc.get('excluded',[]),'mode':editdoc.effective_mode(doc),'render':render,'inputs_current':current})
        roughs=[]
        for v in editdoc.list_versions(ep,'rough',session):
            p=editdoc.path_for(ep,'rough',v,session);doc=R.json_file(p)
            if not doc.get('rough_inputs'):continue
            identity={'stage':'rough','name':session,'version':v,'sha256':sha256_file(p)}
            render=next((a for a in reversed(results) if a.get('edit')==identity and a.get('kind')=='rough'),None)
            try:current=reader.dependencies(doc['rough_inputs']) if reader else rough_inputs(studio,ep,row,{'session':session,'assembly':doc['rough_inputs']['assembly']})==doc['rough_inputs']
            except (ValueError,OSError,KeyError):current=False
            report=copy.deepcopy(doc.get('rough_report'))
            if report:
                source_doc=exact(ep,doc['rough_inputs']['assembly'])[1]
                points=rough_comparison_points(source_doc,doc,report)
                for cut in report['detected_cuts']:cut['comparison']=points.get(cut['id'])
            if report and doc['tracks']['dialogue']:
                input_doc=exact(ep,doc['rough_inputs']['assembly'])[1] if doc.get('combined_constituents') else doc
                for decision in report['decisions']:
                    t=decision['e0'];segment=next((s for s in input_doc['tracks']['dialogue'] if s['o0']<=t<s['o1']),input_doc['tracks']['dialogue'][-1]) if doc.get('combined_constituents') else input_doc['tracks']['dialogue'][0]
                    src=input_doc['sources'][segment['src']];ref=next((s for s in sources if s['file']==src['file']),None)
                    episode_t=segment['e0']+t-segment['o0'] if doc.get('combined_constituents') else t
                    decision['context']={'source_id':ref['id'] if ref else None,'source_seconds':max(0,editdoc.src_time(src,episode_t))}
            roughs.append({**identity,'caption_corrections':copy.deepcopy(doc.get('caption_edit',{}).get('corrections',[])),'duration':doc['duration'],'mode':editdoc.effective_mode(doc),'render':render,'report':report,'inputs_current':current})
        sequences=[]
        renders=safe_dir(studio.sp,studio.sp.work/'production'/'episode-renders'/eid)
        for p in sorted(renders.glob('job_*/sequence-plan.json'),key=lambda p:p.stat().st_mtime_ns):
            p=safe(studio.sp,p);record=read(studio.sp,p);args=record.get('args',{})
            if args.get('session')!=session:continue
            job_id=p.parent.name;plan_sha=sha256_file(p)
            try:
                if reader:current=reader.sequence(record)
                else:sequence_record(studio,ep,row,job_id,plan_sha);current=True
            except (ValueError,OSError,KeyError):current=False
            components={tag:next((a for a in reversed(results) if a['kind']=='sequence-'+tag and a['details'].get('job_id')==job_id and a['details'].get('plan_sha256')==plan_sha),None) for tag in ('opening','closing') if args.get(tag,{}).get('enabled')}
            snapshot=record.get('snapshot',{});labels={a['id']:a.get('label') or 'Licensed recording' for a in snapshot.get('dependencies',{}).get('assets',{}).get('closure',[]) if a.get('id')}
            excerpts={};partial_words={}
            try:
                from .. import sequence
                _,base=exact(ep,args['rough']);base['native_source_hashes']={s['file']:s['sha256'] for s in row['sources']};base['caption_edit']={'style':'podcast','burn':False,'corrections':[]}
                for tag in ('opening','closing'):
                    for i,item in enumerate(args.get(tag,{}).get('components',[])):
                        if item.get('kind')=='excerpt':
                            key=f'{tag}:{i}';a,b=item['start'],item['end']
                            words,partial=sequence.excerpt_words(ep,base,a,b,tag=tag,index=i,corrections=item.get('corrections',[]))
                            excerpts[key]=' '.join(w['w'] for w in words)[:400]
                            partial_words[key]=[w['word'] for w in partial]
            except (ValueError,OSError,KeyError):pass
            sequences.append({'job_id':job_id,'plan_sha256':plan_sha,'inputs_current':current,'args':args,'plan':snapshot.get('plan'),'mode':snapshot.get('mode'),'components':components,'asset_labels':labels,'excerpts':excerpts,'partial_words':partial_words})
        finals=[]
        for v in editdoc.list_versions(ep,'final',session):
            p=editdoc.path_for(ep,'final',v,session);doc=R.json_file(p)
            if not doc.get('final_inputs'):continue
            identity={'stage':'final','name':session,'version':v,'sha256':sha256_file(p)}
            render=next((a for a in reversed(results) if a.get('edit')==identity and a['kind']=='final'),None)
            try:current=reader.dependencies(doc['final_inputs']) if reader else final_inputs(studio,ep,row,doc['final_spec'])==doc['final_inputs']
            except (ValueError,OSError,KeyError):current=False
            labels={a['id']:a.get('label') or 'Licensed recording' for a in doc['final_inputs'].get('assets',{}).get('closure',[]) if a.get('id')}
            finals.append({**identity,'duration':doc['duration'],'mode':editdoc.effective_mode(doc),'render':render,'inputs_current':current,'spec':doc['final_spec'],'asset_labels':labels})
        proposed=None
        if words:
            from .. import trim
            try:proposed=trim.suggest(ep,session=None,save=False,policy=trim.load_policy())
            except (ValueError,SystemExit,KeyError):pass
        if assemblies and states['assembly'].get('payload',{}).get('version')!=assemblies[-1]['version'] and states['assembly']['state'] in ('reviewed exact','review recorded'):
            states['assembly']={'state':'needs review','reason':'A newer assembly version has not been reviewed','prior_review':states['assembly']}
        if roughs and states['rough'].get('payload',{}).get('version')!=roughs[-1]['version'] and states['rough']['state'] in ('reviewed exact','review recorded'):
            states['rough']={'state':'needs review','reason':'A newer rough version has not been reviewed','prior_review':states['rough']}
        if sequences and states['sequence'].get('payload',{}).get('job_id')!=sequences[-1]['job_id'] and states['sequence']['state'] in ('reviewed exact','review recorded'):
            states['sequence']={'state':'needs review','reason':'A newer sequence plan has not been reviewed','prior_review':states['sequence']}
        if finals and states['final'].get('payload',{}).get('version')!=finals[-1]['version'] and states['final']['state'] in ('reviewed exact','review recorded'):
            states['final']={'state':'needs review','reason':'A newer final version has not been reviewed','prior_review':states['final']}
        combined_choices=[]
        session_names=list(dict.fromkeys(e['session'] for e in C.events(studio) if e.get('episode')==eid and e.get('kind')=='assembly' and e.get('session')!='combined'))
        for name in session_names:
            event=C.latest(studio,eid,'assembly',name);saved=status(studio,ep,row,event)
            if saved['state'] in ('reviewed exact','review recorded'):
                _,doc=exact(ep,event['payload']);combined_choices.append({'session':name,'identity':event['payload'],'duration':doc['duration'],'trim':doc.get('trim'),'excluded':doc.get('excluded',[])})
        return {'final_layout':R.policy()['final_layout'],'sequence_max_components':R.policy()['sequence_max_components'],'suggested_bounds':proposed,'id':eid,'label':row['label'],'ownership':row['ownership'],'grant_revision':row['grant_revision'],'session':session,
                'adoption':row.get('adoption'),'combined_choices':combined_choices,'sources':sources,'sessions':(ev['media'] or {}).get('sessions',[]) or [{'id':'main','label':'Full recording'}],
                'media':ev['media'],'sync':ev['sync'],'transcript':{'source':transcript.get('source'),'engine':transcript.get('engine'),'utterances':transcript.get('utterances',[])[:200],'word_count':len(words),'speakers':sorted({w['speaker'] for w in words if w.get('speaker')})} if ev['transcript'] else None,
                'checkpoints':states,'assemblies':assemblies,'roughs':roughs,'sequences':sequences,'finals':finals,'legacy_claims':{'trim':R.json_file(ep.meta_path).get('trim',{}),'note':'Editable historical records are unverified in the app; they never replace exact native confirmation.'},
                'inputs':{op:state_hash(ep,row,op,session) for op in OPS if op not in ('episode-create','episode-adopt')}}


def _current_inputs(studio,ep,row,session,doc=None):
    try:
        if session=='combined':return combined_inputs(studio,ep,row,{'session':'combined','constituents':[r['identity'] for r in (doc or {}).get('combined_constituents',[])]})
        return base_inputs(studio,ep,row,session)
    except ValueError:return None



def fresh_analysis(ep,row):
    """Native consumers never trust adopted/editable analysis caches or receipts.

    Refresh channel evidence from current registered originals, force the shared
    extractor, then check actual output bytes and selected audio-stream duration.
    Legacy CLI cache semantics remain unchanged. The returned audit receipt is
    evidence for this operation only; it is never accepted as a reuse grant.
    """
    from .. import audio
    from ..config import binary
    import soundfile as sf
    import subprocess
    R.assert_sources(ep,row,[s['id'] for s in row['sources']])
    info=media.ingest(ep)
    for item in info['files']:number(item['duration'],.001,R.policy()['max_source_seconds'],'Episode source duration')
    audio.extract_all(ep,force=True)
    sources={s['file']:s for s in row['sources']};receipts=[]
    for item in info['files']:
        if not item['audio']:continue
        raw=R.regular(ep.root/item['file']);out=R.regular(audio.analysis_wav_path(ep,item['file']))
        actual=sf.info(out)
        stream=next(s for s in media._ffprobe(raw)['streams'] if s.get('codec_type')=='audio')
        expected=float(stream.get('duration') or 0)
        duration_basis='selected audio stream duration'
        if expected<=0:
            # Some containers omit stream duration. Count decoded samples from
            # that exact stream without retaining another recording in memory.
            proc=subprocess.Popen([str(binary('ffmpeg')),'-nostdin','-v','error','-i',str(raw),'-map','0:a:0','-vn','-ac','1','-ar',str(audio.SAMPLE_RATE),'-f','s16le','-'],stdout=subprocess.PIPE)
            count=0
            try:
                while chunk:=proc.stdout.read(1024*1024):count+=len(chunk)
            finally:proc.stdout.close()
            if proc.wait():raise ValueError('Cannot verify decoded source audio duration')
            expected=count/(2*audio.SAMPLE_RATE);duration_basis='independently counted decoded source samples'
        tolerance=R.policy()['analysis_duration_tolerance_s']
        if actual.frames<=0 or actual.samplerate!=audio.SAMPLE_RATE or actual.channels!=1 or abs(actual.duration-expected)>tolerance:
            raise ValueError('Fresh analysis does not match the selected source audio stream: '+item['file'])
        receipts.append({'source':sources[item['file']],'analysis_sha256':sha256_file(out),'frames':actual.frames,'sample_rate':actual.samplerate,
                         'duration':actual.duration,'source_audio_duration':expected,'duration_basis':duration_basis,'tolerance_s':tolerance,
                         'channel_map':item.get('channel_map'),'policy':'native-fresh-analysis.v1; no cache reuse'})
    R.assert_sources(ep,row,[s['id'] for s in row['sources']])
    return info,receipts


def assembly_coverage(doc,probes):
    """Actual episode/output relationship, distinct from requested trim bounds."""
    retained=[]
    for seg in doc['tracks']['dialogue']:
        retained.append({'episode_start':seg['e0'],'episode_end':seg['e0']+seg['o1']-seg['o0'],
                         'output_start':seg['o0'],'output_end':seg['o1']})
    bounds=doc.get('trim') or {'in':min(r['episode_start'] for r in retained),'out':max(r['episode_end'] for r in retained)}
    lo,hi=float(bounds['in']),float(bounds['out'])
    reference=doc['sources'][doc['tracks']['dialogue'][0]['src']]['file'];context=doc.get('assembly_context',{});duration=context.get('reference_duration',probes.get(reference,{}).get('duration'))
    from ..cutplan import complement
    spans=[(r['episode_start'],r['episode_end']) for r in retained]
    def reason(a,b):
        transcript_end=context.get('transcript_duration')
        if transcript_end is not None and a>=transcript_end-1e-6:return 'Outside available transcript coverage'
        cameras=context.get('camera_coverage',[])
        if cameras and not any(max(a,c['start'])<min(b,c['end'])-1e-6 for c in cameras):return 'No selected camera covers this interval'
        return 'Not retained by the usable camera/source coverage plan'
    inside=[{'start':a,'end':b,'reason':reason(a,b)} for a,b in complement(spans,lo,hi)]
    rounding=[r for r in inside if r['end']-r['start']<=editdoc.EPS and (abs(r['start']-lo)<1e-9 or abs(r['end']-hi)<1e-9)]
    outside=[]
    if lo>0:outside.append({'start':0.,'end':lo,'reason':'Before confirmed start'})
    if duration is not None and hi<duration:outside.append({'start':hi,'end':duration,'reason':'After confirmed end'})
    return {'reference_duration':duration,'confirmed_bounds':{'in':lo,'out':hi},'confirmed_duration':hi-lo,
            'output_duration':doc['duration'],'retained':retained,'excluded_inside_bounds':inside,'outside_confirmed_bounds':outside,
            'omitted_inside_seconds':sum(r['end']-r['start'] for r in inside),
            'boundary_precision_seconds':editdoc.EPS,'boundary_rounding':rounding}


def progress_phase(studio,job,label):
    from .workbench import job_path
    path=job_path(studio.sp,job['id'])
    if path.exists():
        live=read(studio.sp,path);live['phase']=label;write(studio.sp,path,live)


def render_progress(studio,job):
    last=[0.]
    def update(phase,done,total):
        import time
        now=time.monotonic()
        if done is not None and done!=total and now-last[0]<1:return
        last[0]=now
        progress_phase(studio,job,phase+(f' · {done}/{total}' if done is not None else '')+' · cancellation available')
    return update


def execute(studio,job):
    check_descriptor(studio,job);args=job['args'];op=job['operation']
    from . import episode_release as release
    if op in release.OPS:return release.execute(studio,job)
    if op=='episode-create':
        from . import episode_worker_bridge as bridge
        return bridge.CLIENT.call('create') if bridge.CLIENT is not None else create(studio,args)
    from .. import audio,transcribe,sync,assemble,cutplan
    with R.resolve(studio,args['episode'],args['grant_revision'],verify_sources=True) as (ep,row), editdoc.mutation_lock(ep):
        before=state_hash(ep,row,op,args['session'])
        if before!=args['input_sha256']:raise ValueError('Episode inputs changed before execution')
        if op in ('episode-intake','episode-transcribe','episode-sync'):
            progress_phase(studio,job,'Analysing complete recordings and channels · cancellation available')
            info,analysis=fresh_analysis(ep,row)
        if op=='episode-intake':
            previews=[]
            for index,source in enumerate(row['sources'],1):
                progress_phase(studio,job,f'Preparing recording {index} of {len(row["sources"])} · '+Path(source['file']).name+' · cancellation available')
                probe=next(m for m in info['files'] if m['file']==source['file'])
                number(probe['duration'],.001,R.policy()['max_source_seconds'],'Episode source duration')
                directory=studio.sp.work/'production'/'episode-previews'/row['id']/source['sha256']
                out,wave=proxy(studio.sp,ep.root/source['file'],directory,probe,progress=lambda phase,done,total:progress_phase(studio,job,f'Recording {index}/{len(row["sources"])} · {phase} {done/60:.1f}/{total/60:.1f} min · cancellation available'))
                progress_phase(studio,job,f'Verifying recording {index}/{len(row["sources"])} and source integrity before saving its preview · cancellation available')
                previews.append(register_artifact(studio,row,out,kind='source',source=source,details={'waveform':read(studio.sp,wave),'probe':probe}))
            return {'episode':row['id'],'previews':previews,'analysis':analysis,'warnings':info.get('warnings',[])}
        if op=='episode-transcribe':
            source=next(s for s in row['sources'] if s['id']==args['source_id'])
            progress_phase(studio,job,'Transcribing the complete reference recording with the local speech engine · cancellation available')
            result=transcribe.transcribe(ep,source=source['file'],num_speakers=args['speakers'],diarize=args['speakers']>1)
            return {'episode':row['id'],'words':len(result['words']),'engine':result['engine'],'analysis':analysis}
        if op=='episode-sync':
            source=next(s for s in row['sources'] if s['id']==args['source_id'])
            progress_phase(studio,job,'Aligning camera recordings and measuring clock drift against the complete reference · cancellation available')
            info=C.evidence(ep,row)['media'];syncable=[m for m in info['files'] if m['audio']]
            if len(syncable)==1:
                result={'reference':source['file'],'files':{source['file']:{'start_offset':0.0,'drift_ppm':0.0,'residual_ms':0.0,'points':[]}},'warnings':[],'method':'single source reference; no alignment inferred'}
                write_json(ep.artifact('sync.json'),result)
            else:result=sync.sync_episode(ep,reference=source['file'],points=args['points'])
            return {'episode':row['id'],'sync':result,'analysis':analysis}
        if op=='episode-final':
            from .. import captions,compile as compiler,ffutil,presets,sequence
            from .video_workshop import caption_snapshot,pinned_caption
            deps=final_inputs(studio,ep,row,args);_,rough=exact(ep,args['rough']);doc=editdoc.clone(rough);doc['stage']='final'
            for segment in doc['tracks']['dialogue']:segment['gain_db']=float(segment.get('gain_db',0))+args['gain_db']
            if args.get('music'):
                rec=next(r for r in deps['assets']['closure'] if r['id']==args['music']['id']);copy_library_to_episode(studio,ep,rec)
            doc['tracks']['music']=selected_music_track(ep,args,deps)
            if deps['motion_specs']:
                from .. import motion
                for item,layout_item in zip(args['motion'],deps['motion_specs']):
                    overlay=motion.build_overlay(ep,doc,'lower-third',{'name':item['name'],'title':item.get('title',''),'side':item.get('side','left')},item['start'],item['duration'])
                    overlay['y']=layout_item['y'];overlay['sha256']=sha256_file(R.regular(ep.root/overlay['file']));doc['tracks']['overlays'].append(overlay)
            if deps['broll_proposals']:
                from ..broll import place
                for proposal in deps['broll_proposals']:
                    aid=proposal['placement']['library']['asset'];rec=next(r for r in deps['assets']['closure'] if r['id']==aid)
                    copy_library_to_episode(studio,ep,rec)
                    _,credit=place.append_placement(ep,doc,proposal['placement'],proposal.get('credit'),deps['broll_config'])
                    if credit:credit['sha256']=sha256_file(R.regular(ep.root/credit['file']))
            doc['native_source_hashes']=deps['source_hashes']
            doc['caption_edit']={'style':args['caption_style'],'burn':args['burn'],'corrections':copy.deepcopy(args.get('corrections',rough.get('caption_edit',{}).get('corrections',[])))}
            caption_snapshot(studio.sp,ep,doc,granted_episode=True);profile=pinned_caption(studio.sp,ep,doc,granted_episode=True)
            if profile['style']!=deps['caption_profile']['style'] or profile['font']['sha256']!=deps['caption_profile']['font_sha256']:raise ValueError('Caption style changed during final preparation')
            cues=captions.cues_from_edit(ep,doc,profile['style']);capdir=R.regular(ep.captions/'native'/job['id'],directory=True);capdir.mkdir(parents=True,exist_ok=True)
            srt=write_caption_once(ep,capdir/'body.srt',lambda p:captions.write_srt(cues,p))
            ass=write_caption_once(ep,capdir/'body.ass',lambda p:captions.write_ass(cues,profile['style'],(doc['canvas']['w'],doc['canvas']['h']),p))
            doc['captions']={'srt':ep.rel(srt),'ass':ep.rel(ass),'burn':args['burn']};doc['body_srt_sha256']=sha256_file(srt);doc['body_ass_sha256']=sha256_file(ass);doc['final_inputs']=deps;doc['final_spec']=copy.deepcopy(args)
            doc.setdefault('history',[]).append({'request_id':args['request_id'],'operation':'episode-final','inputs':deps})
            if deps['broll_proposals'] or any(o.get('kind')=='broll' for o in deps.get('inherited_tracks',{}).get('overlays',[])):
                candidate=R.regular(ep.work/'native-final'/job['id']/'candidate.json');candidate.parent.mkdir(parents=True,exist_ok=True)
                if candidate.exists():
                    if R.json_file(candidate)!=doc:raise ValueError('Final candidate differs from the existing job identity')
                else:write_json(candidate,doc)
            progress_phase(studio,job,'Verifying selected media and licenses · cancellation available')
            native_final_license_gate(studio,ep,doc,args,deps)
            problems=editdoc.validate(doc)
            if problems:raise ValueError('; '.join(problems))
            p=editdoc.save_new_version(ep,doc,name=args['session'],locked=True,request_id=args['request_id']);doc=R.json_file(p)
            identity={'stage':'final','name':args['session'],'version':int(editdoc._V.match(p.name)['n']),'sha256':sha256_file(p)}
            directory=safe_dir(studio.sp,studio.sp.work/'production'/'episode-renders'/row['id']/job['id']);directory.mkdir(parents=True,exist_ok=True)
            receipt=directory/'render-receipt.json';saved=read(studio.sp,receipt) if receipt.exists() else None
            if saved:
                out=safe(studio.sp,directory/saved['filename']);program_srt=safe(studio.sp,directory/saved['caption']['filename'])
                if saved['edit']!=identity or sha256_file(out)!=saved['sha256'] or sha256_file(program_srt)!=saved['caption']['sha256']:raise ValueError('Final output receipt changed')
                report=saved['report'];capinfo=saved['caption']
            else:
                suffix='.wav' if deps['mode']=='audio' else '.mp4';n=0;out=directory/('final'+suffix);attempt=directory/f'attempt-{n}'
                while out.exists() or out.with_suffix('.srt').exists() or attempt.exists():
                    n+=1;out=directory/(f'final.recovery{n}'+suffix);attempt=directory/f'attempt-{n}'
                attempt.mkdir();parts={};warnings=[];intro=0.0
                if deps['mode']=='audio':
                    body=attempt/'body.wav';compiler.compile_audio(ep,doc,body,warnings=warnings)
                    for tag,rec in deps['components'].items():parts[tag]=safe(studio.sp,studio.sp.root/rec['file'])
                    if 'opening' in parts:intro=deps['components']['opening']['details']['report']['samples']/compiler.SR
                    joined=sequence.join_audio_files([x for x in (parts.get('opening'),body,parts.get('closing')) if x],attempt/'joined.wav',fade_s=deps['sequence_plan']['policy']['audio']['join_fade_s'])
                    compiler._normalize(attempt/'joined.wav',out,presets.get('audio_only'),joined['samples'],warnings)
                    import soundfile as sf
                    info=sf.info(out)
                    if info.frames!=joined['samples']:raise ValueError('Final audio sample count changed')
                    loud=ffutil.measure_loudness(out)
                    report={'duration':info.duration,'samples':info.frames,'sample_rate':info.samplerate,'joins_samples':joined['joins_samples'],
                            'lufs':round(loud['lufs'],2),'true_peak_db':round(loud['true_peak_db'],2),'warnings':warnings,'preset':'audio WAV'}
                else:
                    chosen_preset=deps['export_preset']['name']
                    if presets.get(chosen_preset)!=deps['export_preset']['settings']:raise ValueError('Final export preset changed')
                    body=attempt/'body.mp4';body_report=compiler.render(ep,doc,body,preset=chosen_preset,burn_captions=args['burn'],progress=render_progress(studio,job))
                    seqargs=deps['sequence_args'];pl,snapshot=make_sequence_plan(studio,ep,seqargs,deps['sequence_plan']['dependencies'])
                    if snapshot!=deps['sequence_plan']:raise ValueError('Sequence plan changed before final build')
                    pl['_bdoc']=doc
                    for tag in ('opening','closing'):
                        if not seqargs[tag]['enabled']:continue
                        native=R.regular(ep.work/'sequence'/'native'/job['id']/f'{tag}-master-{n}',directory=True);native.mkdir(parents=True,exist_ok=False)
                        built=sequence.build(ep,name=args['session'],preset=chosen_preset,which=(tag,),pl=pl,record=False,output_dir=native,policy_data=snapshot['policy'],burn=args['burn'],cold_words=native_cold_words(ep,row,rough,seqargs.get('cold_open')) if tag=='opening' else None)['built'][tag]
                        parts[tag]=R.regular(ep.root/built['file'])
                        if tag=='opening':intro=built['frames']/built['fps']
                    if parts:
                        validate_programme_duration([body_report['duration'],*[media.probe_file(part,ep.root)['duration'] for part in parts.values()]])
                        report=sequence.assemble(ep,body,parts.get('opening'),parts.get('closing'),out,preset=chosen_preset,fade_s=snapshot['policy']['audio']['join_fade_s'],overwrite=False)
                    else:
                        import shutil
                        shutil.copyfile(body,out);report={**body_report,'duration':body_report['duration']}
                # The planned programme sidecar includes only source-derived
                # spoken cues. Selected recordings without transcripts remain warnings.
                program_cues=[];adjustments=[]
                programme_duration=report['samples']/report['sample_rate'] if deps['mode']=='audio' else report.get('expected_duration',report['duration'])
                body_duration=round(doc['duration']*compiler.SR)/compiler.SR if deps['mode']=='audio' else ((report['frames']['body']/report['fps']) if parts else report['duration'])
                body_end=intro+body_duration
                def append_component(source_cues,start,end,label):
                    bounded,changes=bound_component_cues(source_cues,start,end,label);program_cues.extend(bounded);adjustments.extend(changes)
                for tag in ('opening','closing'):
                    rec=deps['components'].get(tag);cap=(rec or {}).get('details',{}).get('caption')
                    if cap:
                        path=safe(studio.sp,(studio.sp.root/rec['file']).parent/cap['filename'])
                        start,end=(0,intro) if tag=='opening' else (body_end,programme_duration)
                        append_component([captions.Cue(c['start'],c['end'],c['text'],words=copy.deepcopy(c.get('words',[]))) for c in cap['cues']],start,end,tag)
                body_cues=copy.deepcopy(cues)
                for cue in body_cues:
                    for word in cue.words:word['rough_start']=word['start']
                append_component(body_cues,intro,body_end,'body')
                program_cues.sort(key=lambda c:c.start)
                program_cues,outer_adjustments=bound_program_cues(program_cues,programme_duration);adjustments.extend(outer_adjustments)
                if adjustments:report.setdefault('warnings',[]).append('Caption cues crossed a component boundary; inspect the recorded adjustments and audio')
                caption_problems=captions.check(program_cues,profile['style'])
                program_srt=out.with_suffix('.srt');captions.write_srt(program_cues,program_srt)
                capinfo={'filename':program_srt.name,'sha256':sha256_file(program_srt),'cues':[{'start':c.start,'end':c.end,'text':c.text,
                         'words':[{k:w[k] for k in ('w','original_word','start','end','rough_start','word_id','occurrence_id','transcript_sha256','component_tag','component_index') if k in w} for w in c.words]} for c in program_cues],
                         'status':'planned from source transcripts; wording and completeness need review','boundary_adjustments':adjustments,'problems':caption_problems}
                validate_programme_caption_boundaries(capinfo['cues'],report)
                credits=list(dict.fromkeys(deps['assets']['coverage']['attributions']+deps['sequence_plan']['dependencies']['assets']['coverage']['attributions']))
                report['credits']=credits;report['caption_snapshot_sha256']=doc['caption_snapshot']['sha256'];report['warnings']=report.get('warnings',[])+[w for rec in deps['components'].values() for w in rec['details'].get('report',{}).get('warnings',[])]
                if credits:report['warnings'].append('Check the required credit lines in the picture, closing and release description')
                report['warnings']=[w.replace("leave a little room at the cut's edge (ave trim) or lower sequences audio.join_fade_s",'listen to this join and adjust the conversation boundaries if needed') for w in report['warnings']]
                write(studio.sp,receipt,{'edit':identity,'filename':out.name,'sha256':sha256_file(out),'caption':capinfo,'report':report})
            from .workbench import job_path
            jobfile=job_path(studio.sp,job['id'])
            if jobfile.exists():
                live=read(studio.sp,jobfile);live['phase']='Checking captions against the final audio';write(studio.sp,jobfile,live)
            asr_receipt=directory/'asr-receipt.json';media_sha=sha256_file(out);source_sha=C.file_sha(ep,'transcript.json')
            if asr_receipt.exists():
                asr=read(studio.sp,asr_receipt);evidence_path=safe(studio.sp,directory/asr['filename'])
                if asr['media_sha256']!=media_sha or asr['configuration']!=deps['asr_configuration'] or asr['source_transcript_sha256']!=source_sha or sha256_file(evidence_path)!=asr['sha256']:raise ValueError('Final transcription evidence changed; completed output preserved')
            else:
                scratch=directory/'asr-working';scratch.mkdir(exist_ok=True)
                trans=final_asr(ep,out,scratch,deps['asr_configuration'])
                if C.file_sha(ep,'transcript.json')!=source_sha or sha256_file(out)!=media_sha:raise ValueError('Final audio or source transcript changed during verification')
                comparison=compare_final_captions(capinfo['cues'],trans['words'])
                content={'schema':'episode_final_asr.v1','media_sha256':media_sha,'configuration':deps['asr_configuration'],
                         'source_transcript_sha256':source_sha,'clock':'final programme output','transcript':trans,'comparison':comparison}
                name='asr-evidence-'+R.digest(content)+'.json';evidence_path=directory/name
                if evidence_path.exists():
                    if read(studio.sp,evidence_path)!=content:raise ValueError('ASR evidence identity collision')
                else:write(studio.sp,evidence_path,content)
                asr={'filename':name,'sha256':sha256_file(evidence_path),'media_sha256':media_sha,'configuration':deps['asr_configuration'],
                     'source_transcript_sha256':source_sha,'comparison':comparison}
                write(studio.sp,asr_receipt,asr)
            report={**report,'asr_comparison':asr['comparison']}
            wave=compiled_waveform(studio.sp,out,directory/'browser',media.probe_file(out,studio.sp.root))
            if final_inputs(studio,ep,row,args)!=deps:raise ValueError('Final dependencies changed during rendering')
            parents=[deps['rough_asset']]+[rec['details']['asset'] for rec in deps['components'].values()]+[x['id'] for x in deps['assets']['selected']]
            if not parents[0]:raise ValueError('Rough render has no provenance asset')
            progress_phase(studio,job,'Verifying exact inputs and registering output · cancellation available')
            asset=library.add_derivative(studio.sp,out,derived_from=parents,origin={'schema':'episode_final_render.v1','episode':row['id'],'edit':identity,'dependencies':deps,'caption_sha256':capinfo['sha256'],'report':report},by='episode-workbench')
            art=register_artifact(studio,row,out,kind='final',edit=identity,details={'report':report,'caption':capinfo,'asr':asr,'asset':asset['id'],'mode':deps['mode'],'waveform':read(studio.sp,wave)})
            return {'episode':row['id'],'edit':identity,'artifact':art,'report':report,'review':'needs review'}
        if op=='episode-sequence':
            from .. import sequence
            deps=sequence_inputs(studio,ep,row,args);pl,snapshot=make_sequence_plan(studio,ep,args,deps)
            directory=safe_dir(studio.sp,studio.sp.work/'production'/'episode-renders'/row['id']/job['id']);directory.mkdir(parents=True,exist_ok=True)
            planfile=directory/'sequence-plan.json';record={'snapshot':snapshot,'descriptor':job['episode_descriptor'],'operation':op,'args':args}
            if planfile.exists():
                if read(studio.sp,planfile)!=record:raise ValueError('Sequence plan identity changed; completed files preserved')
            else:write(studio.sp,planfile,record)
            plan_sha=sha256_file(planfile);outputs={};_,rough=exact(ep,args['rough']);rough['native_source_hashes']={s['file']:s['sha256'] for s in row['sources']};rough['caption_edit']={'style':'podcast','burn':False,'corrections':[]};selected_assets={r['id']:r for r in deps['assets']['closure']}
            for tag in ('opening','closing'):
                if not args[tag]['enabled']:continue
                receipt=directory/(tag+'-receipt.json');saved=read(studio.sp,receipt) if receipt.exists() else None
                if saved:
                    out=safe(studio.sp,directory/saved['filename'])
                    if saved['plan_sha256']!=plan_sha or sha256_file(out)!=saved['sha256']:raise ValueError('Sequence preview receipt changed')
                    measured=saved['report']
                else:
                    suffix='.wav' if deps['mode']=='audio' else '.mp4';out=directory/(tag+suffix);n=0
                    while out.exists():n+=1;out=directory/(f'{tag}.recovery{n}'+suffix)
                    if deps['mode']=='audio':
                        parts=[]
                        for item in args[tag]['components']:
                            if item['kind']=='excerpt':parts.append({'kind':'excerpt','doc':rough,'o0':item['start'],'o1':item['end'],'corrections':item.get('corrections',[]),'tag':tag})
                            else:parts.append({'kind':'recording','file':str(safe(studio.sp,studio.sp.root/selected_assets[item['id']]['file'])),'start':item['start'],'duration':item['duration']})
                        measured=sequence.build_audio_components(ep,parts,out,fade_s=snapshot['policy']['audio']['join_fade_s'])
                        from .. import captions
                        import yaml
                        style=captions.get_style('podcast',styles=yaml.safe_load(captions.REPO_CONFIG.read_text()))
                        cues=captions.cues_from_words(measured['words'],style)
                        caption=out.with_suffix('.srt');captions.write_srt(cues,caption)
                        measured['caption']={'filename':caption.name,'sha256':sha256_file(caption),'cues':[{'start':c.start,'end':c.end,'text':c.text,'words':c.words} for c in cues],
                                             'status':'planned from source transcript; wording not confirmed'}
                    else:
                        native=R.regular(ep.work/'sequence'/'native'/job['id']/f'{tag}-{n}',directory=True)
                        while native.exists():
                            n+=1;native=R.regular(ep.work/'sequence'/'native'/job['id']/f'{tag}-{n}',directory=True)
                        native.mkdir(parents=True,exist_ok=False)
                        built=sequence.build(ep,name=args['session'],preset='preview',which=(tag,),pl=pl,record=False,output_dir=native,policy_data=snapshot['policy'],cold_words=native_cold_words(ep,row,rough,args.get('cold_open')) if tag=='opening' else None)
                        measured=built['built'][tag];source=R.regular(ep.root/measured['file'])
                        copy_episode_output(studio,ep,source,out)
                        if measured.get('cold_open_srt'):
                            from .. import captions
                            source_caption=R.regular(ep.root/measured['cold_open_srt']);dest=out.with_suffix('.srt');copy_episode_output(studio,ep,source_caption,dest)
                            cues=captions.parse_srt(dest)
                            measured['caption']={'filename':dest.name,'sha256':sha256_file(dest),'cues':measured.get('cold_cues',[{'start':c.start,'end':c.end,'text':c.text} for c in cues]),
                                                 'status':'planned from source transcript; wording and completeness need review'}
                    write(studio.sp,receipt,{'plan_sha256':plan_sha,'filename':out.name,'sha256':sha256_file(out),'report':measured})
                wave=compiled_waveform(studio.sp,out,directory/('browser-'+tag),media.probe_file(out,studio.sp.root))
                if sequence_inputs(studio,ep,row,args)!=deps:raise ValueError('Sequence inputs changed during preview')
                parents=[deps['rough_asset']]+[s['id'] for s in deps['assets']['selected']]
                if not parents[0]:raise ValueError('Rough render has no provenance asset')
                progress_phase(studio,job,'Verifying exact inputs and registering output · cancellation available')
                asset=library.add_derivative(studio.sp,out,derived_from=parents,origin={'schema':'episode_sequence_render.v1','episode':row['id'],'plan_sha256':plan_sha,'component':tag,'rough':args['rough']},by='episode-workbench')
                details={'plan_sha256':plan_sha,'job_id':job['id'],'report':measured,'asset':asset['id'],'mode':deps['mode'],'waveform':read(studio.sp,wave),
                         **({'caption':measured['caption']} if measured.get('caption') else {})}
                art=register_artifact(studio,row,out,kind='sequence-'+tag,edit={k:args['rough'][k] for k in ('stage','name','version','sha256')},details=details)
                outputs[tag]={'artifact':art,**({'samples':measured['samples']} if deps['mode']=='audio' else {'duration':measured['duration']})}
            return {'episode':row['id'],'draft':True,'plan':snapshot['plan'],'components':outputs,
                    'review_payload':{'job_id':job['id'],'plan_sha256':plan_sha,'artifacts':{k:v['artifact']['id'] for k,v in outputs.items()}}}
        if op=='episode-combined':
            deps=combined_inputs(studio,ep,row,args);doc=compose_assemblies(ep,deps)
            doc['registration']={'id':row['id'],'grant_revision':row['grant_revision']};doc['native_source_hashes']={s['file']:s['sha256'] for s in row['sources']}
            doc['history'].append({'request_id':args['request_id'],'operation':'episode-combined','inputs':deps})
            path=editdoc.save_new_version(ep,doc,name='combined',locked=True,request_id=args['request_id']);doc=R.json_file(path)
            identity={'stage':'assembly','name':'combined','version':int(editdoc._V.match(path.name)['n']),'sha256':sha256_file(path)}
            from ..compile import render
            mode=editdoc.effective_mode(doc);directory=safe_dir(studio.sp,studio.sp.work/'production'/'episode-renders'/row['id']/job['id']);directory.mkdir(parents=True,exist_ok=True)
            suffix='.m4a' if mode=='audio' else '.mp4';out=directory/('assembly'+suffix);receipt=directory/'render-receipt.json';saved=read(studio.sp,receipt) if receipt.exists() else None
            if saved:
                out=safe(studio.sp,directory/saved['filename'])
                if saved['edit']!=identity or sha256_file(out)!=saved['sha256']:raise ValueError('Combined render receipt changed')
                report=saved['report']
            else:
                n=0
                while out.exists():n+=1;out=directory/(f'assembly.recovery{n}'+suffix)
                report=render(ep,doc,out,preset='audio_only' if mode=='audio' else 'preview',burn_captions=False,progress=render_progress(studio,job))
                write(studio.sp,receipt,{'edit':identity,'filename':out.name,'sha256':sha256_file(out),'report':report})
            wave=compiled_waveform(studio.sp,out,directory/'browser',media.probe_file(out,studio.sp.root))
            if combined_inputs(studio,ep,row,args)!=deps:raise ValueError('A constituent review changed during combination')
            progress_phase(studio,job,'Verifying exact inputs and registering output · cancellation available')
            asset=library.add_derivative(studio.sp,out,derived_from=[r['asset'] for r in deps['constituents']],origin={'schema':'episode_combined_render.v1','episode':row['id'],'edit':identity,'dependencies':deps,'report':report},by='episode-workbench')
            result=register_artifact(studio,row,out,kind='assembly',edit=identity,details={'report':report,'asset':asset['id'],'mode':mode,'waveform':read(studio.sp,wave)})
            return {'episode':row['id'],'edit':identity,'artifact':result,'review':'needs review'}
        if op=='episode-rough':
            from .. import cleanup
            deps=rough_inputs(studio,ep,row,args)
            _,assembly=exact(ep,args['assembly'])
            if not row.get('adoption'):fresh_analysis(ep,row)
            pol=cleanup._deep_merge(cleanup.load_policy(),R.policy()['cleanup_choices'][args['policy']])
            cleanup.validate_policy(pol)
            if assembly.get('combined_constituents') or assembly.get('independent_project'):
                directory=safe_dir(studio.sp,studio.sp.work/'production'/'episode-renders'/row['id']/job['id']);directory.mkdir(parents=True,exist_ok=True)
                n=0;analysis_wav=directory/'combined-analysis.wav'
                while analysis_wav.exists():n+=1;analysis_wav=directory/f'combined-analysis-{n}.wav'
                analyzed=cleanup.analyze_occurrences(ep,assembly,analysis_wav,pol,restore_cuts=args['restore'],
                    progress=lambda phase:progress_phase(studio,job,phase+' · cancellation available'))
            else:
                progress_phase(studio,job,'Analyzing source pauses and edit seams · cancellation available')
                analyzed=cleanup.analyze(ep,assembly,pol,overrides={},restore_cuts=args['restore'])
            doc=analyzed['doc'];doc['stage']='rough';doc['rough_inputs']=deps
            report={'policy':args['policy'],'resolved_policy':pol,'restored':args['restore'],'detected_cuts':[{k:v for k,v in c.items() if k!='_region'} for c in analyzed['detected_cuts']],
                    'decisions':analyzed['decisions'],'cuts':analyzed['cuts'],'warnings':analyzed['warnings'],
                    'pacing':analyzed['pacing'],'cutcheck':analyzed['cutcheck'],
                    'totals':cleanup.totals(analyzed['decisions'],analyzed['cuts'],assembly['duration'],doc['duration'])}
            doc['rough_report']=report
            if row.get('adoption'):write_inherited_candidate(ep,job,doc)
            problems=editdoc.validate(doc)
            if problems:raise ValueError('; '.join(problems))
            doc.setdefault('history',[]).append({'request_id':args['request_id'],'operation':'episode-rough','inputs':deps})
            p=editdoc.save_new_version(ep,doc,name=args['session'],locked=True,request_id=args['request_id']);doc=R.json_file(p)
            identity={'stage':'rough','name':args['session'],'version':int(editdoc._V.match(p.name)['n']),'sha256':sha256_file(p)}
            from ..compile import render
            mode=editdoc.effective_mode(doc);directory=safe_dir(studio.sp,studio.sp.work/'production'/'episode-renders'/row['id']/job['id']);directory.mkdir(parents=True,exist_ok=True)
            suffix='.m4a' if mode=='audio' else '.mp4';out=directory/('rough'+suffix);receipt=directory/'render-receipt.json'
            saved=read(studio.sp,receipt) if receipt.exists() else None
            if saved:
                out=safe(studio.sp,directory/saved['filename'])
                if saved['edit']!=identity or sha256_file(out)!=saved['sha256']:raise ValueError('Completed render receipt changed; output preserved')
                measured=saved['report']
            else:
                n=0
                while out.exists():n+=1;out=directory/(f'rough.recovery{n}'+suffix)
                measured=render(ep,doc,out,preset='audio_only' if mode=='audio' else 'preview',burn_captions=False,progress=render_progress(studio,job))
                write(studio.sp,receipt,{'edit':identity,'filename':out.name,'sha256':sha256_file(out),'report':measured})
            wave=compiled_waveform(studio.sp,out,directory/'browser',media.probe_file(out,studio.sp.root))
            if rough_inputs(studio,ep,row,args)!=deps:raise ValueError('Reviewed assembly changed during cleanup')
            if not deps['assembly_asset']:raise ValueError('Assembly render has no provenance asset')
            origin={'schema':'episode_rough_render.v1','episode':row['id'],'edit':identity,'dependencies':deps,'report':measured}
            progress_phase(studio,job,'Verifying exact inputs and registering output · cancellation available')
            asset=library.add_derivative(studio.sp,out,derived_from=[deps['assembly_asset']],origin=origin,by='episode-workbench')
            result=register_artifact(studio,row,out,kind='rough',edit=identity,details={'report':measured,'asset':asset['id'],'mode':mode,'waveform':read(studio.sp,wave)})
            return {'episode':row['id'],'edit':identity,'artifact':result,'report':report,'review':'needs review'}
        deps=base_inputs(studio,ep,row,args['session'])
        if row.get('adoption'):
            doc=editdoc.clone(R.json_file(editdoc.path_for(ep,'assembly',1,'main')))
            source_ids={s['id']:s for s in row['sources']};mapping={'sources':list(source_ids)}
        else:
            mapping=C.required(studio,ep,row,'mapping',args['session'])['payload'];bounds=C.required(studio,ep,row,'bounds',args['session'])['payload']
            source_ids={s['id']:s for s in row['sources']};ev=C.evidence(ep,row);probes={m['file']:m for m in ev['media']['files']}
            reference=source_ids[mapping['reference']]['file'];clock=ev['sync']['files'][reference]
            source={'file':reference,'kind':probes[reference]['kind'],'start_offset':clock.get('start_offset',0),'drift_ppm':clock.get('drift_ppm',0),'channel_map':probes[reference].get('channel_map'),**({'sync_map':clock['sync_map']} if clock.get('sync_map') else {})}
            if not mapping['cameras']:doc=assemble.build_audio(ep,source,bounds,session=args['session'])
            else:
                metadata={'cameras':{},'speakers':{},'audio_source':reference}
                names=C.required(studio,ep,row,'speakers','main')['payload']['names']
                for sid,cam in mapping['cameras'].items():
                    metadata['cameras'][sid]={'file':source_ids[sid]['file'],'role':cam['role'],'session':args['session']}
                    for speaker in cam['speakers']:metadata['speakers'][speaker]={'name':names[speaker],'camera':sid,'cameras':{args['session']:sid}}
                if not metadata['speakers']:
                    first=next(iter(metadata['cameras']))
                    metadata['speakers']={k:{'name':v,'camera':first,'cameras':{args['session']:first}} for k,v in names.items()}
                cut=cutplan.build_cutlist(ep,policy=cutplan.load_policy(),session=args['session'],metadata=metadata)
                doc=assemble._build_from_cut(ep,assemble.apply_trim(cut,bounds), (640,360),metadata=metadata)
            doc['assembly_context']={'reference_duration':probes[reference]['duration'],'transcript_duration':ev['transcript'].get('duration'),
                                     'camera_coverage':[{'source':sid,'start':editdoc.episode_time(ev['sync']['files'][source_ids[sid]['file']],0),
                                                         'end':editdoc.episode_time(ev['sync']['files'][source_ids[sid]['file']],probes[source_ids[sid]['file']]['duration'])} for sid in mapping['cameras']]}
        doc['episode_inputs']=deps;doc['registration']={'id':row['id'],'grant_revision':row['grant_revision']}
        doc['captions']={'burn':False}
        if row.get('adoption'):write_inherited_candidate(ep,job,doc)
        problems=editdoc.validate(doc)
        if problems:raise ValueError('; '.join(problems))
        doc.setdefault('history',[]).append({'request_id':args['request_id'],'operation':'episode-assembly','inputs':deps})
        p=editdoc.save_new_version(ep,doc,name=args['session'],locked=True,request_id=args['request_id']);doc=R.json_file(p);version=int(editdoc._V.match(p.name)['n'])
        identity={'stage':'assembly','name':args['session'],'version':version,'sha256':sha256_file(p)}
        from ..compile import render
        mode=editdoc.effective_mode(doc);directory=safe_dir(studio.sp,studio.sp.work/'production'/'episode-renders'/row['id']/job['id']);directory.mkdir(parents=True,exist_ok=True)
        suffix='.m4a' if mode=='audio' else '.mp4';out=directory/('assembly'+suffix);receipt=directory/'render-receipt.json'
        saved=read(studio.sp,receipt) if receipt.exists() else None
        if saved:
            out=safe(studio.sp,directory/saved['filename'])
            if saved['edit']!=identity or sha256_file(out)!=saved['sha256']:raise ValueError('Completed render receipt changed; output preserved')
            report=saved['report']
        else:
            # A crash after atomic media publication but before its receipt leaves
            # a historical output, never a file to overwrite on retry.
            n=0
            while out.exists():n+=1;out=directory/(f'assembly.recovery{n}'+suffix)
            report=render(ep,doc,out,preset='audio_only' if mode=='audio' else 'preview',burn_captions=False,progress=render_progress(studio,job))
            write(studio.sp,receipt,{'edit':identity,'filename':out.name,'sha256':sha256_file(out),'report':report})
        wave=compiled_waveform(studio.sp,out,directory/'browser',media.probe_file(out,studio.sp.root))
        waveform=read(studio.sp,wave)
        # Result attachment is another exact input boundary, after potentially long compilation.
        if base_inputs(studio,ep,row,args['session'])!=deps:raise ValueError('Evidence changed during render; immutable output remains as an unreviewed draft')
        R.get(studio.sp,row['id'],row['grant_revision'])
        parents=[]
        for index,sid in enumerate(mapping['sources'],1):
            s=source_ids[sid]
            progress_phase(studio,job,f'Registering source provenance {index}/{len(mapping["sources"])} · copying and verifying full recording · cancellation available')
            parent=library.add_file(studio.sp,ep.root/s['file'],source={'type':'import','ref':s['id']},by='episode-workbench',attested=False,
                license_entry={'source':'registered episode','url':'urn:ave:source:'+s['sha256'],'license':'unverified','license_url':'urn:ave:license:unverified','license_text':'Filesystem access does not establish usage rights. Human license review required.'})
            parents.append(parent['id'])
        if row.get('adoption'):parents.extend(a['id'] for a in row['adopted_assets'])
        progress_phase(studio,job,'Verifying exact inputs and registering assembly · cancellation available')
        origin={'schema':'episode_assembly_render.v1','episode':row['id'],'edit':identity,'dependencies':deps,'sources':[source_ids[s] for s in mapping['sources']],'report':report}
        asset=library.add_derivative(studio.sp,out,derived_from=parents,origin=origin,by='episode-workbench')
        result=register_artifact(studio,row,out,kind='assembly',edit=identity,details={'report':report,'asset':asset['id'],'mode':mode,'waveform':waveform})
        return {'episode':row['id'],'edit':identity,'artifact':result,'review':'needs review'}


def transcript_page(studio,eid,source_id,page=0,assembly=None):
    number(page,0,100000,'Transcript page',True)
    with R.resolve(studio,eid,verify_sources=False) as (ep,row):
        ev=C.evidence(ep,row);trans=ev['transcript'] or {}
        source=next((s for s in row['sources'] if s['id']==source_id),None)
        if not source:raise ValueError('Choose a registered transcript preview source')
        probe=next((p for p in (ev['media'] or {}).get('files',[]) if p['file']==source['file']),{})
        clock=(ev['sync'] or {}).get('files',{}).get(source['file'])
        if clock is None and source['file']!=trans.get('source'):return {'utterances':[],'pages':0,'reason':'Measure alignment before following this camera against the reference transcript.'}
        clock=clock or {'start_offset':0,'drift_ppm':0}
        from .video_workshop import source_words
        from ..transcribe import build_utterances
        sha=C.file_sha(ep,'transcript.json')
        projected=source_words({**clock,'source_id':source_id,'sha256':source['sha256'],'duration':probe.get('duration',0),
                                'transcript':{'clock':'episode','sha256':sha}},trans.get('words',[]))
        # Display only complete words actually present in this source. Keep
        # original episode bounds and word identity instead of relabelling an
        # out-of-coverage paragraph as if its opening occurred at source zero.
        covered=[w for w in projected if editdoc.src_time(clock,w['transcript_start'])>=0 and editdoc.src_time(clock,w['transcript_end'])<=probe.get('duration',0)]
        rows=[]
        for u in build_utterances(covered):
            words=[w for w in covered if u['start']<=w['start'] and w['end']<=u['end']]
            rows.append({**u,'source_start':u['start'],'source_end':u['end'],'episode_start':editdoc.episode_time(clock,u['start']),
                         'episode_end':editdoc.episode_time(clock,u['end']),'word_ids':[w['word_id'] for w in words],'words':words})
        page_size=R.policy()['transcript_page_utterances']
        return {'utterances':rows[page*page_size:(page+1)*page_size],'page':page,'pages':(len(rows)+page_size-1)//page_size,'clock':'source','transcript_sha256':sha}


def caption_words(studio,eid,session,page):
    """Paginated occurrence-aware words for the exact reviewed rough cut."""
    number(page,0,100000,'Caption page',True)
    with R.resolve(studio,eid) as (ep,row):
        event=C.required(studio,ep,row,'rough',session)
        return _caption_word_page(ep,row,event,page)


def _caption_word_page(ep,row,event,page):
    _,doc=exact(ep,event['payload'])
    doc['native_source_hashes']={s['file']:s['sha256'] for s in row['sources']};doc['caption_edit']={'style':'podcast','burn':False,'corrections':[]}
    from ..occurrences import mapped_words
    words=mapped_words(ep,doc);size=40
    return {'rough':event['payload'],'page':page,'pages':(len(words)+size-1)//size,'words':words[page*size:(page+1)*size]}
