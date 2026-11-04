"""Narrow native action coordinator. Page requests identify context, never authority.

prepare captures exact account/workspace revisions; execute consumes it once only,
after native confirmation/authentication. Dependencies are injectable test seams,
not production success flags or environment bypasses.
"""
from __future__ import annotations
import json
import secrets
import time
from .studio import connections as C

ACTIONS={'connectionConfigure','connectionCredentials','connectionRemoveCredentials','connectionVerify','connectionSession','connectionLive','connectionOff',
         'hostConfigure','hostRemove','serviceStart','serviceStop','serviceRepair','serviceRemove','senderEnable','senderDisable','deliveryResume','providerRefresh','reviewProviderOutcome'}


class Coordinator:
    def __init__(self,session,*,keychain=None,reader_factory=None,service=None):
        from .desktop_service import Service
        self.session=session;self.studio=session.server.app.studio;self.sp=self.studio.sp
        self.keychain=keychain;self.reader_factory=reader_factory or C.Reader;self.service=service or Service(self.sp)
        self.pending={};self.used=set()
    def prepare(self,action,args,rid):
        if self.session.fixture:raise ValueError('Sample mode cannot connect credentials or control a service')
        if action not in ACTIONS or not isinstance(rid,str) or rid in self.used:raise ValueError('Unknown or replayed native request')
        self.used.add(rid)
        if len(self.used)>10000:raise ValueError('Reopen the workspace to start a new native session')
        context={'workspace':str(self.sp.root.resolve()),'action':action,'args':dict(args),'at':time.monotonic()}
        if action.startswith('connection'):
            allowed={'account','revision','values'} if action=='connectionConfigure' else {'account','revision'}
            if set(args)!=allowed:raise ValueError('Invalid connection request fields')
            a=C.check_revision(self.sp,args['account'],args['revision']);context['binding']=C.binding(a)
            context['fields']=C.fields(a);context['platform']=a.platform
            detail=f'Workspace: {self.sp.root}\nProfile: {a.label or a.id} ({a.id})\nPlatform: {a.platform}\nProduct: {a.connection.get("product","not selected")}\nDestination: {json.dumps(a.public,sort_keys=True)}'
            if action=='connectionConfigure':detail+='\nNew configuration: '+json.dumps(args['values'],sort_keys=True)
            effects={
             'connectionConfigure':'Changing this destination invalidates earlier verification, account authority and affected approvals.',
             'connectionCredentials':'Enter the complete credential set in native secure fields. Values go directly to macOS Keychain. Old verification and live authority will be invalidated.',
             'connectionRemoveCredentials':'Remove this account’s root and session credentials. Verification and live authority will be invalidated.',
             'connectionVerify':'Perform only this product’s documented identity/permission reads. No post is created and no send is enabled.',
             'connectionSession':'Exchange the stored root credential for a session. Bluesky creates a session at the selected PDS; YouTube refreshes OAuth and checks the selected client/channel. No post is created.',
             'connectionLive':'Attest this exact account for real publishing. This does not approve any post or enable the workspace sender.',
             'connectionOff':'Revoke this account’s live publishing permission. Manual delivery remains available.'}
            detail+='\n\n'+effects[action]
            if a.platform=='x' and action=='connectionVerify':detail+='\nSpend authorization: X GET /2/users/me may be billed by X. Continue authorizes this one selected read.'
        elif action.startswith('service') or action.startswith('sender'):
            if set(args)!={'revision'}:raise ValueError('Invalid service request')
            state=self.service.snapshot()
            if state['revision']!=args['revision']:raise ValueError('Service changed; reopen Background delivery')
            context['service']=state
            detail=f'Workspace: {self.sp.root}\nService: {state["label"]}\nExecutable: {self.service.exe}\nInterval: {state["interval"]} seconds\nCurrent mode: {state["mode"]}\n'
            detail+= {'serviceStart':'Install/start only this per-user job. It rehearses unless this exact saved service was already explicitly enabled.',
                'serviceRepair':'Replace only this workspace’s known job with the current installed executable. Preserve its recorded mode; do not grant sending permission.',
                'serviceStop':'Stop only this workspace’s background job. Account and sender permissions remain separate; Pause blocks delivery immediately.',
                'serviceRemove':'Stop and remove only this workspace’s known job. Sender permission remains separate.',
                'senderEnable':'Enable unattended real posting for this exact workspace and run its service in send mode. Only live-attested accounts with exact-payload approvals can send. This also authorizes required same-principal credential renewal during sending, provider status recovery and media lease cleanup.',
                'senderDisable':'Revoke unattended sending with a signed off record. Existing service may still run checks but cannot send.'}[action]
        elif action=='hostConfigure':
            if set(args)!={'revision','values'}:raise ValueError('Invalid hosting request')
            C.host_config(args['values'])
            if C.digest(C.read(C.directory(self.sp)/'host.json'))!=args['revision']:raise ValueError('Hosting changed; reopen it')
            context['fields']=['access_key','secret_key'];detail=f'Workspace: {self.sp.root}\nS3-compatible host: {json.dumps(args["values"],sort_keys=True)}\nStore these public settings and both keys in Keychain. No bucket creation, upload or paid test will run.'
        elif action=='hostRemove':
            if set(args)!={'revision'}:raise ValueError('Invalid host removal request')
            doc=C.read(C.directory(self.sp)/'host.json')
            if C.digest(doc)!=args['revision']:raise ValueError('Hosting changed; reopen it')
            detail=f'Workspace: {self.sp.root}\nHost: {json.dumps(doc.get("config",{}),sort_keys=True)}\nRemove both host keys and disable use of this host. Existing public media leases remain recorded for separately authorized cleanup.'
        elif action=='deliveryResume':
            if args:raise ValueError('Resume takes no page arguments')
            detail=f'Resume paused delivery for {self.sp.root}. Existing send permission and post approvals are still required.'
        else:
            if set(args) not in ({'post','variant','revision'},{'post','variant','revision','operation'}):raise ValueError('Invalid provider refresh context')
            from .studio import store,model,state
            from .publish import queue
            p=store.load_post(self.sp,args['post']);v=model.find_variant(p,args['variant'])
            if p['rev']!=args['revision'] or not v.get('queue'):raise ValueError('Post changed or has no operation; reopen it')
            item=queue.load(state.queue_episode(self.sp,v),v['queue']['id']);a=C.load_account(self.sp,v['account'])
            if 'operation' in args and args['operation']!=C.digest(item):raise ValueError('Operation changed; reopen the post')
            context.update(binding=C.binding(a),account=a.id,operation=C.digest(item))
            receipt=item.get('results',{}).get(v['platform'],{})
            detail=f'Workspace: {self.sp.root}\nPost: {p["id"]} / {v["id"]}\nAccount: {a.id}\nDestination: {json.dumps(a.public,sort_keys=True)}\nOperation: {json.dumps((receipt.get("receipt") or {}).get("object",{}),sort_keys=True)}\nRead only the existing operation’s status. This cannot create, retry or approve a post; an uncertain receipt stays uncertain without conclusive evidence.'
            if action=='reviewProviderOutcome':
                detail=f'Workspace: {self.sp.root}\nAccount: {a.id}\nPost: {p["id"]} / {v["id"]}\nOperation digest: {context["operation"]}\nReviewed media hash: {item.get("sha256")}\nExact queued caption: {item.get("caption") or item.get("description") or ""}\nDestination: {json.dumps(a.public,sort_keys=True)}\n\nInspect this exact operation in the platform. This records your report, not provider verification. NOT PUBLISHED consumes its old approval and requires fresh review; it never automatically retries.'
                context['fields']=['outcome','posted_url']
        token=secrets.token_hex(24);self.pending[token]=context
        return {'token':token,'detail':detail,'fields':context.get('fields',[]) if action in ('connectionCredentials','hostConfigure','reviewProviderOutcome') else []}
    def cancel(self,token):self.pending.pop(token,None)
    def execute(self,token,secret_values=None):
        context=self.pending.pop(token,None)
        if not context or time.monotonic()-context['at']>180:raise ValueError('Native request expired or was already used')
        from .desktop_auth import require_act
        require_act(self.session.authority,'connection:'+context['action'])
        if self.session.closed or str(self.sp.root.resolve())!=context['workspace']:raise ValueError('Workspace changed')
        action=context['action'];a=context['args'];s=self.studio
        if action.startswith('connection'):
            account=C.check_revision(self.sp,a['account'],a['revision'])
            if C.binding(account)!=context['binding']:raise ValueError('Destination changed')
            if action=='connectionConfigure':return C.save_config(s,a['account'],a['revision'],a['values'])
            if action in ('connectionCredentials','connectionRemoveCredentials'):return C.credentials(s,a['account'],a['revision'],secret_values or {},keychain=self.keychain,remove=action=='connectionRemoveCredentials')
            if action=='connectionVerify':return C.verify_connection(s,a['account'],a['revision'],keychain=self.keychain,reader=self.reader_factory())
            if action=='connectionSession':return C.connect_session(s,a['account'],a['revision'],keychain=self.keychain,reader=self.reader_factory())
            from .platforms import attest
            from .studio.production import workspace_lock
            with C.connection_lock(self.sp):
                C.check_revision(self.sp,a['account'],a['revision']);doc=C.read(C.record_path(self.sp,account.id))
                on=action=='connectionLive'
                if on and (not C.evidence_valid(account,doc) or doc.get('evidence',{}).get('state')!='identity_verified'):raise ValueError('Verify this exact identity before live-attesting it')
                attest.write_live(self.sp.accounts_dir,account.id,'native device owner',live=on)
                doc['live_enabled']=on;C.atomic(C.record_path(self.sp,account.id),doc)
                if C.evidence_valid(account,doc):C.remember_observation(s,account,doc)
                return {'live_enabled':on,'message':'Account attestation saved. No post was approved or sent.'}
        if action=='hostConfigure':return C.save_host(s,a['revision'],a['values'],secret_values or {},keychain=self.keychain)
        if action=='hostRemove':return C.remove_host(s,a['revision'],keychain=self.keychain)
        if action=='deliveryResume':
            from .studio.dispatch import resume
            return resume(self.sp,by='native device owner')
        if action.startswith('service'):
            return self.service.change(action[7:].lower(),a['revision'],mode=context['service']['mode'],interval=context['service']['interval'])
        if action.startswith('sender'):
            from .studio import sendgate,production as P
            with P.workspace_lock(self.sp):
                if self.service.snapshot()['revision']!=a['revision']:raise ValueError('Service changed; reopen it')
                enabled=action=='senderEnable';sendgate.write(self.sp,'native device owner',enabled=enabled)
                C.atomic(C.directory(self.sp)/'sender.json',{'enabled':enabled,'at':s.now().isoformat(),'source':'native decision'})
            if enabled:
                try:return self.service.change('start',a['revision'],mode='send',interval=context['service']['interval'])
                except Exception:
                    with P.workspace_lock(self.sp):
                        sendgate.disable(self.sp,'native service start failed');C.atomic(C.directory(self.sp)/'sender.json',{'enabled':False,'state':'start_failed'})
                    raise
            return {'enabled':False,'message':'Unattended sending disabled. Stop service separately to stop its checks.'}
        if action=='reviewProviderOutcome':
            from .studio.outcome_review import record
            return record(s,context,secret_values or {})
        return self.refresh(context)
    def refresh(self,context):
        from .studio import store,model,state,delivery
        from .publish import queue
        from .platforms.registry import adapter_for
        import copy
        a=context['args'];p=store.load_post(self.sp,a['post']);v=model.find_variant(p,a['variant'])
        item=queue.load(state.queue_episode(self.sp,v),v['queue']['id']);account=C.load_account(self.sp,v['account'])
        if p['rev']!=a['revision'] or C.binding(account)!=context['binding'] or C.digest(item)!=context['operation']:raise ValueError('Operation changed; reopen it')
        from .desktop_status import StatusReader
        reader=StatusReader(account,item,reader=self.reader_factory(),keychain=self.keychain or C.Keychain())
        # The read-only adapter cannot create a post and never owns a live publisher.
        studio=copy.copy(self.studio);studio.publishers={account.id:reader}
        result=delivery.refresh(studio,a['post'],a['variant'],expected_operation=context['operation'],expected_revision=a['revision'],expected_binding=context['binding'])
        q=result['queue'];rec=q.get('results',{}).get(account.platform,{})
        return {'state':rec.get('state','unknown'),'message':'Status read complete. No publication was created or retried.'}
