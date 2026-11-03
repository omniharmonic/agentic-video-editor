"""Review regressions: changing clocks, preserved controls, repeated speech and bounded source words."""
import copy
import hashlib
import json
import tempfile
import subprocess
from fractions import Fraction
from pathlib import Path
import numpy as np
import soundfile as sf
import pytest
from ave import editdoc, compile as compiler
from ave.config import binary
from ave.occurrences import mapped_words
from ave.studio import video_workshop as V
from .test_workbench import studio
from .test_video_workshop import mutate
from .video_fixtures import picture, tone, registered_kept

EVIDENCE = Path(tempfile.gettempdir())/'ave-media-video-fix1-evidence'
EVIDENCE.mkdir(exist_ok=True)


def original_doc(mode='audiovisual', duration=4, offset=0, clock=None):
    src = {'file': 'fixture.mkv', 'kind': 'video' if mode != 'audio' else 'audio', 'start_offset': offset, 'drift_ppm': 80}
    if clock: src['sync_map'] = clock
    doc = editdoc.new_doc('select', 24, (160, 96), {'mic': src}, duration, media_mode=mode)
    doc['tracks']['dialogue'] = [{'id': 'd1', 'src': 'mic', 'o0': 0, 'o1': duration, 'e0': offset}]
    if mode != 'audio': doc['tracks']['video'] = [{'id': 'v1', 'src': 'mic', 'o0': 0, 'o1': duration, 'e0': offset}]
    return doc


