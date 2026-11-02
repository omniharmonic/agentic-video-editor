"""External agent context and bounded stdio MCP transport (no model API)."""
from __future__ import annotations

import json
import sys


def cmd(a):
    from ..agent import AgentApp,mcp_config,serve
    from ..studio.project import find_studio
    from ..studio.service import open_studio
    try:
        sp=find_studio(a.studio)
        sp.require()
        if a.action=='mcp-config':
            print(json.dumps(mcp_config(sp.root,by=a.by),indent=2))
            return 0
        app=AgentApp(open_studio(sp.root),by=a.by)
        if a.action=='context':
            print(json.dumps(app.context(),indent=2,ensure_ascii=False))
            return 0
        return serve(app)
    except (ValueError,OSError,SystemExit) as e:
        # A failed stdio startup must never emit non-protocol stdout.
        if a.action=='serve': print('ave agent: studio could not be opened; check the configured path.',file=sys.stderr)
        else: print(json.dumps({'ok':False,'error':str(e)}))
        return 2


def register(sub):
    p=sub.add_parser('agent',help='external-agent context, MCP configuration and safe stdio tools')
    ss=p.add_subparsers(dest='action',required=True)
    for action,help_ in [('context','read show, progress, pipeline and human gates as JSON'),('mcp-config','print MCP config for this installation (writes no client settings)'),('serve','serve explicit editorial tools over MCP stdio')]:
        q=ss.add_parser(action,help=help_)
        q.add_argument('--studio',metavar='DIR',help='studio folder (default: workspace/studio)')
        q.add_argument('--by',default='external-agent',help='agent name recorded on draft writes')
        q.set_defaults(fn=cmd)
