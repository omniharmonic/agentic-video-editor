"""App-owned synthetic WK → coordinator → private state rehearsal.

This entrypoint creates its own temporary studio and fake Keychain/service/auth
objects. It cannot select a real workspace or grant production authority. It
never clears agent markers, changes OS auth, installs jobs or performs a send.
"""
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile
import threading
import time


def run(destination):
    import AppKit as A
    import Foundation as F
    import WebKit as W
    import objc
    from unittest.mock import patch
    from .desktop_native import BRIDGE_SCRIPT,native_message
    from .desktop_connections import Coordinator
    from .desktop_service import Service
    from .desktop_files import NativeFileWork
    from .desktop_runtime import digest
    from .studio.project import StudioProject
    from .studio.service import open_studio
    from .studio.ui.server import StudioServer
    from .studio import connections as C
    from .platforms.accounts import save_account
    output=Path(destination);output.mkdir(parents=True,exist_ok=False)
    checks=[];state={'done':False,'error':None};start=time.monotonic();main_id=threading.get_ident()
    class Vault:
        def __init__(self):self.values={}
        def get(self,k):return self.values.get(k)
        def set(self,k,v):self.values[k]=v
        def remove(self,k):self.values.pop(k,None)
    class Auth:
        def __init__(self):self.active=False
        def require(self):
            if not self.active:raise ValueError('Synthetic authentication refused')
    class Adapter:
        def __init__(self,root):self.root=root;self.loaded=False;self.arguments=None
        def path(self,label):return self.root/(label+'.plist')
        def state(self,label):return {'loaded':self.loaded,'running':False,'program':self.arguments[0] if self.arguments else None,'arguments':self.arguments}
        def load(self,label):
            import plistlib
            self.arguments=plistlib.loads(self.path(label).read_bytes())['ProgramArguments'];self.loaded=True
        def unload(self,label,*,expected=None):
            if self.loaded and (not expected or self.arguments!=expected):raise ValueError('Unrecognized synthetic job')
            self.loaded=False
    class Transport:
        def request(self,method,url,**kw):
            if method!='GET' or url!='https://graph.instagram.com/v26.0/me':raise ValueError('Unexpected synthetic provider request')
            return SimpleNamespace(status_code=200,iter_content=lambda n:[b'{"id":"app1","user_id":"ig1"}'],close=lambda:None)
    with tempfile.TemporaryDirectory(prefix='ave-connections-native-') as temp,patch('ave.publish.safety.approval_key',lambda:b'synthetic-native-fixture-key'):
        sp=StudioProject(Path(temp)/'studio');sp.create(title='Synthetic Connections only')
        save_account(sp,{'id':'synthetic','platform':'instagram','mode':'graph','label':'Synthetic account','credentials':{'ref':'isolated-native-fixture','ig_user_id':'ig1'},'connection':{'product':'instagram_login','root_revision':'a'*32}})
        studio=open_studio(sp.root,publishers={},host=None);studio.native_authoring=True
        authority=Auth();server=StudioServer(studio,native_authority=authority);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        vault=Vault();session=SimpleNamespace(server=server,fixture=False,closed=False,authority=authority)
        service=Service(sp,adapter=Adapter(Path(temp)/'LaunchAgents'),exe=Path(sys.executable))
        coordinator=Coordinator(session,keychain=vault,reader_factory=lambda:C.Reader(Transport()),service=service)
        file_work=NativeFileWork(lambda f:F.NSOperationQueue.mainQueue().addOperationWithBlock_(f))
        app=A.NSApplication.sharedApplication();app.setActivationPolicy_(A.NSApplicationActivationPolicyAccessory)
        class Receiver(F.NSObject):
            def userContentController_didReceiveScriptMessage_(self,controller,message):
                try:
                    frame=message.frameInfo();rid,action,args=native_message(message.body(),str(frame.request().URL().absoluteString()),server.url(),frame.isMainFrame())
                    prepared=coordinator.prepare(action,args,rid)
                    # Test-only auth object, never NativeAuthority or an OS callback.
                    authority.active=rid!='1'
                    values={}
                    if action=='connectionCredentials':
                        secure=A.NSSecureTextField.alloc().initWithFrame_(((0,0),(300,24)));secure.setStringValue_('synthetic-native-secret')
                        values={'token':str(secure.stringValue())};checks.append({'check':'native secure widget owns synthetic credential','class':str(secure.className())})
                    def finished(result,error):
                        checks.append({'check':action,'request':rid,'success':not bool(error),'main_queue_callback':threading.get_ident()==main_id})
                        script='window.__aveNativeReply('+','.join(json.dumps(x) for x in (rid,not bool(error),{'error':error} if error else result))+');'
                        web.evaluateJavaScript_completionHandler_(script,None)
                    file_work.start(lambda cancel,report:coordinator.execute(prepared['token'],values),finished,critical=True)
                except Exception as e:state['error']=str(e);state['done']=True
            def webView_didFinishNavigation_(self,webview,navigation):
                script="""(async()=>{
                const get=()=>fetch('/api/studio/connections').then(r=>r.json());
                let a=(await get()).accounts[0];
                try{await aveNative.connectionAction('connectionCredentials',{account:a.id,revision:a.revision});throw Error('refusal missing');}catch(e){if(!e.message.includes('refused'))throw e;}
                await aveNative.connectionAction('connectionCredentials',{account:a.id,revision:a.revision});
                a=(await get()).accounts[0];await aveNative.connectionAction('connectionVerify',{account:a.id,revision:a.revision});
                let d=await get();await aveNative.connectionAction('serviceStart',{revision:d.service.revision});
                d=await get();await aveNative.connectionAction('serviceStop',{revision:d.service.revision});
                window.fixtureDone={ok:true,secretLeak:document.documentElement.innerHTML.includes('synthetic-native-secret')};
                })().catch(e=>window.fixtureDone={ok:false,error:e.message});"""
                webview.evaluateJavaScript_completionHandler_(script,None)
        receiver=Receiver.alloc().init();content=W.WKUserContentController.alloc().init();content.addScriptMessageHandler_name_(receiver,'aveNative')
        content.addUserScript_(W.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(BRIDGE_SCRIPT,W.WKUserScriptInjectionTimeAtDocumentStart,True))
        cfg=W.WKWebViewConfiguration.alloc().init();cfg.setUserContentController_(content)
        web=W.WKWebView.alloc().initWithFrame_configuration_(((0,0),(1024,850)),cfg);web.setNavigationDelegate_(receiver)
        window=A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(((0,0),(1024,850)),A.NSWindowStyleMaskTitled,A.NSBackingStoreBuffered,False);window.setContentView_(web)
        web.loadRequest_(F.NSURLRequest.requestWithURL_(F.NSURL.URLWithString_(server.url()+'#/connections')))
        try:
            while not state['done'] and time.monotonic()-start<45:
                F.NSRunLoop.currentRunLoop().runUntilDate_(F.NSDate.dateWithTimeIntervalSinceNow_(.1))
                if not web.isLoading():
                    def result(value,error):
                        if value:state.update(done=True,result=dict(value))
                    web.evaluateJavaScript_completionHandler_('window.fixtureDone || null',result)
            if not state['done']:state['error']='Synthetic WK journey timed out'
            public=C.overview(studio);row=public['accounts'][0]
            state['synthetic_service_stopped']=not service.adapter.loaded and service.snapshot()['state']=='stopped';
            state['public_verified']=row['verified'];state['secret_leak']='synthetic-native-secret' in json.dumps(public)
            state['ok']=not state['error'] and state.get('result',{}).get('ok') and row['verified'] and not state['secret_leak'] and state['synthetic_service_stopped']
            state.update(checks=checks,executable=str(sys.executable),executable_sha256=digest(Path(sys.executable)),actual_os_auth=False,actual_keychain=False,live_service_installed=False)
        finally:
            file_work.close();session.closed=True;server.shutdown();server.server_close();thread.join(timeout=5);window.orderOut_(None)
    state['fixture_removed']=not Path(temp).exists()
    (output/'verification.json').write_text(json.dumps(state,indent=2));print(json.dumps(state));return 0 if state['ok'] else 2
