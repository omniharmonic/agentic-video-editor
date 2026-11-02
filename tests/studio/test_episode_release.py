"""Portable release mechanics; native authority is explicitly simulated."""
import copy
import json
import uuid
from pathlib import Path
import pytest
from ave import editdoc
from ave.studio import episode_registry as R,episode_workshop as E,episode_release as L
from ave.studio.episode_release_bridge import ReleaseParent
from ave.studio.workbench import write,job_path
from ave.publish.safety import sha256_file
from .test_workbench import studio
from .test_episode_workshop import ready,run,confirm


def finished(studio,tmp_path,video=False):
    if video:
        from .test_episode_workshop import gap_episode,FakeAuthority
        from ave.studio.ui.api import StudioApp
        eid,original=gap_episode(studio,tmp_path);app=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority())
    else:eid,original,app=ready(studio,tmp_path)
    assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',a)
    rough=run(studio,eid,'episode-rough',assembly=a,policy='gentle',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
    sequence=run(studio,eid,'episode-sequence',rough=r,show='Synthetic show',episode_title='Release',number='',hosts=[],cold_open=None,opening={'enabled':False,'components':[]},closing={'enabled':False,'components':[]})
    confirm(app,eid,'sequence',sequence['review_payload'])
    final=run(studio,eid,'episode-final',rough=r,sequence=sequence['review_payload'],gain_db=0,caption_style='podcast',burn=False)
    selected={**final['edit'],'artifact_id':final['artifact']['id']};confirm(app,eid,'final',selected)
    return eid,original,app,selected


def release_job(studio,eid,op,**extras):
    with R.resolve(studio,eid,verify_sources=False) as (ep,row):
        args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,op,'main'),'request_id':str(uuid.uuid4()),'session':'main',**extras}
    E.validate_operation(studio.sp,op,args)
    job={'id':'job_'+uuid.uuid4().hex[:24],'operation':op,'args':args,'status':'queued','by':'synthetic'}
    job['episode_descriptor']=E.descriptor(studio,op,args);write(studio.sp,job_path(studio.sp,job['id']),job)
    return job


def test_exact_select_keep_revoke_and_source_history(studio,tmp_path,synthetic_final_asr):
    eid,original,app,final=finished(studio,tmp_path);before=sha256_file(original)
    proposal=L.candidates(studio,eid,'main');assert proposal['final']==final and proposal['candidates']
    assert all(c['occurrences'] and all(w['word_id'] and w['occurrence_id'] for w in c['occurrences']) for c in proposal['candidates'])
    job=release_job(studio,eid,'episode-select',final=final,start=0,end=1.5,title='A complete hello')
    bridge=ReleaseParent(studio,job)
    try:
        directory=L.directory(studio,job);directory.mkdir(parents=True)
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):L.execute_clip(studio,ep,row,job,bridge.initial,directory)
        result=bridge.handle({'job_id':job['id'],'action':'episode-select-register','data':{}})
        assert result['artifact']['kind']=='select'
        payload={**result['edit'],'artifact_id':result['artifact']['id'],'complete_statement':True,'context_reviewed':True}
        confirm(app,eid,'keep',payload,session=result['edit']['name'])
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):
            assert E.C.required(studio,ep,row,'keep',result['edit']['name'])['payload']==payload
            _,doc=E.exact(ep,result['edit']);assert doc['sources'] and doc['clips'] and doc['release_inputs']['final']==final
        assert bridge.handle({'job_id':job['id'],'action':'episode-select-status','data':{}})==result
        from ave import library
        from ave.studio import video_workshop as V
        from ave.studio.workbench import Workbench
        for source in doc['sources'].values():library.attest_license(studio.sp,source['parent'],'simulated fixture')
        wb=Workbench(studio)
        try:
            descriptor=next(k for k in V.kept_clips(studio.sp)['clips'] if k['source_id']==eid)
            descriptor.pop('title');kept=wait_job(wb,wb.start_media('video-kept',{**descriptor,'request_id':'native-kept-transfer'}))
            assert kept['doc']['kept_clip']['native'] and kept['sources'][0]['url']
            rendered=wait_job(wb,wb.start_media('video-render',{'project':kept['id'],'version':kept['version'],'sha256':kept['sha256'],'preset':'wav','request_id':'native-kept-render'}))
            assert rendered['assets'][0]['kind']=='audio' and rendered['native_keep']['source_id']==eid
        finally:wb.close()
        with pytest.raises(ValueError,match='scope'):bridge.handle({'job_id':job['id'],'action':'episode-select-register','data':{'file':'outside'}})
        prepared=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':1,'kind':'keep','session':result['edit']['name'],'payload':payload,'decision':'revoke'})
        req=prepared['request'];app.episode_checkpoints.view({'token':prepared['token'],'payload_sha256':req['payload_sha256'],'preview_sha256':None});app.episode_checkpoints.prepared[prepared['token']]['viewed']-=3
        app.episode_checkpoints.confirm_native(prepared['token'],'123456')
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):
            with pytest.raises(ValueError,match='revoked'):E.C.required(studio,ep,row,'keep',result['edit']['name'])
        assert sha256_file(original)==before
    finally:bridge.close();app.close()


