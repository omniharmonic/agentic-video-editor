"""Exact release selections on the shared episode registry and workbench jobs."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import zipfile
from contextlib import contextmanager

from .. import captions, editdoc, library, media
from ..publish.safety import sha256_file
from . import episode_registry as R, episode_checkpoints as C, episode_workshop as E
from . import production, video_workshop as V, seal
from .workbench import safe, read, write

OPS=('episode-select','episode-short','episode-package','episode-handoff','episode-adopt')
IDENTITY=('stage','name','version','sha256')


def catalog(studio,eid,session):
    return _catalog(studio,eid,session)


def _catalog(studio,eid,session,*,reader=None):
    with R.resolve(studio,eid,verify_sources=False) as (ep,row):
        rows=[];shorts=[];packages=[];handoffs=[];labels=R.json_file(ep.meta_path).get('source_labels',{})
        artifacts=[]
        for path in sorted((studio.sp.work/'production'/'episode-artifacts').glob('artifact_*.json'),key=lambda p:p.stat().st_mtime_ns):
            rec=read(studio.sp,path)
            if rec['episode']==eid and rec['kind'] in ('select','short'):
                artifacts.append(reader.artifact(studio,eid,rec['id']) if reader else E.artifact(studio,eid,rec['id']))
        for artifact in artifacts:
            _,doc=E.exact(ep,artifact['edit']);selection=doc.get('release_selection',{})
            if selection.get('session')!=session:continue
            item={'duration':doc['duration'],'sources':[{'id':key,'label':labels.get(src['file'],Path(src['file']).name),'w':src.get('w'),'h':src.get('h')} for key,src in doc['sources'].items() if any(s['src']==key for s in doc['tracks']['video'])],'edit':artifact['edit'],'artifact':E.public_artifact(artifact),'title':selection.get('title'),'selection':doc.get('release_inputs',{}).get('selection')}
            if artifact['kind']=='select':
                event=C.latest(studio,eid,'keep',artifact['edit']['name']);item['keep']=(reader.status(studio,ep,row,event) if reader else C.status(studio,ep,row,event));rows.append(item)
            else:shorts.append(item)
        for path in sorted((studio.sp.work/'production'/'episode-release').glob('job_*/result.json'),key=lambda p:p.stat().st_mtime_ns):
            item=read_result(studio,path)
            if item.get('episode')!=eid:continue
            if item.get('kind')=='package':packages.append(item)
            elif item.get('kind')=='handoff':handoffs.append(item)
        from .. import opening,shorts as shorts_engine
        opening_config=opening.config(shorts_engine.load_config(ep))
        return {'clips':rows,'shorts':shorts,'packages':packages,'handoffs':handoffs,'short_show':opening.show_name(ep,opening_config),'short_title_limit':min(60,opening_config['title']['max_chars'])}


def write_result(studio,path,result):
    from .workbench import job_path
    job=read(studio.sp,job_path(studio.sp,path.parent.name))
    value=copy.deepcopy(result);value['_job']={k:copy.deepcopy(job[k]) for k in ('id','operation','args')}
    value['signature']=seal.mac('episode-release-result.v1',R.digest(value))
    write(studio.sp,path,value)
    return result


def read_result(studio,path):
    from .workbench import job_path
    value=read(studio.sp,path);signature=value.pop('signature',None)
    if not seal.verify_mac(signature,'episode-release-result.v1',R.digest(value)):raise ValueError('Release result receipt changed')
    job=read(studio.sp,job_path(studio.sp,path.parent.name));binding=value.pop('_job',None)
    if binding!={k:job[k] for k in ('id','operation','args')}:raise ValueError('Release result belongs to a different job')
    return value


def recover_committed(studio,job):
    """Read an owned, already committed result without reopening source grants."""
    folder=directory(studio,job);receipt=folder/'result.json'
    if not receipt.exists():return recover_transfer_intent(studio,job)
    result=read_result(studio,receipt)
    def verify(path,digest):
        path=safe(studio.sp,path)
        if sha256_file(path)!=digest:raise ValueError('Committed release output changed')
    if result.get('kind')=='package':
        package=V.safe_dir(studio.sp,folder/result['package_directory'])
        verify(folder/result['archive'],result['sha256']);verify(package/'manifest.json',result['manifest_sha256']);verify(package/'checksums.sha256',result['checksums_sha256'])
        for item in result['manifest']['files']:verify(package/item['file'],item['sha256'])
    elif result.get('kind')=='handoff':
        verify(folder/'plan.json',result['plan_sha256']) # a historical external receipt, never resend
        for item in result.get('companion_files',[]):verify(folder/item['file'],item['sha256'])
    elif result.get('artifact'):
        E.artifact(studio,result['episode'],result['artifact']['id'])
    elif job['operation']=='episode-adopt':
        row=R.records(studio.sp).get(result['episode'])
        if not row or row['ownership']!='managed_copy':raise ValueError('Committed managed registration is missing')
        root=V.safe_dir(studio.sp,studio.sp.root/row['root_hint'])
        verify(root/'edits'/'assembly.main.v1.json',row['adopted_assembly_sha256'])
        for item in row['adopted_files']:verify(root/item['file'],item['sha256'])
    elif job['operation']=='video-kept':
        ep,meta,path,doc,sha=V.exact(studio.sp,result['id'],result['version'],result['sha256'])
        for item in result['owned_files']:verify(studio.sp.root/item['file'],item['sha256'])
    elif job['operation']=='video-render':
        for asset in result['assets']:verify(studio.sp.root/asset['file'],asset['sha256'])
    else:raise ValueError('Unknown committed release result')
    return {**result,'recovery':{'committed_earlier':True,'current_eligibility':'not re-established','note':'Recovered an already committed result. Current review, source grants and licenses must be checked before further use; no side effects were repeated.'}}


def recover_transfer_intent(studio,job):
    """Project a torn transfer commit without registering or reopening inputs."""
    if job['operation'] not in ('episode-adopt','video-kept'):return None
    path=directory(studio,job)/'publication-intent.json'
    if not path.exists():return None
    intent=read_result(studio,path)
    if intent.get('kind')!='transfer-publication-intent':raise ValueError('Invalid transfer publication intent')
    expected_id=('episode_' if job['operation']=='episode-adopt' else 'project_')+R.digest({('adopt' if job['operation']=='episode-adopt' else 'kept'):job['args']['request_id']})[:24]
    target=V.safe_dir(studio.sp,studio.sp.work/'production'/'episodes'/expected_id)
    if intent['target']!=studio.sp.rel(target) or intent['id']!=expected_id:raise ValueError('Transfer intent target differs from its job')
    state='intent only; nothing published'
    if target.exists():
        stat=target.stat()
        if [stat.st_dev,stat.st_ino]!=intent['directory_identity']:raise ValueError('Published directory is foreign to this transfer')
        for item in intent['files']:
            file=safe(studio.sp,target/item['file'])
            if sha256_file(file)!=item['sha256']:raise ValueError('Incomplete published transfer bytes changed')
        state='files published; registration incomplete'
        if job['operation']=='episode-adopt':
            row=R.records(studio.sp).get(expected_id)
            if row:
                if row['ownership']!='managed_copy' or row['root_hint']!=intent['target'] or row['binding']['identity']!=intent['directory_identity']:raise ValueError('Managed registration ownership differs from published transfer')
                state='registration committed; result receipt incomplete'
        else:state='Media files published; result receipt incomplete'
    return {'kind':'incomplete-transfer','target':intent['target'],'recovery':{'incomplete':True,'committed_earlier':target.exists(),'state':state,'current_eligibility':'not re-established','note':'Preserved transfer files. Retry explicitly to finish with current source, keep and license eligibility; no registration was replayed by recovery.'}}


def _package_export(studio,directory_,result,path,expected_sha256):
    """Fresh export eligibility; historical playback grants no such authority."""
    from types import SimpleNamespace
    from .episode_worker_bridge import Parent
    from .workbench import job_path
    job=read(studio.sp,job_path(studio.sp,directory_.name));args=job['args']
    if job['operation']!='episode-package':raise ValueError('Package belongs to another operation')
    with R.resolve(studio,result['episode'],args['grant_revision'],_fresh_sources=True) as (ep,row):
        current=inputs(studio,ep,row,'episode-package',args)
        if current!=result.get('dependencies'):raise ValueError('Package dependencies changed; build a new release package')
        guard=SimpleNamespace(studio=studio,job=job,initial=current)
        before=Parent._episode_freshness(guard,ep,row)
    # R.resolve completes its full exit verification before this final byte
    # check and short current-state barrier. No historical job is re-executed.
    if sha256_file(path)!=expected_sha256:raise ValueError('Package export bytes changed')
    with R.resolve(studio,result['episode'],args['grant_revision'],verify_sources=False) as (ep,row):
        if before!=Parent._episode_freshness(guard,ep,row) or read_result(studio,directory_/'result.json')!=result:
            raise ValueError('Package eligibility changed while preparing export')


def downloadable(studio,path,*,index_policy=False,export=False):
    """Serve only a completed parent's exact package, never transfer staging."""
    base=studio.sp.work/'production'/'episode-release';path=safe(studio.sp,path)
    relative=path.relative_to(base)
    if len(relative.parts)<2:raise ValueError('Incomplete release output is not downloadable')
    directory_=base/relative.parts[0];result=read_result(studio,directory_/'result.json')
    if result.get('kind')!='package':raise ValueError('Use the registered episode artifact endpoint')
    R.get(studio.sp,result['episode'])
    allowed={result['archive']:result['sha256'],**{result['package_directory']+'/'+f['file']:f['sha256'] for f in result['manifest']['files']},
             result['package_directory']+'/manifest.json':result['manifest_sha256'],result['package_directory']+'/checksums.sha256':result['checksums_sha256']}
    name=Path(*relative.parts[1:]).as_posix()
    if name not in allowed or sha256_file(path)!=allowed[name]:raise ValueError('File is outside the completed release package')
    manifest_path=directory_/result['package_directory']/'manifest.json'
    if sha256_file(manifest_path)!=result['manifest_sha256'] or R.json_file(manifest_path)!=result['manifest']:raise ValueError('Release manifest changed')
    if name==result['archive'] or export:
        _package_export(studio,directory_,result,path,allowed[name])
    if index_policy:
        if name!=result['package_directory']+'/index.html':return None
        # The signed parent's manifest, rather than an HTML suffix, selects this
        # policy. Each subsequently requested member is checked by this route.
        return [V.url(studio.sp,directory_/member) for member in allowed if member.startswith(result['package_directory']+'/') and member!=name]
    return path


