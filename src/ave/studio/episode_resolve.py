"""Exact master and explicitly limited source-body Resolve companions.

This module constructs plans; the release parent owns their captured inputs
and receipts. It never supplies approval or opens Resolve itself.
"""
from __future__ import annotations
import copy
from pathlib import Path
from .. import handoff,editdoc,proxies,media
from ..publish.safety import sha256_file
from . import episode_registry as R

LIMITATIONS=[
    'Source-body reference only: opening and closing programme components remain in the finished-master timeline.',
    'Source clips use exact source-clock in-points. Resolve Free cannot reproduce drift retiming, crossfades, ducking or gain envelopes through this bridge.',
    'Motion, B-roll fit/crop/animation and caption appearance may differ. Compare with the finished master before polishing.',
]


def companion_files(doc,args,directory,*,required):
    """Inspect only fixed, job-owned compiled companion audio destinations."""
    import soundfile as sf
    names={'source-body-mix.wav':round(doc['duration']*48000)}
    if args['layers']:
        keys={d['src'] for d in doc['tracks']['dialogue'] if d.get('src')}
        if doc['tracks'].get('room_tone'):keys.add(doc['tracks']['room_tone']['src'])
        names.update({'source-'+R.digest(key)[:20]+'.wav':None for key in keys})
    result=[]
    for name,frames in sorted(names.items()):
        path=R.regular(directory/name)
        if not path.exists():
            if required:raise ValueError('Resolve companion audio is missing: '+name)
            continue
        info=sf.info(path)
        if info.samplerate!=48000 or info.channels!=2 or not info.frames or (frames is not None and info.frames!=frames):raise ValueError('Resolve companion audio dimensions differ: '+name)
        result.append({'file':name,'sha256':sha256_file(path),'frames':info.frames,'sample_rate':info.samplerate,'channels':info.channels})
    return result

def inputs(ep,doc,args,artifact,studio):
    master=studio.sp.root/artifact['file'];info=media.probe_file(master,studio.sp.root)
    pmap,missing=proxies.proxy_map(ep,doc) if args['proxies'] else ({},[])
    verified=[]
    for original,proxy in sorted(pmap.items()):
        source=R.regular(original);path=R.regular(proxy)
        if not any(source==R.regular(ep.root/s['file']) for s in doc['sources'].values()):raise ValueError('Proxy source leaves selected edit')
        # Standard proxy locations are beneath the original's raw folder.
        if not any(path.is_relative_to(R.regular(root,directory=True)) for root in ep.raw_dirs):raise ValueError('Proxy leaves the granted raw folders')
        verified.append({'source':str(source),'path':str(path),'sha256':sha256_file(path)})
    unsupported=None
    if args.get('sources',False) or args['layers'] or args['proxies']:
        if editdoc.effective_mode(doc)=='audio':unsupported='Audio episodes support the finished audio-master timeline; picture/source-layer companions are unavailable.'
        elif args['layers'] and (doc.get('independent_project') or doc.get('combined_constituents')):unsupported='Alternate-camera layers require a shared episode clock; this independent or combined programme supports a source-body companion without alternate layers.'
    return {'canvas':{'w':info['video']['width'],'h':info['video']['height']} if info['video'] else doc['canvas'],
            'proxies':verified,'missing_proxies':missing,'unsupported':unsupported,'limitations':LIMITATIONS}

def plans(ep,doc,args,deps,directory,studio,*,materialize=False):
    artifact=deps['artifact'];path=studio.sp.root/artifact['file'];cap=artifact['details']['caption'];captured=deps['handoff']
    master=handoff.master_review_plan(doc,name=args['name'],master=path,duration=artifact['details']['report']['duration'],srt=path.parent/cap['filename'],try_subtitles=args['subtitles'])
    master['canvas']=captured['canvas']
    result={'master':master,'companion':None,'limitations':captured['limitations'],'unsupported':captured['unsupported']}
    if not (args.get('sources',False) or args['layers'] or args['proxies']) or captured['unsupported']:return result
    from .. import compile as compiler
    body=copy.deepcopy(doc);body.pop('sequences',None)
    audio=directory/'source-body-mix.wav'
    wavs={key:directory/('source-'+R.digest(key)[:20]+'.wav') for key in body['sources']}
    if materialize:
        if not audio.exists():compiler.compile_audio(ep,body,audio)
        if args['layers']:
            keys={d['src'] for d in body['tracks']['dialogue'] if d.get('src')}
            if body['tracks'].get('room_tone'):keys.add(body['tracks']['room_tone']['src'])
            for key in sorted(keys):
                if not wavs[key].exists():compiler.compile_source_sum(ep,body,key,wavs[key])
    pmap={x['source']:x['path'] for x in captured['proxies']}
    companion=handoff.plan(ep,body,name=args['name']+' · source body',audio_master=audio,layers=args['layers'],sum_wavs=wavs,
                            proxies=pmap,try_subtitles=args['subtitles'])
    if not args['layers']:companion['music']=[]  # already in the compiled body mix
    companion['warnings'].extend(captured['limitations'])
    if captured['missing_proxies']:companion['warnings'].append('No verified Resolve proxy for '+', '.join(Path(p).name for p in captured['missing_proxies'])+'; those clips use their originals.')
    companion['review_mode']='source-body-reference'
    result['companion']=companion
    return result
