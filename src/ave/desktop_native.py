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
import time
from urllib.parse import unquote, urlsplit
from .humanpresence import agent_markers_set

from .desktop_connections import ACTIONS as CONNECTION_ACTIONS
# The two approval sheets (desktop_approvals): the web view names a post's variant and the sha it showed, or a project and a
# session. The words, the device-owner check and the PIN are all native; an agent marker refuses every step.
from .desktop_approvals import ACTIONS as APPROVAL_ACTIONS, PIN_ACTIONS, Approvals, approval_args, refuse_agent
COLLABORATION_ACTIONS = {'exchangeEnable','exchangeStop','exchangeInvite','exchangeJoin','exchangeConfirm','exchangeCancelPair','exchangeRevoke'}
# Project sharing over Syncthing (ave.syncthing.members). The web view names a project by its id; the person, media choice,
# invitation text, folder and six-digit code are only ever entered in native sheets.
SHARING_ACTIONS = {'shareProject','joinProject','removeMember','handOverPublishing'}
PROJECT_ID = re.compile(r'prj_[A-Za-z0-9]{1,64}')

ACTIONS = CONNECTION_ACTIONS | COLLABORATION_ACTIONS | SHARING_ACTIONS | APPROVAL_ACTIONS | {'agentConfig', 'openResolve', 'openTools', 'import', 'importLibrary', 'savePackage', 'sharePackage', 'revealPackage', 'recordManualPublication', 'diagnostics', 'selectEpisode', 'reconnectEpisode', 'revokeEpisode', 'confirmEpisode'}
REPORT_FIELDS = {'post_id', 'variant_id', 'package_id', 'reviewed_payload_hash', 'posted_url', 'posted_at',
                 'expected_rev', 'request_id', 'by', 'changed_in_native', 'url_mismatch_explanation'}


TOOL_ROUTE = re.compile(r'#/(pipeline|workbench|grid(/[A-Za-z0-9_-]{1,80})?|post/[A-Za-z0-9_-]{1,80})')


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


def sharing_args(action, args):
    """The only argument a sharing request may carry: the id of a project on this board (none to join one)."""
    if action=='joinProject':
        if args:raise ValueError('The invitation and the folder are chosen in the native sheet')
        return None
    if set(args)!={'project'} or not isinstance(args['project'],str) or not PROJECT_ID.fullmatch(args['project']):raise ValueError('Choose a project on the board')
    return args['project']


def share_words(person, title, media):
    from .syncthing.members import MEDIA_WORDS
    return (f'Send this invitation to {person} yourself, in Messages or another app you trust. It works for 10 minutes and only for {title}.\n\n'
            f'{person} will keep {MEDIA_WORDS[media]}. When {person} has pasted it, a six-digit code appears on {person}\'s screen. '
            f'{person} reads it to you and you type it here.')


def inviter_code_words(person, answered):
    """The owner's code sheet. It shows no code: the owner types the one the invitee reads aloud."""
    more = (f'{answered} computers answered this invitation. Only the one whose code {person} reads to you is added.\n\n'
            if answered > 1 else '')
    return more + (f'Ask {person} to read the six-digit code on their screen, and type it here. '
                   f'Do not type a code from anywhere else. Nothing is shared until it matches.')


def invitee_code_words(who, code):
    """The invitee's sheet: the only place the code is shown."""
    from .syncthing.members import display_code
    return (f'Read this code to {who}: {display_code(code)}\n\n'
            f'When {who} has typed it, press Continue. This computer then checks that it is talking to {who}\'s computer '
            f'before anything is shared.')


def removal_words(person, title):
    return (f'{person} stops receiving anything new from this computer straight away, and from the other members as soon as they '
            f'next hear from this one. {person} keeps everything already received.')


