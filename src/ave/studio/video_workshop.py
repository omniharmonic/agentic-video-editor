"""Bounded independent-recording authoring on shared EditDocs, sources and jobs.

No runtime approval, arbitrary paths, code or FFmpeg filters cross this boundary.
"""
from __future__ import annotations
from ..atomic import publish as publish_atomic
import copy
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote
from .. import captions, editdoc, library, media
from ..episode import Episode
from ..config import binary
from ..publish.safety import sha256_file
from . import model, production, store, desk
from .events import Actor
from .workbench import safe, read, write, recording, episode_for, _copy_immutable

RENDERER = 'ave.compile.v2-clock-occurrences'
OPS = ('video-source', 'video-render', 'video-kept', 'video-cleanup')
PROJECT = re.compile(r'^project_[a-f0-9]{24}$')
SHA = re.compile(r'^[a-f0-9]{64}$')


def number(x, lo, hi, label, integer=False):
    if type(x) not in ((int,) if integer else (int,float)) or not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f'{label} must be {"an integer" if integer else "a number"} from {lo} to {hi}')
    return x


def digest(value): return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
def url(sp,p): return '/files/' + quote(sp.rel(p), safe='/')
def request(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',value): raise ValueError('A stable request ID is required')
    return value


def safe_dir(sp,path):
    path=Path(path);desk._safe_path(path,sp.root)
    return path


def project(sp,pid):
    if not isinstance(pid,str) or not PROJECT.fullmatch(pid): raise ValueError('Choose a managed editing project')
    ep=Episode(safe_dir(sp,sp.work/'production'/'episodes'/pid))
    meta=read(sp,ep.meta_path)
    if meta.get('workbench_project')!=pid or meta.get('layout')!='flat': raise ValueError('Project ownership mismatch')
    return ep,meta


def caption_snapshot(sp,ep,doc,*,granted_episode=False):
    previous=doc.get('caption_snapshot')
    style=captions.get_style(doc['caption_edit']['style'])
    font=captions.resolve_font(style['fonts'],bold=bool(style.get('bold')))
    if not font.get('file') or not font.get('bundled'): raise ValueError('Caption rendering requires the bundled font')
    resource={'family':font['family'],'file_name':Path(font['file']).name,'sha256':sha256_file(Path(font['file']))}
    snapshot={'schema':'caption_snapshot.v1','edit':doc['caption_edit'],'style':style,'font':resource,'timing_adjustment_limit_s':captions.timing_adjustment_limit(ep)}
    data=(json.dumps(snapshot,sort_keys=True,ensure_ascii=False,allow_nan=False)+'\n').encode();sha=hashlib.sha256(data).hexdigest()
    if granted_episode:
        from .episode_registry import regular
        path=regular(ep.captions/'snapshots'/(sha+'.json'))
    else:path=safe(sp,ep.captions/'snapshots'/(sha+'.json'))
    if path.exists():
        if sha256_file(path)!=sha: raise ValueError('Caption snapshot bytes changed')
    else:
        path.parent.mkdir(parents=True,exist_ok=True)
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(dir=path.parent,delete=False) as f: f.write(data);f.flush();os.fsync(f.fileno());temp=Path(f.name)
        try:
            try: publish_atomic(temp,path)
            except FileExistsError:
                if sha256_file(path)!=sha: raise ValueError('Caption snapshot collision')
        finally: temp.unlink(missing_ok=True)
    doc['caption_snapshot']={'file':ep.rel(path),'sha256':sha}


def pinned_caption(sp,ep,doc,*,granted_episode=False):
    ref=doc.get('caption_snapshot')
    if not ref: raise ValueError('Save a new edit revision to snapshot the caption style and font')
    if granted_episode:
        from .episode_registry import regular
        path=regular(ep.root/ref['file'])
        if not path.is_relative_to(regular(ep.captions,directory=True)):raise ValueError('Caption snapshot leaves the granted captions folder')
    else:path=safe(sp,ep.root/ref['file'])
    if sha256_file(path)!=ref['sha256']: raise ValueError('Caption snapshot changed')
    snapshot=json.loads(path.read_text()) if granted_episode else read(sp,path)
    if snapshot['edit']!=doc['caption_edit']: raise ValueError('Caption correction identity differs')
    font=captions.resolve_font(snapshot['style']['fonts'],bold=bool(snapshot['style'].get('bold')))
    if not font.get('file') or sha256_file(Path(font['file']))!=snapshot['font']['sha256']: raise ValueError('Pinned caption font changed')
    return snapshot


def create(studio,body):
    production.only(body,{'title','mode','request_id'})
    rid=request(body.get('request_id')); mode=body.get('mode')
    if mode not in ('audiovisual','audio','silent_video'): raise ValueError('Choose video with sound, audio, or silent video')
    title=production.text(body.get('title'),'Project title',120,True)
    # Request-derived identity makes a lost create response recoverable.
    pid='project_'+digest({'request_id':rid})[:24];sp=studio.sp
    root=safe_dir(sp,sp.work/'production'/'episodes'/pid);ep=Episode(root)
    root.mkdir(parents=True,exist_ok=True)
    with editdoc.mutation_lock(ep):
        if ep.meta_path.exists():
            meta=read(sp,ep.meta_path)
            if meta.get('create')!=body: raise ValueError('Project request belongs to different parameters')
        else:
            write(sp,ep.meta_path,{'title':body['title'],'layout':'flat','workbench_project':pid,'project_rev':1,'project_sources':{},'create':body,'speakers':{},'cameras':{},'stages':{}})
        if editdoc.latest_path(ep,'short') is None:
            ep.create()
            doc=editdoc.new_doc('short',24,(640,360),{},media_mode=mode)
            doc['independent_project']=pid;doc['clips']=[];doc['caption_edit']={'style':'podcast','burn':mode!='audio','corrections':[]}
            caption_snapshot(sp,ep,doc)
            editdoc.save_new_version(ep,doc,expected_version=0,locked=True)
    return load(sp,pid)


def catalog(sp):
    directory=safe_dir(sp,sp.work/'production'/'episodes')
    rows=[]
    for p in sorted(directory.glob('project_*'))[:200]:
        ep,meta=project(sp,p.name)
        rows.append({'id':p.name,'title':meta['title'],'revision':meta['project_rev']})
    return {'projects':rows}


def exact(sp,pid,version=None,sha=None):
    ep,meta=project(sp,pid)
    if version is None: path=editdoc.latest_path(ep,'short')
    else: number(version,1,1000000,'Exact edit version',True);path=editdoc.path_for(ep,'short',version)
    if path is None: raise ValueError('Project has no edit')
    safe(sp,path); doc=read(sp,path);actual=sha256_file(path)
    if sha is not None and (not isinstance(sha,str) or not SHA.fullmatch(sha) or sha!=actual): raise ValueError('Exact EditDoc identity changed')
    return ep,meta,path,doc,actual


def load(sp,pid,version=None):
    ep,meta,path,doc,sha=exact(sp,pid,version)
    rows=[]
    for key,s in meta['project_sources'].items():
        s={**s,**doc['sources'].get(key,{})}
        row={k:v for k,v in s.items() if k not in ('transcript',)}
        row['key']=key; row['url']=url(sp,ep.root/s['proxy']);row['transcript_ready']=bool(s.get('transcript'))
        row['waveform']=read(sp,ep.root/s['waveform']) if s.get('waveform') else {'peaks':[]}
        rows.append(row)
    outputs=[]
    for jp in sorted((sp.work/'production'/'jobs').glob('job_*.json'))[-500:]:
        job=read(sp,jp)
        if job.get('operation')=='video-render' and job.get('status')=='done' and job.get('args',{}).get('project')==pid and job.get('result'):
            outputs.append({**job['result'],'job_id':job['id'],'job_created':job.get('created','')})
    outputs.sort(key=lambda r:(r['version'],r['job_created']),reverse=True)
    return {'outputs':outputs[:30],'id':pid,'title':meta['title'],'revision':meta['project_rev'],'version':int(editdoc._V.match(path.name)['n']),
            'sha256':sha,'versions':editdoc.list_versions(ep,'short'),'doc':doc,'sources':rows}


def input_source(sp,sid):
    if isinstance(sid,str) and sid.startswith('a_'):
        rec=library.get(sp,sid); original=safe(sp,sp.root/rec['file'])
        if rec['kind'] not in ('audio','video'): raise ValueError('Choose a recording')
        row={'source_id':sid,'sha256':rec['sha256'],'file':rec['file'],'label':Path(rec['file']).name,'kind':rec['kind'],'parent':rec['id']}
    else:
        row=recording(sp,sid)
        if not row or row['kind'] not in ('audio','video') or not row.get('ready'): raise ValueError('Choose an imported recording; wait for its import job')
        row=dict(row);original=safe(sp,sp.root/row['file'])
    if sha256_file(original)!=row['sha256']: raise ValueError('Registered source bytes changed')
    return row,original


def validate_operation(sp,op,args):
    if op=='video-kept': return resolve_kept(sp,args)[1],None
    if op=='video-cleanup':
        production.only(args,{'project','version','sha256','base_version','base_sha256','policy','restore','request_id'})
        request(args.get('request_id'));number(args.get('version'),1,1000000,'Exact edit version',True)
        if not isinstance(args.get('sha256'),str) or not SHA.fullmatch(args['sha256']):raise ValueError('Exact EditDoc SHA-256 required')
        ep,meta,path,doc,sha=exact(sp,args.get('project'),args['version'],args['sha256'])
        if args.get('policy') not in ('balanced','gentle'):raise ValueError('Choose a cleanup approach')
        if not isinstance(args.get('restore'),list) or len(args['restore'])>10000 or any(not isinstance(x,str) for x in args['restore']):raise ValueError('Invalid restore choices')
        if not doc['duration'] or editdoc.effective_mode(doc)=='silent_video':raise ValueError('Cleanup requires a programme with dialogue')
        if args.get('base_version') is not None:
            number(args['base_version'],1,1000000,'Cleanup source version',True)
            if not isinstance(args.get('base_sha256'),str) or not SHA.fullmatch(args['base_sha256']):raise ValueError('Exact cleanup source identity required')
            doc=exact(sp,args['project'],args['base_version'],args['base_sha256'])[3]
        recut_output(doc,[]) # reject unsupported effects before writes
        return doc,None
    if op=='video-source':
        production.only(args,{'project','source_id','expected_revision','request_id'})
        request(args.get('request_id'));number(args.get('expected_revision'),1,1000000,'Project revision',True)
        ep,meta=project(sp,args.get('project'));row,path=input_source(sp,args.get('source_id'))
        if len(meta['project_sources'])>=40 and args['source_id'] not in meta['project_sources']: raise ValueError('Project supports up to 40 sources')
        return row,None
    production.only(args,{'project','version','sha256','preset','request_id'})
    request(args.get('request_id'));number(args.get('version'),1,1000000,'Exact edit version',True)
    if not isinstance(args.get('sha256'),str) or not SHA.fullmatch(args['sha256']): raise ValueError('Exact EditDoc SHA-256 required')
    ep,meta,path,doc,sha=exact(sp,args.get('project'),args['version'],args['sha256'])
    if args.get('preset') not in ('master','audio_only','podcast_mp3','wav'): raise ValueError('Choose MP4, AAC, MP3, or WAV')
    if editdoc.effective_mode(doc)=='audio' and args['preset']=='master': raise ValueError('Audio edit has no picture; choose an audio export.')
    if not doc['duration']: raise ValueError('Add a source range to the programme before rendering')
    return doc,None


def compiled_waveform(sp,original,directory,probe,*,progress=None):
    """Use the exact compiled master only when its browser format is supported."""
    info=media._ffprobe(original);formats=set(info.get('format',{}).get('format_name','').split(','))
    videos=[s for s in info.get('streams',[]) if s.get('codec_type')=='video']
    audios=[s for s in info.get('streams',[]) if s.get('codec_type')=='audio']
    if videos:
        compatible='mp4' in formats and all(s.get('codec_name')=='h264' and s.get('pix_fmt')=='yuv420p' for s in videos) and all(s.get('codec_name')=='aac' for s in audios)
    else:
        compatible=bool(audios) and (('mp4' in formats and all(s.get('codec_name')=='aac' for s in audios)) or ('wav' in formats and all(s.get('codec_name') in ('pcm_s16le','pcm_s24le','pcm_f32le') for s in audios)))
    if not compatible:raise ValueError('Compiled output is not browser-compatible H.264/AAC or PCM audio; a supported preview is required')
    return waveform(sp,original,directory,probe,progress=progress)


def waveform(sp,original,directory,probe,*,progress=None):
    """Decode bounded PCM bins without encoding an unused video rendition."""
    import numpy as np
    directory=safe_dir(sp,directory);directory.mkdir(parents=True,exist_ok=True)
    destination=directory/'waveform.json'
    # Bin actual PCM maxima; bounded memory even for hour-long recordings.
    peaks=[];count=0
    if probe['audio']:
        bins=1200;hop=max(1,math.ceil(probe['duration']*8000/bins))
        proc=subprocess.Popen([binary('ffmpeg'),'-nostdin','-v','error','-i',str(original),'-vn','-ac','1','-ar','8000','-f','f32le','-'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        try:
            while chunk:=proc.stdout.read(hop*4):
                data=np.frombuffer(chunk,dtype='<f4'); peaks.append(round(float(np.max(np.abs(data))),5));count+=len(data)
                if progress and len(peaks)%20==0:progress('waveform',count/8000,probe['duration'])
            if proc.wait(): raise ValueError('Waveform decoder failed')
        finally:
            if proc.poll() is None: proc.kill();proc.wait()
    write(sp,destination,{'duration':probe['duration'],'sample_rate':8000,'decoded_samples':count,'peaks':peaks,'source_sha256':sha256_file(original)})
    return destination


def proxy(sp,original,directory,probe,*,progress=None):
    """Every video gets a browser H.264/AAC rendition; original render input is retained."""
    directory=safe_dir(sp,directory);directory.mkdir(parents=True,exist_ok=True)
    out=directory/('preview.mp4' if probe['video'] else 'preview.m4a');receipt=directory/'preview.json'
    if out.exists() and receipt.exists() and read(sp,receipt).get('sha256')==sha256_file(out): return out,directory/'waveform.json'
    partial=out.with_name('preview.partial'+out.suffix)
    cmd=[binary('ffmpeg'),'-nostdin','-v','error','-y','-i',str(original)]
    if probe['video']: cmd+=['-map','0:v:0','-map','0:a:0?','-vf','scale=640:360:force_original_aspect_ratio=decrease:force_divisible_by=2','-c:v','libx264','-preset','ultrafast','-crf','28','-pix_fmt','yuv420p']
    else: cmd+=['-vn']
    cmd+=['-c:a','aac','-b:a','96k','-movflags','+faststart',str(partial)]
    if progress is None:subprocess.run(cmd,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=14400)
    else:
        import time,tempfile
        # Real encoded output time, not a heartbeat pretending to be progress.
        with tempfile.TemporaryFile() as errors:
            proc=subprocess.Popen(cmd[:-1]+['-progress','pipe:1','-nostats',cmd[-1]],stdout=subprocess.PIPE,stderr=errors,text=True)
            started=time.monotonic();last=0.
            try:
                for line in proc.stdout:
                    if time.monotonic()-started>14400:raise TimeoutError('Browser preview exceeded its four-hour budget')
                    key,_,value=line.strip().partition('=')
                    if key=='out_time_us' and value.isdigit() and time.monotonic()-last>=1:
                        progress('preview',min(probe['duration'],int(value)/1000000),probe['duration']);last=time.monotonic()
                if proc.wait():
                    errors.seek(0);raise ValueError('Browser preview failed: '+errors.read()[-1200:].decode(errors='replace'))
            finally:
                if proc.poll() is None:proc.kill();proc.wait()
        progress('waveform',0,probe['duration'])
    waveform(sp,original,directory,probe,progress=progress)
    partial.replace(out)
    write(sp,receipt,{'sha256':sha256_file(out),'duration':probe['duration'],'source_sha256':sha256_file(original),'format':'H.264/AAC' if probe['video'] else 'AAC'})
    return out,directory/'waveform.json'


def add_source(studio,args):
    sp=studio.sp;ep,meta=project(sp,args['project']);row,original=input_source(sp,args['source_id']);key=row['source_id']
    if key in meta['project_sources']:
        old=meta['project_sources'][key]
        if old['sha256']!=row['sha256']: raise ValueError('Source identity changed')
        return load(sp,args['project'])
    probe=media.probe_file(original,sp.root)
    number(probe['duration'],.001,8*3600,'Source duration')
    if not (probe['video'] or probe['audio']): raise ValueError('No playable streams')
    directory=ep.work/'sources'/key;copy_path=directory/('original'+original.suffix.lower());_copy_immutable(sp,original,copy_path,row['sha256'])
    parent=row.get('parent')
    if not parent:
        parent=library.add_file(sp,original,source={'type':'import','ref':key},license_entry={'source':'registered upload','url':'urn:ave:source:'+key,'license':'unverified','license_url':'urn:ave:license:unverified','license_text':'No ownership or usage rights established. Human license review and attestation required.'},by='workbench',attested=False,origin={'workbench_source':key})['id']
    preview,waveform=proxy(sp,copy_path,directory,probe)
    transcript=None;channel_map=[]
    if not key.startswith('a_'):
        source_ep=episode_for(sp,row);tp=source_ep.artifact('transcript.json')
        mp=source_ep.artifact('media.json')
        if mp.exists():
            media_info=read(sp,mp)
            media_rows=media_info.get('files',[])
            channel_map=next((x.get('channel_map',[]) for x in media_rows if Path(x['file']).name==original.name),[])
        if tp.exists():
            t_sha=sha256_file(tp);target=directory/('transcript-'+t_sha+'.json');_copy_immutable(sp,tp,target,t_sha)
            tr=read(sp,target);transcript={'file':ep.rel(target),'sha256':t_sha,'engine':tr.get('engine'),'clock':'source'}
    with editdoc.mutation_lock(ep):
        ep,meta=project(sp,args['project'])
        if key in meta['project_sources']: return load(sp,args['project'])
        if meta['project_rev']!=args['expected_revision']: raise editdoc.EditConflict('Project source list changed; completed preview is preserved. Reopen and add this source again.')
        origin=max((s['start_offset']+s['duration']+1 for s in meta['project_sources'].values()),default=0)
        video=probe['video']
        meta['project_sources'][key]={'source_id':key,'label':row['label'],'sha256':row['sha256'],'parent':parent,
            'file':ep.rel(copy_path),'kind':'video' if video else 'audio','duration':probe['duration'],'has_audio':bool(probe['audio']),
            'w':video.get('width',0) if isinstance(video,dict) else 0,'h':video.get('height',0) if isinstance(video,dict) else 0,
            'start_offset':origin,'drift_ppm':0,'channel_map':channel_map,'transcript':transcript,'proxy':ep.rel(preview),'waveform':ep.rel(waveform)}
        meta['project_rev']+=1;write(sp,ep.meta_path,meta)
    return load(sp,args['project'])


def source_words(src, words):
    """Project a pinned snapshot into file time, retaining the original clock and bounds."""
    snapshot = src['transcript']
    episode_clock = snapshot.get('clock') == 'episode'
    projected = []
    for index, word in enumerate(words):
        a, b = word['start'], word['end']
        lo, hi = (editdoc.src_time(src, a), editdoc.src_time(src, b)) if episode_clock else (a, b)
        if hi <= 0 or lo >= src['duration']:
            continue
        projected.append({**word, 'start': max(0, lo), 'end': min(src['duration'], hi),
                          'transcript_start': a, 'transcript_end': b,
                          'transcript_clock': snapshot.get('clock', 'source'),
                          'source_id': src['source_id'], 'transcript_sha256': snapshot['sha256'],
                          'word_id': f"{src['sha256']}:{snapshot['sha256']}:{index}"})
    return projected


def transcript_page(sp,pid,key,page=0,programme=False,version=None):
    number(page,0,100000,'Transcript page',True)
    ep,meta,path,doc,sha=exact(sp,pid,version)
    if programme:
        from ..occurrences import mapped_words
        words=mapped_words(ep,doc)
    else:
        src=doc['sources'].get(key) or meta['project_sources'].get(key)
        if src is None: raise ValueError('Choose a project source')
        snapshot=src.get('transcript')
        if not snapshot: return {'words':[],'pages':0,'total':0,'note':'No transcript pinned to this revision. Transcribe this recording, then adopt its words below.'}
        path=safe(sp,ep.root/snapshot['file'])
        if sha256_file(path)!=snapshot['sha256']: raise ValueError('Pinned transcript changed')
        words=source_words(src, read(sp,path).get('words',[]))
    return {'words':words[page*100:(page+1)*100],'pages':math.ceil(len(words)/100),'total':len(words),'page':page}


def clip_bounds(doc, clip):
    source = doc['sources'][clip['source']]
    start = editdoc.episode_time(source, clip['in'])
    end = editdoc.episode_time(source, clip['out'])
    return source, start, end


def build_picture(clip, base, source):
    if source['kind'] != 'video':
        raise ValueError('Video programmes require picture for every range; choose an audio project for audio-only sources')
    return {**base, 'id': 'v_' + clip['id'],
            **{key: copy.deepcopy(clip[key]) for key in ('crop', 'zoom') if key in clip}}


def build_dialogue(doc, clip, base, source, start, end):
    if clip.get('dialogue_source'):
        ds = doc['sources'][clip['dialogue_source']]
        delta = start - editdoc.episode_time(source, clip['link_source_in'])
        base = {**base, 'src': clip['dialogue_source'],
                'e0': editdoc.episode_time(ds, clip['dialogue_in']) + delta}
    ds = doc['sources'][base['src']]
    sound = {**base, 'id': clip.get('dialogue_id', 'd_' + clip['id']),
             'sound_group': clip.get('sound_group', clip['occurrence_id']),
             **{key: clip.get(key, default) for key, default in
                [('xfade_ms', 0), ('gain_db', 0), ('channel', 'original'), ('fade_in_s', 0), ('fade_out_s', 0)]}}
    sound['fade_in_offset_s'] = start - editdoc.episode_time(source, clip.get('sound_envelope_in', clip['in']))
    sound['fade_out_remaining_s'] = editdoc.episode_time(source, clip.get('sound_envelope_out', clip['out'])) - end
    if editdoc.effective_mode(doc) == 'silent_video' or clip.get('muted') or not ds['has_audio']:
        sound['fill'] = 'silence'
    elif clip.get('fill'):
        sound['fill'] = clip['fill']
    if not sound.get('fill'):
        lo = editdoc.src_time(ds, sound['e0'])
        hi = editdoc.src_time(ds, sound['e0'] + end - start)
        if lo < -1e-7 or hi > ds['duration'] + 1e-7:
            raise ValueError('Linked dialogue would leave its source coverage; choose a range covered by both recordings')
    return sound


def append_dialogue(segments, sound):
    """Undo only artificial picture/clock splits, retaining original seam widths and envelopes."""
    previous = segments[-1] if segments else None
    controls = ('src', 'occurrence_id', 'sound_group', 'gain_db', 'channel', 'fill', 'fade_in_s', 'fade_out_s')
    continuous = previous and all(previous.get(k) == sound.get(k) for k in controls)
    if continuous:
        continuous = all(abs(a - b) < 1e-8 for a, b in (
            (previous['o1'], sound['o0']),
            (previous['e0'] + previous['o1'] - previous['o0'], sound['e0']),
            (previous['o0'] - previous['fade_in_offset_s'], sound['o0'] - sound['fade_in_offset_s']),
            (previous['o1'] + previous['fade_out_remaining_s'], sound['o1'] + sound['fade_out_remaining_s'])))
    if continuous and not sound['xfade_ms']:
        previous['o1'] = sound['o1']
        previous['fade_out_remaining_s'] = sound['fade_out_remaining_s']
    else:
        # A moved/split piece must still have a unique segment ID when it cannot coalesce.
        if any(s['id'] == sound['id'] for s in segments):
            sound['id'] += '_' + sound['segment_id']
        segments.append(sound)


def _tracks(doc):
    video, dialogue, t = [], [], 0
    for clip in doc['clips']:
        source, start, end = clip_bounds(doc, clip)
        base = {'o0': t, 'o1': t + end - start, 'e0': start, 'src': clip['source'],
                'occurrence_id': clip['occurrence_id'], 'segment_id': clip['id']}
        if editdoc.effective_mode(doc) != 'audio':
            video.append(build_picture(clip, base, source))
        append_dialogue(dialogue, build_dialogue(doc, clip, base, source, start, end))
        t += end - start
    doc['duration'] = t
    doc['tracks']['video'], doc['tracks']['dialogue'] = video, dialogue
    doc['tracks']['music'] = [{**m, 'o1': min(m['o1'], t)} for m in doc['tracks']['music'] if m['o0'] < t]
    doc['captions'] = {'srt': None, 'ass': None, 'burn': False}


def recut_output(doc, cuts):
    """Remove output-clock intervals from a 7b programme without losing tracks.

    The caller supplies already-reviewed/analysed intervals and saves a new
    version. Original source clocks, occurrence IDs, envelope origins and the
    pinned caption snapshot remain intact. Unsupported effects fail before a
    document is returned.
    """
    if not doc.get('independent_project') and not doc.get('combined_constituents'):raise ValueError('Output recut requires a shared Media or combined episode programme')
    if doc.get('independent_project') and not isinstance(doc.get('clips'),list):raise ValueError('Shared Media programme lacks clips')
    if any(o.get('kind') not in ('broll','credit','motion','image') for o in doc['tracks'].get('overlays',[])):raise ValueError('Unsupported overlay effect; preserve the existing edit')
    duration=doc['duration'];ordered=sorted(cuts,key=lambda c:c['o0']);last=0.;keeps=[]
    for cut in ordered:
        production.only(cut,{'o0','o1','xfade_ms','id'})
        a=number(cut.get('o0'),0,duration,'Cut start');b=number(cut.get('o1'),0,duration,'Cut end')
        if b<=a or a<last-1e-8:raise ValueError('Cleanup cuts must be non-overlapping output intervals')
        number(cut.get('xfade_ms',0),0,500,'Seam crossfade')
        if a>last+editdoc.EPS:keeps.append((last,a))
        last=b
    if last<duration-editdoc.EPS:keeps.append((last,duration))
    if not keeps:raise ValueError('Cleanup cannot remove the entire programme')
    out=copy.deepcopy(doc)
    if not doc.get('independent_project'):
        if doc['tracks'].get('music') or doc['tracks'].get('overlays') or doc['tracks'].get('room_tone'):raise ValueError('Combined cleanup cannot discard dependent tracks')
        offset=0.;tracks={'video':[],'dialogue':[]}
        for n,(a,b) in enumerate(keeps):
            for kind in tracks:
                for seg in doc['tracks'][kind]:
                    lo=max(a,seg['o0']);hi=min(b,seg['o1'])
                    if hi-lo<=editdoc.EPS:continue
                    part=copy.deepcopy(seg);part['id']=f'{seg["id"]}_cut{n+1}';part['e0']=seg['e0']+lo-seg['o0'];part['o0']=offset+lo-a;part['o1']=offset+hi-a
                    if kind=='dialogue' and lo>seg['o0']+editdoc.EPS:part['xfade_ms']=next((x.get('xfade_ms',0) for x in ordered if abs(x['o1']-lo)<1e-7),0)
                    tracks[kind].append(part)
            offset+=b-a
        out['tracks'].update(tracks);out['duration']=offset;out['captions']={'burn':False}
        problems=editdoc.validate(out)
        if problems:raise ValueError('; '.join(problems))
        return out
    new_clips=[];position=0.;piece=0
    for clip in doc['clips']:
        source,start,end=clip_bounds(doc,clip);length=end-start
        for a,b in keeps:
            lo=max(a,position);hi=min(b,position+length)
            if hi-lo<=editdoc.EPS:continue
            c=copy.deepcopy(clip);piece+=1;c['id']=f"{clip['id']}_cut{piece}"
            c['in']=editdoc.src_time(source,start+lo-position);c['out']=editdoc.src_time(source,start+hi-position)
            c.setdefault('sound_envelope_in',clip.get('in'));c.setdefault('sound_envelope_out',clip.get('out'))
            seam=next((k for k in ordered if abs(k['o1']-lo)<1e-7),None)
            if seam:c['xfade_ms']=seam.get('xfade_ms',0)
            elif lo>position+1e-8:c['xfade_ms']=0
            new_clips.append(c)
        position+=length
    if abs(position-duration)>1e-5:raise ValueError('Programme clips do not cover their declared duration')
    out['clips']=new_clips;out['tracks']['music']=[];out['tracks']['overlays']=[];_tracks(out)
    intervals=[];t=0.
    for a,b in keeps:intervals.append((a,b,t));t+=b-a
    return map_dependent_tracks(doc,out,intervals)


def map_dependent_tracks(doc,out,intervals):
    """Retain supported dependent tracks through one explicit output interval map.

    Each (old_start, old_end, new_start) preserves duration and source clock.
    An occurrence anchor follows those slices; a programme anchor stays at its
    authored output seconds. Linked credits travel with the relevant picture.
    """
    if any(o.get('kind') not in ('broll','credit','motion','image') for o in doc['tracks'].get('overlays',[])):
        raise ValueError('Unsupported overlay effect; preserve the existing edit')
    def pieces(segment,track):
        result=[]
        if segment.get('anchor', 'occurrence') == 'programme':
            if segment['o0'] >= out['duration']:return result
            part=copy.deepcopy(segment);part['o1']=min(part['o1'],out['duration'])
            return [(0,part)]                             # a programme bed fades at the programme's new end
        if segment.get('anchor','occurrence') != 'occurrence':raise ValueError('Unknown dependent track anchor')
        for k,(a,b,offset) in enumerate(intervals):
            lo=max(a,segment['o0']);hi=min(b,segment['o1'])
            if hi-lo<=editdoc.EPS:continue
            part=copy.deepcopy(segment);part['id']=f"{segment['id']}_cut{k+1}";part['o0']=offset+lo-a;part['o1']=offset+hi-a
            front=lo-segment['o0'];tail=segment['o1']-hi
            if track=='music':part['in']=segment.get('in',0)+front
            if track=='music' or (segment.get('kind')=='broll' and segment.get('audio')=='keep'):
                part['fade_in_offset_s']=segment.get('fade_in_offset_s',0)+front;part['fade_out_remaining_s']=segment.get('fade_out_remaining_s',0)+tail
            if track=='overlay':
                if segment.get('kind')=='broll' and not segment['file'].lower().endswith(tuple(('.jpg','.jpeg','.png','.webp','.gif','.tif','.tiff'))):part['src_in']=segment.get('src_in',0)+front
                if segment.get('kind') in ('motion','broll'):
                    part['animation_offset_s']=segment.get('animation_offset_s',0)+front
                    part['animation_duration_s']=segment.get('animation_duration_s',segment['o1']-segment['o0'])
            result.append((k,part))
        return result
    out['tracks']['music']=[p for m in doc['tracks'].get('music',[]) for _,p in pieces(m,'music')]
    source_overlays=doc['tracks'].get('overlays',[]);mapped={};new_overlays=[]
    for original in source_overlays:
        if original.get('kind')=='credit':continue
        for k,part in pieces(original,'overlay'):
            mapped[(original['id'],k)]=part['id'];new_overlays.append(part)
    for original in source_overlays:
        if original.get('kind')!='credit':continue
        parent=original.get('credit_for')
        for k,part in pieces(original,'overlay'):
            target=mapped.get((parent,k))
            if target is None:
                candidates=[(j,ident) for (oid,j),ident in mapped.items() if oid==parent]
                if not candidates:continue  # the associated picture was removed
                target=min(candidates,key=lambda x:abs(x[0]-k))[1]
            part['credit_for']=target;new_overlays.append(part)
    out['tracks']['overlays']=new_overlays
    # Each retained B-roll piece points to a corresponding retained credit.
    for broll in (x for x in new_overlays if x.get('kind')=='broll' and x.get('credit_id')):
        linked=next((x for x in new_overlays if x.get('kind')=='credit' and x.get('credit_for')==broll['id']),None)
        if linked is None:
            original_id=next(oid for (oid,_),part_id in mapped.items() if part_id==broll['id']);original_broll=next(x for x in source_overlays if x.get('kind')=='broll' and x['id']==original_id)
            original_credit=next((x for x in source_overlays if x.get('id')==original_broll['credit_id'] and x.get('credit_for')==original_broll['id']),None)
            if original_credit is None:raise ValueError('Required B-roll credit is missing')
            linked=copy.deepcopy(original_credit);linked['id']=broll['id']+'_credit';linked['credit_for']=broll['id'];linked['o0']=broll['o0'];linked['o1']=min(broll['o1'],broll['o0']+original_credit['o1']-original_credit['o0']);new_overlays.append(linked)
        broll['credit_id']=linked['id']
    problems=editdoc.validate(out)
    if problems:raise ValueError('; '.join(problems))
    return out


def retime_dependent_tracks(before,after):
    """Map edited occurrences to their original output ranges, never array-merge."""
    old=[];t=0.
    for c in before['clips']:
        _,a,b=clip_bounds(before,c);old.append((c,a,b,t));t+=b-a
    intervals=[];t=0.
    for c in after['clips']:
        _,a,b=clip_bounds(after,c)
        candidates=[(p,x,y,o) for p,x,y,o in old if p['occurrence_id']==c['occurrence_id'] and p['source']==c['source'] and min(b,y)>max(a,x)+editdoc.EPS]
        covered=0.
        for p,x,y,o in candidates:
            lo=max(a,x);hi=min(b,y);intervals.append((o+lo-x,o+hi-x,t+lo-a));covered+=hi-lo
        if candidates and covered < b-a-editdoc.EPS:
            raise ValueError('Extending a clip with dependent tracks is ambiguous; trim within its original range or remove those placements explicitly')
        t+=b-a
    return map_dependent_tracks(before,after,intervals)


def validate_clip_operation(operation, selection):
    if selection is None:
        raise ValueError('Choose a programme clip')
    fields = {'trim': {'in', 'out'}, 'split': {'at'}, 'remove': set(), 'move': {'to'}, 'join': set(),
              'sound': {'gain_db', 'fade_in_s', 'fade_out_s', 'muted', 'channel'}, 'reframe': {'x', 'y', 'w', 'h'}}
    production.only(operation, {'kind', 'clip'} | fields[operation['kind']])


def mutate(studio,body):
    production.only(body,{'project','expected_version','expected_sha256','expected_revision','request_id','operation'})
    rid=request(body.get('request_id'));number(body.get('expected_version'),1,1000000,'Edit version',True);number(body.get('expected_revision'),1,1000000,'Project revision',True)
    if not isinstance(body.get('expected_sha256'),str) or not SHA.fullmatch(body['expected_sha256']): raise ValueError('Captured EditDoc SHA-256 required')
    sp=studio.sp;ep,meta=project(sp,body.get('project'));op=body.get('operation')
    if not isinstance(op,dict): raise ValueError('Choose an editing operation')
    opname=op.get('kind');history={'request_id':rid,'operation_sha256':digest(body),'operation':copy.deepcopy(op),'reason':'Creator editing action; review remains outstanding','confidence':1.0}
    with editdoc.mutation_lock(ep):
        ep,meta,path,doc,sha=exact(sp,body['project'])
        # Recover after commit-before-response without rebasing or duplicating edits.
        for v in editdoc.list_versions(ep,'short'):
            old=read(sp,editdoc.path_for(ep,'short',v));found=next((h for h in old.get('history',[]) if h.get('request_id')==rid),None)
            if found:
                if found!=history: raise editdoc.EditConflict('Request ID belongs to different parameters')
                return load(sp,body['project'],v)
        if meta['project_rev']!=body['expected_revision']: raise editdoc.EditConflict('Project source list changed; reopen before editing')
        if int(editdoc._V.match(path.name)['n'])!=body['expected_version'] or sha!=body['expected_sha256']: raise editdoc.EditConflict('Programme changed; reopen before editing')
        prior=copy.deepcopy(doc)
        clips=doc['clips'];selection=next((c for c in clips if c['id']==op.get('clip')),None)
        if opname=='restore':
            production.only(op,{'kind','version','sha256'})
            restored=exact(sp,body['project'],op.get('version'),op.get('sha256'))[3]
            doc=copy.deepcopy(restored)
        elif opname=='insert':
            production.only(op,{'kind','source','in','out','at'})
            src=meta['project_sources'].get(op.get('source'))
            if not src: raise ValueError('Choose a project source')
            a=number(op.get('in'),0,src['duration'],'Source in');b=number(op.get('out'),0,src['duration'],'Source out')
            if b-a<1/24: raise ValueError('Choose at least one frame of source')
            if len(clips)>=200: raise ValueError('Programme supports up to 200 clips')
            at=number(op.get('at',len(clips)),0,len(clips),'Insert position',True)
            doc['sources'].setdefault(op['source'],copy.deepcopy(src))
            ident=digest({'request':rid,'clip':True})[:20]
            clips.insert(at,{'id':ident,'occurrence_id':ident,'source':op['source'],'in':a,'out':b})
        elif opname in ('trim','split','remove','move','join','sound','reframe'):
            validate_clip_operation(op, selection)
            idx=clips.index(selection);src=doc['sources'][selection['source']]
            if opname=='trim':
                a=number(op.get('in'),0,src['duration'],'Source in');b=number(op.get('out'),0,src['duration'],'Source out')
                if b-a<1/24: raise ValueError('Choose at least one frame')
                selection.update({'in':a,'out':b,'sound_envelope_in':a,'sound_envelope_out':b})
            elif opname=='split':
                t=number(op.get('at'),selection['in']+1/24,selection['out']-1/24,'Split in source seconds')
                child=copy.deepcopy(selection);child.update(id=digest({'request':rid})[:20],parent_segment=selection['id']);child['in']=t;child['xfade_ms']=0;selection['out']=t;clips.insert(idx+1,child)
            elif opname=='remove': clips.pop(idx)
            elif opname=='move':
                to=number(op.get('to'),0,len(clips)-1,'Clip position',True);clips.insert(to,clips.pop(idx))
            elif opname=='join':
                if idx+1>=len(clips): raise ValueError('Choose a clip with a following range')
                nxt=clips[idx+1]
                props=('source','occurrence_id','gain_db','channel','muted','crop','fade_in_s','fade_out_s','dialogue_source','dialogue_in','link_source_in','fill','zoom','sound_group','sound_envelope_in','sound_envelope_out')
                if nxt.get('xfade_ms',0) or any(selection.get(k)!=nxt.get(k) for k in props) or abs(selection['out']-nxt['in'])>1e-8: raise ValueError('Join supports adjacent split ranges from the same occurrence with matching controls')
                selection['out']=nxt['out'];clips.pop(idx+1)
            elif opname=='sound':
                selection.update(sound_envelope_in=selection['in'],sound_envelope_out=selection['out'])
                for k,lo,hi in [('gain_db',-60,12),('fade_in_s',0,30),('fade_out_s',0,30)]:
                    if k in op: selection[k]=number(op[k],lo,min(hi,selection['out']-selection['in']) if 'fade' in k else hi,k)
                if 'channel' in op:
                    if op['channel'] not in ('original','left','right','mono'): raise ValueError('Unknown audio channel choice')
                    selection['channel']=op['channel']
                if 'muted' in op:
                    if type(op['muted']) is not bool: raise ValueError('Mute must be boolean')
                    selection['muted']=op['muted']
            else:
                if editdoc.effective_mode(doc)=='audio': raise ValueError('Audio has no picture to reframe')
                x=number(op.get('x'),0,src['w'],'Crop x');y=number(op.get('y'),0,src['h'],'Crop y');w=number(op.get('w'),2,src['w'],'Crop width');h=number(op.get('h'),2,src['h'],'Crop height')
                if x+w>src['w'] or y+h>src['h']: raise ValueError('Crop exceeds source picture')
                selection['crop']=[int(x)//2*2,int(y)//2*2,int(w)//2*2,int(h)//2*2]
                selection['zoom']=1
        elif opname=='format':
            production.only(op,{'kind','aspect','mode','resolution'})
            if op.get('resolution','working') not in ('working','hd'): raise ValueError('Choose working preview or full HD output')
            if op.get('aspect') not in ('16:9','9:16','1:1'): raise ValueError('Choose landscape, portrait or square')
            if op.get('mode') not in ('audiovisual','audio','silent_video'): raise ValueError('Unknown media mode')
            doc['media_mode']={'version':1,'kind':op['mode']};w,h={'16:9':(640,360),'9:16':(360,640),'1:1':(640,640)}[op['aspect']];doc['canvas']={'w':w*(3 if op.get('resolution')=='hd' else 1),'h':h*(3 if op.get('resolution')=='hd' else 1)}
            if op['mode']=='silent_video': doc['tracks']['music']=[]
            if op['mode']=='audio': doc['caption_edit']['burn']=False
        elif opname=='transcript':
            production.only(op,{'kind','source'})
            key=op.get('source');src=meta['project_sources'].get(key)
            if not src: raise ValueError('Choose a project recording')
            sid='recording_'+src['sha256'];registered=recording(sp,sid)
            if not registered: raise ValueError('Import this library recording into Sources, then transcribe locally')
            tep=episode_for(sp,registered);tp=safe(sp,tep.artifact('transcript.json'))
            if not tp.exists(): raise ValueError('Transcribe the recording first, then adopt its transcript')
            t_sha=sha256_file(tp);dest=ep.work/'sources'/key/('transcript-'+t_sha+'.json');_copy_immutable(sp,tp,dest,t_sha)
            tr=read(sp,dest);snap={'file':ep.rel(dest),'sha256':t_sha,'engine':tr.get('engine'),'clock':'source'}
            doc['sources'].setdefault(key,copy.deepcopy(src))['transcript']=snap
        elif opname=='captions':
            production.only(op,{'kind','style','burn','correction'})
            if 'style' in op:
                if op['style'] not in ('podcast','short'): raise ValueError('Choose podcast or shorts caption style')
                captions.get_style(op['style']);doc['caption_edit']['style']=op['style']
            if 'burn' in op:
                if type(op['burn']) is not bool or (op['burn'] and editdoc.effective_mode(doc)=='audio'): raise ValueError('Caption burn requires picture; audio supports sidecars')
                doc['caption_edit']['burn']=op['burn']
            if op.get('correction') is not None:
                c=op['correction'];production.only(c,{'occurrence_id','transcript_sha256','word_ids','text','start_offset_s','end_offset_s'})
                production.text(c.get('text'),'Caption correction',300,True)
                timing_limit=captions.timing_adjustment_limit(ep)
                for key in ('start_offset_s','end_offset_s'):
                    if key in c:number(c[key],-timing_limit,timing_limit,'Caption timing adjustment')
                if not isinstance(c.get('word_ids'),list) or not 1<=len(c['word_ids'])<=30 or len(set(c['word_ids']))!=len(c['word_ids']): raise ValueError('Choose 1–30 occurrence words')
                from ..occurrences import mapped_words, correction_words
                original=copy.deepcopy(doc);original['caption_edit']['corrections']=[]
                uncorrected=mapped_words(ep,original)
                correction_words(uncorrected,c)
                available={w['word_id'] for w in uncorrected if w['occurrence_id']==c.get('occurrence_id') and w['transcript_sha256']==c.get('transcript_sha256')}
                if not set(c['word_ids'])<=available: raise ValueError('Correction refers to stale or different source words')
                corrections=doc['caption_edit']['corrections'];corrections[:]=[x for x in corrections if not (x['occurrence_id']==c['occurrence_id'] and set(x['word_ids'])&set(c['word_ids']))];corrections.append(copy.deepcopy(c))
        elif opname=='music':
            production.only(op,{'kind','asset','gain_db','fade_in_s','fade_out_s','duck_db','in'})
            if op.get('asset') is None: doc['tracks']['music']=[]
            else:
                if editdoc.effective_mode(doc)=='silent_video': raise ValueError('Switch to video with sound before adding music')
                rec,original=input_source(sp,op['asset']);coverage=library.coverage(sp,[op['asset']])
                if not coverage['ok']: raise ValueError('Music needs a licensed, human-attested library asset: '+str(coverage['missing']))
                probe=media.probe_file(original,sp.root);a=number(op.get('in',0),0,probe['duration'],'Music source in')
                if not probe['audio'] or a+doc['duration']>probe['duration']: raise ValueError('Music must contain audio covering the programme')
                path=ep.work/'music'/(rec['sha256']+original.suffix);_copy_immutable(sp,original,path,rec['sha256'])
                music={'id':'music','anchor':'programme','file':ep.rel(path),'asset':op['asset'],'sha256':rec['sha256'],'o0':0,'o1':doc['duration'],'in':a}
                for k,lo,hi,default in [('gain_db',-60,0,-20),('fade_in_s',0,30,0),('fade_out_s',0,30,0),('duck_db',-40,0,-12)]: music[k]=number(op.get(k,default),lo,hi,k)
                doc['tracks']['music']=[music]
        else: raise ValueError('Unknown bounded timeline operation')
        _tracks(doc)
        if opname in ('insert','trim','split','remove','move','join') and (prior['tracks'].get('music') or prior['tracks'].get('overlays')):
            doc=retime_dependent_tracks(prior,doc)
        if doc['duration']>3600: raise ValueError('Short-form programmes support up to one hour; use Episode for full production')
        if doc.get('caption_edit', {}).get('corrections'):
            from ..occurrences import mapped_words
            mapped_words(ep, doc)  # reject disconnected corrections before publishing a revision
        caption_snapshot(sp,ep,doc)
        doc.setdefault('history',[]).append(history)
        if len(json.dumps(doc,ensure_ascii=False).encode())>desk.MAX_STATE_FILE: raise ValueError('Programme history exceeds the bounded project size; create a new programme from its registered output')
        out=editdoc.save_new_version(ep,doc,expected_version=body['expected_version'],expected_sha256=body['expected_sha256'],request_id=rid,locked=True)
    return load(sp,body['project'],int(editdoc._V.match(out.name)['n']))


def verified_programme_parents(sp,ep,doc):
    """Verify exact source and licensed dependent media before compiling or registering."""
    from ..broll import place
    parents=[]
    used={seg['src'] for name in ('video','dialogue') for seg in doc['tracks'][name]}
    if doc['tracks'].get('room_tone'): used.add(doc['tracks']['room_tone']['src'])
    for key in used:
        src=doc['sources'][key]
        original=safe(sp,ep.root/src['file'])
        if sha256_file(original)!=src['sha256']: raise ValueError('Project source changed; render stopped')
        rec=library.get(sp,src['parent'])
        if rec['sha256']!=src['sha256']: raise ValueError('Parent source identity differs')
        parents.append(rec['id'])
    for m in doc['tracks']['music']:
        if sha256_file(safe(sp,ep.root/m['file']))!=m['sha256']: raise ValueError('Music identity changed')
        if not library.coverage(sp,[m['asset']])['ok']: raise ValueError('Music license/attestation is no longer valid')
        parents.append(m['asset'])
    closure=place.closure(ep,doc)
    if not closure['ok']:raise ValueError('B-roll license or bytes changed: '+str(closure['missing']+closure['problems']))
    parents.extend(closure['assets'])
    for overlay in doc['tracks'].get('overlays',[]):
        path=safe(sp,ep.root/overlay['file'])
        if overlay.get('sha256') and sha256_file(path)!=overlay['sha256']:raise ValueError('Overlay bytes changed')
    return list(dict.fromkeys(parents))


def render(studio,args):
    from .. import compile as compiler
    sp=studio.sp;ep,meta,path,doc,sha=exact(sp,args['project'],args['version'],args['sha256'])
    from . import episode_worker_bridge as bridge
    native=bool(doc.get('kept_clip',{}).get('native'))
    if native:
        if bridge.CLIENT is None:raise ValueError('Native kept render requires its inherited parent verifier')
        deps=bridge.CLIENT.call('video-render-inputs')
        if deps['doc']!=doc or deps['identity']!=args:raise ValueError('Native kept render differs from captured programme')
        parents=[]
    else:parents=verified_programme_parents(sp,ep,doc)
    identity={'schema':'video_render.v1','project':args['project'],'stage':'short','version':args['version'],'edit_sha256':sha,'caption_edit_sha256':digest(doc['caption_edit']),'preset':args['preset'],'mode':editdoc.effective_mode(doc),'request_id':args['request_id'],'renderer':RENDERER}
    snapshot=pinned_caption(sp,ep,doc);identity['caption_snapshot_sha256']=doc['caption_snapshot']['sha256'];identity['font']=snapshot['font']
    legacy_identity = {**identity, 'renderer': 'ave.compile.v1'}
    legacy = safe_dir(sp, ep.exports / digest(legacy_identity))
    if (legacy / 'result.json').exists():
        committed = read(sp, legacy / 'render.json')
        if committed['identity'] != legacy_identity or any(sha256_file(safe(sp, legacy / name)) != value for name, value in committed['hashes'].items()):
            raise ValueError('Historical completed render changed; existing result preserved')
        return read(sp, legacy / 'result.json')  # exact completed-request replay, never relabel old bytes
    final=safe_dir(sp,ep.exports/digest(identity));directory=final.with_name(final.name+'.building')
    from .workbench import _own_stage, _publish_staged
    _own_stage(sp,directory,directory.with_suffix('.owner.json'),{'identity':identity})
    if not final.exists():
        directory.mkdir(parents=True,exist_ok=True)
        # Only uncommitted, owned staging bytes can be regenerated after interruption.
        cue_list=captions.cues_from_edit(ep,doc,snapshot['style'])
        srt=directory/'captions.srt';ass=directory/'captions.ass'
        captions.write_srt(cue_list,srt);captions.write_ass(cue_list,snapshot['style'],tuple(doc['canvas'].values()),ass)
        compiled=copy.deepcopy(doc);compiled['captions']={'srt':ep.rel(srt),'ass':ep.rel(ass),'burn':doc['caption_edit']['burn']}
        if native:
            from .episode_release import directory as release_directory
            job={'id':bridge.CLIENT.jid};candidate_dir=release_directory(studio,job);candidate_dir.mkdir(parents=True,exist_ok=True)
            write(sp,candidate_dir/'edit-candidate.json',compiled)
        extension={'master':'.mp4','audio_only':'.m4a','podcast_mp3':'.mp3','wav':'.wav'}[args['preset']];output=directory/('programme'+extension)
        output.unlink(missing_ok=True)
        if args['preset']=='wav':
            compiler.compile_audio(ep,compiled,directory/'mix.wav')
            subprocess.run([binary('ffmpeg'),'-nostdin','-v','error','-y','-i',str(directory/'mix.wav'),'-map_metadata','-1','-c:a','pcm_s24le','-fflags','+bitexact',str(output)],check=True)
            (directory/'mix.wav').unlink()
            report={'duration':doc['duration'],'preset':'wav','warnings':[],'captions_burned':False}
        else: report=compiler.render(ep,compiled,output,preset=args['preset'],burn_captions=doc['caption_edit']['burn'])
        # Publish media, captions, numerical report and hashes as one immutable directory.
        write(sp,directory/'render.json',{'identity':identity,'report':report,'output':output.name,'hashes':{p.name:sha256_file(p) for p in (output,srt,ass)}})
        _publish_staged(directory,final)
    committed=read(sp,final/'render.json')
    if committed['identity']!=identity or any(sha256_file(safe(sp,final/name))!=sha for name,sha in committed['hashes'].items()): raise ValueError('Committed render identity changed; existing output preserved')
    output=final/committed['output'];srt=final/'captions.srt';ass=final/'captions.ass';report=committed['report'];receipt=final/'result.json'
    identity.update(srt_sha256=sha256_file(srt),ass_sha256=sha256_file(ass))
    if native:
        from .episode_release import directory as release_directory
        result={'project':args['project'],'version':args['version'],'edit_sha256':sha,'mode':editdoc.effective_mode(doc),'srt_url':url(sp,srt),'ass_url':url(sp,ass),'report':report,'note':'Exact kept revision; nothing attached automatically.'}
        write(sp,release_directory(studio,{'id':bridge.CLIENT.jid})/'render-candidate.json',{'identity':identity,'file':sp.rel(output),'result':result})
        return bridge.CLIENT.call('video-render-register')
    rec=library.add_derivative(sp,output,derived_from=parents,origin=identity,by='workbench')
    asset={'asset':rec['id'],'file':rec['file'],'sha256':rec['sha256'],'kind':rec['kind'],'url':url(sp,sp.root/rec['file']),'duration':doc['duration'],**doc['canvas']}
    result={'assets':[asset],'project':args['project'],'version':args['version'],'edit_sha256':sha,'mode':editdoc.effective_mode(doc),'srt_url':url(sp,srt),'ass_url':url(sp,ass),'report':report,'coverage':library.coverage(sp,[rec['id']]),'note':'Finished output. Review and licensing remain separate; nothing is attached automatically.'}
    write(sp,receipt,result)
    return result


def cleanup_dependencies(studio,args,*,committed=None):
    """Parent-only, exact programme and license closure for one cleanup job."""
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is not None:raise ValueError('Cleanup dependency verification requires the parent')
    sp=studio.sp;ep,meta,path,current,current_sha=exact(sp,args['project'],args['version'],args['sha256'])
    versions=editdoc.list_versions(ep,'short');head=versions[-1]
    if committed:
        if head!=committed['version'] or sha256_file(editdoc.path_for(ep,'short',head))!=committed['sha256']:raise ValueError('Published cleanup revision changed')
    elif head!=args['version']:raise ValueError('Programme revision changed during cleanup')
    base_version=args.get('base_version',args['version']);base_sha=args.get('base_sha256',args['sha256'])
    _,_,_,doc,sha=exact(sp,args['project'],base_version,base_sha);recut_output(doc,[])
    parents=verified_programme_parents(sp,ep,doc);pinned_caption(sp,ep,doc)
    from ..library import derive
    records=library.read_all(sp);order,unknown=derive.closure(records,parents)
    if unknown or len(order)>200:raise ValueError('Programme provenance closure is missing or too large')
    closure=[]
    for aid in order:
        rec=records[aid];path=safe(sp,sp.root/rec['file'])
        if sha256_file(path)!=rec['sha256']:raise ValueError('Programme provenance bytes changed')
        closure.append({'id':aid,'sha256':rec['sha256'],'derived_from':rec.get('derived_from',[])})
    from .episode_registry import policy
    return {'project':args['project'],'project_revision':meta['project_rev'],'current_sha256':current_sha,'base_sha256':sha,'parents':parents,'closure':closure,'caption_snapshot':copy.deepcopy(doc['caption_snapshot']),'cleanup_policy':sha256_file(__import__('ave.cleanup',fromlist=['DEFAULT_POLICY']).DEFAULT_POLICY),'choice':policy()['cleanup_choices'][args['policy']]}


def cleanup_freshness(studio,args,deps,*,committed=None):
    """Bounded heartbeat: verify ledger and stat identities without rehashing media."""
    from . import episode_registry as R
    from ..library import registry
    sp=studio.sp;R.control(sp)
    problems=registry.license_log(sp).verify()
    if problems:raise ValueError('License ledger verification failed')
    ep,meta,_,_,_=exact(sp,args['project'],args['version'],args['sha256'])
    head=editdoc.list_versions(ep,'short')[-1];expected=committed['version'] if committed else args['version']
    if head!=expected:raise ValueError('Programme revision changed during cleanup')
    if committed and sha256_file(editdoc.path_for(ep,'short',head))!=committed['sha256']:raise ValueError('Published cleanup revision changed')
    _,_,_,doc,_=exact(sp,args['project'],args.get('base_version',args['version']),args.get('base_sha256',args['sha256']))
    records=library.read_all(sp);paths=[ep.root/doc['caption_snapshot']['file']]
    for src in doc['sources'].values():
        paths.append(ep.root/src['file'])
        if src.get('transcript'):paths.append(ep.root/src['transcript']['file'])
    paths.extend(ep.root/m['file'] for m in doc['tracks'].get('music',[]));paths.extend(ep.root/o['file'] for o in doc['tracks'].get('overlays',[]))
    for item in deps['closure']:
        rec=records.get(item['id'])
        if not rec or rec['sha256']!=item['sha256'] or rec.get('derived_from',[])!=item['derived_from']:raise ValueError('Programme provenance changed')
        paths.append(sp.root/rec['file'])
    stats=[]
    for path in paths:
        p=safe(sp,path);st=p.stat();stats.append((str(p),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns))
    shas={item['sha256'] for item in deps['closure']};ids={item['id'] for item in deps['closure']}
    events=[r for r in R.control(sp).rows() if r.get('sha256') in shas or r.get('asset') in ids]
    entries=[registry.license_log(sp).find(sha256=sha) for sha in sorted(shas)]
    from .. import cleanup
    return {'project_revision':meta['project_rev'],'files':stats,'control':events,'license_entries':entries,'cleanup_policy':sha256_file(cleanup.DEFAULT_POLICY),'choice':R.policy()['cleanup_choices'][args['policy']]}


def publish_cleanup_candidate(studio,job,deps,*,committed=None,before_publish=None):
    """Parent publishes only this job's mechanically reconstructed candidate."""
    from . import episode_registry as R
    sp=studio.sp;args=job['args'];ep,meta,_,_,_=exact(sp,args['project'],args['version'],args['sha256'])
    base_version=args.get('base_version',args['version']);base_sha=args.get('base_sha256',args['sha256']);_,_,_,base,_=exact(sp,args['project'],base_version,base_sha)
    directory=safe_dir(sp,ep.work/'cleanup-jobs'/job['id']);candidate=read(sp,directory/'candidate.json');report=candidate.get('cleanup_report') or {}
    if report.get('policy')!=args['policy'] or report.get('restored')!=args['restore'] or report.get('source_version')!=base_version or report.get('source_sha256')!=base_sha:raise ValueError('Cleanup candidate differs from the captured choices')
    detected=report.get('detected_cuts',[]);cuts=report.get('cuts',[]);by_id={c['id']:c for c in detected}
    if len(by_id)!=len(detected) or {c['id'] for c in cuts}!={c['id'] for c in detected}-set(args['restore']):raise ValueError('Cleanup cut identities differ from the analysis')
    if any(any(c[k]!=by_id[c['id']][k] for k in ('e0','e1','xfade_ms')) for c in cuts):raise ValueError('Cleanup intervals differ from analysis')
    wavs=list(directory.glob('analysis*.wav'))
    if not any(sha256_file(safe(sp,p))==report.get('analysis_sha256') for p in wavs):raise ValueError('Cleanup analysis bytes changed')
    expected=recut_output(base,[{'id':c['id'],'o0':c['e0'],'o1':c['e1'],'xfade_ms':c['xfade_ms']} for c in cuts]);expected['stage']='short';expected['cleanup_report']=report
    expected.setdefault('history',[]).append({'request_id':args['request_id'],'operation':'occurrence-cleanup','source_sha256':base_sha,'reason':'Mechanical cleanup; editorial review remains outstanding','confidence':1.0})
    if candidate!=expected:raise ValueError('Cleanup candidate changes content outside the exact cuts')
    with editdoc.mutation_lock(ep):
        if cleanup_dependencies(studio,args,committed=committed)!=deps:raise ValueError('Cleanup dependencies changed before publication')
        if before_publish:before_publish()
        if committed:
            out=editdoc.path_for(ep,'short',committed['version'])
            if read(sp,out)!=candidate or sha256_file(out)!=committed['sha256']:raise ValueError('Previously published cleanup revision differs')
        else:out=editdoc.save_new_version(ep,candidate,expected_version=args['version'],expected_sha256=args['sha256'],request_id=args['request_id'],locked=True)
    return {'project':args['project'],'version':int(editdoc._V.match(out.name)['n']),'sha256':sha256_file(out),'report':report,'review':'not reviewed'}


def cleanup_programme(studio,job):
    """Create a mechanically analyzed Media revision; no review authority transfers."""
    from .. import cleanup
    from ..broll import place
    from . import episode_registry as R
    sp=studio.sp;args=job['args'];ep,meta,path,current,current_sha=exact(sp,args['project'],args['version'],args['sha256'])
    base_version=args.get('base_version',args['version']);base_sha=args.get('base_sha256',args['sha256'])
    _,_,_,doc,sha=exact(sp,args['project'],base_version,base_sha)
    from . import episode_worker_bridge as bridge
    def verify():
        return bridge.CLIENT.call('cleanup-inputs') if bridge.CLIENT is not None else cleanup_dependencies(studio,args)
    deps=verify()
    if bridge.CLIENT is not None:
        recovered=bridge.CLIENT.call('cleanup-status')
        if recovered:return recovered
    directory=safe_dir(sp,ep.work/'cleanup-jobs'/job['id']);directory.mkdir(parents=True,exist_ok=True)
    n=0;wav=directory/'analysis.wav'
    while wav.exists():
        n+=1;wav=directory/f'analysis-retry{n}.wav'
    pol=cleanup._deep_merge(cleanup.load_policy(),R.policy()['cleanup_choices'][args['policy']]);cleanup.validate_policy(pol)
    analyzed=cleanup.analyze_occurrences(ep,doc,wav,pol,restore_cuts=args['restore'])
    verify()
    revised=analyzed.pop('doc');revised['stage']='short';revised['cleanup_report']={**analyzed,'policy':args['policy'],'restored':args['restore'],'source_version':base_version,'source_sha256':sha}
    revised.setdefault('history',[]).append({'request_id':args['request_id'],'operation':'occurrence-cleanup','source_sha256':sha,'reason':'Mechanical cleanup; editorial review remains outstanding','confidence':1.0})
    candidate=directory/'candidate.json'
    if candidate.exists():
        if read(sp,candidate)!=revised:raise ValueError('Cleanup candidate differs from the existing job')
    else:write(sp,candidate,revised)
    if bridge.CLIENT is not None:
        from .episode_workshop import progress_phase
        progress_phase(studio,job,'Verifying exact inputs and saving cleanup · cancellation available')
        return bridge.CLIENT.call('cleanup-register')
    return publish_cleanup_candidate(studio,job,deps)



def execute(studio,job):
    if job['operation']=='video-kept' and str(job['args'].get('source_id','')).startswith('episode_'):
        from .episode_release import execute as release_execute
        return release_execute(studio,job)
    validate_operation(studio.sp,job['operation'],job['args'])
    if job['operation']=='video-kept': return adapt_kept(studio,job['args'])
    if job['operation']=='video-cleanup': return cleanup_programme(studio,job)
    return add_source(studio,job['args']) if job['operation']=='video-source' else render(studio,job['args'])


def audio_sources(sp):
    rows=[]
    for rec in store.read_assets(sp).values():
        if rec['kind']=='audio' and (rec.get('origin') or {}).get('schema')=='video_render.v1':
            origin=rec['origin'];ep,meta,path,doc,sha=exact(sp,origin['project'],origin['version'],origin['edit_sha256'])
            rows.append({'source_id':'output_'+rec['id'],'kind':'audio','label':meta['title']+' — production reference','duration':doc['duration'],'asset':rec['id'],'sha256':rec['sha256'],'previews':[{'kind':'audio','url':url(sp,sp.root/rec['file']),'label':'Production reference; excluded from social delivery'}],'ready':True,'production_reference':True})
    return rows


def attach_audio(studio,body):
    from .workbench import job_path
    production.only(body,{'job_id','asset','post','expected_rev'})
    job=read(studio.sp,job_path(studio.sp,body.get('job_id')))
    if job['status']!='done' or job['operation']!='video-render': raise ValueError('Choose a completed programme export')
    selected=next((a for a in job['result']['assets'] if a['asset']==body.get('asset') and a['kind']=='audio'),None)
    if not selected: raise ValueError('Choose this job’s exact audio output')
    post=store.load_post(studio.sp,model.check_id(body.get('post')))
    number(body.get('expected_rev'),1,1000000,'Captured post revision',True)
    refs=production.defaults(post)['sources'];sid='output_'+selected['asset']
    if not any(r['source_id']==sid for r in refs): refs.append({'source_id':sid,'label':'Production reference (excluded from social delivery)','locator':{'start':0,'end':selected['duration']}})
    from .episode_release import native_attachment
    with native_attachment(studio,job) as guard:
        return production.update(studio,post['id'],{'sources':refs},expected_rev=body['expected_rev'],actor=Actor('human','local creator'),_commit_context=guard)


def kept_clips(sp):
    """Only server-registered managed episodes; native external-root registration belongs to Episode."""
    from .. import learning, clips
    from .workbench import source_index
    rows=[]
    for recording_row in source_index(sp)['sources']:
        if recording_row.get('kind') not in ('audio','video') or not recording_row.get('ready'): continue
        ep=episode_for(sp,recording_row);path=ep.work/'clips.json'
        if not path.exists(): continue
        data=read(sp,path);decisions=learning.latest_decisions(ep)
        for clip in data.get('clips',[]):
            decision=decisions.get(clip['id'],{})
            if decision.get('verdict')!='keep' or decision.get('via') not in ('tty','ui') or not clip.get('edit'): continue
            sel=clip['edit'];p=safe(sp,editdoc.path_for(ep,'select',sel['version'],sel['name']))
            if not p.exists(): continue
            rows.append({'source_id':recording_row['source_id'],'clip_id':clip['id'],'title':clip.get('title',clip['id']),
                         'version':sel['version'],'sha256':sha256_file(p),'decision_sha256':digest(decision)})
    from .service import open_studio
    from . import episode_registry as R,episode_checkpoints as C
    studio=open_studio(sp.root,publishers={},host=None)
    latest={}
    for event in C.events(studio):
        if event.get('kind')=='keep':latest[(event['episode'],event['session'])]=event
    for (eid,name),event in latest.items():
        try:
            with R.resolve(studio,eid,verify_sources=False) as (ep,registration):
                if C.status(studio,ep,registration,event)['state']!='reviewed exact':continue
                from .episode_workshop import exact as episode_exact
                _,doc=episode_exact(ep,event['payload'])
                rows.append({'source_id':eid,'clip_id':name,'title':doc['release_selection']['title'],'version':event['payload']['version'],'sha256':event['payload']['sha256'],'decision_sha256':R.digest(event)})
        except (ValueError,OSError,KeyError):continue
    return {'clips':rows[:200]}


def resolve_kept(sp,args):
    if str(args.get('source_id','')).startswith('episode_'):
        from .service import open_studio
        from .episode_release import native_kept_inputs
        from .episode_registry import GrantedEpisode
        deps=native_kept_inputs(open_studio(sp.root,publishers={},host=None),args)
        return GrantedEpisode(Path(deps['source_episode'])),deps['doc'],{**args,'title':deps['title'],'native':True}
    production.only(args,{'source_id','clip_id','version','sha256','decision_sha256','request_id'})
    request(args.get('request_id'));number(args.get('version'),1,1000000,'Select version',True)
    descriptor=next((x for x in kept_clips(sp)['clips'] if x['source_id']==args.get('source_id') and x['clip_id']==args.get('clip_id')),None)
    if not descriptor or any(descriptor[k]!=args.get(k) for k in ('version','sha256','decision_sha256')): raise ValueError('Kept clip decision or exact select changed; refresh the registered selection')
    from .. import clips
    row=recording(sp,args['source_id']);ep=episode_for(sp,row);clip=next(c for c in read(sp,ep.work/'clips.json')['clips'] if c['id']==args['clip_id'])
    # Reuse existing exact-source transcript/EditDoc fingerprint validation.
    clips._source_docs(ep,clips.load_rubric(ep),[clip])
    path=safe(sp,editdoc.path_for(ep,'select',clip['edit']['version'],clip['edit']['name']))
    return ep,read(sp,path),descriptor


def kept_dialogue_intervals(doc):
    """Preserve compiler seam midpoints and sample-grid envelope anchors for overlapping handles."""
    from ..compile import SR
    segments = sorted(doc['tracks']['dialogue'], key=lambda s: s['o0'])
    boundaries = [0, *[(a['o1'] + b['o0']) / 2 for a, b in zip(segments, segments[1:])], doc['duration']]
    projected = []
    for segment, a, b in zip(segments, boundaries, boundaries[1:]):
        start, end = round(a * SR), round(b * SR)
        old_start, old_end = round(segment['o0'] * SR), round(segment['o1'] * SR)
        projected.append({**segment, 'o0': a, 'o1': b,
            'e0': segment['e0'] + (start - old_start) / SR,
            'fade_in_offset_s': (start - old_start + round(segment.get('fade_in_offset_s', 0) * SR)) / SR,
            'fade_out_remaining_s': (old_end - end + round(segment.get('fade_out_remaining_s', 0) * SR)) / SR})
    return projected


def kept_clip_segments(doc, descriptor):
    """Project original picture/dialogue intersections without replacing either track's controls."""
    dialogue_track = kept_dialogue_intervals(doc)
    boundaries = {0, doc['duration']}
    for segments in (doc['tracks']['video'], dialogue_track):
        for segment in segments:
            boundaries.update((segment['o0'], segment['o1']))
    bounds = sorted(boundaries)
    result, previous_dialogue, occurrence = [], None, None
    for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
        dialogue = next(s for s in dialogue_track if s['o0'] <= a + 1e-8 and s['o1'] >= b - 1e-8)
        picture = next((s for s in doc['tracks']['video'] if s['o0'] <= a + 1e-8 and s['o1'] >= b - 1e-8), dialogue)
        src, dsrc = doc['sources'][picture['src']], doc['sources'][dialogue['src']]
        e0 = picture['e0'] + a - picture['o0']
        de0 = dialogue['e0'] + a - dialogue['o0']
        continuous = previous_dialogue and previous_dialogue['src'] == dialogue['src'] and not previous_dialogue.get('fill') and not dialogue.get('fill') and abs(previous_dialogue['end'] - de0) < 1e-8
        if not continuous:
            occurrence = 'kept_' + digest({'descriptor': descriptor, 'piece': i})[:20]
        previous_dialogue = {'src': dialogue['src'], 'end': de0 + b - a, 'fill': dialogue.get('fill')}
        fi = editdoc.src_time(src, e0)
        result.append({'id': f'kept_{i}', 'occurrence_id': occurrence,
                       'source': picture['src'], 'in': fi, 'out': editdoc.src_time(src, e0 + b - a),
                       'dialogue_source': dialogue['src'], 'dialogue_in': editdoc.src_time(dsrc, de0),
                       'link_source_in': fi, 'dialogue_id': dialogue['id'], 'sound_group': dialogue['id'],
                       'xfade_ms': dialogue.get('xfade_ms', 0) if abs(a - dialogue['o0']) < 1e-8 else 0,
                       'sound_envelope_in': editdoc.src_time(src, e0 - (a - dialogue['o0']) - dialogue.get('fade_in_offset_s', 0)),
                       'sound_envelope_out': editdoc.src_time(src, e0 + dialogue['o1'] - a + dialogue.get('fade_out_remaining_s', 0)),
                       **{k: copy.deepcopy(dialogue[k]) for k in ('fill', 'gain_db', 'channel', 'fade_in_s', 'fade_out_s') if k in dialogue},
                       **{k: copy.deepcopy(picture[k]) for k in ('crop', 'zoom') if k in picture}})
    return result


def adapt_kept(studio,args):
    """Preserve source clocks and J/L relationships; never reinterpret cameras as independent recordings."""
    if str(args.get('source_id','')).startswith('episode_'):
        raise ValueError('Native kept intake requires its bound durable worker')
    sp=studio.sp;source_ep,original,descriptor=resolve_kept(sp,args)
    unsupported=[track for track in ('music','overlays') if original['tracks'].get(track)]
    if unsupported:
        raise ValueError('Kept-clip '+editdoc.effective_mode(original)+' select contains '+', '.join(unsupported)+' tracks. Open it in Episode for track-preserving intake; this narrow Media adapter accepts picture/dialogue selects only.')
    p=create(studio,{'title':descriptor['title'],'mode':editdoc.effective_mode(original),'request_id':'kept-'+args['request_id']})
    ep,meta=project(sp,p['id'])
    if p['doc'].get('kept_clip'):
        if meta['project_sources']!=p['doc']['sources']:
            with editdoc.mutation_lock(ep):
                meta['project_sources']=copy.deepcopy(p['doc']['sources']);meta['project_rev']+=1;write(sp,ep.meta_path,meta)
        return load(sp,p['id'])
    transcript=source_ep.artifact('transcript.json');transcript_sha=sha256_file(safe(sp,transcript))
    target=ep.work/'transcripts'/(transcript_sha+'.json');_copy_immutable(sp,transcript,target,transcript_sha)
    doc=copy.deepcopy(original);doc.update(stage='short',independent_project=p['id'],kept_clip=descriptor,media_mode={'version':1,'kind':editdoc.effective_mode(original)},caption_edit={'style':'podcast','burn':editdoc.effective_mode(original)!='audio','corrections':[]},clips=[])
    for key,src in doc['sources'].items():
        source=safe(sp,source_ep.root/src['file']);probe=media.probe_file(source,sp.root);sha=sha256_file(source);directory=ep.work/'sources'/digest(key)[:24];dest=directory/('original'+source.suffix);_copy_immutable(sp,source,dest,sha)
        parent=next((r['id'] for r in store.read_assets(sp).values() if r['sha256']==sha and not r.get('derivation')),None)
        if parent is None:
            parent=library.add_file(sp,source,source={'type':'import','ref':args['source_id']},license_entry={'source':'registered episode','url':'urn:ave:source:'+sha,'license':'unverified','license_url':'urn:ave:license:unverified','license_text':'Usage rights not established; human license review and attestation required.'},by='workbench',attested=False)['id']
        proxy_path,wave=proxy(sp,dest,directory,probe)
        src.update(file=ep.rel(dest),source_id=args['source_id']+':'+key,sha256=sha,parent=parent,duration=probe['duration'],has_audio=bool(probe['audio']),label=key,w=(probe['video'] or {}).get('width',0),h=(probe['video'] or {}).get('height',0),proxy=ep.rel(proxy_path),waveform=ep.rel(wave),transcript={'file':ep.rel(target),'sha256':transcript_sha,'clock':'episode'})
        # The compiler applies a local clock rate, so pieces are split at every knot below.
    doc['clips'] = kept_clip_segments(doc, descriptor)
    _tracks(doc);caption_snapshot(sp,ep,doc);doc['history']=[{'request_id':args['request_id'],'operation_sha256':digest(args),'reason':'Import exact human-kept select; editorial review is not transferred','confidence':1.0}]
    with editdoc.mutation_lock(ep):
        meta['project_sources']=copy.deepcopy(doc['sources']);meta['project_rev']+=1
        editdoc.save_new_version(ep,doc,expected_version=1,expected_sha256=p['sha256'],request_id=args['request_id'],locked=True)
        write(sp,ep.meta_path,meta)
    return load(sp,p['id'])
