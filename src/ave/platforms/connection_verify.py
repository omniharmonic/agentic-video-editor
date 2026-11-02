"""Fixed semantic-read contracts. Never call publishing preflight or createSession.

Provider output is untrusted. Persist only bounded typed evidence; never provider
messages, URLs, arbitrary fields, token bodies or pagination links.
"""
from __future__ import annotations
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

PRODUCTS = {
 'instagram_login': ('instagram', 'Instagram User access token', 'v26.0'),
 'instagram_facebook': ('instagram', 'Facebook User access token', 'v26.0'),
 'facebook_page': ('facebook', 'Facebook Page access token', 'v26.0'),
 'threads': ('threads', 'Threads User access token', 'v1.0'),
 'linkedin_member': ('linkedin', 'Member OAuth access token', '202610'),
 'linkedin_organization': ('linkedin', 'Member OAuth access token', '202610'),
 'x_user': ('x', 'OAuth 2 User access token', 'v2'),
 'bluesky': ('bluesky', 'App password / session access JWT', 'Lexicon 1'),
 'pinterest': ('pinterest', 'OAuth User access token', 'v5'),
 'youtube': ('youtube', 'Google User OAuth refresh token / access token', 'v3'),
 'tiktok_inbox': ('tiktok', 'User access token', 'v2'),
}
DESTINATIONS = {'instagram':'ig_user_id','facebook':'page_id','threads':'threads_user_id',
 'linkedin':'author','x':'user_id','bluesky':'did','pinterest':'board_id','youtube':'channel_id','tiktok':'user_id'}

class VerificationError(ValueError):
    def __init__(self, kind, *, status=None, code=None, subcode=None):
        self.detail = {'state':kind, 'http_status':status, 'provider_code':code, 'provider_subcode':subcode}
        super().__init__(kind.replace('_',' '))


def identifier(value, *, required=True):
    if value is None and not required: return None
    from ..publish.safety import redact
    if not isinstance(value,str) or redact(value)!=value or not re.fullmatch(r'[A-Za-z0-9_.:@-]{1,250}',value):
        raise VerificationError('malformed_identity')
    return value


def endpoint(value):
    p=urlsplit(value)
    if len(value)>300 or p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('','/') or p.port not in (None,443):
        raise ValueError('Use a complete HTTPS host without credentials, path or query')
    import ipaddress
    try: ipaddress.ip_address(p.hostname); raise ValueError('Use a public DNS host')
    except ValueError as e:
        if str(e)=='Use a public DNS host': raise
    if '.' not in p.hostname or p.hostname.endswith(('.local','.localhost')): raise ValueError('Use a public DNS host')
    return 'https://'+p.hostname


def atoms(values):
    if not isinstance(values,list) or len(values)>100: raise VerificationError('malformed_permissions')
    return [v if isinstance(v,str) and re.fullmatch(r'https://www\.googleapis\.com/auth/[a-zA-Z0-9._-]{1,100}',v) else identifier(v) for v in values]


class Reader:
    """One action: at most eight requests / 45 seconds / 256 KiB per body.
    Redirects and provider supplied next URLs are never followed.
    """
    def __init__(self, transport=None):
        self.transport=transport; self.deadline=time.monotonic()+45; self.calls=0

    def request(self, method, url, *, token=None, params=None, data=None, json_body=None, headers=None):
        import requests
        from ..publish.safety import register_secret
        if token:register_secret(token)
        for obj in (params,data,json_body):
            if isinstance(obj,dict):
                for key,value in obj.items():
                    if key in ('access_token','refresh_token','client_secret','password','token') and isinstance(value,str):register_secret(value)
        self.calls+=1
        if self.calls>8 or time.monotonic()>=self.deadline: raise VerificationError('read_budget_exhausted')
        if urlsplit(url).scheme!='https':raise VerificationError('https_required')
        try:
            from .public_network import session
            transport=self.transport or session()
            try:
                remaining=max(.001,self.deadline-time.monotonic())
                r=transport.request(method,url,headers={**({'Authorization':'Bearer '+token} if token else {}),**(headers or {})},
                    params=params,data=data,json=json_body,timeout=(min(5,remaining),min(10,remaining)),allow_redirects=False,stream=True)
            except Exception:
                if self.transport is None:transport.close()
                raise
            raw=bytearray()
            try:
                for chunk in r.iter_content(1):
                    raw.extend(chunk)
                    if len(raw)>262144 or time.monotonic()>self.deadline: raise VerificationError('read_budget_exhausted')
                d=json.loads(raw)
            finally:
                r.close()
                if self.transport is None:transport.close()
            if not isinstance(d,dict): raise VerificationError('malformed_response')
            error=d.get('error')
            if r.status_code>=300 or (isinstance(error,dict) and error.get('code') not in (None,0,'ok')):
                e=error if isinstance(error,dict) else {}
                code=e.get('code'); sub=e.get('error_subcode')
                code=code if type(code) is int or code in ('access_token_invalid','access_token_expired','invalid_grant','invalid_token','ExpiredToken','InvalidToken') else None; sub=sub if type(sub) is int else None
                kind='authentication_failed' if r.status_code==401 or code in (190,102) else 'permission_denied' if r.status_code==403 or code in (10,200) else 'rate_limited' if r.status_code==429 else 'provider_error'
                if sub==463 or code in ('access_token_expired','ExpiredToken'): kind='expired_token'
                if sub==467: kind='revoked_token'
                raise VerificationError(kind,status=r.status_code,code=code,subcode=sub)
            return d
        except VerificationError: raise
        except requests.Timeout: raise VerificationError('network_timeout') from None
        except Exception: raise VerificationError('network_or_response_failure') from None


