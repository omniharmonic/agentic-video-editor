"""Versioned delivery evidence. An accepted operation is not a publication.

The queue remains authoritative; receipts are embedded in its per-provider result.
No migration rewrites legacy approvals or fabricates evidence for old results.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from . import safety

PHASES = frozenset({'published', 'processing', 'scheduled', 'awaiting_user_publish', 'sandbox', 'draft', 'failed', 'uncertain'})
LOCKED = frozenset({'sending', 'published', 'processing', 'scheduled', 'awaiting_user_publish', 'sandbox', 'draft', 'uncertain'})
PENDING = LOCKED - {'published', 'sending', 'uncertain', 'sandbox', 'draft'}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def payload_hash(item):
    return hashlib.sha256(json.dumps({'media': item.get('sha256'), 'metadata': safety.meta_hash(item)}, sort_keys=True).encode()).hexdigest()


def receipt(*, provider, account=None, phase, object_id=None, object_kind='post', publication_id=None, url=None,
            source='provider_response', certainty='confirmed', accepted_at=None, published_at=None, diagnostics=None, adjuncts=None):
    if phase not in PHASES: raise ValueError('invalid delivery phase')
    if not isinstance(provider,str) or not provider or len(provider)>80: raise ValueError('invalid provider')
    if account is not None and (not isinstance(account,str) or len(account)>100): raise ValueError('invalid account')
    if not isinstance(object_kind,str) or not object_kind or len(object_kind)>80: raise ValueError('invalid object kind')
    for value in (diagnostics,adjuncts):
        if value is not None and (not isinstance(value,dict) or len(json.dumps(value))>32768): raise ValueError('invalid delivery diagnostics or adjuncts')
    for value in (accepted_at,published_at):
        if value is not None:
            dt=datetime.fromisoformat(value.replace('Z','+00:00'))
            if dt.tzinfo is None: raise ValueError('delivery timestamps require a timezone')
    if source not in {'provider_response','provider_status','human_report','legacy_unknown'}: raise ValueError('invalid evidence source')
    if certainty not in {'confirmed','unknown','reported'}: raise ValueError('invalid certainty')
    if phase=='published' and ((source=='human_report') != (certainty=='reported') or certainty=='unknown'): raise ValueError('published evidence certainty mismatch')
    if phase != 'published' and (publication_id or url or published_at): raise ValueError('unpublished operation cannot carry publication identity')
    if phase == 'published' and not (publication_id or (source == 'human_report' and url)):
        raise ValueError('published receipt requires publication identity')
    return safety.redact_obj({'v':1,'phase':phase,'certainty':certainty,'source':source,'provider':provider,'account':account,
        'object':{'kind':object_kind,'id':str(object_id) if object_id is not None else None},
        'publication':{'id':str(publication_id) if publication_id is not None else None,'url':url,'at':published_at},
        'accepted_at':accepted_at, 'diagnostics':diagnostics or {}, 'adjuncts':adjuncts or {}})


def normalize(result, item, provider):
    """Unknown dictionaries remain uncertain. Only a validated v1 receipt claims success."""
    result = result if isinstance(result,dict) else {}
    r=result.get('receipt')
    if not isinstance(r,dict) or r.get('v')!=1:
        r=receipt(provider=provider,account=item.get('account'),phase='uncertain',certainty='unknown',source='legacy_unknown')
    else:
        if r.get('provider')!=provider or r.get('account') not in (None,item.get('account')): raise ValueError('receipt destination mismatch')
        if r.get('reviewed_payload_hash') not in (None,payload_hash(item)): raise ValueError('receipt payload mismatch')
        obj=r.get('object') or {}; pub=r.get('publication') or {}
        r=receipt(provider=provider,account=item.get('account'),phase=r.get('phase'),object_id=obj.get('id'),object_kind=obj.get('kind','post'),
                  publication_id=pub.get('id'),url=pub.get('url'),source=r.get('source'),certainty=r.get('certainty'),
                  accepted_at=r.get('accepted_at'),published_at=pub.get('at'),diagnostics=r.get('diagnostics'),adjuncts=r.get('adjuncts'))
    r['reviewed_payload_hash']=payload_hash(item)
    phase=r['phase']
    return {**safety.redact_obj(result),'receipt':r,'state':'sending' if phase=='uncertain' else phase,
            'id':r['publication']['id'],'url':r['publication']['url'],'at':timestamp()}


def result(provider, id_=None, url=None, *, account=None, phase='published', object_kind='post', **extra):
    if phase=='published' and not id_: phase='uncertain'
    r=receipt(provider=provider,account=account,phase=phase,object_id=id_,object_kind=object_kind,
              publication_id=id_ if phase=='published' else None,url=url if phase=='published' else None,
              certainty='unknown' if phase=='uncertain' else 'confirmed',accepted_at=timestamp(),
              published_at=timestamp() if phase=='published' else None, diagnostics=extra.pop('diagnostics',None), adjuncts=extra.pop('adjuncts',None))
    return {'platform':provider,'account':account,'id':id_ if phase=='published' else None,'url':url if phase=='published' else None,'receipt':r,**extra}


def error(exc):
    kind={'AuthError':'auth','QuotaError':'quota'}.get(type(exc).__name__)
    return {'kind':kind,'type':type(exc).__name__,'message':safety.redact(exc),'retry_after':getattr(exc,'retry_after',None)}


def aggregate(item):
    states=[item.get('results',{}).get(p,{}).get('state') for p in item['platforms']]
    if all(s=='published' for s in states): return 'published'
    if any(s in LOCKED-{'published'} for s in states): return 'publishing'
    return 'failed'


def expired_leases(record, now=None):
    """Bounded host expiry does not prove absence and must never unlock creation."""
    now=now or datetime.now(timezone.utc)
    expired=[]
    for lease in record.get('host_leases',[]):
        try:
            at=datetime.fromisoformat(lease['expires'].replace('Z','+00:00'))
            if at<=now: expired.append(lease['key'])
        except (KeyError,AttributeError,TypeError,ValueError): continue
    return expired
