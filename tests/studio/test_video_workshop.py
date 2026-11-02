"""Independent source clocks, immutable editing and real decoded audio contracts."""
import copy
import hashlib
import json
import tempfile
import multiprocessing
import uuid
from pathlib import Path
import numpy as np
import pytest
import soundfile as sf
from ave import editdoc, library, captions
from ave.episode import Episode,write_json
from ave.occurrences import mapped_words
from ave.studio import video_workshop as V
from .test_workbench import studio


def test_modes_and_atomic_allocation(tmp_path):
    ep=Episode(tmp_path/'episode');ep.create()
    doc=editdoc.new_doc('short',24,(640,360),{},media_mode='audio')
    path=editdoc.save_new_version(ep,doc,expected_version=0)
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(editdoc.EditConflict): editdoc.save_new_version(ep,doc,expected_version=0)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==sha
    doc['media_mode']['version']=2
    assert editdoc.validate(doc)


def mutate(studio,p,op,rid=None):
    return V.mutate(studio,{'project':p['id'],'expected_revision':p['revision'],'expected_version':p['version'],'expected_sha256':p['sha256'],'request_id':rid or str(uuid.uuid4()),'operation':op})


def test_independent_occurrences_and_corrections(studio,tmp_path):
    p=V.create(studio,{'title':'Clock test','mode':'audio','request_id':'clock-test'})
    ep,meta=V.project(studio.sp,p['id'])
    for key,words,origin in [('A',[{'w':'alpha','start':.2,'end':.4},{'w':'beta','start':1.2,'end':1.4}],0),('B',[{'w':'gamma','start':.2,'end':.4}],3)]:
        path=ep.work/(key+'.json');write_json(path,{'words':words});sha=hashlib.sha256(path.read_bytes()).hexdigest()
        meta['project_sources'][key]={'source_id':key,'file':'unused.wav','kind':'audio','has_audio':True,'duration':2,'start_offset':origin,'drift_ppm':0,'sha256':key*64,'transcript':{'file':ep.rel(path),'sha256':sha},'proxy':'unused.wav','label':key,'parent':'unused'}
    write_json(ep.meta_path,meta)
    for key,a,b in [('A',1,2),('B',0,1),('A',0,1),('A',0,1)]: p=mutate(studio,p,{'kind':'insert','source':key,'in':a,'out':b})
    words=mapped_words(ep,p['doc']);assert [w['w'] for w in words]==['beta','gamma','alpha','alpha'];assert np.allclose([w['start'] for w in words],[.2,1.2,2.2,3.2])
    assert words[-1]['occurrence_id']!=words[-2]['occurrence_id']
    c=words[-1];p=mutate(studio,p,{'kind':'captions','correction':{'occurrence_id':c['occurrence_id'],'transcript_sha256':c['transcript_sha256'],'word_ids':[c['word_id']],'text':'corrected {\\test}'}})
    assert [w['w'] for w in mapped_words(ep,p['doc'])]==['beta','gamma','alpha','corrected {\\test}']
    p=mutate(studio,p,{'kind':'split','clip':p['doc']['clips'][-2]['id'],'at':.3})
    assert len(mapped_words(ep,p['doc']))==4
    cs=captions.cues_from_edit(ep,p['doc']);assert all(c.end<=4 for c in cs)
    p=mutate(studio,p,{'kind':'remove','clip':p['doc']['clips'][-1]['id']})
    assert [w['w'] for w in mapped_words(ep,p['doc'])]==['beta','gamma','alpha']


def test_real_audio_gain_channels_fades_and_cas(studio,tmp_path):
    from ave.compile import compile_audio
    path=tmp_path/'stereo.wav';sr=48000;t=np.arange(sr*2)/sr
    data=np.column_stack([.2*np.sin(2*np.pi*440*t),.1*np.sin(2*np.pi*660*t)]);sf.write(path,data,sr,subtype='PCM_24')
    rec=library.add_file(studio.sp,path,source={'type':'own'},by='synthetic')
    p=V.create(studio,{'title':'Sound','mode':'audio','request_id':'sound-test'})
    p=V.add_source(studio,{'project':p['id'],'source_id':rec['id'],'expected_revision':1,'request_id':'source-test'})
    p=mutate(studio,p,{'kind':'insert','source':rec['id'],'in':0,'out':2})
    before=copy.deepcopy(p)
    p=mutate(studio,p,{'kind':'sound','clip':p['doc']['clips'][0]['id'],'channel':'left','gain_db':-6,'fade_in_s':.2,'fade_out_s':.2})
    with pytest.raises(editdoc.EditConflict): mutate(studio,before,{'kind':'remove','clip':before['doc']['clips'][0]['id']})
    ep,_=V.project(studio.sp,p['id']);out=tmp_path/'compiled.wav';compile_audio(ep,p['doc'],out);got,sr=sf.read(out)
    assert got.shape==(96000,2);assert np.array_equal(got[:,0],got[:,1]);assert abs(got[12000:24000,0].std()-.2/2**.5*10**(-6/20))<1e-6
    assert abs(got[:4800,0]).max()<.072


