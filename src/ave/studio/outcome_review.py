"""Native exact-operation human outcome review with forward-only sealed recovery.

This is not provider evidence or approval. A negative report permanently consumes
the producing approval and requires fresh review; it never retries a create.
"""
from __future__ import annotations
import copy
from . import connections as C,dispatch,model,seal,state,store
from .events import Actor
from ..publish import queue as pq,receipts


def finish(sp,intent):
    pid,vid=intent['post'],intent['variant'];item=pq.load(sp,intent['queue']);platform=intent['platform']
    post=store.load_post(sp,pid);variant=model.find_variant(post,vid)
    if (variant.get('delivery') or {}).get('method')=='manual' or (variant.get('queue') or {}).get('id')!=intent['queue'] or variant['account']!=intent['account'] or C.binding(C.load_account(sp,variant['account']))!=intent['binding']:raise ValueError('The original account and automatic operation must be restored before review recovery')
    from ..publish import safety
    original=intent['snapshot']
    if item.get('sha256')!=original.get('sha256') or item.get('approvals')!=original.get('approvals') or safety.meta_hash(item)!=safety.meta_hash(original):raise ValueError('The producing payload or approval changed; review recovery cannot replace a newer operation')
    current=item.get('results',{}).get(platform,{})
    if current.get('outcome_review_id')!=intent['request_id']:
        if C.digest(item)!=intent['snapshot_hash']:raise ValueError('Operation changed after the review intent; preserve both records and inspect the operation')
        item=copy.deepcopy(intent['snapshot']);item.setdefault('results',{})[platform]=intent['result']
        item['status']=receipts.aggregate(item)
        if intent['decision']=='not_published':
            item['delivery_review_required']=True
            dispatch.require_terminal_review(sp,item,platform,intent['result'])
        pq._event(item,'native_outcome_review',request_id=intent['request_id'],decision=intent['decision']);pq.save(sp,item)
    if intent['decision']=='published':
        if dispatch.consumption(sp,dispatch.approval_sig(item))!='published':dispatch.record_consumption(sp,'finished',item,pid,vid,outcome='published',by='native device owner')
    else:dispatch.require_terminal_review(sp,item,platform,intent['result'])
    for _ in range(5):
        post=store.load_post(sp,pid);v=model.find_variant(post,vid)
        if (v.get('result') or {}).get('outcome_review_id')==intent['request_id']:break
        if (v.get('queue') or {}).get('id')!=intent['queue'] or v['account']!=intent['account']:raise ValueError('The original operation must be restored before review recovery')
        v['result']=intent['result'];v['attempts']=[*v.get('attempts',[]),{'at':intent['at'],'outcome':'human_outcome_review','detail':intent['decision']}]
        try:store.save_post(sp,post,expected_rev=post['rev'],actor=Actor('human','native device owner'),op='outcome_review',variant=vid);break
        except model.RevConflict:continue
    else:raise ValueError('Post kept changing; reopen outcome review to recover the saved decision')
    for key,event in dispatch.open_dispatches(sp).items():
        if event.get('post')==pid and event.get('variant')==vid and dispatch.latest_dispatch_approval(sp,key)==dispatch.approval_sig(item):
            from .clock import parse_ts
            dispatch.log_dispatch(sp,'dispatch_resolved',pid,vid,key,parse_ts(intent['at']),Actor('human','native device owner'),outcome=intent['decision'],source='human_report')
    if not any(r.get('op')=='outcome_review_complete' and r.get('request_id')==intent['request_id'] for r in seal.log(sp,'control').rows()):
        seal.log(sp,'control').append({'op':'outcome_review_complete','request_id':intent['request_id'],'at':intent['at']})
    return {'state':intent['result']['state'],'source':'human_report','message':'Your publication report was recorded.' if intent['decision']=='published' else 'The old approval was consumed. Review and approve the payload again before any new attempt.'}


