"""Exact editorial decisions on the existing verified native control ledger.

Prepare/view are authoring operations. Confirm is called only by the native
controller after a fresh contextual authentication and PIN; no HTTP route calls
it. Signatures establish sanctioned writes, not proof that footage was watched
or protection against a fully compromised same-user process (see seal.py).
"""
from __future__ import annotations
import copy
import secrets
import time
from .. import editdoc
from ..publish.safety import sha256_file
from . import episode_registry as R, ledger, production
from .clock import iso_utc

KINDS=('speakers','mapping','bounds','assembly','rough','sequence','final','keep')
EXPENSIVE_KINDS=('assembly','rough','sequence','final','keep')


def events(studio):
    return [r for r in R.control(studio.sp).rows() if r.get('schema')=='episode_checkpoint.v1' and r.get('sig')]


def latest(studio,eid,kind,session):
    return next((r for r in reversed(events(studio)) if r['episode']==eid and r['kind']==kind and r['session']==session),None)


def evidence(ep,row):
    def artifact(name):
        p=ep.artifact(name)
        return R.json_file(p) if p.exists() else None
    result={'media':artifact('media.json'),'transcript':artifact('transcript.json'),'sync':artifact('sync.json')}
    allowed={s['file'] for s in row['sources']}
    if result['media'] and any(m.get('file') not in allowed for m in result['media'].get('files',[])):raise ValueError('Media evidence references an unregistered source')
    if result['transcript'] and result['transcript'].get('source') not in allowed:raise ValueError('Transcript references an unregistered source')
    if result['sync'] and (result['sync'].get('reference') not in allowed or set(result['sync'].get('files',{}))-allowed):raise ValueError('Sync evidence references an unregistered source')
    return result


def file_sha(ep,name):
    p=R.regular(ep.artifact(name))
    if not p.exists(): raise ValueError('Generate '+name+' before this decision')
    return sha256_file(p)


def required(studio,ep,row,kind,session):
    event=latest(studio,row['id'],kind,session)
    state=status(studio,ep,row,event)
    if state['state']!='reviewed exact': raise ValueError(f'{kind} {session}: '+state['reason'])
    return event