def _save_worker(root):
    ep=Episode(root);doc=editdoc.new_doc('short',24,(640,360),{},media_mode='audio')
    return str(editdoc.save_new_version(ep,doc))


def test_cross_process_no_clobber_versions(tmp_path):
    from concurrent.futures import ProcessPoolExecutor
    root=tmp_path/'parallel';Episode(root).create()
    with ProcessPoolExecutor(max_workers=4,mp_context=multiprocessing.get_context('spawn')) as pool:paths=list(pool.map(_save_worker,[root]*12))
    assert len(set(paths))==12;assert editdoc.list_versions(Episode(root),'short')==list(range(1,13))
    assert all(json.loads(Path(p).read_text())['media_mode']['kind']=='audio' for p in paths)


@pytest.mark.parametrize('value',[True,float('nan'),float('inf'),-1,100000])
def test_bad_source_range_has_no_edit(studio,value):
    p=V.create(studio,{'title':'Bounds','mode':'audio','request_id':'bounded-range'})
    ep,meta=V.project(studio.sp,p['id']);meta['project_sources']['A']={'source_id':'A','file':'none.wav','kind':'audio','has_audio':True,'duration':2,'start_offset':0,'drift_ppm':0,'sha256':'a'*64,'proxy':'none.wav','label':'A','parent':'none'};write_json(ep.meta_path,meta)
    with pytest.raises(ValueError):mutate(studio,p,{'kind':'insert','source':'A','in':value,'out':2})
    assert editdoc.list_versions(ep,'short')==[1]


def test_revision_replay_conflict_snapshot_and_symlink(studio,tmp_path):
    p=V.create(studio,{'title':'Replay','mode':'audio','request_id':'replay-project'})
    body={'project':p['id'],'expected_revision':p['revision'],'expected_version':p['version'],'expected_sha256':p['sha256'],'request_id':'replay-operation','operation':{'kind':'captions','style':'short'}}
    out=V.mutate(studio,body);assert V.mutate(studio,body)['sha256']==out['sha256']
    bad=copy.deepcopy(body);bad['operation']['style']='podcast'
    with pytest.raises(editdoc.EditConflict):V.mutate(studio,bad)
    ep,_=V.project(studio.sp,p['id']);assert V.pinned_caption(studio.sp,ep,out['doc'])['style']['name']=='short'
    first=ep.root/p['doc']['caption_snapshot']['file'];first_hash=hashlib.sha256(first.read_bytes()).hexdigest()
    assert V.load(studio.sp,p['id'],1)['sha256']==p['sha256'];assert hashlib.sha256(first.read_bytes()).hexdigest()==first_hash
    outside=tmp_path/'outside.json';outside.write_text('{}');editdoc.path_for(ep,'short',3).symlink_to(outside)
    with pytest.raises(ValueError): editdoc.save_new_version(ep,out['doc'])
    assert outside.read_text()=='{}'


def test_unverified_upload_parent_never_attested(studio,tmp_path):
    from ave.studio.workbench import write,source_index
    path=studio.sp.work/'raw-upload.wav';sf.write(path,np.zeros(4800),48000)
    sha=hashlib.sha256(path.read_bytes()).hexdigest();sid='recording_'+sha
    write(studio.sp,studio.sp.work/'production'/'sources.json',{'v':1,'sources':[{'source_id':sid,'kind':'audio','file':studio.sp.rel(path),'sha256':sha,'ready':True,'duration':.1,'label':'Imported recording'}]})
    p=V.create(studio,{'title':'Unverified rights','mode':'audio','request_id':'unverified-source'})
    p=V.add_source(studio,{'project':p['id'],'source_id':sid,'expected_revision':1,'request_id':'unverified-add'})
    parent=p['sources'][0]['parent'];coverage=library.coverage(studio.sp,[parent]);assert not coverage['ok'];assert 'not attested' in coverage['missing'][0]['why']


