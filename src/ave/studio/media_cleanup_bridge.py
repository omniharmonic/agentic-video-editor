"""Job-bound keyless verification and typed revision publication for Media cleanup."""
from __future__ import annotations
import json
import socket
from .episode_worker_bridge import Parent


class MediaCleanupParent(Parent):
    def __init__(self,studio,job):
        from .episode_workshop import check_descriptor
        from .video_workshop import cleanup_dependencies
        check_descriptor(studio,job)
        if job['operation']!='video-cleanup':raise ValueError('Wrong Media bridge operation')
        self.studio=studio;self.job=json.loads(json.dumps(job));self.sock,self.child=socket.socketpair();self.thread=None;self.scope=None;self.root=None;self.additional={};self.committed=None
        from .video_workshop import project
        from .. import editdoc
        from .workbench import read
        ep,_=project(studio.sp,job['args']['project']);head=editdoc.list_versions(ep,'short')[-1];path=editdoc.latest_path(ep,'short');doc=read(studio.sp,path)
        if head!=job['args']['version'] and any(h.get('request_id')==job['args']['request_id'] and h.get('operation')=='occurrence-cleanup' for h in doc.get('history',[])):
            from ..publish.safety import sha256_file
            self.committed={'project':job['args']['project'],'version':head,'sha256':sha256_file(path),'report':doc.get('cleanup_report'),'review':'not reviewed'}
        self.initial=cleanup_dependencies(studio,job['args'],committed=self.committed)
        from .video_workshop import cleanup_freshness,publish_cleanup_candidate
        if self.committed:self.committed=publish_cleanup_candidate(studio,job,self.initial,committed=self.committed)
        self.fresh=cleanup_freshness(studio,job['args'],self.initial,committed=self.committed)

    def _current(self):
        from .workbench import read,job_path
        from .episode_workshop import check_descriptor
        from .video_workshop import cleanup_freshness
        current_path=job_path(self.studio.sp,self.job['id']);current=read(self.studio.sp,current_path)
        if current_path.with_suffix('.cancel').exists():raise ValueError('Job cancellation requested')
        if current['args']!=self.job['args'] or current['operation']!='video-cleanup' or current['status'] not in ('queued','running'):raise ValueError('Media cleanup job identity or lifecycle changed')
        check_descriptor(self.studio,current)
        if cleanup_freshness(self.studio,current['args'],self.initial,committed=self.committed)!=self.fresh:raise ValueError('Media cleanup license, source or revision dependencies changed')
        return current

    def handle(self,message):
        if set(message)!={'job_id','action','data'} or message['job_id']!=self.job['id'] or not isinstance(message['data'],dict):raise ValueError('Wrong-job or malformed verification request')
        job=self._current();action=message['action'];data=message['data']
        if data:raise ValueError('Media cleanup bridge accepts no client scope')
        if action=='snapshot':return {'rows':[],'root':None,'additional':{}}
        if action=='cleanup-inputs':return self.initial
        if action=='cleanup-status':return self.committed
        if action=='cleanup-register':
            if self.committed:return self.committed
            from .video_workshop import publish_cleanup_candidate
            self.committed=publish_cleanup_candidate(self.studio,job,self.initial,before_publish=self._current)
            return self.committed
        raise ValueError('Action is outside this Media cleanup grant')