def directory(studio, job):
    return V.safe_dir(studio.sp,studio.sp.work/'production'/'episode-release'/job['id'])


def adoption_inputs(studio,args):
    from .episode_transfer import closure
    production.only(args,{'project','version','sha256','revision','title','request_id'})
    V.request(args.get('request_id'));production.text(args.get('title'),'Episode title',120,True)
    ep,meta,path,doc,sha=V.exact(studio.sp,args.get('project'),args.get('version'),args.get('sha256'))
    if meta['project_rev']!=args.get('revision') or editdoc.list_versions(ep,'short')[-1]!=args['version']:raise editdoc.EditConflict('Selected Media programme changed; reopen before adopting')
    if not doc['duration']:raise ValueError('Add a programme before adopting it as an episode')
    E.validate_programme_duration([doc['duration']])
    return {'schema':'episode_adoption_input.v1','identity':copy.deepcopy(args),'doc':doc,'closure':closure(studio,ep,doc),
            'source_episode':str(ep.root),'source_edit_sha256':sha,'review':'no editorial authority transferred'}


def native_kept_inputs(studio,args):
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is not None:return bridge.CLIENT.call('video-kept-inputs')
    production.only(args,{'source_id','clip_id','version','sha256','decision_sha256','request_id'})
    V.request(args.get('request_id'))
    from .episode_transfer import closure
    with R.resolve(studio,args.get('source_id')) as (ep,row):
        event=C.required(studio,ep,row,'keep',args.get('clip_id'));payload=event['payload']
        if payload['version']!=args.get('version') or payload['sha256']!=args.get('sha256') or R.digest(event)!=args.get('decision_sha256'):raise ValueError('Kept clip was changed or unkept')
        _,doc=E.exact(ep,payload)
        return {'schema':'episode_kept_input.v1','identity':copy.deepcopy(args),'doc':doc,'closure':closure(studio,ep,doc,row),
                'source_episode':str(ep.root),'episode':row['id'],'grant_revision':row['grant_revision'],
                'keep_event':event['checkpoint_id'],'keep_dependency_sha256':event['dependency_sha256'],'keep':payload,
                'title':doc['release_selection']['title'],'review':'no editorial authority transferred'}


