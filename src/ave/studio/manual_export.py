"""Exact-byte manual publishing packs. Downloads never approve or publish.

A reviewed pack is bound to the same local approval/media/copy as automatic posting.
Draft packs need no connected social account and are conspicuously unreviewed.
"""
from __future__ import annotations

import copy
import fcntl
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
import zipfile

from ..publish import queue as pq, safety
from . import ledger, model, policy, posttext

MAX_METADATA = 8 * 1024 * 1024
MAX_FILE = 512 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_MEDIA = 20
PACKAGE_ID = re.compile(r'^manual-[0-9a-f]{32}$')
EXTENSIONS = {'.jpg','.jpeg','.png','.webp','.gif','.tif','.tiff','.bmp','.mp4','.mov','.webm','.m4v','.mp3','.wav','.m4a','.pdf'}


def _safe(path: Path, root: Path | None = None) -> Path:
    path = Path(os.path.abspath(Path(path).expanduser()))
    if root is not None:
        try: path.relative_to(root)
        except ValueError: raise ValueError('manual export file is outside the studio') from None
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('manual export never follows symlinks')
    if path.exists() and not path.is_file() and not path.is_dir():
        raise ValueError('manual export accepts regular files and directories only')
    return path


def _read(path: Path, root: Path, *, optional=False) -> bytes:
    _safe(path,root)
    if optional and not path.exists(): return b''
    if not path.is_file(): raise FileNotFoundError('manual export source metadata is missing')
    with path.open('rb') as f: raw=f.read(MAX_METADATA+1)
    if len(raw)>MAX_METADATA: raise ValueError('manual export metadata is too large')
    return raw


def _metadata(sp, post_id):
    model.check_id(post_id,'post id')
    for path in (sp.meta_path, sp.posts_dir/(post_id+'.json'),sp.library_dir/'assets.jsonl',sp.library_dir/'licenses.jsonl',
                 sp.ledger_dir/'control.jsonl',sp.ledger_dir/'.control.head',sp.ledger_dir/'feedback.jsonl',sp.ledger_dir/'.feedback.head',
                 sp.ledger_dir/'dispatch.jsonl',sp.ledger_dir/'.dispatch.head'):
        _read(path,sp.root,optional=path not in (sp.meta_path,sp.posts_dir/(post_id+'.json')))
    post=json.loads(_read(sp.posts_dir/(post_id+'.json'),sp.root))
    model.require_valid('post',post)
    rows={}
    for line in _read(sp.library_dir/'assets.jsonl',sp.root,optional=True).splitlines():
        if not line.strip(): continue
        rec=json.loads(line)
        model.require_valid('asset',rec)
        rows[rec['id']]=rec
    return post,rows


def _variant(post,variant_id):
    variants=post.get('variants') or []
    if variant_id is not None:
        model.check_id(variant_id,'variant id')
        return model.find_variant(post,variant_id)
    if len(variants)>1: raise ValueError('choose one variant for the manual export')
    if variants: return variants[0]
    return {'id':None,'account':None,'platform':'instagram','post_type':'carousel' if len(post.get('media') or [])>1 else 'feed',
            'copy':{},'media':None,'queue':None,'dropped':False}


def _asset_path(sp,rec):
    raw=rec.get('file')
    if not isinstance(raw,str) or Path(raw).is_absolute() or '\\' in raw or any(p in ('..','.') for p in raw.split('/')):
        raise ValueError('asset path must be a relative library file')
    path=_safe(sp.root/raw,sp.root)
    try: path.relative_to(sp.library_dir/'assets')
    except ValueError: raise ValueError('manual export only reads registered library media') from None
    if not path.is_file(): raise FileNotFoundError('manual export media is missing')
    if path.suffix.lower() not in EXTENSIONS: raise ValueError('unsupported manual export media format')
    return path