def test_real_worker_exact_audio_package_and_bound_chapters(studio,tmp_path,synthetic_final_asr):
    from ave.studio.workbench import Workbench
    import time,subprocess
    from ave.ffutil import _bin
    eid,original,app,final=finished(studio,tmp_path);before=sha256_file(original)
    with R.resolve(studio,eid,verify_sources=False) as (ep,row):
        args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,'episode-package','main'),'request_id':str(uuid.uuid4()),'session':'main','final':final,
              'chapters':[{'start':0,'title':'Opening'},{'start':1,'title':'Second occurrence'}],'formats':['mp3','aac'],'originals':True,'copy':'A synthetic release.'}
        # Unrelated newer-looking render/caption files must never become inputs.
        (ep.exports/'final.v999.mp4').write_bytes(b'not selected');(ep.captions/'final.srt').write_text('unrelated mutable captions')
    wb=Workbench(studio)
    try:
        job=wb.start_media('episode-package',args);deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            current=wb.tail(job['id'])
            if current['status'] not in ('queued','running'):break
            time.sleep(.05)
        assert current['status']=='done',current.get('diagnostics')
        result=current['result'];manifest=result['manifest'];assert manifest['input']['binding']['edit']==final
        assert manifest['input']['chapters']['chapters']==args['chapters'];assert len(manifest['audio'])==2
        root=L.directory(studio,job)/'package';original_file=next(p for p in root.joinpath('originals').iterdir())
        assert sha256_file(original_file)==before
        for encoded in manifest['audio']:
            path=root/encoded['file'];probe=json.loads(subprocess.check_output([_bin('ffprobe'),'-v','error','-show_chapters','-show_streams','-of','json',str(path)]))
            assert sum(s['codec_type']=='audio' for s in probe['streams'])==1
            assert not any(s['codec_type']=='video' for s in probe['streams'])  # AAC chapter metadata may be a data stream.
            assert [round(float(c['start_time']),3) for c in probe['chapters']]==[0,1]
            assert encoded['loudness']['ok']
        assert sha256_file(original)==before
        # Playback and export are different actions; both are bound to owned
        # package members, while exporting also rechecks current eligibility.
        index=root/'index.html';saved=index.read_bytes()
        assert L.downloadable(studio,index,index_policy=True)
        assert L.downloadable(studio,L.directory(studio,job)/result['archive'])
        rogue=root/'unlisted.html';rogue.write_text('<script>parent.compromised=true</script>')
        with pytest.raises(ValueError,match='outside'):L.downloadable(studio,rogue,index_policy=True)
        index.write_bytes(saved+b'<script>parent.compromised=true</script>')
        with pytest.raises(ValueError,match='outside'):L.downloadable(studio,index,index_policy=True)
        index.write_bytes(saved)
        saved_manifest=(root/'manifest.json').read_bytes()
        (root/'manifest.json').write_bytes(saved_manifest+b' ')
        with pytest.raises(ValueError,match='manifest changed'):L.downloadable(studio,original_file)
        (root/'manifest.json').write_bytes(saved_manifest)
        prepared=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':1,'kind':'final','session':'main','payload':final,'decision':'revoke'})
        req=prepared['request'];app.episode_checkpoints.view({'token':prepared['token'],'payload_sha256':req['payload_sha256'],'preview_sha256':None});app.episode_checkpoints.prepared[prepared['token']]['viewed']-=3
        app.episode_checkpoints.confirm_native(prepared['token'],'123456')
        assert L.recover_committed(studio,job)['recovery']['committed_earlier']
        assert L.downloadable(studio,index,index_policy=True) # saved historical playback
        with pytest.raises(ValueError,match='revoked'):L.downloadable(studio,L.directory(studio,job)/result['archive'])
        with pytest.raises(ValueError,match='revoked'):L.downloadable(studio,original_file,export=True)
        R.revoke(studio,eid)
        with pytest.raises(ValueError):L.downloadable(studio,index,index_policy=True)
    finally:wb.close();app.close()


