"""Opt-in native widget acceptance on a generated, temporary fixture ONLY.

This never changes an auth callback or chooses a caller workspace. It observes
and operates the application's own WKWebView/NSSavePanel widgets. It cannot
unlock review. Not a substitute for human device-owner/Gatekeeper acceptance.
"""
from pathlib import Path
import json
import socket
import time
import sys
import threading
from .desktop_runtime import digest


def run(destination):
    import AppKit as A
    import Foundation as F
    from . import desktop_native as native
    from . import desktop_files
    from .desktop_files import package_file, NativeFileWork, save_verified, checked_digest
    output=Path(destination);output.mkdir(parents=True,exist_ok=False)
    state={'phase':'waiting','started':time.monotonic(),'checks':[]}
    main_thread=threading.get_ident();original_package=desktop_files.package_file
    package_threads=[]
    def observed_package(*args,**kwargs):
        package_threads.append(threading.get_ident())
        return original_package(*args,**kwargs)
    desktop_files.package_file=observed_package
    file_work=NativeFileWork(lambda fn:F.NSOperationQueue.mainQueue().addOperationWithBlock_(fn))
    original=native.NativeSession.__init__
    # main passes path=None positionally; allow exactly that, never other paths.
    def capture(self,path=None,*,fixture=False):
        if path is not None or fixture is not True:raise ValueError('Synthetic native acceptance only')
        original(self,path,fixture=True);state['session']=self
    native.NativeSession.__init__=capture
    def record(name,**detail):
        state['checks'].append({'check':name,**detail})
        (output/'progress.json').write_text(json.dumps({'phase':state['phase'],'checks':state['checks']},indent=2))
    def png(rep,path):
        data=rep.representationUsingType_properties_(A.NSBitmapImageFileTypePNG,{})
        if data is None:raise ValueError('Native snapshot returned no PNG')
        data.writeToFile_atomically_(str(path),True)
    def native_snapshot(view,name):
        rep=view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(),rep)
        png(rep,output/name)
    def buttons(view):
        found=[]
        if isinstance(view,A.NSButton):found.append(view)
        for sub in view.subviews():found.extend(buttons(sub))
        return found
    def download(web):
        url=state['zip_url']
        script="(()=>{const a=document.createElement('a');a.href="+json.dumps(url)+";a.download='synthetic-package.zip';document.body.append(a);a.click();a.remove();return true})()"
        web.evaluateJavaScript_completionHandler_(script,lambda result,error:record('WK download attribute activated',error=str(error) if error else None))
    def tick(timer):
        try:
            app=A.NSApplication.sharedApplication()
            session=state.get('session')
            window=next((w for w in app.windows() if str(w.title()).startswith('AVE Studio')),None)
            modal=app.modalWindow()
            if state['phase']=='closing':
                if modal:modal.cancel_(None)
                elif window:timer.invalidate();window.close()
                return
            if time.monotonic()-state['started']>55:raise RuntimeError('Native widget acceptance timed out at '+state['phase'])
            if not session or not window:return
            state['window']=window;web=window.contentView();modal=app.modalWindow()
            phase=state['phase']
            if phase=='waiting':
                if web.URL() is None or web.isLoading():return
                from .desktop_files import import_selected
                from .studio.workbench import Workbench
                selected=session.server.app.ep.work/'native-retry.md'
                selected.write_text('A complete synthetic sentence for repeated native import and retry.\n')
                first=import_selected(session.server.app.workbench,[selected])
                second=import_selected(session.server.app.workbench,[selected])
                reopened=Workbench(session.server.app.studio)
                third=import_selected(reopened,[selected]);reopened.close()
                try:import_selected(session.server.app.workbench,[selected],cancelled=lambda:True)
                except ValueError:cancelled=True
                else:cancelled=False
                assert first[0]['source_id']==second[0]['source_id']==third[0]['source_id'] and cancelled
                record('Native repeat import and reopened-workbench retry',source_id=first[0]['source_id'],cancelled=cancelled,original_sha256=digest(selected))
                state['phase']='file-work'
                large=session.server.app.ep.work/'native-copy-fixture.zip'
                with large.open('wb') as f:f.truncate(128*1024**2)
                target=large.with_name('native-copy-cancelled.zip')
                state['ticks']=0
                def cancel_progress(n,total):file_work.cancel()
                def cancelled(result,error):
                    if not error or target.exists():state['error']='Copy cancellation failed';state['phase']='failed';return
                    record('Owned native copy cancelled with main queue responsive',error=error,timer_ticks=state['ticks'],no_temporary=not list(target.parent.glob('.ave-save-*')))
                    def complete(result,error):
                        if error:state['error']=error;state['phase']='failed';return
                        record('Owned native copy committed before late cancel retains success',sha256=result,bytes=large.stat().st_size,callback_main_thread=threading.get_ident()==main_thread,late_cancel_requested=file_work.cancelled.is_set(),no_temporary=not list(target.parent.glob('.ave-save-*')))
                        state['phase']='snapshot-start'
                    def copy(cancel,progress):
                        assert threading.get_ident()!=main_thread
                        sha=checked_digest(large,cancelled=cancel)
                        save_verified(large,target,sha,cancelled=cancel,progress=progress)
                        # Only this synthetic harness requests cancellation at
                        # the actual service-return/commit boundary.
                        file_work.cancel()
                        assert digest(target)==sha
                        return sha
                    file_work.start(copy,complete,committed_on_return=True)
                file_work.start(lambda cancel,progress:save_verified(large,target,'unused-on-cancel',cancelled=cancel,progress=progress),cancelled,cancel_progress,committed_on_return=True)
            elif phase=='file-work':
                state['ticks']+=1
            elif phase=='snapshot-start':
                state['phase']='snapshot'
                manifest=next((session.server.app.ep.work/'manual_exports').glob('*/manifest.json'))
                package_id=json.loads(manifest.read_text())['package_id']
                source,sha,m=package_file(session.server.app.ep,package_id)
                state.update(post=m['post'],source=source,sha=sha,zip_url='/files/'+session.server.app.ep.rel(source),port=session.server.port,fixture=str(session.server.app.ep.root))
                def snapshot(image,error):
                    if error:state['error']=str(error);state['phase']='failed';return
                    png(A.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation()),output/'native-workspace.png')
                    record('Actual WK workspace rendered',width=image.size().width,height=image.size().height)
                    state['phase']='media';web.evaluateJavaScript_completionHandler_("location.hash="+json.dumps('#/post/'+state['post']),None)
                web.takeSnapshotWithConfiguration_completionHandler_(None,snapshot)
            elif phase=='media':
                state['phase']='media-check'
                def decoded(value,error):
                    if error or not value:state['phase']='media';return
                    def captured(image,error):
                        if error:state['error']=str(error);state['phase']='failed';return
                        png(A.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation()),output/'native-media.png')
                        record('WK composer media decoded and captured',images=int(value))
                        state['phase']='cancel-panel';download(web)
                    web.takeSnapshotWithConfiguration_completionHandler_(None,captured)
                web.evaluateJavaScript_completionHandler_("Array.from(document.images).filter(i=>i.complete&&i.naturalWidth>100&&i.getBoundingClientRect().width>100).length",decoded)
            elif phase=='cancel-panel'  and modal and hasattr(modal,'setNameFieldStringValue_'):
                native_snapshot(modal.contentView(),'native-save-cancel.png')
                state['phase']='cancelled';modal.cancel_(None)
                record('Actual NSSavePanel cancelled',saved=(output/'native-saved.zip').exists(),package_validation_off_main=bool(package_threads) and all(t!=main_thread for t in package_threads))
            elif phase=='cancelled' and modal is None:
                state['phase']='save-panel';download(web)
            elif phase=='save-panel' and modal and hasattr(modal,'setNameFieldStringValue_'):
                modal.setDirectoryURL_(F.NSURL.fileURLWithPath_(str(output)))
                modal.setNameFieldStringValue_('native-saved.zip')
                native_snapshot(modal.contentView(),'native-save-confirm.png')
                state['phase']='save-ready';state['save_started']=time.monotonic()
                record('Save panel selection configured',directory=str(modal.directoryURL().path()),filename=str(modal.nameFieldStringValue()))
            elif phase=='save-ready' and modal:
                state['phase']='saved'
                event=A.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(A.NSKeyDown,(0,0),0,time.monotonic(),modal.windowNumber(),None,'\r','\r',False,36)
                handled=modal.performKeyEquivalent_(event)
                record('Actual native Return key equivalent',handled=bool(handled))
                if not handled:modal.sendEvent_(event)
            elif phase=='saved' and time.monotonic()-state['save_started']>8 and not (output/'native-saved.zip').exists():
                record('Save acceptance requires human check',reason='OS remote Save view did not accept supported in-app key equivalent; no private APIs or OS permission bypass used')
                state['save_unverified']=True;state['phase']='save-limited';modal.cancel_(None)
            elif phase=='save-limited' and modal is None:
                state['phase']='navigation';state['local_url']=str(web.URL().absoluteString())
                web.loadRequest_(F.NSURLRequest.requestWithURL_(F.NSURL.URLWithString_('https://example.invalid/native-test')))
            elif phase=='saved' and (output/'native-saved.zip').is_file():
                state['save_unverified']=False
                actual=digest(output/'native-saved.zip')
                if actual!=state['sha']:raise ValueError('Native saved ZIP differs from validated package')
                record('Actual NSSavePanel saved exact bytes',sha256=actual,bytes=(output/'native-saved.zip').stat().st_size)
                if modal:
                    native_snapshot(modal.contentView(),'native-save-complete.png')
                    button=next((b for b in buttons(modal.contentView()) if str(b.title())=='OK'),None)
                    if button:button.performClick_(None)
                state['phase']='navigation'
                state['local_url']=str(web.URL().absoluteString())
                web.loadRequest_(F.NSURLRequest.requestWithURL_(F.NSURL.URLWithString_('https://example.invalid/native-test')))
            elif phase=='navigation':
                if str(web.URL().absoluteString())!=state['local_url']:raise ValueError('External native navigation escaped')
                record('WK external navigation denied',url=str(web.URL().absoluteString()).split('?')[0])
                state['phase']='file-navigation'
                web.loadRequest_(F.NSURLRequest.requestWithURL_(F.NSURL.URLWithString_('file:///etc/hosts')))
            elif phase=='file-navigation':
                if str(web.URL().absoluteString())!=state['local_url']:raise ValueError('File navigation escaped')
                record('WK file navigation denied')
                state['phase']='complete';timer.invalidate();window.close()
            elif phase=='failed':raise RuntimeError(state.get('error','Native check failed'))
        except Exception as exc:
            state['error']=str(exc);state['phase']='closing';record('Failure',error=str(exc))
            modal=A.NSApplication.sharedApplication().modalWindow()
            if modal and hasattr(modal,'cancel_'):modal.cancel_(None)
            if not modal and state.get('window'):timer.invalidate();state['window'].close()
    timer=F.NSTimer.timerWithTimeInterval_repeats_block_(.25,True,tick)
    loop=F.NSRunLoop.currentRunLoop()
    loop.addTimer_forMode_(timer,F.NSDefaultRunLoopMode)
    loop.addTimer_forMode_(timer,A.NSModalPanelRunLoopMode)
    try:native.main(fixture=True)
    finally:
        timer.invalidate();file_work.close();native.NativeSession.__init__=original;desktop_files.package_file=original_package
    if state.get('session'):
        closed=state['session'].closed and not Path(state.get('fixture','')).exists()
        with socket.socket() as sock:sock.settimeout(1);refused=sock.connect_ex(('127.0.0.1',state['port']))!=0
        record('Actual window close cleaned server and fixture',session_closed=closed,socket_closed=refused)
        if not closed or not refused:state['error']='Native cleanup failed'
    result={'is_frozen':bool(getattr(sys,'frozen',False)),'executable':sys.executable,'executable_sha256':digest(sys.executable),'ok':state['phase']=='complete' and not state.get('error') and not state.get('save_unverified'),'save_acceptance_verified':not state.get('save_unverified',True),'error':state.get('error'),'checks':state['checks'],
            'scope':'Actual app-owned WKWebView and NSSavePanel automation, generated synthetic workspace only. No real OS authentication or publication. Not human interaction or clean-Mac evidence.',
            'files':{p.name:digest(p) for p in output.iterdir() if p.is_file()}}
    (output/'verification.json').write_text(json.dumps(result,indent=2))
    return 0 if result['ok'] else 2
