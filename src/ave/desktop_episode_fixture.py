"""Explicit isolated native episode fixture, with real packaged workers.

No picker, OS authorization, Keychain, peer or provider. A caller may supply a
synthetic spoken WAV to exercise the bundled speech path without generating or
downloading a model. All other inputs are generated locally in the new fixture.
"""
from __future__ import annotations
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid


def run(destination, speech_fixture=None):
    root=Path(destination).absolute()
    if root.exists():raise ValueError('Choose a new isolated episode fixture destination')
    root.mkdir(parents=True)
    from .publish import safety
    safety.approval_key=lambda:b'isolated-episode-native-fixture-key-only'
    from .studio.project import StudioProject
    from .studio.service import open_studio
    from .studio.ui.api import StudioApp
    from .studio import episode_registry as R,episode_workshop as E
    from .episode import Episode,write_json
    from .config import binary
    from .compile import compile_audio
    import numpy as np
    import soundfile as sf
    class Authority:
        active=True
        simulated=True
        def require(self):
            if not self.active:raise ValueError('Simulated authority is inactive')
    class Books:
        def capture(self,path):return {'bookmark':str(path),'identity':{'volume':'SIMULATED-fixture-volume','resource':str(Path(path).stat().st_ino)}}
        @contextmanager
        def access(self,binding):
            path=Path(binding['bookmark'])
            if not path.exists() or self.capture(path)['identity']!=binding['identity']:raise ValueError('Simulated offline/replaced volume')
            yield path
    R.NativeBooks=Books # Only this explicitly named fixture dispatch injects it.
    sp=StudioProject(root/'studio');sp.create(title='Simulated native episode registration',tz='UTC');studio=open_studio(sp.root)
    app=StudioApp(studio,fixtures=False,pin='424242',native_authority=Authority());app.workbench._native_cancel=True
    cases=[]
    try:
        for mode in ('audio','audiovisual','camera-gap','source-names'):
            ep=Episode(root/('external-'+mode));ep.create(title='Synthetic external '+mode,layout='numbered' if mode=='audio' else 'flat')
            recorder=ep.raw/'conversation.wav';sr=48000;t=np.arange(sr*6)/sr;signal=.18*np.sin(2*np.pi*(180*t+11*t*t))
            if mode=='audio' and speech_fixture:shutil.copyfile(speech_fixture,recorder)
            else:sf.write(recorder,np.column_stack([signal,signal*.7]),sr,subtype='PCM_24')
            if mode in ('audiovisual','source-names'):
                subprocess.run([str(binary('ffmpeg')),'-v','error','-y','-f','lavfi','-i','color=c=0x315a7b:s=320x180:r=24:d=5.25','-ss','0.75','-i',str(recorder),'-map','0:v','-map','1:a','-c:v','prores_ks','-profile:v','0','-c:a','pcm_s24le','-t','5.25',str(ep.raw/'camera.mov')],check=True)
            if mode=='source-names':
                raw_samples,_=sf.read(recorder)
                sf.write(ep.raw/'camera.wav',-.5*raw_samples[:252000],sr,subtype='PCM_24')
                (ep.raw/'take').mkdir();sf.write(ep.raw/'take/dialogue.wav',raw_samples,sr,subtype='PCM_24');sf.write(ep.raw/'take__dialogue.wav',-.5*raw_samples,sr,subtype='PCM_24')
            if mode=='camera-gap':
                for name,start in [('first',0),('second',4)]:
                    subprocess.run([str(binary('ffmpeg')),'-v','error','-y','-f','lavfi','-i','color=c=0x315a7b:s=320x180:r=24:d=2','-ss',str(start),'-i',str(recorder),'-map','0:v','-map','1:a','-c:v','prores_ks','-profile:v','0','-c:a','pcm_s24le','-t','2',str(ep.raw/(name+'.mov'))],check=True)
            meta_sha=safety.sha256_file(ep.meta_path);row=R.register_selected(studio,ep.root,derived_consent=True);eid=row['id'];jobs=[]
            original_hashes={s['file']:safety.sha256_file(ep.root/s['file']) for s in row['sources']}
            def job(op,**extra):
                loaded=E.load(studio,eid);args={'episode':eid,'grant_revision':loaded['grant_revision'],'input_sha256':loaded['inputs'][op],'session':'main','request_id':str(uuid.uuid4()),**extra}
                queued=app.workbench.start_media(op,args);deadline=time.monotonic()+180
                while time.monotonic()<deadline:
                    current=app.workbench.tail(queued['id'])
                    if current['status'] not in ('queued','running'):break
                    time.sleep(.05)
                if current['status']!='done':raise AssertionError(current)
                jobs.append(current);return current['result']
            def confirm(kind,payload):
                result=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':row['grant_revision'],'kind':kind,'session':'main','payload':payload});req=result['request']
                app.episode_checkpoints.view({'token':result['token'],'payload_sha256':req['payload_sha256'],'preview_sha256':req['dependencies'].get('preview_sha256')});app.episode_checkpoints.prepared[result['token']]['viewed']-=3
                return app.episode_checkpoints.confirm_native(result['token'],'424242')
            from .audio import analysis_wav_path,REGISTERED_ANALYSIS_CONTEXT
            ep.analysis_context=REGISTERED_ANALYSIS_CONTEXT
            source=next(s for s in row['sources'] if s['file'].endswith('conversation.wav'))
            cache=analysis_wav_path(ep,source['file']);cache.parent.mkdir(parents=True,exist_ok=True);sf.write(cache,np.full(4000,.6),16000,subtype='PCM_16');stale_short=safety.sha256_file(cache)
            initial=job('episode-intake');assert safety.sha256_file(cache)!=stale_short
            first_preview=initial['previews'][0];history=studio.sp.work/'production/episode-artifacts'/(first_preview['id']+'.json');historical_bytes=history.read_bytes()
            row=R.register_selected(studio,ep.root,derived_consent=True,reconnect=eid)
            renewed=job('episode-intake');assert all(a['grant_revision']==2 for a in renewed['previews']);assert first_preview['id'] not in {a['id'] for a in renewed['previews']};assert history.read_bytes()==historical_bytes
            reconnect={'initial_preview':first_preview['id'],'renewed_previews':[a['id'] for a in renewed['previews']],'historical_record_unchanged':True}

            if mode=='audio' and speech_fixture:job('episode-transcribe',source_id=source['id'],speakers=2)
            else:write_json(ep.artifact('transcript.json'),{'source':source['file'],'duration':6,'engine':'generated-ground-truth','words':[{'w':'Synthetic','start':1,'end':1.5,'speaker':'S1'},{'w':'conversation.','start':2,'end':2.5,'speaker':'S2'}],'utterances':[{'text':'Synthetic conversation.','speaker':'S1','start':1,'end':2.5}]})
            transcript=R.json_file(ep.artifact('transcript.json'));speakers=sorted({w['speaker'] for w in transcript['words'] if w.get('speaker')})
            confirm('speakers',{'names':{sid:'Synthetic '+sid for sid in speakers}})
            # A full-duration wrong cache and apparently matching editable map
            # cannot bypass the direct native sync consumer's regeneration.
            cache_info=sf.info(cache);sf.write(cache,np.full(cache_info.frames,.6),16000,subtype='PCM_16');stale_full=safety.sha256_file(cache)
            alignment=job('episode-sync',source_id=source['id'],points=3);assert safety.sha256_file(cache)!=stale_full

            cameras={s['id']:{'role':'speaker','speakers':speakers} for s in row['sources'] if s['file'].endswith('.mov')}
            if mode=='camera-gap':
                for camera,speaker in zip(sorted(cameras),speakers):cameras[camera]['speakers']=[speaker]
            confirm('mapping',{'reference':source['id'],'sources':[s['id'] for s in row['sources']],'cameras':cameras});confirm('bounds',{'in':0,'out':6} if mode=='camera-gap' else {'in':1,'out':5.5})

            rendered=job('episode-assembly');confirm('assembly',{**rendered['edit'],'artifact_id':rendered['artifact']['id']})
            assert E.load(studio,eid)['checkpoints']['assembly']['state']=='reviewed exact'
            rec=E.artifact(studio,eid,rendered['artifact']['id']);render_path=studio.sp.root/rec['file']
            decoded=root/(mode+'-decoded.wav');subprocess.run([str(binary('ffmpeg')),'-v','error','-y','-i',str(render_path),'-vn','-c:a','pcm_f32le',str(decoded)],check=True)
            samples,rate=sf.read(decoded);assert abs(len(samples)/rate-(4 if mode=='camera-gap' else 4.5))<.05 and np.std(samples)>.01
            numerical={'decoded_audio_samples':len(samples),'decoded_sample_rate':rate,'decoded_rms':float(np.sqrt(np.mean(samples**2)))}
            if mode in ('audiovisual','source-names'):
                camera=next(s for s in row['sources'] if s['file'].endswith('.mov'));offset=alignment['sync']['files'][camera['file']]['start_offset'];assert abs(offset-.75)<.03
                rgb=list(subprocess.check_output([str(binary('ffmpeg')),'-v','error','-i',str(render_path),'-frames:v','1','-vf','scale=1:1','-pix_fmt','rgb24','-f','rawvideo','-']));assert rgb[2]>rgb[0]+30
                _,doc=E.exact(ep,rendered['edit']);pre=root/'audiovisual-pre-normalization.wav';compile_audio(ep,doc,pre);got,_=sf.read(pre);expected,_=sf.read(recorder);error=float(np.max(np.abs(got-expected[48000:264000])));assert error<2e-6
                numerical.update(measured_camera_offset=offset,first_frame_rgb=rgb,pre_normalization_max_sample_error=error)
            if mode=='camera-gap':
                coverage=E.load(studio,eid)['assemblies'][-1]['coverage'];assert abs(coverage['omitted_inside_seconds']-2)<.03
                _,doc=E.exact(ep,rendered['edit']);pre=root/'camera-gap-pre-normalization.wav';compile_audio(ep,doc,pre);got,_=sf.read(pre);raw,_=sf.read(recorder)
                expected=np.concatenate([raw[:96000],raw[192000:288000]]);assert got.shape==expected.shape;error=float(np.max(np.abs(got-expected)));assert error<2e-6
                numerical.update(coverage=coverage,pre_normalization_max_sample_error=error)
            if mode=='source-names':
                paths=[analysis_wav_path(ep,s['file']) for s in row['sources']];assert len(set(paths))==len(paths);checked=[]
                for index,source_record in enumerate(row['sources']):
                    oracle=root/('source-names-oracle-'+str(index)+'.wav')
                    subprocess.run([str(binary('ffmpeg')),'-v','error','-y','-i',str(ep.root/source_record['file']),'-vn','-map','0:a:0','-ac','1','-ar','16000','-c:a','pcm_s16le',str(oracle)],check=True)
                    got,_=sf.read(paths[index]);expected,_=sf.read(oracle);assert got.shape==expected.shape;error=float(np.max(np.abs(got-expected)));assert error==0
                    checked.append({'source':source_record['file'],'analysis_file':ep.rel(paths[index]),'analysis_sha256':safety.sha256_file(paths[index]),'max_sample_error':error})
                numerical['distinct_analysis_sources']=checked
            assert original_hashes=={s['file']:safety.sha256_file(ep.root/s['file']) for s in row['sources']}
            restarted=StudioApp(studio,fixtures=False,pin='424242',native_authority=Authority());state=E.load(restarted.studio,eid);restarted.close()
            R.revoke(studio,eid)
            try:E.load(studio,eid)
            except ValueError:revocation_blocked=True
            else:revocation_blocked=False
            assert revocation_blocked
            cases.append({'mode':mode,'ownership':row['ownership'],'registration':{'id':eid,'root_identity':row['binding']['identity'],'grant_revision':row['grant_revision']},'source_sha256':original_hashes,
                          'transcript_sha256':safety.sha256_file(ep.artifact('transcript.json')),'transcript_engine':transcript['engine'],'word_count':len(transcript['words']),
                          'original_metadata_sha256':meta_sha,'current_metadata_sha256':safety.sha256_file(ep.meta_path),'restart_review':state['checkpoints']['assembly'],
                          'revocation_blocked':revocation_blocked,'reconnect':reconnect,'cache_repair':{'short_sha256':stale_short,'full_wrong_sha256':stale_full,'current_sha256':safety.sha256_file(cache)},'numerical':numerical,'render':rec,'jobs':jobs})
        (root/'results.json').write_text(json.dumps({'ok':True,'simulated_native_boundary':True,'native_picker':False,'os_authentication':False,'keychain':False,'cases':cases},indent=2));return 0
    finally:app.close()
