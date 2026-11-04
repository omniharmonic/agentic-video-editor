"""Inherited-socket verification for one durable episode job.

No approval key enters the worker. The parent verifies the control ledger and
returns bounded read snapshots. Only mechanical managed intake and exact output
registration can write through this bridge. Loss/revocation cancels the worker
process group; completed immutable output is retained for recovery.
"""
from __future__ import annotations
import json
import os
import socket
import threading

MAX_MESSAGE=16*1024*1024
CLIENT=None


def receive(stream):
    line=stream.readline(MAX_MESSAGE+1)
    if not line or len(line)>MAX_MESSAGE or not line.endswith(b'\n'):raise ValueError('Episode verification bridge closed or message too large')
    obj=json.loads(line)
    if not isinstance(obj,dict):raise ValueError('Invalid episode bridge message')
    return obj


def send(stream,obj):
    data=json.dumps(obj,allow_nan=False).encode()+b'\n'
    if len(data)>MAX_MESSAGE:raise ValueError('Episode bridge response exceeds budget')
    stream.write(data);stream.flush()


class Client:
    def __init__(self,fd,jid):
        self.sock=socket.socket(fileno=fd);self.sock.settimeout(10);self.stream=self.sock.makefile('rwb');self.lock=threading.Lock();self.jid=jid;self.stopped=threading.Event();self.failure=None
        self.call('snapshot')
        self.monitor=threading.Thread(target=self._monitor,daemon=True);self.monitor.start()
    def call(self,action,**data):
        with self.lock:
            # Critical registration performs full media hashes, which may take
            # minutes on multi-hour camera files. Heartbeats stay bounded at 10s.
            self.sock.settimeout(14400 if action in ('artifact','cleanup-register','final-broll') or action.endswith('-register') else 10)
            try:send(self.stream,{'job_id':self.jid,'action':action,'data':data});result=receive(self.stream)
            finally:self.sock.settimeout(10)
        if not result.get('ok'):raise ValueError(result.get('error','Episode verification refused'))
        return result['result']
    def _monitor(self):
        import signal
        while not self.stopped.wait(1):
            try:self.call('snapshot')
            except Exception as exc:
                self.failure=str(exc)
                if not self.stopped.is_set():os.killpg(os.getpgrp(),signal.SIGTERM)
                return
    def close(self):
        self.stopped.set()
        try:self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.monitor.join(timeout=1)
        try:self.stream.close()
        except OSError:pass
        self.sock.close()


class RemoteControl:
    def rows(self):return CLIENT.call('snapshot')['rows']
    def append_locked(self,*args,**kwargs):raise ValueError('Workers cannot write native control records')


