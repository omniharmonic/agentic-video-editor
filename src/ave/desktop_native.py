"""Standalone AppKit/WKWebView authoring shell and narrow native action requests.

Web content can request a native sheet, never supply a path or authority. All
workspace/file destinations are selected by the human in AppKit. No eval/exec,
shell, generic Python API or remote navigation is exposed.
"""
from __future__ import annotations
import json
from pathlib import Path
import re
import threading
from urllib.parse import unquote, urlsplit
from .humanpresence import agent_markers_set

from .desktop_connections import ACTIONS as CONNECTION_ACTIONS
COLLABORATION_ACTIONS = {'exchangeSendFile','exchangeEnable','exchangeStop','exchangeInvite','exchangeJoin','exchangeConfirm','exchangeCancelPair','exchangeRevoke'}

ACTIONS = CONNECTION_ACTIONS | COLLABORATION_ACTIONS | {'agentConfig', 'openResolve', 'review', 'import', 'savePackage', 'sharePackage', 'revealPackage', 'recordManualPublication', 'diagnostics', 'selectEpisode', 'reconnectEpisode', 'revokeEpisode', 'confirmEpisode'}
REPORT_FIELDS = {'post_id', 'variant_id', 'package_id', 'reviewed_payload_hash', 'posted_url', 'posted_at',
                 'expected_rev', 'request_id', 'by', 'changed_in_native', 'url_mismatch_explanation'}


def navigation(url, origin):
    """A strict policy evaluated before any download or new-window handling."""
    parsed, expected = urlsplit(url), urlsplit(origin)
    if parsed.username or parsed.password: return 'deny'
    if (parsed.scheme, parsed.hostname, parsed.port) == (expected.scheme, expected.hostname, expected.port): return 'local'
    return 'deny'


def native_message(body, frame_url, origin, main_frame=True):
    if not main_frame or navigation(frame_url,origin)!='local':raise ValueError('Untrusted native request origin')
    body=dict(body);rid=body.get('id');action=body.get('action');args=dict(body.get('args') or {})
    if set(body)!={'id','action','args'} or not isinstance(rid,str) or not re.fullmatch(r'[0-9]{1,12}',rid) or action not in ACTIONS:raise ValueError('Unknown native action')
    if len(json.dumps(args))>12000:raise ValueError('Native request too large')
    return rid,action,args


def external_url(url):
    p = urlsplit(url)
    domains = ('instagram.com','facebook.com','threads.com','threads.net','linkedin.com','tiktok.com','pinterest.com','x.com','twitter.com','bsky.app','youtube.com','youtu.be','developers.facebook.com','developers.google.com','canva.com')
    try: port = p.port
    except ValueError: return False
    return (p.scheme == 'https' and not p.username and not p.password and port in (None,443)
            and bool(p.hostname) and any(p.hostname == d or p.hostname.endswith('.'+d) for d in domains))


def package_navigation(url, sp):
    path = unquote(urlsplit(url).path)
    prefix = '/files/' + sp.rel(sp.work / 'manual_exports') + '/'
    if not path.startswith(prefix): return None
    part = path[len(prefix):]
    if '/' not in part and part.endswith('.zip'): return part[:-4], None
    pieces = part.split('/')
    return (pieces[0], pieces[1]) if len(pieces) == 2 else None


class NativeSession:
    def __init__(self, path=None, *, fixture=False):
        from .desktop_auth import NativeAuthority
        self.authority = NativeAuthority()
        self.fixture = fixture
        if not fixture and agent_markers_set(): raise RuntimeError('Agent sessions cannot launch the native human authority surface')
        from .studio.service import FixtureStudio, open_studio
        from .studio.ui.server import StudioServer
        self.temporary = None
        if fixture:
            from .desktop_fixture import create
            studio, self.temporary = create()
        else: studio = open_studio(path, publishers={}, host=None)
        studio.native_authoring = True
        self.server = StudioServer(studio, fixtures=False, native_authority=self.authority)
        self.server.app.workbench._native_cancel = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.closed = False
        from .desktop_connections import Coordinator
        self.connections = Coordinator(self)

    def close(self):
        if self.closed: return
        self.closed = True
        self.server.app.collaboration.close()
        self.server.app.studio.__dict__.pop('_connection_observations',None)
        self.authority.close()
        self.server.app.views.clear()
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        if self.temporary: self.temporary.cleanup()

    def record(self, request, pin):
        """Only the native controller calls after showing exact data and OS auth."""
        if self.fixture: raise ValueError('Sample work cannot record publication')
        self.authority.require()
        if not isinstance(request, dict) or set(request) != REPORT_FIELDS: raise ValueError('Invalid publication report fields')
        self.server.app.pin.check(pin)
        from .studio.manual_publication import _record
        return _record(self.server.app.ep, **request)