def _closure(sp,assets,ids):
    todo=list(ids);seen=set();total=0
    while todo:
        aid=todo.pop()
        if aid in seen: continue
        seen.add(aid)
        if len(seen)>200: raise ValueError('asset provenance closure is too large')
        rec=assets.get(aid)
        if rec is None: raise ValueError(f'asset {aid} is missing from the local library')
        path=_asset_path(sp,rec)
        size=path.stat().st_size
        total+=size
        if size>MAX_FILE or total>MAX_TOTAL: raise ValueError('manual export media exceeds the size limit')
        if safety.sha256_file(path)!=rec['sha256']: raise ValueError(f'asset {aid} bytes changed since registration')
        todo.extend(rec.get('derived_from') or [])


def _prepare(sp,post_id,variant_id,draft,now):
    sp.require()
    post,assets=_metadata(sp,post_id)
    variant=_variant(post,variant_id)
    if variant.get('account') is not None:
        account_id=model.check_id(variant['account'],'account id')
        _read(sp.accounts_dir/(account_id+'.yaml'),sp.root,optional=True)
    if post.get('stage')=='dropped' or variant.get('dropped'): raise ValueError('dropped content cannot be exported')
    from .composer import require_projection
    projected=require_projection(post,variant,assets)
    media=projected['content']
    cover=projected['cover']
    if len(media)>MAX_MEDIA: raise ValueError(f'at most {MAX_MEDIA} media files per manual pack')
    ids=[m['asset'] for m in media]
    all_ids=ids+([cover["asset"]] if cover else [])
    _closure(sp,assets,all_ids)
    from ..library.coverage import coverage
    cov=coverage(sp,all_ids)
    # Include credits required anywhere in the provenance closure, not only roots.
    composed=posttext.compose(model.effective_copy(post,variant),variant['platform'],credits=cov['attributions'])
    problems=list(composed.problems)
    from ..library.registry import license_log
    problems += ['license log integrity: '+p for p in license_log(sp).verify()]
    if not cov['ok']: problems+=['license closure incomplete: '+r['why'] for r in cov['missing']]
    if not media and not composed.text: raise ValueError('there is no media or caption to export')

    reviewed=False
    if not draft:
        ref=variant.get('queue')
        if not ref or ref.get('root','.')!='.':
            problems.append('reviewed manual export needs this studio’s exact queue item and human approval')
        else:
            qid=model.check_id(ref.get('id'),'queue id')
            path=sp.publish/'queue'/(qid+'.json')
            item=json.loads(_read(path,sp.root))
            for f in pq.resolve_files(sp,item): _safe(f,sp.root)
            if item.get('status') not in ('approved','published'):
                problems.append('the queue item has no completed human approval')
            verification=copy.deepcopy(item)
            if verification.get('status')=='published': verification['status']='approved'
            problems+=safety.approval_problems(verification,pq.current_sha(sp,item),now.timestamp())
            if policy.approval_expired(item,now,policy.approval_ttl_days(sp,variant.get('account'))):
                problems.append('studio approval expired; review again before final export')
            if item.get('meta_v') not in (2,3) or item.get('account')!=variant.get('account'):
                problems.append('queue item does not belong to this variant’s studio account')
            approval=(item.get('approvals') or [{}])[-1]
            bind_problem=safety.bind_problem(approval)
            if bind_problem:
                problems.append(bind_problem)
            else:
                # Expiry uses today's settings, but the human also signed the
                # lifetime policy in force during review. Widening that policy
                # cannot resurrect an approval that already aged out.
                signed_ttl=(approval.get('bind') or {}).get('ttl') or {}
                current_ttl={'publish_days':safety.approval_ttl_days(),
                             'studio_days':policy.approval_ttl_days(sp,variant.get('account'))}
                if not isinstance(signed_ttl,dict):
                    problems.append('the approved lifetime policy is invalid; review again')
                else:
                    import math
                    for key,current in current_ttl.items():
                        if key not in signed_ttl:
                            continue
                        bound=signed_ttl[key]
                        if isinstance(bound,bool) or not isinstance(bound,(int,float)) or not math.isfinite(bound) or bound<=0:
                            problems.append('the approved lifetime policy is invalid; review again')
                        elif current>bound:
                            problems.append(f'approval lifetime settings were raised after this approval ({key}: {bound:g} -> {current:g} days); review again under the new policy')
            from .sync import queue_mismatch
            problems+=['queue differs from reviewed post: '+p for p in queue_mismatch(sp,post,variant,item)]
            queued=pq.resolve_files(sp,item)
            if [p.resolve() for p in queued]!=[_asset_path(sp,assets[aid]).resolve() for aid in ids]:
                problems.append('queue media order differs from the export media')
        from . import seal
        problems += ['studio ledger integrity: '+p for p in seal.verify_all(sp)]
        if ref and ref.get('root','.')=='.' and (item.get('caption') != composed.text or item.get('first_comment') != composed.first_comment):
            problems.append('export text differs from the exact reviewed queue text')
        for note in ledger.open_feedback(sp,post=post_id,variant=variant.get('id'),now=now):
            if note.get('kind') in ('rework_copy','rework_visual','drop','retime'):
                problems.append('open feedback requires review before final export')
        reviewed=not problems
    manifest={'v':1,'kind':'manual-publishing-pack','mode':'draft' if draft else 'reviewed','reviewed':reviewed,
              'post':post['id'],'rev':post['rev'],'variant':variant.get('id'),'platform':variant['platform'],
              'account':variant.get('account'),'created':now.isoformat(),'published':False,
              'caption':composed.text,'first_comment':composed.first_comment,'credits':cov['attributions'],
              'warnings':list(dict.fromkeys(problems)),'media':[],
              'notice':'DRAFT — NOT REVIEWED. Do not publish until you review media, copy and licensing.' if draft else
                       'Reviewed manual handoff. Downloading does not publish or record a post as published.'}
    if reviewed:
        from ..publish.receipts import payload_hash
        manifest['reviewed_snapshot']=copy.deepcopy(item)
        manifest['reviewed_payload_hash']=payload_hash(item)
    payload_bytes=0
    for i,m in enumerate(media):
        rec=assets[m['asset']];path=_asset_path(sp,rec)
        payload_bytes+=path.stat().st_size
        if payload_bytes>MAX_TOTAL:
            raise ValueError('manual export payload exceeds the aggregate size limit')
        dimensions={}
        if rec.get('kind')=='image':
            from PIL import Image
            with Image.open(path) as image: dimensions={'width':image.width,'height':image.height}
        name=f'{i+1:02d}-{rec["id"]}{path.suffix.lower()}'
        manifest['media'].append({'position':i+1,'filename':name,'asset':rec['id'],'kind':rec['kind'],
                'sha256':rec['sha256'],'bytes':path.stat().st_size,'alt':m.get('alt') or rec.get('alt') or '',**dimensions})
    manifest['cover']=None
    manifest['delivery']=variant.get('delivery') or {'method':'automatic'}
    manifest['presentation']=variant.get('presentation') or {}
    if cover:
        rec=assets[cover['asset']];path=_asset_path(sp,rec)
        if payload_bytes+path.stat().st_size>MAX_TOTAL: raise ValueError('manual export payload exceeds the aggregate size limit')
        manifest['cover']={'filename':'cover'+path.suffix.lower(),'asset':rec['id'],'kind':'image','sha256':rec['sha256'],'bytes':path.stat().st_size,'alt':cover.get('alt') or rec.get('alt') or ''}
    return post,assets,manifest