def handover_words(person, title):
    return (f"{person}'s computer will publish {title}, and this one will stop. Nothing sends from {person}'s computer until "
            f'{person} approves it again there.')


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
        self._sync = None
        self._sync_lock = threading.Lock()
        self.server.workspace.sync = lambda: self._sync    # the board's sync pills read this computer's sync
        if not fixture: threading.Thread(target=self._resume_sync, daemon=True, name='ave-sync-resume').start()
        from .desktop_connections import Coordinator
        self.connections = Coordinator(self)
        self.approvals = Approvals(self.server.app, self.server.workspace, getattr(self.server, 'episodes', None), server=self.server)

    def person(self):
        """Who approves on this computer: its person, as the board names them."""
        from .project.model import Device, _default_person
        return _default_person(Device.load())

    def sync(self):
        """This computer's project sync, started the first time a project is shared or joined. Tailscale only."""
        with self._sync_lock:
            if self._sync is None:
                from .project.model import Device, _default_person
                from .syncthing import SyncManager
                self._sync = SyncManager(name=_default_person(Device.load()))
            if self.closed: raise ValueError('The window is closing.')
            self._sync.start()
        if self._sync.state not in ('running', 'starting', 'restarting'):
            raise ValueError('Sharing needs Tailscale on this computer. '+(self._sync.waiting_reason or 'Open Tailscale and sign in, then try again.'))
        return self._sync

    def _resume_sync(self):
        """A project shared with someone keeps syncing from launch, so its pill is live without sharing anything again.
        Without Tailscale the manager keeps checking, and the pill says "Tailscale is off"."""
        try:
            from .project.model import Device
            me = Device.load().id
            if not any(any(d != me for m in p.members for d in (m.get('devices') or [])) for p in self.server.workspace.projects()): return
            self.sync()
        except Exception: pass   # sync stays off; sharing starts it and says why if it cannot

    def project(self, project_id):
        return self.server.workspace._project(project_id)

    def close(self):
        if self.closed: return
        self.closed = True
        with self._sync_lock:
            if self._sync is not None: self._sync.stop()
        self.server.app.collaboration.close()
        self.server.app.studio.__dict__.pop('_connection_observations',None)
        self.authority.close()
        self.server.app.views.clear()
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        if self.temporary: self.temporary.cleanup()

    def record(self, request, pin, grant=None):
        """Only the native controller calls after showing exact data and OS auth, spending that check's grant."""
        if self.fixture: raise ValueError('Sample work cannot record publication')
        if not isinstance(request, dict) or set(request) != REPORT_FIELDS: raise ValueError('Invalid publication report fields')
        with self.authority.use(grant, 'recordManualPublication', (request['package_id'],)):
            self.authority.require('recordManualPublication')
            self.server.app.pin.check(pin)
            from .studio.manual_publication import _record
            return _record(self.server.app.ep, **request)


def menu_entries(update_title):
    """The menu bar: [(menu, [(label, selector, key)])]. Every selector is a Controller method in main()."""
    return [('AVE Studio',[(update_title,'checkForUpdates:',''),('Quit AVE Studio','quit:','q')]),
            ('File',[('Open Workspace…','open:','o'),('Create Studio…','create:','n'),('Import Files or Folder…','import:','i'),('Cancel File Operation','cancel:','')]),
            ('View',[('The Room and the Workspace','room:','1'),('More Tools…','moreTools:','')]),
            ('Help',[('AVE Studio Help','help:',''),('Runtime Diagnostics','diagnostics:','')])]


BRIDGE_SCRIPT = r'''
(()=>{const pending=new Map();let serial=0;
function ask(action,args={}){return new Promise((resolve,reject)=>{const id=String(++serial);pending.set(id,{resolve,reject});window.webkit.messageHandlers.aveNative.postMessage({id,action,args});});}
Object.defineProperty(window,'aveNative',{value:Object.freeze({connectionAction:(action,args)=>ask(action,args),agentConfig:()=>ask('agentConfig'),openResolve:()=>ask('openResolve'),recordManualPublication:r=>ask('recordManualPublication',r),openTools:route=>ask('openTools',{route}),importFiles:()=>ask('import'),importLibrary:()=>ask('importLibrary'),selectEpisode:()=>ask('selectEpisode'),reconnectEpisode:id=>ask('reconnectEpisode',{episode:id}),revokeEpisode:id=>ask('revokeEpisode',{episode:id}),confirmEpisode:token=>ask('confirmEpisode',{token}),savePackage:id=>ask('savePackage',{package_id:id}),sharePackage:(id,filename=null)=>ask('sharePackage',{package_id:id,filename}),revealPackage:id=>ask('revealPackage',{package_id:id}),shareProject:id=>ask('shareProject',{project:id}),joinProject:()=>ask('joinProject'),removeMember:id=>ask('removeMember',{project:id}),handOverPublishing:id=>ask('handOverPublishing',{project:id}),approvePublish:items=>ask('approvePublish',{items}),approveTrim:(project,session)=>ask('approveTrim',{project,session}),ackAlert:id=>ask('ackAlert',{alert:id}),approveContent:(project,doc,id,note)=>ask('approveContent',note?{project,doc,id,note}:{project,doc,id}),clearTrim:(project,session)=>ask('clearTrim',{project,session}),rejectContent:(project,doc,id,note)=>ask('rejectContent',note?{project,doc,id,note}:{project,doc,id}),rejectEpisodePost:(project,id,reason)=>ask('rejectEpisodePost',reason?{project,id,reason}:{project,id}),pickBroll:(search,candidate,by)=>ask('pickBroll',{search,candidate,by}),acceptProposal:id=>ask('acceptProposal',{proposal:id}),confirmRelayed:(key,event)=>ask('confirmRelayed',{artifact:key,event}),undoRelayed:(key,event)=>ask('undoRelayed',{artifact:key,event}),boardWrite:(method,path,body)=>ask('boardWrite',{method,path,body})}),writable:false});
Object.defineProperty(window,'__aveNativeReply',{value:(id,ok,data)=>{const p=pending.get(id);if(!p)return;pending.delete(id);if(ok){p.resolve(data);return;}const e=new Error(data.error||'Native action cancelled');if(data.status)e.status=data.status;p.reject(e);}});
})();
'''