def wait_job(wb,job,timeout=60):
    import time
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        current=wb.tail(job['id'])
        if current['status'] not in ('queued','running'):
            assert current['status']=='done',current.get('diagnostics')
            return current['result']
        time.sleep(.05)
    raise AssertionError('Job exceeded fixture deadline: '+str(current))


def test_adopt_reordered_repeated_programme_requires_fresh_review(studio,tmp_path,synthetic_final_asr):
    from ave import library
    from ave.episode import write_json
    from ave.studio import video_workshop as V
    from ave.studio.workbench import Workbench
    from ave.studio.ui.api import StudioApp
    from ave.occurrences import mapped_words
    from .test_video_workshop import mutate
    from .test_episode_workshop import recording,FakeAuthority
    original=recording(tmp_path);rec=library.add_file(studio.sp,original,source={'type':'own'},by='synthetic')
    p=V.create(studio,{'title':'Reordered conversation','mode':'audio','request_id':'adoption-programme'})
    p=V.add_source(studio,{'project':p['id'],'source_id':rec['id'],'expected_revision':1,'request_id':'adoption-source'})
    ep,meta=V.project(studio.sp,p['id']);tp=ep.work/'synthetic-words.json'
    write_json(tp,{'words':[{'w':'First.','start':.2,'end':.5},{'w':'Second.','start':2.2,'end':2.5}]})
    meta['project_sources'][rec['id']]['transcript']={'file':ep.rel(tp),'sha256':sha256_file(tp),'clock':'source'};write_json(ep.meta_path,meta)
    for a,b in [(2,3),(0,1),(0,1)]:p=mutate(studio,p,{'kind':'insert','source':rec['id'],'in':a,'out':b})
    library.attest_license(studio.sp,rec['id'],'simulated fixture')
    p=mutate(studio,p,{'kind':'music','asset':rec['id'],'gain_db':-20,'duck_db':-12,'fade_in_s':.2,'fade_out_s':.2})
    expected=mapped_words(ep,p['doc']);expected_music=copy.deepcopy(p['doc']['tracks']['music']);wb=Workbench(studio);app=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority())
    try:
        args={'project':p['id'],'version':p['version'],'sha256':p['sha256'],'revision':p['revision'],'title':'Adopted episode','request_id':'adoption-test'}
        job=wb.start_media('episode-adopt',args);result=wait_job(wb,job);eid=result['episode']
        with R.resolve(studio,eid) as (target,row):
            imported=R.json_file(editdoc.path_for(target,'assembly',1,'main'))
            assert mapped_words(target,imported)==expected
            assert not E.C.events(studio) or not any(r.get('episode')==eid for r in E.C.events(studio))
            assert row['adoption']['source_edit_sha256']==p['sha256']
        assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']}
        confirm(app,eid,'assembly',a)
        rough=run(studio,eid,'episode-rough',assembly=a,policy='gentle',restore=[])
        r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
        sequence=run(studio,eid,'episode-sequence',rough=r,show='Adoption',episode_title='Repeated words',number='',hosts=[],cold_open=None,opening={'enabled':False,'components':[]},closing={'enabled':False,'components':[]})
        confirm(app,eid,'sequence',sequence['review_payload'])
        final=run(studio,eid,'episode-final',rough=r,sequence=sequence['review_payload'],gain_db=0,caption_style='podcast',burn=False)
        confirm(app,eid,'final',{**final['edit'],'artifact_id':final['artifact']['id']})
        proposals=L.candidates(studio,eid,'main')
        assert [c['text'] for c in proposals['candidates']]==['Second.','First.','First.']
        repeated=[c['occurrences'][0] for c in proposals['candidates'][1:]]
        assert repeated[0]['word_id']==repeated[1]['word_id'] and repeated[0]['occurrence_id']!=repeated[1]['occurrence_id']
        with R.resolve(studio,eid) as (target,row):
            doc=E.exact(target,final['edit'])[1];assert [w['w'] for w in mapped_words(target,doc)]==['Second.','First.','First.']
            assert len(doc['tracks']['music'])==1
            assert {k:v for k,v in doc['tracks']['music'][0].items() if k not in ('file','fade_out_remaining_s')}=={k:v for k,v in expected_music[0].items() if k!='file'}
            from ave.compile import compile_audio
            import soundfile as sf,numpy as np
            before=tmp_path/'before-final.wav';after=tmp_path/'after-final.wav'
            compile_audio(target,E.exact(target,r)[1],before);compile_audio(target,doc,after)
            assert np.array_equal(sf.read(before)[0],sf.read(after)[0])
            # Repeated source words are different programme occurrences. A
            # chapter cannot resolve the second occurrence by text/source time.
            art=E.artifact(studio,eid,final['artifact']['id'])
            words=[w for cue in art['details']['caption']['cues'] for w in cue.get('words',[])]
            repeated_words=[w for w in words if w['w']=='First.'];assert len(repeated_words)==2
            chapters=[{'start':w['start'],'title':'Occurrence '+str(i+1),'locator':{k:w[k] for k in ('occurrence_id','transcript_sha256','word_id')}} for i,w in enumerate(repeated_words)]
            package_args={'session':'main','final':{**final['edit'],'artifact_id':final['artifact']['id']},'chapters':chapters,'formats':['aac'],'originals':False,'copy':''}
            bound=L.inputs(studio,target,row,'episode-package',package_args)['chapters']
            assert bound['chapters']==chapters and bound['binding']['edit']==package_args['final']
            from ave.studio.episode_transfer import closure
            wrong_preview=copy.deepcopy(doc);next(iter(wrong_preview['sources'].values()))['proxy']='work/not-in-adopted-closure.mp4'
            with pytest.raises(ValueError,match='outside its exact registered closure'):closure(studio,target,wrong_preview,row)
            stale=copy.deepcopy(package_args);stale['chapters'][1]['locator']=chapters[0]['locator']
            with pytest.raises(ValueError,match='occurrence changed'):L.inputs(studio,target,row,'episode-package',stale)
            package_args.update(episode=eid,grant_revision=row['grant_revision'],input_sha256=E.state_hash(target,row,'episode-package','main'),request_id='adopted-repeat-package')
        packaged=wait_job(wb,wb.start_media('episode-package',package_args))
        manifest=packaged['manifest']
        assert manifest['input']['chapters']['chapters']==chapters
        assert wait_job(wb,wb.start_media('episode-adopt',args))['episode']==eid
    finally:wb.close();app.close()