def is_native_render(studio,args):
    """The owned project's origin chooses the verifier, never a client switch."""
    ep,meta,path,doc,sha=V.exact(studio.sp,args.get('project'),args.get('version'),args.get('sha256'))
    native=bool(meta.get('create',{}).get('native_kept')) or bool(doc.get('kept_clip',{}).get('native'))
    if native and not doc.get('kept_clip',{}).get('native'):raise ValueError('Native kept lineage was removed from this programme')
    return native


def kept_render_inputs(studio,args):
    from .episode_transfer import closure
    ep,meta,path,doc,sha=V.exact(studio.sp,args['project'],args['version'],args['sha256'])
    descriptor=doc.get('kept_clip') or {}
    if not descriptor.get('native'):raise ValueError('Native kept render has no verified kept lineage')
    captured={k:descriptor[k] for k in ('source_id','clip_id','version','sha256','decision_sha256','request_id')}
    keep=native_kept_inputs(studio,captured)
    used=closure(studio,ep,doc)
    parent_ids={s['parent'] for s in used['sources'].values()}
    parent_ids.update(a['id'] for a in used['assets']['closure'])
    used['assets']=E.licensed_asset_closure(studio,[{'id':aid,'sha256':library.get(studio.sp,aid)['sha256']} for aid in sorted(parent_ids)])
    return {'schema':'native_kept_render_input.v1','identity':copy.deepcopy(args),'doc':doc,'closure':used,
            'source_episode':str(ep.root),'project_revision':meta['project_rev'],'keep_origin':keep,
            'episode':keep['episode'],'grant_revision':keep['grant_revision'],'keep':keep['keep']}


