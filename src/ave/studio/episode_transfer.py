"""Exact used-file closure and path rebasing for episode/Media transfers.

Filesystem access and authorship are separate: copying cannot approve an edit
or attest a license. Clock data, occurrence IDs and dependent tracks survive.
"""
from __future__ import annotations
import copy
import os
from pathlib import Path

from .. import editdoc, library, media
from ..publish.safety import sha256_file
from . import episode_registry as R, episode_workshop as E, video_workshop as V
from .workbench import safe, _copy_immutable


def closure(studio,ep,doc,registered=None):
    records=library.read_all(studio.sp)
    by_sha={rec['sha256']:rec for rec in records.values() if not rec.get('derivation')}
    used={s['src'] for kind in ('video','dialogue') for s in doc['tracks'][kind]}
    if doc['tracks'].get('room_tone'):used.add(doc['tracks']['room_tone']['src'])
    files={};sources={};assets={}
    def add(rel,role,expected=None,owned=False):
        path=R.regular(ep.root/rel)
        allowed=[studio.sp.work] if owned else [*ep.raw_dirs,ep.work,ep.captions,ep.gfx,ep.music]
        if not any(path.is_relative_to(R.regular(root,directory=True)) for root in allowed):raise ValueError('Programme dependency leaves its granted episode')
        stat=path.stat();identity=[stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns]
        actual=R._verified_raw_sha(ep,registered,path) if registered and role=='source' else sha256_file(path)
        after=path.stat()
        if [after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns]!=identity:raise ValueError('Programme dependency changed during verification: '+role)
        if expected and actual!=expected:raise ValueError('Programme dependency changed: '+role)
        dest=('raw/' if role=='source' else 'captions/snapshots/' if role=='caption-profile' else 'work/closure/')+actual+path.suffix.lower()
        files[rel]={'path':str(path),'sha256':actual,'destination':dest,'role':role,'bytes':stat.st_size,'stat_identity':identity}
        return files[rel]
    for key in sorted(used):
        src=doc['sources'][key];expected=src.get('sha256')
        if registered:
            reg=next((s for s in registered['sources'] if s['file']==src['file']),None)
            if not reg:raise ValueError('Used source is not in the registered episode')
            expected=reg['sha256']
        item=add(src['file'],'source',expected);probe=media.probe_file(Path(item['path']),ep.root)
        parent=records.get(src.get('parent')) or by_sha.get(item['sha256'])
        if not parent or parent['sha256']!=item['sha256']:raise ValueError('Used source has no exact library provenance')
        sources[key]={'sha256':item['sha256'],'parent':parent['id'],'probe':probe,'source_id':src.get('source_id') or (registered['id']+':'+key if registered else key)}
        # Reuse the exact existing browser rendition; never decode hours of
        # original footage again merely to move an editable programme.
        if src.get('proxy') and (not registered or registered.get('adoption')):
            inherited={v['file']:v['sha256'] for v in registered.get('adopted_files',[])} if registered else None
            if inherited is not None and src['proxy'] not in inherited:raise ValueError('Adopted preview is outside its exact registered closure')
            add(src['proxy'],'source-preview',inherited[src['proxy']] if inherited is not None else None);sources[key]['proxy']=src['proxy']
            if src.get('waveform'):
                if inherited is not None and src['waveform'] not in inherited:raise ValueError('Adopted waveform is outside its exact registered closure')
                add(src['waveform'],'waveform',inherited[src['waveform']] if inherited is not None else None);sources[key]['waveform']=src['waveform']
        elif registered:
            previews=studio.sp.work/'production'/'episode-artifacts'
            for record_path in sorted(previews.glob('artifact_*.json')):
                from .workbench import read
                record=read(studio.sp,record_path)
                if record.get('episode')!=registered['id'] or record.get('kind')!='source' or record.get('source',{}).get('sha256')!=item['sha256'] or record.get('grant_revision')!=registered['grant_revision']:continue
                record=E.artifact(studio,registered['id'],record['id'])
                preview=str(studio.sp.root/record['file'])
                add(preview,'source-preview',record['sha256'],owned=True);sources[key]['proxy']=preview
                break
        if not sources[key].get('proxy'):raise ValueError('Used source needs its registered browser preview before transfer')
        transcript=src.get('transcript')
        if transcript:add(transcript['file'],'transcript',transcript['sha256'])
    if not doc.get('independent_project'):
        add(ep.rel(ep.artifact('transcript.json')),'transcript')
    for track in ('music','overlays'):
        for item in doc['tracks'].get(track,[]):
            if track=='overlays' and item.get('kind') not in ('broll','credit','motion','image'):raise ValueError('Unsupported overlay cannot be transferred: '+str(item.get('kind')))
            ref=item.get('library') or {};sha=item.get('sha256') or ref.get('sha256')
            add(item['file'],track,sha)
            aid=item.get('asset') or ref.get('asset')
            if aid:
                if ref.get('studio') and (ep.root/ref['studio']).resolve()!=studio.sp.root.resolve():raise ValueError('Asset license belongs to a different studio; import its provenance first')
                rec=records.get(aid)
                if not rec:raise ValueError('Dependent asset is missing from the library')
                assets[aid]=rec['sha256']
    if doc.get('caption_snapshot'):add(doc['caption_snapshot']['file'],'caption-profile',doc['caption_snapshot']['sha256'])
    for key in ('srt','ass'):
        if doc.get('captions',{}).get(key):add(doc['captions'][key],'caption')
    licensed=E.licensed_asset_closure(studio,[{'id':aid,'sha256':sha} for aid,sha in sorted(assets.items())])
    return {'files':files,'sources':sources,'assets':licensed,'source_episode':str(ep.root)}


