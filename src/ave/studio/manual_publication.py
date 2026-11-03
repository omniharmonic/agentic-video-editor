"""Protected manual reconciliation over the existing queue and sealed ledgers.

Call `record_manual_publication` only from a human boundary. The CLI/native
boundary calls require_human; ordinary HTTP and external agent tools expose no
mutation route. `_record` is the internal transaction, not an authority flag.
Sealed control intents are forward-only recovery evidence, never approvals.
Lock order: scheduler tick -> queue -> post CAS. A package is a historical snapshot.
"""
from __future__ import annotations
import copy
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit
from . import dispatch, manual_export, model, seal, state, store
from .clock import SystemClock, iso_utc, parse_ts
from .events import Actor
from ..publish import queue as pq, receipts, safety


def _url(value):
    if not isinstance(value,str) or not 1<=len(value)<=2048 or any(ord(c)<33 for c in value):
        raise ValueError('Enter a publication URL (up to 2048 characters)')
    p=urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):
        raise ValueError('Use a public HTTPS publication URL without credentials')
    try: ipaddress.ip_address(p.hostname)
    except ValueError: pass
    else: raise ValueError('Use the platform hostname, not an IP address')
    if '.' not in p.hostname or p.hostname.endswith(('.localhost','.local')): raise ValueError('Use a public platform hostname')
    return value


def record_manual_publication(sp, **request):
    """Importable native/terminal protected handler. Never accepts human=true/PIN in a request."""
    from ..humanpresence import require_human
    require_human('record manual publication', 'Use the native desktop publication form')
    return _record(sp, **request)


def _intents(sp):
    problems=seal.verify_all(sp)
    if problems: raise ValueError('Studio ledger integrity: '+'; '.join(problems))
    return [r for r in seal.log(sp,'control').rows() if r.get('op')=='manual_publication_intent']


def _finish(sp, intent):
    """Idempotent forward application while caller holds tick and queue locks."""
    record=intent['result']; pid=intent['post']; vid=intent['variant']
    item=pq.load(sp,intent['queue'])
    existing=item.get('results',{}).get(intent['platform'],{})
    if existing.get('request_id')!=intent['request_id']:
        if existing.get('state') in receipts.LOCKED: raise ValueError('Another delivery operation owns this queue item')
        # Preserve the historical approved payload; current draft text remains
        # in the Post and the receipt explicitly identifies the exported revision.
        item=copy.deepcopy(intent['snapshot'])
        item.setdefault('results',{})[intent['platform']]=record
        item['status']='published'
        pq._event(item,'manual_publication',request_id=intent['request_id'],package_id=intent['package_id'])
        pq.save(sp,item)
    if dispatch.consumption(sp,dispatch.approval_sig(item)) != 'published':
        dispatch.record_consumption(sp,'finished',item,pid,vid,outcome='published',by=intent['by'],now=parse_ts(intent['at']))
    for _ in range(5):
        post=store.load_post(sp,pid); v=model.find_variant(post,vid)
        if (v.get('result') or {}).get('request_id')==intent['request_id']: break
        if v['account']!=intent['account'] or (v.get('queue') or {}).get('id')!=intent['queue']:
            raise ValueError('Destination changed after manual publication intent; recovery requires the original variant')
        v['result']=record
        v['attempts']=[*v.get('attempts',[]),{'at':intent['at'],'outcome':'manual_reported','detail':record['url']}]
        try:
            store.save_post(sp,post,expected_rev=post['rev'],actor=Actor('human',intent['by']),op='manual_publication',variant=vid,
                detail={'package_id':intent['package_id'],'request_id':intent['request_id'],'exported_rev':intent['exported_rev']},now=parse_ts(intent['at']))
            break
        except model.RevConflict: continue
    else: raise ValueError('Post is changing; replay this same manual publication request to recover')
    rows=seal.log(sp,'control').rows()
    if not any(r.get('op')=='manual_publication_complete' and r.get('request_id')==intent['request_id'] for r in rows):
        seal.log(sp,'control').append({'op':'manual_publication_complete','request_id':intent['request_id'],'at':intent['at']})
    return record