def dependency_freshness(studio,ep,row,dependencies,operation,args):
    """Verify ledgers and cheap exact file identities between full closure checks."""
    from . import episode_registry as R,episode_checkpoints as C
    from .workbench import safe,read
    from .episode_workshop import state_hash
    from .. import library
    from ..library import derive,registry
    R.get(studio.sp,row['id'],row['grant_revision']);control=R.control(studio.sp).rows()
    problems=registry.license_log(studio.sp).verify()
    if problems:raise ValueError('License ledger verification failed')
    session=args['session'];sessions={session}
    if session=='combined':
        combined=C.latest(studio,row['id'],'assembly','combined')
        if combined:
            from .episode_workshop import exact
            _,d=exact(ep,combined['payload']);sessions.update(r['identity']['name'] for r in d.get('combined_constituents',[]))
    records=library.read_all(studio.sp);strings=set();edits=[];resources=[];asrs=[]
    def collect(v):
        if isinstance(v,dict):
            if {'stage','name','version','sha256'}<=set(v):edits.append(v)
            if {'path','sha256'}<=set(v):resources.append(v['path'])
            if v.get('engine')=='fluidaudio-parakeet-v3' and v.get('model_dir'):asrs.append(v)
            for x in v.values():collect(x)
        elif isinstance(v,list):
            for x in v:collect(x)
        elif isinstance(v,str):strings.add(v)
    collect(dependencies);collect(args);asset_ids=sorted(strings.intersection(records));order,unknown=derive.closure(records,asset_ids)
    scopes={(r.get('kind'),r.get('session')) for r in control if r.get('checkpoint_id') in strings}
    events=[r for r in control if r.get('schema')=='episode_checkpoint.v1' and r.get('episode')==row['id'] and ((r.get('kind')=='speakers' and r.get('session')=='main') or r.get('session') in sessions or (r.get('kind'),r.get('session')) in scopes)]
    if unknown or len(order)>300:raise ValueError('Episode asset closure changed')
    shas={records[x]['sha256'] for x in order};rights=[r for r in control if r.get('sha256') in shas or r.get('asset') in order]
    entries=[registry.license_log(studio.sp).find(sha256=x) for x in sorted(shas)]
    # state_hash already checks these small evidence files by bytes; fresh
    # extraction may atomically rewrite equivalent media.json content.
    paths=[ep.meta_path]
    paths.extend(ep.root/x['file'] for x in row['sources'])
    paths.extend(studio.sp.root/records[x]['file'] for x in order)
    from pathlib import Path
    paths.extend(Path(p) for p in resources)
    for asr in asrs:
        from .. import config
        model=Path(asr['model_dir']);files=sorted(p for p in model.rglob('*') if p.is_file() or p.is_symlink())
        if not files or len(files)>5000:raise ValueError('ASR model inventory changed')
        packaged=model.parent/'speech'/'fluidaudiocli';cli=packaged if packaged.is_file() else Path(config.fluidaudio_cli())
        paths.extend(files);paths.append(cli)
    from .. import editdoc
    for d in edits:
        try:
            p=editdoc.resolve(ep,d['stage'],d['version'],d['name'],id=d.get('id'),sha256=d.get('sha256'));paths.append(p)
            doc=R.json_file(R.regular(p))
            for track in ('music','overlays'):
                paths.extend(ep.root/item['file'] for item in doc['tracks'].get(track,[]) if item.get('file'))
            if doc.get('caption_snapshot'):paths.append(ep.root/doc['caption_snapshot']['file'])
            paths.extend(ep.root/doc['captions'][k] for k in ('srt','ass') if doc.get('captions',{}).get(k))
        except (ValueError,TypeError,KeyError):raise ValueError('Invalid dependent edit identity')
    for jid in (x for x in strings if x.startswith('job_')):
        plan=studio.sp.work/'production'/'episode-renders'/row['id']/jid/'sequence-plan.json'
        if plan.exists():paths.append(plan)
    for aid in (x for x in strings if x.startswith('artifact_')):
        p=studio.sp.work/'production'/'episode-artifacts'/(aid+'.json')
        if p.exists():
            record=read(studio.sp,p);paths.extend([p,studio.sp.root/record['file']])
            details=record.get('details') or {}
            for k in ('caption','asr'):
                if details.get(k,{}).get('filename'):paths.append((studio.sp.root/record['file']).parent/details[k]['filename'])
    stats=[]
    for p in sorted(set(paths),key=str):
        if not p.exists():continue
        p=safe(studio.sp,p) if p.is_relative_to(studio.sp.root) else R.regular(p);st=p.stat();stats.append((str(p),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns))
    return {'state_hash':state_hash(ep,row,operation,session),'source_stats':R._source_stats(ep,row),'episode_policy':R.policy(),'events':events,'rights':rights,'entries':entries,'files':stats}