def verify(account, secret, *, reader=None):
    r=reader or Reader(); product=account.connection.get('product')
    if account.mode=='aggregator' or product not in PRODUCTS: raise VerificationError('unsupported_verification')
    platform,family,version=PRODUCTS[product]
    if platform!=account.platform: raise VerificationError('product_mismatch')
    expected=identifier(account.public.get(DESTINATIONS[platform])); token=secret('access_token' if platform in ('bluesky','youtube') else 'token')
    if not token: raise VerificationError('connect_session_required' if platform in ('bluesky','youtube') else 'credential_missing')
    out={'state':'identity_verified','product':product,'token_family':family,'api_version':version,
        'destination':expected,'actor':None,'grants':[],'tasks':[],'roles':[],'expires_at':None,
        'grant_source':'unknown','grant_complete':False,'capabilities':{'publishing':'unverified','insights':'unverified','comments':'unverified'},
        'limitations':['Identity reads do not certify publishing, app access tier, insights or comments.'],
        'checked_at':datetime.now(timezone.utc).isoformat(),'source':'provider_read'}
    def get(host,path,params=None,auth=token,headers=None): return r.request('GET',host+path,token=auth,params=params,headers=headers)
    if product=='instagram_login':
        d=get('https://graph.instagram.com','/v26.0/me',{'fields':'id,user_id,username,account_type'})
        if 'data' in d:
            if not isinstance(d['data'],list) or len(d['data'])!=1: raise VerificationError('ambiguous_identity')
            d=d['data'][0]
        actual=identifier(d.get('user_id')); out['actor']={'namespace':'instagram_app_scoped','id':identifier(d.get('id'))}
        out['destination_namespace']='instagram_professional'
        out['limitations'].append('Pasted Instagram Login tokens have no supported complete grant/expiry introspection.')
    elif product=='instagram_facebook':
        host='https://graph.facebook.com'; who=get(host,'/v26.0/me',{'fields':'id,name'})
        out['actor']={'namespace':'facebook_user','id':identifier(who.get('id'))}
        permissions=get(host,'/v26.0/me/permissions'); rows=permissions.get('data',[])
        out['grants']=atoms([x.get('permission') for x in rows if isinstance(x,dict) and x.get('status')=='granted'])
        out['grant_source']='user_permissions';out['grant_complete']=not bool(permissions.get('paging',{}).get('next'))
        cursor=None;seen=set();page=None
        for index in range(4):
            params={'fields':'id,name,tasks,instagram_business_account','limit':50}
            if cursor:params['after']=cursor
            pages=get(host,'/v26.0/me/accounts',params)
            entries=pages.get('data')
            if not isinstance(entries,list) or any(not isinstance(p,dict) for p in entries):raise VerificationError('malformed_response')
            page=next((p for p in entries if p.get('id')==account.public.get('page_id')),None)
            if page:break
            paging=pages.get('paging') or {}
            if not paging.get('next'):break
            cursor=(paging.get('cursors') or {}).get('after')
            if index==3 or r.calls>=7 or not isinstance(cursor,str) or not re.fullmatch(r'[A-Za-z0-9_+./=-]{1,2048}',cursor) or cursor in seen:
                raise VerificationError('destination_listing_incomplete')
            seen.add(cursor)
        if not page or (page.get('instagram_business_account') or {}).get('id')!=expected: raise VerificationError('destination_not_managed')
        out['tasks']=atoms(page.get('tasks',[]));d=get(host,'/v26.0/'+expected,{'fields':'id,username,account_type'});actual=identifier(d.get('id'))
        out['destination_namespace']='instagram_professional'
    elif product=='facebook_page':
        d=get('https://graph.facebook.com','/v26.0/me',{'fields':'id,name'});actual=identifier(d.get('id'));out['actor']={'namespace':'facebook_page','id':actual}
        out['limitations'].append('Page token identity does not expose the granting person’s tasks or full permission list.')
    elif product=='threads':
        d=get('https://graph.threads.net','/v1.0/me',{'fields':'id,username,name'});actual=identifier(d.get('id'));out['actor']={'namespace':'threads_user','id':actual}
    elif product.startswith('linkedin'):
        d=get('https://api.linkedin.com','/v2/userinfo');member='urn:li:person:'+identifier(d.get('sub'));out['actor']={'namespace':'linkedin_app_member','id':member}
        actual=member
        if product=='linkedin_organization':
            matches=[]
            for index in range(4):
                start=index*10
                roles=get('https://api.linkedin.com','/rest/organizationAcls',{'q':'roleAssignee','state':'APPROVED','count':10,'start':start},headers={'Linkedin-Version':'202610','X-Restli-Protocol-Version':'2.0.0'})
                entries=roles.get('elements')
                if not isinstance(entries,list) or any(not isinstance(x,dict) for x in entries):raise VerificationError('malformed_response')
                matches=[x for x in entries if x.get('roleAssignee')==member and x.get('state')=='APPROVED' and (x.get('organization') or x.get('organizationTarget'))==expected]
                if matches:break
                paging=roles.get('paging') or {};total=paging.get('total')
                more=any(isinstance(x,dict) and x.get('rel')=='next' for x in paging.get('links',[])) or len(entries)>=10 or (type(total) is int and total>start+len(entries))
                if not more:break
                if index==3 or r.calls>=7 or paging.get('start',start)!=start or paging.get('count',10)!=10:raise VerificationError('role_listing_incomplete')
            if not matches: raise VerificationError('organization_role_unverified')
            out['roles']=atoms([x.get('role') for x in matches]);actual=expected
        out['limitations'].append('OIDC and organization-role reads do not prove social write or versioned Images GET permissions. Issuing-app introspection is not configured.')
    elif product=='x_user':
        d=get('https://api.x.com','/2/users/me')['data'];actual=identifier(d.get('id'));out['actor']={'namespace':'x_user','id':actual}
        out['limitations'].append('This selected X read may be billable; no write grants or credit balance were inferred.')
    elif product=='bluesky':
        d=get(endpoint(account.public.get('pds') or 'https://bsky.social'),'/xrpc/com.atproto.server.getSession');actual=identifier(d.get('did'))
        if d.get('active') is False: raise VerificationError('inactive_identity')
        out['actor']={'namespace':'atproto_did','id':actual};out['limitations'].append('Session read does not introspect app-password grants or authenticate decoded JWT expiry claims.')
    elif product=='pinterest':
        host='https://api-sandbox.pinterest.com' if account.public.get('sandbox') else 'https://api.pinterest.com'
        who=get(host,'/v5/user_account');out['actor']={'namespace':'pinterest_user','id':identifier(who.get('id'))}
        if out['actor']['id']!=account.public.get('user_id'): raise VerificationError('actor_mismatch')
        d=get(host,'/v5/boards/'+expected);actual=identifier(d.get('id'))
        owner=(d.get('owner') or {}).get('username');out['board_owned']=bool(owner and owner==who.get('username'))
        out['limitations'].append('Readable shared boards do not establish ownership or pin creation rights.');out['sandbox']=bool(account.public.get('sandbox'))
    elif product=='youtube':
        d=get('https://www.googleapis.com','/youtube/v3/channels',{'part':'id,snippet','mine':'true','maxResults':50,'fields':'items(id,snippet/title),nextPageToken'})
        if not any(x.get('id')==expected for x in d.get('items',[])): raise VerificationError('channel_not_found')
        actual=expected;out['actor']={'namespace':'youtube_channel','id':expected}
        info=get('https://oauth2.googleapis.com','/tokeninfo',{'access_token':token},auth=None)
        if info.get('aud')!=account.public.get('client_id') or info.get('azp',info.get('aud'))!=account.public.get('client_id'): raise VerificationError('oauth_client_mismatch')
        out['grants']=atoms(str(info.get('scope','')).split());out['grant_source']='google_tokeninfo';out['grant_complete']=True
        exp=info.get('exp')
        if str(exp).isdigit() and int(exp)>0: out['expires_at']=datetime.fromtimestamp(int(exp),timezone.utc).isoformat()
    elif product=='tiktok_inbox':
        d=get('https://open.tiktokapis.com','/v2/user/info/',{'fields':'open_id,display_name'});actual=identifier(d.get('data',{}).get('user',{}).get('open_id'));out['actor']={'namespace':'tiktok_app_user','id':actual}
    else: raise VerificationError('unsupported_verification')
    if actual!=expected: raise VerificationError('destination_mismatch')
    requirements={
        'instagram_login':['instagram_business_content_publish'], 'instagram_facebook':['instagram_content_publish','instagram_basic'],
        'facebook_page':['pages_manage_posts'], 'threads':['threads_content_publish'],
        'linkedin_member':['w_member_social'], 'linkedin_organization':['w_organization_social'],
        'x_user':['tweet.write','media.write'], 'pinterest':['pins:write'],
        'youtube':['https://www.googleapis.com/auth/youtube.upload'], 'tiktok_inbox':['video.upload'], 'bluesky':[]}
    out['publishing_access']={'required':requirements[product],
        'observed':[p for p in requirements[product] if p in out['grants']],
        'missing':[p for p in requirements[product] if p not in out['grants']] if out['grant_complete'] else [],
        'unknown':[p for p in requirements[product] if p not in out['grants']] if not out['grant_complete'] else [],
        'note':'Documented scope requirements only; provider product access, target roles and live format certification remain separate.'}
    return out
