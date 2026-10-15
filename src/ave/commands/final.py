"""`ave final`: the third cut stage, rendered and verified."""

from __future__ import annotations

import json

from .. import final, state
from ..episode import Episode


def cmd(a) -> int:
    ep = Episode(a.episode)
    ep.require()
    rep = final.build_final(ep, style=a.style, music=a.music, music_in=a.music_in, music_gain_db=a.music_gain,
                            duck_db=a.duck, preset=a.preset, render=not a.no_render, burn=a.burn,
                            do_retranscribe=not a.skip_retranscribe, allow_unlicensed=a.allow_unlicensed, asr=a.asr, name=a.name,
                            opening=a.opening, closing=a.closing, draft_sequences=a.draft_sequences, from_final=a.from_final)
    print(json.dumps(rep, indent=2, default=str))
    ep.mark_stage("final", edit=rep["edit"])
    state.set_review(ep, "final cut ready: review it (ave handoff), then approve for clips/shorts/publish")
    return 0


def register(sub) -> None:
    s = sub.add_parser("final", help="rough cut + music + captions -> verified final render + exact-match SRT")
    s.add_argument("episode")
    s.add_argument("--style", default="podcast", help="caption style (podcast|short|minimal)")
    s.add_argument("--music", help="music file (must be in the license log, see ave assets)")
    s.add_argument("--music-in", type=float, default=0.0, help="start offset into the music file, seconds")
    s.add_argument("--music-gain", type=float, default=-20.0, help="music gain dB under dialogue (default -20)")
    s.add_argument("--duck", type=float, default=-8.0, help="extra duck dB while dialogue speaks (default -8)")
    s.add_argument("--preset", default="master")
    s.add_argument("--burn", action="store_true", help="burn the styled captions into the render")
    s.add_argument("--no-render", action="store_true", help="only build the EditDoc and captions")
    s.add_argument("--skip-retranscribe", action="store_true")
    s.add_argument("--allow-unlicensed", action="store_true")
    s.add_argument("--name", help="named edit, e.g. a session id: rough.<name> -> final.<name>")
    s.add_argument("--from-final", action="store_true",
                   help="continue the latest final edit (keeps overlays placed with `ave motion place --stage final`) instead of cloning the rough cut")
    s.add_argument("--asr", choices=["fluidaudio", "whisper"], default="fluidaudio")
    s.add_argument("--opening", dest="opening", action="store_true", default=None, help="build the opening sequence (overrides policy sequences.opening)")
    s.add_argument("--no-opening", dest="opening", action="store_false")
    s.add_argument("--closing", dest="closing", action="store_true", default=None, help="build the closing sequence (overrides policy sequences.closing)")
    s.add_argument("--no-closing", dest="closing", action="store_false")
    s.add_argument("--draft-sequences", action="store_true", help="use sequences whose plan the human has not approved (the result is marked a draft)")
    s.set_defaults(fn=cmd)
