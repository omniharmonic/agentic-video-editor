"""Bounded, non-authoritative saved episode views. Never used by mutations.

This module reads signed history, small documents and current filesystem facts.
It does not establish media integrity, editorial eligibility or license coverage.
Protected actions keep their ordinary full validators, even for these inputs.
"""
from __future__ import annotations
import copy
from pathlib import Path
from .. import library
from ..publish.safety import sha256_file
from . import episode_registry as R,episode_checkpoints as C,episode_workshop as E
from .workbench import safe,read

PROVENANCE={'schema':'episode_saved_view.v1','authoritative':False,'content_verified':False,
            'note':'Saved reviews and versions. Sources, licenses and exact bytes are checked again before protected actions.'}


def _strings(value):
    if isinstance(value,str):yield value
    elif isinstance(value,dict):
        for v in value.values():yield from _strings(v)
    elif isinstance(value,list):
        for v in value:yield from _strings(v)


def _stat(path):
    s=R.regular(path).stat()
    return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]


def observation(studio,ep,row,deps):
    """Cheap facts signed with a new checkpoint, never a content digest cache."""
    files={p:list(s) for p,s in R._source_stats(ep,row).items()}
    records=library.read_all(studio.sp)
    for key in set(_strings(deps)).intersection(records):
        p=safe(studio.sp,studio.sp.root/records[key]['file']);files[str(p)]=_stat(p)
    return {'source_fingerprint':row['source_fingerprint'],'files':files}


