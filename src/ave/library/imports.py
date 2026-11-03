"""Receiver-owned promotion of verified historical occurrences, without authority.

Used by bounded collaboration now and chunk completion later. Parent full identity
anchors are mandatory. This never calls add_derivative or rewrites the preimage.
"""
from __future__ import annotations
import copy
import fcntl
import os
import tempfile
from pathlib import Path
from .. import collab as C
from .. import collab_v2 as V
from . import registry
from .ingest import _probe
from ..studio import model
from ..studio.clock import SystemClock


def promote(sp, manifest, byte_path, *, receipt):
    V.validate_manifest(manifest);C._no_symlink(Path(byte_path))
    if not isinstance(receipt,str) or not C._HEX.fullmatch(receipt):raise ValueError('Exact bundle receipt required')
    if Path(byte_path).stat().st_size!=manifest['bytes'] or C.sha256_file(byte_path)!=manifest['sha256']:raise ValueError('Imported bytes differ from manifest')
    C._inside(sp,sp.library_dir/'.registration.lock');sp.library_dir.mkdir(parents=True,exist_ok=True)
    with (sp.library_dir/'.registration.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        known=registry.read_all(sp);existing=known.get(manifest['id'])
        if existing:
            if (existing.get('derivation') or {}).get('sha256',existing['sha256'])!=V.identity(manifest) or existing['sha256']!=manifest['sha256']:raise ValueError('Imported short identity collision')
            return existing
        for parent,anchor in zip(manifest.get('occurrence',{}).get('parents',[]),manifest['parents']):
            local=known.get(parent['id'])
            if not local or local['sha256']!=parent['sha256'] or (local.get('derivation') or {}).get('sha256',local['sha256'])!=anchor['identity_sha256']:raise ValueError('Exact parent identity unavailable: '+parent['id'])
            if C.sha256_file(C._inside(sp,registry.asset_file(sp,local)))!=parent['sha256']:raise ValueError('Parent bytes changed')
        rel=registry.rel_file(manifest['sha256'],manifest['ext']);dest=C._inside(sp,sp.root/rel)
        if dest.exists() and C.sha256_file(dest)!=manifest['sha256']:raise ValueError('Existing content addressed bytes differ')
        media=_probe(Path(byte_path),manifest['kind']);occ=manifest.get('occurrence')
        record={'v':1,'id':manifest['id'],'sha256':manifest['sha256'],'kind':manifest['kind'],'ext':manifest['ext'],'file':rel,'bytes':manifest['bytes'],**media,
                'source':{'type':'import'},'ai_generated':False,'derived_from':[p['id'] for p in manifest['parents']],
                'origin':copy.deepcopy(occ['origin']) if occ else None,'added':SystemClock().now().isoformat(),'added_by':'collaboration',
                'license':{'entry':None,'summary':'Untrusted import; local human license review required','attribution':{'required':False,'text':None}},
                'untrusted_import':{'schema':'received_occurrence.v1','manifest':copy.deepcopy(manifest),'receipt':receipt}}
        if occ:record['derivation']={'schema':'derivative_occurrence.v1','sha256':manifest['derivation_sha256']}
        model.require_valid('asset',record)
        if not dest.exists():
            dest.parent.mkdir(parents=True,exist_ok=True);fd,tmp=tempfile.mkstemp(prefix='.received-',dir=dest.parent)
            try:
                with os.fdopen(fd,'wb') as out,Path(byte_path).open('rb') as src:
                    while chunk:=src.read(1024*1024):out.write(chunk)
                    out.flush();os.fsync(out.fileno())
                if C.sha256_file(Path(tmp))!=manifest['sha256']:raise ValueError('Import source changed during copy')
                os.replace(tmp,dest)
            finally:Path(tmp).unlink(missing_ok=True)
        return registry.append(sp,record)
