"""Local full-resolution manual publishing pack (never sends)."""
from __future__ import annotations

from ..studio.cli import guarded,out


@guarded
def command(a):
    from ..studio.project import find_studio
    from ..studio.manual_export import build,describe
    sp=find_studio(a.studio)
    if a.check: result=describe(sp,a.post,a.variant,draft=a.draft)
    else: result=build(sp,a.post,a.variant,output=a.output,draft=a.draft,expected_rev=a.expected_rev)
    out(result)
    return 0 if result['ok'] else 2


def register(sub):
    p=sub.add_parser('export-post',help='download exact-resolution post media and copy for manual posting; never publishes')
    p.add_argument('studio',help='studio folder')
    p.add_argument('post',help='post id')
    p.add_argument('--variant',help='variant id (required when a post has several)')
    p.add_argument('--output',help='parent directory for a new immutable pack; default studio work/manual_exports')
    p.add_argument('--expected-rev',type=int,help='refuse if another creator has changed the post')
    p.add_argument('--draft',action='store_true',help='unreviewed draft pack, marked clearly; no account login needed')
    p.add_argument('--check',action='store_true',help='read-only readiness and exact media manifest')
    p.set_defaults(fn=command)