def inputs(studio,ep,row,kind,session,payload):
    if kind=='keep':
        from .episode_release import keep_inputs
        return keep_inputs(studio,ep,row,payload)
    if row.get('adoption') and kind in ('assembly','rough','sequence','final'):
        from .episode_workshop import adoption_base
        base=adoption_base(ep,row,session)
    else:
        ev=evidence(ep,row);media=ev['media'] or {};trans=ev['transcript'] or {}
        source_by_file={s['file']:s for s in row['sources']}
        reference=trans.get('source')
        if reference not in source_by_file: raise ValueError('Transcript must reference an exact registered recording')
        R.assert_sources(ep,row,[source_by_file[reference]['id']])
        selected=[m for m in media.get('files',[]) if m.get('file')==reference]
        base={'root_identity':row['binding']['identity'],'source_reference':source_by_file[reference],
              'transcript_sha256':file_sha(ep,'transcript.json'),'channels':selected}
        if kind=='speakers':return base
        speakers=required(studio,ep,row,'speakers','main');base['speakers_event']=speakers['checkpoint_id']
        if kind=='mapping':
            wanted=set(payload['sources']);chosen=[s for s in row['sources'] if s['id'] in wanted]
            R.assert_sources(ep,row,wanted)
            sync=ev['sync'] or {}
            if sync.get('reference')!=reference:raise ValueError('Sync and transcript must share the same reference recording')
            base.update(sources=chosen,media=[m for m in media.get('files',[]) if m['file'] in {s['file'] for s in chosen}],
                        sync={s['file']:sync.get('files',{}).get(s['file']) for s in chosen})
            if any(v is None for v in base['sync'].values()): raise ValueError('Every selected source requires measured sync or an explicit reference clock')
            return base
        if session=='combined' and kind in ('assembly','rough','sequence','final'):
            from .episode_workshop import exact,combined_inputs
            combined_payload=payload if kind=='assembly' else required(studio,ep,row,'assembly','combined')['payload']
            _,combined_doc=exact(ep,combined_payload)
            identities=[r['identity'] for r in combined_doc.get('combined_constituents',[])]
            base=combined_inputs(studio,ep,row,{'session':'combined','constituents':identities})
        else:
            mapping=required(studio,ep,row,'mapping',session);base['mapping_event']=mapping['checkpoint_id']
            if kind=='bounds':return base
            bounds=required(studio,ep,row,'bounds',session);base['bounds_event']=bounds['checkpoint_id']
    if kind=='assembly':
        from .episode_workshop import exact,artifact
        path,doc=exact(ep,payload)
        if doc.get('episode_inputs')!=base: raise ValueError('Assembly inputs changed; rebuild from the current confirmed evidence')
        result=artifact(studio,row['id'],payload['artifact_id'])
        if result['kind']!=kind or result['edit']!={k:payload[k] for k in ('stage','name','version','sha256')}:raise ValueError('Select the render of this exact edit version')
        base.update(edit={k:payload[k] for k in ('stage','name','version','sha256')},preview_sha256=result['sha256'],artifact_id=result['id'])
        return base
    if kind=='rough':
        from .episode_workshop import exact,artifact,rough_inputs
        path,doc=exact(ep,payload)
        upstream=rough_inputs(studio,ep,row,{'session':session,'assembly':doc.get('rough_inputs',{}).get('assembly')})
        if doc.get('rough_inputs')!=upstream:raise ValueError('Rough inputs changed; rebuild from the reviewed assembly')
        result=artifact(studio,row['id'],payload['artifact_id'])
        if result['kind']!=kind or result['edit']!={k:payload[k] for k in ('stage','name','version','sha256')}:raise ValueError('Select the render of this exact edit version')
        base.update(rough_inputs=upstream,edit={k:payload[k] for k in ('stage','name','version','sha256')},preview_sha256=result['sha256'],artifact_id=result['id'])
        return base
    if kind=='sequence':
        from .episode_workshop import sequence_record,artifact
        rec=sequence_record(studio,ep,row,payload['job_id'],payload['plan_sha256']);args=rec['args']
        if args['session']!=session:raise ValueError('Sequence belongs to another session')
        enabled={tag for tag in ('opening','closing') if args[tag]['enabled']}
        if set(payload['artifacts'])!=enabled:raise ValueError('Review each enabled sequence preview')
        previews={}
        for tag,aid in payload['artifacts'].items():
            result=artifact(studio,row['id'],aid)
            if result['kind']!='sequence-'+tag or result['details'].get('plan_sha256')!=payload['plan_sha256'] or result['details'].get('job_id')!=payload['job_id']:raise ValueError('Sequence preview differs from the exact plan')
            previews[tag]=result['sha256']
        base.update(sequence_dependencies=rec['snapshot']['dependencies'],plan_sha256=payload['plan_sha256'],previews=previews,preview_sha256=R.digest(previews))
        return base
    if kind=='final':
        from .episode_workshop import exact,artifact,final_inputs,native_final_license_gate,validate_final_candidate
        path,doc=exact(ep,payload);deps=final_inputs(studio,ep,row,doc['final_spec'])
        if doc.get('final_inputs')!=deps:raise ValueError('Final dependencies changed; render a new final')
        validate_final_candidate(ep,doc,doc['final_spec'],deps)
        native_final_license_gate(studio,ep,doc,doc['final_spec'],deps)
        from .video_workshop import pinned_caption
        profile=pinned_caption(studio.sp,ep,doc,granted_episode=True)
        if profile['style']!=deps['caption_profile']['style'] or profile['font']['sha256']!=deps['caption_profile']['font_sha256']:raise ValueError('Final caption snapshot changed')
        result=artifact(studio,row['id'],payload['artifact_id'])
        if result['kind']!='final' or result['edit']!={k:payload[k] for k in ('stage','name','version','sha256')}:raise ValueError('Select the render of this exact final edit')
        base.update(final_dependencies=deps,edit={k:payload[k] for k in ('stage','name','version','sha256')},preview_sha256=result['sha256'],
                    caption_sha256=result['details']['caption']['sha256'],asr_sha256=result['details']['asr']['sha256'],artifact_id=result['id'])
        return base
    raise ValueError('Unsupported checkpoint kind')


