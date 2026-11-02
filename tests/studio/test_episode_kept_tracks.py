"""Native kept optional tracks survive supported edits and real compilation."""
import copy
import json
import subprocess
import uuid
import numpy as np
import pytest
from PIL import Image
from ave import editdoc,library
from ave.compile import compile_audio
from ave.config import binary
from ave.occurrences import mapped_words
from ave.studio import episode_registry as R,episode_workshop as E,episode_release as L,video_workshop as V
from ave.studio.workbench import Workbench
from .test_workbench import studio
from .test_episode_release import finished,release_job,wait_job
from .test_episode_workshop import run,confirm,recording
from .test_video_workshop import mutate


def test_native_kept_optional_tracks_reorder_remove_and_adoption(studio,tmp_path,synthetic_final_asr):
    eid,original,app,first=finished(studio,tmp_path,video=True)
    image=tmp_path/'red-reference.png';Image.new('RGB',(320,180),(240,20,30)).save(image)
    entry={'source':'synthetic','url':'https://example.org/fixture','license':'CC BY','license_url':'https://example.org/license','license_text':'Synthetic test license','attribution':{'required':True,'text':'Red reference by fixture'}}
    picture=library.add_file(studio.sp,image,source={'type':'import'},license_entry=entry,by='synthetic',attested=False)
    bed=library.add_file(studio.sp,recording(tmp_path,'optional-bed.wav',duration=10),source={'type':'own'},by='synthetic',attested=False)
    for rec in (picture,bed):library.attest_license(studio.sp,rec['id'],'simulated fixture')
    with R.resolve(studio,eid) as (ep,row):
        old=E.exact(ep,first)[1];spec=old['final_spec']
        for rec in library.read_all(studio.sp).values():
            if rec['source']['type']=='own':library.attest_license(studio.sp,rec['id'],'simulated fixture')
    final=run(studio,eid,'episode-final',rough=spec['rough'],sequence=spec['sequence'],gain_db=-3,caption_style='podcast',burn=False,
              music={'id':bed['id'],'sha256':bed['sha256'],'start':0,'gain_db':-22,'duck_db':-9,'fade_in_s':.3,'fade_out_s':.4},
              broll=[{'id':picture['id'],'sha256':picture['sha256'],'start':.3,'duration':1.,'source_in':0,'ken_burns':False}])
    identity={**final['edit'],'artifact_id':final['artifact']['id']};confirm(app,eid,'final',identity)
    wb=Workbench(studio)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (source_ep,row):
            args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(source_ep,row,'episode-select','main'),'request_id':str(uuid.uuid4()),'session':'main','final':identity,'start':0,'end':3.5,'title':'First and second thoughts'}
        selected=wait_job(wb,wb.start_media('episode-select',args))
        keep={**selected['edit'],'artifact_id':selected['artifact']['id'],'complete_statement':True,'context_reviewed':True};confirm(app,eid,'keep',keep,session=keep['name'])
        descriptor=next(c for c in V.kept_clips(studio.sp)['clips'] if c['source_id']==eid);descriptor.pop('title')
        p=wait_job(wb,wb.start_media('video-kept',{**descriptor,'request_id':'optional-kept'}));before=copy.deepcopy(p['doc']);ep,_=V.project(studio.sp,p['id'])
        assert before['tracks']['music'] and {o['kind'] for o in before['tracks']['overlays']}=={'broll','credit'}
        assert before['caption_snapshot'] and mapped_words(ep,before)
        # Split an existing occurrence, then move it; dependent pieces follow
        # exact source intervals rather than being reattached by array index.
        first_clip=p['doc']['clips'][0]
        split=(first_clip['in']+first_clip['out'])/2
        p=mutate(studio,p,{'kind':'split','clip':first_clip['id'],'at':split})
        moved=p['doc']['clips'][0]['id'];p=mutate(studio,p,{'kind':'move','clip':moved,'to':len(p['doc']['clips'])-1})
        assert p['doc']['kept_clip']==before['kept_clip'] and p['doc']['caption_snapshot']==before['caption_snapshot']
        assert p['doc']['tracks']['music'] and any(o['kind']=='credit' for o in p['doc']['tracks']['overlays'])
        # Output samples must be the same mapped programme samples, including
        # the authored music fades and dialogue gains (outside join samples).
        import soundfile as sf
        initial=tmp_path/'initial.wav';changed=tmp_path/'changed.wav'
        # Automatic ducking deliberately follows the new dialogue; isolate
        # authored gains/fades here while the actual render retains duck_db.
        original_mix=copy.deepcopy(before);changed_mix=copy.deepcopy(p['doc'])
        assert all(m['duck_db']==-9 for m in changed_mix['tracks']['music'])
        for d in (original_mix,changed_mix):
            for m in d['tracks']['music']:m['duck_db']=0
        compile_audio(ep,original_mix,initial);compile_audio(ep,changed_mix,changed)
        a,sr=sf.read(initial);b,_=sf.read(changed);cut=round((split-first_clip['in'])*sr)
        expected=np.concatenate([a[cut:],a[:cut]])
        assert b.shape==expected.shape
        assert np.max(abs(b[2000:-2000]-expected[2000:-2000]))<3e-5
        # Exercise the inherited native renderer, including linked credit pixels.
        render_job=wb.start_media('video-render',{'project':p['id'],'version':p['version'],'sha256':p['sha256'],'preset':'master','request_id':'optional-kept-render'});rendered=wait_job(wb,render_job)
        assert rendered['native_keep']['source_id']==eid
        broll=next(o for o in p['doc']['tracks']['overlays'] if o['kind']=='broll')
        frame=subprocess.check_output([str(binary('ffmpeg')),'-v','error','-ss',str((broll['o0']+broll['o1'])/2),'-i',str(studio.sp.root/rendered['assets'][0]['file']),'-frames:v','1','-vf','crop=iw:ih/2:0:0,scale=1:1','-pix_fmt','rgb24','-f','rawvideo','-'])
        assert frame[0]>150 and frame[0]>frame[1]*2
        from ave.studio import composer,media_workshop as M,store,model
        from ave.studio.events import Actor
        from .test_episode_release_attachment import bounded_attachment
        actor=Actor('human','synthetic');post=model.new_post(idea='Kept video attachment',by='synthetic');post=store.save_post(studio.sp,post,expected_rev=None,actor=actor,op='test')
        composer.media_action(studio,post['id'],{'op':'add','asset':picture['id'],'expected_rev':post['rev']},actor);post=store.load_post(studio.sp,post['id'])
        target={'post':post['id'],'variant':None,'item':post['media'][0]['id'],'expected_rev':post['rev']}
        with bounded_attachment():M.attach(studio,{'job_id':render_job['id'],'asset':rendered['assets'][0]['asset'],'target':target},actor)
        assert store.load_post(studio.sp,post['id'])['media'][0]['asset']==rendered['assets'][0]['asset']
        with pytest.raises(model.RevConflict):M.attach(studio,{'job_id':render_job['id'],'asset':rendered['assets'][0]['asset'],'target':target},actor)
        # Removing the picture occurrence removes only its linked placements.
        p=mutate(studio,p,{'kind':'remove','clip':moved})
        assert all(o.get('credit_for') in {v['id'] for v in p['doc']['tracks']['overlays'] if v['kind']!='credit'} for o in p['doc']['tracks']['overlays'] if o['kind']=='credit')
        adopted=wait_job(wb,wb.start_media('episode-adopt',{'project':p['id'],'version':p['version'],'sha256':p['sha256'],'revision':p['revision'],'title':'Optional tracks adopted','request_id':'optional-adopt'}))
        assembly=run(studio,adopted['episode'],'episode-assembly');confirm(app,adopted['episode'],'assembly',{**assembly['edit'],'artifact_id':assembly['artifact']['id']})
        rough=run(studio,adopted['episode'],'episode-rough',assembly={**assembly['edit'],'artifact_id':assembly['artifact']['id']},policy='gentle',restore=[])
        with R.resolve(studio,adopted['episode']) as (target,row):
            doc=E.exact(target,rough['edit'])[1];assert doc['tracks']['music'];assert mapped_words(target,doc)
            from ave.studio.episode_worker_bridge import Parent
            from ave.studio.workbench import write,job_path
            pending_args={'episode':row['id'],'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(target,row,'episode-assembly','main'),'request_id':str(uuid.uuid4()),'session':'main'}
        pending={'id':'job_'+uuid.uuid4().hex[:24],'operation':'episode-assembly','args':pending_args,'status':'queued'}
        pending['episode_descriptor']=E.descriptor(studio,pending['operation'],pending_args);write(studio.sp,job_path(studio.sp,pending['id']),pending)
        parent=Parent(studio,pending)
        try:
            with pytest.raises(ValueError,match='Wrong-job'):parent.handle({'job_id':'job_wrong','action':'final-broll','data':{}})
            with pytest.raises(ValueError,match='outside'):parent.handle({'job_id':pending['id'],'action':'final-broll','data':{'asset':picture['id']}})
            from ave.studio import seal
            from ave.assets import licenses
            entry_hash=licenses.entry_hash(library.registry.license_log(studio.sp).find(sha256=picture['sha256']))
            seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':picture['id'],'sha256':picture['sha256'],'entry':entry_hash,'purpose':'license-use','by':'simulated fixture'})
            with pytest.raises(ValueError,match='changed'):parent.handle({'job_id':pending['id'],'action':'snapshot','data':{}})
        finally:parent.close()
    finally:wb.close();app.close()