def _when(now):
    result=now or datetime.now(timezone.utc)
    if not isinstance(result,datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('manual export time must include a timezone')
    return result


def describe(sp,post_id: str,variant_id: str | None=None,*,draft=False,now: datetime | None=None) -> dict:
    """Read-only readiness and exact media/copy manifest. No package is written."""
    if not isinstance(draft,bool): raise ValueError('draft must be a boolean')
    _,_,manifest=_prepare(sp,post_id,variant_id,draft,_when(now))
    manifest.pop('reviewed_snapshot',None)
    return {'ok':bool(draft) or manifest['reviewed'],'mode':manifest['mode'],'manifest':manifest,'warnings':manifest['warnings']}


def _offline(manifest):
    esc=lambda s:html.escape(str(s or ''),quote=True)
    items=[]
    for m in manifest['media']:
        n=esc(m['filename']);alt=esc(m['alt'])
        preview=f'<img src="{n}" alt="{alt}">' if m['kind']=='image' else f'<video controls src="{n}" preload="metadata"></video>' if m['kind']=='video' else ''
        items.append(f'<article>{preview}<a download href="{n}">Save original {m["position"]}</a><p>{alt}</p></article>')
    warnings=''.join('<li>'+esc(w)+'</li>' for w in manifest['warnings'])
    cover=manifest.get('cover')
    sidecar=(f'<h2>Separate cover</h2><p>Choose this as the cover in the publishing app. Do not add it as a content slide.</p><article><img src="{esc(cover["filename"])}" alt="{esc(cover["alt"])}"><a download href="{esc(cover["filename"])}">Save cover</a></article>') if cover else ''
    feed=(manifest.get('presentation') or {}).get('feed_presence')
    instructions='' if feed is None else '<p>Reel feed presence: '+('show in profile feed' if feed else 'keep off the profile feed')+'. Apply this choice in the publishing app.</p>'
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; media-src 'self' file:; style-src 'unsafe-inline'; script-src 'none'; base-uri 'none'; form-action 'none'">
<title>Manual publishing pack</title><style>body{{font:16px/1.55 system-ui,sans-serif;margin:auto;padding:24px;max-width:720px;color:#202330;background:#f7f8fc}}h1{{font-size:26px}}article{{background:white;padding:12px;margin:16px 0;border-radius:12px}}img,video{{display:block;max-width:100%;height:auto;margin-bottom:12px}}textarea{{width:100%;box-sizing:border-box;min-height:180px;font:inherit;padding:12px}}a{{color:#5143ad}}.notice{{padding:16px;background:#fff1cc;border:1px solid #8f6400;border-radius:10px}}li{{overflow-wrap:anywhere}}</style>
<h1>{'Draft' if not manifest['reviewed'] else 'Reviewed'} manual publishing pack</h1><p class="notice">{esc(manifest['notice'])}</p>
<p>Open the extracted folder on your phone. Save each original in order, then copy the caption into {esc(manifest["platform"].replace("_"," ").title())} yourself. Your device may require Files → Share → Save Image or Save Video. This page does not connect to the platform or post anything.</p>
<h2>Caption</h2><textarea readonly aria-label="Caption">{esc(manifest['caption'])}</textarea><p><a download href="caption.txt">Save caption text</a></p>
<h2>First comment</h2><textarea readonly aria-label="First comment">{esc(manifest.get('first_comment'))}</textarea>{instructions}<h2>Media in posting order</h2>{''.join(items)}{sidecar}
<h2>Alt text</h2><p><a download href="alt-text.txt">Save alt text</a></p><ul>{warnings}</ul><p><a href="manifest.json">File hashes and review information</a></p></html>'''


def build(sp,post_id: str,variant_id: str | None=None,*,output: str | Path | None=None,draft=False,
          expected_rev: int | None=None,now: datetime | None=None) -> dict:
    """Create a new immutable directory plus ZIP; never update publishing state.

    UI callers use output=None. package_id is exactly manual-<32 lowercase hex>;
    ZIP is <work>/manual_exports/<package_id>.zip, directory alongside it.
    """
    if not isinstance(draft,bool): raise ValueError('draft must be a boolean')
    if expected_rev is not None and (not isinstance(expected_rev,int) or isinstance(expected_rev,bool) or expected_rev<1):
        raise ValueError('expected_rev must be a positive revision')
    when=_when(now)
    post,assets,manifest=_prepare(sp,post_id,variant_id,draft,when)
    if expected_rev is not None and post['rev']!=expected_rev: raise model.RevConflict(post)
    if not draft and not manifest['reviewed']:
        raise ValueError('not ready for reviewed export: '+'; '.join(manifest['warnings']))
    historical=manifest.pop('reviewed_snapshot',None)
    parent=_safe(Path(output).expanduser() if output is not None else sp.work/'manual_exports')
    for raw in _output_raw_roots(sp,parent):
        if parent==raw or raw in parent.parents: raise ValueError('manual export never writes into raw folders')
    if parent==sp.root or any(parent==d or d in parent.parents for d in (sp.posts_dir,sp.ledger_dir,sp.accounts_dir,sp.publish,sp.library_dir)):
        raise ValueError('choose a dedicated export directory, not operational studio storage')
    parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    package_id='manual-'+uuid.uuid4().hex
    target=parent/package_id;zip_path=parent/(package_id+'.zip')
    if target.exists() or zip_path.exists(): raise ValueError('package exists; never overwrite an export')
    temp=Path(tempfile.mkdtemp(prefix='.manual-',dir=parent));archive=temp.with_suffix('.zip')
    try:
        manifest['package_id']=package_id
        copied_total=0
        for m in manifest['media']+([manifest['cover']] if manifest.get('cover') else []):
            src=_asset_path(sp,assets[m['asset']]);dest=temp/m['filename']
            with src.open('rb') as inp,dest.open('xb') as out:
                h=hashlib.sha256();copied=0
                while chunk:=inp.read(1024*1024):
                    copied+=len(chunk)
                    copied_total+=len(chunk)
                    if copied_total>MAX_TOTAL: raise ValueError('media payload grew beyond aggregate export limit')
                    if copied>MAX_FILE: raise ValueError('media grew beyond export limit')
                    out.write(chunk);h.update(chunk)
                out.flush();os.fsync(out.fileno())
            if h.hexdigest()!=m['sha256'] or copied!=m['bytes']: raise ValueError('media changed during export; no package was finalized')
        (temp/'caption.txt').write_text(manifest['caption'],encoding='utf-8')
        (temp/'first-comment.txt').write_text(manifest['first_comment'] or '',encoding='utf-8')
        (temp/'alt-text.txt').write_text('\n\n'.join(f'{m["position"]}. {m["alt"]}' for m in manifest['media']),encoding='utf-8')
        (temp/'credits.txt').write_text('\n'.join(str(x) for x in manifest['credits']),encoding='utf-8')
        manifest['native_instructions']=[f"Publish to {manifest['platform']} using account {manifest.get('account') or 'your chosen account'}.",
            'Add music, people/product tags and location in the native app if desired; disclose those changes when recording publication.',
            'Add the separate first-comment text after publishing when supported.',
            'Apply each slide alt text in the native app; automatic API support differs by platform.']
        if manifest.get('cover'): manifest['native_instructions'].append('Choose cover as a sidecar; do not add it as a content slide.')
        (temp/'instructions.txt').write_text('\n'.join(manifest['native_instructions']),encoding='utf-8')
        from . import seal
        manifest['package_signature']=seal.mac('manual-package-v1',safety.canonical(manifest))
        (temp/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        (temp/'index.html').write_text(_offline(manifest),encoding='utf-8')
        manifest.pop('package_signature',None)
        manifest['content_sha256']={f.name:safety.sha256_file(f) for f in temp.iterdir() if f.is_file() and f.name!='manifest.json'}
        manifest['package_signature']=seal.mac('manual-package-v1',safety.canonical(manifest))
        (temp/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        (temp/'checksums.sha256').write_text(''.join(safety.sha256_file(f)+'  '+f.name+'\n' for f in sorted(temp.iterdir()) if f.is_file()),encoding='utf-8')
        # Metadata/revision is checked again after copying; do not ship a stale
        # reviewed post when another editor changed it during a long transfer.
        latest=json.loads(_read(sp.posts_dir/(post_id+'.json'),sp.root))
        if latest.get('rev')!=post['rev']: raise model.RevConflict(latest)
        if not draft:
            _,_,current_manifest=_prepare(sp,post_id,variant_id,False,when)
            if not current_manifest['reviewed']:
                raise ValueError('review changed during export; review the post again')
        with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_STORED) as z:
            for file in sorted(temp.iterdir()): z.write(file,arcname=file.name)
        # Historical approval stays in local ledger attachments, never in the
        # portable ZIP or general API manifest. This MAC is integrity, not authority.
        if historical is not None:
            review={'package_id':package_id,'reviewed_payload_hash':manifest['reviewed_payload_hash'],
                    'post':manifest['post'],'variant':manifest['variant'],'snapshot':historical}
            review['signature']=seal.mac('manual-review-snapshot-v1',safety.canonical(review))
            reviews=_safe(sp.ledger_dir/'manual_reviews',sp.root);reviews.mkdir(mode=0o700,exist_ok=True)
            review_path=_safe(reviews/(package_id+'.json'),sp.root)
            from ..episode import write_json
            write_json(review_path,review);review_path.chmod(0o600)
            with review_path.open('rb') as saved: os.fsync(saved.fileno())
        # ExFAT has no hardlinks. Serialize cooperating exporters with a plain
        # file lock, recheck both names, then rename the completed archive. The
        # ZIP is never visible as a partially copied file. A folder interrupted
        # before archive publication retains an explicit .incomplete marker.
        archive.chmod(0o600)
        with archive.open('rb') as complete:
            os.fsync(complete.fileno())
        lock_path=_safe(parent/'.manual-export.lock')
        fd=os.open(lock_path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'a+') as held:
            fcntl.flock(held,fcntl.LOCK_EX)
            _safe(parent);_safe(target);_safe(zip_path)
            if target.exists() or zip_path.exists():
                raise ValueError('package exists; never overwrite an export')
            target.mkdir(mode=0o700)
            marker=target/'.incomplete'
            marker.write_text('Interrupted exports are not ready until their complete ZIP exists.\n')
            published=False
            try:
                for file in temp.iterdir(): os.replace(file,target/file.name)
                _publish_archive(archive,zip_path)
                published=True
                marker.unlink()
            except BaseException:
                shutil.rmtree(target,ignore_errors=True)
                if published:zip_path.unlink(missing_ok=True)
                raise
        return {'ok':True,'mode':manifest['mode'],'package_id':package_id,'directory':str(target),'zip':str(zip_path),
                'manifest':manifest,'warnings':manifest['warnings'],'published':False}
    finally:
        shutil.rmtree(temp,ignore_errors=True);archive.unlink(missing_ok=True)



def _output_raw_roots(sp, parent: Path) -> set[Path]:
    """Honor markers above a caller-selected output, including sibling episodes.

    Only bounded marker files are read; no workspace/drive scan and no footage
    directory needs to exist for the guard to recognize its configured path.
    """
    from ..episode import resolve_layout
    roots={p.resolve() for p in sp.raw_dirs}
    ancestors=(parent,*parent.parents)
    if len(ancestors)>64:
        raise ValueError('output path has too many ancestors to inspect safely')
    for ancestor in ancestors:
        for marker_name in ('episode.json','studio.json'):
            marker=ancestor/marker_name
            if not marker.exists() and not marker.is_symlink(): continue
            raw=_read(marker,ancestor)
            if len(raw)>262144:
                raise ValueError('output project marker is too large to determine raw folders safely')
            try:
                meta=json.loads(raw)
                if not isinstance(meta,dict): raise ValueError('marker is not an object')
                layout=resolve_layout(meta.get('layout'))
                configured=layout['raw_dirs']
                if not isinstance(configured,list) or len(configured)>128:
                    raise ValueError('too many raw folders')
                for entry in configured:
                    if not isinstance(entry,str) or not entry or len(entry)>2000:
                        raise ValueError('invalid raw folder')
                    roots.add((ancestor/entry).resolve())
            except (ValueError,TypeError,SystemExit,RecursionError):
                raise ValueError('cannot determine raw folders from the output project marker') from None
    return roots

def _publish_archive(archive: Path, destination: Path) -> None:
    # Caller holds the export-parent lock and has refused existing destinations.
    # Both paths are on the same filesystem; rename publishes complete bytes.
    os.rename(archive,destination)


def load_package(sp, package_id, *, cancelled=lambda:False, progress=lambda n,total:None):
    """Read only server-named packages, verify the signed manifest and every file."""
    from . import seal
    def stream_hash(stream,total):
        h=hashlib.sha256();n=0
        if cancelled(): raise ValueError('Package verification cancelled')
        while block:=stream.read(1024**2):
            if cancelled(): raise ValueError('Package verification cancelled')
            h.update(block);n+=len(block);progress(n,total)
        if cancelled(): raise ValueError('Package verification cancelled')
        return h.hexdigest()
    def file_hash(path):
        with path.open('rb') as stream:return stream_hash(stream,path.stat().st_size)
    if cancelled(): raise ValueError('Package verification cancelled')
    if not isinstance(package_id,str) or not PACKAGE_ID.fullmatch(package_id): raise ValueError('Invalid package id')
    root=_safe(sp.work/'manual_exports'/package_id,sp.root)
    if (root/'.incomplete').exists(): raise ValueError('Package is incomplete')
    manifest=json.loads(_read(root/'manifest.json',sp.root))
    signature=manifest.pop('package_signature',None)
    if not seal.verify_mac(signature,'manual-package-v1',safety.canonical(manifest)): raise ValueError('Package manifest signature is invalid')
    manifest['package_signature']=signature
    if manifest.get('package_id')!=package_id: raise ValueError('Package identity mismatch')
    for entry in manifest['media']+([manifest['cover']] if manifest.get('cover') else []):
        path=_safe(root/entry['filename'],root)
        if not path.is_file() or file_hash(path)!=entry['sha256']: raise ValueError('Package media changed')
    for name,sha in manifest.get('content_sha256',{}).items():
        path=_safe(root/name,root)
        if not path.is_file() or file_hash(path)!=sha: raise ValueError('Signed package contents changed')
    archive=_safe(root.with_suffix('.zip'),sp.root)
    try:
        with zipfile.ZipFile(archive) as z:
            for name,sha in {**manifest.get('content_sha256',{}),'manifest.json':file_hash(root/'manifest.json')}.items():
                with z.open(name) as stream:
                    if stream_hash(stream,z.getinfo(name).file_size)!=sha: raise ValueError('Package ZIP contents changed')
    except (zipfile.BadZipFile,KeyError): raise ValueError('Package ZIP is invalid') from None
    for line in _read(root/'checksums.sha256',sp.root).decode().splitlines():
        sha,name=line.split('  ',1); path=_safe(root/name,root)
        if not path.is_file() or file_hash(path)!=sha: raise ValueError('Package contents changed')
    return manifest



def load_review_snapshot(sp,manifest):
    """Protected local attachment, loaded only for core reconciliation."""
    from . import seal
    package_id=manifest['package_id']
    if not PACKAGE_ID.fullmatch(package_id): raise ValueError('Invalid package id')
    path=_safe(sp.ledger_dir/'manual_reviews'/(package_id+'.json'),sp.root)
    review=json.loads(_read(path,sp.root));signature=review.pop('signature',None)
    if not seal.verify_mac(signature,'manual-review-snapshot-v1',safety.canonical(review)):
        raise ValueError('Historical review attachment signature is invalid')
    for key in ('package_id','reviewed_payload_hash','post','variant'):
        if review[key]!=manifest[key]: raise ValueError('Historical review attachment does not match package')
    from ..publish.receipts import payload_hash
    if payload_hash(review['snapshot'])!=manifest['reviewed_payload_hash']: raise ValueError('Historical payload hash mismatch')
    return review['snapshot']

def history(sp,post_id,variant_id=None):
    rows=[]
    for path in sorted((sp.work/'manual_exports').glob('manual-*/manifest.json'),reverse=True):
        try:
            m=load_package(sp,path.parent.name)
            if m['post']!=post_id or variant_id is not None and m['variant']!=variant_id: continue
            row={k:v for k,v in m.items() if k not in ('reviewed_snapshot','package_signature')}
            from . import store
            post=store.load_post(sp,post_id)
            v=next((v for v in post['variants'] if v['id']==m['variant']),{})
            result=v.get('result') or {}
            if result.get('package_id')==m['package_id']: row['publication']=result
            rows.append(row)
        except (ValueError,OSError,KeyError): continue
    return rows