def _record(sp, *, post_id, variant_id, package_id, reviewed_payload_hash, posted_url, posted_at,
            expected_rev, request_id, by, changed_in_native='', url_mismatch_explanation='', now=None):
    from ..schedule.tick import tick_lock
    now=now or SystemClock().now()
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',request_id): raise ValueError('A stable request id is required')
    if not isinstance(by,str) or not by.strip() or len(by)>120: raise ValueError('The reporting person is required')
    if not isinstance(changed_in_native,str) or len(changed_in_native)>2000: raise ValueError('Native changes must be a short text disclosure')
    if not isinstance(url_mismatch_explanation,str) or len(url_mismatch_explanation)>500: raise ValueError('URL explanation must be under 500 characters')
    posted_url=_url(posted_url); when=parse_ts(posted_at)
    if when>now: raise ValueError('Publication time cannot be in the future')
    payload={'post':post_id,'variant':variant_id,'package_id':package_id,'reviewed_payload_hash':reviewed_payload_hash,
             'url':posted_url,'posted_at':iso_utc(when),'expected_rev':expected_rev,'by':by,'changed_in_native':changed_in_native,'url_mismatch_explanation':url_mismatch_explanation}
    digest=hashlib.sha256(safety.canonical(payload).encode()).hexdigest()
    with tick_lock(sp) as got:
        if not got: raise ValueError('The sender is active; retry this report when it finishes')
        prior=next((r for r in _intents(sp) if r['request_id']==request_id),None)
        if prior:
            if prior['request_digest']!=digest: raise ValueError('This request id was already used for a different report')
            with pq._lock(sp,prior['queue']): return _finish(sp,prior)
        manifest=manual_export.load_package(sp,package_id)
        if not manifest.get('reviewed'): raise ValueError('Only a reviewed package can be reported as published')
        if (manifest['post'],manifest['variant'],manifest['reviewed_payload_hash'])!=(post_id,variant_id,reviewed_payload_hash):
            raise ValueError('The report does not match this reviewed package')
        if when<parse_ts(manifest['created']): raise ValueError('Publication time must be after this package was exported')
        snapshot=manual_export.load_review_snapshot(sp,manifest); qid=snapshot['id']
        platform=snapshot['platforms'][0]
        domains={'instagram':['instagram.com'],'facebook':['facebook.com','fb.watch'],'threads':['threads.com','threads.net'],'linkedin':['linkedin.com'],'tiktok':['tiktok.com'],'pinterest':['pinterest.com','pin.it'],'x':['x.com','twitter.com'],'bluesky':['bsky.app'],'youtube':['youtube.com','youtu.be']}.get(platform,[])
        hostname=urlsplit(posted_url).hostname.lower()
        if domains and not any(hostname==d or hostname.endswith('.'+d) for d in domains) and not url_mismatch_explanation.strip():
            raise ValueError('This URL is outside the selected platform; explain the alternate publication link')
        # Expiry is evaluated at reported publication, never at report time.
        check=copy.deepcopy(snapshot); check['status']='approved'
        problems=safety.approval_problems(check,check['sha256'],when.timestamp())
        from . import policy
        if policy.approval_expired(check,when,policy.approval_ttl_days(sp,manifest['account'])): problems.append('Approval had expired at the reported publication time')
        signed_ttl=(check.get('approvals') or [{}])[-1].get('bind',{}).get('ttl',{})
        for days in signed_ttl.values():
            if policy.approval_expired(check,when,days): problems.append('Signed approval lifetime had expired at publication')
        if problems: raise ValueError('; '.join(problems))
        with pq._lock(sp,qid):
            post=store.load_post(sp,post_id)
            if post['rev']!=expected_rev: raise model.RevConflict(post)
            v=model.find_variant(post,variant_id)
            if v['account']!=manifest['account'] or (v.get('queue') or {}).get('id')!=qid: raise ValueError('Package destination does not match this variant')
            item=pq.load(sp,qid)
            if any(r.get('state') in receipts.LOCKED for r in item.get('results',{}).values()): raise ValueError('Another delivery operation already owns this variant')
            if dispatch.consumption(sp,dispatch.approval_sig(snapshot))!='free': raise ValueError('This approval has already been used or has an unresolved send')
            if any(r['post']==post_id and r['variant']==variant_id for r in _intents(sp)): raise ValueError('This variant already has a manual publication intent')
            r=receipts.receipt(provider=v['platform'],account=v['account'],phase='published',url=posted_url,source='human_report',certainty='reported',published_at=iso_utc(when))
            r['reviewed_payload_hash']=reviewed_payload_hash
            result={'state':'published','receipt':r,'url':posted_url,'at':iso_utc(now),'posted_at':iso_utc(when),'by':by,
                    'package_id':package_id,'request_id':request_id,'exported_rev':manifest['rev'],'changed_in_native':changed_in_native,'url_mismatch_explanation':url_mismatch_explanation}
            intent={'op':'manual_publication_intent','request_id':request_id,'request_digest':digest,'post':post_id,'variant':variant_id,
                    'queue':qid,'account':v['account'],'platform':v['platform'],'package_id':package_id,'exported_rev':manifest['rev'],
                    'reviewed_payload_hash':reviewed_payload_hash,'snapshot':snapshot,'result':result,'by':by,'at':iso_utc(now)}
            seal.log(sp,'control').append(intent)  # durable before any queue/post/consumption write
            return _finish(sp,intent)


def recover(sp):
    """Forward-only replay of already protected, sealed intents; grants no new authority."""
    from ..schedule.tick import tick_lock
    with tick_lock(sp) as got:
        if not got: return []
        out=[]
        for intent in _intents(sp):
            with pq._lock(sp,intent['queue']): out.append(_finish(sp,intent))
        return out


def recover_locked(sp):
    """Scheduler already holds its operation lock; finish signed intents first."""
    out=[]
    for intent in _intents(sp):
        with pq._lock(sp,intent['queue']): out.append(_finish(sp,intent))
    return out