@contextmanager
def native_attachment(studio,job):
    """Full verification, then a short current-ledger/stat barrier around CAS."""
    if job['operation']!='video-render' or not is_native_render(studio,job['args']):
        yield
        return
    doc=V.exact(studio.sp,job['args']['project'],job['args']['version'],job['args']['sha256'])[3]
    # Keep the native path lease alive while the caller reaches its own post CAS.
    with R.resolve(studio,doc['kept_clip']['source_id'],verify_sources=False) as (_,row):
        control_before=R.control(studio.sp).rows()
        def freshness():
            R.get(studio.sp,row['id'],row['grant_revision'])
            values=[]
            for item in deps['closure']['files'].values():
                st=R.regular(item['path']).stat();values.append((item['path'],st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns))
            return {'files':values,'control':R.control(studio.sp).rows()}
        with R.resolve(studio,row['id'],row['grant_revision'],_fresh_sources=True):
            deps=kept_render_inputs(studio,job['args'])
            if job['result'].get('native_keep')!=deps['doc']['kept_clip']:raise ValueError('Rendered kept lineage differs from this exact programme')
            assets=[a['asset'] for a in job['result']['assets']]
            if not library.coverage(studio.sp,assets)['ok']:raise ValueError('Rendered asset license/attestation is no longer valid')
            expected={'files':[(item['path'],*item['stat_identity']) for item in deps['closure']['files'].values()],'control':control_before}
            if freshness()!=expected:raise ValueError('Keep, source or license changed during attachment verification; finished artifact preserved')
        # Exit hashing is finished. The store enters this only after post CAS
        # and schema validation, then releases it before ordinary event append.
        from . import ledger
        @contextmanager
        def commit_guard():
            with ledger.ledger_lock(studio.sp):
                if freshness()!=expected:raise ValueError('Keep, source or license changed before attachment; finished artifact preserved')
                yield
        yield commit_guard


def transfer_doc(studio,target,job,deps):
    from .episode_transfer import rebase
    doc=rebase(studio,target,deps['doc'],deps['closure'])
    if job['operation']=='episode-adopt':
        doc['stage']='assembly';doc['adoption_inputs']={k:copy.deepcopy(deps[k]) for k in ('schema','identity','source_edit_sha256','review')}
        doc['session']='main';doc['registration']={'id':'episode_'+R.digest({'adopt':job['args']['request_id']})[:24],'grant_revision':1}
    else:
        doc['stage']='short';doc['independent_project']='project_'+R.digest({'kept':job['args']['request_id']})[:24]
        doc['kept_clip']={**copy.deepcopy(job['args']),'native':True,'title':deps['title'],'keep_event':deps['keep_event']}
    doc['history']=[{'request_id':job['args']['request_id'],'operation':job['operation'],'inputs':{k:v for k,v in deps.items() if k not in ('doc','closure')},'reason':'Exact editable transfer; fresh review required','confidence':1.0}]
    return doc


def transfer_meta(job,deps,doc):
    meta={'title':job['args'].get('title',deps.get('title','Kept clip')),'layout':'flat','speakers':{},'cameras':{},'stages':{}}
    if job['operation']=='episode-adopt':meta.update(registered_copy=doc['registration']['id'],adoption_inputs=doc['adoption_inputs'])
    else:meta.update(workbench_project=doc['independent_project'],project_rev=1,project_sources=copy.deepcopy(doc['sources']),create={'native_kept':job['args']})
    return meta


def execute_transfer(studio,job,deps):
    from ..episode import Episode
    from .episode_transfer import materialize
    from . import episode_worker_bridge as bridge
    directory_=directory(studio,job);target=Episode(directory_/'managed');target.create()
    materialize(studio,target,deps['closure']);doc=transfer_doc(studio,target,job,deps)
    meta=transfer_meta(job,deps,doc)
    write(studio.sp,target.meta_path,meta)
    # Exact snapshot remains pinned; storage rebasing changes no authored policy.
    V.pinned_caption(studio.sp,target,doc)
    write(studio.sp,directory_/'edit-candidate.json',doc)
    write(studio.sp,directory_/'candidate.json',{'operation':job['operation'],'args':job['args'],'dependencies':deps,'doc':doc})
    return bridge.CLIENT.call(job['operation']+'-register')


def selected_final(studio,ep,row,args):
    event=C.required(studio,ep,row,'final',args['session'])
    if args.get('final')!=event['payload']:raise ValueError('Choose the exact currently reviewed final')
    path,doc=E.exact(ep,args['final']);artifact=E.artifact(studio,row['id'],args['final']['artifact_id'])
    return event,doc,artifact


def final_snapshot(studio,ep,row,args):
    event,doc,artifact=selected_final(studio,ep,row,args)
    # Check every contributing dependent asset, including the final's programme
    # components, through the same native closure that established final review.
    from .episode_transfer import closure
    return {'final_event':event['checkpoint_id'],'final_dependency_sha256':event['dependency_sha256'],
            'final':copy.deepcopy(args['final']),'artifact':artifact,'mode':editdoc.effective_mode(doc),
            'source_fingerprint':row['source_fingerprint'],'closure':closure(studio,ep,doc,row)}


