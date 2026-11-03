"""Local profile decoration and revisioned writing groups. No connection authority."""
from __future__ import annotations
import hashlib
import json
import secrets
from urllib.parse import urlparse
import yaml
from ..episode import read_json, write_json
from . import model, store, production as P
from .events import append_event, make_event


def profiles(studio):
    out=[]
    for path in sorted(studio.sp.accounts_dir.glob('*.yaml')):
        P.safe_path(studio.sp,path)
        raw=path.read_bytes();cfg=yaml.safe_load(raw)
        out.append({'id':cfg['id'],'platform':cfg['platform'],'label':cfg.get('label') or cfg['id'],
                    'profile':cfg.get('profile') or {},'revision':hashlib.sha256(raw).hexdigest()})
    return out


def url(value):
    P.text(value,'URL',2000)
    if value and (urlparse(value).scheme not in ('https','http') or not urlparse(value).hostname): raise ValueError('Use a complete http or https URL')
    return value


def profile_save(studio,body,actor,pid=None):
    P.only(body,{'expected_rev','platform','profile'})
    profile=body.get('profile');P.only(profile,{'display_name','handle','bio','avatar','url'})
    for k,n in [('display_name',150),('handle',100),('bio',2000)]: P.text(profile.get(k,''),k,n,required=k=='display_name')
    url(profile.get('url',''))
    avatar=profile.get('avatar')
    if avatar and store.read_assets(studio.sp).get(avatar,{}).get('kind')!='image': raise ValueError('Choose a registered image for the avatar')
    with P.workspace_lock(studio.sp):
        if pid:
            model.check_id(pid);path=P.safe_path(studio.sp,studio.sp.accounts_dir/(pid+'.yaml'))
            raw=path.read_bytes();cfg=yaml.safe_load(raw)
            if hashlib.sha256(raw).hexdigest()!=body.get('expected_rev'): raise model.RevConflict(next(x for x in profiles(studio) if x['id']==pid))
            if 'platform' in body and body['platform']!=cfg['platform']: raise ValueError('Profile edits cannot change the destination platform')
        else:
            if body.get('expected_rev') is not None: raise ValueError('New profile must not have a revision')
            platform=body.get('platform')
            if platform not in model.PLATFORMS: raise ValueError('Choose a supported platform')
            pid='local_'+secrets.token_hex(8);path=P.safe_path(studio.sp,studio.sp.accounts_dir/(pid+'.yaml'))
            cfg={'v':1,'id':pid,'platform':platform,'mode':'manual','label':profile['display_name'],'tz':'UTC','post_types':list(model.POST_TYPES)}
        cfg.update(profile=profile,label=profile['display_name'])
        from ..platforms.accounts import Account
        Account.from_config(cfg,studio.sp.accounts_dir)
        path.parent.mkdir(parents=True,exist_ok=True)
        temp=P.safe_path(studio.sp,path.with_suffix('.yaml.tmp'))
        temp.write_text(yaml.safe_dump(cfg,sort_keys=False));temp.replace(path)
        append_event(studio.sp,make_event(actor=actor,op='profile.edit' if body.get('expected_rev') else 'profile.create',detail={'account':pid},now=studio.now()))
    return next(x for x in profiles(studio) if x['id']==pid)


def profile_delete(studio,pid,body,actor):
    P.only(body,{'expected_rev'})
    with P.workspace_lock(studio.sp):
        row=next(x for x in profiles(studio) if x['id']==pid)
        if row['revision']!=body.get('expected_rev'): raise model.RevConflict(row)
        if any(v['account']==pid for p in store.list_posts(studio.sp) for v in p['variants']): raise ValueError('This profile has content. Keep it to preserve channel history.')
        path=P.safe_path(studio.sp,studio.sp.accounts_dir/(model.check_id(pid)+'.yaml'))
        cfg=yaml.safe_load(path.read_text())
        if cfg.get('mode')!='manual' or cfg.get('credentials') or path.with_suffix('.live.json').exists(): raise ValueError('Connected profiles cannot be removed through authoring')
        path.unlink()
        append_event(studio.sp,make_event(actor=actor,op='profile.remove',detail={'account':pid},now=studio.now()))
    return {'removed':pid}


def groups(studio):
    path=P.safe_path(studio.sp,studio.sp.work/'authoring'/'copy-groups.json')
    return read_json(path) if path.exists() else {'v':1,'rev':0,'items':[]}


def group_save(studio,body,actor):
    P.only(body,{'expected_rev','item','remove'})
    with P.workspace_lock(studio.sp):
        data=groups(studio)
        if type(body.get('expected_rev')) is not int or body['expected_rev']!=data['rev']: raise model.RevConflict(data)
        if body.get('remove'):
            model.check_id(body['remove']);data['items']=[i for i in data['items'] if i['id']!=body['remove']]
        else:
            item=body.get('item');P.only(item,{'id','kind','name','text','platform'})
            if item.get('kind') not in ('caption','hook','hashtags'): raise ValueError('Choose caption, hook or hashtags')
            P.text(item.get('name'),'Name',150,True);P.text(item.get('text'),'Text',20000,True)
            if item.get('platform') not in (None,*model.PLATFORMS): raise ValueError('Unknown platform')
            gid=model.check_id(item['id']) if item.get('id') else 'g_'+secrets.token_hex(8)
            old=next((i for i in data['items'] if i['id']==gid),{})
            item={**item,'id':gid,'rev':old.get('rev',0)+1,'updated':studio.now().isoformat(),'by':actor.name}
            data['items']=[i for i in data['items'] if i['id']!=gid]+[item]
            if len(data['items'])>500: raise ValueError('Maximum 500 saved writing groups')
        data['rev']+=1
        path=P.safe_path(studio.sp,studio.sp.work/'authoring'/'copy-groups.json');write_json(path,data)
        append_event(studio.sp,make_event(actor=actor,op='copy-group.save',detail={'rev':data['rev']},now=studio.now()))
        return data
