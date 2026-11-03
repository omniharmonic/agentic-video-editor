"""Pure capability intersection and status-only refresh. Never approves or creates a post."""
from . import dispatch, model, policy, state, store
from .events import AGENT
from ..publish import queue as pq

CONTRACTS={
 'instagram':{'version':'Graph v21.0 (supported through 2027-01-21)','shapes':['image','carousel','video'],'alt':'Feed images only, 1000 characters','cover':'Reel JPEG cover_url; preview crop is not delivered','feed':'Reel share_to_feed','comment':'Optional separate comment operation','host':True},
 'facebook':{'version':'Graph v21.0','shapes':['image','carousel','video'],'alt':'Photo alt_text_custom','cover':'manual','feed':'Provider surface rules','comment':'manual','host':False},
 'threads':{'version':'Threads v1.0','shapes':['text','image','carousel'],'alt':'Each image','cover':'manual','feed':'Native feed','comment':'Separate reply post','host':True},
 'linkedin':{'version':'202610','shapes':['text','image','carousel','document'],'alt':'Image altText','cover':'manual','feed':'Public or member connections; private refused','comment':'manual','host':False},
 'tiktok':{'version':'Content Posting v2; inbox only','shapes':['video','image','carousel'],'alt':'manual','cover':'manual','feed':'User publishes in app; photo upload requires verified host domain','comment':'manual','host':False},
 'pinterest':{'version':'v5 OpenAPI 5.28.0','shapes':['image','carousel'],'alt':'Top-level only; per-slide alt is a manual step','cover':'manual','feed':'Trial is sandbox only','comment':'manual','host':False},
 'x':{'version':'API v2','shapes':['text','image','carousel'],'alt':'Each uploaded image','cover':'manual','feed':'Native post','comment':'manual','host':False},
 'bluesky':{'version':'AT Protocol createRecord','shapes':['text','image','carousel'],'alt':'Each embed image','cover':'manual','feed':'Native post','comment':'manual','host':False},
 'youtube':{'version':'Data API v3','shapes':['video'],'alt':'manual','cover':'manual','feed':'Video visibility and processing status','comment':'manual','host':False},
}


def capabilities(studio,post_id,variant_id):
    from ..platforms.accounts import load_account
    from ..schedule.tick import heartbeat_path
    from .clock import parse_ts
    import json
    try:
        hb=json.loads(heartbeat_path(studio.sp).read_text());age=(studio.now()-parse_ts(hb["at"])).total_seconds()
        worker={"alive":0<=age<=180,"age_s":age,"reason":"Ready" if 0<=age<=180 else "Heartbeat stale; worker may be offline"}
    except (OSError,ValueError,KeyError): worker={"alive":False,"reason":"Worker has not reported a heartbeat"}
    post=store.load_post(studio.sp,post_id);v=model.find_variant(post,variant_id)
    account=load_account(studio.sp,v['account']);contract=CONTRACTS.get(v['platform'],{'version':'Manual only','shapes':[]})
    contract=dict(contract)
    if v['platform']=='tiktok': contract['host']='Verified public domain for photos; video uses direct upload'
    contract.update(documentation_checked='2026-10-29',synthetic_verified=True,live_verified=False,
        scheduling='Provider scheduling or local worker' if v['platform'] in ('facebook','youtube') else 'Local worker only',
        links='Included in reviewed caption; clickability depends on provider surface',
        native_only=['Music','People/product tags','Location'],
        required_scopes={'instagram':['instagram_content_publish (Facebook Login) or instagram_business_content_publish (Instagram Login)'],
          'facebook':['pages_manage_posts','Page content task'], 'threads':['threads_basic','threads_content_publish'],
          'linkedin':['w_member_social or w_organization_social for matching author','Images: compatible rw_ads / w_organization_social / w_power_creators grant for versioned readiness GET'],
          'tiktok':['video.upload','Photo: verified media-host domain'], 'pinterest':['pins:write','Board access'],
          'x':['tweet.write','users.read','media.write for images'], 'bluesky':['Authenticated repository write'],
          'youtube':['youtube.upload']}.get(v['platform'],[]))
    connection=account.connection
    from ..platforms.registry import delivery_contract,adapter_for
    native = bool(getattr(studio, 'native_authoring', False))
    if native:
        # Even adapter preflight may fall back from a public ID to Keychain.
        # Ordinary native readiness never constructs or calls an adapter.
        selected = None
        implemented = {'adapter':'Unverified','shapes':[], 'first_comment':None,
                       'cover':None,'feed_presence':None,'checked':False}
    else:
        selected=dispatch.queue_publishers(studio.publishers,v).get(v['platform'])
        if selected is None: selected=adapter_for(account,host=studio.host,now=studio.clock.now)
        implemented=delivery_contract(selected)
    from .sync import _spec
    spec,problems=_spec(studio.sp,post,v,store.read_assets(studio.sp))
    method=(v.get('delivery') or {}).get('method','automatic')
    if native and method=='automatic': problems.append('Automatic delivery readiness is unverified; use protected Connections setup when available.')
    elif spec and method=='automatic': problems+=pq.preflight_item(studio.sp,{**spec,'video':str(spec.get('video') or ''),'files':[str(p) for p in spec.get('files',[])]},{v['platform']:selected})
    return {'method':method,'provider_contract':contract,'implemented_shapes':implemented['shapes'],'selected_adapter':{**implemented,'mode':account.mode},
        'pending_shapes':['mixed image/video carousel']+(['video'] if v['platform'] in ('threads','linkedin','x','bluesky','pinterest') else []),
        'destination_evidence':{'account':account.id,'product':connection.get('product'),'destination':connection.get('destination_id'),'scopes':connection.get('scopes',[]),'source':connection.get('source','unknown'),'verified':False,'note':'Stored connection metadata is not provider permission proof or a live attestation'},
        'credential_evidence':{'checked':False,'expires_at':connection.get('expires_at'),'note':'Credential presence and provider-granted permissions are checked during protected connection setup; this view does not access secrets.'},
        'worker':worker,'live_verification':{'verified':False,'note':'No live account/post-type certification recorded by this task'},
        'manual_steps':(['TikTok inbox video caption is entered in the app; copy the reviewed caption from the manual package'] if v['platform']=='tiktok' else [])+['Native music, people/product tags and locations','First comment where automatic delivery is unsupported','Native framing may differ from the preview'],
        'problems':list(dict.fromkeys(problems)),'manual_available':True}