def payload_for(ep,row,kind,session,payload):
    from .video_workshop import number
    if kind=='keep':
        if payload.get('name')!=session:raise ValueError('Keep scope must match the exact clip name')
        production.only(payload,{'stage','name','version','sha256','artifact_id','complete_statement','context_reviewed'})
        if payload.get('stage')!='select':raise ValueError('Keep requires an exact select')
        number(payload.get('version'),1,1000000,'Select version',True)
        return copy.deepcopy(payload)
    production.only(payload,{'names'} if kind=='speakers' else {'reference','sources','cameras'} if kind=='mapping' else {'in','out'} if kind=='bounds' else {'job_id','plan_sha256','artifacts'} if kind=='sequence' else {'stage','name','version','sha256','artifact_id'})
    ev=evidence(ep,row);trans=ev['transcript'] or {};ids={s['id']:s for s in row['sources']}
    if not isinstance(session,str) or not __import__('re').fullmatch(r'[A-Za-z0-9_-]{1,40}',session):raise ValueError('Choose a plain session identity')
    if kind=='speakers':
        if session!='main':raise ValueError('Speaker names are episode-scoped')
        names=payload.get('names');known={w.get('speaker') for w in trans.get('words',[]) if w.get('speaker')}
        if not isinstance(names,dict) or set(names)!=known or not known:raise ValueError('Name every transcribed speaker ID')
        return {'names':{k:production.text(v,'Speaker name',100,True) for k,v in names.items()}}
    if kind=='mapping':
        sources=payload.get('sources');cameras=payload.get('cameras')
        if not isinstance(sources,list) or not sources or len(sources)>200 or len(set(sources))!=len(sources) or any(s not in ids for s in sources):raise ValueError('Choose registered session sources')
        if payload.get('reference') not in sources or ids[payload['reference']]['file']!=trans.get('source'):raise ValueError('Choose the transcribed reference as the dialogue source')
        if not isinstance(cameras,dict):raise ValueError('Camera mapping must be an object')
        probes={m['file']:m for m in (ev['media'] or {}).get('files',[])}
        videos={s for s in sources if probes.get(ids[s]['file'],{}).get('video')}
        if set(cameras)!=videos:raise ValueError('Map every selected camera; audio sources need no fictional cameras')
        known={w.get('speaker') for w in trans.get('words',[]) if w.get('speaker')}
        for sid,cam in cameras.items():
            production.only(cam,{'role','speakers'})
            if cam.get('role') not in ('speaker','wide') or not isinstance(cam.get('speakers'),list) or any(s not in known for s in cam['speakers']):raise ValueError('Choose a camera role and known speaker IDs')
            if cam['role']=='speaker' and not cam['speakers']:raise ValueError('Choose the visible speaker for each camera, or explicitly choose a wide view')
            if cam['role']=='wide' and cam['speakers']:raise ValueError('A wide view does not claim a speaker identity')
        return copy.deepcopy(payload)
    if kind=='bounds':
        a=number(payload.get('in'),0,R.policy()['max_session_seconds'],'Start');b=number(payload.get('out'),0,R.policy()['max_session_seconds'],'End')
        if b<=a:raise ValueError('End must follow start')
        reference=next((m for m in (ev['media'] or {}).get('files',[]) if m['file']==trans.get('source')),None)
        if not reference or b>reference['duration']+1e-6:raise ValueError('Bounds exceed the actual reference recording')
        return {'in':a,'out':b}
    if kind=='sequence':
        import re
        if not isinstance(payload.get('job_id'),str) or not re.fullmatch(r'job_[a-f0-9]{24}',payload['job_id']) or not isinstance(payload.get('plan_sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',payload['plan_sha256']) or not isinstance(payload.get('artifacts'),dict):raise ValueError('Choose an exact sequence plan and previews')
        return copy.deepcopy(payload)
    if payload.get('stage')!=kind or payload.get('name')!=session:raise ValueError('Review the selected session and exact stage')
    number(payload.get('version'),1,1000000,'Edit version',True)
    return copy.deepcopy(payload)


def status(studio,ep,row,event):
    if not event:return {'state':'needs review','reason':'No exact native confirmation'}
    if event.get('revoked'):return {'state':'stale','reason':'Decision was revoked','event':event['checkpoint_id']}
    try:
        if event.get('grant_revision')!=row['grant_revision']:raise ValueError('Episode access grant changed; review the current registration')
        now=inputs(studio,ep,row,event['kind'],event['session'],event['payload'])
        if R.digest(now)!=event['dependency_sha256']:raise ValueError('Relevant evidence or upstream confirmation changed')
    except (ValueError,OSError,KeyError) as exc:return {'state':'stale','reason':str(exc),'event':event['checkpoint_id']}
    return {'state':'reviewed exact','reason':('Simulated native fixture confirmation' if event.get('simulation') else 'Native confirmation')+' matches these exact inputs','simulation':bool(event.get('simulation')),'event':event['checkpoint_id'],'payload':event['payload']}


def request_inputs(studio,ep,row,kind,session,payload,decision):
    if decision=='revoke':
        event=latest(studio,row['id'],kind,session)
        if not event or event.get('revoked') or event['payload']!=payload:raise ValueError('Select the current exact confirmation to revoke')
        return {'target_event':event['checkpoint_id'],'root_identity':row['binding']['identity']}
    return inputs(studio,ep,row,kind,session,payload)


class Checkpoints:
    def __init__(self,app):self.app=app;self.prepared={}
    def prepare(self,body):
        production.only(body,{'episode','grant_revision','kind','session','payload','decision'})
        if body.get('kind') not in KINDS:raise ValueError('Choose an available episode checkpoint')
        with R.resolve(self.app.studio,body.get('episode'),body.get('grant_revision'),verify_sources=body['kind'] in EXPENSIVE_KINDS,_fresh_sources=True) as (ep,row):
            payload=payload_for(ep,row,body['kind'],body.get('session'),body.get('payload'))
            decision=body.get('decision','confirm')
            if decision not in ('confirm','revoke'):raise ValueError('Choose confirm or revoke')
            deps=request_inputs(self.app.studio,ep,row,body['kind'],body['session'],payload,decision)
            token=secrets.token_hex(24);request={**copy.deepcopy(body),'decision':decision,'payload':payload,'dependencies':deps,'dependency_sha256':R.digest(deps),'payload_sha256':R.digest(payload),'label':row['label']}
        self.prepared={k:v for k,v in self.prepared.items() if time.monotonic()-v['created']<900}
        from .workbench import write
        write(self.app.studio.sp,self.app.studio.sp.work/'production'/'episode-proposals'/(token+'.json'),request)
        self.prepared[token]={'request':request,'created':time.monotonic(),'viewed':None}
        return {'token':token,'request':request}
    def view(self,body):
        production.only(body,{'token','payload_sha256','preview_sha256'})
        item=self._item(body.get('token'));request=item['request']
        if body.get('payload_sha256')!=request['payload_sha256'] or body.get('preview_sha256')!=request['dependencies'].get('preview_sha256'):raise ValueError('View does not match the exact prepared decision')
        item['viewed']=time.monotonic();return {'viewed':True}
    def _item(self,token):
        item=self.prepared.get(token)
        if not item or time.monotonic()-item['created']>900:raise ValueError('Review request expired; prepare it again')
        return item
    def describe(self,token):return copy.deepcopy(self._item(token)['request'])
    def confirm_native(self,token,pin,*,cancelled=None):
        from .._verification_cancel import scope
        with scope(cancelled):
            return self._confirm_native(token,pin,cancelled=cancelled)

    def _confirm_native(self,token,pin,*,cancelled):
        def check_cancelled():
            if cancelled is not None and cancelled():raise ValueError('Episode confirmation cancelled; no checkpoint was recorded')
        check_cancelled()
        authority=self.app.native_authority
        if authority is None:raise ValueError('Exact confirmation requires the native review sheet')
        authority.require();self.app.pin.check(pin);item=self._item(token)
        if item['viewed'] is None or time.monotonic()-item['viewed']<2:raise ValueError('Inspect the exact prepared evidence and playback before confirming')
        req=item['request'];studio=self.app.studio
        with R.resolve(studio,req['episode'],req['grant_revision'],verify_sources=False) as (ep,row), editdoc.mutation_lock(ep,cancelled=cancelled):
            from .episode_worker_bridge import dependency_freshness
            # Capture control and filesystem identities before any expensive
            # source/asset checks. The outer lease remains open through commit.
            operation='episode-final' if req['kind']=='keep' else 'episode-'+req['kind']
            # Small decisions retain their narrow source checks. Their evidence
            # consists of the same media/transcript/sync inputs as assembly.
            if req['kind'] not in EXPENSIVE_KINDS:operation='episode-assembly'
            args={'session':req['session'],**req['payload']}
            freshness=lambda:dependency_freshness(studio,ep,row,req['dependencies'],operation,args)
            baseline=freshness()
            from .episode_view import observation
            with R.resolve(studio,row['id'],row['grant_revision'],verify_sources=req['kind'] in EXPENSIVE_KINDS,_fresh_sources=True):
                deps=request_inputs(studio,ep,row,req['kind'],req['session'],req['payload'],req['decision'])
                if R.digest(deps)!=req['dependency_sha256']:raise ValueError('Review target changed during authentication; inspect the new evidence')
                view_observation=observation(studio,ep,row,deps)
            # Exit verification has completed before the short ledger boundary.
            check_cancelled()
            authority.require();R.regular(ep.meta_path.with_suffix('.json.tmp'))
            with ledger.ledger_lock(studio.sp):
                R.get(studio.sp,row['id'],row['grant_revision'])
                authority.require()
                if self._item(token) is not item or freshness()!=baseline:raise ValueError('Review source, checkpoint or license changed before confirmation')
                previous=latest(studio,row['id'],req['kind'],req['session'])
                ev={'schema':'episode_checkpoint.v1','checkpoint_id':'check_'+secrets.token_hex(16),'episode':row['id'],'root_identity':row['binding']['identity'],
                    'grant_revision':row['grant_revision'],'kind':req['kind'],'session':req['session'],'payload':req['payload'],'payload_sha256':req['payload_sha256'],
                    'dependencies':deps,'dependency_sha256':req['dependency_sha256'],'preview_sha256':deps.get('preview_sha256'),'actor':'simulated-native-fixture' if getattr(authority,'simulated',False) else 'native-device-owner',
                    'simulation':bool(getattr(authority,'simulated',False)),'view_observation':view_observation,
                    'revoked':req['decision']=='revoke','at':iso_utc(studio.now()),'request_id':token,'prior_event':previous['checkpoint_id'] if previous else None}
                check_cancelled()
                result=R.control(studio.sp).append_locked(ev)
            # A compatibility projection only. Its editable by field never grants authority.
            if req['kind']=='bounds' and req['decision']=='confirm':
                meta=R.json_file(ep.meta_path);meta.setdefault('trim',{})[req['session']]={**req['payload'],'by':'human','at':ev['at'],'checkpoint':ev['checkpoint_id']}
                from ..episode import write_json
                R.regular(ep.meta_path.with_suffix('.json.tmp'));write_json(ep.meta_path,meta)
            self.prepared.pop(token,None)
            return {'checkpoint':result['checkpoint_id'],'state':'stale' if result['revoked'] else 'reviewed exact'}