@pytest.mark.real_motion
def test_actual_portrait_title_caption_recut_and_video_package(studio,tmp_path,synthetic_final_asr,monkeypatch):
    from .test_workbench import BROWSER_CACHE
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',BROWSER_CACHE)
    from ave.studio.workbench import Workbench
    from ave import media
    from ave.config import binary
    import subprocess,numpy as np
    eid,original,app,final=finished(studio,tmp_path,video=True);before=sha256_file(original);wb=Workbench(studio)
    def start(op,**extra):
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):
            args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,op,'main'),'request_id':str(uuid.uuid4()),'session':'main',**extra}
        return wait_job(wb,wb.start_media(op,args),timeout=120)
    try:
        with R.resolve(studio,eid) as (ep,row):duration=E.exact(ep,final)[1]['duration']
        select=start('episode-select',final=final,start=0,end=duration,title='Two complete thoughts')
        keep={**select['edit'],'artifact_id':select['artifact']['id'],'complete_statement':True,'context_reviewed':True};confirm(app,eid,'keep',keep,session=select['edit']['name'])
        short=start('episode-short',keep=keep,start=0,end=duration,title='A second perspective',layout='crop',captions=True,reframe=None)
        path=studio.sp.root/E.artifact(studio,eid,short['artifact']['id'])['file'];probe=media.probe_file(path,studio.sp.root)
        assert (probe['video']['width'],probe['video']['height'])==(1080,1920)
        with R.resolve(studio,eid) as (ep,row):
            doc=E.exact(ep,short['edit'])[1];assert any(o.get('template')=='short-title' for o in doc['tracks']['overlays'])
            assert doc['captions']['burn'] and len(doc['clips'])>=2
            assert 'First.' in (ep.root/doc['captions']['srt']).read_text()
        pixels=subprocess.check_output([str(binary('ffmpeg')),'-v','error','-ss','0.5','-i',str(path),'-frames:v','1','-vf','scale=108:192','-pix_fmt','rgb24','-f','rawvideo','-'])
        assert len(pixels)==108*192*3 and np.std(np.frombuffer(pixels,np.uint8))>20
        pcm=subprocess.check_output([str(binary('ffmpeg')),'-v','error','-i',str(path),'-vn','-ac','1','-ar','48000','-f','f32le','-'])
        assert abs(len(pcm)/4/48000-duration)<.05 and np.std(np.frombuffer(pcm,np.float32))>.001
        pack=start('episode-package',final=final,chapters=[{'start':0,'title':'First'},{'start':2,'title':'Second'}],formats=['mp3'],originals=False,copy='Synthetic video release')
        assert pack['manifest']['input']['mode']=='audiovisual'
        assert next(f for f in pack['files'] if f['role']=='episode_video')['sha256']==E.artifact(studio,eid,final['artifact_id'])['sha256']
        assert sha256_file(original)==before
    finally:wb.close();app.close()


