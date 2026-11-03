"""Enumerated release-job authority over the existing inherited transport."""
from __future__ import annotations
import copy
import json
import socket
from pathlib import Path

from .episode_worker_bridge import Parent
from . import episode_registry as R, episode_workshop as E, episode_release as L, video_workshop as V
from .workbench import safe, read, write, job_path
from .. import editdoc, library, media
from ..publish.safety import sha256_file


class ReleaseParent(Parent):
    def _prepare_transfer(self):
        args=self.job['args'];op=self.job['operation']
        self.initial=(L.adoption_inputs(self.studio,args) if op=='episode-adopt' else L.kept_render_inputs(self.studio,args) if op=='video-render' else L.native_kept_inputs(self.studio,args))
        self.episode_id=self.initial.get('episode')
        if self.episode_id:
            self.scope=R.resolve(self.studio,self.episode_id,self.initial['grant_revision'],verify_sources=False)
            ep,row=self.scope.__enter__();self.root=ep.root;self.additional={k:str(ep.root/row['layout'][k]) for k in row.get('additional',{})}
        self.release_fresh=self._release_freshness();self.transfer_fresh=self._transfer_freshness()

    def _transfer_freshness(self):
        args=self.job['args'];op=self.job['operation']
        if op in ('episode-adopt','video-render'):
            ep,meta=V.project(self.studio.sp,args['project']);latest=editdoc.latest_path(ep,'short')
            result={'revision':meta['project_rev'],'head':latest.name,'sha256':sha256_file(latest)}
            if op=='video-render':
                from . import episode_checkpoints as C
                result['events']=[r for r in C.events(self.studio) if r.get('episode')==self.initial['episode']]
                result['registration']=R.get(self.studio.sp,self.initial['episode'],self.initial['grant_revision'])
                result['source_stats']=R._source_stats(R.GrantedEpisode(self.root),result['registration'])
            return result
        from . import episode_checkpoints as C
        row=R.get(self.studio.sp,self.initial['episode'],self.initial['grant_revision'])
        events=[r for r in C.events(self.studio) if r.get('episode')==row['id']]
        return {'registration':row,'events':events,'source_stats':R._source_stats(R.GrantedEpisode(self.root),row)}

    def _current_transfer(self,current):
        if self._transfer_freshness()!=self.transfer_fresh or self._release_freshness()!=self.release_fresh:
            raise ValueError('Transfer source, keep, license or revision changed')
        from ..library import registry
        if registry.license_log(self.studio.sp).verify():raise ValueError('License ledger changed')
        # Verification of signed rights is cheap and remains current even while
        # a large source copy is in progress.
        rights=R.control(self.studio.sp).rows();asset_ids={x['id'] for x in self.initial['closure']['assets']['closure']}
        relevant=[r for r in rights if r.get('asset') in asset_ids]
        if not hasattr(self,'transfer_rights'):self.transfer_rights=relevant
        if relevant!=self.transfer_rights:raise ValueError('Transfer license/attestation changed')
        return current

    def _register_transfer(self):
        from ..episode import Episode
        from .workbench import _publish_staged
        from . import ledger
        studio=self.studio;job=self.job;op=job['operation'];args=job['args'];directory=L.directory(studio,job)
        resultpath=directory/'result.json'
        if resultpath.exists():return L.read_result(studio,resultpath)
        deps=L.adoption_inputs(studio,args) if op=='episode-adopt' else L.native_kept_inputs(studio,args)
        if deps!=self.initial:raise ValueError('Transfer input changed before publication')
        stage=Episode(directory/'managed');candidate=read(studio.sp,directory/'candidate.json')
        expected=L.transfer_doc(studio,stage,job,deps)
        if candidate!={'operation':op,'args':args,'dependencies':deps,'doc':expected}:raise ValueError('Transfer candidate differs from captured input')
        ident=expected['registration']['id'] if op=='episode-adopt' else expected['independent_project']
        target=Episode(V.safe_dir(studio.sp,studio.sp.work/'production'/'episodes'/ident))
        published=target.root.exists();materialized=target if published else stage
        if published:
            recovered=L.recover_transfer_intent(studio,job)
            if not recovered or not recovered['recovery']['committed_earlier']:raise ValueError('Existing transfer target has no owned publication intent')
        for item in deps['closure']['files'].values():
            if sha256_file(safe(studio.sp,materialized.root/item['destination']))!=item['sha256']:raise ValueError('Transferred dependency changed')
        # Rebase the library reference for its final owned root before publishing.
        for overlay in expected['tracks'].get('overlays',[]):
            if overlay.get('library'):
                import os
                overlay['library']['studio']=Path(os.path.relpath(studio.sp.root,target.root)).as_posix()
        meta=L.transfer_meta(job,deps,expected)
        name='main' if op=='episode-adopt' else None
        if published:
            if R.json_file(target.meta_path)!=meta or R.json_file(editdoc.path_for(target,expected['stage'],1,name))!=expected:raise ValueError('Published transfer differs from the captured request')
        else:
            if read(studio.sp,stage.meta_path)!=L.transfer_meta(job,deps,candidate['doc']):raise ValueError('Transferred metadata differs from captured request')
            write(studio.sp,stage.meta_path,meta)
            with editdoc.mutation_lock(stage):
                editdoc.save_new_version(stage,expected,name=name,request_id=args['request_id'],expected_version=0,locked=True)
        if op=='episode-adopt' and not published:
            # Disk evidence retains the original per-source snapshots; this
            # aggregate is a projection for episode orientation/ASR comparison.
            from ..occurrences import mapped_words
            words=mapped_words(stage,expected);source=next(iter(expected['sources'].values()))['file']
            write(studio.sp,stage.artifact('transcript.json'),{'source':source,'duration':expected['duration'],'engine':'adopted immutable source transcripts','words':words,'utterances':[]})
            probes=[media.probe_file(stage.root/s['file'],stage.root) for s in expected['sources'].values()]
            write(studio.sp,stage.artifact('media.json'),{'files':probes,'sessions':[{'id':'main','label':'Adopted programme'}]})
            write(studio.sp,stage.artifact('sync.json'),{'reference':source,'files':{s['file']:{k:copy.deepcopy(s[k]) for k in ('start_offset','drift_ppm','sync_map') if k in s} for s in expected['sources'].values()}})
        if op=='episode-adopt':
            spec,paths=R.layout_paths(materialized.root,meta);sources=R.scan(materialized.root,paths)
        owned=[{'file':v['destination'],'sha256':v['sha256']} for v in deps['closure']['files'].values()]
        if not published:
            # Prepare a parent-signed ownership receipt before the final guard.
            # Rename preserves this directory inode; foreign same-byte folders
            # therefore cannot impersonate a previously published transfer.
            files={x['file']:x['sha256'] for x in owned}
            fixed=[stage.meta_path,editdoc.path_for(stage,expected['stage'],1,name)]
            if op=='episode-adopt':fixed.extend(stage.artifact(n) for n in ('transcript.json','media.json','sync.json'))
            files.update({p.relative_to(stage.root).as_posix():sha256_file(p) for p in fixed})
            st=stage.root.stat()
            intent={'kind':'transfer-publication-intent','id':ident,'target':studio.sp.rel(target.root),'directory_identity':[st.st_dev,st.st_ino],
                    'files':[{'file':k,'sha256':v} for k,v in sorted(files.items())]}
            L.write_result(studio,directory/'publication-intent.json',intent)
        self._current()
        target.root.parent.mkdir(parents=True,exist_ok=True)
        with ledger.ledger_lock(studio.sp):
            self._current()
            if not target.root.exists():_publish_staged(stage.root,target.root)
            else:
                existing=R.json_file(target.meta_path)
                if existing!=meta:raise ValueError('Transferred project destination already belongs to different input')
            if op=='episode-adopt':
                row={'schema':'registered_episode.v1','id':ident,'workspace':str(studio.sp.root),'ownership':'managed_copy','label':args['title'],
                     'root_hint':studio.sp.rel(target.root),'binding':{'identity':[target.root.stat().st_dev,target.root.stat().st_ino]},'additional':{},'layout':spec,'sources':sources,
                     'source_fingerprint':R.digest(sources),'grant_revision':1,'derived_consent':True,'adoption':expected['adoption_inputs'],
                     'adopted_assembly_sha256':sha256_file(editdoc.path_for(target,'assembly',1,'main')),
                     'adopted_files':[{'file':v['destination'],'sha256':v['sha256']} for v in deps['closure']['files'].values()],
                     'adopted_assets':deps['closure']['assets']['selected']}
                if ident not in R.records(studio.sp):R.control(studio.sp).append_locked(row)
                result={'episode':ident,'review':'Fresh rendered assembly review required','adoption':expected['adoption_inputs']}
            else:result=V.load(studio.sp,ident)
            result['owned_files']=[{'file':studio.sp.rel(target.root/x['file']),'sha256':x['sha256']} for x in owned]
            L.write_result(studio,resultpath,result)
        return result

    def __init__(self,studio,job):
        E.check_descriptor(studio,job)
        self.studio=studio;self.job=copy.deepcopy(job);self.sock,self.child=socket.socketpair();self.thread=None
        self.scope=None;self.root=None;self.additional={};self.committed=None;self.closed=False
        try:
            op=job['operation'];args=job['args']
            if op in ('episode-adopt','video-kept','video-render'):
                self._prepare_transfer()
                return
            if op not in L.OPS:raise ValueError('Unknown release grant')
            self.scope=R.resolve(studio,args['episode'],args['grant_revision'],verify_sources=False)
            ep,row=self.scope.__enter__();self.root=ep.root;self.additional={k:str(ep.root/row['layout'][k]) for k in row.get('additional',{})}
            with R.resolve(studio,args['episode'],args['grant_revision']) as (verified,verified_row):
                self.initial=L.inputs(studio,verified,verified_row,op,args)
            self.episode_fresh=self._episode_freshness(ep,row)
            self.release_fresh=self._release_freshness()

        except BaseException:
            self.close()
            raise

    def _release_freshness(self):
        result=[]
        def walk(value):
            if isinstance(value,dict):
                if {'path','sha256'}<=set(value):
                    p=R.regular(value['path']);st=p.stat();result.append((str(p),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns))
                for item in value.values():walk(item)
            elif isinstance(value,list):
                for item in value:walk(item)
        walk(self.initial)
        rows=R.control(self.studio.sp).rows();keep=(self.initial.get('keep') or {}).get('name')
        return {'files':result,'configuration':L.short_configuration(R.GrantedEpisode(self.root),self.job['args'].get('show')) if self.job['operation']=='episode-short' else None,'keep':[r for r in rows if r.get('schema')=='episode_checkpoint.v1' and r.get('kind')=='keep' and r.get('session')==keep]}

    def _current(self):
        if self.closed:raise ValueError('Release verification parent closed; retry with a fresh parent')
        path=job_path(self.studio.sp,self.job['id']);current=read(self.studio.sp,path)
        if path.with_suffix('.cancel').exists():raise ValueError('Job cancellation requested')
        if current['args']!=self.job['args'] or current['operation']!=self.job['operation'] or current['status'] not in ('queued','running'):raise ValueError('Release job identity or lifecycle changed')
        E.check_descriptor(self.studio,current)
        if current['operation'] in ('episode-adopt','video-kept','video-render'):return self._current_transfer(current)
        row=R.get(self.studio.sp,current['args']['episode'],current['args']['grant_revision']);ep=R.GrantedEpisode(self.root)
        if self._episode_freshness(ep,row)!=self.episode_fresh or self._release_freshness()!=self.release_fresh:
            raise ValueError('Release source, review, keep or license dependencies changed')
        return current

    def handle(self,message):
        if set(message)!={'job_id','action','data'} or message['job_id']!=self.job['id'] or not isinstance(message['data'],dict):raise ValueError('Wrong-job or malformed release request')
        op=self.job['operation'];action=message['action'];data=message['data']
        if data:raise ValueError('Release bridge accepts no client-expanded scope')
        self._current()
        if action=='snapshot':
            rows=R.control(self.studio.sp).rows();eid=self.job['args'].get('episode') or getattr(self,'episode_id',None)
            selected=[r for r in rows if (r.get('schema')=='registered_episode.v1' and r.get('id')==eid) or (r.get('schema')=='episode_checkpoint.v1' and r.get('episode')==eid)]
            selected=[{**r,'binding':{'identity':r['binding']['identity']},'additional':{k:{} for k in r.get('additional',{})}} if r.get('schema')=='registered_episode.v1' else r for r in selected]
            return {'rows':selected,'root':str(self.root) if self.root else None,'additional':self.additional}
        if action==op+'-inputs':return self.initial
        if action==op+'-status':
            p=L.directory(self.studio,self.job)/'result.json'
            if p.exists():return L.read_result(self.studio,p)
            if op in ('episode-adopt','video-kept'):
                ident=('episode_' if op=='episode-adopt' else 'project_')+R.digest({('adopt' if op=='episode-adopt' else 'kept'):self.job['args']['request_id']})[:24]
                if (self.studio.sp.work/'production'/'episodes'/ident).exists():return self._register_transfer()
            return None
        if action==op+'-register':
            if op=='video-render':return self._register_render()
            if op in ('episode-adopt','video-kept'):return self._register_transfer()
            return self._register()
        if action=='final-broll' and op in ('episode-select','episode-short','video-render'):
            ep=R.GrantedEpisode(self.root) if op!='video-render' else V.project(self.studio.sp,self.job['args']['project'])[0]
            doc=read(self.studio.sp,L.directory(self.studio,self.job)/'edit-candidate.json')
            if op=='video-render':
                if any(doc.get(k)!=self.initial['doc'].get(k) for k in ('tracks','sources','duration','caption_edit','caption_snapshot')):raise ValueError('Native kept rendered candidate changed')
            else:self._validate_clip(doc)
            from ..broll import place
            result=place.closure(ep,doc)
            if not result['ok']:raise ValueError(place.blocked_message(result))
            self._current();return result
        raise ValueError('Action is outside this exact release job grant')

    def _register_render(self):
        studio=self.studio;job=self.job;args=job['args'];directory=L.directory(studio,job)
        if (directory/'result.json').exists():return L.read_result(studio,directory/'result.json')
        with R.resolve(studio,self.initial['episode'],self.initial['grant_revision'],_fresh_sources=True):
            if L.kept_render_inputs(studio,args)!=self.initial:raise ValueError('Native kept render dependencies changed')
        c=read(studio.sp,directory/'render-candidate.json');ep,meta,path,doc,sha=V.exact(studio.sp,args['project'],args['version'],args['sha256'])
        identity=c['identity']
        for key,value in {'project':args['project'],'version':args['version'],'edit_sha256':args['sha256'],'preset':args['preset'],'request_id':args['request_id']}.items():
            if identity.get(key)!=value:raise ValueError('Native kept rendered identity differs from job')
        output=safe(studio.sp,studio.sp.root/c['file'])
        if not output.is_relative_to(ep.exports):raise ValueError('Native kept render is outside its project')
        receipt=read(studio.sp,output.parent/'render.json')
        if receipt['output']!=output.name or receipt['identity']!={k:v for k,v in identity.items() if k not in ('srt_sha256','ass_sha256')}:raise ValueError('Native kept render receipt differs')
        for name,digest in receipt['hashes'].items():
            p=safe(studio.sp,output.parent/name)
            if p.parent!=output.parent or sha256_file(p)!=digest:raise ValueError('Native kept rendered bytes changed')
        parents=V.verified_programme_parents(studio.sp,ep,doc)
        origin={**identity,'native_keep_event':self.initial['keep_origin']['keep_event'],'native_keep_dependencies':self.initial['keep_origin']['keep_dependency_sha256']}
        asset=library.add_derivative(studio.sp,output,derived_from=parents,origin=origin,by='workbench')
        output_sha=sha256_file(output);self._current()
        result={**c['result'],'assets':[{'asset':asset['id'],'file':asset['file'],'sha256':output_sha,'kind':asset['kind'],'url':V.url(studio.sp,studio.sp.root/asset['file']),'duration':doc['duration'],**doc['canvas']}],
                'coverage':library.coverage(studio.sp,[asset['id']]),'native_keep':doc['kept_clip']}
        from . import ledger
        with ledger.ledger_lock(studio.sp):
            self._current()
            write(studio.sp,output.parent/'result.json',result);L.write_result(studio,directory/'result.json',result)
        return result

    def _validate_clip(self,doc):
        args=self.job['args'];ep=R.GrantedEpisode(self.root)
        if self.job['operation']=='episode-select':
            _,base=E.exact(ep,args['final']);expected=L.select_doc(ep,base,args,self.initial)
            for key in ('tracks','sources','duration','canvas','clips','caption_edit','release_inputs','release_selection'):
                if doc.get(key)!=expected.get(key):raise ValueError('Select candidate differs from the exact job: '+key)
        else:
            _,base=E.exact(ep,args['keep']);expected=L.short_doc(ep,base,args,self.initial,self.job['id'],materialize=False)
            for key in ('sources','tracks','duration','canvas','clips','caption_edit','release_short','release_selection'):
                if doc.get(key)!=expected.get(key):raise ValueError('Short candidate differs from its exact kept source: '+key)
        V.pinned_caption(self.studio.sp,ep,doc,granted_episode=True)
        for key in ('srt','ass'):
            p=R.regular(ep.root/doc['captions'][key])
            if not p.is_relative_to(R.regular(ep.captions/'native-release'/self.job['id'],directory=True)):raise ValueError('Caption is outside this job grant')

    def _register(self):
        studio=self.studio;job=self.job;args=job['args'];op=job['operation'];directory=L.directory(studio,job)
        if (directory/'result.json').exists():return L.read_result(studio,directory/'result.json')
        ep=R.GrantedEpisode(self.root);row=R.get(studio.sp,args['episode'],args['grant_revision'])
        candidate=read(studio.sp,directory/'candidate.json')
        if candidate.get('operation')!=op or candidate.get('args')!=args or candidate.get('dependencies')!=self.initial:raise ValueError('Release candidate identity changed')
        with R.resolve(studio,args['episode'],args['grant_revision'],_fresh_sources=True) as (verified,verified_row):
            if L.inputs(studio,verified,verified_row,op,args)!=self.initial:raise ValueError('Release dependencies changed before publication')
        if op=='episode-package':
            spec=L.package_spec(studio,ep,row,args,self.initial)
            manifest=candidate['manifest']
            if manifest['input']!=spec:raise ValueError('Package input differs from selected final')
            folder=V.safe_dir(studio.sp,directory/candidate['directory'])
            if folder.parent!=directory:raise ValueError('Package is outside this job')
            for item in manifest['files']:
                path=safe(studio.sp,folder/item['file'])
                if not path.is_relative_to(folder) or sha256_file(path)!=item['sha256']:raise ValueError('Packaged output changed')
            archive=safe(studio.sp,directory/candidate['archive'])
            if archive.parent!=directory or sha256_file(archive)!=candidate['archive_sha256']:raise ValueError('Package archive changed')
            from ..package import exact as package,audio
            import zipfile,hashlib
            plan=package.plan(spec,formats=args['formats'],include_originals=args['originals'])
            expected_files={e['destination'] for e in plan['entries']}|{'captions/programme.vtt','copy/description.txt','chapters.json','credits.txt','index.html'}|{'audio/podcast'+audio.EXT[f] for f in args['formats']}
            if {f['file'] for f in manifest['files']}!=expected_files or len(manifest['files'])!=len(expected_files):raise ValueError('Package contains missing, duplicate or extra outputs')
            for entry in plan['entries']:
                if next(f['sha256'] for f in manifest['files'] if f['file']==entry['destination'])!=entry['sha256']:raise ValueError('Package selected/original bytes differ from exact input')
            if read(studio.sp,folder/'manifest.json')!=manifest or read(studio.sp,folder/'chapters.json')!=spec['chapters']:raise ValueError('Package manifest or chapters differ')
            checks={f['file']:f['sha256'] for f in manifest['files']};checks['manifest.json']=sha256_file(folder/'manifest.json');checks['checksums.sha256']=sha256_file(folder/'checksums.sha256')
            with zipfile.ZipFile(archive) as zipped:
                names=zipped.namelist()
                if len(names)!=len(set(names)) or set(names)!=set(checks):raise ValueError('Package archive scope differs from exact manifest')
                for name in names:
                    digest=hashlib.sha256()
                    with zipped.open(name) as stream:
                        for block in iter(lambda:stream.read(8*1024*1024),b''):digest.update(block)
                    if digest.hexdigest()!=checks[name]:raise ValueError('Package archive bytes differ from verified files')
            result={'episode':row['id'],'kind':'package','manifest':manifest,'dependencies':self.initial,'url':L.V.url(studio.sp,archive),'preview_url':L.V.url(studio.sp,folder/'index.html'),
                    'files':[{'url':L.V.url(studio.sp,folder/f['file'])+'?export=1',**f} for f in manifest['files']], 'sha256':candidate['archive_sha256'],'publication':'not queued or sent','archive':archive.name,'package_directory':folder.name,
                    'manifest_sha256':sha256_file(folder/'manifest.json'),'checksums_sha256':sha256_file(folder/'checksums.sha256')}
        elif op=='episode-handoff':
            from ..handoff import checked_report
            from . import episode_resolve
            checked_report(json.dumps(candidate['report']),returncode=0,expected_name=args['name'])
            if sha256_file(directory/'plan.json')!=candidate['plan_sha256']:raise ValueError('Resolve plan changed')
            _,doc=E.exact(ep,args['final'])
            expected=episode_resolve.plans(ep,doc,args,self.initial,directory,studio)
            if candidate['plan']!=expected or read(studio.sp,directory/'plan.json')!=expected:raise ValueError('Resolve plan differs from captured programme')
            companion=candidate['companion']
            if companion.get('report'):checked_report(json.dumps(companion['report']),returncode=0,expected_name=args['name']+' · source body')
            if companion.get('limitations')!=expected['limitations']:raise ValueError('Source reference limitations changed')
            files=episode_resolve.companion_files(doc,args,directory,required=companion['status']=='created reference timeline') if expected['companion'] else []
            if candidate.get('companion_files')!=files:raise ValueError('Resolve companion audio changed')
            result={'episode':row['id'],'kind':'handoff','input':self.initial['final'],'plan_sha256':candidate['plan_sha256'],'report':candidate['report'],'companion':companion,'companion_files':files}
        else:
            doc=candidate['doc'];self._validate_clip(doc)
            output=safe(studio.sp,directory/candidate['filename'])
            if output.parent!=directory or sha256_file(output)!=candidate['sha256']:raise ValueError('Clip output changed')
            probe=media.probe_file(output,studio.sp.root)
            if abs(probe['duration']-doc['duration'])>.1 or bool(probe['video'])!=(editdoc.effective_mode(doc)!='audio'):raise ValueError('Clip output does not match the selected programme')
            caption=candidate['caption'];cap=output.with_suffix('.srt')
            if caption['filename']!=cap.name or sha256_file(cap)!=caption['sha256']:raise ValueError('Clip caption changed')
            parents=([self.initial['artifact']['details']['asset']] if op=='episode-select' else [E.artifact(studio,row['id'],args['keep']['artifact_id'])['details']['asset']])
            asset=library.add_derivative(studio.sp,output,derived_from=parents,origin={'schema':'episode_release_render.v1','operation':op,'args':args,'dependencies':self.initial,'caption':caption},by='episode-workbench')
            output_sha=sha256_file(output)
            self._current()
            with editdoc.mutation_lock(ep):
                self._current();doc.setdefault('history',[]).append({'request_id':args['request_id'],'operation':op,'reason':'Exact release selection; review remains separate','confidence':1.0})
                name='clip_'+R.digest({'request':args['request_id']})[:20]
                path=editdoc.save_new_version(ep,doc,name=name,request_id=args['request_id'],locked=True)
                identity={'stage':doc['stage'],'name':name,'version':int(editdoc._V.match(path.name)['n']),'sha256':sha256_file(path)}
                art=E.register_artifact(studio,row,output,kind='select' if op=='episode-select' else 'short',edit=identity,
                    details={'asset':asset['id'],'mode':editdoc.effective_mode(doc),'report':candidate['report'],'caption':caption,'selection':self.initial.get('selection'),'needs_review':True},verified_sha256=output_sha,before_publish=self._current)
                result={'episode':row['id'],'edit':identity,'artifact':art,'review':'needs review'}
        self._current();L.write_result(studio,directory/'result.json',result)
        return result
