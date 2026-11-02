"""Portable generated media; authority/bookmarks are explicitly injected fakes."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import time
import uuid
import os
import numpy as np
import pytest
import soundfile as sf
from ave import library, editdoc
from ave.episode import Episode, write_json
from ave.publish.safety import sha256_file
from ave.studio import episode_registry as R, episode_workshop as E, episode_checkpoints as C
from ave.studio.ui.api import StudioApp
from .test_workbench import studio, real_browser_cache

FINAL_REPLAY=os.getenv('AVE_EPISODE_REPLAY')

class FakeAuthority:
    active=True
    simulated=True
    def require(self):
        if not self.active:raise ValueError('simulated native authority inactive')

class FakeBooks:
    def capture(self,path):
        path=Path(path)
        return {'bookmark':str(path),'identity':{'volume':'SIMULATED-volume','resource':str(path.stat().st_ino)}}
    @contextmanager
    def access(self,binding):
        p=Path(binding['bookmark'])
        if not p.exists() or self.capture(p)['identity']!=binding['identity']:raise ValueError('SIMULATED offline or replaced root')
        yield p


def recording(tmp_path,name='recording.wav',duration=4):
    p=tmp_path/name;sr=48000;t=np.arange(round(sr*duration))/sr
    # Deterministic envelope and two distinct channels, no private fixture path.
    signal=.22*np.sin(2*np.pi*317*t)*(0.6+0.4*np.sin(2*np.pi*2*t)**2)
    sf.write(p,np.column_stack([signal,signal*.7]),sr,subtype='PCM_24');return p


def managed(studio,tmp_path):
    source=recording(tmp_path);asset=library.add_file(studio.sp,source,source={'type':'own'},by='synthetic',attested=False)
    result=E.create(studio,{'title':'Synthetic conversation','sources':[asset['id']],'request_id':str(uuid.uuid4())})
    return result['episode'],source


def run(studio,eid,op,**extra):
    with R.resolve(studio,eid) as (ep,row):args={'episode':eid,'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(ep,row,op,'main'),'request_id':str(uuid.uuid4()),'session':'main',**extra}
    job={'id':'job_'+uuid.uuid4().hex[:24],'operation':op,'args':args,'by':'synthetic'};job['episode_descriptor']=E.descriptor(studio,op,args)
    E.validate_operation(studio.sp,op,args)
    return E.execute(studio,job)


def seed_transcript(studio,eid):
    with R.resolve(studio,eid) as (ep,row):
        source=row['sources'][0]
        write_json(ep.artifact('transcript.json'),{'source':source['file'],'duration':4,'engine':'generated-ground-truth','words':[{'w':'Hello','start':.3,'end':.7,'speaker':'S1'},{'w':'world.','start':1,'end':1.5,'speaker':'S2'}],'utterances':[{'speaker':'S1','start':.3,'end':.7,'text':'Hello'},{'speaker':'S2','start':1,'end':1.5,'text':'world.'}]})
    return source


def confirm(app,eid,kind,payload,session='main'):
    row=R.get(app.studio.sp,eid)
    result=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':row['grant_revision'],'kind':kind,'session':session,'payload':payload})
    req=result['request'];app.episode_checkpoints.view({'token':result['token'],'payload_sha256':req['payload_sha256'],'preview_sha256':req['dependencies'].get('preview_sha256')})
    app.episode_checkpoints.prepared[result['token']]['viewed']-=3 # injected elapsed-view fixture, not a human claim
    return app.episode_checkpoints.confirm_native(result['token'],'123456')


def ready(studio,tmp_path):
    eid,original=managed(studio,tmp_path);run(studio,eid,'episode-intake');source=seed_transcript(studio,eid)
    app=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority())
    confirm(app,eid,'speakers',{'names':{'S1':'Synthetic host','S2':'Synthetic guest'}})
    run(studio,eid,'episode-sync',source_id=source['id'],points=5)
    confirm(app,eid,'mapping',{'reference':source['id'],'sources':[source['id']],'cameras':{}})
    confirm(app,eid,'bounds',{'in':.25,'out':3.75})
    return eid,original,app


def test_actual_audio_assembly_exact_review_and_restart(studio,tmp_path):
    eid,original,app=ready(studio,tmp_path);before=sha256_file(original)
    result=run(studio,eid,'episode-assembly');assert result['review']=='needs review'
    render=E.artifact(studio,eid,result['artifact']['id']);path=studio.sp.root/render['file']
    from ave.media import probe_file
    probe=probe_file(path,studio.sp.root);assert probe['audio'] and not probe['video'];assert abs(probe['duration']-3.5)<.05
    with R.resolve(studio,eid) as (ep,row):
        p,doc=E.exact(ep,result['edit']);assert editdoc.effective_mode(doc)=='audio';assert not doc['tracks']['video'];assert doc['tracks']['dialogue'][0]['e0']==.25
    confirm(app,eid,'assembly',{**result['edit'],'artifact_id':render['id']})
    assert E.load(studio,eid)['checkpoints']['assembly']['state']=='reviewed exact'
    restarted=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority())
    assert E.load(restarted.studio,eid)['checkpoints']['assembly']['state']=='reviewed exact'
    with R.resolve(studio,eid) as (ep,row):
        renewed={**row,'grant_revision':row['grant_revision']+1}
        assert C.status(studio,ep,renewed,C.latest(studio,eid,'assembly','main'))['state']=='stale'
    confirm(app,eid,'bounds',{'in':.5,'out':3.5})
    assert E.load(studio,eid)['checkpoints']['assembly']['state']=='stale'
    assert sha256_file(original)==before
    app.close();restarted.close()


def test_authority_wrong_pin_changed_target_and_forged_fields(studio,tmp_path):
    eid,original,app=ready(studio,tmp_path)
    body={'episode':eid,'grant_revision':1,'kind':'bounds','session':'main','payload':{'in':0,'out':3}}
    with pytest.raises(Exception):app.episode_checkpoints.prepare({**body,'human':True})
    request=app.episode_checkpoints.prepare(body);token=request['token'];req=request['request']
    app.episode_checkpoints.view({'token':token,'payload_sha256':req['payload_sha256'],'preview_sha256':None});app.episode_checkpoints.prepared[token]['viewed']-=3
    count=len(C.events(studio))
    with pytest.raises(Exception):app.episode_checkpoints.confirm_native(token,'wrong')
    assert len(C.events(studio))==count
    with R.resolve(studio,eid) as (ep,row):
        t=R.json_file(ep.artifact('transcript.json'));t['words'][0]['w']='Changed';write_json(ep.artifact('transcript.json'),t)
    with pytest.raises(ValueError):app.episode_checkpoints.confirm_native(token,'123456')
    assert len(C.events(studio))==count
    app.close()


@pytest.mark.parametrize('layout',['flat','numbered',{'work':'custom/work','edits':'custom/edits','exports':'custom/exports'}])
def test_native_registry_layout_scope_and_offline(studio,tmp_path,layout):
    ep=Episode(tmp_path/'external');ep.create(layout=layout);source=recording(tmp_path);__import__('shutil').copyfile(source,ep.raw/source.name)
    original_meta=ep.meta_path.read_bytes();books=FakeBooks()
    row=R.register_selected(studio,ep.root,derived_consent=True,books=books)
    assert row['ownership']=='external_reference';assert ep.meta_path.read_bytes()==original_meta
    with R.resolve(studio,row['id'],books=books) as (resolved,_):assert resolved.layout==ep.layout
    moved=tmp_path/'disconnected';ep.root.rename(moved)
    assert R.catalog(studio,books=books)['episodes'][0]['status']=='blocked';assert not ep.root.exists()


def test_raw_overlap_symlink_grant_tamper_and_revocation(studio,tmp_path):
    eid,source=managed(studio,tmp_path)
    with R.resolve(studio,eid) as (ep,row):
        meta=R.json_file(ep.meta_path);meta['layout']={'work':'raw/output'};write_json(ep.meta_path,meta)
    with pytest.raises(ValueError,match='overlaps'):E.load(studio,eid)
    with pytest.raises(ValueError):R.get(studio.sp,'/tmp/anything')
    ep.meta_path.write_text(json.dumps({**meta,'layout':'flat'}))
    ep.work.mkdir(exist_ok=True);(ep.work/'alias').symlink_to(source)
    with pytest.raises(ValueError,match='symlink'):E.load(studio,eid)
    (ep.work/'alias').unlink();R.revoke(studio,eid)
    with pytest.raises(ValueError,match='revoked'):E.load(studio,eid)


def test_actual_video_sync_and_assembly(studio,tmp_path):
    import subprocess
    from ave.config import binary
    from .video_fixtures import picture
    original=picture(tmp_path/'camera-original.mov',seconds=8,width=320,height=180,timecode=True)
    camera=tmp_path/'camera.mov'
    subprocess.run([str(binary('ffmpeg')),'-v','error','-y','-ss','0.75','-i',str(original),'-c:v','prores_ks','-c:a','pcm_s24le',str(camera)],check=True)
    recorder=original.with_suffix('.wav');assets=[library.add_file(studio.sp,p,source={'type':'own'},by='synthetic',attested=False) for p in (recorder,camera)]
    eid=E.create(studio,{'title':'Synthetic camera alignment','sources':[a['id'] for a in assets],'request_id':str(uuid.uuid4())})['episode']
    hashes={str(p):sha256_file(p) for p in (recorder,camera)};run(studio,eid,'episode-intake');source=seed_transcript(studio,eid)
    app=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority());confirm(app,eid,'speakers',{'names':{'S1':'Host','S2':'Guest'}})
    result=run(studio,eid,'episode-sync',source_id=source['id'],points=3)
    with R.resolve(studio,eid) as (ep,row):
        camera_source=row['sources'][1];clock=result['sync']['files'][camera_source['file']]
        assert abs(clock['start_offset']-.75)<.03
        # Preserve measured offset plus an explicit synthetic clock-knot fixture.
        sync=R.json_file(ep.artifact('sync.json'));sync['files'][camera_source['file']]['sync_map']=[[0,.75],[3,.75024],[7.25,.75058]];write_json(ep.artifact('sync.json'),sync)
        sources=row['sources']
    count=len(C.events(studio))
    with pytest.raises(ValueError,match='visible speaker'):
        confirm(app,eid,'mapping',{'reference':source['id'],'sources':[s['id'] for s in sources],'cameras':{camera_source['id']:{'role':'speaker','speakers':[]}}})
    assert len(C.events(studio))==count
    confirm(app,eid,'mapping',{'reference':source['id'],'sources':[s['id'] for s in sources],'cameras':{camera_source['id']:{'role':'speaker','speakers':['S1','S2']}}})
    confirm(app,eid,'bounds',{'in':1,'out':3.75})
    result=run(studio,eid,'episode-assembly');rec=E.artifact(studio,eid,result['artifact']['id']);out=studio.sp.root/rec['file']
    with R.resolve(studio,eid) as (ep,row):
        p,doc=E.exact(ep,result['edit']);assert doc['sources'][camera_source['id']]['sync_map']==[[0,.75],[3,.75024],[7.25,.75058]]
        assert abs(editdoc.src_time(doc['sources'][camera_source['id']],1)-.24998)<1e-4
        from ave.compile import compile_audio
        output=tmp_path/'pre-normalization.wav';compile_audio(ep,doc,output)
        got,sr=sf.read(output);reference,_=sf.read(recorder);expected=reference[48000:180000]
        assert got.shape==expected.shape;assert np.max(abs(got-expected))<2e-6
    raw=subprocess.check_output([str(binary('ffmpeg')),'-v','error','-i',str(out),'-frames:v','1','-vf','scale=1:1','-pix_fmt','rgb24','-f','rawvideo','-'])
    assert abs(raw[0]-24)<10
    confirm(app,eid,'assembly',{**result['edit'],'artifact_id':rec['id']});assert E.load(studio,eid)['checkpoints']['assembly']['state']=='reviewed exact'
    assert hashes=={str(p):sha256_file(p) for p in (recorder,camera)};app.close()


def test_revision_history_no_transfer_and_retry(studio,tmp_path):
    eid,source,app=ready(studio,tmp_path)
    first=run(studio,eid,'episode-assembly');confirm(app,eid,'assembly',{**first['edit'],'artifact_id':first['artifact']['id']})
    second=run(studio,eid,'episode-assembly');assert second['edit']['version']==2
    state=E.load(studio,eid);assert state['checkpoints']['assembly']['state']=='needs review';assert state['checkpoints']['assembly']['prior_review']['payload']['version']==1
    with R.resolve(studio,eid) as (ep,row):
        assert len(editdoc.list_versions(ep,'assembly','main'))==2
        doc=R.json_file(editdoc.path_for(ep,'assembly',2,'main'));rid=doc['history'][-1]['request_id']
        args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,'episode-assembly','main'),'request_id':rid,'session':'main'}
    # Interrupted result attachment reuses immutable EditDoc and exact render receipt.
    directory=next((studio.sp.work/'production'/'episode-renders'/eid).glob('job_*'))
    matching=next(p for p in (studio.sp.work/'production'/'episode-renders'/eid).glob('job_*') if R.json_file(p/'render-receipt.json')['edit']==second['edit'])
    job={'id':matching.name,'operation':'episode-assembly','args':args,'by':'synthetic'};job['episode_descriptor']=E.descriptor(studio,job['operation'],args)
    retry=E.execute(studio,job);assert retry['edit']==second['edit'];assert retry['artifact']['id']==second['artifact']['id']
    app.close()


def test_revocation_and_session_local_dependencies(studio,tmp_path):
    eid,source,app=ready(studio,tmp_path)
    with R.resolve(studio,eid) as (ep,row):mapping=C.latest(studio,eid,'mapping','main')['payload']
    confirm(app,eid,'mapping',mapping,session='second');confirm(app,eid,'bounds',{'in':1,'out':2},session='second')
    confirm(app,eid,'bounds',{'in':.5,'out':3})
    with R.resolve(studio,eid) as (ep,row):assert C.status(studio,ep,row,C.latest(studio,eid,'bounds','second'))['state']=='reviewed exact'
    request=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':1,'kind':'mapping','session':'main','payload':mapping,'decision':'revoke'})
    req=request['request'];app.episode_checkpoints.view({'token':request['token'],'payload_sha256':req['payload_sha256'],'preview_sha256':None});app.episode_checkpoints.prepared[request['token']]['viewed']-=3
    app.episode_checkpoints.confirm_native(request['token'],'123456')
    state=E.load(studio,eid);assert state['checkpoints']['mapping']['state']=='stale';assert state['checkpoints']['bounds']['state']=='stale'
    with pytest.raises(ValueError):run(studio,eid,'episode-assembly')
    app.close()


def test_ledger_tampering_and_legacy_claim_do_not_confirm(studio,tmp_path):
    eid,source,app=ready(studio,tmp_path)
    with R.resolve(studio,eid) as (ep,row):
        meta=R.json_file(ep.meta_path);meta['trim']['main']={'in':0,'out':1,'by':'human'};write_json(ep.meta_path,meta)
        # Editable legacy projection is visible, but does not replace sealed bounds.
        assert C.required(studio,ep,row,'bounds','main')['payload']=={'in':.25,'out':3.75}
    path=studio.sp.ledger_dir/'control.jsonl';original=path.read_bytes();path.write_bytes(original.rsplit(b'\n',2)[0]+b'\n')
    with pytest.raises(ValueError,match='verification'):E.load(studio,eid)
    path.write_bytes(original);app.close()


def test_native_additional_layout_grant_and_reconnect(studio,tmp_path):
    ep=Episode(tmp_path/'custom');ep.create();source=recording(tmp_path);__import__('shutil').copyfile(source,ep.raw/source.name)
    destination=tmp_path/'separate-exports';destination.mkdir();meta=R.json_file(ep.meta_path);meta['layout']={'exports':'../separate-exports'};write_json(ep.meta_path,meta);books=FakeBooks()
    with pytest.raises(ValueError,match='Additional native folder grant required'):R.register_selected(studio,ep.root,derived_consent=True,books=books)
    row=R.register_selected(studio,ep.root,derived_consent=True,books=books,additional={'exports':destination})
    with R.resolve(studio,row['id'],books=books) as (resolved,_):assert resolved.exports.resolve()==destination
    R.revoke(studio,row['id'])
    renewed=R.register_selected(studio,ep.root,derived_consent=True,books=books,additional={'exports':destination},reconnect=row['id'])
    assert renewed['grant_revision']==3
    with R.resolve(studio,row['id'],books=books):pass
    with pytest.raises(ValueError,match='grant changed'):
        with R.resolve(studio,row['id'],revision=1,books=books):pass


def test_audio_session_beyond_one_hour_and_paged_source_clock(studio,tmp_path):
    # Actual sparse PCM recording; no long speech-engine benchmark. Only the
    # selected two-second late span is compiled and numerically inspected.
    p=tmp_path/'long.wav';rate=8000
    with sf.SoundFile(p,'w',samplerate=rate,channels=1,subtype='PCM_16') as out:
        out.write(np.zeros(3601*rate,dtype='float32'))
        t=np.arange(rate*3)/rate;out.write(.2*np.sin(2*np.pi*220*t))
    asset=library.add_file(studio.sp,p,source={'type':'own'},by='synthetic',attested=False)
    eid=E.create(studio,{'title':'Long synthetic recorder','sources':[asset['id']],'request_id':str(uuid.uuid4())})['episode']
    with R.resolve(studio,eid) as (ep,row):
        source=row['sources'][0];from ave.media import probe_file
        write_json(ep.artifact('media.json'),{'files':[probe_file(ep.root/source['file'],ep.root)],'sessions':[]})
        words=[{'w':'early.','start':i*30.,'end':i*30.+1,'speaker':'S1' if i%2 else 'S2'} for i in range(100)]+[{'w':'late.','start':3601,'end':3602,'speaker':'S1'}]
        utterances=[{'text':'Earlier synthetic interval','speaker':'S1','start':i*30.,'end':i*30.+1} for i in range(100)]+[{'text':'Late source interval','speaker':'S1','start':3601,'end':3602}]
        write_json(ep.artifact('transcript.json'),{'source':source['file'],'engine':'generated-ground-truth','duration':3604,'words':words,'utterances':utterances})
        write_json(ep.artifact('sync.json'),{'reference':source['file'],'files':{source['file']:{'start_offset':0.,'drift_ppm':0.}},'warnings':[]})
    app=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority())
    confirm(app,eid,'speakers',{'names':{'S1':'Synthetic late voice','S2':'Synthetic earlier voice'}});confirm(app,eid,'mapping',{'reference':source['id'],'sources':[source['id']],'cameras':{}});confirm(app,eid,'bounds',{'in':3601,'out':3603})
    page=E.transcript_page(studio,eid,source['id'],page=1);assert page['pages']==2;assert page['utterances'][-1]['source_start']==3601
    result=run(studio,eid,'episode-assembly')
    with R.resolve(studio,eid) as (ep,row):
        _,doc=E.exact(ep,result['edit']);assert doc['duration']==2;assert doc['tracks']['dialogue'][0]['e0']==3601
        from ave.compile import compile_audio
        out=tmp_path/'late.wav';compile_audio(ep,doc,out);got,sr=sf.read(out);assert got.shape==(96000,2);assert got.std()>.1
    assert R.policy()['max_session_seconds']>3600;app.close()


def test_scoped_evidence_refuses_external_sidecar_paths_without_writes(studio,tmp_path):
    eid,source=managed(studio,tmp_path);run(studio,eid,'episode-intake')
    with R.resolve(studio,eid) as (ep,row):
        media=R.json_file(ep.artifact('media.json'));media['files'][0]['file']='../../unregistered.wav';write_json(ep.artifact('media.json'),media)
    before=len(R.control(studio.sp).rows())
    with pytest.raises(ValueError,match='unregistered'):E.load(studio,eid)
    assert len(R.control(studio.sp).rows())==before


def test_metadata_sidecar_alias_refuses_registration(studio,tmp_path):
    ep=Episode(tmp_path/'ep');ep.create();source=recording(tmp_path);__import__('shutil').copyfile(source,ep.raw/source.name)
    outside=tmp_path/'outside.json';outside.write_text('unchanged');ep.meta_path.with_suffix('.json.tmp').symlink_to(outside)
    with pytest.raises(ValueError,match='symlink'):R.register_selected(studio,ep.root,derived_consent=True,books=FakeBooks())
    assert outside.read_text()=='unchanged';assert R.records(studio.sp)=={}


def test_offset_source_transcript_contains_only_covered_words(studio,tmp_path):
    eid,source=managed(studio,tmp_path);run(studio,eid,'episode-intake')
    with R.resolve(studio,eid) as (ep,row):
        src=row['sources'][0]
        write_json(ep.artifact('transcript.json'),{'source':src['file'],'duration':10,'engine':{'asr':'fluidaudio'},'words':[
            {'w':'unavailable','start':0,'end':1,'speaker':'S1'},
            {'w':'partial','start':2.8,'end':3.4,'speaker':'S1'},
            {'w':'covered','start':3.5,'end':4,'speaker':'S1'},
            {'w':'last.','start':6,'end':6.5,'speaker':'S1'},
            {'w':'outside','start':7,'end':8,'speaker':'S1'}],'utterances':[{'text':'unavailable partial covered last. outside','start':0,'end':8,'speaker':'S1'}]})
        write_json(ep.artifact('sync.json'),{'reference':src['file'],'files':{src['file']:{'start_offset':3.2,'drift_ppm':0}}})
    result=E.transcript_page(studio,eid,src['id']);words=[w for u in result['utterances'] for w in u['words']]
    assert [w['w'] for w in words]==['covered','last.'];assert words[0]['start']==pytest.approx(.3)
    assert words[0]['transcript_start']==3.5;assert words[0]['word_id'].endswith(':2');assert result['transcript_sha256']==words[0]['transcript_sha256']


@pytest.mark.parametrize('operation',['episode-intake','episode-transcribe','episode-sync'])
@pytest.mark.parametrize('cache_seconds',[.25,4])
def test_adopted_cache_regenerated_before_every_native_consumer(studio,tmp_path,monkeypatch,operation,cache_seconds):
    from ave import audio,media,transcribe
    import shutil
    monkeypatch.setattr(R,'NativeBooks',FakeBooks)
    ep=Episode(tmp_path/'adopted');ep.create();raw=recording(tmp_path);shutil.copyfile(raw,ep.raw/raw.name)
    row=R.register_selected(studio,ep.root,derived_consent=True);eid=row['id'];source=row['sources'][0]
    with R.resolve(studio,eid) as (granted,_):
        info=media.ingest(granted);item=info['files'][0]
        # Adopted metadata/analysis are not trusted just because the files and
        # editable sidecars claim to belong to the newly selected recording.
        item['duration']=.25;item['channel_map']=[{'start':0,'end':4,'use':'left'}];write_json(granted.artifact('media.json'),info)
        cache=audio.analysis_wav_path(granted,source['file']);cache.parent.mkdir(parents=True,exist_ok=True)
        sf.write(cache,np.full(round(16000*cache_seconds),.6),16000,subtype='PCM_16')
        audio._sidecar(cache).write_text('null')
        cache.with_suffix('.native.json').write_text(json.dumps({'source_sha256':source['sha256'],'output_sha256':sha256_file(cache),'verified':True}))
        write_json(granted.artifact('transcript.json'),{'source':source['file'],'duration':4,'engine':'historical generated fixture','words':[{'w':'historical','start':0,'end':1,'speaker':'S1'}]})
        prior_transcript=sha256_file(granted.artifact('transcript.json'))
    legacy=audio.analysis_wav_path(Episode(ep.root),source['file']);legacy.parent.mkdir(parents=True,exist_ok=True)
    sf.write(legacy,np.full(round(16000*cache_seconds),.6),16000,subtype='PCM_16');legacy_sha=sha256_file(legacy)
    wrong_sha=sha256_file(cache);observed=[]
    def actual_cache_asr(wav,*args,**kwargs):
        samples,rate=sf.read(wav);observed.append((sha256_file(wav),len(samples),rate,float(samples.mean()),float(samples.std())))
        return [{'w':'synthetic','start':.2,'end':1.,'conf':1.}]
    monkeypatch.setattr(transcribe,'run_fluidaudio_asr',actual_cache_asr)
    extras={} if operation=='episode-intake' else {'source_id':source['id'],**({'speakers':1} if operation=='episode-transcribe' else {'points':3})}
    result=run(studio,eid,operation,**extras)
    samples,rate=sf.read(cache);assert rate==16000 and len(samples)==64000;assert abs(samples.mean())<.01;assert samples.std()>.05;assert sha256_file(cache)!=wrong_sha
    assert result['analysis'][0]['analysis_sha256']==sha256_file(cache);assert result['analysis'][0]['source']['sha256']==sha256_file(raw)
    assert legacy!=cache and sha256_file(legacy)==legacy_sha
    assert result['analysis'][0]['source_audio_duration']==4;assert result['analysis'][0]['channel_map']!=item['channel_map']
    if operation=='episode-transcribe':assert observed[0][0]==sha256_file(cache) and observed[0][1:3]==(64000,16000)
    else:assert sha256_file(ep.artifact('transcript.json'))==prior_transcript
    assert sha256_file(ep.raw/raw.name)==sha256_file(raw)


def test_reconnect_intake_allocates_new_grant_artifact_preserving_history(studio,tmp_path,monkeypatch):
    import shutil
    monkeypatch.setattr(R,'NativeBooks',FakeBooks)
    ep=Episode(tmp_path/'external');ep.create();raw=recording(tmp_path);shutil.copyfile(raw,ep.raw/raw.name)
    row=R.register_selected(studio,ep.root,derived_consent=True);eid=row['id']
    first=run(studio,eid,'episode-intake')['previews'][0];record=studio.sp.work/'production/episode-artifacts'/(first['id']+'.json');historical=record.read_bytes()
    source=seed_transcript(studio,eid);app=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority())
    confirm(app,eid,'speakers',{'names':{'S1':'First','S2':'Second'}});event=C.latest(studio,eid,'speakers','main')
    renewed=R.register_selected(studio,ep.root,derived_consent=True,reconnect=eid);assert renewed['grant_revision']==2
    assert E.load(studio,eid)['checkpoints']['speakers']['state']=='stale'
    second=run(studio,eid,'episode-intake')['previews'][0];assert second['id']!=first['id'];assert second['sha256']==first['sha256'];assert second['grant_revision']==2
    assert record.read_bytes()==historical;assert C.latest(studio,eid,'speakers','main')==event
    assert E.load(studio,eid)['sources'][0]['preview']['id']==second['id']
    replay=run(studio,eid,'episode-intake')['previews'][0];assert replay['id']==second['id'];app.close()


def gap_episode(studio,tmp_path):
    """Actual cut camera files, explicitly known clock fixture, real assembly."""
    import subprocess
    from ave.config import binary
    from .video_fixtures import picture
    master=picture(tmp_path/'master.mov',seconds=8,width=320,height=180,timecode=True);recorder=master.with_suffix('.wav');cameras=[]
    for name,start in [('first',0),('second',4)]:
        p=tmp_path/(name+'.mov');subprocess.run([str(binary('ffmpeg')),'-v','error','-y','-ss',str(start),'-i',str(master),'-t','2','-c:v','prores_ks','-c:a','pcm_s24le',str(p)],check=True);cameras.append(p)
    assets=[library.add_file(studio.sp,p,source={'type':'own'},by='synthetic',attested=False) for p in [recorder,*cameras]]
    eid=E.create(studio,{'title':'Synthetic camera gap','sources':[a['id'] for a in assets],'request_id':str(uuid.uuid4())})['episode'];run(studio,eid,'episode-intake')
    with R.resolve(studio,eid) as (ep,row):
        sources=row['sources'];words=[{'w':'First.','start':.2,'end':1.4,'speaker':'S1'},{'w':'Omitted gap speech.','start':2.5,'end':3.5,'speaker':'S1'},{'w':'Second.','start':4.2,'end':5.6,'speaker':'S2'}]
        write_json(ep.artifact('transcript.json'),{'source':sources[0]['file'],'duration':8,'engine':'generated-ground-truth','words':words,'utterances':[{'text':w['w'],**{k:w[k] for k in ('start','end','speaker')}} for w in words]})
        write_json(ep.artifact('sync.json'),{'reference':sources[0]['file'],'files':{s['file']:{'start_offset':offset,'drift_ppm':0} for s,offset in zip(sources,[0,0,4])},'warnings':[],'method':'generated known camera cut clocks; not a measured sync claim'})
    app=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority())
    confirm(app,eid,'speakers',{'names':{'S1':'Synthetic first','S2':'Synthetic second'}})
    confirm(app,eid,'mapping',{'reference':sources[0]['id'],'sources':[s['id'] for s in sources],'cameras':{s['id']:{'role':'speaker','speakers':[sid]} for s,sid in zip(sources[1:],['S1','S2'])}})
    confirm(app,eid,'bounds',{'in':0,'out':6});app.close();return eid,recorder


def test_actual_gap_assembly_discloses_episode_output_intervals(studio,tmp_path):
    eid,recorder=gap_episode(studio,tmp_path);original=sha256_file(recorder);result=run(studio,eid,'episode-assembly');assembly=E.load(studio,eid)['assemblies'][-1];coverage=assembly['coverage']
    assert coverage['confirmed_bounds']=={'in':0,'out':6};assert coverage['reference_duration']==8;assert coverage['output_duration']==4
    assert coverage['retained']==[{'episode_start':0,'episode_end':2,'output_start':0,'output_end':2},{'episode_start':4,'episode_end':6,'output_start':2,'output_end':4}]
    assert coverage['excluded_inside_bounds']==[{'start':2,'end':4,'reason':'No selected camera covers this interval'}]
    assert coverage['outside_confirmed_bounds']==[{'start':6,'end':8,'reason':'After confirmed end'}]
    with R.resolve(studio,eid) as (ep,row):
        from ave.compile import compile_audio
        _,doc=E.exact(ep,result['edit']);p=tmp_path/'gap-audio.wav';compile_audio(ep,doc,p);got,_=sf.read(p);raw,_=sf.read(recorder)
        expected=np.concatenate([raw[:96000],raw[192000:288000]]);assert got.shape==expected.shape;assert np.max(abs(got-expected))<2e-6
    assert sha256_file(recorder)==original


@pytest.mark.parametrize('missing_stream_duration',[False,True])
def test_native_analysis_uses_audio_stream_duration_not_picture_extent(studio,tmp_path,monkeypatch,missing_stream_duration):
    import subprocess
    from ave.config import binary
    from .video_fixtures import picture
    original=picture(tmp_path/'eight-second-picture.mov',seconds=8,width=160,height=90)
    camera=tmp_path/'four-second-audio.mov'
    subprocess.run([str(binary('ffmpeg')),'-v','error','-y','-i',str(original),'-c:v','copy','-af','atrim=duration=4','-c:a','pcm_s24le',str(camera)],check=True)
    asset=library.add_file(studio.sp,camera,source={'type':'own'},by='synthetic',attested=False)
    eid=E.create(studio,{'title':'Unequal stream lengths','sources':[asset['id']],'request_id':str(uuid.uuid4())})['episode']
    if missing_stream_duration:
        from ave import media
        original_probe=media._ffprobe
        def without_audio_duration(path):
            info=original_probe(path)
            for stream in info['streams']:
                if stream.get('codec_type')=='audio':stream.pop('duration',None)
            return info
        monkeypatch.setattr(media,'_ffprobe',without_audio_duration)
    result=run(studio,eid,'episode-intake');assert result['analysis'][0]['duration']==4;assert result['analysis'][0]['source_audio_duration']==4
    if missing_stream_duration:assert result['analysis'][0]['duration_basis']=='independently counted decoded source samples'
    assert E.load(studio,eid)['sources'][0]['probe']['duration']==8


@pytest.mark.parametrize('shape',['same-basename','nested-flatten'])
@pytest.mark.parametrize('operation',['episode-intake','episode-transcribe','episode-sync'])
def test_registered_analysis_names_preserve_distinct_sources(studio,tmp_path,monkeypatch,shape,operation):
    import shutil,subprocess
    from ave import audio,media,transcribe,sync
    from ave.config import binary
    monkeypatch.setattr(R,'NativeBooks',FakeBooks)
    ep=Episode(tmp_path/'external');ep.create();rate=48000;t=np.arange(rate*4)/rate
    a=tmp_path/'first.wav';b=tmp_path/'second.wav';sf.write(a,.2*np.sin(2*np.pi*(173*t+17*t*t)),rate,subtype='PCM_24');sf.write(b,.12*np.cos(2*np.pi*(311*t+9*t*t)),rate,subtype='PCM_24')
    if shape=='same-basename':
        first=ep.raw/'take.mov';second=ep.raw/'take.wav'
        subprocess.run([str(binary('ffmpeg')),'-v','error','-y','-f','lavfi','-i','color=s=160x90:r=24:d=4','-i',str(a),'-c:v','prores_ks','-c:a','pcm_s24le','-t','4',str(first)],check=True);shutil.copyfile(b,second)
    else:
        first=ep.raw/'cam/take.wav';second=ep.raw/'cam__take.wav';first.parent.mkdir();shutil.copyfile(a,first);shutil.copyfile(b,second)
    assert audio.analysis_wav_path(ep,ep.rel(first))==audio.analysis_wav_path(ep,ep.rel(second)) # legacy naming retained
    expected={}
    for index,path in enumerate([first,second]):
        out=tmp_path/f'expected-{index}.wav';subprocess.run([str(binary('ffmpeg')),'-v','error','-y','-i',str(path),'-map','0:a:0','-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(out)],check=True);expected[ep.rel(path)]=sf.read(out)[0]
    row=R.register_selected(studio,ep.root,derived_consent=True);eid=row['id']
    with R.resolve(studio,eid) as (granted,_):
        media.ingest(granted);paths={s['file']:audio.analysis_wav_path(granted,s['file']) for s in row['sources']};assert len(set(paths.values()))==2
        assert all(p.parent==granted.work/'audio'/audio.REGISTERED_ANALYSIS_CONTEXT for p in paths.values())
    observed=[]
    def asr(wav,*args,**kwargs):
        samples,_=sf.read(wav);match=[f for f,p in paths.items() if p==wav];assert len(match)==1;np.testing.assert_array_equal(samples,expected[match[0]]);observed.append(match[0]);return [{'w':'Synthetic.','start':.1,'end':1,'conf':1.}]
    monkeypatch.setattr(transcribe,'run_fluidaudio_asr',asr)
    original_pair=sync.sync_pair
    def checked_pair(reference,target,**kwargs):
        for path in [reference,target]:
            file=next(f for f,p in paths.items() if p==path);np.testing.assert_array_equal(sf.read(path)[0],expected[file]);observed.append(file)
        return original_pair(reference,target,**kwargs)
    monkeypatch.setattr(sync,'sync_pair',checked_pair)
    if operation=='episode-transcribe':
        for source in row['sources']:run(studio,eid,operation,source_id=source['id'],speakers=1)
        assert set(observed)==set(paths)
    elif operation=='episode-sync':
        run(studio,eid,operation,source_id=row['sources'][0]['id'],points=3);assert set(observed)==set(paths)
    else:run(studio,eid,operation)
    for file,path in paths.items():np.testing.assert_array_equal(sf.read(path)[0],expected[file])
    assert sha256_file(paths[ep.rel(first)])!=sha256_file(paths[ep.rel(second)])
    assert {s['file']:s['sha256'] for s in row['sources']}=={s['file']:sha256_file(ep.root/s['file']) for s in row['sources']}


def test_rough_from_exact_reviewed_audio_assembly_and_restoration(studio,tmp_path):
    eid,original,app=ready(studio,tmp_path)
    with R.resolve(studio,eid) as (ep,row):
        transcript=R.json_file(ep.artifact('transcript.json'))
        transcript['words'].insert(1,{'w':'um','start':.8,'end':.9,'speaker':'S1'})
        write_json(ep.artifact('transcript.json'),transcript)
        source=row['sources'][0]
    confirm(app,eid,'speakers',{'names':{'S1':'Synthetic host','S2':'Synthetic guest'}})
    confirm(app,eid,'mapping',{'reference':source['id'],'sources':[source['id']],'cameras':{}})
    confirm(app,eid,'bounds',{'in':.25,'out':3.75})
    assembly=run(studio,eid,'episode-assembly')
    selected={**assembly['edit'],'artifact_id':assembly['artifact']['id']}
    with pytest.raises(ValueError,match='assembly'):
        run(studio,eid,'episode-rough',assembly=selected,policy='balanced',restore=[])
    confirm(app,eid,'assembly',selected)
    first=run(studio,eid,'episode-rough',assembly=selected,policy='balanced',restore=[])
    assert first['artifact']['kind']=='rough'
    assert first['report']['cuts']
    restored=run(studio,eid,'episode-rough',assembly=selected,policy='balanced',restore=[first['report']['cuts'][0]['id']])
    assert restored['edit']['version']>first['edit']['version']
    assert restored['report']['totals']['output_duration_s']>first['report']['totals']['output_duration_s']
    confirm(app,eid,'rough',{**restored['edit'],'artifact_id':restored['artifact']['id']})
    assert E.load(studio,eid)['checkpoints']['rough']['state']=='reviewed exact'
    confirm(app,eid,'bounds',{'in':.5,'out':3.5})
    assert E.load(studio,eid)['checkpoints']['rough']['state']=='stale'
    app.close()


def test_sequence_audio_preview_requires_real_component_and_exact_rough_review(studio,tmp_path):
    eid,original,app=ready(studio,tmp_path)
    assembly=run(studio,eid,'episode-assembly');confirm(app,eid,'assembly',{**assembly['edit'],'artifact_id':assembly['artifact']['id']})
    rough=run(studio,eid,'episode-rough',assembly={**assembly['edit'],'artifact_id':assembly['artifact']['id']},policy='balanced',restore=[])
    selected={**rough['edit'],'artifact_id':rough['artifact']['id']}
    confirm(app,eid,'rough',selected)
    spec={'rough':selected,'show':'A real show','episode_title':'A real episode','number':'1',
          'hosts':[{'name':'Host','title':''}],'cold_open':None,
          'opening':{'enabled':True,'components':[{'kind':'excerpt','start':.1,'end':1.1}]},
          'closing':{'enabled':False,'components':[]}}
    with pytest.raises(ValueError,match='real audio component'):
        run(studio,eid,'episode-sequence',**{**spec,'opening':{'enabled':True,'components':[]}})
    result=run(studio,eid,'episode-sequence',**spec)
    assert result['draft'] and result['components']['opening']['artifact']['kind']=='sequence-opening'
    projection=E.load(studio,eid);assert projection['roughs'][-1]['render']['id']==selected['artifact_id'] and projection['roughs'][-1]['render']['kind']=='rough'
    with pytest.raises(ValueError,match='exact edit'):confirm(app,eid,'rough',{**selected,'artifact_id':result['components']['opening']['artifact']['id']})
    assert result['components']['opening']['samples']==48000
    confirm(app,eid,'sequence',result['review_payload'])
    assert E.load(studio,eid)['checkpoints']['sequence']['state']=='reviewed exact'
    app.close()


def test_audiovisual_sequence_preview_builds_real_picture(studio,tmp_path,synthetic_final_asr):
    from ave.config import binary
    from ave.studio.workbench import Workbench
    from PIL import Image
    import subprocess
    eid,_=gap_episode(studio,tmp_path)
    app=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority())
    assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',a)
    rough=run(studio,eid,'episode-rough',assembly=a,policy='gentle',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
    bedfile=recording(tmp_path,'sequence-bed.wav',duration=10);bed=library.add_file(studio.sp,bedfile,source={'type':'own'},by='synthetic',attested=False);library.attest_license(studio.sp,bed['id'],'simulated fixture')
    spec={'rough':r,'show':'Synthetic show','episode_title':'Synthetic episode','number':'1','hosts':[{'name':'Host','title':'Host'}],
          'editor':'Editor','cold_open':None,'opening':{'enabled':True,'components':[{'kind':'asset','id':bed['id'],'sha256':bed['sha256'],'start':0,'duration':10}]},'closing':{'enabled':False,'components':[]}}
    with R.resolve(studio,eid) as (ep,row):args={'episode':eid,'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(ep,row,'episode-sequence','main'),'session':'main','request_id':str(uuid.uuid4()),**spec}
    wb=Workbench(studio)
    try:
        job=wb.start_media('episode-sequence',args);deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            state=wb.tail(job['id'])
            if state['status'] not in ('queued','running'):break
            time.sleep(.05)
        assert state['status']=='done',state.get('diagnostics')
        result=state['result']
    finally:wb.close()
    artifact=E.artifact(studio,eid,result['components']['opening']['artifact']['id']);path=studio.sp.root/artifact['file']
    assert bed['id'] in library.get(studio.sp,artifact['details']['asset'])['derived_from']
    pixels=subprocess.check_output([str(binary('ffmpeg')),'-v','error','-ss','1','-i',str(path),'-frames:v','1','-vf','scale=1:1','-pix_fmt','rgb24','-f','rawvideo','-'])
    assert len(pixels)==3 and max(pixels)>5
    pcm=subprocess.check_output([str(binary('ffmpeg')),'-v','error','-ss','2','-t','0.5','-i',str(path),'-vn','-ac','1','-ar','48000','-f','f32le','-'])
    assert np.sqrt(np.mean(np.frombuffer(pcm,np.float32)**2))>.0001
    confirm(app,eid,'sequence',result['review_payload']);assert E.load(studio,eid)['checkpoints']['sequence']['state']=='reviewed exact'
    image=tmp_path/'synthetic-red.png';Image.new('RGB',(320,180),(240,20,30)).save(image)
    license_entry={'source':'synthetic','url':'https://example.org/fixture','license':'CC BY','license_url':'https://example.org/license','license_text':'Synthetic test license','attribution':{'required':True,'text':'Synthetic red image by fixture'}}
    broll=library.add_file(studio.sp,image,source={'type':'import'},license_entry=license_entry,by='synthetic',attested=False);library.attest_license(studio.sp,broll['id'],'simulated fixture')
    with R.resolve(studio,eid) as (ep,row):final_args={'episode':eid,'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(ep,row,'episode-final','main'),'session':'main','request_id':str(uuid.uuid4()),'rough':r,'sequence':result['review_payload'],'gain_db':0,'caption_style':'podcast','burn':True,'motion':[{'name':'Synthetic host','title':'Host','side':'left','start':.25,'duration':2.0}],
        'broll':[{'id':broll['id'],'sha256':broll['sha256'],'start':.5,'duration':1.0,'source_in':0,'ken_burns':False}]}
    wb=Workbench(studio)
    try:
        job=wb.start_media('episode-final',final_args);deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            state=wb.tail(job['id'])
            if state['status'] not in ('queued','running'):break
            time.sleep(.05)
        assert state['status']=='done',state.get('diagnostics')
        final=state['result'];assert final['artifact']['kind']=='final' and final['report']['duration']>rough['report']['totals']['output_duration_s']
        with R.resolve(studio,eid) as (ep,row):
            _,doc=E.exact(ep,final['edit']);overlay=doc['tracks']['overlays'][0];assert overlay['kind']=='motion' and sha256_file(ep.root/overlay['file'])==overlay['sha256']
            assert {o['kind'] for o in doc['tracks']['overlays']}=={'motion','broll','credit'}
        rendered=studio.sp.root/E.artifact(studio,eid,final['artifact']['id'])['file'];at=final['report']['joins_s'][0]+1
        pixel=subprocess.check_output([str(binary('ffmpeg')),'-v','error','-ss',str(at),'-i',str(rendered),'-frames:v','1','-vf','scale=1:1','-pix_fmt','rgb24','-f','rawvideo','-'])
        assert pixel[0]>150 and pixel[0]>pixel[1]*2
        assert 'Synthetic red image by fixture' in final['report']['credits']
        confirm(app,eid,'final',{**final['edit'],'artifact_id':final['artifact']['id']})
        assert E.load(studio,eid)['checkpoints']['final']['state']=='reviewed exact'
        from ave.studio import seal
        from ave.assets import licenses
        from ave.studio.episode_worker_bridge import Parent
        from ave.studio.workbench import job_path,write
        pending={**job,'id':'job_'+uuid.uuid4().hex[:24],'status':'queued'};pending['episode_descriptor']=E.descriptor(studio,pending['operation'],pending['args']);write(studio.sp,job_path(studio.sp,pending['id']),pending)
        bridge=Parent(studio,pending)
        try:
            with pytest.raises(ValueError):bridge.handle({'job_id':pending['id'],'action':'final-broll','data':{'asset':broll['id']}})
            entry=licenses.entry_hash(library.registry.license_log(studio.sp).find(sha256=broll['sha256']))
            seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':broll['id'],'sha256':broll['sha256'],'entry':entry,'purpose':'license-use','by':'simulated fixture'})
            with pytest.raises(ValueError,match='attestation'):bridge.handle({'job_id':pending['id'],'action':'snapshot','data':{}})
            assert E.load(studio,eid)['checkpoints']['final']['state']=='stale'
        finally:bridge.close()
    finally:wb.close()
    app.close()


def test_final_audio_binds_rough_sequence_and_measured_output(studio,tmp_path,synthetic_final_asr):
    eid,original,app=ready(studio,tmp_path)
    assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',a)
    rough=run(studio,eid,'episode-rough',assembly=a,policy='balanced',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
    plan=run(studio,eid,'episode-sequence',rough=r,show='Synthetic show',episode_title='Episode',number='1',hosts=[],cold_open=None,
             opening={'enabled':True,'components':[{'kind':'excerpt','start':0,'end':1}]},closing={'enabled':False,'components':[]})
    confirm(app,eid,'sequence',plan['review_payload'])
    result=run(studio,eid,'episode-final',rough=r,sequence=plan['review_payload'],gain_db=0,caption_style='podcast',burn=False)
    assert result['artifact']['kind']=='final' and result['report']['duration']>3.5
    assert result['report']['lufs'] is not None and result['report']['true_peak_db'] is not None
    confirm(app,eid,'final',{**result['edit'],'artifact_id':result['artifact']['id']})
    assert E.load(studio,eid)['checkpoints']['final']['state']=='reviewed exact'
    from ave.occurrences import mapped_words
    with R.resolve(studio,eid) as (ep,row):
        _,doc=E.exact(ep,r);doc['caption_edit']={'style':'podcast','burn':False,'corrections':[]};doc['native_source_hashes']={s['file']:s['sha256'] for s in row['sources']}
        word=mapped_words(ep,doc)[0];before=sha256_file(ep.artifact('transcript.json'))
    correction={'occurrence_id':word['occurrence_id'],'transcript_sha256':word['transcript_sha256'],'word_ids':[word['word_id']],'text':'Greetings'}
    revised=run(studio,eid,'episode-final',rough=r,sequence=plan['review_payload'],gain_db=0,caption_style='podcast',burn=False,corrections=[correction])
    assert any('Greetings' in c['text'] for c in revised['artifact']['details']['caption']['cues'])
    with R.resolve(studio,eid) as (ep,row):assert sha256_file(ep.artifact('transcript.json'))==before
    assert E.load(studio,eid)['checkpoints']['final']['state']=='needs review'
    confirm(app,eid,'bounds',{'in':.5,'out':3.5})
    assert E.load(studio,eid)['checkpoints']['final']['state']=='stale'
    app.close()


def test_retained_conversation_final_real_asr(studio,tmp_path,monkeypatch):
    import os,shutil
    from unittest.mock import patch
    manifest=FINAL_REPLAY
    if not manifest:pytest.skip('Opt-in retained conversation and real local final ASR')
    entry=next(e for e in json.loads(Path(manifest).read_text()) if e['mode']=='audio-390')
    root=tmp_path/'retained';shutil.copytree(entry['retained_root'],root)
    monkeypatch.setattr(R,'NativeBooks',FakeBooks)
    row=R.register_selected(studio,root,derived_consent=True);eid=row['id']
    original={s['file']:sha256_file(root/s['file']) for s in row['sources']};source_sha=sha256_file(root/'work/transcript.json')
    assert source_sha==entry['transcript_sha256'] and sha256_file(root/'work/sync.json')==entry['sync_sha256']
    # The test's isolated HOME has no models. Pin the already-installed local model
    # explicitly, then run the actual keyless local ASR executable without downloads.
    with patch.object(Path,'home',return_value=Path('/Users/maren')):configuration=E.final_asr_configuration()
    monkeypatch.setattr(E,'final_asr_configuration',lambda:configuration)
    run(studio,eid,'episode-intake');source=row['sources'][0]
    app=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority())
    confirm(app,eid,'speakers',{'names':{'S1':'Synthetic one','S2':'Synthetic two'}})
    confirm(app,eid,'mapping',{'reference':source['id'],'sources':[source['id']],'cameras':{}})
    confirm(app,eid,'bounds',{'in':0,'out':133.3})
    assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',a)
    rough=run(studio,eid,'episode-rough',assembly=a,policy='gentle',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
    plan=run(studio,eid,'episode-sequence',rough=r,show='Synthetic conversation',episode_title='Listening',number='1',hosts=[],cold_open=None,
             opening={'enabled':False,'components':[]},closing={'enabled':False,'components':[]});confirm(app,eid,'sequence',plan['review_payload'])
    final=run(studio,eid,'episode-final',rough=r,sequence=plan['review_payload'],gain_db=0,caption_style='podcast',burn=False)
    result=E.artifact(studio,eid,final['artifact']['id']);evidence=json.loads((studio.sp.root/result['file']).with_name(result['details']['asr']['filename']).read_text())
    assert evidence['transcript']['engine']['asr']=='fluidaudio' and len(evidence['transcript']['words'])>100
    assert evidence['media_sha256']==sha256_file(studio.sp.root/result['file']) and evidence['source_transcript_sha256']==source_sha
    assert sha256_file(root/'work/transcript.json')==source_sha and original=={s['file']:sha256_file(root/s['file']) for s in row['sources']}
    confirm(app,eid,'final',{**final['edit'],'artifact_id':result['id']});assert E.load(studio,eid)['checkpoints']['final']['state']=='reviewed exact'
    app.close()


def test_final_cues_cannot_exceed_sample_bound_and_disclose_clipped_words(tmp_path):
    from ave.captions import Cue,write_srt,parse_srt
    cues=[Cue(0,1.2,'complete',words=[{'w':'complete','start':.2,'end':.8}]),
          Cue(1.5,2.5,'partial',words=[{'w':'partial','start':1.7,'end':2.2}])]
    bounded,changes=E.bound_program_cues(cues,2.0004)
    path=tmp_path/'captions.srt';write_srt(bounded,path);parsed=parse_srt(path)
    assert max(c.end for c in parsed)<=2.0004
    assert changes[0]['clipped_words']==['partial'] and cues[-1].end==2.5
    comparison=E.compare_final_captions([{'start':0,'end':1,'text':'hello'}],[{'w':'hello','start':4,'end':4.3}])
    assert comparison['discrepancies']==1 and comparison['items'][0]['kind']=='timing'


def test_combined_assembly_exact_constituents_and_staleness(studio,tmp_path,synthetic_final_asr,monkeypatch):
    eid,source,app=ready(studio,tmp_path);source_before=sha256_file(source)
    with R.resolve(studio,eid) as (ep,row):mapping=C.latest(studio,eid,'mapping','main')['payload']
    confirm(app,eid,'mapping',mapping,session='second');confirm(app,eid,'bounds',{'in':1,'out':2},session='second')
    first=run(studio,eid,'episode-assembly');a={**first['edit'],'artifact_id':first['artifact']['id']};confirm(app,eid,'assembly',a)
    def execute_for(op,session,**extra):
        with R.resolve(studio,eid) as (ep,row):args={'episode':eid,'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(ep,row,op,session),'request_id':str(uuid.uuid4()),'session':session,**extra}
        job={'id':'job_'+uuid.uuid4().hex[:24],'operation':op,'args':args};job['episode_descriptor']=E.descriptor(studio,op,args);return E.execute(studio,job)
    second=execute_for('episode-assembly','second');b={**second['edit'],'artifact_id':second['artifact']['id']};confirm(app,eid,'assembly',b,session='second')
    from ave.studio.workbench import Workbench
    from ave.studio.episode_worker_bridge import Parent
    with R.resolve(studio,eid) as (ep,row):args={'episode':eid,'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(ep,row,'episode-combined','combined'),'request_id':str(uuid.uuid4()),'session':'combined','constituents':[b,a]}
    with R.resolve(studio,eid) as (ep,row):
        deps=E.combined_inputs(studio,ep,row,args);total=sum(x['duration'] for x in deps['constituents']);before=set(ep.edits.iterdir())
        with monkeypatch.context() as m:
            policy=R.policy();m.setattr(R,'policy',lambda:{**policy,'max_session_seconds':total})
            assert len(E.combined_inputs(studio,ep,row,args)['constituents'])==2
            m.setattr(R,'policy',lambda:{**policy,'max_session_seconds':total-.01})
            with pytest.raises(ValueError,match='duration limit'):E.combined_inputs(studio,ep,row,args)
        assert set(ep.edits.iterdir())==before
    wb=Workbench(studio)
    try:
        job=wb.start_media('episode-combined',args);deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            state=wb.tail(job['id'])
            if state['status'] not in ('queued','running'):break
            time.sleep(.05)
        assert state['status']=='done',state.get('diagnostics');result=state['result']
    finally:wb.close()
    combined={**result['edit'],'artifact_id':result['artifact']['id']}
    from ave.studio.workbench import write,job_path
    pending={**job,'id':'job_'+uuid.uuid4().hex[:24],'status':'queued'};write(studio.sp,job_path(studio.sp,pending['id']),pending);bridge=Parent(studio,pending)
    try:
        with monkeypatch.context() as m:
            m.setattr(R,'assert_sources',lambda *args:(_ for _ in ()).throw(AssertionError('heartbeat rehashed source media')))
            for _ in range(3):bridge.handle({'job_id':pending['id'],'action':'snapshot','data':{}})
        with pytest.raises(ValueError,match='Wrong-job'):bridge.handle({'job_id':'job_wrong','action':'combined-inputs','data':{}})
        with pytest.raises(ValueError,match='outside this job grant'):bridge.handle({'job_id':pending['id'],'action':'combined-inputs','data':{'constituent':a}})
    finally:bridge.close()
    with R.resolve(studio,eid) as (ep,row):
        _,doc=E.exact(ep,combined);assert doc['duration']==pytest.approx(sum(x['duration'] for x in doc['combined_constituents']));assert [x['identity']['name'] for x in doc['combined_constituents']]==['second','main']
    confirm(app,eid,'assembly',combined,session='combined')
    with R.resolve(studio,eid) as (ep,row):assert C.status(studio,ep,row,C.latest(studio,eid,'assembly','combined'))['state']=='reviewed exact'
    rough=execute_for('episode-rough','combined',assembly=combined,policy='gentle',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r,session='combined')
    seq=execute_for('episode-sequence','combined',rough=r,show='Synthetic',episode_title='Combined',number='1',hosts=[],cold_open=None,opening={'enabled':False,'components':[]},closing={'enabled':False,'components':[]});confirm(app,eid,'sequence',seq['review_payload'],session='combined')
    final=execute_for('episode-final','combined',rough=r,sequence=seq['review_payload'],gain_db=0,caption_style='podcast',burn=False);confirm(app,eid,'final',{**final['edit'],'artifact_id':final['artifact']['id']},session='combined')
    confirm(app,eid,'bounds',{'in':1.1,'out':2},session='second')
    with R.resolve(studio,eid) as (ep,row):
        assert C.status(studio,ep,row,C.latest(studio,eid,'assembly','combined'))['state']=='stale'
        assert C.status(studio,ep,row,C.latest(studio,eid,'rough','combined'))['state']=='stale'
        assert C.status(studio,ep,row,C.latest(studio,eid,'final','combined'))['state']=='stale'
        assert C.status(studio,ep,row,C.latest(studio,eid,'assembly','main'))['state']=='reviewed exact'
    assert sha256_file(source)==source_before
    app.close()


def test_component_captions_do_not_cross_sample_exact_joins(tmp_path):
    from ave.captions import Cue,write_srt,parse_srt
    opening=[Cue(.2,3.09,'opening',words=[{'w':'opening','start':2.9,'end':3.05}])]
    body=[Cue(0,2.2,'body',words=[{'w':'body','start':1.8,'end':2.1}])]
    closing=[Cue(-.1,.8,'closing',words=[{'w':'closing','start':-.05,'end':.5}])]
    join1=144016/48000;join2=240032/48000;end=288032/48000
    a,ac=E.bound_component_cues(opening,0,join1,'opening');b,bc=E.bound_component_cues(body,join1,join2,'body');c,cc=E.bound_component_cues(closing,join2,end,'closing')
    path=tmp_path/'component.srt';write_srt(a+b+c,path);parsed=parse_srt(path)
    assert parsed[0].end<=join1<=parsed[1].start and parsed[1].end<=join2<=parsed[2].start
    assert ac[0]['component']=='opening' and ac[0]['clipped_words']==['opening']
    assert bc[0]['component']=='body' and bc[0]['clipped_words']==['body']
    assert cc[0]['component']=='closing' and cc[0]['clipped_words']==['closing']
    assert opening[0].end==3.09 and body[0].end==2.2 and closing[0].start==-.1
    report={'duration':end,'sample_rate':48000,'joins_samples':[144016,240032]}
    E.validate_programme_caption_boundaries([{'start':x.start,'end':x.end} for x in parsed],report)
    with pytest.raises(ValueError,match='component join'):E.validate_programme_caption_boundaries([{'start':3.0,'end':3.09}],report)


def test_audio_final_sidecar_respects_opening_body_and_closing(studio,tmp_path,synthetic_final_asr):
    from ave import captions
    eid,original,app=ready(studio,tmp_path);assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',a)
    rough=run(studio,eid,'episode-rough',assembly=a,policy='gentle',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
    plan=run(studio,eid,'episode-sequence',rough=r,show='Synthetic',episode_title='Boundaries',number='1',hosts=[],cold_open=None,opening={'enabled':True,'components':[{'kind':'excerpt','start':.05,'end':.95}]},closing={'enabled':True,'components':[{'kind':'excerpt','start':.05,'end':.95}]});confirm(app,eid,'sequence',plan['review_payload'])
    final=run(studio,eid,'episode-final',rough=r,sequence=plan['review_payload'],gain_db=0,caption_style='podcast',burn=False)
    details=final['artifact']['details'];joins=[n/details['report']['sample_rate'] for n in details['report']['joins_samples']]
    cues=details['caption']['cues'];assert len(joins)==2
    assert all(not (c['start']<j<c['end']) for c in cues for j in joins)
    assert all(c['end']<=details['report']['duration'] for c in cues)
    app.close()


def test_video_component_captions_respect_fractional_frame_join(tmp_path):
    from ave.captions import Cue,write_srt,parse_srt
    fps=24000/1001;join1=101/fps;join2=202/fps;end=250/fps
    opening,adjustment=E.bound_component_cues([Cue(3.9,4.4,'opening')],0,join1,'opening')
    body,changes=E.bound_component_cues([Cue(0,join2-join1+.1,'body')],join1,join2,'body')
    closing,_=E.bound_component_cues([Cue(-.1,.5,'closing')],join2,end,'closing')
    path=tmp_path/'frame-joins.srt';write_srt(opening+body+closing,path);cues=parse_srt(path)
    assert cues[0].end<=join1<=cues[1].start and cues[1].end<=join2<=cues[2].start
    E.validate_programme_caption_boundaries([{'start':c.start,'end':c.end} for c in cues],{'duration':end,'expected_duration':end,'joins_s':[round(join1,4),round(join2,4)]})
    assert adjustment[0]['component']=='opening' and changes[0]['component']=='body'


def test_complete_programme_duration_policy_before_writes(tmp_path):
    limit=R.policy()['max_session_seconds']
    assert E.validate_programme_duration([limit-1,.5,.5])==limit
    with pytest.raises(ValueError,match='duration limit'):E.validate_programme_duration([limit-1,.6,.5])
    assert not list(tmp_path.iterdir())


def test_rough_comparison_later_seam_after_prior_cuts():
    assembly={'tracks':{'dialogue':[{'id':'a','o0':0,'o1':20,'e0':0,'src':'s'}]}}
    rough={'duration':17,'tracks':{'dialogue':[{'id':'a','o0':0,'o1':2,'e0':0,'src':'s'},{'id':'b','o0':2,'o1':5,'e0':3,'src':'s'},{'id':'c','o0':5,'o1':17,'e0':8,'src':'s'}]}}
    report={'cuts':[{'id':'c1','e0':2,'e1':3},{'id':'c2','e0':6,'e1':8}], 'detected_cuts':[{'id':'c1','e0':2,'e1':3},{'id':'c2','e0':6,'e1':8}]}
    p=E.rough_comparison_points(assembly,rough,report);assert p['c2']['assembly_output_s']==6 and p['c2']['rough_output_s']==5
    assembly['combined_constituents']=[{}];assembly['tracks']['dialogue']=[{'id':'a','o0':0,'o1':10,'e0':10,'src':'s'},{'id':'b','o0':10,'o1':20,'e0':0,'src':'s'}]
    p=E.rough_comparison_points(assembly,rough,report);assert p['c2']=={'assembly_output_s':6,'rough_output_s':5,'input_clock':'combined assembly output'}


def test_audio_opening_word_correction_is_component_scoped(studio,tmp_path,synthetic_final_asr):
    eid,original,app=ready(studio,tmp_path);assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',a)
    rough=run(studio,eid,'episode-rough',assembly=a,policy='gentle',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
    spec={'rough':r,'show':'Synthetic','episode_title':'Component correction','number':'1','hosts':[],'cold_open':None,'opening':{'enabled':True,'components':[{'kind':'excerpt','start':0,'end':.9}]},'closing':{'enabled':False,'components':[]}}
    preview=run(studio,eid,'episode-sequence',**spec);words=[w for c in preview['components']['opening']['artifact']['details']['caption']['cues'] for w in c['words']];word=next(w for w in words if w['w']=='Hello')
    correction={'occurrence_id':word['occurrence_id'],'transcript_sha256':word['transcript_sha256'],'word_ids':[word['word_id']],'text':'Greetings'}
    spec['opening']['components'][0]['corrections']=[correction]
    preview=run(studio,eid,'episode-sequence',**spec);confirm(app,eid,'sequence',preview['review_payload'])
    final=run(studio,eid,'episode-final',rough=r,sequence=preview['review_payload'],gain_db=0,caption_style='podcast',burn=False)
    cues=final['artifact']['details']['caption']['cues'];opening=[c for c in cues if c['start']<.9];body=[c for c in cues if c['start']>=.9]
    assert any('Greetings' in c['text'] for c in opening) and any('Hello' in c['text'] for c in body)
    assert any(w.get('component_tag')=='opening' and w.get('component_index')==0 for c in opening for w in c['words'])
    app.close()


def test_av_cold_open_word_correction_is_component_scoped(studio,tmp_path,synthetic_final_asr):
    eid,_=gap_episode(studio,tmp_path);app=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority())
    assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',a)
    rough=run(studio,eid,'episode-rough',assembly=a,policy='gentle',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
    spec={'rough':r,'show':'Synthetic','episode_title':'Cold open','number':'1','hosts':[{'name':'Host','title':'Host'}],'editor':'Editor','cold_open':{'start':0,'end':1.5},'opening':{'enabled':True,'components':[]},'closing':{'enabled':False,'components':[]}}
    preview=run(studio,eid,'episode-sequence',**spec);words=[w for c in preview['components']['opening']['artifact']['details']['caption']['cues'] for w in c['words']];word=next(w for w in words if w['w']=='First.')
    correction={'occurrence_id':word['occurrence_id'],'transcript_sha256':word['transcript_sha256'],'word_ids':[word['word_id']],'text':'Beginning.'};spec['cold_open']['corrections']=[correction]
    preview=run(studio,eid,'episode-sequence',**spec);confirm(app,eid,'sequence',preview['review_payload'])
    final=run(studio,eid,'episode-final',rough=r,sequence=preview['review_payload'],gain_db=0,caption_style='podcast',burn=True)
    cues=final['artifact']['details']['caption']['cues'];join=final['report']['joins_s'][0]
    assert any('Beginning.' in c['text'] for c in cues if c['start']<join) and any('First.' in c['text'] for c in cues if c['start']>=join)
    assert any(w.get('component_index')==-1 for c in cues if c['start']<join for w in c['words'])
    app.close()


def test_reversed_combined_seam_auditions_decode_actual_samples(tmp_path):
    from ave.episode import Episode
    from ave.compile import compile_audio
    from ave.studio.video_workshop import recut_output
    ep=Episode(tmp_path/'seams');ep.create();sr=48000
    samples=np.repeat(np.arange(20,dtype=np.float32)/100,sr);sf.write(ep.raw/'clock.wav',np.column_stack([samples,samples]),sr,subtype='PCM_24')
    sources={'s':{'file':ep.rel(ep.raw/'clock.wav'),'kind':'audio','start_offset':0,'drift_ppm':0}}
    doc=editdoc.new_doc('assembly',24,(640,360),sources,20,media_mode='audio');doc['combined_constituents']=[{'identity':{'name':'second'}},{'identity':{'name':'first'}}]
    doc['tracks']['dialogue']=[{'id':'d1','src':'s','o0':0.,'o1':10.,'e0':10.,'xfade_ms':0},{'id':'d2','src':'s','o0':10.,'o1':20.,'e0':0.,'xfade_ms':0}]
    cuts=[{'id':'c1','o0':2.,'o1':3.,'xfade_ms':0},{'id':'c2','o0':6.,'o1':8.,'xfade_ms':0}];rough=recut_output(doc,cuts)
    report={'cuts':[{'id':x['id'],'e0':x['o0'],'e1':x['o1']} for x in cuts],'detected_cuts':[{'id':x['id'],'e0':x['o0'],'e1':x['o1']} for x in cuts]}
    points=E.rough_comparison_points(doc,rough,report);before=tmp_path/'before.wav';after=tmp_path/'after.wav';compile_audio(ep,doc,before);compile_audio(ep,rough,after);a,_=sf.read(before);b,_=sf.read(after)
    seam=points['c2'];assert seam['assembly_output_s']==6 and seam['rough_output_s']==5
    assert b[round((seam['rough_output_s']+.1)*sr),0]==pytest.approx(a[round(8.1*sr),0],abs=2e-6)


def test_joined_final_duration_denied_before_version_write(studio,tmp_path,synthetic_final_asr,monkeypatch):
    eid,original,app=ready(studio,tmp_path);assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',a)
    rough=run(studio,eid,'episode-rough',assembly=a,policy='gentle',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
    plan=run(studio,eid,'episode-sequence',rough=r,show='Synthetic',episode_title='Cap',number='1',hosts=[],cold_open=None,opening={'enabled':True,'components':[{'kind':'excerpt','start':0,'end':1}]},closing={'enabled':False,'components':[]});confirm(app,eid,'sequence',plan['review_payload'])
    policy=R.policy();policy['max_session_seconds']=4.4;monkeypatch.setattr(R,'policy',lambda:policy)
    with R.resolve(studio,eid) as (ep,row):before=editdoc.list_versions(ep,'final','main')
    with pytest.raises(ValueError,match='duration limit'):run(studio,eid,'episode-final',rough=r,sequence=plan['review_payload'],gain_db=0,caption_style='podcast',burn=False)
    with R.resolve(studio,eid) as (ep,row):assert editdoc.list_versions(ep,'final','main')==before
    app.close()