@pytest.mark.parametrize('after',['cancel','revoke','closed-parent-retry'])
def test_release_last_output_hash_refuses_current_lifecycle_change(studio,tmp_path,synthetic_final_asr,monkeypatch,after):
    from ave.studio import episode_release_bridge as B
    eid,original,app,final=finished(studio,tmp_path)
    job=release_job(studio,eid,'episode-select',final=final,start=0,end=1.5,title='Hello')
    parent=ReleaseParent(studio,job);folder=L.directory(studio,job);folder.mkdir(parents=True)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):L.execute_clip(studio,ep,row,job,parent.initial,folder)
        target=folder/json.loads((folder/'candidate.json').read_text())['filename'];real=B.sha256_file;calls=0
        def changed(path):
            nonlocal calls
            value=real(path)
            if Path(path)==target:
                calls+=1
                if calls==2:
                    if after=='cancel':job_path(studio.sp,job['id']).with_suffix('.cancel').write_text('cancel')
                    elif after=='revoke':R.revoke(studio,eid)
                    else:
                        parent.close();write(studio.sp,job_path(studio.sp,job['id']),{**job,'status':'running'})
            return value
        monkeypatch.setattr(B,'sha256_file',changed)
        with pytest.raises(ValueError,match='cancel|grant|revoked|parent closed'):
            parent.handle({'job_id':job['id'],'action':'episode-select-register','data':{}})
        assert calls==2 and target.exists() and not (folder/'result.json').exists()
    finally:parent.close();app.close()