BRIDGE_SCRIPT = r'''
(()=>{const pending=new Map();let serial=0;
function ask(action,args={}){return new Promise((resolve,reject)=>{const id=String(++serial);pending.set(id,{resolve,reject});window.webkit.messageHandlers.aveNative.postMessage({id,action,args});});}
Object.defineProperty(window,'aveNative',{value:Object.freeze({connectionAction:(action,args)=>ask(action,args),agentConfig:()=>ask('agentConfig'),openResolve:()=>ask('openResolve'),recordManualPublication:r=>ask('recordManualPublication',r),requestReview:()=>ask('review'),importFiles:()=>ask('import'),selectEpisode:()=>ask('selectEpisode'),reconnectEpisode:id=>ask('reconnectEpisode',{episode:id}),revokeEpisode:id=>ask('revokeEpisode',{episode:id}),confirmEpisode:token=>ask('confirmEpisode',{token}),savePackage:id=>ask('savePackage',{package_id:id}),sharePackage:(id,filename=null)=>ask('sharePackage',{package_id:id,filename}),revealPackage:id=>ask('revealPackage',{package_id:id})}),writable:false});
Object.defineProperty(window,'__aveNativeReply',{value:(id,ok,data)=>{const p=pending.get(id);if(!p)return;pending.delete(id);ok?p.resolve(data):p.reject(new Error(data.error||'Native action cancelled'));}});
addEventListener('DOMContentLoaded',()=>{const bar=document.createElement('aside');bar.setAttribute('aria-label','Native workspace actions');bar.style.cssText='position:fixed;bottom:12px;left:12px;z-index:90;display:flex;gap:6px';for(const [label,action] of [['Import files','import'],['Unlock review','review']]){const b=document.createElement('button');b.textContent=label;b.onclick=()=>ask(action).catch(e=>{b.textContent=e.message;setTimeout(()=>b.textContent=label,5000)});bar.append(b);}document.body.append(bar);});})();
'''