def main(*, fixture=False):
    if not fixture and agent_markers_set(): raise SystemExit('Native launch refused: an agent marker is set. Open the application yourself in Finder.')
    # Framework imports only after the production marker gate. Headless dispatch
    # never imports this GUI implementation.
    import AppKit as A
    import Foundation as F
    import WebKit as W
    import objc
    from .desktop_files import import_library, import_selected, package_file, save_verified, NativeFileWork
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

    def pick_project(folders):
        """A folder of projects: ask which one, rather than making the person find it."""
        box=A.NSAlert.alloc().init();box.setMessageText_('Choose a project');box.setInformativeText_(f'{len(folders)} projects are in this folder.')
        box.addButtonWithTitle_('Open');box.addButtonWithTitle_('Cancel')
        menu=A.NSPopUpButton.alloc().initWithFrame_pullsDown_(((0,0),(320,26)),False);menu.addItemsWithTitles_([f.name for f in folders]);box.setAccessoryView_(menu)
        return folders[menu.indexOfSelectedItem()] if box.runModal()==A.NSAlertFirstButtonReturn else None

    class Controller(F.NSObject):
        def init(self):
            self=objc.super(Controller,self).init()
            self.session=None;self.window=None;self.web=None;self.busy=False;self.cancelled=threading.Event();self.picker=None;self.sheet=None
            self.tools=[];self.reply_to={}         # the More tools windows (window, web view), and which web view asked each request
            self.file_work=NativeFileWork(on_main)
            self.updater=None          # desktop_updates.DesktopUpdater: Sparkle's Check for Updates, and when it may restart
            return self

        @objc.python_method
        def reply(self, rid, ok, result):
            if rid is not None and rid==self.sheet:self.sheet=None      # the open approval sheet has its answer
            web=self.reply_to.pop(rid,None) or self.web
            if rid and web:
                script='window.__aveNativeReply('+','.join(json.dumps(x) for x in (rid,ok,result))+');'
                web.evaluateJavaScript_completionHandler_(script, None)

        @objc.python_method
        def start(self, path=None):
            self.file_work.close();self.file_work=NativeFileWork(on_main);self.sheet=None
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
            self.load(self.session.server.app_url())     # the Room and the Workspace

        @objc.python_method
        def load(self, url):
            if self.web:self.web.loadRequest_(F.NSURLRequest.requestWithURL_(F.NSURL.URLWithString_(url)))
        def room_(self,sender):
            if self.session:self.load(self.session.server.app_url())
        def moreTools_(self,sender):
            if self.session:self.open_tools('#/pipeline')

        @objc.python_method
        def open_tools(self,route):
            """More tools: the four older tools kept unchanged (Production, Media tools, Feed planner, Post editor), in a window of
            their own on the same loopback server, with the same bridge. Its native requests are answered in that window."""
            if not TOOL_ROUTE.fullmatch(route or ''):raise ValueError('Choose one of the tools in the More tools menu')
            cfg=W.WKWebViewConfiguration.alloc().init()
            cfg.setWebsiteDataStore_(self.web.configuration().websiteDataStore() if self.web else W.WKWebsiteDataStore.nonPersistentDataStore())
            content=W.WKUserContentController.alloc().init()
            content.addScriptMessageHandler_name_(self,'aveNative')
            content.addUserScript_(W.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(BRIDGE_SCRIPT,W.WKUserScriptInjectionTimeAtDocumentStart,True))
            cfg.setUserContentController_(content)
            web=W.WKWebView.alloc().initWithFrame_configuration_(((0,0),(1280,860)),cfg)
            web.setNavigationDelegate_(self);web.setUIDelegate_(self)
            win=A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(((160,140),(1280,860)),A.NSWindowStyleMaskTitled|A.NSWindowStyleMaskClosable|A.NSWindowStyleMaskResizable|A.NSWindowStyleMaskMiniaturizable,A.NSBackingStoreBuffered,False)
            win.setReleasedWhenClosed_(False);win.setTitle_('AVE Studio · More tools');win.setContentView_(web);win.makeKeyAndOrderFront_(None)
            self.tools=[(w,v) for w,v in self.tools if w.isVisible()]+[(win,web)]
            web.loadRequest_(F.NSURLRequest.requestWithURL_(F.NSURL.URLWithString_(self.session.server.tools_url()+route)))

        def webView_decidePolicyForNavigationAction_decisionHandler_(self, web, action, decision):
            url=str(action.request().URL().absoluteString())
            if web is not self.web and urlsplit(url).path.startswith('/app'):     # "Back to the Workspace" in a More tools window
                decision(W.WKNavigationActionPolicyCancel)
                if self.window:self.window.makeKeyAndOrderFront_(None)
                return
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
                if rid:
                    self.reply_to[rid]=message.webView()
                    while len(self.reply_to)>256:self.reply_to.pop(next(iter(self.reply_to)))   # requests that never answer do not pile up
                if action=='openTools':
                    if set(args)!={'route'}:raise ValueError('Choose one of the tools in the More tools menu')
                    self.open_tools(args['route']);self.reply(rid,True,{});return
                if action in CONNECTION_ACTIONS:self.connection_action(action,args,rid)
                elif action in COLLABORATION_ACTIONS:self.collaboration_action(action,args,rid)
                elif action in SHARING_ACTIONS:self.sharing_action(action,sharing_args(action,args),rid)
                elif action in APPROVAL_ACTIONS:self.approval_action(action,approval_args(action,args),rid)
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
                elif action=='import':self.import_files(rid)
                elif action=='importLibrary':self.import_library(rid)
                else:self.diagnostics_(None);self.reply(rid,True,{})
            except Exception as exc:self.reply(locals().get('rid'),False,{'error':str(exc),**({'status':exc.status} if isinstance(getattr(exc,'status',None),int) else {})})

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
        def sharing_action(self, action, pid, rid):
            if fixture:raise ValueError('Sample mode cannot share projects')
            if self.busy or self.file_work.busy:raise ValueError('Wait for the current native operation')
            from .syncthing import members as M
            session=self.session
            def cancelled(what='Sharing cancelled'):self.reply(rid,False,{'error':what})
            def run(operation,then):
                def finished(result,error):
                    if session is not self.session:return
                    if error:self.reply(rid,False,{'error':error});return
                    try:then(result)
                    except Exception as exc:self.reply(rid,False,{'error':str(exc)})
                self.file_work.start(lambda cancel,report:operation(cancel),finished,critical=False)
            def code_sheet(title,detail,check,on_ok,on_cancel):
                """Ask for the code the other person reads out, until it matches, they cancel, or it expires."""
                while True:
                    typed=alert(title,detail,True,True)
                    if typed is None:on_cancel();return
                    try:result=check(typed)
                    except M.MemberError as exc:
                        if 'does not match' in str(exc) or 'six digits' in str(exc):detail=str(exc)+'\n\n'+detail.split('\n\n')[-1];continue
                        raise
                    on_ok(result);return
            if action=='joinProject':
                text=alert('Join a shared project','Paste the invitation you were sent. It works for 10 minutes after it was made, and only for one project.',True,True)
                if text is None:cancelled();return
                inv=M.parse_invitation(text);who=inv['inviter']['person'];title=inv['title'] or 'the project'
                if not alert(f'Join {title}?',f'{who} invites you to {title}. You will keep {M.MEDIA_WORDS[inv["invitee"]["media"]]}.\n\nNext, choose where to keep it on this computer. A folder called {title} is made there.',True):cancelled();return
                panel=A.NSOpenPanel.openPanel();panel.setCanChooseDirectories_(True);panel.setCanChooseFiles_(False);panel.setCanCreateDirectories_(True);panel.setAllowsMultipleSelection_(False)
                panel.setMessage_(f'Choose where to keep {title}');panel.setPrompt_('Keep it here')
                panel.setDirectoryURL_(F.NSURL.fileURLWithPath_(str(session.server.app.studio.sp.root)))
                if panel.runModal()!=A.NSModalResponseOK:cancelled();return
                parent=Path(str(panel.URL().path()))
                def accepted(project,note=''):
                    mgr=session.sync()
                    if not alert(f'Read this code to {who}',note+invitee_code_words(who,M.join_code(project)),True):
                        M.cancel_join(project,manager=mgr);cancelled('Joining cancelled. Nothing was shared.');return
                    def finished(result,error):
                        if session is not self.session:return
                        if error and 'not confirmed yet' in error:accepted(project,error+'\n\n');return
                        if error:self.reply(rid,False,{'error':error});return
                        self.reply(rid,True,{'state':'joined','title':title,'folder':result.root.name})
                    self.file_work.start(lambda cancel,report:M.confirm_join(project,manager=mgr),finished)
                run(lambda cancel:M.accept(text,parent,manager=session.sync()),accepted);return
            project=session.project(pid);title=project.title or project.root.name
            if action=='shareProject':
                box=A.NSAlert.alloc().init();box.setMessageText_(f'Share {title}');box.setInformativeText_('Who are you inviting, and how much media should they keep? They need AVE Studio and Tailscale.')
                box.addButtonWithTitle_('Make invitation');box.addButtonWithTitle_('Cancel')
                view=A.NSView.alloc().initWithFrame_(((0,0),(340,62)))
                name=A.NSTextField.alloc().initWithFrame_(((0,34),(340,24)));name.setPlaceholderString_('Their name, for example Tomas')
                media=A.NSPopUpButton.alloc().initWithFrame_pullsDown_(((0,0),(340,26)),False);media.addItemsWithTitles_(['Review proxies plus what they work on','All the media'])
                view.addSubview_(name);view.addSubview_(media);box.setAccessoryView_(view);box.window().setInitialFirstResponder_(name)
                if box.runModal()!=A.NSAlertFirstButtonReturn:cancelled();return
                person=str(name.stringValue()).strip();choice=('proxies','full')[media.indexOfSelectedItem()]
                def made(text):
                    sheet=A.NSAlert.alloc().init();sheet.setMessageText_(f'Invitation for {person} is ready');sheet.setInformativeText_(share_words(person,title,choice))
                    sheet.addButtonWithTitle_('Copy invitation');sheet.addButtonWithTitle_('Cancel')
                    if sheet.runModal()!=A.NSAlertFirstButtonReturn:M.cancel_invitation(project.root);cancelled();return
                    board=A.NSPasteboard.generalPasteboard();board.clearContents();board.setString_forType_(text,A.NSPasteboardTypeString)
                    self.window.setTitle_(f'AVE Studio · Waiting for {person} to paste the invitation · Cancel File Operation to stop')
                    def answered(found):
                        self.window.setTitle_('AVE Studio')
                        mgr=session.sync()
                        code_sheet(f'Type the code {person} reads to you',inviter_code_words(person,len(found)),
                                   lambda typed:M.confirm_invitee(project.root,typed,manager=mgr),
                                   lambda _:self.reply(rid,True,{'state':'shared','person':person,'title':title}),
                                   lambda:(M.cancel_invitation(project.root),cancelled('Sharing cancelled. Nothing was shared.')))
                    run(lambda cancel:M.wait_for_answers(project.root,manager=session.sync(),cancelled=cancel),answered)
                run(lambda cancel:M.invite(project.root,person,choice,manager=session.sync()),made);return
            others=[m for m in M.members(project.root) if project.device.id not in m['devices']]
            if action=='removeMember':others=[m for m in others if not m['publisher']]
            if not others:raise ValueError('There is nobody else on this project to choose.' if action=='handOverPublishing' else 'There is nobody on this project you can remove. The computer that publishes stays until publishing is handed over.')
            box=A.NSAlert.alloc().init()
            box.setMessageText_(f'Remove someone from {title}' if action=='removeMember' else f'Hand over publishing for {title}')
            box.setInformativeText_('Choose the person.');box.addButtonWithTitle_('Continue');box.addButtonWithTitle_('Cancel')
            pick=A.NSPopUpButton.alloc().initWithFrame_pullsDown_(((0,0),(320,26)),False);pick.addItemsWithTitles_([str(m['person']) for m in others]);box.setAccessoryView_(pick)
            if box.runModal()!=A.NSAlertFirstButtonReturn:cancelled();return
            person=str(others[pick.indexOfSelectedItem()]['person'])
            approvals=session.approvals
            keys=approvals.sharing_keys(action,pid)
            # Both change authority (who publishes, who is on the project), so both take the device-owner check bound to this act
            # on this project, spent once by this native code (desktop_approvals.run_sharing), like every other such act.
            if action=='removeMember':
                if not alert(f'Remove {person}?',removal_words(person,title),True):cancelled('Nobody was removed');return
                def checked(grant):
                    try:
                        if session is not self.session:return
                        if not grant:raise ValueError('Device-owner authentication failed or was cancelled. Nobody was removed.')
                        run(lambda cancel:approvals.run_sharing(action,pid,grant,lambda:M.remove(project.root,person,manager=session.sync())),
                            lambda said:self.reply(rid,True,{'state':'removed','person':person,'message':said}))
                    except Exception as exc:self.reply(rid,False,{'error':str(exc)});session.authority.cancel()
                session.authority.cancel();session.authority.request(f'Remove {person} from {title}',checked,action=action,keys=keys)
                return
            if not alert(f'Hand publishing to {person}?',handover_words(person,title),True):cancelled('Publishing stays with this computer');return
            def handed(grant):
                try:
                    if session is not self.session:return
                    if not grant:raise ValueError('Device-owner authentication failed or was cancelled. Publishing stays with this computer.')
                    said=approvals.run_sharing(action,pid,grant,lambda:M.hand_over(project.root,person,device_id=project.device.id))
                    self.reply(rid,True,{'state':'handed_over','person':person,'message':said})
                except Exception as exc:self.reply(rid,False,{'error':str(exc)})
                finally:session.authority.cancel()
            session.authority.cancel();session.authority.request(f'Hand publishing for {title} to {person}',handed,action=action,keys=keys)

        @objc.python_method
        def approval_action(self,action,args,rid):
            """A dedicated sheet: the exact words again, from the server's own reading, then the device-owner check, which mints a
            grant for exactly this act and these targets. Approvals also take the PIN. Only one sheet is open at a time."""
            if fixture:raise ValueError('Sample mode cannot approve anything')
            refuse_agent()
            session=self.session;approvals=session.approvals
            if action=='boardWrite':                    # the person's own board write: theirs at once (even while a file
                req=approvals.board_request(args)       # operation runs), or a sheet and the device-owner check
                if req['kind']=='app':self.reply(rid,True,approvals.board_write(req));return
            if self.busy or self.file_work.busy:raise ValueError('Wait for the current native operation')
            if action=='undoRelayed':self.reply(rid,True,approvals.undo_relayed(args));return
            if action in ('rejectContent','rejectEpisodePost'):self.reply(rid,True,approvals.app_reject(action,args,session.person()));return
            if self.sheet is not None:raise ValueError('Another approval sheet is open. Finish or cancel it first.')
            if action=='boardWrite':pass
            elif action=='approvePublish':req=approvals.publish_request(args['items'])
            elif action=='approveTrim':req=approvals.trim_request(args['project'],args['session'])
            else:req=approvals.protected_request(action,args)
            self.sheet=rid
            started=time.monotonic()
            if not alert(req['title'],req['detail'],True):self.reply(rid,False,{'error':'Cancelled. Nothing was done.'});return
            shown=int((time.monotonic()-started)*1000)     # how long the exact words were on screen: the studio wants 2 seconds
            root=str(session.server.app.ep.root)
            if action not in PIN_ACTIONS:
                def done(grant):
                    try:
                        if session is not self.session:return
                        if not grant:raise ValueError('Device-owner authentication failed or was cancelled. Nothing was done.')
                        self.reply(rid,True,approvals.run_protected(req,grant,by=session.person()))
                    except Exception as exc:self.reply(rid,False,{'error':str(exc)})
                    finally:session.authority.cancel()
                session.authority.cancel();session.authority.request(req['title'].rstrip('?')+' in '+root,done,action=req['action'],keys=req['keys'])
                return
            def approve(grant):
                try:
                    if session is not self.session:return
                    pin=alert('Type the approval PIN','Type the PIN this computer just showed you. Never give it to an agent.',True,True,True)
                    if pin is None:raise ValueError('Approval cancelled. Nothing was approved.')
                    if req['kind']=='publish':
                        typed=None
                        if req['typed']:
                            typed={}
                            for x in req['items']:
                                t=alert('Type the start of the approval code','Type at least 8 characters of:\n'+x['sha'],True,True)
                                if t is None:raise ValueError('Approval cancelled. Nothing was approved.')
                                typed[x['post']+'/'+x['variant']]=t
                        result=approvals.approve_publish(req,grant,pin,shown,by=session.person(),confirm=typed)
                    else:result=approvals.approve_trim(req,grant,pin)
                    self.reply(rid,True,result)
                except Exception as exc:self.reply(rid,False,{'error':str(exc)})
                finally:session.authority.cancel()        # one check, one act: nothing is left behind
            n=len(req.get('items') or [])
            what=('this post' if n==1 else f'these {n} posts') if req['kind']=='publish' else 'the trim for '+req['session']
            self.unlock(rid,approve,purpose=('Check that it is you','macOS asks for your password or Touch ID next, then shows the approval PIN. Approving '+what+' sends nothing now.','Approve '+what+' in '+root),action=req['action'],keys=req['keys'])

        @objc.python_method
        def connection_action(self,action,args,rid):
            if self.busy or self.file_work.busy:raise ValueError('Wait for the current native operation')
            session=self.session;coordinator=session.connections
            prepared=coordinator.prepare(action,args,rid);token=prepared['token']
            session.authority.cancel();session.server.app.views.clear()
            if not alert('Confirm connection action',prepared['detail'],True):
                coordinator.cancel(token);self.reply(rid,False,{'error':'Action cancelled; nothing changed'});return
            def completed(grant):
                if session is not self.session:coordinator.cancel(token);return
                if not grant:coordinator.cancel(token);self.reply(rid,False,{'error':'Device-owner authentication failed or was cancelled'});return
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
                def run():
                    with session.authority.use(grant,'connection:'+action):return coordinator.execute(token,values)
                try:self.file_work.start(lambda cancel,report:run(),finished,critical=True)
                except Exception:
                    coordinator.cancel(token);session.authority.cancel();raise
            session.authority.request('Confirm '+action+' for '+str(session.server.app.ep.root),completed,action='connection:'+action)

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
                def confirmed(grant):
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
                        def run(cancel):
                            with session.authority.use(grant,'confirmEpisode',(token,)):return checkpoint.confirm_native(token,pin,cancelled=cancel)
                        owner.start(lambda cancel,report:run(cancel),finished,committed_on_return=True)
                    except Exception as exc:
                        if session is self.session and owner is self.file_work and not owner.closed:
                            self.window.setTitle_('AVE Studio');self.reply(rid,False,{'error':str(exc)})
                if session is self.session and owner is self.file_work and not owner.closed:self.unlock(rid,confirmed,None,action='confirmEpisode',keys=(token,))
                return
            if action=='revokeEpisode':
                if set(args)!={'episode'}:raise ValueError('Choose a registered episode')
                row=ER.get(session.server.app.studio.sp,args['episode'])
                if not alert('Disconnect episode',row['label']+'\nStop all new app reads and writes. Existing files are preserved.',True):self.reply(rid,False,{'error':'Disconnect cancelled'});return
                ER.revoke(session.server.app.studio,args['episode']);self.reply(rid,True,{'revoked':True});return
            if set(args)!=(set() if action=='selectEpisode' else {'episode'}):raise ValueError('Only a registered reconnect identity is accepted')
            panel=A.NSOpenPanel.openPanel();panel.setCanChooseDirectories_(True);panel.setCanChooseFiles_(False);panel.setCanCreateDirectories_(False);panel.setAllowsMultipleSelection_(False)
            panel.setMessage_('Choose a project folder, or a folder of projects. Original recordings stay read-only; edits and exports are saved in the project.')
            if panel.runModal()!=A.NSModalResponseOK:self.reply(rid,False,{'error':'Episode selection cancelled'});return
            root=ER.project_root(panel.URL().path(),pick_project)
            if root is None:self.reply(rid,False,{'error':'Episode selection cancelled'});return
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
        def unlock(self,rid,after,purpose,action,keys=()):
            if fixture:self.reply(rid,False,{'error':'Sample mode cannot activate protected authority'});return
            # A cancelled contextual prompt also revokes earlier session authority.
            self.session.authority.cancel()
            self.session.server.app.views.clear()
            title,detail,reason=purpose or ('Check that it is you',f'{self.session.server.app.ep.root}\nDevice-owner authentication for this one act. It does not approve or send content.','Confirm in '+str(self.session.server.app.ep.root))
            if not alert(title,detail,True):
                self.reply(rid,False,{'error':'Authentication cancelled'});return
            current=self.session
            def completed(grant):
                if current is not self.session:return
                if not grant:self.reply(rid,False,{'error':'Device-owner authentication failed or was cancelled'});return
                # PIN is only ever sent to AppKit, not the webview or protocol.
                alert('Approval PIN',current.server.app.pin.reveal()+'\nType this PIN in the next sheet. Never give it to an agent.')
                # The check is bound to the one act that asked for it; on its own (the old Unlock Review) it grants nothing.
                after(grant)
            current.authority.request(reason,completed,action=action,keys=keys)

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
                    def record(grant):
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
                            self.file_work.start(lambda cancel,report:session.record(request,pin,grant),finished,critical=True)
                        except Exception as exc:self.reply(rid,False,{'error':str(exc)})
                    self.unlock(rid,record,None,action='recordManualPublication',keys=(args['package_id'],))
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
        def import_library(self,rid):
            # WKWebView hands a file input nothing back, so the Library's files come through a native panel.
            if self.busy or self.file_work.busy:raise ValueError('Wait for the current file operation to finish')
            panel=A.NSOpenPanel.openPanel();panel.setCanChooseFiles_(True);panel.setCanChooseDirectories_(False);panel.setAllowsMultipleSelection_(True)
            panel.setMessage_('Choose images, video, audio or PDFs. Your originals stay where they are.')
            if panel.runModal()!=A.NSModalResponseOK:self.reply(rid,True,{'added':[]});return
            chosen=[Path(str(u.path())) for u in panel.URLs()];session=self.session
            self.file_work.start(lambda cancel,report:{'added':import_library(session.server.app.studio,chosen)},lambda result,error:self.reply(rid,not bool(error),{'error':error} if error else result))

        def import_files(self,rid=None):
            from .updates import refuse_if_draining
            refuse_if_draining()
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
        def checkForUpdates_(self,sender):
            if self.updater:self.updater.check()
        def updateTick_(self,timer):
            if self.updater:self.updater.poll()

    controller=Controller.alloc().init()
    menu=A.NSMenu.alloc().init();app.setMainMenu_(menu)
    from .desktop_updates import DesktopUpdater
    def update_waits(reasons):
        if controller.window:controller.window.setTitle_(('AVE Studio · The update installs when this finishes: '+'; '.join(reasons)) if reasons else 'AVE Studio')
    controller.updater=DesktopUpdater(controller,lambda title,detail:alert(title,detail),update_waits,
                                      ask=lambda title,detail:bool(alert(title,detail,True)),on_main=on_main)
    if not fixture:controller.updater.start()
    for title,entries in menu_entries(controller.updater.title):
        item=A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title,None,'');sub=A.NSMenu.alloc().initWithTitle_(title);item.setSubmenu_(sub);menu.addItem_(item)
        for label,selector,key in entries:
            child=A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label,selector,key);child.setTarget_(controller);sub.addItem_(child)
    # Sparkle holds a finished download here until nothing would be cut short (updates.safe_to_restart).
    F.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(5.0,controller,'updateTick:',None,True)
    if frozen() and not fixture:
        def refresh():         # first launch of a new version: the bridge files again, off the main thread (it runs an installer)
            try:
                from .desktop_install import refresh_after_update
                from .updates import status as update_status
                refreshed=refresh_after_update(update_status()['build'])
                if refreshed and 'failed' in str(refreshed.get('resolve_bridge',{}).get('status','')):
                    on_main(lambda:alert('The Resolve bridge was not refreshed',refreshed['resolve_bridge']['status']+'\nRun AVE Studio --install-cli to try again.'))
            except Exception as exc:
                problem=str(exc);on_main(lambda:alert('Support files were not refreshed',problem))
        threading.Thread(target=refresh,daemon=True,name='ave-refresh-support-files').start()
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