def selection_words(ep,doc):
    from ..occurrences import mapped_words
    return mapped_words(ep,doc,identities=True)


def candidates(studio,eid,session):
    with R.resolve(studio,eid) as (ep,row):
        event=C.required(studio,ep,row,'final',session)
        return _candidate_table(ep,event)


def _candidate_table(ep,event):
    _,doc=E.exact(ep,event['payload'])
    from .. import clips
    sentences=clips.Sentences(ep,doc,clips.load_rubric(ep),words=selection_words(ep,doc))
    for item in sentences.items:
        item['occurrences']=[{k:w[k] for k in ('word_id','occurrence_id','transcript_sha256','source_id','start','end')} for w in sentences.words[item['w0']:item['w1']]]
    return {'final':event['payload'],'clock':'final body output','duration':doc['duration'],
            'candidates':sentences.items,'note':'Local sentence boundaries are proposals; inspect preceding/following context before keeping.'}


def selection_evidence(ep,doc,args):
    a=V.number(args.get('start'),0,doc['duration'],'Clip start');b=V.number(args.get('end'),0,doc['duration'],'Clip end')
    if b<=a:raise ValueError('Clip end must follow start')
    words=selection_words(ep,doc)
    if any(w['start']<b and w['end']>a and (w['start']<a-1e-6 or w['end']>b+1e-6) for w in words):
        raise ValueError('Clip boundaries cut through a spoken word; choose a word gap')
    selected=[w for w in words if a<=w['start'] and w['end']<=b]
    if not selected:raise ValueError('Choose a spoken statement')
    from .. import clips
    from .. import segment
    rubric=clips.load_rubric(ep);sentences=clips.Sentences(ep,doc,rubric,words=words)
    first=next((s for s in sentences.items if s['start']<=selected[0]['start']+1e-6 and s['end']>=selected[0]['end']-1e-6),None)
    last=next((s for s in sentences.items if s['start']<=selected[-1]['start']+1e-6 and s['end']>=selected[-1]['end']-1e-6),None)
    complete=bool(first and last and first['features'].get('start_complete') and last['features'].get('end_complete') and a<=first['start']+.001 and b>=last['end']-.001)
    return {'start':a,'end':b,'text':' '.join(w['w'] for w in selected),
            'complete_statement':complete,'needs_context':bool(first and first['features'].get('needs_context')),
            'reason':'Sentence-boundary heuristic; a native keep decision confirms the selected statement in context.',
            'confidence':0.6,'before':' '.join(w['w'] for w in words if a-15<=w['start']<a),
            'after':' '.join(w['w'] for w in words if b<w['end']<=b+15),
            'words':selected}


def select_doc(ep,doc,args,deps):
    """Use the shared occurrence engine even for a legacy episode body."""
    from .episode_transfer import independent
    original=independent(ep,doc,deps['closure'],{'final':args['final']})
    a,b=args['start'],args['end'];cuts=[]
    if a>0:cuts.append({'o0':0,'o1':a})
    if b<original['duration']:cuts.append({'o0':b,'o1':original['duration']})
    result=V.recut_output(original,cuts)
    # Source transcript snapshots are materialized by the closure adapter before
    # this independent programme is compiled or opened in the Media editor.
    result.update(stage='select',release_inputs=copy.deepcopy(deps),release_selection=copy.deepcopy(args))
    result['captions']={'burn':False}
    return result