def refresh(studio,post_id,variant_id,*,expected_operation=None,expected_revision=None,expected_binding=None):
    from ..schedule.tick import tick_lock
    with tick_lock(studio.sp) as got:
        if not got: raise ValueError('Sender is busy; refresh after it finishes')
        p=store.load_post(studio.sp,post_id);v=model.find_variant(p,variant_id)
        if not v.get('queue'): raise ValueError('No delivery operation to refresh')
        ep=state.queue_episode(studio.sp,v)
        before=pq.load(ep,v['queue']['id'])
        if expected_operation is not None:
            from .connections import binding,digest
            from ..platforms.accounts import load_account
            current=binding(load_account(studio.sp,v['account']))
            if p['rev']!=expected_revision or digest(before)!=expected_operation or current!=expected_binding:
                raise ValueError('Operation or connection changed; reopen the post')
            if (before.get('approvals') or [{}])[-1].get('bind',{}).get('connection')!=current:
                raise ValueError('The operation belongs to another or unverified connection revision; inspect its native destination')
        selected=pq.selected_publisher(studio.sp,before,v['platform'],studio.publishers)
        item=pq.refresh(ep,v['queue']['id'],publishers={v['platform']:selected})
        terminal_evidence=dispatch.project_terminal_reviews(ep,studio.sp,item,v['platform'])
        result=item.get('results',{}).get(v['platform'])
        if result:
            # Refresh preserves current copy/media; only projects provider evidence.
            for _ in range(5):
                p=store.load_post(studio.sp,post_id);nv=model.find_variant(p,variant_id);nv['result']=result
                if result.get('state')=='failed' and (not nv.get('attempts') or nv['attempts'][-1].get('outcome')!='provider_failed'):
                    nv['attempts']=[*nv.get('attempts',[]),{'at':studio.now().isoformat(),'outcome':'provider_failed','detail':'Provider rejected the accepted operation. Fresh human review and approval are required; status refresh never retries.'}]
                try: store.save_post(studio.sp,p,expected_rev=p['rev'],actor=AGENT,op='delivery_refresh',variant=variant_id,now=studio.now());break
                except model.RevConflict: continue
            else: raise ValueError('Post kept changing during status refresh; refresh again to project the saved provider result')
        producing_outcomes={e['approval']:'failed' for e in terminal_evidence}
        if result and result.get('state')=='published':
            producing_outcomes[dispatch.approval_sig(item)]='published'
            dispatch.record_consumption(studio.sp,'finished',item,post_id,variant_id,outcome='published',now=studio.now())
        for key,event in dispatch.open_dispatches(studio.sp).items():
            producer=dispatch.latest_dispatch_approval(studio.sp,key)
            if event.get('post')==post_id and event.get('variant')==variant_id and producer in producing_outcomes:
                dispatch.log_dispatch(studio.sp,'dispatch_finished',post_id,variant_id,key,studio.now(),AGENT,outcome=producing_outcomes[producer])
        return {'post':studio.post(post_id) if hasattr(studio,'post') else studio.view(store.load_post(studio.sp,post_id)),'queue':item}