def test_audio_derivative_occurrence_attachment_and_resume(studio,tmp_path,monkeypatch):
    from ave.studio import store,model
    from ave.studio.workbench import write,job_path
    from ave.studio.events import Actor
    path=tmp_path/'speech.wav';sf.write(path,.15*np.sin(np.arange(48000)*2*np.pi*440/48000),48000)
    rec=library.add_file(studio.sp,path,source={'type':'own'},by='synthetic fixture authority')
    p=V.create(studio,{'title':'Audio reference','mode':'audio','request_id':'audio-reference'})
    p=V.add_source(studio,{'project':p['id'],'source_id':rec['id'],'expected_revision':1,'request_id':'audio-ref-source'});p=mutate(studio,p,{'kind':'insert','source':rec['id'],'in':0,'out':1})
    args={'project':p['id'],'version':p['version'],'sha256':p['sha256'],'preset':'wav','request_id':'render-audio-ref'}
    real=library.add_derivative
    monkeypatch.setattr(library,'add_derivative',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('simulated registration interruption')))
    with pytest.raises(RuntimeError):V.render(studio,args)
    monkeypatch.setattr(library,'add_derivative',real);a=V.render(studio,args);assert V.render(studio,args)['assets']==a['assets']
    other=V.render(studio,{**args,'request_id':'render-audio-ref-other'});assert other['assets'][0]['sha256']==a['assets'][0]['sha256'];assert other['assets'][0]['asset']!=a['assets'][0]['asset']
    post=model.new_post(idea='Audio production reference',by='synthetic');post=store.save_post(studio.sp,post,expected_rev=None,actor=Actor('human','synthetic'),op='test')
    jid='job_'+'1'*24;write(studio.sp,job_path(studio.sp,jid),{'id':jid,'operation':'video-render','status':'done','args':args,'result':a})
    body={'job_id':jid,'asset':a['assets'][0]['asset'],'post':post['id'],'expected_rev':post['rev']}
    V.attach_audio(studio,body);saved=store.load_post(studio.sp,post['id']);assert saved['production']['sources'][0]['source_id']=='output_'+a['assets'][0]['asset'];assert saved['media']==post['media']
    with pytest.raises(model.RevConflict):V.attach_audio(studio,body)
    assert len(library.get(studio.sp,a['assets'][0]['asset'])['derived_from'])==1
    assert not list(studio.sp.publish.glob('queue/*.json'))


def test_kept_adapter_preserves_offset_drift_and_knot(studio,tmp_path,monkeypatch):
    # Synthetic already-kept descriptor. No human approval API is invoked.
    ep=Episode(studio.sp.work/'production'/'episodes'/'synthetic_kept');ep.create()
    wav=ep.work/'source.wav';sf.write(wav,.1*np.sin(np.arange(48000*3)*2*np.pi*440/48000),48000)
    src={'file':ep.rel(wav),'kind':'audio','start_offset':2,'drift_ppm':80,'sync_map':[[0,2],[1,2.00008],[3,2.00024]]}
    doc=editdoc.new_doc('select',24,(640,360),{'mic':src},1.5,media_mode='audio');doc['tracks']['dialogue']=[{'id':'d','src':'mic','o0':0,'o1':1.5,'e0':2.5}]
    write_json(ep.artifact('transcript.json'),{'words':[{'w':'kept','start':2.6,'end':2.8},{'w':'again','start':3.2,'end':3.4}]})
    descriptor={'source_id':'recording_'+'a'*64,'clip_id':'kept','title':'Synthetic kept selection','version':1,'sha256':'b'*64,'decision_sha256':'c'*64}
    monkeypatch.setattr(V,'resolve_kept',lambda sp,args:(ep,copy.deepcopy(doc),descriptor))
    doc['tracks']['overlays']=[{'kind':'text','text':'Preserve me'}]
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in studio.sp.root.rglob('*') if p.is_file()}
    with pytest.raises(ValueError,match='audio select contains overlays tracks.*Episode'):
        V.adapt_kept(studio,{'request_id':'kept-refusal','source_id':descriptor['source_id']})
    assert before=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in studio.sp.root.rglob('*') if p.is_file()}
    doc['tracks']['overlays']=[]
    p=V.adapt_kept(studio,{'request_id':'kept-adapter','source_id':descriptor['source_id']});out_ep,_=V.project(studio.sp,p['id'])
    assert p['doc']['sources']['mic']['start_offset']==2;assert p['doc']['sources']['mic']['drift_ppm']==80
    assert len(p['doc']['clips'])==1 # compiler maps piecewise clocks; no artificial editable clip needed
    words=mapped_words(out_ep,p['doc']);assert [w['w'] for w in words]==['kept','again'];assert np.allclose([w['start'] for w in words],[.1,.7])
    from ave.compile import compile_audio
    actual=tmp_path/'kept.wav';compile_audio(out_ep,p['doc'],actual);assert sf.info(actual).frames==72000
    bad={'source_id':'../../outside','clip_id':'unknown','version':1,'sha256':'b'*64,'decision_sha256':'c'*64,'request_id':'bad-kept'}
    monkeypatch.undo()
    with pytest.raises(ValueError):V.resolve_kept(studio.sp,bad)


