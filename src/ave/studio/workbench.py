"""Local source-to-artifact work. Durable typed jobs have no approval authority.

The subprocess entry point is ``ave.studio.workbench_worker``. Packaged launchers
can dispatch ``--workbench-worker ROOT JOB`` to that module's main before GUI
startup. Records contain validated operation arguments, never Python or shell.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

from ..episode import Episode
from ..publish import safety
from ..ui.jobs import child_env
from . import desk, model, production, store
from .clock import iso_utc
from .events import Actor
from .ingest import AUDIO_EXTS, VIDEO_EXTS

TEXT_EXTS = ('.md', '.markdown', '.txt')
DOCUMENT_EXTS = ('.docx', '.pdf')
MAX_UPLOAD = 512 * 1024 * 1024
MAX_JOBS = 500
MAX_SOURCES = 200
JOB_ID = re.compile(r'^job_[a-f0-9]{24}$')
SOURCE_ID = re.compile(r'^recording_[a-f0-9]{64}$')
RECIPES = [{'id': 'quote-post', 'label': 'Selected verbatim quote', 'styles': ['ink', 'paper', 'clay'], 'formats': ['1x1', '4x5', '9x16']}]


class Busy(ValueError):
    code = 'conflict.workbench'


def safe(sp, path):
    desk._safe_path(Path(path), sp.root)
    return production.safe_path(sp, Path(path))


def read(sp, path):
    safe(sp, path)
    return desk._json_file(path, sp.root)


def write(sp, path, obj):
    safe(sp, path)
    data = (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode()
    if len(data) > desk.MAX_STATE_FILE:
        raise ValueError('Workbench state exceeds its bounded storage limit')
    path.parent.mkdir(parents=True, exist_ok=True)
    desk._atomic_bytes(path, data)


def _publish_staged(source, destination):
    """Atomic no-clobber publication, including whole episode directories.

    Unlike os.rename/replace this can never overwrite a concurrently created
    destination. Same-volume macOS/Linux primitives also work without hardlinks.
    """
    from ..atomic import publish
    return publish(source,destination)


def _own_stage(sp, path, marker, identity):
    """A durable sidecar claims only this deterministic staging destination."""
    desk._safe_path(path, sp.root)
    safe(sp, marker)
    if marker.exists():
        if read(sp, marker) != identity: raise ValueError('Staging ownership mismatch; existing files preserved')
    else:
        if path.exists(): raise ValueError('Unowned staging path exists; existing files preserved')
        write(sp, marker, identity)


def _copy_immutable(sp, source, destination, digest):
    source, destination = safe(sp, source), safe(sp, destination)
    if destination.exists():
        if safety.sha256_file(destination) != digest:
            raise ValueError('Existing immutable copy differs; refusing replacement')
        return
    key = hashlib.sha256(sp.rel(destination).encode()).hexdigest()
    part = safe(sp, sp.work / 'production' / 'staging' / 'copies' / (key + '.part'))
    marker = part.with_suffix('.owner.json')
    _own_stage(sp, part, marker, {'v': 1, 'target': sp.rel(destination), 'sha256': digest})
    # Only a matching ownership marker permits truncation of an interrupted
    # derivative. Final source/raw files are never opened for writing.
    if not part.exists() or safety.sha256_file(part) != digest:
        with part.open('wb') as out, source.open('rb') as inp:
            shutil.copyfileobj(inp, out)
            out.flush(); os.fsync(out.fileno())
    if safety.sha256_file(part) != digest: raise ValueError('Staged copy checksum failed; original preserved')
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _publish_staged(part, destination)
    except FileExistsError:
        if safety.sha256_file(safe(sp, destination)) != digest:
            raise ValueError('Existing immutable copy differs; refusing replacement') from None


def _prepare_episode(sp, source):
    ep = episode_for(sp, source)
    if ep.meta_path.exists(): return ep
    if ep.root.exists(): raise ValueError('Managed episode folder exists without ownership; refusing replacement')
    sid = source['source_id']
    stage = sp.work / 'production' / 'staging' / 'episodes' / sid
    marker = stage.with_suffix('.owner.json')
    _own_stage(sp, stage, marker, {'v': 1, 'target': sp.rel(ep.root), 'source_id': sid})
    if stage.exists():
        for base, dirs, files in os.walk(stage, followlinks=False):
            for name in dirs + files: desk._safe_path(Path(base) / name, sp.root)
    staged = Episode(stage)
    if staged.meta_path.exists():
        meta = read(sp, staged.meta_path)
        if meta.get('workbench_source') != sid or meta.get('layout') != 'flat':
            raise ValueError('Staged episode ownership mismatch; preserved for inspection')
    else:
        # Ownership is part of the FIRST metadata document, never a second write
        # after an ordinary episode has already become visible at the final root.
        write(sp, staged.meta_path, {'title': source['label'], 'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
              'layout': 'flat', 'speakers': {}, 'cameras': {}, 'stages': {}, 'workbench_source': sid})
    staged.create()
    ep.root.parent.mkdir(parents=True, exist_ok=True)
    _publish_staged(stage, ep.root)
    return episode_for(sp, source)


def source_index(sp):
    p = sp.work / 'production' / 'sources.json'
    return read(sp, p) if safe(sp, p).exists() else {'v': 1, 'sources': []}


def inventory(sp):
    out = []
    p = sp.work / 'content' / 'writing.json'
    if safe(sp, p).exists():
        data = read(sp, p)
        for f in data.get('files', [])[:MAX_SOURCES]:
            # The index, rather than a client path, is the source of provenance.
            filename = f.get('file', '')
            if not filename or Path(filename).name != filename or '\\' in filename:
                raise ValueError('Invalid indexed writing filename')
            safe(sp, sp.work / 'writing' / filename)
            out.append({'source_id': filename, 'kind': 'writing', 'label': f.get('title') or filename,
                        'sha256': f.get('sha256'), 'sentences': [dict(s) for p in f.get('passages', []) for s in p.get('sentences', [])]})
    for s in source_index(sp)['sources']:
        if not SOURCE_ID.fullmatch(s.get('source_id', '')):
            raise ValueError('Invalid recording source identity')
        path = safe(sp, sp.root / s['file'])
        if not path.is_file():
            raise ValueError('Managed recording is missing')
        row = dict(s)
        row['previews'] = []
        if s.get('ready') and s.get('kind') != 'document':
            from ..audio import analysis_wav_path
            ep = episode_for(sp, s)
            wav = safe(sp, analysis_wav_path(ep, ep.rel(ep.raw / path.name)))
            if s['kind'] == 'video': row['previews'].append({'kind': 'video', 'url': '/files/' + quote(s.get('preview_file',s['file']), safe='/'), 'label': 'Browser preview (original preserved)'})
            if wav.is_file(): row['previews'].append({'kind': 'audio', 'url': '/files/' + quote(sp.rel(wav), safe='/'), 'label': 'Extracted audio preview'})
        out.append(row)
    from .video_workshop import audio_sources
    out.extend(audio_sources(sp))
    return out


def recording(sp, sid):
    return next((s for s in source_index(sp)['sources'] if s['source_id'] == sid), None)


def episode_for(sp, source):
    sid = source['source_id']
    if not SOURCE_ID.fullmatch(sid): raise ValueError('Invalid managed source id')
    root = sp.work / 'production' / 'episodes' / sid
    desk._safe_path(root, sp.root)
    # Managed roots are fixed, flat episodes. Do not accept layout redirection.
    ep = Episode(root)
    if ep.meta_path.exists():
        meta = read(sp, ep.meta_path)
        if meta.get('layout') not in (None, 'flat') or meta.get('workbench_source') != sid:
            raise ValueError('Existing episode is unrelated to this managed recording')
    if root.exists():
        for base, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                desk._safe_path(Path(base) / name, sp.root)
    return ep


def validate_operation(sp, operation, args):
    if operation.startswith('episode-'):
        from .episode_workshop import validate_operation as validate_episode
        return validate_episode(sp,operation,args)
    if operation in ('video-source','video-render','video-kept','video-cleanup'):
        from .video_workshop import validate_operation as validate_video
        return validate_video(sp,operation,args)
    if operation in ('image-transform','design-render','image-split'):
        from .media_workshop import validate_operation as validate_media
        return validate_media(sp, operation, args)
    if operation == 'manuscript':
        production.only(args, {'source_id'})
        src=recording(sp,args.get('source_id'))
        if not src or src.get('kind')!='document': raise ValueError('Choose a manuscript source')
        return src,None
    if operation == 'frame':
        production.only(args, {'post','variant','asset','seconds','expected_rev'})
        rec=store.read_assets(sp).get(args.get('asset'))
        if not rec or rec.get('kind')!='video': raise ValueError('Choose a registered video')
        original=safe(sp,sp.root/rec['file'])
        if safety.sha256_file(original)!=rec['sha256']: raise ValueError('Video bytes changed since registration')
        from ..media import probe_file
        duration=probe_file(original,sp.root)['duration']
        seconds=args.get('seconds')
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0<=seconds<duration: raise ValueError('Frame time must be within the video duration')
        if type(args.get('expected_rev')) is not int or args['expected_rev']<1: raise ValueError('A captured post revision is required')
        post=store.load_post(sp,model.check_id(args.get('post')))
        if args.get('variant'): model.find_variant(post,args['variant'])
        return rec,None
    if operation == 'render':
        production.only(args, {'source_id', 'sentence_id', 'recipe', 'style', 'format'})
        if args.get('recipe') != 'quote-post': raise ValueError('Choose the selected verbatim quote recipe')
        if args.get('style') not in RECIPES[0]['styles']: raise ValueError('Unknown visual style')
        if args.get('format') not in RECIPES[0]['formats']: raise ValueError('Unknown image format')
        src = next((s for s in inventory(sp) if s['source_id'] == args.get('source_id') and s['kind'] == 'writing'), None)
        sentence = next((s for s in (src or {}).get('sentences', []) if s['id'] == args.get('sentence_id')), None)
        if sentence is None: raise ValueError('Choose an indexed source sentence')
        if not 12 <= len(sentence['text']) <= 240: raise ValueError('Choose a complete sentence of 12–240 characters')
        return src, sentence
    if operation in ('import', 'transcribe'):
        production.only(args, {'source_id'} if operation == 'import' else {'source_id', 'speakers'})
        src = recording(sp, args.get('source_id'))
        if src is None: raise ValueError('Choose an uploaded recording')
        safe(sp, sp.root / src['file'])
        episode_for(sp, src)
        if operation == 'transcribe':
            if type(args.get('speakers')) is not int or not 1 <= args['speakers'] <= 16:
                raise ValueError('Speaker count must be an integer from 1 to 16')
            if src.get('has_audio') is False: raise ValueError('Silent video has no speech to transcribe')
            if not src.get('ready'): raise ValueError('Wait for probe and audio extraction before transcription')
        return src, None
    raise ValueError('Unknown workbench operation')


def job_path(sp, jid):
    if not isinstance(jid, str) or not JOB_ID.fullmatch(jid): raise ValueError('Invalid job id')
    return safe(sp, sp.work / 'production' / 'jobs' / (jid + '.json'))


def diagnostic(exc):
    return str(safety.redact_obj(f'{type(exc).__name__}: {exc}'))[-4000:]


def worker_argv(root, jid):
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--workbench-worker', str(root), jid]
    return [sys.executable, '-m', 'ave.studio.workbench_worker', str(root), jid]


class Workbench:
    def __init__(self, studio, *, fixtures=False, by='local'):
        self.studio, self.sp, self.fixtures, self.by = studio, studio.sp, fixtures, by
        self._threads = []
        self._closed = False
        if self.sp is not None and not fixtures:
            try:
                held = self._claim()
            except Busy:
                return
            try:
                for job in self._jobs():
                    if job['status'] in ('queued', 'running'):
                        job.update(status='interrupted', diagnostics='The local worker stopped before completion. Input is preserved; retry this job.')
                        write(self.sp, job_path(self.sp, job['id']), job)
                self._recover_imports()
            finally:
                held.close()

    def _claim(self):
        if self.fixtures or self.sp is None: raise ValueError('Sample mode is read-only. Use a writable studio for production.')
        if self._closed: raise ValueError('Workbench is shutting down')
        p = safe(self.sp, self.sp.work / 'production' / 'worker.lock')
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        held = os.fdopen(fd, 'a')
        for attempt in range(6):
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == 5:
                    held.close()
                    raise Busy('Another production job is running. Wait for it to finish.') from None
                time.sleep(.02)  # allow a terminal worker's short exit/FD teardown

        return held

    def _jobs(self):
        directory = self.sp.work / 'production' / 'jobs'
        desk._safe_path(directory, self.sp.root)
        paths = sorted(directory.glob('*.json')) if directory.exists() else []
        if len(paths) > MAX_JOBS: raise ValueError('Workbench job limit reached')
        return [read(self.sp, p) for p in paths]

    def _recover_imports(self):
        """Reconcile an uploaded source's durable intent while holding worker.lock.

        Expose an interrupted job for explicit retry; never launch on startup.
        This also repairs older registrations that predate import_job metadata.
        """
        jobs = self._jobs()
        for src in source_index(self.sp)['sources']:
            if src.get('ready') or any(j['operation'] in ('import','manuscript') and j['args'].get('source_id') == src['source_id'] for j in jobs):
                continue
            if len(jobs) >= MAX_JOBS: raise ValueError('Workbench job limit reached during import recovery')
            sid = src['source_id']
            if not SOURCE_ID.fullmatch(sid): raise ValueError('Invalid recording source identity')
            jid = src.get('import_job') or 'job_' + sid.removeprefix('recording_')[:24]
            path = job_path(self.sp, jid)
            if path.exists(): raise ValueError('Import intent job identity collision; existing job preserved')
            job = {'v': 1, 'id': jid, 'operation': 'manuscript' if src.get('kind')=='document' else 'import', 'args': {'source_id': sid}, 'status': 'interrupted',
                   'created': iso_utc(self.studio.now()), 'by': self.by, 'result': None,
                   'diagnostics': 'Source upload was preserved, but import did not start. Retry to probe and extract audio.'}
            write(self.sp, path, job); jobs.append(job)
        return jobs

    def overview(self):
        if self.fixtures or self.sp is None:
            return {'sources': [], 'jobs': [], 'recipes': RECIPES, 'read_only': True, 'warnings': ['Sample data: uploads and production jobs are disabled.']}
        from .. import config
        warnings = []
        if not config.fluidaudio_cli(): warnings.append('Local FluidAudio is not installed. Recordings can be imported, but transcription needs setup.')
        return {'sources': inventory(self.sp), 'jobs': sorted(self._jobs(), key=lambda j: j['created'], reverse=True)[:50],
                'recipes': RECIPES, 'read_only': False, 'warnings': warnings}

    def tail(self, jid):
        return read(self.sp, job_path(self.sp, jid))

    def upload(self, tmp_path, name, size, sha256):
        if type(size) is not int or not 0 < size <= MAX_UPLOAD: raise ValueError('Empty or oversized upload')
        return self._register_staged(tmp_path, name, size, sha256)

    def _register_staged(self, tmp_path, name, size, sha256):
        """Trusted native intake and bounded HTTP upload share immutable registration."""
        if not isinstance(name, str) or not name or len(name) > 200 or name.startswith('.') or any(c in name for c in '/\\\x00') or any(ord(c) < 32 for c in name):
            raise ValueError('Choose a plain filename')
        suffix = Path(name).suffix.lower()
        if suffix not in TEXT_EXTS + DOCUMENT_EXTS + AUDIO_EXTS + VIDEO_EXTS: raise ValueError('Upload UTF-8 text, audio or video')
        safe(self.sp, tmp_path)
        if tmp_path.is_symlink() or not tmp_path.is_file(): raise ValueError('Upload must be a regular file')
        if type(size) is not int or not 0 < size <= 1024**4 or tmp_path.stat().st_size != size: raise ValueError('Empty or oversized upload')
        # Do not trust the caller's digest, even for internal callers.
        digest = safety.sha256_file(tmp_path)
        if sha256 != digest: raise ValueError('Upload checksum does not match')
        if suffix in TEXT_EXTS and size <= desk.MAX_TEXT:
            try: body = tmp_path.read_bytes().decode('utf-8')
            except UnicodeDecodeError: raise ValueError('Writing must be valid UTF-8') from None
            held = self._claim()
            try:
                if len(inventory(self.sp)) >= MAX_SOURCES: raise ValueError('Workbench source limit reached')
                result = desk.intake(self.studio, kind='writing', text=body, title='', actor=Actor('human', self.by))
                return {'source_id': result['source']['file'], 'sha256': digest, 'kind': 'writing'}
            finally: held.close()
        held = self._claim()
        try:
            idx = source_index(self.sp)
            sid = 'recording_' + digest
            src = next((s for s in idx['sources'] if s['source_id'] == sid), None)
            if src:
                if safety.sha256_file(safe(self.sp, self.sp.root / src['file'])) != digest: raise ValueError('Existing recording hash mismatch; original preserved')
                jobs = self._recover_imports()
                job = next((j for j in jobs if j['operation'] in ('import','manuscript') and j['args'].get('source_id') == sid), None)
                held.close()
                return {'source_id': sid, 'duplicate': True, **({'job': job} if job else {})}
            if len(inventory(self.sp)) >= MAX_SOURCES: raise ValueError('Workbench source limit reached')
            if len(self._jobs()) >= MAX_JOBS: raise ValueError('Workbench job limit reached')
            dest = safe(self.sp, self.sp.work / 'production' / 'sources' / (digest + suffix))
            _copy_immutable(self.sp, tmp_path, dest, digest)
            src = {'source_id': sid, 'kind': 'document' if suffix in TEXT_EXTS + DOCUMENT_EXTS else ('video' if suffix in VIDEO_EXTS else 'audio'), 'label': name,
                   'file': self.sp.rel(dest), 'sha256': digest, 'size': size, 'ready': False,
                   'import_job': 'job_' + digest[:24]}
            idx['sources'].append(src)
            write(self.sp, self.sp.work / 'production' / 'sources.json', idx)
            return {'source_id': sid, 'job': self._start('manuscript' if src['kind']=='document' else 'import', {'source_id': sid}, held)}
        except BaseException:
            try:
                self._recover_imports()
            finally:
                held.close()
            raise

    def cancel_frame(self, body):
        production.only(body, {'job_id'})
        if self.fixtures: raise ValueError('Sample mode is read-only')
        job=self.tail(body.get('job_id'))
        if job['operation']!='frame' or job['status'] not in ('queued','running'): raise ValueError('Only an active frame extraction can be cancelled here')
        with frame_attachment_lock(self.sp,job['id']):
            if safe(self.sp,job_path(self.sp,job['id']).with_suffix('.attached')).exists():
                raise ValueError('The frame is already attached; remove the cover in the composer if needed')
            marker=safe(self.sp,job_path(self.sp,job['id']).with_suffix('.cancel'))
            marker.write_text('cancel requested')
        return {'id':job['id'],'cancel_requested':True}
    def cancel_media(self, body):
        production.only(body, {'job_id'})
        if self.fixtures: raise ValueError('Sample mode is read-only')
        job=self.tail(body.get('job_id'))
        if job['operation'] not in ('import','transcribe','video-source','video-render','video-kept','video-cleanup','manuscript','image-transform','design-render','image-split','episode-create','episode-intake','episode-transcribe','episode-sync','episode-assembly','episode-combined','episode-rough','episode-sequence','episode-final','episode-select','episode-short','episode-package','episode-handoff','episode-adopt') or job['status'] not in ('queued','running'):
            raise ValueError('Choose an active media or manuscript job')
        safe(self.sp,job_path(self.sp,job['id']).with_suffix('.cancel')).write_text('cancel requested')
        proc=getattr(self,'_job_processes',{}).get(job['id'])
        if proc and proc.poll() is None:
            import signal
            try: os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError: pass
        return {'id':job['id'],'cancel_requested':True}

    def start_media(self, op, body):
        if op not in ('video-source','video-render','video-kept','video-cleanup','image-transform','design-render','image-split','episode-create','episode-intake','episode-transcribe','episode-sync','episode-assembly','episode-combined','episode-rough','episode-sequence','episode-final','episode-select','episode-short','episode-package','episode-handoff','episode-adopt'): raise ValueError('Unknown workshop action')
        return self._begin(op, body)

    def start_frame(self, body): return self._begin('frame', body)
    def start_render(self, body): return self._begin('render', body)
    def start_transcribe(self, body): return self._begin('transcribe', body)

    def _begin(self, op, body):
        held = self._claim()
        try: return self._start(op, body, held)
        except BaseException:
            held.close()
            raise

    def retry(self, body):
        production.only(body, {'job_id'})
        held = self._claim()
        try:
            job = self.tail(body.get('job_id'))
            if job['status'] not in ('failed', 'interrupted','cancelled'): raise ValueError('Only failed or interrupted jobs can be retried')
            from .episode_release import OPS as release_ops,recover_committed
            if job['operation'] in (*release_ops,'video-kept','video-render'):
                recovered=recover_committed(self.studio,job)
                if recovered is not None and not recovered.get('recovery',{}).get('incomplete'):
                    job.update(status='done',result=recovered,diagnostics='Recovered an earlier committed output; current eligibility has not been re-established.')
                    write(self.sp,job_path(self.sp,job['id']),job);held.close();return job
                if recovered is not None:
                    job.update(result=recovered,diagnostics=recovered['recovery']['note'])
                    write(self.sp,job_path(self.sp,job['id']),job)
            return self._start(job['operation'], job['args'], held, previous=job)
        except BaseException:
            held.close()
            raise

    def _start(self, operation, args, held, previous=None):
        import secrets
        src, sentence = validate_operation(self.sp, operation, args)
        if (operation.startswith('episode-') or operation in ('video-source','video-render','video-kept','video-cleanup','image-transform','design-render','image-split')) and not previous:
            matches=[j for j in self._jobs() if j['args'].get('request_id')==args['request_id']]
            if matches:
                found=matches[0]
                if found['operation']!=operation or found['args']!=args: raise ValueError('Request ID already belongs to different parameters')
                held.close();return found
        if operation=='frame' and not previous and store.load_post(self.sp,args['post'])['rev']!=args['expected_rev']: raise model.RevConflict(store.load_post(self.sp,args['post']))
        if not previous and len(self._jobs()) >= MAX_JOBS: raise ValueError('Workbench job limit reached')
        jid = previous['id'] if previous else (src.get('import_job') if operation in ('import','manuscript') else None) or 'job_' + secrets.token_hex(12)
        if not previous and job_path(self.sp, jid).exists(): raise ValueError('Job identity collision; existing job preserved')
        if operation.startswith('episode-') or operation in ('import','transcribe','video-source','video-render','video-kept','video-cleanup','frame','manuscript','image-transform','design-render','image-split'): safe(self.sp,job_path(self.sp,jid).with_suffix('.cancel')).unlink(missing_ok=True)
        job = {'v': 1, 'id': jid, 'operation': operation,
               'args': args, 'status': 'queued', 'created': previous['created'] if previous else iso_utc(self.studio.now()),
               'diagnostics': '', 'result': None, 'by': self.by}
        if previous:
            job['attempts'] = (previous.get('attempts', []) + [{'status': previous['status'],
                'diagnostics': previous.get('diagnostics', ''), 'at': iso_utc(self.studio.now())}])[-20:]
        if operation == 'render':
            pid = previous.get('post') if previous else None
            post = None
            if pid and safe(self.sp, store.post_path(self.sp, pid)).exists():
                post = store.load_post(self.sp, pid)
            create = post is None
            if create:
                pid = pid or 'p_' + self.studio.now().strftime('%Y%m%d') + '_' + job['id'][4:]
                if safe(self.sp, store.post_path(self.sp, pid)).exists(): raise ValueError('Draft identity collision; existing draft preserved')
                post = model.new_post(idea=sentence['text'], copy={'base': sentence['text']}, by=self.by, post_id=pid,
                    origin={'type': 'blog', 'ref': args['source_id'], 'passages': []},
                    reason='Human-selected verbatim sentence', confidence=1.0, now=self.studio.now())
                post['production'] = {**production.defaults(post), 'title': sentence['text'][:100], 'status': 'design',
                                      'sources': [{'source_id': src['source_id'], 'label': src['label']}]}
            job['post'] = post['id']
            job['expected_rev'] = previous['expected_rev'] if previous else post['rev']
            # The deterministic post ID is persisted first, including across retries.
            write(self.sp, job_path(self.sp, job['id']), job)
            if create:
                saved = store.save_post(self.sp, post, expected_rev=None, actor=Actor('human', self.by), op='workbench.draft', now=self.studio.now())
                if not previous: job['expected_rev'] = saved['rev']
        from .episode_release import is_native_render
        native_render=operation=='video-render' and is_native_render(self.studio,args)
        if operation.startswith('episode-') or operation=='video-cleanup' or native_render or (operation=='video-kept' and str(args.get('source_id','')).startswith('episode_')):
            from .episode_workshop import descriptor
            job['episode_descriptor']=descriptor(self.studio,operation,args)
        write(self.sp, job_path(self.sp, job['id']), job)
        thread = threading.Thread(target=self._launch, args=(job['id'], held), daemon=False)
        try:
            thread.start()
        except Exception as exc:
            job.update(status='failed', diagnostics=diagnostic(exc))
            write(self.sp, job_path(self.sp, job['id']), job)
            raise
        self._threads.append(thread)
        return job

    def _launch(self, jid, held):
        bridge=None
        try:
            # Inherit the lock FD so a server crash does not allow a second worker
            # to declare the still-running child interrupted.
            argv=worker_argv(self.sp.root,jid);fds=(held.fileno(),)
            operation=self.tail(jid)['operation']
            current=self.tail(jid)
            native_kept=operation=='video-kept' and str(current['args'].get('source_id','')).startswith('episode_')
            from .episode_release import is_native_render
            native_render=operation=='video-render' and is_native_render(self.studio,current['args'])
            if operation.startswith('episode-') or operation=='video-cleanup' or native_kept or native_render:
                from .episode_release import OPS as release_ops
                if operation in release_ops or native_kept or native_render:
                    from .episode_release_bridge import ReleaseParent as Parent
                elif operation=='video-cleanup':
                    from .media_cleanup_bridge import MediaCleanupParent as Parent
                else:
                    from .episode_worker_bridge import Parent
                current=self.tail(jid)
                current['phase']='Verifying captured media and review dependencies before worker starts'
                write(self.sp,job_path(self.sp,jid),current)
                bridge=Parent(self.studio,self.tail(jid));bridge.start()
                argv+=['--episode-bridge',str(bridge.child.fileno())];fds+=(bridge.child.fileno(),)
            proc = subprocess.Popen(argv, env=child_env(), pass_fds=fds, start_new_session=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if not hasattr(self, '_processes'): self._processes = []
            self._processes.append(proc)
            if not hasattr(self,"_job_processes"): self._job_processes={}
            self._job_processes[jid]=proc
            if self._closed and getattr(self, '_native_cancel', False):
                import signal
                os.killpg(proc.pid, signal.SIGTERM)
            tail = b''
            while chunk := proc.stdout.read(4096): tail = (tail + chunk)[-8000:]
            rc = proc.wait()
            job = self.tail(jid)
            if safe(self.sp,job_path(self.sp,jid).with_suffix('.cancel')).exists() and job['status'] in ('queued','running'):
                job.update(status='cancelled',diagnostics='Cancelled. Original and completed outputs are preserved; retry to resume.')
                write(self.sp,job_path(self.sp,jid),job)
            if job['status'] in ('queued', 'running') or (rc and job['status'] == 'done'):
                job.update(status='failed', diagnostics=diagnostic(RuntimeError(f'Worker exited {rc}. ' + tail.decode(errors='replace'))))
                write(self.sp, job_path(self.sp, jid), job)
        except (Exception, SystemExit) as exc:
            job = self.tail(jid); job.update(status='failed', diagnostics=diagnostic(exc)); write(self.sp, job_path(self.sp, jid), job)
        finally:
            if bridge:bridge.close()
            held.close()

    def close(self):
        self._closed = True
        if getattr(self, '_native_cancel', False):
            import signal
            for proc in getattr(self, '_processes', []):
                if proc.poll() is None:
                    try: os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
        for thread in self._threads: thread.join()


from contextlib import contextmanager

@contextmanager
def frame_attachment_lock(sp,jid):
    path=safe(sp,job_path(sp,jid).with_suffix('.attachment.lock'))
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def execute_operation(studio, job):
    """Typed, importable execution seam; only managed IDs and known recipes."""
    from .. import audio, media, transcribe
    from .render import render_post
    sp, args, op = studio.sp, job['args'], job['operation']
    def phase(label):
        job['phase']=label
        if job.get('id') and job_path(sp,job['id']).exists(): write(sp,job_path(sp,job['id']),job)
    if op.startswith('episode-') or op in ('video-kept','video-render'):
        phase('Verifying source and review dependencies · cancellation available')
    source, sentence = validate_operation(sp, op, args)
    phase({'import':'Probing and decoding the recording','transcribe':'Transcribing speech and assigning speakers','video-source':'Preparing source proxy and waveform','video-render':'Rendering exact programme','video-kept':'Preparing kept episode clip'}.get(op,'Verifying source and review dependencies · cancellation available' if op.startswith('episode-') else 'Working locally'))
    if op.startswith('episode-'):
        from .episode_workshop import execute
        return execute(studio,job)
    if op in ('video-source','video-render','video-kept','video-cleanup'):
        from .video_workshop import execute
        return execute(studio,job)
    if op == 'manuscript':
        from .manuscripts import execute
        return execute(studio,source)
    if op in ('image-transform','design-render','image-split'):
        from .media_workshop import execute
        return execute(studio,job)
    if op == 'frame':
        import subprocess
        from .. import library
        from . import composer
        from ..config import binary
        FFMPEG=binary("ffmpeg")
        if not FFMPEG: raise ValueError("FFmpeg is not installed")
        directory=safe(sp,sp.work/'production'/'frames'/job['id']);directory.mkdir(parents=True,exist_ok=True)
        output=safe(sp,directory/'frame.png')
        import time
        marker=safe(sp,job_path(sp,job['id']).with_suffix('.cancel'))
        if marker.exists(): raise ValueError('Frame extraction cancelled; original video preserved')
        command=[str(FFMPEG),'-nostdin','-v','error','-y','-ss',str(args['seconds']),'-i',str(safe(sp,sp.root/source['file'])),'-frames:v','1','-vf','scale=1280:1280:force_original_aspect_ratio=decrease',str(output)]
        with subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE) as proc:
            deadline=time.monotonic()+60
            while proc.poll() is None:
                if marker.exists() or time.monotonic()>deadline:
                    proc.kill();proc.communicate();raise ValueError('Frame extraction cancelled or timed out; original video preserved')
                time.sleep(.05)
            _,err=proc.communicate()
            if proc.returncode: raise ValueError('Frame extraction failed: '+err.decode(errors='replace')[-2000:])
        if marker.exists(): raise ValueError('Frame extraction cancelled; original video preserved')
        from PIL import Image
        with Image.open(output) as im: im.load()
        rec=library.add_file(sp,output,source={'type':'render'},by=job['by'],derived_from=[source['id']],alt=f"Cover frame at {args['seconds']:g} seconds",origin={'frame_seconds':args['seconds'],'source_sha256':source['sha256']},now=studio.now())
        result={'assets':[{**rec,'url':'/files/'+quote(rec['file'],safe='/')}],'attachment_conflict':False}
        with frame_attachment_lock(sp,job['id']):
            if marker.exists(): raise ValueError('Frame extraction cancelled; extracted image preserved in library')
            try:
                composer.media_action(studio,args['post'],{'expected_rev':args['expected_rev'],'variant':args.get('variant'),'op':'cover','asset':rec['id']},Actor('human',job['by']))
                safe(sp,job_path(sp,job['id']).with_suffix('.attached')).write_text('attached')
                result['note']='Cover frame saved separately from content slides.'
            except model.RevConflict:
                result.update(attachment_conflict=True,note='Draft changed. The extracted cover remains in the library.')
        return result
    if op == 'render':
        # Validate all trees written by the renderer before invoking it.
        for directory in (sp.work / 'content', sp.root / 'library', sp.posts_dir):
            desk._safe_path(directory, sp.root)
            for base, dirs, files in os.walk(directory, followlinks=False):
                for name in dirs + files: desk._safe_path(Path(base) / name, sp.root)
        result = render_post(studio, job['post'], style=args['style'], fmt=args['format'],
            source={'writing': args['source_id'], 'sentences': [args['sentence_id']]},
            caption=sentence['text'], by=job['by'], actor=Actor('human', job['by']),
            reason='Human-selected verbatim sentence', confidence=1.0, expected_rev=job['expected_rev'])
        for a in result['assets']: a['url'] = '/files/' + quote(a['file'], safe='/')
        return result
    ep = episode_for(sp, source)
    if op == 'import':
        original = safe(sp, sp.root / source['file'])
        if safety.sha256_file(original) != source['sha256']: raise ValueError('Managed source changed; import stopped')
        probe = media.probe_file(original, sp.root)
        if not math.isfinite(probe['duration']) or probe['duration'] <= 0 or not (probe['audio'] or probe['video']):
            raise ValueError('Recording has no nonempty audio samples or positive duration. Choose a recording containing audio.')
        ep = _prepare_episode(sp, source)
        target = safe(sp, ep.raw / original.name)
        _copy_immutable(sp, original, target, source['sha256'])
        info = media.ingest(ep)
        outs = audio.extract_all(ep) if probe['audio'] else []
        import soundfile as sf
        if probe['audio'] and (not outs or any(sf.info(p).frames <= 0 for p in outs)): raise ValueError('Audio extraction produced no samples')
        phase('Preparing browser playback and waveform')
        from .video_workshop import proxy
        preview,waveform=proxy(sp,original,ep.work/'browser',probe)
        idx = source_index(sp)
        for row in idx['sources']:
            if row['source_id'] == source['source_id']:
                row.update(ready=True, duration=probe['duration'], episode=sp.rel(ep.root),has_audio=bool(probe['audio']),preview_file=sp.rel(preview),waveform_file=sp.rel(waveform))
        write(sp, sp.work / 'production' / 'sources.json', idx)
        return {'source_id': source['source_id'], 'episode': sp.rel(ep.root), 'duration': probe['duration'],
                'probe': info, 'analysis_audio': [sp.rel(p) for p in outs],
                'next': 'Run local transcription. Speaker names and camera mapping remain human checkpoints.'}
    result = transcribe.transcribe(ep, num_speakers=args['speakers'], diarize=args['speakers'] > 1)
    if not result.get('words'): raise ValueError('Local transcription produced no words')
    transcript_path = ep.artifact('transcript.json')
    preview, budget = [], 20000
    for utterance in result['utterances']:
        if len(utterance['text']) > budget or len(preview) >= 100: break
        preview.append(utterance); budget -= len(utterance['text'])
    return {'source_id': source['source_id'], 'transcript_url': '/files/' + quote(sp.rel(transcript_path), safe='/'),
            'utterances': preview, 'preview_truncated': len(preview) < len(result['utterances']), 'words': len(result['words']), 'engine': result['engine'],
            'next': 'Confirm speaker names, then camera mapping (video), before assembly and cut review. No names, cameras or cuts were approved.'}
