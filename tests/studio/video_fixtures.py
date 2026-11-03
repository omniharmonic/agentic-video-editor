"""Small deterministic media and explicitly synthetic kept authority for portable regressions."""
import copy
import hashlib
import json
import subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from ave import clips, editdoc, media
from ave.config import binary
from ave.episode import Episode, write_json


def tone(path, seconds=8, rate=48000):
    t = np.arange(round(seconds * rate)) / rate
    # Chirped, distinct stereo channels expose wrong clocks/channels and seam handling.
    data = np.column_stack([.15 * np.sin(2*np.pi*(173*t+17*t*t)), .09*np.cos(2*np.pi*(311*t+9*t*t))])
    sf.write(path, data, rate, subtype='PCM_24')
    return path


def picture(path, seconds=8, width=640, height=360, prores=True, timecode=False):
    wav = tone(path.with_suffix('.wav'), seconds)
    args = [binary('ffmpeg'), '-v', 'error', '-y']
    if timecode:
        frames = []
        y, x = np.indices((height, width))
        for n in range(seconds * 24):
            frames.append(np.stack([np.full_like(x, n % 256), x * 255 // width, y * 255 // height], axis=-1).astype('uint8'))
        args += ['-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{width}x{height}', '-r', '24', '-i', 'pipe:0']
        raw = np.stack(frames).tobytes()
    else:
        args += ['-f', 'lavfi', '-i', f'color=c=0x335978:s={width}x{height}:r=24:d={seconds}']
        raw = None
    args += ['-i', str(wav), '-t', str(seconds), '-c:v', 'prores_ks' if prores else 'ffv1', '-pix_fmt', 'yuv422p10le' if prores else 'bgr0', '-c:a', 'pcm_s24le', str(path)]
    subprocess.run(args, input=raw, check=True)
    return path


def calibration(directory):
    directory.mkdir(exist_ok=True)
    rate = 48000
    t = np.arange(rate*30)/rate
    signal = np.column_stack([.2*np.sin(2*np.pi*440*t)*(t<10).__or__(t>=20), .2*np.sin(2*np.pi*660*t)*(t>=10)])
    sf.write(directory/'channel-calibration-30s.wav', signal, rate, subtype='PCM_24')
    tone(directory/'original-tonal-bed-64s.wav', 64)
    return directory


def registered_kept(studio, source, original, words):
    """No approval API: fixture ledger is explicitly synthetic and never production authority."""
    sp = studio.sp
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    sid = 'recording_' + sha
    ep = Episode(sp.work/'production'/'episodes'/sid)
    ep.create()
    meta=json.loads(ep.meta_path.read_text());meta.update(layout='flat',workbench_source=sid);write_json(ep.meta_path,meta)
    target = ep.raw/source.name
    target.write_bytes(source.read_bytes())
    probe=media.probe_file(target,ep.root)
    write_json(ep.work/'media.json', {'files':[probe]})
    doc = copy.deepcopy(original)
    for value in doc['sources'].values():
        value['file'] = ep.rel(target)
    write_json(ep.artifact('transcript.json'), {'words': words})
    rough = copy.deepcopy(doc)
    rough['stage'] = 'rough'
    editdoc.save_new_version(ep, rough)
    fingerprint = clips.Sentences(ep, rough, clips.load_rubric(ep)).fingerprint()
    doc['stage'] = 'select'
    editdoc.save_new_version(ep, doc, name='fixture')
    write_json(ep.work/'clips.json', {'clips': [{'id': 'fixture', 'title': 'Synthetic kept test',
        'source': {'stage': 'rough', 'name': None, 'version': 1, 'fingerprint': fingerprint}, 'edit': {'name': 'fixture', 'version': 1}}]})
    ledger = ep.work/'learning'/'clip_decisions.jsonl'
    ledger.parent.mkdir(exist_ok=True)
    ledger.write_text(json.dumps({'clip_id': 'fixture', 'verdict': 'keep', 'via': 'ui', 'by': 'synthetic fixture authority'})+'\n')
    write_json(sp.work/'production'/'sources.json', {'v': 1, 'sources': [{'source_id': sid, 'kind': 'video' if source.suffix != '.wav' else 'audio', 'ready': True, 'duration':probe['duration'], 'has_audio':bool(probe['audio']), 'file': sp.rel(target), 'sha256': sha, 'label': source.name}]})
    from ave.studio import video_workshop as V
    descriptor = V.kept_clips(sp)['clips'][0]
    args = {k: v for k, v in descriptor.items() if k != 'title'}
    args['request_id'] = 'synthetic-kept-' + sha[:12]
    return ep, doc, args