def main(*, fixture=False):
    if not fixture and agent_markers_set(): raise SystemExit('Native launch refused: an agent marker is set. Open the application yourself in Finder.')
    # Framework imports only after the production marker gate. Headless dispatch
    # never imports this GUI implementation.
    import AppKit as A
    import Foundation as F
    import WebKit as W
    import objc
    from .desktop_files import import_selected, package_file, save_verified, NativeFileWork
    from .desktop_runtime import diagnostics, frozen
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)

    def on_main(fn): F.NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
    def alert(title, detail='', confirm=False, field=False, secret=False):
        box=A.NSAlert.alloc().init();box.setMessageText_(title);box.setInformativeText_(detail)
        box.addButtonWithTitle_('Continue' if confirm else 'OK')
        if confirm: box.addButtonWithTitle_('Cancel')
        inp=None
        if field:
            cls=A.NSSecureTextField if secret else A.NSTextField
            inp=cls.alloc().initWithFrame_(((0,0),(420,26)));box.setAccessoryView_(inp)
        result=box.runModal()
        if result!=A.NSAlertFirstButtonReturn: return None
        return str(inp.stringValue()) if inp is not None else True
    def choose(create=False):
        panel=A.NSOpenPanel.openPanel();panel.setCanChooseDirectories_(True);panel.setCanChooseFiles_(False)
        panel.setCanCreateDirectories_(True);panel.setAllowsMultipleSelection_(False)
        panel.setMessage_('Choose the parent folder for a new studio' if create else 'Choose an existing studio folder (contains studio.json)')
        if panel.runModal()!=A.NSModalResponseOK:return None
        path=Path(str(panel.URL().path()))
        if create:
            title=alert('Create a studio', f'Parent folder: {path}\nEnter a name for the new folder and studio.', True, True)
            if not title:return None
            if len(title)>100 or title in ('.','..') or any(c in title for c in '/\\\0'):raise ValueError('Use a plain studio name')
            path=path/title
            if path.exists():raise ValueError('That folder already exists; choose Open Workspace instead')
            if not alert('Create studio',str(path),True):return None
            from .studio.project import StudioProject
            StudioProject(path).create(title=title)
        from .studio.project import StudioProject
        StudioProject(path).require()
        return path

    class Controller(F.NSObject):
        def init(self):
            self=objc.super(Controller,self).init()
            self.session=None;self.window=None;self.web=None;self.busy=False;self.cancelled=threading.Event();self.picker=None
            self.file_work=NativeFileWork(on_main)
            return self

        @objc.python_method
        def reply(self, rid, ok, result):
            if rid and self.web:
                script='window.__aveNativeReply('+','.join(json.dumps(x) for x in (rid,ok,result))+');'
                self.web.evaluateJavaScript_completionHandler_(script, None)

        @objc.python_method
        def start(self, path=None):
            self.file_work.close();self.file_work=NativeFileWork(on_main)
            if self.session:self.session.close()
            self.session=NativeSession(path,fixture=fixture)
            cfg=W.WKWebViewConfiguration.alloc().init()
            cfg.setWebsiteDataStore_(W.WKWebsiteDataStore.nonPersistentDataStore())
            content=W.WKUserContentController.alloc().init()
            content.addScriptMessageHandler_name_(self,'aveNative')
            content.addUserScript_(W.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(BRIDGE_SCRIPT,W.WKUserScriptInjectionTimeAtDocumentStart,True))
            cfg.setUserContentController_(content)
            self.web=W.WKWebView.alloc().initWithFrame_configuration_(((0,0),(1440,900)),cfg)
            self.web.setNavigationDelegate_(self); self.web.setUIDelegate_(self)
            if not self.window:
                self.window=A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(((100,100),(1440,900)),A.NSWindowStyleMaskTitled|A.NSWindowStyleMaskClosable|A.NSWindowStyleMaskResizable|A.NSWindowStyleMaskMiniaturizable,A.NSBackingStoreBuffered,False)
                self.window.setDelegate_(self)
            self.window.setTitle_('AVE Studio · Sample' if fixture else 'AVE Studio · '+self.session.server.app.ep.read_meta()['title'])
            self.window.setContentView_(self.web);self.window.makeKeyAndOrderFront_(None)
            self.web.loadRequest_(F.NSURLRequest.requestWithURL_(F.NSURL.URLWithString_(self.session.server.url())))

        def webView_decidePolicyForNavigationAction_decisionHandler_(self, web, action, decision):
            url=str(action.request().URL().absoluteString())
            if navigation(url,self.session.server.url())!='local':
                decision(W.WKNavigationActionPolicyCancel)
                if action.navigationType()==W.WKNavigationTypeLinkActivated and external_url(url):A.NSWorkspace.sharedWorkspace().openURL_(F.NSURL.URLWithString_(url))
                return
            package=package_navigation(url,self.session.server.app.ep)
            if package:
                decision(W.WKNavigationActionPolicyCancel)
                on_main(lambda:self.save_package(*package));return
            decision(W.WKNavigationActionPolicyAllow)

        def webView_decidePolicyForNavigationResponse_decisionHandler_(self, web, response, decision):
            url=str(response.response().URL().absoluteString())
            # Never permit WebKit's alternative unauthenticated download path.
            decision(W.WKNavigationResponsePolicyAllow if navigation(url,self.session.server.url())=='local' and response.canShowMIMEType() else W.WKNavigationResponsePolicyCancel)

        def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self, web, config, action, features):
            url=str(action.request().URL().absoluteString())
            if action.navigationType()==W.WKNavigationTypeLinkActivated and external_url(url):A.NSWorkspace.sharedWorkspace().openURL_(F.NSURL.URLWithString_(url))
            return None

        def userContentController_didReceiveScriptMessage_(self, controller, message):
            try:
                frame=message.frameInfo()
                rid,action,args=native_message(message.body(),str(frame.request().URL().absoluteString()),self.session.server.url(),frame.isMainFrame())
                if action in CONNECTION_ACTIONS:self.connection_action(action,args,rid)
                elif action in COLLABORATION_ACTIONS:self.collaboration_action(action,args,rid)
                elif action=='recordManualPublication':self.report(args,rid)
                elif action in ('selectEpisode','reconnectEpisode','revokeEpisode','confirmEpisode'):self.episode_action(action,args,rid)
                elif action in ('savePackage','sharePackage','revealPackage'):
                    if set(args)-{'package_id','filename'} or 'package_id' not in args:raise ValueError('Only a registered package identity is accepted')
                    if args.get('filename') is not None and (action!='sharePackage' or not isinstance(args['filename'],str)):raise ValueError('Choose a registered package original')
                    self.package_action(action,args['package_id'],args.get('filename'),rid)
                elif args:raise ValueError('This native action takes no arguments')
                elif action=='agentConfig':self.agent_config(rid)
                elif action=='openResolve':
                    if fixture:raise ValueError('Sample mode cannot open external apps')
                    A.NSWorkspace.sharedWorkspace().openURL_(F.NSURL.fileURLWithPath_('/Applications/DaVinci Resolve/DaVinci Resolve.app'));self.reply(rid,True,{})
                elif action=='review':self.unlock(rid)
                elif action=='import':self.import_files(rid)
                else:self.diagnostics_(None);self.reply(rid,True,{})
            except Exception as exc:self.reply(locals().get('rid'),False,{'error':str(exc)})

        @objc.python_method
        def collaboration_action(self, action, args, rid):
            if fixture:raise ValueError('Sample mode cannot enable sharing')
            if self.busy or self.file_work.busy:raise ValueError('Wait for the current native operation')
            if args:raise ValueError('Sharing inputs are entered only in the native sheet')
            session=self.session;facade=session.server.app.collaboration;operation=None
            if action=='exchangeEnable':
                panel=A.NSOpenPanel.openPanel();panel.setCanChooseDirectories_(True);panel.setCanChooseFiles_(False);panel.setCanCreateDirectories_(True);panel.setAllowsMultipleSelection_(False)
                panel.setMessage_('Choose a dedicated exchange folder outside live studios and recordings')
                if panel.runModal()!=A.NSModalResponseOK:self.reply(rid,False,{'error':'Sharing cancelled'});return
                directory=Path(str(panel.URL().path()))
                if not alert('Enable editorial exchange',f'Workspace: {facade.sp.root}\nExchange: {directory}\nOnly the verified Tailscale interface on port37091. Active while this app is open. Incoming bundles require local review; no publishing authority is shared.',True):self.reply(rid,False,{'error':'Sharing cancelled'});return
                operation=lambda:facade.enable_native(directory)
            elif action=='exchangeJoin':
                value=alert('Add collaborator','Paste the invitation shared directly by your collaborator. It expires in ten minutes. Both people must compare and confirm the exact code.',True,True,True)
                if value is None:self.reply(rid,False,{'error':'Pairing cancelled'});return
                from .collab_transport import parse,PAIR_LIMIT
                invitation=parse(value.encode(),PAIR_LIMIT)
                operation=lambda:facade.require_exchange().join(invitation)
            elif action=='exchangeConfirm':
                view=facade.snapshot()['pairing']
                if not view:raise ValueError('No pending pairing; create or add an invitation first')
                detail=f"Compare over your trusted channel:\n{view['code']}\nNode: {view['peer']['identity']['node']}\nUser: {view['peer']['identity']['user']}\nStudio: {view['peer']['studio']}\nApp certificate: {view['peer']['tls']['pin']}\nBoth people must confirm these same identities and code."
                if not alert('Confirm this exact collaborator',detail,True):facade.require_exchange().cancel_pair();self.reply(rid,False,{'error':'Pairing cancelled'});return
                operation=lambda:facade.require_exchange().confirm(view['hash'])
            elif action=='exchangeSendFile':
                exchange=facade.require_exchange();peer=exchange.store.state['peer']
                if not peer:raise ValueError('Pair a collaborator first')
                panel=A.NSOpenPanel.openPanel();panel.setCanChooseFiles_(True);panel.setCanChooseDirectories_(False);panel.setAllowsMultipleSelection_(False)
                panel.setMessage_('Choose one file to send to your paired collaborator')
                if panel.runModal()!=A.NSModalResponseOK:self.reply(rid,False,{'error':'Sending cancelled'});return
                chosen=Path(str(panel.URL().path()))
                if not alert('Send this file',f"{chosen.name}\n{chosen.stat().st_size:,} bytes\nTo studio {peer['descriptor']['studio']} on {peer['descriptor']['identity']['node']}.\nIt is checked first, which takes a while for large files.",True):self.reply(rid,False,{'error':'Sending cancelled'});return
                operation=lambda:exchange.offer_file(chosen)
            elif action=='exchangeInvite':operation=lambda:facade.require_exchange().invite()
            elif action=='exchangeCancelPair':operation=lambda:facade.require_exchange().cancel_pair()
            elif action=='exchangeStop':operation=lambda:facade.require_exchange().stop()
            elif action=='exchangeRevoke':
                if not alert('Revoke collaborator','Transport credentials are removed immediately. Pending sends stop; received editorial history is retained.',True):self.reply(rid,False,{'error':'Revocation cancelled'});return
                operation=lambda:facade.require_exchange().revoke()
            def finished(result,error):
                if session is not self.session:return
                if error:self.reply(rid,False,{'error':error});return
                if action=='exchangeInvite':
                    # Secret never enters WebKit, diagnostics or URL/history.
                    box=A.NSAlert.alloc().init();box.setMessageText_('Invitation ready');box.setInformativeText_('Copy this single-use invitation and share it through your trusted channel. It expires in ten minutes. Never post it publicly.');box.addButtonWithTitle_('Copy invitation');box.addButtonWithTitle_('Cancel')
                    if box.runModal()==A.NSAlertFirstButtonReturn:
                        board=A.NSPasteboard.generalPasteboard();board.clearContents();board.setString_forType_(json.dumps(result,separators=(',',':')),A.NSPasteboardTypeString)
                        result={'state':'invitation_copied'}
                    else:facade.require_exchange().cancel_pair();result={'state':'cancelled'}
                self.reply(rid,True,result or {'state':'done'})
            self.file_work.start(lambda cancel,report:operation(),finished,critical=True)

        @objc.python_method
        def connection_action(self,action,args,rid):
            if self.busy or self.file_work.busy:raise ValueError('Wait for the current native operation')
            session=self.session;coordinator=session.connections
            prepared=coordinator.prepare(action,args,rid);token=prepared['token']
            session.authority.cancel();session.server.app.views.clear()
            if not alert('Confirm connection action',prepared['detail'],True):
                coordinator.cancel(token);self.reply(rid,False,{'error':'Action cancelled; nothing changed'});return
            def completed(ok):
                if session is not self.session:coordinator.cancel(token);return
                if not ok:coordinator.cancel(token);self.reply(rid,False,{'error':'Device-owner authentication failed or was cancelled'});return
                values={}
                for name in prepared['fields']:
                    is_report=action=='reviewProviderOutcome'
                    detail=('Type PUBLISHED or NOT PUBLISHED after inspecting the exact operation.' if name=='outcome' else 'Publication URL. Leave blank for NOT PUBLISHED.') if is_report else name.replace('_',' ')+'\nStored directly in macOS Keychain for this exact workspace connection.'
                    value=alert('Publication outcome report' if is_report else 'Secure credential entry',detail,True,True,not is_report)
                    if value is None:
                        coordinator.cancel(token);session.authority.cancel();self.reply(rid,False,{'error':'Credential entry cancelled; nothing changed'});return
                    values[name]=value
                self.window.setTitle_('AVE Studio · Completing connection action · cancellation unavailable')
                def finished(result,error):
                    self.window.setTitle_('AVE Studio');session.authority.cancel()
                    self.reply(rid,not bool(error),{'error':error} if error else result)
                try:self.file_work.start(lambda cancel,report:coordinator.execute(token,values),finished,critical=True)
                except Exception:
                    coordinator.cancel(token);session.authority.cancel();raise
            session.authority.request('Confirm '+action+' for '+str(session.server.app.ep.root),completed)

        @objc.python_method
        def agent_config(self,rid):
            if fixture:raise ValueError('Sample mode cannot save host configuration')
            from .agent import mcp_config
            from .desktop_tools import save_agent_config
            config=mcp_config(self.session.server.app.ep.root)
            if not alert('Save this agent configuration',json.dumps(config,indent=2)+'\nThis creates configuration only; it does not establish an agent connection. An existing file is backed up.',True):
                self.reply(rid,False,{'error':'Configuration save cancelled'});return
            panel=A.NSSavePanel.savePanel();panel.setNameFieldStringValue_('ave-studio-mcp.json')
            if panel.runModal()!=A.NSModalResponseOK:self.reply(rid,False,{'error':'Configuration save cancelled'});return
            target=Path(str(panel.URL().path()))
            result=save_agent_config(target,config);self.reply(rid,True,result)

        @objc.python_method
        def episode_action(self,action,args,rid):
            if fixture:raise ValueError('Sample mode cannot select folders or confirm editorial decisions')
            session=self.session
            from .studio import episode_registry as ER
            if action=='confirmEpisode':
                if self.busy or self.file_work.busy:raise ValueError('A native operation is running; wait or cancel it before confirming')
                if set(args)!={'token'}:raise ValueError('An exact prepared decision token is required')
                checkpoint=session.server.app.episode_checkpoints;token=args['token'];owner=self.file_work
                req=checkpoint.describe(token)
                detail=f"Episode: {req['label']}\nSession: {req['session']}\nDecision: {req['decision']} {req['kind']}\n"+json.dumps(req['payload'],indent=2)+f"\nExact input identity: {req['dependency_sha256']}\nPreview: {req['dependencies'].get('preview_sha256','source evidence')}\nThis confirms editorial choices only; it grants no publishing or license authority."
                if not alert('Confirm this exact episode decision?',detail,True):self.reply(rid,False,{'error':'Decision cancelled'});return
                def confirmed():
                    try:
                        if session is not self.session or owner is not self.file_work or owner.closed:return
                        if self.busy or owner.busy:raise ValueError('A native operation is running; wait or cancel it before confirming')
                        pin=alert('Confirm episode decision','Enter the PIN shown in the native review sheet.',True,True,True)
                        if pin is None:raise ValueError('Decision cancelled')
                        if session is not self.session or owner is not self.file_work or owner.closed:return
                        self.window.setTitle_('AVE Studio · Checking episode decision · Cancel File Operation to stop')
                        def finished(result,error):
                            if session is not self.session or owner is not self.file_work:return
                            self.window.setTitle_('AVE Studio')
                            self.reply(rid,not bool(error),{'error':error} if error else result)
                        # Contextual sheets, authentication and PIN stay on main;
                        # the native-owned verification thread receives no JS capability.
                        owner.start(lambda cancel,report:checkpoint.confirm_native(token,pin,cancelled=cancel),finished,committed_on_return=True)
                    except Exception as exc:
                        if session is self.session and owner is self.file_work and not owner.closed:
                            self.window.setTitle_('AVE Studio');self.reply(rid,False,{'error':str(exc)})
                if session is self.session and owner is self.file_work and not owner.closed:self.unlock(rid,confirmed)
                return
            if action=='revokeEpisode':
                if set(args)!={'episode'}:raise ValueError('Choose a registered episode')
                row=ER.get(session.server.app.studio.sp,args['episode'])
                if not alert('Disconnect episode',row['label']+'\nStop all new app reads and writes. Existing files are preserved.',True):self.reply(rid,False,{'error':'Disconnect cancelled'});return
                ER.revoke(session.server.app.studio,args['episode']);self.reply(rid,True,{'revoked':True});return
            if set(args)!=(set() if action=='selectEpisode' else {'episode'}):raise ValueError('Only a registered reconnect identity is accepted')
            panel=A.NSOpenPanel.openPanel();panel.setCanChooseDirectories_(True);panel.setCanChooseFiles_(False);panel.setCanCreateDirectories_(False);panel.setAllowsMultipleSelection_(False)
            panel.setMessage_('Choose an existing episode folder containing episode.json. Its original recordings remain read-only.')
            if panel.runModal()!=A.NSModalResponseOK:self.reply(rid,False,{'error':'Episode selection cancelled'});return
            root=Path(str(panel.URL().path()))
            meta=ER.json_file(root/'episode.json')
            from .episode import resolve_layout
            layout=resolve_layout(meta.get('layout'));additional={}
            for kind in ER.KINDS:
                destination=Path(__import__('os').path.abspath(root/layout[kind]))
                if destination.is_relative_to(root):continue
                extra=A.NSOpenPanel.openPanel();extra.setCanChooseDirectories_(True);extra.setCanChooseFiles_(False);extra.setCanCreateDirectories_(False)
                extra.setMessage_(f'This custom layout needs a separate {kind} folder grant: {destination}. Choose that exact existing folder.')
                if extra.runModal()!=A.NSModalResponseOK or Path(str(extra.URL().path()))!=destination:raise ValueError('Additional '+kind+' folder grant was not provided')
                additional[kind]=destination
            _,paths=ER.layout_paths(root,meta,additional)
            description='Read original recordings in:\n'+ '\n'.join(str(p) for p in paths['raw_dirs'])+'\n\nCreate derived analysis, edits and exports in:\n'+'\n'.join(str(paths[k][0]) for k in ER.KINDS)+'\n\nNative-confirmed bounds may update only episode.json trim. The app does not adopt ownership or assert usage rights.'
            if not alert('Allow scoped episode access?',description,True):self.reply(rid,False,{'error':'Episode access cancelled'});return
            def finished(result,error):
                self.reply(rid,not bool(error),{'error':error} if error else {k:result[k] for k in ('id','label','ownership','grant_revision')})
            self.file_work.start(lambda cancel,report:ER.register_selected(session.server.app.studio,root,derived_consent=True,additional=additional,reconnect=args.get('episode')),finished)

        @objc.python_method
        def unlock(self,rid=None,after=None):
            if fixture:self.reply(rid,False,{'error':'Sample mode cannot activate protected authority'});return
            # A cancelled contextual prompt also revokes earlier session authority.
            self.session.authority.cancel()
            self.session.server.app.views.clear()
            if not alert('Unlock review for this workspace',f'{self.session.server.app.ep.root}\nDevice-owner authentication enables review and manual publication recording for this session. It does not approve or send content.',True):
                self.reply(rid,False,{'error':'Authentication cancelled'});return
            current=self.session
            def completed(ok):
                if current is not self.session:return
                if not ok:self.reply(rid,False,{'error':'Device-owner authentication failed or was cancelled'});return
                # PIN is only ever sent to AppKit, not the webview or protocol.
                alert('Approval PIN',current.server.app.pin.reveal()+'\nReview the exact media, then enter this PIN in the review form. Never give it to an agent.')
                if after:after()
                else:self.reply(rid,True,{'authenticated':True})
            current.authority.request('Review and record publication in '+str(current.server.app.ep.root),completed)

        @objc.python_method
        def report(self,args,rid):
            if set(args)!=REPORT_FIELDS:raise ValueError('Invalid publication report')
            if self.busy:raise ValueError('Wait for the current import')
            session=self.session
            from .studio import store
            def progress(n,total):self.window.setTitle_(f'AVE Studio · Checking report package {n/1024**2:.0f} of {total/1024**2:.0f} MiB')
            def prepared(result,error):
                self.window.setTitle_('AVE Studio')
                if error:self.reply(rid,False,{'error':error});return
                try:
                    m=result[2]
                    if not m.get('reviewed') or (m['post'],m['variant'],m['reviewed_payload_hash'])!=(args['post_id'],args['variant_id'],args['reviewed_payload_hash']):raise ValueError('Report differs from the reviewed package')
                    if store.load_post(session.server.app.ep,m['post'])['rev']!=args['expected_rev']:raise ValueError('Draft changed; reopen this report')
                    description=f"Workspace: {session.server.app.ep.root}\nPackage: {m['package_id']}\nDestination: {m['account']}\nExported revision: {m['rev']}\nReviewed payload: {m['reviewed_payload_hash']}\nPublished URL: {args['posted_url']}\nPublished at: {args['posted_at']}\nReporter: {args['by']}\nNative changes: {args['changed_in_native']}\nAlternate URL: {args['url_mismatch_explanation']}\n\nThis records your statement; it does not verify the platform or send anything."
                    if not alert('Record this exact manual publication?',description,True):self.reply(rid,False,{'error':'Report cancelled'});return
                    def record():
                        try:
                            pin=alert('Confirm publication report','Enter the approval PIN shown in the native sheet.',True,True,True)
                            if pin is None:raise ValueError('Report cancelled')
                            # The exact captured session/request/PIN stay in-process.
                            # Once dispatched, the existing journal must finish; close
                            # joins it before revoking this session or changing workspace.
                            request=dict(args)
                            self.window.setTitle_('AVE Studio · Recording publication · cancellation unavailable')
                            def finished(result,error):
                                self.window.setTitle_('AVE Studio')
                                self.reply(rid,not bool(error),{'error':error} if error else result)
                            self.file_work.start(lambda cancel,report:session.record(request,pin),finished,critical=True)
                        except Exception as exc:self.reply(rid,False,{'error':str(exc)})
                    self.unlock(rid,record)
                except Exception as exc:self.reply(rid,False,{'error':str(exc)})
            self.file_work.start(lambda cancel,report:package_file(session.server.app.ep,args['package_id'],cancelled=cancel,progress=report),prepared,progress)

        @objc.python_method
        def save_package(self,package_id,filename=None):
            try:self.package_action('savePackage',package_id,filename)
            except Exception as exc:alert('Could not prepare package',str(exc))

        @objc.python_method
        def package_action(self,action,pid,filename=None,rid=None):
            if self.busy:raise ValueError('An import is running; wait before preparing a package')
            session=self.session
            def progress(n,total):self.window.setTitle_(f'AVE Studio · Package {n/1024**2:.0f} of {total/1024**2:.0f} MiB · Cancel File Operation in File menu')
            def failed(error):
                self.window.setTitle_('AVE Studio');self.reply(rid,False,{'error':error})
                if not rid:alert('Package operation stopped',error)
            def saved(target,error):
                if error:failed(error);return
                self.window.setTitle_('AVE Studio');self.reply(rid,True,{})
                alert('Saved exact package bytes',str(target))
            def prepared(result,error):
                self.window.setTitle_('AVE Studio')
                if error:failed(error);return
                source,sha,_=result
                try:
                    if action=='savePackage':
                        panel=A.NSSavePanel.savePanel();panel.setNameFieldStringValue_(source.name)
                        if panel.runModal()!=A.NSModalResponseOK:
                            self.reply(rid,False,{'error':'Save cancelled'});return
                        destination=Path(str(panel.URL().path()))
                        self.file_work.start(lambda cancel,report:save_verified(source,destination,sha,cancelled=cancel,progress=report),saved,progress,committed_on_return=True)
                        return
                    url=F.NSURL.fileURLWithPath_(str(source))
                    if action=='revealPackage':A.NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([url])
                    else:
                        self.picker=A.NSSharingServicePicker.alloc().initWithItems_([url])
                        self.picker.showRelativeToRect_ofView_preferredEdge_(((10,10),(1,1)),self.web,A.NSMinYEdge)
                    self.reply(rid,True,{})
                except Exception as exc:failed(str(exc))
            self.window.setTitle_('AVE Studio · Verifying package · Cancel File Operation in File menu')
            self.file_work.start(lambda cancel,report:package_file(session.server.app.ep,pid,filename,cancelled=cancel,progress=report),prepared,progress)

        @objc.python_method
        def import_files(self,rid=None):
            if self.file_work.busy:raise ValueError('A package operation is running; wait or cancel it first')
            if self.busy:raise ValueError('An import is already running; use Cancel Import to stop it')
            panel=A.NSOpenPanel.openPanel();panel.setCanChooseFiles_(True);panel.setCanChooseDirectories_(True);panel.setAllowsMultipleSelection_(True)
            panel.setMessage_('Choose audio/video files or folders. Originals remain unchanged; folders include their immediate regular media files.')
            if panel.runModal()!=A.NSModalResponseOK:self.reply(rid,False,{'error':'Import cancelled'});return
            from .studio.workbench import TEXT_EXTS,DOCUMENT_EXTS,AUDIO_EXTS,VIDEO_EXTS
            selected=[]
            for u in panel.URLs():
                p=Path(str(u.path()))
                selected.extend(sorted(x for x in p.iterdir() if x.is_file() and not x.is_symlink() and not x.name.startswith('._') and x.suffix.lower() in TEXT_EXTS+DOCUMENT_EXTS+AUDIO_EXTS+VIDEO_EXTS) if p.is_dir() else [p])
            self.busy=True;self.cancelled.clear();session=self.session
            def progress(n,total):on_main(lambda:self.window.setTitle_(f'AVE Studio · Importing {n/1024**2:.0f} of {total/1024**2:.0f} MiB · Cancel File Operation in File menu'))
            def run():
                try:result=import_selected(session.server.app.workbench,selected,cancelled=self.cancelled.is_set,progress=progress);ok=True
                except Exception as exc:result={'error':str(exc)};ok=False
                def finished():
                    self.busy=False;self.window.setTitle_('AVE Studio');self.reply(rid,ok,result)
                    if not rid:alert('Import completed' if ok else 'Import stopped',str(result))
                on_main(finished)
            threading.Thread(target=run,daemon=True).start()

        def open_(self,sender):
            if self.busy or self.file_work.busy:alert('File operation in progress','Cancel the file operation and wait before switching workspaces.');return
            try:
                path=choose()
                if path:self.start(path)
            except (Exception,SystemExit) as exc:alert('Could not open workspace',str(exc))
        def create_(self,sender):
            if self.busy or self.file_work.busy:return
            try:
                path=choose(True)
                if path:self.start(path)
            except (Exception,SystemExit) as exc:alert('Could not create workspace',str(exc))
        def unlock_(self,sender):self.unlock()
        def import_(self,sender):
            try:self.import_files()
            except Exception as exc:alert('Could not import',str(exc))
        def cancel_(self,sender):self.cancelled.set();self.file_work.cancel()
        def help_(self,sender):
            import sys
            if frozen():
                path=Path(sys.executable).parent.parent/'Resources/help/index.html'
                A.NSWorkspace.sharedWorkspace().openURL_(F.NSURL.fileURLWithPath_(str(path)))
            else:alert('AVE Studio Help','Read docs/desktop.md in the source checkout.')
        def diagnostics_(self,sender):
            try:alert('Runtime diagnostics',json.dumps(diagnostics() if frozen() else {'runtime':'source checkout'},indent=2))
            except Exception as exc:alert('Runtime unavailable',str(exc))
        def windowWillClose_(self,notification):
            self.cancelled.set()
            self.file_work.close()
            if self.session:self.session.close()
            app.stop_(None)
            wake=A.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(A.NSApplicationDefined,(0,0),0,0,0,None,0,0,0)
            app.postEvent_atStart_(wake,False)
        def quit_(self,sender):self.window.close()

    controller=Controller.alloc().init()
    menu=A.NSMenu.alloc().init();app.setMainMenu_(menu)
    for title,entries in [('AVE Studio',[('Quit AVE Studio','quit:','q')]),('File',[('Open Workspace…','open:','o'),('Create Studio…','create:','n'),('Import Files or Folder…','import:','i'),('Cancel File Operation','cancel:','')]),('Review',[('Unlock Review / Show Approval PIN…','unlock:','')]),('Help',[('AVE Studio Help','help:',''),('Runtime Diagnostics','diagnostics:','')])]:
        item=A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title,None,'');sub=A.NSMenu.alloc().initWithTitle_(title);item.setSubmenu_(sub);menu.addItem_(item)
        for label,selector,key in entries:
            child=A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label,selector,key);child.setTarget_(controller);sub.addItem_(child)
    try:
        if fixture:controller.start()
        else:
            box=A.NSAlert.alloc().init();box.setMessageText_('Welcome to AVE Studio');box.setInformativeText_('Choose a workspace. Creating and editing do not require authentication.')
            for label in ('Open Workspace','Create Studio','Cancel'):box.addButtonWithTitle_(label)
            result=box.runModal()
            if result==A.NSAlertThirdButtonReturn:return 0
            while True:
                try:path=choose(result==A.NSAlertSecondButtonReturn);break
                except (SystemExit,ValueError) as exc:      # `require` exits for a CLI; here the person just picked another folder
                    alert('That folder is not a studio',f'{exc}\nChoose the folder that contains studio.json.')
            if path is None:return 0
            controller.start(path)
        app.activateIgnoringOtherApps_(True);app.run()
    finally:
        controller.file_work.close()
        if controller.session:controller.session.close()
    return 0