def decode_video(path):
    data = subprocess.check_output([binary('ffmpeg'), '-v', 'error', '-i', str(path), '-an', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'])
    return np.frombuffer(data, dtype='uint8').reshape(-1, 96, 160, 3)


def test_repeated_kept_correction_and_disconnected_guard(studio, tmp_path):
    source = tone(tmp_path/'repeat.wav')
    doc = original_doc('audio')
    doc['tracks']['dialogue'] = [{'id': f'd{i}', 'src': 'mic', 'o0': i*2, 'o1': i*2+2, 'e0': 0} for i in range(2)]
    ep, original, args = registered_kept(studio, source, doc, [{'w': 'writing', 'start': 0, 'end': .28}])
    p = V.adapt_kept(studio, args)
    target_ep, _ = V.project(studio.sp, p['id'])
    words = mapped_words(target_ep, p['doc'])
    assert len(words) == 2 and words[0]['occurrence_id'] != words[1]['occurrence_id']
    c = {k: words[0][k] for k in ('occurrence_id', 'transcript_sha256')}
    c.update(word_ids=[words[0]['word_id']], text='CORRECTED')
    p = mutate(studio, p, {'kind': 'captions', 'correction': c})
    fixed = mapped_words(target_ep, p['doc'])
    assert [w['w'] for w in fixed] == ['CORRECTED', 'writing']
    assert np.allclose([(w['start'], w['end']) for w in fixed], [(0, .28), (2, 2.28)])
    broken = copy.deepcopy(p['doc'])
    for segment in broken['tracks']['dialogue']: segment['occurrence_id'] = c['occurrence_id']
    with pytest.raises(ValueError, match='disconnected output spans'): mapped_words(target_ep, broken)
    # Model a persisted pre-repair identity collision; authoring must refuse before a new version is saved.
    editdoc.save_new_version(target_ep, broken)
    legacy=V.load(studio.sp,p['id']);versions=editdoc.list_versions(target_ep,'short')
    with pytest.raises(ValueError, match='disconnected output spans'):
        mutate(studio,legacy,{'kind':'captions','correction':c})
    assert editdoc.list_versions(target_ep,'short')==versions
    (EVIDENCE/'repeat-correction.json').write_text(json.dumps(fixed, indent=2))


def test_changing_clock_video_plan_and_post_trim_audio(studio, tmp_path):
    source = picture(tmp_path/'clock.mkv', seconds=6, width=160, height=96, prores=False, timecode=True)
    doc = original_doc(clock=[[0, 0], [2, .4], [4, 0]])
    ep, original, args = registered_kept(studio, source, doc, [{'w': 'knot', 'start': 2.2, 'end': 2.6}])
    p = V.adapt_kept(studio, args)
    # Extend a single clip across both changing slopes; every supported edit rebuild uses the same clock-aware compiler.
    p = mutate(studio, p, {'kind': 'trim', 'clip': p['doc']['clips'][0]['id'], 'in': .25, 'out': 3.75})
    p = mutate(studio, p, {'kind': 'split', 'clip': p['doc']['clips'][0]['id'], 'at': 2.1})
    p = mutate(studio, p, {'kind': 'join', 'clip': p['doc']['clips'][0]['id']})
    target_ep, _ = V.project(studio.sp, p['id'])
    plan = compiler._plan_video(p['doc'], Fraction(24), [])
    assert len(plan) == 2 and [round(x['clock_rate'], 6) for x in plan] == [1.2, .8]
    long_doc=original_doc(duration=200,clock=[[0,0],[100,.4],[200,0]])
    long_plan=compiler._plan_video(long_doc,Fraction(24),[])
    assert len(long_plan)==2
    assert abs(editdoc.src_time(long_doc['sources']['mic'],200)-200)<1e-12
    audio = tmp_path/'mapped.wav'
    compiler.compile_audio(target_ep, p['doc'], audio)
    got, sr = sf.read(audio, dtype='float32')
    raw, raw_sr = sf.read(source.with_suffix('.wav'), dtype='float32')
    e0 = editdoc.episode_time(p['doc']['sources']['mic'], .25)
    times = e0 + np.arange(len(got))/sr
    positions = editdoc.src_time(p['doc']['sources']['mic'], times)*raw_sr
    expected = np.stack([np.interp(positions, np.arange(len(raw)), raw[:, c]) for c in range(2)], axis=1)
    error = float(np.max(abs(got-expected)))
    assert error < 2e-6
    output = tmp_path/'clock-output.mp4'
    compiler.render(target_ep, p['doc'], output, preset='master', burn_captions=False)
    pixels = decode_video(output)
    observed = [float(pixels[n, 30:60, 40:80, 0].mean()) for n in (12, 48, 72)]
    wanted = [float(editdoc.src_time(p['doc']['sources']['mic'], e0+n/24)*24) for n in (12, 48, 72)]
    assert max(abs(a-b) for a,b in zip(observed,wanted)) < 4
    assert abs(len(pixels)/24-p['doc']['duration']) <= 1/24
    # Scalar/vector mapping agrees through knots and extrapolated crossfade handles.
    probes = np.array([-.1, 0, 2.4-1e-6, 2.4, 2.4+1e-6, 4, 4.1])
    assert np.allclose(editdoc.src_time(doc['sources']['mic'], probes), [editdoc.src_time(doc['sources']['mic'], float(t)) for t in probes], atol=1e-12)
    (EVIDENCE/'changing-clock.json').write_text(json.dumps({'plan': plan, 'sample_max_error': error, 'sample_tolerance': 2e-6, 'observed_frame_red': observed, 'expected_source_frame': wanted, 'frame_tolerance': 4, 'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest()}, indent=2))


@pytest.mark.parametrize('overlap',[False,True])
def test_kept_controls_preserve_actual_picture_audio(studio, tmp_path, overlap):
    source = picture(tmp_path/'controls.mkv', seconds=6, width=160, height=96, prores=False, timecode=True)
    doc = original_doc()
    doc['sources']['mic']['drift_ppm'] = 0
    doc['sources']['mic']['sync_map'] = [[0,0],[2,.4],[4,0]]
    doc['tracks']['video'] = [dict(id=f'v{i}', src='mic', o0=a, o1=b, e0=.5+a, crop=[8, 4, 144, 88], zoom=1.5) for i,(a,b) in enumerate([(0,1),(1,2),(2,4)])]
    doc['tracks']['dialogue'] = [dict(id='d1',src='mic',o0=0,o1=2,e0=.5,channel='left',gain_db=-3,fade_in_s=.4,fade_out_s=.6,fade_in_offset_s=.2,fade_out_remaining_s=.1), dict(id='d2',src='mic',o0=2,o1=4,e0=3,channel='right',gain_db=-4,xfade_ms=80,fade_in_s=.3,fade_out_s=.4,fade_in_offset_s=.1,fade_out_remaining_s=.2)]
    if overlap:
        doc['tracks']['dialogue'][0]['o1']=2.02
        doc['tracks']['dialogue'][1]['o0']=1.98
    ep, original, args = registered_kept(studio, source, doc, [{'w':'settings','start':.6,'end':.9}])
    p = V.adapt_kept(studio, args)
    target_ep, _ = V.project(studio.sp, p['id'])
    assert len(p['doc']['tracks']['dialogue']) == 2
    assert p['doc']['tracks']['dialogue'][1]['xfade_ms'] == 80
    outputs=[]
    for label, episode, edit in [('source',ep,original),('import',target_ep,p['doc'])]:
        pcm = tmp_path/(label+'.wav'); compiler.compile_audio(episode,edit,pcm)
        video = tmp_path/(label+'.mp4'); compiler.render(episode,edit,video,preset='master',burn_captions=False)
        outputs.append((sf.read(pcm,dtype='float32')[0],decode_video(video)))
    sample_error=float(np.max(abs(outputs[0][0]-outputs[1][0])))
    pixel_error=int(np.max(abs(outputs[0][1].astype(int)-outputs[1][1].astype(int))))
    assert sample_error < 1e-7 and pixel_error == 0
    (EVIDENCE/('kept-controls-overlap.json' if overlap else 'kept-controls.json')).write_text(json.dumps({'sample_max_error':sample_error,'sample_tolerance':1e-7,'pixel_max_error':pixel_error,'dialogue':p['doc']['tracks']['dialogue'],'picture':p['doc']['tracks']['video']},indent=2))


def test_source_words_file_clock_coverage_and_provenance(studio,tmp_path):
    source=tone(tmp_path/'offset.wav',3)
    doc=original_doc('audio',duration=1.5,offset=2)
    words=[{'w':'before','start':.2,'end':.5},{'w':'edge','start':1.9,'end':2.1},{'w':'local','start':2.6,'end':2.8},{'w':'after','start':6,'end':6.5}]
    ep,original,args=registered_kept(studio,source,doc,words)
    original_hash=hashlib.sha256(ep.artifact('transcript.json').read_bytes()).hexdigest()
    p=V.adapt_kept(studio,args)
    page=V.transcript_page(studio.sp,p['id'],'mic')
    assert [w['w'] for w in page['words']]==['edge','local']
    assert page['words'][0]['start']==0
    local=page['words'][1]
    assert abs(local['start']-(.6/1.00008))<1e-10 and local['transcript_start']==2.6 and local['transcript_clock']=='episode'
    assert hashlib.sha256(ep.artifact('transcript.json').read_bytes()).hexdigest()==original_hash


def test_renderer_revision_preserves_old_request_and_rebuilds_new(studio,tmp_path,monkeypatch):
    from ave import library
    source=tone(tmp_path/'render.wav',2)
    rec=library.add_file(studio.sp,source,source={'type':'own'},by='synthetic fixture authority')
    p=V.create(studio,{'title':'Renderer revision','mode':'audio','request_id':'renderer-revision'})
    p=V.add_source(studio,{'project':p['id'],'source_id':rec['id'],'expected_revision':1,'request_id':'renderer-source'})
    p=mutate(studio,p,{'kind':'insert','source':rec['id'],'in':0,'out':1})
    args={'project':p['id'],'version':p['version'],'sha256':p['sha256'],'preset':'wav','request_id':'legacy-request'}
    monkeypatch.setattr(V,'RENDERER','ave.compile.v1');old=V.render(studio,args)
    old_asset=library.get(studio.sp,old['assets'][0]['asset']);monkeypatch.setattr(V,'RENDERER','ave.compile.v2-clock-occurrences')
    real=compiler.compile_audio;calls=[]
    def counted(*a,**k):calls.append(True);return real(*a,**k)
    monkeypatch.setattr(compiler,'compile_audio',counted)
    assert V.render(studio,args)==old and not calls
    new=V.render(studio,{**args,'request_id':'new-render-request'})
    assert len(calls)==1 and new['assets'][0]['asset']!=old['assets'][0]['asset']
    assert library.get(studio.sp,old['assets'][0]['asset'])==old_asset
    assert library.get(studio.sp,new['assets'][0]['asset'])['origin']['renderer']==V.RENDERER