class Reader:
    def __init__(self,studio):self.studio=studio

    def bind(self,ep,row):
        self.ep=ep;self.row=row;self.events=C.events(self.studio)
        self.by_id={e['checkpoint_id']:e for e in self.events}
        self.latest={(e['episode'],e['kind'],e['session']):e for e in self.events}
        self.evidence=C.evidence(ep,row);self.source_problem=None
        try:self.source_stats={p:list(s) for p,s in R._source_stats(ep,row).items()}
        except (ValueError,OSError) as exc:self.source_stats={};self.source_problem=str(exc)
        self.records=library.read_all(self.studio.sp)
        from ..library import registry,ingest
        self.license_log=registry.license_log(self.studio.sp)
        if self.license_log.verify():raise ValueError('License ledger verification failed')
        self.attestations=ingest.attestation_events(self.studio.sp)
        return self

    def _assets(self,value):
        from ..library import derive,ingest
        from ..assets import licenses
        ids=set(_strings(value)).intersection(self.records)
        order,unknown=derive.closure(self.records,ids)
        if unknown or len(order)>300:raise ValueError('Saved asset provenance changed')
        for aid in order:
            rec=self.records[aid];safe(self.studio.sp,self.studio.sp.root/rec['file']).stat()
            entry=self.license_log.find(sha256=rec['sha256'])
            state=ingest.attestation_for(self.attestations,aid,rec['sha256'],licenses.entry_hash(entry) if entry else None)
            if state in ('withdrawn','stale'):raise ValueError('Saved asset license or attestation changed')

    def _dependencies(self,value,seen):
        if isinstance(value,str) and value.startswith('check_'):
            event=self.by_id.get(value)
            if not event:raise ValueError('Saved upstream review is missing')
            self._event(event,seen)
        elif isinstance(value,list):
            for v in value:self._dependencies(v,seen)
        elif isinstance(value,dict):
            if value.get('v')==1 and value.get('kind')=='broll.proposal':
                asset=value.get('asset') or {};ref=(value.get('placement') or {}).get('library') or {}
                aid=asset.get('id') if isinstance(asset,dict) else None
                rec=self.records.get(aid) if isinstance(aid,str) else None
                if not rec or ref.get('asset')!=aid or ref.get('sha256')!=rec['sha256']:raise ValueError('Saved B-roll asset identity changed or missing')
            if {'stage','name','version','sha256'}<=set(value):E.exact(self.ep,value)
            if 'source_reference' in value:
                if value['source_reference'] not in self.row['sources']:raise ValueError('Saved source registration changed')
                if value.get('transcript_sha256')!=C.file_sha(self.ep,'transcript.json'):raise ValueError('Saved transcript changed')
            if 'source_fingerprint' in value and value['source_fingerprint']!=self.row['source_fingerprint']:raise ValueError('Saved source inventory changed')
            if 'sync' in value and isinstance(value['sync'],dict):
                current=(self.evidence['sync'] or {}).get('files',{})
                if any(current.get(k)!=v for k,v in value['sync'].items()):raise ValueError('Saved source clocks changed')
            if isinstance(value.get('id'),str) and value['id'] in self.records and 'sha256' in value and self.records[value['id']]['sha256']!=value['sha256']:raise ValueError('Saved asset identity changed')
            if isinstance(value.get('asset'),str) and value['asset'] in self.records and value.get('entry'):
                from ..assets import licenses
                rec=self.records[value['asset']];entry=self.license_log.find(sha256=rec['sha256'])
                if not entry or licenses.entry_hash(entry)!=value['entry']:raise ValueError('Saved license entry changed')
            for v in value.values():self._dependencies(v,seen)

    def _event(self,event,seen):
        if event['checkpoint_id'] in seen:return
        seen.add(event['checkpoint_id'])
        latest=self.latest.get((self.row['id'],event['kind'],event['session']))
        if latest!=event or event.get('revoked'):raise ValueError('Saved review was replaced or revoked')
        if event['grant_revision']!=self.row['grant_revision']:raise ValueError('Saved review belongs to an earlier access grant')
        if self.source_problem:raise ValueError(self.source_problem)
        saved=event.get('view_observation')
        if saved:
            for path,stats in saved['files'].items():
                if _stat(path)!=stats:raise ValueError('Source or asset filesystem facts changed; content needs verification')
        if R.digest(event['dependencies'])!=event['dependency_sha256']:raise ValueError('Saved dependency receipt changed')
        self._dependencies(event['dependencies'],seen);self._assets(event['dependencies'])

    def status(self,studio,ep,row,event):
        if not event:return {'state':'needs review','reason':'No saved native confirmation','authoritative':False}
        try:self._event(event,set())
        except (ValueError,OSError,KeyError) as exc:return {'state':'stale','reason':str(exc),'event':event['checkpoint_id'],'authoritative':False}
        return {'state':'review recorded','reason':PROVENANCE['note'],'event':event['checkpoint_id'],'payload':copy.deepcopy(event['payload']),
                'simulation':bool(event.get('simulation')),'authoritative':False,'content_verified':False}

    def recorded(self,kind,session):
        event=self.latest.get((self.row['id'],kind,session));status=self.status(self.studio,self.ep,self.row,event)
        if status['state']!='review recorded':raise ValueError(status['reason'])
        return event

    def dependencies(self,value):
        try:
            if self.source_problem:raise ValueError(self.source_problem)
            self._dependencies(value,set());self._assets(value)
            return True
        except (ValueError,OSError,KeyError):return False

    def artifact(self,studio,eid,aid):
        rec=E._artifact_record(studio,eid,aid);path=safe(studio.sp,studio.sp.root/rec['file'])
        facts=_stat(path)
        if rec.get('file_stats') and facts!=rec['file_stats']:raise ValueError('Saved preview filesystem facts changed')
        return rec

    def sequence(self,record):
        E.check_descriptor(self.studio,{'id':record['args'].get('job_id'),'operation':record['operation'],'args':record['args'],'episode_descriptor':record['descriptor']})
        return self.dependencies(record['snapshot']['dependencies'])


def load(studio,eid,session='main'):
    result=E._construct_load(studio,eid,session,reader=Reader(studio))
    result['view_provenance']=copy.deepcopy(PROVENANCE)
    for key in ('assemblies','roughs','sequences','finals'):
        for item in result[key]:item['saved_inputs_match']=item.pop('inputs_current')
    return result


def release(studio,eid,session):
    from . import episode_release as L
    with R.resolve(studio,eid,verify_sources=False) as (ep,row):
        reader=Reader(studio).bind(ep,row)
        result=L._catalog(studio,eid,session,reader=reader)
        result['view_provenance']=copy.deepcopy(PROVENANCE)
        return result


def candidates(studio,eid,session):
    from . import episode_release as L
    with R.resolve(studio,eid,verify_sources=False) as (ep,row):
        reader=Reader(studio).bind(ep,row);event=reader.recorded('final',session)
        return {**L._candidate_table(ep,event),'view_provenance':copy.deepcopy(PROVENANCE)}


def caption_words(studio,eid,session,page):
    E.number(page,0,100000,'Caption page',True)
    with R.resolve(studio,eid,verify_sources=False) as (ep,row):
        reader=Reader(studio).bind(ep,row);event=reader.recorded('rough',session)
        return {**E._caption_word_page(ep,row,event,page),'view_provenance':copy.deepcopy(PROVENANCE)}