def short_doc(ep,base,args,deps,job_id,*,materialize=True):
    from .. import reframe,shorts,opening
    doc=copy.deepcopy(base)
    evidence=selection_evidence(ep,doc,args)
    if not evidence['complete_statement'] and (args['start']>0 or args['end']<base['duration']):raise ValueError('The recut is not a complete statement; restore the wider kept selection')
    cuts=[]
    if args['start']>0:cuts.append({'o0':0,'o1':args['start']})
    if args['end']<doc['duration']:cuts.append({'o0':args['end'],'o1':doc['duration']})
    doc=V.recut_output(doc,cuts);doc['stage']='short';doc['canvas']={'w':1080,'h':1920}
    cfg=copy.deepcopy(deps['configuration']['shorts'])
    for seg in doc['tracks']['video']:
        src=doc['sources'][seg['src']];r=(args.get('reframes') or {}).get(seg['src'],args.get('reframe'))
        seg['crop']=[int(r['x']*src['w'])//2*2,int(r['y']*src['h'])//2*2,int(r['w']*src['w'])//2*2,int(r['h']*src['h'])//2*2] if r else reframe.crop_for_face(None,src['w'],src['h'])
        seg['reason']='Creator crop' if r else 'Centred source crop; inspect speaker framing';seg['confidence']=1 if r else .3
    title=opening.apply(ep,doc,{'title':args['title'],'slug':'native-'+job_id,'id':args['keep']['name']},cfg,'title_overlay',args['title'],'crop',materialize=materialize)
    if title['mode']!='title_overlay':raise ValueError('Opening title needs revision: '+'; '.join(title['problems']))
    for clip in doc['clips']:
        source=doc['sources'][clip['source']]
        picture=next((v for v in doc['tracks']['video'] if v.get('occurrence_id')==clip['occurrence_id']),None)
        if picture:clip['crop']=copy.deepcopy(picture['crop']);clip.pop('zoom',None)
    doc['caption_edit']['style']='short';doc['caption_edit']['burn']=args['captions'];doc['release_short']={'args':copy.deepcopy(args),'inputs':deps,'needs_review':True,'completeness':evidence}
    return doc


def execute_clip(studio,ep,row,job,deps,outdir):
    from .. import compile as compiler, reframe, shorts, opening
    args=job['args'];op=job['operation']
    if (outdir/'candidate.json').exists():return
    if op=='episode-select':
        _,base=E.exact(ep,args['final']);doc=select_doc(ep,base,args,deps)
    else:
        _,base=E.exact(ep,args['keep']);doc=short_doc(ep,base,args,deps,job['id'])
    V.caption_snapshot(studio.sp,ep,doc,granted_episode=True);profile=V.pinned_caption(studio.sp,ep,doc,granted_episode=True)
    cues=captions.cues_from_edit(ep,doc,profile['style']);capdir=R.regular(ep.captions/'native-release'/job['id'],directory=True);capdir.mkdir(parents=True,exist_ok=True)
    srt=E.write_caption_once(ep,capdir/'programme.srt',lambda p:captions.write_srt(cues,p));ass=E.write_caption_once(ep,capdir/'programme.ass',lambda p:captions.write_ass(cues,profile['style'],tuple(doc['canvas'].values()),p))
    doc['captions']={'srt':ep.rel(srt),'ass':ep.rel(ass),'burn':bool(op=='episode-short' and args['captions'])}
    # Parent can inspect only this fixed candidate path during a B-roll gate.
    write(studio.sp,outdir/'edit-candidate.json',doc)
    ext='.m4a' if editdoc.effective_mode(doc)=='audio' else '.mp4';output=outdir/('preview'+ext);n=0
    while output.exists():n+=1;output=outdir/(f'preview-recovery{n}'+ext)
    report=compiler.render(ep,doc,output,preset='audio_only' if ext=='.m4a' else 'master',burn_captions=doc['captions']['burn'])
    E.copy_episode_output(studio,ep,srt,output.with_suffix('.srt'))
    write(studio.sp,outdir/'candidate.json',{'operation':op,'args':args,'dependencies':deps,'doc':doc,'filename':output.name,
          'sha256':sha256_file(output),'report':report,'caption':{'filename':output.with_suffix('.srt').name,'sha256':sha256_file(output.with_suffix('.srt'))}})


def validate(studio,op,args):
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is not None:return bridge.CLIENT.call(op+'-inputs')
    common={'episode','grant_revision','input_sha256','session','request_id'}
    allowed={'episode-select':{'final','start','end','title'},
             'episode-short':{'keep','title','show','start','end','layout','captions','reframe','reframes'},
             'episode-package':{'final','chapters','formats','originals','copy'},
             'episode-handoff':{'final','name','subtitles','layers','proxies','sources'},
             'episode-adopt':{'project','version','sha256','revision','title','request_id'}}
    if op not in allowed:raise ValueError('Unknown release operation')
    production.only(args,allowed[op] if op=='episode-adopt' else common|allowed[op]);V.request(args.get('request_id'))
    if op=='episode-adopt':return adoption_inputs(studio,args)
    V.number(args.get('grant_revision'),1,1000000,'Grant revision',True)
    if not isinstance(args.get('session'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',args['session']):raise ValueError('Choose a registered session')
    with R.resolve(studio,args.get('episode'),args['grant_revision']) as (ep,row):
        if E.state_hash(ep,row,op,args['session'])!=args.get('input_sha256'):raise ValueError('Episode inputs changed; refresh the release')
        return inputs(studio,ep,row,op,args)


def inputs(studio,ep,row,op,args):
    if op=='episode-short':return short_inputs(studio,ep,row,args)
    deps=final_snapshot(studio,ep,row,args);_,doc=E.exact(ep,args['final'])
    if op=='episode-select':
        production.text(args.get('title'),'Clip title',120,True)
        deps['selection']=selection_evidence(ep,doc,args)
    elif op=='episode-package':
        if type(args.get('originals')) is not bool:raise ValueError('Choose whether to include full-quality originals')
        production.text(args.get('copy',''),'Release copy',20000)
        from ..package import exact as package
        binding={'episode':row['id'],'edit':args['final'],'render_sha256':deps['artifact']['sha256']}
        words=[w for c in deps['artifact']['details']['caption']['cues'] for w in c.get('words',[])]
        deps['chapters']=package.bound_chapters(args.get('chapters'),deps['artifact']['details']['report']['duration'],binding,words)
        if not isinstance(args.get('formats'),list) or not args['formats'] or set(args['formats'])-{'mp3','aac'}:raise ValueError('Choose podcast audio formats')
    elif op=='episode-handoff':
        production.text(args.get('name'),'Resolve timeline name',120,True)
        for key in ('subtitles','layers','proxies'):
            if type(args.get(key)) is not bool:raise ValueError('Choose Resolve '+key)
        if type(args.get('sources',False)) is not bool:raise ValueError('Choose source-body reference')
        from . import episode_resolve
        deps['handoff']=episode_resolve.inputs(ep,doc,args,deps['artifact'],studio)
    return deps


def keep_inputs(studio,ep,row,payload):
    production.only(payload,{'stage','name','version','sha256','artifact_id','complete_statement','context_reviewed'})
    if payload.get('stage')!='select' or type(payload.get('complete_statement')) is not bool or payload.get('complete_statement') is not True or payload.get('context_reviewed') is not True:
        raise ValueError('Keep requires a complete statement and explicit context review')
    path,doc=E.exact(ep,payload);args=doc['release_selection'];deps=inputs(studio,ep,row,'episode-select',args)
    if deps!=doc['release_inputs']:raise ValueError('The source final or selected statement changed')
    art=E.artifact(studio,row['id'],payload['artifact_id'])
    if art['kind']!='select' or art['edit']!={k:payload[k] for k in IDENTITY}:raise ValueError('Review the exact selected clip render')
    return {'select':{k:payload[k] for k in IDENTITY},'final_dependencies':deps,
            'preview_sha256':art['sha256'],'artifact_id':art['id']}


def short_configuration(ep,show=None):
    from .. import shorts,opening,motion
    from ..package import collect
    cfg=shorts.load_config(ep);oc=opening.config(cfg);oc['show']=opening.show_name(ep,oc) if show is None else show;cfg['opening']=oc
    production.text(oc['show'],'Short show label',40)
    style=captions.get_style('short');font=captions.resolve_font(style['fonts'],bold=bool(style.get('bold')))
    if not font.get('file') or not font.get('bundled'):raise ValueError('Short captions require the bundled font')
    paths=[motion.KIT/'templates'/'short-title'/'template.html',motion.KIT/'templates'/'short-title'/'schema.json',motion.KIT/'brand'/'tokens.json',motion.KIT/'lib'/'timeline.js',motion.KIT/'lib'/'base.css',Path(font['file'])]
    # Motion templates and fonts remain captured renderer inputs, not approval.
    return {'shorts':cfg,'package_policy':collect.load_policy(ep),'caption_style':style,'resources':[{'path':str(p),'sha256':sha256_file(p)} for p in paths]}


def short_inputs(studio,ep,row,args):
    keep=args.get('keep')
    if not isinstance(keep,dict):raise ValueError('Choose an exact human-kept selection')
    event=C.required(studio,ep,row,'keep',keep.get('name'))
    if keep!=event['payload']:raise ValueError('Selection was changed or unkept')
    _,doc=E.exact(ep,keep)
    if editdoc.effective_mode(doc)=='audio':raise ValueError('Audio selection has no picture; open it in Media for audio delivery')
    production.text(args.get('title'),'Short title',60,True)
    if 'show' in args:production.text(args['show'],'Short show label',40)
    from .. import opening
    configuration=short_configuration(ep,args.get('show'))
    title=opening.resolve_title(ep,{'title':args['title']},opening.config(configuration['shorts']),args['title'])
    if title['problems']:raise ValueError('Opening title needs revision: '+'; '.join(title['problems']))
    if args.get('layout')!='crop':raise ValueError('Choose the supported portrait crop layout')
    if type(args.get('captions')) is not bool:raise ValueError('Choose caption burn')
    a=V.number(args.get('start'),0,doc['duration'],'Recut start');b=V.number(args.get('end'),0,doc['duration'],'Recut end')
    if b<=a:raise ValueError('Recut end must follow start')
    reframes=args.get('reframes') or {}
    if not isinstance(reframes,dict) or set(reframes)-{s['src'] for s in doc['tracks']['video']}:raise ValueError('Choose crops only for this selection’s camera sources')
    for r in [*reframes.values(),*([args['reframe']] if args.get('reframe') is not None else [])]:
        production.only(r,{'x','y','w','h'})
        for k in ('x','y','w','h'):V.number(r.get(k),0,1,'Reframe '+k)
        if r['w']<=0 or r['h']<=0 or r['x']+r['w']>1 or r['y']+r['h']>1:raise ValueError('Reframe exceeds source')
    return {'keep_event':event['checkpoint_id'],'keep_dependencies':event['dependency_sha256'],'keep':keep,
            'select_dependencies':doc['release_inputs'],'configuration':configuration}


def package_spec(studio,ep,row,args,deps):
    artifact=deps['artifact'];path=safe(studio.sp,studio.sp.root/artifact['file']);cap=artifact['details']['caption'];report=artifact['details']['report']
    probe=media.probe_file(path,studio.sp.root)
    return {'schema':'package_input.v1','binding':{'episode':row['id'],'session':args['session'],'edit':args['final'],
               'checkpoint':deps['final_event'],'dependencies':deps['final_dependency_sha256']},
            'title':row['label'],'mode':deps['mode'],'duration':report['duration'],
            'render':{'id':artifact['id'],'path':str(path),'sha256':artifact['sha256'],'quality':{'video':probe['video'],'audio':probe['audio'],'preset':report.get('preset')}},
            'caption':{'path':str(path.parent/cap['filename']),'sha256':cap['sha256']},'chapters':deps['chapters'],
            'credits':report.get('credits',[]),'copy':args.get('copy',''),
            'sources':[{'path':str(ep.root/s['file']),'sha256':s['sha256'],'id':s['id']} for s in row['sources']]}


def execute(studio,job):
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is None:raise ValueError('Release work requires its live parent verification bridge')
    op=job['operation'];args=job['args'];deps=bridge.CLIENT.call(op+'-inputs');outdir=directory(studio,job);outdir.mkdir(parents=True,exist_ok=True)
    committed=bridge.CLIENT.call(op+'-status')
    if committed:return committed
    if op in ('episode-adopt','video-kept'):return execute_transfer(studio,job,deps)
    with R.resolve(studio,args['episode'],args['grant_revision'],verify_sources=False) as (ep,row):
        if op=='episode-package':
            from ..package import exact as package,collect
            spec=package_spec(studio,ep,row,args,deps);plan=package.plan(spec,formats=args['formats'],include_originals=args['originals'])
            candidate=outdir/'package'
            if not (outdir/'candidate.json').exists():
                attempt=0
                while candidate.exists():attempt+=1;candidate=outdir/f'package-recovery{attempt}'
                manifest=package.build(plan,candidate,policy=collect.load_policy(ep))
                archive=outdir/(candidate.name+'.zip')
                with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as zip:
                    for path in sorted(candidate.rglob('*')):
                        if path.is_file():zip.write(path,path.relative_to(candidate))
                write(studio.sp,outdir/'candidate.json',{'operation':op,'args':args,'dependencies':deps,'directory':candidate.name,'archive':archive.name,'archive_sha256':sha256_file(archive),'manifest':manifest})
        elif op=='episode-handoff':
            from .. import handoff,config
            from . import episode_resolve
            _,doc=E.exact(ep,args['final'])
            python,mcp=config.resolve_mcp_python(),config.resolve_mcp_dir()
            if not python or not mcp:raise ValueError('Resolve bridge runtime unavailable; start the installed bridge in Resolve')
            plans=episode_resolve.plans(ep,doc,args,deps,outdir,studio)
            def send(kind,plan):
                destination=outdir/(kind+'-plan.json');write(studio.sp,destination,plan)
                proc=subprocess.run([str(python),str(config.REPO_ROOT/'scripts/resolve_handoff.py'),str(destination),'--mcp-src',str(mcp/'src')],capture_output=True,text=True,timeout=600)
                return handoff.checked_report(proc.stdout,returncode=proc.returncode,expected_name=plan['name'])
            # Persist the successful master immediately. A companion failure or
            # retry cannot erase that external side effect or create it twice.
            master_receipt=outdir/'resolve-master-report.json'
            if master_receipt.exists():report=read(studio.sp,master_receipt);handoff.checked_report(json.dumps(report),returncode=0,expected_name=args['name'])
            else:
                report=send('master',plans['master']);write(studio.sp,master_receipt,report)
            companion={'status':'unsupported' if plans['unsupported'] else 'not requested','reason':plans['unsupported'],'limitations':plans['limitations']}
            if plans['companion']:
                companion_receipt=outdir/'resolve-companion-report.json'
                try:
                    if companion_receipt.exists():other=read(studio.sp,companion_receipt);handoff.checked_report(json.dumps(other),returncode=0,expected_name=plans['companion']['name'])
                    else:
                        plans=episode_resolve.plans(ep,doc,args,deps,outdir,studio,materialize=True)
                        other=send('companion',plans['companion']);write(studio.sp,companion_receipt,other)
                    companion={'status':'created reference timeline','report':other,'limitations':plans['limitations']}
                except (ValueError,OSError,subprocess.SubprocessError) as exc:
                    companion={'status':'failed; master preserved','reason':str(exc),'limitations':plans['limitations']}
            write(studio.sp,outdir/'plan.json',plans)
            files=episode_resolve.companion_files(doc,args,outdir,required=companion['status']=='created reference timeline') if plans['companion'] else []
            write(studio.sp,outdir/'candidate.json',{'operation':op,'args':args,'dependencies':deps,'plan':plans,'plan_sha256':sha256_file(outdir/'plan.json'),'report':report,'companion':companion,'companion_files':files})
        else:execute_clip(studio,ep,row,job,deps,outdir)
    return bridge.CLIENT.call(op+'-register')