def record(studio,context,values):
    from ..schedule.tick import tick_lock
    from .manual_publication import _url
    from urllib.parse import urlsplit
    from ..desktop_native import external_url
    if set(values)!={'outcome','posted_url'} or values['outcome'] not in ('PUBLISHED','NOT PUBLISHED'):raise ValueError('Type PUBLISHED or NOT PUBLISHED in the native sheet')
    decision='published' if values['outcome']=='PUBLISHED' else 'not_published'
    url=values['posted_url'].strip() if decision=='published' else None
    if url and (not external_url(_url(url)) or urlsplit(url).hostname in ('developers.facebook.com','developers.google.com','www.canva.com')):raise ValueError('Use the social platform publication URL')
    if decision=='published' and not url:raise ValueError('A publication URL is required for a published report')
    sp=studio.sp;a=context['args']
    with tick_lock(sp) as got:
        if not got:raise ValueError('Delivery is busy; retry outcome review after it finishes')
        if seal.verify_all(sp):raise ValueError('Ledger integrity must be restored before outcome review')
        # Recover prior durable decisions first; never reinterpret them as new actions.
        for prior in seal.log(sp,'control').rows():
            if prior.get('op')=='outcome_review_intent' and prior.get('post')==a['post'] and prior.get('variant')==a['variant']:
                completed=any(x.get('op')=='outcome_review_complete' and x.get('request_id')==prior['request_id'] for x in seal.log(sp,'control').rows())
                if not completed:
                    with pq._lock(sp,prior['queue']):return finish(sp,prior)
        p=store.load_post(sp,a['post']);v=model.find_variant(p,a['variant'])
        if p['rev']!=a['revision'] or C.binding(C.load_account(sp,v['account']))!=context['binding']:raise ValueError('Post or destination changed; reopen outcome review')
        if url:
            hosts={'instagram':['instagram.com'],'facebook':['facebook.com'],'threads':['threads.net','threads.com'],'linkedin':['linkedin.com'],'x':['x.com','twitter.com'],'pinterest':['pinterest.com'],'bluesky':['bsky.app'],'youtube':['youtube.com','youtu.be'],'tiktok':['tiktok.com']}.get(v['platform'],[])
            host=urlsplit(url).hostname
            if not any(host==h or host.endswith('.'+h) for h in hosts):raise ValueError('Publication URL must match this operation’s platform')
        if (v.get('delivery') or {}).get('method')=='manual':raise ValueError('Manual packages use their separate publication report')
        ep=state.queue_episode(sp,v)
        if ep.root!=sp.root:raise ValueError('Review the original episode operation in its own workspace')
        with pq._lock(sp,v['queue']['id']):
            q=pq.load(sp,v['queue']['id'])
            if C.digest(q)!=context['operation']:raise ValueError('Operation changed; reopen outcome review')
            current_account=C.load_account(sp,v['account'])
            if current_account.connection.get('root_revision') and (q.get('approvals') or [{}])[-1].get('bind',{}).get('connection')!=context['binding']:raise ValueError('Operation belongs to an earlier connection; restore its matching destination before review')
            old=q.get('results',{}).get(v['platform'],{})
            if old.get('state') not in receipts.LOCKED-{'published'}:raise ValueError('Only an unresolved existing operation can be reviewed')
            request_id='native_'+__import__('secrets').token_hex(16)
            evidence=receipts.receipt(provider=v['platform'],account=v['account'],phase='published' if decision=='published' else 'failed',url=url,source='human_report',certainty='reported',diagnostics={'kind':'human_outcome_review','decision':decision})
            result={**old,**receipts.normalize({'receipt':evidence},q,v['platform']),'outcome_review_id':request_id,'reported_by':'native device owner'}
            intent={'op':'outcome_review_intent','request_id':request_id,'post':p['id'],'variant':v['id'],'queue':q['id'],'account':v['account'],'platform':v['platform'],'at':studio.now().isoformat(),'decision':decision,'snapshot':q,'snapshot_hash':context['operation'],'binding':context['binding'],'result':result}
            seal.log(sp,'control').append(intent)
            return finish(sp,intent)