def test_real_music_ducking_and_split_sound_identity(studio,tmp_path):
    from ave.compile import compile_audio
    from .video_fixtures import calibration as make_calibration
    calibration=make_calibration(tmp_path/'calibration')
    files=list(calibration.glob('*.wav'))
    speech=next(p for p in files if sf.info(p).duration==30);bed=next(p for p in files if sf.info(p).duration==64)
    source=library.add_file(studio.sp,speech,source={'type':'own'},by='synthetic fixture authority')
    music=library.add_file(studio.sp,bed,source={'type':'own'},by='synthetic fixture authority')
    p=V.create(studio,{'title':'Synthetic mix calibration','mode':'audio','request_id':'music-calibration'})
    p=V.add_source(studio,{'project':p['id'],'source_id':source['id'],'expected_revision':1,'request_id':'calibration-source'})
    p=mutate(studio,p,{'kind':'insert','source':source['id'],'in':0,'out':8});p=mutate(studio,p,{'kind':'sound','clip':p['doc']['clips'][0]['id'],'gain_db':-3,'channel':'left','fade_in_s':2,'fade_out_s':2})
    ep,_=V.project(studio.sp,p['id']);dialogue=tmp_path/'dialogue.wav';compile_audio(ep,p['doc'],dialogue);base,_=sf.read(dialogue,dtype='float32')
    p=mutate(studio,p,{'kind':'split','clip':p['doc']['clips'][0]['id'],'at':1});split=tmp_path/'split.wav';compile_audio(ep,p['doc'],split);split_pcm,_=sf.read(split,dtype='float32');assert np.max(abs(split_pcm-base))<1e-7
    p=mutate(studio,p,{'kind':'music','asset':music['id'],'gain_db':-10,'duck_db':0,'fade_in_s':.2,'fade_out_s':.2});plain=tmp_path/'plain.wav';compile_audio(ep,p['doc'],plain);un_ducked,_=sf.read(plain,dtype='float32')
    p=mutate(studio,p,{'kind':'music','asset':music['id'],'gain_db':-10,'duck_db':-12,'fade_in_s':.2,'fade_out_s':.2});duck=tmp_path/'duck.wav';compile_audio(ep,p['doc'],duck);ducked,_=sf.read(duck,dtype='float32')
    a=un_ducked[144000:240000]-base[144000:240000];b=ducked[144000:240000]-base[144000:240000]
    ratio=np.linalg.norm(b)/np.linalg.norm(a);assert abs(20*np.log10(ratio)+12)<.1
    evidence=Path(tempfile.gettempdir())/'ave-media-video-fix1-evidence';evidence.mkdir(exist_ok=True)
    (evidence/'sound-numerical.json').write_text(json.dumps({'sources':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [speech,bed]},'ducking_db':float(20*np.log10(ratio)),'duck_tolerance_db':.1,'split_max_sample_error':float(np.max(abs(split_pcm-base))),'split_tolerance':1e-7,'sample_rate':48000,'programme_samples':len(base),'synthetic_authority':True},indent=2))