def independent(ep,doc,dependencies,descriptor):
    result=copy.deepcopy(doc);legacy=not result.get('independent_project')
    if legacy:
        result['independent_project']='episode-selection'
        result['clips']=V.kept_clip_segments(result,descriptor)
    result['sources']={key:src for key,src in result['sources'].items() if key in dependencies['sources']}
    transcript_rel=ep.rel(ep.artifact('transcript.json'))
    for key,src in result['sources'].items():
        identity=dependencies['sources'][key];probe=identity['probe']
        src.update(sha256=identity['sha256'],parent=identity['parent'],source_id=identity['source_id'],
                   duration=probe['duration'],has_audio=bool(probe['audio']),label=src.get('label',key),proxy=identity['proxy'],
                   w=(probe.get('video') or {}).get('width',0),h=(probe.get('video') or {}).get('height',0))
        if legacy:src['transcript']={'file':transcript_rel,'sha256':dependencies['files'][transcript_rel]['sha256'],'clock':'episode'}
    result.setdefault('caption_edit',{'style':'podcast','burn':False,'corrections':[]})
    for track in ('music','overlays'):
        for item in result['tracks'].get(track,[]):item.setdefault('anchor','occurrence')
    if legacy:
        # Keep existing source occurrence correction identities. For legacy cuts
        # without occurrence IDs, remap corrections by their exact output words.
        from ..occurrences import mapped_words
        old_words=mapped_words(ep,doc);corrections=result['caption_edit'].get('corrections',[])
        V._tracks(result)
        result['caption_edit']['corrections']=[];new_words=mapped_words(ep,result)
        for correction in corrections:
            original=[w for w in old_words if w.get('occurrence_id')==correction['occurrence_id'] and w.get('word_id') in correction['word_ids']]
            matching=[w for w in new_words if any(w['word_id']==v['word_id'] and abs(w['start']-v['start'])<.001 for v in original)]
            if len({w['occurrence_id'] for w in matching})!=1:raise ValueError('Caption correction does not have one transferable occurrence')
            c=copy.deepcopy(correction);c['occurrence_id']=matching[0]['occurrence_id'];result['caption_edit']['corrections'].append(c)
    return result


def rebase(studio,target,doc,dependencies):
    result=copy.deepcopy(doc);paths={old:item['destination'] for old,item in dependencies['files'].items()}
    for key,src in result['sources'].items():
        src['file']=paths[src['file']]
        identity=dependencies['sources'][key]
        src['proxy']=paths[identity['proxy']]
        if identity.get('waveform'):src['waveform']=paths[identity['waveform']]
        if src.get('transcript'):src['transcript']['file']=paths[src['transcript']['file']]
    for kind in ('music','overlays'):
        for item in result['tracks'].get(kind,[]):
            item['file']=paths[item['file']]
            if item.get('library'):item['library']['studio']=Path(os.path.relpath(studio.sp.root,target.root)).as_posix()
    if result.get('caption_snapshot'):result['caption_snapshot']['file']=paths[result['caption_snapshot']['file']]
    for key in ('srt','ass'):
        if result.get('captions',{}).get(key):result['captions'][key]=paths[result['captions'][key]]
    if result.get('native_source_hashes'):result['native_source_hashes']={paths[k]:v for k,v in result['native_source_hashes'].items() if k in paths}
    return result


def materialize(studio,target,dependencies):
    """Copy each distinct used dependency once; immutable target bytes can be reused."""
    for item in dependencies['files'].values():
        source=R.regular(item['path']);destination=safe(studio.sp,target.root/item['destination'])
        if source.is_relative_to(studio.sp.root):
            _copy_immutable(studio.sp,source,destination,item['sha256'])
        else:
            # The caller's inherited grant supplies this exact path and hash.
            # Use the shared staging/no-clobber primitives without broadening
            # ordinary studio.safe_path to accept external source aliases.
            from .workbench import _own_stage,_publish_staged
            import shutil
            destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists():
                if sha256_file(destination)!=item['sha256']:raise ValueError('Immutable transferred source changed')
                continue
            part=destination.with_suffix(destination.suffix+'.part');marker=part.with_suffix('.owner.json')
            _own_stage(studio.sp,part,marker,{'source_sha256':item['sha256'],'destination':studio.sp.rel(destination)})
            with source.open('rb') as inp,part.open('wb') as out:
                shutil.copyfileobj(inp,out,length=8*1024*1024);out.flush();os.fsync(out.fileno())
            if sha256_file(part)!=item['sha256']:raise ValueError('Transferred source differs from exact selected input')
            _publish_staged(part,destination)
