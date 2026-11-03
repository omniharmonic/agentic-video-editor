"""Typed authoring operations and the canonical ordered-content/cover projection.

These operations carry no approval authority. Preview framing never changes bytes.
"""
from __future__ import annotations
import copy
import math
import secrets
from . import model, store, state, production


def delivery(v):
    return (v.get('delivery') or {}).get('method', 'automatic')


def projection(post, variant, assets):
    content, covers, problems = [], [], []
    raw=variant.get('media') if variant.get('media') is not None else post.get('media',[])
    if not isinstance(raw,list) or any(not isinstance(m,dict) or type(m.get('slot')) is not int or m['slot']<0 or not isinstance(m.get('asset'),str) or not isinstance(m.get('role'),str) or ('id' in m and not isinstance(m['id'],str)) for m in raw):
        return {'content':[],'cover':None,'problems':['Invalid media item shape'],'inherited':variant.get('media') is None}
    if len(raw)>21:return {'content':[],'cover':None,'problems':['At most 20 content items and one cover'],'inherited':variant.get('media') is None}
    items = sorted(raw, key=lambda m: m.get('slot', 0))
    slots, ids = set(), set()
    for i, original in enumerate(items):
        m = copy.deepcopy(original)
        m.setdefault('id', 'legacy_' + str(i))
        if m['id'] in ids: problems.append('Media item identities must be unique')
        ids.add(m['id'])
        if m.get('slot') in slots: problems.append('Media slots must be unique')
        slots.add(m.get('slot'))
        rec = assets.get(m.get('asset'))
        if rec is None: problems.append('Asset is not in the library registry; choose registered media')
        elif rec.get('kind') != ('image' if m.get('role') == 'cover' else m.get('role')):
            problems.append('Media role does not match the registered asset kind')
        crop = m.get('crop')
        if isinstance(crop,dict) and set(crop)=={'grid_focus'}:
            point=crop['grid_focus']
            if not isinstance(point,list) or len(point)!=2: problems.append('Invalid legacy focal point')
            else:
                crop={'v':1,'x':point[0],'y':point[1],'aspect':'3:4','purpose':'preview'}
                m['crop']=crop
        if crop:
            if not isinstance(crop, dict) or set(crop) != {'v','x','y','aspect','purpose'} or crop.get('v') != 1 or crop.get('purpose') != 'preview' or crop.get('aspect') not in ('original','1:1','3:4','4:5','9:16'):
                problems.append('Preview framing must specify version, focal point, aspect and preview purpose')
            elif any(type(crop[k]) not in (int,float) or not math.isfinite(crop[k]) or not 0 <= crop[k] <= 1 for k in ('x','y')):
                problems.append('Preview focal coordinates must be finite numbers from 0 to 1')
        if m.get('role') == 'cover': covers.append(m)
        else:
            content.append(m)
    if len(content)>20: problems.append('At most 20 content items')
    if len(covers) > 1: problems.append('Choose only one cover')
    return {'content': content, 'cover': covers[0] if covers else None, 'problems': list(dict.fromkeys(problems)),
            'inherited': variant.get('media') is None}


def require_projection(post, variant, assets):
    p = projection(post, variant, assets)
    if p['problems']: raise ValueError('; '.join(p['problems']))
    return p


def editable(studio, post, variant=None):
    variants = [variant] if variant else post.get('variants', [])
    for v in variants:
        item = state.load_queue_item(studio.sp, v)
        if (v.get('result') or {}).get('state') in ('published','sending','publishing','uncertain') or (item and (item.get('status') in ('published','publishing') or any(r.get('state') == 'sending' for r in item.get('results',{}).values()))):
            raise ValueError('Published, sending or uncertain content is locked. Duplicate it for a revision.')


def undo_media(change, current, *, pid, variant, assets):
    """Invert one scoped operation, preserving changes to untouched fields.

    Order is one field; each item's metadata fields are separate. An overlapping
    change is refused rather than replacing another editor's newer work.
    """
    if not isinstance(change,dict): raise ValueError('Invalid media undo')
    production.only(change, {'post','variant','operation','before','after','inherited'})
    if change.get('post') != pid or change.get('variant') != variant:
        raise ValueError('This media undo belongs to another post or channel')
    if change.get('operation') not in ('add','replace','cover','remove','move','alt','crop','reset'):
        raise ValueError('Invalid media undo operation')
    before, after = change.get('before'), change.get('after')
    for items in (before,after):
        require_projection({'media':items},{},assets)
        if any(not m.get('id') for m in items): raise ValueError('Undo requires media identities')
    def order(items): return [m['id'] for m in items]
    def records(items): return {m['id']:{k:v for k,v in m.items() if k!='slot'} for m in items}
    b,a,c = records(before),records(after),records(current)
    merged=copy.deepcopy(c)
    missing=object()
    def conflict(): raise ValueError('Cannot undo: this media changed again. Your current media is preserved.')
    for iid in b.keys() | a.keys():
        if iid not in b:  # Undo addition only while that addition is unchanged.
            if iid in c and c[iid] != a[iid]: conflict()
            merged.pop(iid,None)
        elif iid not in a:
            if iid in c and c[iid] != b[iid]: conflict()
            merged[iid]=copy.deepcopy(b[iid])
        else:
            for key in b[iid].keys() | a[iid].keys():
                old,new=b[iid].get(key,missing),a[iid].get(key,missing)
                if old==new: continue
                if iid not in c: conflict()
                now=c[iid].get(key,missing)
                if now!=new and now!=old: conflict()
                if old is missing: merged[iid].pop(key,None)
                else: merged[iid][key]=copy.deepcopy(old)
    bo,ao,co=order(before),order(after),order(current)
    if bo!=ao:
        if co!=ao and co!=bo: conflict()
        result_order=bo
    else: result_order=co
    return [dict(merged[iid],slot=i) for i,iid in enumerate(result_order)]


