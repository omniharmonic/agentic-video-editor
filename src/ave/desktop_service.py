"""Workspace-owned per-user launchd service and constrained headless tick.

No shell, developer PATH, checkout or GUI startup. Mutations are called only by
native contextual actions. Tests inject adapters and private LaunchAgents roots.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from .studio.connections import atomic, read, digest, directory


def identity(sp):return 'com.ave.studio.'+hashlib.sha256(str(sp.root.resolve()).encode()).hexdigest()[:24]
def record(sp):
    from .studio.production import safe_path
    return safe_path(sp,directory(sp)/'service.json')
def heartbeat(sp):
    from .studio.production import safe_path
    return safe_path(sp,directory(sp)/'service-heartbeat.json')
def executable():return Path(sys.executable).resolve()


def executable_identity(path):
    from .publish.safety import sha256_file
    if not path.is_absolute() or not path.is_file() or not os.access(path,os.X_OK):raise ValueError('Installed executable is missing. Open the installed app and repair Background delivery.')
    return sha256_file(path)


class Launchd:
    def __init__(self,root=None):self.root=Path(root) if root else Path.home()/'Library'/'LaunchAgents'
    def path(self,label):return self.root/(label+'.plist')
    def call(self,*args):
        try:r=subprocess.run(['/bin/launchctl',*args],capture_output=True,timeout=12,check=False)
        except (OSError,subprocess.TimeoutExpired):raise ValueError('The service manager did not respond; retry Diagnostics') from None
        return r
    def state(self,label):
        r=self.call('print',f'gui/{os.getuid()}/{label}')
        if r.returncode:return {'loaded':False,'running':False}
        import re
        s=r.stdout.decode(errors='replace');pid=re.search(r'\bpid = (\d+)',s);exit_code=re.search(r'last exit code = (\d+)',s)
        program=re.findall(r'^\tprogram = (.+)$',s,re.M)
        arguments=re.findall(r'^\targuments = \{\n((?:\t\t[^\n]*\n)*)\t\}',s,re.M)
        argv=arguments[0].splitlines() if len(arguments)==1 else None
        return {'loaded':True,'running':bool(pid),'pid':int(pid.group(1)) if pid else None,'last_exit':int(exit_code.group(1)) if exit_code else None,
                'program':program[0].strip() if len(program)==1 else None,'arguments':[line[2:] for line in argv] if argv is not None else None}
    def load(self,label):
        r=self.call('bootstrap',f'gui/{os.getuid()}',str(self.path(label)))
        if r.returncode:raise ValueError('The service did not start. Check macOS background-item permissions, then Repair service.')
    def unload(self,label,*,expected=None):
        state=self.state(label)
        if state['loaded'] and (not expected or state.get('program')!=expected[0] or state.get('arguments')!=expected):
            raise ValueError('Loaded service ownership is unknown or changed; no job was stopped')
        if state['loaded'] and self.call('bootout',f'gui/{os.getuid()}/{label}').returncode:
            raise ValueError('The service could not stop; Pause delivery is still available')


class Service:
    def __init__(self,sp,*,adapter=None,exe=None):
        from .desktop_runtime import frozen
        self.sp=sp;self.adapter=adapter or Launchd();self.exe=Path(exe) if exe else executable();self.installed=frozen() or exe is not None
    def owned_arguments(self,rec):
        exe=rec.get('executable');mode=rec.get('mode')
        if (not isinstance(exe,str) or not Path(exe).is_absolute() or mode not in ('rehearsal','send')
                or rec.get('workspace')!=str(self.sp.root.resolve()) or rec.get('label')!=identity(self.sp)):
            return None
        args=[exe,'--scheduler','--studio',str(self.sp.root.resolve()),'--mode',mode]
        return args if rec.get('arguments')==args else None
    def require_owned_loaded(self,rec,state):
        args=self.owned_arguments(rec)
        if state.get('loaded') and (not args or state.get('program')!=args[0] or state.get('arguments')!=args):
            raise ValueError('Loaded service ownership is unknown or foreign; no job was changed')
        return args
    def snapshot(self):
        rec=read(record(self.sp));label=identity(self.sp);hb=read(heartbeat(self.sp))
        try:state=self.adapter.state(label)
        except ValueError:state={'loaded':False,'running':False,'unavailable':True}
        status='not_installed';reason='Start the service to rehearse due slots while the app is closed.'
        try:self.require_owned_loaded(rec,state);foreign=False
        except ValueError:foreign=True
        if foreign:status='foreign_loaded_job';reason='A loaded job has unknown or different program/arguments. No lifecycle action will change it.'
        if rec and not foreign:
            if rec.get('workspace')!=str(self.sp.root.resolve()):status='workspace_moved';reason='This service record belongs to another folder. Start a service for this workspace; the previous job has not been changed.'
            elif rec.get('label')!=label:status='identity_mismatch';reason='Service identity differs. Reopen the original workspace before repair.'
            elif not Path(rec.get('executable','')).is_file():status='executable_missing';reason='The application moved or was removed. Repair service from the installed app.'
            elif not os.access(self.sp.root,os.R_OK|os.W_OK):status='permission_revoked';reason='Choose this workspace again in the app to restore folder access.'
            elif rec.get('executable')!=str(self.exe) or rec.get('executable_sha')!=executable_identity(self.exe):status='update_required';reason='The installed app changed. Repair service to use this executable.'
            elif state.get('unavailable'):status='manager_unavailable';reason='macOS did not report service state. Retry Diagnostics; do not infer Running from the saved job.'
            elif rec.get('state')=='failed':status='failed';reason='Installation or launch failed. Repair service after checking background permissions.'
            elif not state.get('loaded'):status='stopped';reason='The service is stopped. Start it to resume scheduled checks.'
            elif state.get('last_exit') not in (None,0):status='failed';reason='The last service run failed. Reopen folder access and inspect Diagnostics.'
            elif hb.get('service')!=label or hb.get('executable_sha')!=rec.get('executable_sha'):status='waiting';reason='Service installed; waiting for its first verified heartbeat.'
            else:
                stamp=hb.get('at');interval=rec.get('interval',60)
                age=time.time()-stamp if type(stamp) in (int,float) else float('inf')
                if type(interval) is not int or not 60<=interval<=900:interval=60
                if age<0 or age>3*interval:status='stale';reason='No recent check. The Mac may be asleep, offline, or background execution may be disabled. Missed slots follow each account’s catch-up policy.'
                elif state.get('running') and state.get('pid')==hb.get('pid'):status='running';reason='The service is running with a recent heartbeat.'
                else:status='waiting';reason='The last scheduled check finished. Waiting for the next interval.'
        return {'state':status,'reason':reason,'loaded':bool(state.get('loaded')),'running':status=='running','label':label,
            'revision':digest(rec),'mode':rec.get('mode','rehearsal'),'interval':rec.get('interval',60),'last_check':hb.get('at'),
            'last_result':hb.get('result'),'executable':rec.get('executable'),'workspace':str(self.sp.root.resolve()),'last_exit':state.get('last_exit')}
    def change(self,operation,expected,*,mode='rehearsal',interval=60):
        from .studio.production import workspace_lock
        with workspace_lock(self.sp):
            old=read(record(self.sp))
            if digest(old)!=expected:raise ValueError('Service changed; reopen Background delivery')
            label=identity(self.sp);path=self.adapter.path(label)
            previous=None
            if old and (old.get('workspace')!=str(self.sp.root.resolve()) or old.get('label')!=label):
                if operation!='start':raise ValueError('The saved job belongs to another workspace; it was not changed. Start a new rehearsal service for this folder.')
                previous=old
                old={};mode='rehearsal'
            loaded=self.adapter.state(label)
            owned=self.require_owned_loaded(old,loaded)
            if path.is_symlink():raise ValueError('Refusing a linked service file')
            if path.exists():
                try:existing=plistlib.loads(path.read_bytes())
                except Exception:raise ValueError('Existing service file is malformed; it was not replaced') from None
                if existing.get('Label')!=label or not owned or existing.get('ProgramArguments')!=owned or existing.get('Program',owned[0])!=owned[0]:raise ValueError('An unrecognized job occupies this identity; it was not replaced')
            if operation in ('stop','remove'):
                self.adapter.unload(label,expected=owned)
                if operation=='remove':path.unlink(missing_ok=True)
                if old:old['state']='removed' if operation=='remove' else 'stopped';atomic(record(self.sp),old)
                return {'state':old.get('state','not_installed'),'message':'Service stopped. Sending permission and approvals were not changed.'}
            if operation not in ('start','repair') or mode not in ('rehearsal','send') or type(interval) is not int or not 60<=interval<=900:raise ValueError('Invalid service operation')
            if not self.installed:raise ValueError('Open the installed AVE Studio app to start its background service')
            sha=executable_identity(self.exe)
            args=[str(self.exe),'--scheduler','--studio',str(self.sp.root.resolve()),'--mode',mode]
            logs=directory(self.sp)/'logs'
            from .studio.desk import _safe_path
            _safe_path(logs,self.sp.root)
            logs.mkdir(parents=True,exist_ok=True,mode=0o700);logs.chmod(0o700)
            for name in ('scheduler.log','scheduler-error.log'):
                p=logs/name
                if p.is_symlink():raise ValueError('Refusing linked service log')
                p.touch(mode=0o600,exist_ok=True);p.chmod(0o600)
            plist={'Label':label,'ProgramArguments':args,'StartInterval':interval,'RunAtLoad':True,'ProcessType':'Background','Umask':0o077,
                'StandardOutPath':str(logs/'scheduler.log'),'StandardErrorPath':str(logs/'scheduler-error.log')}
            self.adapter.unload(label,expected=owned);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            if previous:atomic(directory(self.sp)/('previous-service-'+digest(previous)[:16]+'.json'),previous)
            temp=path.with_suffix('.tmp');temp.write_bytes(plistlib.dumps(plist,sort_keys=True));temp.chmod(0o600);temp.replace(path)
            new={'label':label,'workspace':str(self.sp.root.resolve()),'executable':str(self.exe),'executable_sha':sha,'arguments':args,'interval':interval,'mode':mode,'state':'installing'}
            atomic(record(self.sp),new)
            try:self.adapter.load(label);new['state']='installed'
            except Exception:new['state']='failed';atomic(record(self.sp),new);raise
            atomic(record(self.sp),new)
            return {'state':'installed','mode':mode,'message':'Service installed. Running state waits for an actual process and fresh heartbeat.'}


def run_tick(path,mode,*,publishers=None,host=None):
    """One bounded scheduler pass. launchd owns cadence; no GUI or retry loop."""
    from .humanpresence import agent_markers_set
    from .studio.project import StudioProject
    from .studio.clock import SystemClock
    from .schedule.tick import tick
    from .studio import sendgate
    if mode not in ('rehearsal','send'):raise ValueError('Invalid scheduler mode')
    sp=StudioProject(Path(path));sp.require()
    if mode=='send':
        if agent_markers_set():raise ValueError('Agent sessions cannot run the sending service')
        # Headless sending requires standing permission even under a human TTY.
        if not sendgate.verify(sp).ok:raise ValueError('Automatic posting is not enabled for this workspace')
        if host is None:
            from .studio.connections import load_workspace_host
            host=load_workspace_host(sp)
        if publishers is None:
            from .studio.connections import renew_due_sessions
            renew_due_sessions(sp,SystemClock().now())
            from .platforms.accounts import list_accounts
            from .platforms.registry import adapter_for
            publishers={a.id:adapter_for(a,host=host) for a in list_accounts(sp) if a.mode!='manual'}
    rep=tick(sp,clock=SystemClock(),send=mode=='send',publishers=publishers,host=host)
    summary={'mode':rep.mode,'locked':rep.locked,'paused':rep.paused,'posted':len(rep.posted),'blocked':len(rep.blocked),'missed':len(rep.missed)}
    atomic(heartbeat(sp),{'service':identity(sp),'at':time.time(),'pid':os.getpid(),'executable_sha':executable_identity(executable()),'result':summary})
    return summary
