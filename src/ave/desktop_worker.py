"""Typed frozen dispatch, before GUI initialization. Always agent authority."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import sys
import uuid


def job_directory():
    from .resources import user_data_dir
    return user_data_dir() / 'desktop-jobs'


def enqueue(ep, cmd, args):
    from .ui.jobs import build_argv
    build_argv(ep, cmd, args)
    jid = uuid.uuid4().hex
    directory = job_directory(); directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    file = directory / (jid + '.json')
    with file.open('x') as out: json.dump({'episode': str(ep.root), 'cmd': cmd, 'args': args or {}}, out)
    file.chmod(0o600)
    return jid


def production(jid):
    from .episode import Episode
    from .ui.jobs import build_argv
    from .cli import main
    if not re.fullmatch('[a-f0-9]{32}', jid): raise ValueError('Invalid worker job identity')
    path = job_directory() / (jid + '.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16000: raise ValueError('Invalid worker job')
    job = json.loads(path.read_text())
    if set(job) != {'episode', 'cmd', 'args'}: raise ValueError('Invalid worker schema')
    ep = Episode(job['episode']); ep.require()
    argv = build_argv(ep, job['cmd'], job['args'])
    # One-shot consumed request. Crash recovery creates a new validated job.
    path.unlink()
    return main(argv)


def dispatch(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    from .desktop_runtime import frozen, configure
    if args and args[0] in ('--production-worker', '--workbench-worker', '--mcp'):
        os.environ['AVE_AGENT'] = '1'
        os.environ.pop('AVE_APPROVAL_KEY', None)
    if frozen(): configure()
    if args[:1] == ['--cli']:
        from .cli import main
        return main(args[1:])
    if args == ['--install-cli']:
        from .desktop_install import run
        return run()
    if args[:1] == ['--resolve-handoff']:
        import runpy
        sys.argv = [args[0], *args[1:]]
        runpy.run_path(str(Path(sys._MEIPASS) / 'resolve-mcp/resolve_handoff.py'), run_name='__main__')
        return 0
    if len(args) == 2 and args[0] == '--diagnostics-json':
        from .desktop_runtime import diagnostics
        data = diagnostics(); Path(args[1]).write_text(json.dumps(data, indent=2))
        return 0 if data['ok'] else 2
    if len(args) == 5 and args[:2] == ['--scheduler', '--studio'] and args[3] == '--mode':
        from .desktop_service import run_tick
        print(json.dumps(run_tick(args[2], args[4]), sort_keys=True))
        return 0
    if len(args) == 2 and args[0] == '--production-worker': return production(args[1])
    if len(args) in (3,5) and args[0] == '--workbench-worker':
        from .studio.workbench_worker import main
        return main(args[1:])
    if len(args) == 3 and args[:2] == ['--mcp', '--studio']:
        from .agent import AgentApp, serve
        from .studio.service import open_studio
        studio = open_studio(args[2], publishers={}, host=None); studio.native_authoring = True
        return serve(AgentApp(studio))
    if len(args) == 2 and args[0] == '--connections-fixture-check':
        from .desktop_connections_fixture import run
        return run(args[1])
    if len(args) in (2,3) and args[0] == '--episode-fixture-check':
        from .desktop_episode_fixture import run
        return run(args[1],args[2] if len(args)==3 else None)
    if len(args) == 2 and args[0] == '--native-fixture-check':
        from .desktop_fixture_check import run
        return run(args[1])
    if args not in ([], ['--native-fixture']): raise ValueError('Unknown standalone application operation')
    from .desktop_native import main
    return main(fixture=bool(args))