def media_action(studio, pid, body, actor, *, _commit_context=None):
    operation_fields = {'add': {'asset'}, 'replace': {'item','asset'}, 'cover': {'asset'},
                        'remove': {'item'}, 'move': {'item','before'}, 'alt': {'item','alt'},
                        'crop': {'item','crop'}, 'reset': set(), 'undo': {'change'}}
    if not isinstance(body.get('op'),str) or body['op'] not in operation_fields: raise ValueError('Unknown media operation')
    production.only(body, {'expected_rev','variant','op'} | operation_fields[body['op']])
    post = store.load_post(studio.sp, pid)
    v = model.find_variant(post, body['variant']) if body.get('variant') else None
    editable(studio, post, v)
    target = v if v is not None else post
    assets = store.read_assets(studio.sp)
    current = projection(post, v or {}, assets)
    items = copy.deepcopy(current['content'] + ([current['cover']] if current['cover'] else []))
    # Resolve legacy identity against the original projection before assigning durable IDs.
    original = current['content'] + ([current['cover']] if current['cover'] else [])
    mapping = {a['id']: b['id'] for a,b in zip(original,items)}
    iid = mapping.get(body.get('item'), body.get('item'))
    before = mapping.get(body.get('before'), body.get('before'))
    op = body.get('op')
    selected = next((m for m in items if m['id'] == iid), None)
    if op == 'reset':
        if v is None: raise ValueError('Only a channel can inherit shared media')
        target['media'] = None
    else:
        if op in ('add','replace','cover'):
            rec = assets.get(body.get('asset'))
            if not rec or rec.get('kind') not in ('image','video','document'): raise ValueError('Choose a registered image, video or PDF')
            if op == 'cover' and rec['kind'] != 'image': raise ValueError('A cover must be an image')
            if op == 'replace':
                if selected is None: raise ValueError('Slide is no longer present')
                selected.update(asset=rec['id'], role='cover' if selected['role']=='cover' else rec['kind'])
            else:
                if op == 'cover': items = [m for m in items if m['role'] != 'cover']
                items.append({'id':'m_'+secrets.token_hex(8),'slot':len(items),'asset':rec['id'],'role':'cover' if op=='cover' else rec['kind'],'alt':rec.get('alt') or ''})
        elif op in ('remove','move','alt','crop'):
            if selected is None: raise ValueError('Slide is no longer present')
            if op == 'remove': items.remove(selected)
            elif op == 'move':
                if selected['role'] == 'cover': raise ValueError('The cover is separate from content order')
                if before == iid: return studio.view(post)
                dest = next((m for m in items if m['id']==before and m['role']!='cover'), None)
                if before is not None and dest is None: raise ValueError('Destination slide is no longer present')
                items.remove(selected); items.insert(items.index(dest) if dest else len(items), selected)
            elif op == 'alt':
                if not isinstance(body.get('alt'), str) or len(body['alt']) > 10000: raise ValueError('Alt text must be at most 10000 characters')
                selected['alt'] = body['alt']
            else: selected['crop'] = body.get('crop')
        elif op == 'undo':
            items = undo_media(body.get('change'),items,pid=pid,variant=v['id'] if v else None,assets=assets)
        else: raise ValueError('Unknown media operation')
        content = [m for m in items if m['role'] != 'cover']; covers = [m for m in items if m['role']=='cover']
        for i,m in enumerate(content+covers): m['slot']=i
        target['media'] = content+covers
        require_projection(post,v or {},assets)
        if op=='undo' and v is not None and body['change'].get('inherited'):
            def logical_items(projected):
                ordered=projected['content']+([projected['cover']] if projected['cover'] else [])
                # Slot numbers encode order, not identity. Legacy gaps must not
                # prevent restoring inheritance; all actual item fields still match.
                return [{k:value for k,value in m.items() if k!='slot'} for m in ordered]
            if logical_items(projection(post,v,assets))==logical_items(projection(post,{},assets)):
                target['media']=None
    for channel in ([v] if v else post.get('variants',[])):
        channel.setdefault('delivery',{'method':'automatic'});channel['payload_version']=3
    saved = store.save_post(studio.sp, post, expected_rev=body['expected_rev'], actor=actor, op='composer.media', detail={'operation':op}, now=studio.now(),_commit_context=_commit_context)
    return studio.view(saved)


def add_variant(studio, pid, body, actor):
    production.only(body, {'expected_rev','account','post_type','delivery'})
    from ..platforms.accounts import load_account
    account = load_account(studio.sp, body['account'])
    if body.get('delivery','manual') not in ('manual','automatic'): raise ValueError('Choose manual or automatic delivery')
    if body.get('post_type') not in model.POST_TYPES: raise ValueError('Unknown post type')
    if account.post_types and body['post_type'] not in account.post_types: raise ValueError('This profile does not support that post type')
    post = store.load_post(studio.sp,pid)
    if any(v['account']==account.id and v['post_type']==body['post_type'] and not v.get('dropped') for v in post['variants']): raise ValueError('This channel and format already exist')
    v = model.new_variant(account=account.id,platform=account.platform,post_type=body['post_type'])
    v.update(delivery={'method':body.get('delivery','manual')},payload_version=3)
    if account.platform=='instagram' and body['post_type']=='reel' and delivery(v)=='manual':
        v['presentation']={'feed_presence':True}
    post['variants'].append(v)
    saved=store.save_post(studio.sp,post,expected_rev=body['expected_rev'],actor=actor,op='composer.channel',now=studio.now())
    return studio.view(saved)
