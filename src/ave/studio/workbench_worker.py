"""Narrow subprocess/frozen-app dispatch entrypoint for durable workbench jobs."""
from __future__ import annotations
import os
import sys
from .workbench import LOCK_FD_ENV, diagnostic, release_lock, execute_operation, job_path, read, write
from .service import open_studio


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (2,4) or (len(args)==4 and args[2]!='--episode-bridge'):
        raise SystemExit('workbench worker requires workspace and job id')
    from . import episode_worker_bridge as bridge
    if len(args)==4:
        from ..publish.safety import forbid_approval_keys
        forbid_approval_keys()
        bridge.CLIENT=bridge.Client(int(args[3]),args[1])
    studio = open_studio(args[0])
    path = job_path(studio.sp, args[1])
    job = read(studio.sp, path)
    if (job['operation'].startswith('episode-') or job['operation']=='video-cleanup' or (job['operation']=='video-kept' and str(job['args'].get('source_id','')).startswith('episode_'))) and bridge.CLIENT is None:raise SystemExit('Episode jobs require a live parent verification bridge')
    if job['operation']=='video-render':
        from .episode_release import is_native_render
        if is_native_render(studio,job['args']) and bridge.CLIENT is None:raise SystemExit('Native kept render requires a live parent verification bridge')
    if job['status'] != 'queued': raise SystemExit('job is not queued')
    from .clock import iso_utc
    job['started'] = iso_utc(studio.now())
    job['status'] = 'running'; write(studio.sp, path, job)
    import signal
    class Cancelled(Exception): pass
    class AuthorityLost(Exception): pass
    def cancelled(signum,frame):
        if bridge.CLIENT and bridge.CLIENT.failure:
            raise AuthorityLost('Episode verification lost: '+bridge.CLIENT.failure+'. Original and completed outputs preserved; reconnect if needed, then retry this job.')
        raise Cancelled('Cancelled; original and completed outputs preserved')
    def timeout(signum, frame): raise TimeoutError('Local operation exceeded its bounded time budget; input and completed outputs are preserved')
    if job['operation'].startswith('episode-') or job['operation'] in ('import','transcribe','video-source','video-render','video-kept','video-cleanup','manuscript','image-transform','design-render','image-split'):
        signal.signal(signal.SIGALRM,timeout);signal.alarm(14400 if job['operation'].startswith('episode-') or job['operation'] in ('import','transcribe','video-source','video-render','video-kept','video-cleanup') else 120)
        signal.signal(signal.SIGTERM,cancelled)
    try:
        if path.with_suffix('.cancel').exists(): raise Cancelled('Cancelled before execution')
        job['result'] = execute_operation(studio, job)
        if path.with_suffix('.cancel').exists(): raise Cancelled('Cancelled; completed output remains available')
        job['status'] = 'done'
    except AuthorityLost as exc:
        job.update(status='interrupted',diagnostics=diagnostic(exc))
    except Cancelled as exc:
        job.update(status='cancelled',diagnostics=diagnostic(exc))
    except (Exception, SystemExit) as exc:
        job.update(status='failed', diagnostics=diagnostic(exc))
    signal.alarm(0)
    if (fd := os.environ.get(LOCK_FD_ENV, '')).isdigit():       # free worker.lock before the job reads as finished
        release_lock(int(fd))
    if bridge.CLIENT:bridge.CLIENT.close();bridge.CLIENT=None
    write(studio.sp, path, job)
    return 0 if job['status'] == 'done' else 2


if __name__ == '__main__':
    raise SystemExit(main())