def test_cancelled_committed_result_recovers_without_revoked_raw_access(studio,tmp_path,synthetic_final_asr,monkeypatch):
    from ave.studio.workbench import Workbench
    eid,original,app,final=finished(studio,tmp_path)
    job=release_job(studio,eid,'episode-select',final=final,start=0,end=1.5,title='Committed hello')
    parent=ReleaseParent(studio,job);folder=L.directory(studio,job);folder.mkdir(parents=True)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):L.execute_clip(studio,ep,row,job,parent.initial,folder)
        result=parent.handle({'job_id':job['id'],'action':'episode-select-register','data':{}})
        parent.close();R.revoke(studio,eid)
        job.update(status='cancelled',diagnostics='cancelled after publication');write(studio.sp,job_path(studio.sp,job['id']),job)
        job_path(studio.sp,job['id']).with_suffix('.cancel').write_text('cancel')
        monkeypatch.setattr(R,'resolve',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('reopened revoked raw grant')))
        wb=Workbench(studio)
        try:
            recovered=wb.retry({'job_id':job['id']})
            assert recovered['status']=='done' and recovered['result']['artifact']==result['artifact']
            assert recovered['result']['recovery']['current_eligibility']=='not re-established'
            assert job_path(studio.sp,job['id']).with_suffix('.cancel').exists()
            wrong={**job,'id':'job_'+'f'*24};write(studio.sp,job_path(studio.sp,wrong['id']),wrong)
            other=L.directory(studio,wrong);other.mkdir();(other/'result.json').write_bytes((folder/'result.json').read_bytes())
            with pytest.raises(ValueError,match='different job'):L.recover_committed(studio,wrong)
        finally:wb.close()
    finally:parent.close();app.close()


def test_final_output_quality_is_reviewed_and_package_selects_that_version(studio,tmp_path,synthetic_final_asr):
    from ave import media
    from ave.studio import episode_view as View
    from ave.studio.workbench import Workbench
    eid,original,app,first=finished(studio,tmp_path,video=True);wb=Workbench(studio)
    try:
        with R.resolve(studio,eid) as (ep,row):spec=E.exact(ep,first)[1]['final_spec']
        selected=[]
        for preset,height in [('episode_draft',540),('episode_hd',1080)]:
            result=run(studio,eid,'episode-final',rough=spec['rough'],sequence=spec['sequence'],gain_db=0,caption_style='podcast',burn=True,preset=preset)
            current={**result['edit'],'artifact_id':result['artifact']['id']}
            assert View.load(studio,eid)['checkpoints']['final']['state']=='needs review'
            with R.resolve(studio,eid,verify_sources=False) as (ep,row):
                args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,'episode-package','main'),'request_id':str(uuid.uuid4()),'session':'main','final':current,'chapters':[],'formats':['aac'],'originals':False,'copy':'Selected output quality'}
            with pytest.raises(ValueError):L.validate(studio,'episode-package',args)
            owned=E.artifact(studio,eid,result['artifact']['id']);probe=media.probe_file(studio.sp.root/owned['file'],studio.sp.root)
            assert (probe['video']['width'],probe['video']['height'])==(height*16//9,height)
            confirm(app,eid,'final',current);selected.append(current)
        packaged=wait_job(wb,wb.start_media('episode-package',args))
        assert packaged['manifest']['input']['binding']['edit']==selected[-1]
        video=next(f for f in packaged['manifest']['files'] if f['file'].endswith('.mp4'))
        assert video['sha256']==result['artifact']['sha256']
        with pytest.raises(ValueError):L.validate(studio,'episode-package',{**args,'final':selected[0],'request_id':str(uuid.uuid4())})
    finally:wb.close();app.close()