def test_full_hd_silent_video_pixels_and_no_audio_probe(studio,tmp_path):
    import subprocess
    from ave.config import binary
    from ave import media
    from .video_fixtures import picture
    source=picture(tmp_path/'source.mov')
    silent=tmp_path/'silent.mp4'
    subprocess.run([binary('ffmpeg'),'-v','error','-y','-i',str(source),'-t','2','-an','-c:v','libx264','-preset','ultrafast','-crf','18',str(silent)],check=True)
    rec=library.add_file(studio.sp,silent,source={'type':'own'},by='synthetic fixture authority')
    p=V.create(studio,{'title':'Silent full HD','mode':'silent_video','request_id':'silent-hd-project'})
    p=V.add_source(studio,{'project':p['id'],'source_id':rec['id'],'expected_revision':1,'request_id':'silent-hd-source'});assert not p['sources'][0]['has_audio']
    p=mutate(studio,p,{'kind':'insert','source':rec['id'],'in':0,'out':2});p=mutate(studio,p,{'kind':'format','mode':'silent_video','aspect':'9:16','resolution':'hd'})
    p=mutate(studio,p,{'kind':'reframe','clip':p['doc']['clips'][0]['id'],'x':220,'y':0,'w':200,'h':360})
    args={'project':p['id'],'version':p['version'],'sha256':p['sha256'],'preset':'master','request_id':'silent-hd-output'};r=V.render(studio,args);output=studio.sp.root/r['assets'][0]['file'];probe=media.probe_file(output,studio.sp.root)
    assert probe['video']['width']==1080 and probe['video']['height']==1920;assert abs(probe['duration']-2)<1/24
    audio=subprocess.check_output([binary('ffmpeg'),'-v','error','-i',str(output),'-vn','-f','f32le','-']);pcm=np.frombuffer(audio,dtype='<f4');assert np.max(abs(pcm))==0
    image=subprocess.check_output([binary('ffmpeg'),'-v','error','-i',str(output),'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','-']);pixels=np.frombuffer(image,dtype=np.uint8).reshape(1920,1080,3)
    # Source-blue interior remains painted, with intentional aspect-preserving side bars.
    region=pixels[1100:1300,300:700];mean=region.mean(axis=(0,1));assert 35<mean[0]<90 and 65<mean[1]<130 and 90<mean[2]<170
    assert np.max(pixels[:,0])<5
    evidence=Path(tempfile.gettempdir())/'ave-media-video-fix1-evidence';evidence.mkdir(exist_ok=True);(evidence/'full-hd-silent.json').write_text(json.dumps({'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'silent_source_sha256':rec['sha256'],'output':r,'probe':probe,'decoded_audio_peak':float(np.max(abs(pcm))),'decoded_interior_rgb':mean.tolist(),'duration_tolerance_s':1/24,'synthetic_authority':True},indent=2))
    from PIL import Image
    Image.fromarray(pixels).save(evidence/'full-hd-silent-frame.png')
    from ave.studio import model,store,media_workshop
    from ave.studio.events import Actor
    from ave.studio.workbench import write,job_path
    post=model.new_post(idea='Synthetic video attachment',by='synthetic');post['media']=[{'id':'m_clip','asset':rec['id'],'slot':0,'role':'video','alt':'Synthetic source'}]
    post=store.save_post(studio.sp,post,expected_rev=None,actor=Actor('human','synthetic'),op='test');oldrev=post['rev'];post['idea']['text']='Changed while rendering';post=store.save_post(studio.sp,post,expected_rev=oldrev,actor=Actor('human','synthetic'),op='test')
    jid='job_'+'2'*24;write(studio.sp,job_path(studio.sp,jid),{'id':jid,'operation':'video-render','status':'done','args':args,'result':r})
    body={'job_id':jid,'asset':r['assets'][0]['asset'],'target':{'post':post['id'],'item':'m_clip','variant':None,'expected_rev':oldrev}}
    with pytest.raises(model.RevConflict):media_workshop.attach(studio,body,Actor('human','synthetic'))
    assert store.load_post(studio.sp,post['id'])['media'][0]['asset']==rec['id'];assert hashlib.sha256(output.read_bytes()).hexdigest()==r['assets'][0]['sha256']
    before=copy.deepcopy(p);p=mutate(studio,p,{'kind':'format','mode':'audio','aspect':'16:9'})
    with pytest.raises(ValueError,match='no picture'):V.validate_operation(studio.sp,'video-render',{'project':p['id'],'version':p['version'],'sha256':p['sha256'],'preset':'master','request_id':'audio-no-video'})
    from ave import handoff
    with pytest.raises(ValueError,match='Audio review requires'):handoff.plan(V.project(studio.sp,p['id'])[0],p['doc'],name='Synthetic')


def test_output_recut_retains_reordered_occurrences_and_real_audio(studio,tmp_path):
    from ave.compile import compile_audio
    source=tmp_path/'recut.wav';rate=48000
    signal=np.concatenate([np.full(rate,.1),np.full(rate,.2),np.full(rate,.3)])
    sf.write(source,np.column_stack([signal,signal]),rate,subtype='PCM_24')
    rec=library.add_file(studio.sp,source,source={'type':'own'},by='synthetic')
    p=V.create(studio,{'title':'Recut','mode':'audio','request_id':'recut-project'})
    p=V.add_source(studio,{'project':p['id'],'source_id':rec['id'],'expected_revision':1,'request_id':'recut-source'})
    for a,b in [(2,3),(0,1),(2,3)]:p=mutate(studio,p,{'kind':'insert','source':rec['id'],'in':a,'out':b})
    original=p['doc'];revised=V.recut_output(original,[{'id':'cut1','o0':.5,'o1':1.5,'xfade_ms':0}])
    assert revised['independent_project'] and revised['caption_snapshot']==original['caption_snapshot']
    assert [c['occurrence_id'] for c in revised['clips']]==[c['occurrence_id'] for c in original['clips']]
    ep,_=V.project(studio.sp,p['id']);out=tmp_path/'recut-output.wav';compile_audio(ep,revised,out);data,sr=sf.read(out)
    assert sr==rate and len(data)==rate*2
    assert np.allclose(data[[rate//4,3*rate//4,3*rate//2],0],[.3,.1,.3],atol=1e-5)
    music_path=tmp_path/'bed.wav';sf.write(music_path,np.full((rate*4,2),.05),rate,subtype='PCM_24')
    music=library.add_file(studio.sp,music_path,source={'type':'own'},by='synthetic')
    p=mutate(studio,p,{'kind':'music','asset':music['id'],'gain_db':-6,'duck_db':0,'fade_in_s':1,'fade_out_s':1})
    before=tmp_path/'before-recut.wav';compile_audio(ep,p['doc'],before);pcm,_=sf.read(before)
    revised=V.recut_output(p['doc'],[{'id':'cut1','o0':.5,'o1':1.5,'xfade_ms':0}])
    after=tmp_path/'after-recut.wav';compile_audio(ep,revised,after);actual,_=sf.read(after)
    dry=tmp_path/'dry-recut.wav';compile_audio(ep,{**revised,'tracks':{**revised['tracks'],'music':[]}},dry);plain,_=sf.read(dry)
    t=(np.arange(len(actual))+.5)/rate;bed=.05*10**(-6/20)*np.sin(np.clip(t,0,1)*np.pi/2)*np.sin(np.clip(2-t,0,1)*np.pi/2)
    assert np.max(np.abs(actual[:,0]-plain[:,0]-bed))<5e-4  # one continuous bed: faded in at the start, out at the new end
    assert all(m['asset']==music['id'] for m in revised['tracks']['music'])
    unsupported=copy.deepcopy(original);unsupported['tracks']['overlays'].append({'id':'unknown','kind':'unknown'})
    with pytest.raises(ValueError,match='Unsupported overlay'):V.recut_output(unsupported,[])


def test_caption_timing_limit_is_pinned_in_snapshot(studio,monkeypatch):
    from ave.occurrences import _timing_limit
    p=V.create(studio,{'title':'Caption policy','mode':'audio','request_id':'timing-snapshot'})
    ep,_=V.project(studio.sp,p['id']);saved=V.pinned_caption(studio.sp,ep,p['doc'])['timing_adjustment_limit_s']
    monkeypatch.setattr(captions,'timing_adjustment_limit',lambda ep=None: saved+7)
    assert _timing_limit(ep,p['doc'])==saved


def test_occurrence_cleanup_creates_immutable_media_revision(studio,tmp_path):
    source=tmp_path/'cleanup.wav';rate=48000
    signal=np.zeros((rate*3,2));signal[:rate//2]=.1;signal[rate*2:rate*5//2]=.1
    sf.write(source,signal,rate,subtype='PCM_24')
    rec=library.add_file(studio.sp,source,source={'type':'own'},by='synthetic')
    p=V.create(studio,{'title':'Cleanup','mode':'audio','request_id':'cleanup-project'})
    p=V.add_source(studio,{'project':p['id'],'source_id':rec['id'],'expected_revision':1,'request_id':'cleanup-source'})
    ep,meta=V.project(studio.sp,p['id']);tp=ep.work/'words.json';write_json(tp,{'words':[{'w':'hello','start':.1,'end':.4,'speaker':'S1'},{'w':'again','start':2.1,'end':2.4,'speaker':'S1'}]})
    meta['project_sources'][rec['id']]['transcript']={'file':ep.rel(tp),'sha256':hashlib.sha256(tp.read_bytes()).hexdigest(),'clock':'source'};write_json(ep.meta_path,meta)
    p=mutate(studio,p,{'kind':'insert','source':rec['id'],'in':0,'out':3})
    args={'project':p['id'],'version':p['version'],'sha256':p['sha256'],'policy':'balanced','restore':[],'request_id':'cleanup-one'}
    V.validate_operation(studio.sp,'video-cleanup',args);result=V.cleanup_programme(studio,{'id':'cleanup-fixture','args':args})
    revised=V.load(studio.sp,p['id'],result['version']);assert revised['doc']['independent_project']
    assert revised['doc']['duration']<p['doc']['duration'] and result['review']=='not reviewed'
    assert V.load(studio.sp,p['id'],p['version'])['sha256']==p['sha256']
    assert result['report']['analysis_sha256'] and result['report']['decisions']
    restored={**args,'version':result['version'],'sha256':result['sha256'],'base_version':p['version'],'base_sha256':p['sha256'],'restore':[c['id'] for c in result['report']['detected_cuts']],'request_id':'cleanup-restore'}
    again=V.cleanup_programme(studio,{'id':'cleanup-restore-fixture','args':restored});assert V.load(studio.sp,p['id'],again['version'])['doc']['duration']==pytest.approx(p['doc']['duration'])


def test_recut_splits_visual_credit_with_linked_pieces(studio,tmp_path):
    from .video_fixtures import picture
    from PIL import Image
    from ave.compile import compile_video
    from ave.config import binary
    import subprocess
    source=picture(tmp_path/'source.mov',seconds=4,prores=False);rec=library.add_file(studio.sp,source,source={'type':'own'},by='synthetic')
    p=V.create(studio,{'title':'Visual recut','mode':'audiovisual','request_id':'visual-recut'});p=V.add_source(studio,{'project':p['id'],'source_id':rec['id'],'expected_revision':1,'request_id':'visual-source'});p=mutate(studio,p,{'kind':'insert','source':rec['id'],'in':0,'out':4});ep,_=V.project(studio.sp,p['id'])
    red=ep.work/'red.png';credit=ep.work/'credit.png';Image.new('RGB',(320,180),(240,0,0)).save(red);Image.new('RGBA',(100,30),(255,255,255,255)).save(credit)
    doc=copy.deepcopy(p['doc']);doc['tracks']['overlays']=[{'id':'b','kind':'broll','file':ep.rel(red),'o0':0.,'o1':4.,'x':0,'y':0,'w':640,'h':360,'mode':'cutaway','fit':'cover','layer':1,'audio':'mute','credit_id':'bc'}, {'id':'bc','kind':'credit','file':ep.rel(credit),'o0':0.,'o1':1.,'x':20,'y':20,'w':100,'h':30,'layer':2,'alpha':True,'credit_for':'b','text':'Synthetic credit'}]
    revised=V.recut_output(doc,[{'id':'c','o0':1,'o1':2,'xfade_ms':0}]);b=[x for x in revised['tracks']['overlays'] if x['kind']=='broll'];c=[x for x in revised['tracks']['overlays'] if x['kind']=='credit']
    assert len(b)==len(c)==2 and all(any(x['id']==y['credit_id'] and x['credit_for']==y['id'] for x in c) for y in b)
    out=tmp_path/'visual-recut.mp4';compile_video(ep,revised,out,preset='preview')
    for at in (.5,1.5):
        raw=subprocess.check_output([str(binary('ffmpeg')),'-v','error','-ss',str(at),'-i',str(out),'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','-']);pixels=np.frombuffer(raw,np.uint8).reshape((360,640,3));assert pixels[200,300,0]>200 and pixels[200,300,1]<40;assert pixels[25,25].mean()>200