class Parent:
    def __init__(self,studio,job):
        from .episode_workshop import check_descriptor
        check_descriptor(studio,job)
        self.studio=studio;self.job=json.loads(json.dumps(job));self.sock,self.child=socket.socketpair();self.thread=None;self.scope=None;self.root=None;self.additional={};self.initial=None;self.closed=False
        try:
            if job['operation']!='episode-create':
                from . import episode_registry as R
                self.scope=R.resolve(studio,job['args']['episode'],job['args']['grant_revision'],verify_sources=False)
                ep,row=self.scope.__enter__();self.root=ep.root
                self.additional={k:str(ep.root/row['layout'][k]) for k in row.get('additional',{})}
                if job['operation'] in ('episode-assembly','episode-combined','episode-rough','episode-sequence','episode-final'):
                    captured=R.control(studio.sp).rows()
                    # One synchronous parent-construction scope. Its token ends
                    # before the inherited RPC thread starts; no cross-thread trust.
                    with R.resolve(studio,row['id'],row['grant_revision'],_fresh_sources=True):
                        if job['operation']=='episode-combined':
                            from .episode_workshop import combined_inputs
                            self.initial=combined_inputs(studio,ep,row,job['args'])
                        elif job['operation']=='episode-assembly':
                            from .episode_workshop import base_inputs
                            self.initial=base_inputs(studio,ep,row,job['args']['session'])
                        elif job['operation']=='episode-rough':
                            from .episode_workshop import rough_inputs
                            self.initial=rough_inputs(studio,ep,row,job['args'])
                        elif job['operation']=='episode-sequence':
                            from .episode_workshop import sequence_inputs
                            self.initial=sequence_inputs(studio,ep,row,job['args'])
                        else:
                            from .episode_workshop import final_inputs
                            self.initial=final_inputs(studio,ep,row,job['args'])
                    if job['operation']=='episode-combined':
                        self.combined_fresh=self._combined_freshness(ep,row);fresh=self.combined_fresh
                    else:self.episode_fresh=self._episode_freshness(ep,row);fresh=self.episode_fresh
                    scopes={(r['kind'],r['session']) for r in fresh['events']}
                    before=[r for r in captured if r.get('schema')=='episode_checkpoint.v1' and r.get('episode')==row['id'] and (r.get('kind'),r.get('session')) in scopes]
                    if before!=fresh['events']:raise ValueError('Episode checkpoints changed during initial parent verification')
        except BaseException:
            self.close()
            raise
    def start(self):
        self.thread=threading.Thread(target=self._serve,daemon=True);self.thread.start()
    def _episode_freshness(self,ep,row):
        return dependency_freshness(self.studio,ep,row,self.initial,self.job['operation'],self.job['args'])

    def _combined_freshness(self,ep,row):
        """Check held identities and relevant verified events, not media bytes."""
        from . import episode_registry as R,episode_checkpoints as C
        from .workbench import safe
        from .. import library,editdoc
        current=R.get(self.studio.sp,row['id'],row['grant_revision'])
        if current['source_fingerprint']!=row['source_fingerprint']:raise ValueError('Registered source inventory changed')
        sessions={r['identity']['name'] for r in self.initial['constituents']}
        events=[r for r in C.events(self.studio) if r.get('episode')==row['id'] and ((r.get('kind')=='speakers' and r.get('session')=='main') or (r.get('session') in sessions and r.get('kind') in ('mapping','bounds','assembly')))]
        paths=[ep.meta_path,ep.artifact('media.json'),ep.artifact('transcript.json'),ep.artifact('sync.json')]
        paths.extend(ep.root/s['file'] for s in row['sources'])
        records=library.read_all(self.studio.sp)
        for item in self.initial['constituents']:
            identity=item['identity'];paths.append(editdoc.resolve(ep,'assembly',identity['version'],identity['name'],id=identity.get('id'),sha256=identity.get('sha256')))
            for ref in item['asset_closure']:
                rec=records.get(ref['id'])
                if not rec or rec['sha256']!=ref['sha256'] or rec.get('derived_from',[])!=ref['derived_from']:raise ValueError('Constituent provenance changed')
                paths.append(self.studio.sp.root/rec['file'])
        stats=[]
        for p in paths:
            p=safe(self.studio.sp,p) if p.is_relative_to(self.studio.sp.root) else R.regular(p)
            st=p.stat();stats.append((str(p),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns))
        return {'events':events,'files':stats,'source_stats':R._source_stats(ep,row)}

    def _current(self):
        if self.closed:raise ValueError('Episode verification parent closed; retry with a fresh parent')
        from .workbench import read,job_path
        from .episode_workshop import check_descriptor
        current_path=job_path(self.studio.sp,self.job['id']);current=read(self.studio.sp,current_path)
        if current_path.with_suffix('.cancel').exists():raise ValueError('Job cancellation requested')
        if current['args']!=self.job['args'] or current['operation']!=self.job['operation'] or current['status'] not in ('queued','running'):raise ValueError('Episode job identity or lifecycle changed')
        check_descriptor(self.studio,current)
        from . import episode_registry as R
        if current['operation'] in ('episode-rough','episode-sequence','episode-final') or (current['operation']=='episode-assembly' and self.initial is not None):
            ep=R.GrantedEpisode(self.root);row=R.get(self.studio.sp,current['args']['episode'],current['args']['grant_revision'])
            observed=self._episode_freshness(ep,row)
            if observed!=self.episode_fresh:raise ValueError('Episode reviewed assembly, source or license/attestation changed during execution: '+', '.join(k for k in observed if observed[k]!=self.episode_fresh[k]))
            return current
        if current['operation']=='episode-combined':
            ep=R.GrantedEpisode(self.root);row=R.get(self.studio.sp,current['args']['episode'],current['args']['grant_revision'])
            if self._combined_freshness(ep,row)!=self.combined_fresh:raise ValueError('Combined constituent changed during execution')
            return current
        if current['operation']!='episode-create':
            with R.resolve(self.studio,current['args']['episode'],current['args']['grant_revision'],verify_sources=False) as (ep,row):
                if ep.root!=self.root:raise ValueError('Episode location changed during this job; retry after reconnecting')
        return current
    def handle(self,message):
        if set(message)!={'job_id','action','data'} or message['job_id']!=self.job['id'] or not isinstance(message['data'],dict):raise ValueError('Wrong-job or malformed verification request')
        job=self._current();data=message['data'];action=message['action'];studio=self.studio
        from . import episode_registry as R,episode_workshop as E
        if action=='snapshot':
            if data:raise ValueError('Snapshot accepts no client scope')
            rows=R.control(studio.sp).rows();eid=job['args'].get('episode')
            selected=[r for r in rows if (r.get('schema')=='registered_episode.v1' and r.get('id')==eid) or (r.get('schema')=='episode_checkpoint.v1' and r.get('episode')==eid)]
            selected=[{**r,'binding':{'identity':r['binding']['identity']},'additional':{k:{} for k in r.get('additional',{})}} if r.get('schema')=='registered_episode.v1' else r for r in selected]
            return {'rows':selected,'root':str(self.root) if self.root else None,'additional':self.additional}
        if action=='create':
            if data or job['operation']!='episode-create':raise ValueError('Managed creation is not granted to this job')
            return E.create(studio,job['args'])
        if action=='combined-inputs':
            if data or job['operation']!='episode-combined':raise ValueError('Combined inputs are outside this job grant')
            return self.initial
        if action=='rough-inputs':
            if data or job['operation']!='episode-rough':raise ValueError('Rough inputs are outside this job grant')
            return self.initial
        if action=='sequence-inputs':
            if data or job['operation']!='episode-sequence':raise ValueError('Sequence inputs are outside this job grant')
            return self.initial
        if action=='final-inputs':
            if data or job['operation']!='episode-final':raise ValueError('Final inputs are outside this job grant')
            return self.initial
        if action=='final-broll':
            if data or job['operation'] not in ('episode-final','episode-assembly','episode-rough'):raise ValueError('B-roll verification is outside this job grant')
            with R.resolve(studio,job['args']['episode'],job['args']['grant_revision'],_fresh_sources=True) as (ep,row):
                if job['operation']=='episode-final':
                    if not self.initial['broll_proposals'] and not any(o.get('kind')=='broll' for o in self.initial.get('inherited_tracks',{}).get('overlays',[])):raise ValueError('B-roll is outside this final job grant')
                    candidate=R.regular(ep.work/'native-final'/job['id']/'candidate.json');doc=R.json_file(candidate)
                    E.validate_final_candidate(ep,doc,job['args'],self.initial)
                else:
                    candidate=R.regular(ep.work/'native-inherited'/job['id']/'candidate.json');doc=R.json_file(candidate)
                    E.validate_inherited_candidate(studio,ep,row,job,doc)
                from ..broll import place
                result=place.closure(ep,doc)
                if not result['ok']:raise ValueError(place.blocked_message(result))
            self._current()
            return result
        if action=='artifact':
            if set(data)!={'file','kind','source','edit','details'}:raise ValueError('Invalid artifact registration request')
            with R.resolve(studio,job['args']['episode'],job['args']['grant_revision'],_fresh_sources=True) as (ep,row):
                if job['operation']=='episode-intake' and data['kind']=='source':
                    source=next((s for s in row['sources'] if s==data['source']),None)
                    if not source or data['edit'] is not None:raise ValueError('Artifact source differs from registered input')
                    directory=studio.sp.work/'production'/'episode-previews'/row['id']/source['sha256']
                    allowed=[directory/'preview.mp4',directory/'preview.m4a']
                elif job['operation']=='episode-sequence' and data['kind'] in ('sequence-opening','sequence-closing'):
                    tag=data['kind'].removeprefix('sequence-')
                    if not job['args'][tag]['enabled'] or data['source'] is not None:raise ValueError('Sequence component is outside this job grant')
                    identity={k:job['args']['rough'][k] for k in ('stage','name','version','sha256')}
                    if data['edit']!=identity:raise ValueError('Sequence source differs from reviewed rough')
                    directory=studio.sp.work/'production'/'episode-renders'/row['id']/job['id']
                    from .workbench import read
                    receipt=read(studio.sp,directory/(tag+'-receipt.json'))
                    plan_sha=receipt['plan_sha256']
                    record=E.sequence_record(studio,ep,row,job['id'],plan_sha)
                    if record['args']!=job['args'] or record['snapshot']['dependencies']!=self.initial:raise ValueError('Sequence plan differs from this job')
                    if data['details'].get('job_id')!=job['id'] or data['details'].get('plan_sha256')!=plan_sha:raise ValueError('Sequence artifact does not identify this plan')
                    allowed=[directory/receipt['filename']]
                elif job['operation'] in ('episode-assembly','episode-combined','episode-rough','episode-final') and data['kind']==('assembly' if job['operation']=='episode-combined' else job['operation'].removeprefix('episode-')):
                    p,doc=E.exact(ep,data['edit'])
                    if data['edit']['name']!=job['args']['session']:raise ValueError('Render session differs from job')
                    if job['operation']=='episode-combined':
                        if E.combined_inputs(studio,ep,row,job['args'])!=self.initial:raise ValueError('Combined constituent changed before registration')
                        expected=E.compose_assemblies(ep,self.initial)
                        if doc.get('episode_inputs')!=self.initial or doc.get('combined_constituents')!=self.initial['constituents'] or any(doc.get(k)!=expected.get(k) for k in ('tracks','sources','duration','fps','canvas','media_mode')):raise ValueError('Combined assembly differs from its exact constituents')

                    if job['operation']=='episode-assembly' and doc['episode_inputs']!=E.base_inputs(studio,ep,row,job['args']['session']):raise ValueError('Assembly dependencies changed during execution')
                    if job['operation'] in ('episode-assembly','episode-rough') and row.get('adoption'):E.validate_inherited_candidate(studio,ep,row,job,doc)
                    if job['operation']=='episode-rough':
                        if doc.get('rough_inputs')!=E.rough_inputs(studio,ep,row,job['args']):raise ValueError('Rough dependencies changed during execution')
                        report=doc.get('rough_report') or {}
                        if report.get('policy')!=job['args']['policy'] or report.get('restored')!=job['args']['restore']:raise ValueError('Rough choices differ from this job')
                    if job['operation']=='episode-final':
                        if E.final_inputs(studio,ep,row,job['args'])!=self.initial:raise ValueError('Final dependencies changed before registration')
                        if doc.get('final_spec')!=job['args'] or doc.get('final_inputs')!=self.initial:raise ValueError('Final choices or dependencies differ from this job')
                        E.validate_final_candidate(ep,doc,job['args'],self.initial)
                        E.native_final_license_gate(studio,ep,doc,job['args'],self.initial)
                        E._ASR_FINGERPRINT_CACHE.clear()
                        if E.final_asr_configuration()!=self.initial['asr_configuration']:raise ValueError('ASR model or executable changed before registration')
                        from .video_workshop import pinned_caption
                        profile=pinned_caption(studio.sp,ep,doc,granted_episode=True)
                        if profile['style']!=self.initial['caption_profile']['style'] or profile['font']['sha256']!=self.initial['caption_profile']['font_sha256']:raise ValueError('Final caption snapshot changed')
                    directory=studio.sp.work/'production'/'episode-renders'/row['id']/job['id']
                    from .workbench import read
                    receipt=read(studio.sp,directory/'render-receipt.json')
                    if receipt['edit']!=data['edit']:raise ValueError('Render receipt differs from selected edit')
                    allowed=[directory/receipt['filename']]
                else:raise ValueError('Artifact operation is outside this job grant')
                from .workbench import safe
                path=safe(studio.sp,studio.sp.root/data['file'])
                if path not in allowed:raise ValueError('Artifact destination is outside this job grant')
                from ..media import probe_file
                from ..publish.safety import sha256_file
                probe=probe_file(path,studio.sp.root)
                if job['operation']=='episode-combined':
                    from .. import library
                    rec=library.get(studio.sp,data['details'].get('asset'));origin=rec.get('origin') or {}
                    if rec.get('sha256')!=sha256_file(path) or rec.get('derived_from')!=sorted({r['asset'] for r in self.initial['constituents']}) or origin.get('schema')!='episode_combined_render.v1' or origin.get('edit')!=data['edit'] or origin.get('dependencies')!=self.initial:raise ValueError('Combined output provenance differs from its constituents')
                if data['kind'] in ('assembly','rough','final'):
                    expected_duration=receipt['report']['duration'] if data['kind']=='final' else doc['duration']
                    if sha256_file(path)!=receipt['sha256'] or abs(probe['duration']-expected_duration)>.1:raise ValueError('Rendered output identity/duration mismatch')
                    from ..editdoc import effective_mode
                    if bool(probe['video'])!=(effective_mode(doc)!='audio') or not probe['audio']:raise ValueError('Rendered streams differ from declared episode mode')
                    if data['kind']=='final':
                        if self.initial['mode']!='audio':
                            from ..compile import _canvas_for
                            expected_size=_canvas_for(doc,self.initial['export_preset']['settings'])
                            if (probe['video']['width'],probe['video']['height'])!=expected_size:raise ValueError('Final picture size differs from selected export quality')
                        cap=receipt.get('caption') or {}
                        E.validate_programme_caption_boundaries(cap.get('cues',[]),receipt['report'])
                        asr=read(studio.sp,directory/'asr-receipt.json')
                        if data['details'].get('asr')!=asr or data['details'].get('report')!={**receipt['report'],'asr_comparison':asr['comparison']}:raise ValueError('Final transcription receipt differs from this output')
                        import re
                        if not re.fullmatch(r'asr-evidence-[a-f0-9]{64}\.json',asr.get('filename','')):raise ValueError('Invalid final transcription evidence path')
                        evidence=safe(studio.sp,directory/asr['filename'])
                        if asr['media_sha256']!=receipt['sha256'] or asr['configuration']!=self.initial['asr_configuration'] or sha256_file(evidence)!=asr['sha256']:raise ValueError('Final transcription identity changed')
                        content=read(studio.sp,evidence)
                        if asr['filename']!='asr-evidence-'+R.digest(content)+'.json' or content.get('media_sha256')!=receipt['sha256'] or content.get('configuration')!=self.initial['asr_configuration'] or content.get('source_transcript_sha256')!=E.C.file_sha(ep,'transcript.json') or E.compare_final_captions(cap['cues'],content['transcript']['words'])!=asr['comparison']:raise ValueError('Final transcription evidence does not match its inputs')
                        if data['details'].get('caption')!=cap or cap.get('filename')!=path.with_suffix('.srt').name or sha256_file(path.with_suffix('.srt'))!=cap.get('sha256'):raise ValueError('Final caption receipt differs from this output')
                        if self.initial['mode']=='audio':
                            expected=round(doc['duration']*48000)+sum(rec['details']['report']['samples'] for rec in self.initial['components'].values())
                            if receipt['report'].get('samples')!=expected or abs(probe['duration']-expected/48000)>.001:raise ValueError('Final audio samples differ from selected parts')
                if data['kind'].startswith('sequence-'):
                    if sha256_file(path)!=receipt['sha256'] or not probe['audio'] or bool(probe['video'])!=(self.initial['mode']!='audio'):raise ValueError('Sequence preview identity or streams changed')
                    if data['details'].get('report')!=receipt['report']:raise ValueError('Sequence report differs from its receipt')
                    caption=receipt['report'].get('caption')
                    if caption and (data['details'].get('caption')!=caption or caption.get('filename')!=path.with_suffix('.srt').name or sha256_file(path.with_suffix('.srt'))!=caption.get('sha256')):raise ValueError('Sequence caption bytes changed')
                    if self.initial['mode']=='audio':
                        expected=sum(round((x['end']-x['start'] if x['kind']=='excerpt' else x['duration'])*48000) for x in job['args'][tag]['components'])
                        if receipt['report'].get('samples')!=expected or abs(probe['duration']-expected/48000)>.001:raise ValueError('Sequence audio samples differ from selected components')
                        caption=receipt['report'].get('caption') or {}
                        if data['details'].get('caption')!=caption:raise ValueError('Sequence caption receipt differs from this preview')
                        if caption.get('filename')!=path.with_suffix('.srt').name or sha256_file(path.with_suffix('.srt'))!=caption.get('sha256'):raise ValueError('Sequence caption bytes changed')
                output_sha=sha256_file(path)  # last full output read before publication
                if data['kind']!='source' and output_sha!=receipt['sha256']:raise ValueError('Output changed before registration')
            # The fresh scope's exit scan is part of verification, never a
            # post-publication check. self.scope retains the native path lease.
            return E.register_artifact(studio,row,path,kind=data['kind'],source=data['source'],edit=data['edit'],details=data['details'],verified_sha256=output_sha,before_publish=self._current)
        raise ValueError('Unknown episode bridge action; native confirmation is unavailable')
    def _serve(self):
        stream=self.sock.makefile('rwb')
        try:
            while True:
                message=receive(stream)
                try:result={'ok':True,'result':self.handle(message)}
                except Exception as exc:result={'ok':False,'error':str(exc)}
                send(stream,result)
        except (ValueError,OSError):pass
        finally:
            try:stream.close()
            except OSError:pass  # Worker termination may leave a buffered reply.
            self.sock.close()
    def close(self):
        self.closed=True
        self.child.close()
        try:self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.sock.close()
        if self.thread:self.thread.join(timeout=3)
        if self.scope:
            try:self.scope.__exit__(None,None,None)
            except (ValueError,OSError):pass
            self.scope=None
