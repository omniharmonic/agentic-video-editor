"""Mechanical release packaging from an explicitly selected immutable receipt.

No discovery, approvals or latest-file fallback lives here. The caller supplies
granted, validated paths and revalidates its authority before publication.
Legacy CLI discovery remains in package.build.
"""
from __future__ import annotations
import copy
import html
import json
import math
from pathlib import Path
import shutil

from .. import ffutil
from ..publish.safety import sha256_file
from . import audio, chapters


def bound_chapters(items, duration, binding, words=()):
    """Output-time chapters, with optional unambiguous occurrence locators."""
    if not isinstance(items,list) or len(items)>200:
        raise ValueError('Choose at most 200 chapters')
    result=[];previous=-1.
    for item in items:
        if not isinstance(item,dict) or set(item)-{'start','title','locator'}:
            raise ValueError('Invalid chapter fields')
        start=item.get('start');title=item.get('title')
        if type(start) not in (int,float) or not math.isfinite(start) or not 0<=start<duration or start<=previous:
            raise ValueError('Chapters must have increasing output times inside the selected programme')
        if not isinstance(title,str) or not title.strip() or len(title)>200:
            raise ValueError('Give each chapter a title of at most 200 characters')
        locator=item.get('locator')
        if locator is not None:
            if not isinstance(locator,dict) or set(locator)!={'occurrence_id','transcript_sha256','word_id'}:
                raise ValueError('A source chapter requires an exact occurrence and word identity')
            found=[w for w in words if all(w.get(k)==v for k,v in locator.items())]
            if len(found)!=1 or abs(found[0]['start']-start)>.001:
                raise ValueError('Chapter source occurrence changed or was removed; select the current word')
        result.append(copy.deepcopy(item));previous=start
    return {'schema':'chapters.v1','clock':'output','binding':copy.deepcopy(binding),
            'chapters':result,'normalization':[], 'title_status':'creator-authored draft'}


def verify_input(spec):
    if spec.get('schema')!='package_input.v1':raise ValueError('Exact package input required')
    for key in ('render','caption'):
        selected=spec[key];path=Path(selected['path'])
        if not path.is_file() or sha256_file(path)!=selected['sha256']:
            raise ValueError('Selected '+key+' bytes changed; no fallback is permitted')
    probe=ffutil.probe(Path(spec['render']['path']))
    if abs(probe['duration']-spec['duration'])>.15:raise ValueError('Selected programme duration changed')
    return probe


def plan(spec, *, formats=('mp3',), include_originals=False):
    verify_input(spec)
    if not isinstance(formats,(list,tuple)) or not formats or set(formats)-{'mp3','aac'} or len(set(formats))!=len(formats):
        raise ValueError('Choose MP3 and/or AAC podcast delivery')
    selected=Path(spec['render']['path'])
    entries=[{'source':str(selected),'sha256':spec['render']['sha256'],
              'destination':'episode/selected-final'+selected.suffix,
              'role':'episode_audio' if spec['mode']=='audio' else 'episode_video'},
             {'source':spec['caption']['path'],'sha256':spec['caption']['sha256'],
              'destination':'captions/programme.srt','role':'captions'}]
    if include_originals:
        for i,source in enumerate(spec['sources']):
            entries.append({'source':source['path'],'sha256':source['sha256'],
                            'destination':f'originals/{i+1:03d}-'+Path(source['path']).name,
                            'role':'original_source'})
    return {'schema':'exact_package_plan.v1','input':copy.deepcopy(spec),'entries':entries,
            'audio_formats':list(formats),'include_originals':bool(include_originals),
            'selected_quality':spec['render'].get('quality',{}),
            'delivery_note':'Selected final bytes are copied unchanged. Podcast audio is encoded at the recorded delivery settings. '
                            'Originals, when selected, are independent full-quality source copies. Nothing publishes.'}


def build(pl, out, *, policy):
    """Write only a new caller-owned staging directory; never a reference-only pack."""
    from .build import vtt_from_srt
    spec=pl['input'];verify_input(spec);out=Path(out)
    if out.exists():raise ValueError('Package destination already exists; preserve its bytes')
    out.mkdir(parents=True)
    files=[]
    for entry in pl['entries']:
        source=Path(entry['source']);target=out/entry['destination'];target.parent.mkdir(parents=True,exist_ok=True)
        if sha256_file(source)!=entry['sha256']:raise ValueError('Package source changed')
        shutil.copyfile(source,target)
        if sha256_file(target)!=entry['sha256']:raise ValueError('Package copy differs from selected input')
        files.append({'file':entry['destination'],'sha256':entry['sha256'],'role':entry['role'],'bytes':target.stat().st_size})
    (out/'captions/programme.vtt').write_text(vtt_from_srt(out/'captions/programme.srt'))
    audio_reports=[]
    for fmt in pl['audio_formats']:
        target=out/'audio'/('podcast'+audio.EXT[fmt])
        report=audio.build(Path(spec['render']['path']),target,fmt,spec['chapters']['chapters'],{'title':spec['title']},policy)
        report['file']=target.relative_to(out).as_posix();audio_reports.append(report)
    (out/'copy').mkdir()
    (out/'copy/description.txt').write_text(spec.get('copy','')+'\n\n'+'\n'.join(spec.get('credits',[]))+'\n')
    (out/'chapters.json').write_text(json.dumps(spec['chapters'],ensure_ascii=False,indent=2)+'\n')
    (out/'credits.txt').write_text('\n'.join(spec.get('credits',[]))+'\n')
    playable='episode/selected-final'+Path(spec['render']['path']).suffix
    tag='audio' if spec['mode']=='audio' else 'video'
    links=''.join(f'<li>Podcast {html.escape(r["format"].upper())}<audio controls src="{html.escape(r["file"])}"></audio></li>' for r in audio_reports)
    (out/'index.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>body{font:18px system-ui;max-width:900px;margin:40px auto;padding:20px;background:#f7f4ed;color:#18372d}video,audio{width:100%}a{color:#245f49}</style>'
        f'<title>{html.escape(spec["title"])}</title><h1>{html.escape(spec["title"])}</h1>'
        f'<{tag} controls src="{html.escape(playable)}"></{tag}><p>Final version {spec["binding"]["edit"]["version"]}</p><ul>{links}</ul>'
        f'<details><summary>Selected version details</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{html.escape(json.dumps(spec["binding"],indent=2))}</pre></details>'
        '<p>Saved package playback. Current export eligibility is checked when downloading from the workspace. Publication is a separate protected action.</p>')
    indexed={r['file'] for r in files}
    for path in sorted(out.rglob('*')):
        if path.is_file() and path.relative_to(out).as_posix() not in indexed:
            files.append({'file':path.relative_to(out).as_posix(),'sha256':sha256_file(path),'bytes':path.stat().st_size,'role':'delivery'})
    # Absolute paths are input provenance, never downloadable folder references.
    manifest={'schema':'exact_release_package.v1','input':copy.deepcopy(spec),'files':files,
              'audio':audio_reports,'publication':'not queued or sent','delivery':pl['delivery_note']}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    hashes=[(r['file'],r['sha256']) for r in files]+[('manifest.json',sha256_file(out/'manifest.json'))]
    (out/'checksums.sha256').write_text(''.join(f'{sha}  {name}\n' for name,sha in sorted(hashes)))
    return manifest
