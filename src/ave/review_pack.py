"""Human review pack: one static HTML page that makes judging 20 clip proposals take minutes, not an afternoon.

Everything the human needs to decide sits on the card: three real frames (in / middle / out), the hook, the score
breakdown with the penalties that were applied, the transcript with the in/out handles and a few sentences of context
either side, and the agent's rationale. Boundaries are adjusted by clicking a sentence ("start here" / "end here"), not
by typing numbers. The page lists EVERY surfaced section (sortable, groupable), each with Keep / Maybe / Skip and reason chips, and exports a decisions JSON
that `ave clips import-decisions` ingests into the learning ledger; it works from file:// and sends nothing anywhere.
"""

from __future__ import annotations

import html
import importlib
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import clips as C
from . import editdoc
from .episode import Episode

THUMB_W = 480


def frame_source(doc: dict, o: float) -> tuple[Path, float]:
    """Which file and which source-clock time shows output time `o` of `doc` (as a relative path + seconds)."""
    o = min(max(o, 0.0), max(doc["duration"] - 0.05, 0.0))
    seg = next((s for s in doc["tracks"]["video"] if s["o0"] <= o < s["o1"]), doc["tracks"]["video"][-1])
    src = doc["sources"][seg["src"]]
    t = editdoc.src_time(src, seg["e0"] + (o - seg["o0"]))
    return Path(src["file"]), max(t, 0.0)


def extract_frame(video: Path, t: float, out: Path) -> str | None:
    """One JPEG frame; returns an error string or None."""
    if not shutil.which("ffmpeg"):
        return "ffmpeg not found"
    if not video.exists():
        return f"missing source {video}"
    out.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                        "-vf", f"scale={THUMB_W}:-2", "-q:v", "3", str(out)], capture_output=True, text=True)
    if p.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        return (p.stderr or "no frame produced").strip()[-200:]
    return None


def make_thumbs(ep: Episode, doc: dict, cl: list[dict], out_dir: Path) -> tuple[dict, list[str]]:
    """thumbs/<clip>_<in|mid|out>_<ms>.jpg for every clip. Names carry the time so a nudge never shows a stale frame."""
    jobs, names = [], {}
    for c in cl:
        mid = (c["start"] + c["end"]) / 2
        for tag, o in (("in", c["start"] + 0.05), ("mid", mid), ("out", c["end"] - 0.15)):
            video, t = frame_source(doc, o)
            name = f"thumbs/{c['id']}_{tag}_{int(round(t * 1000))}.jpg"
            names.setdefault(c["id"], {})[tag] = name
            jobs.append((ep.root / video, t, out_dir / name))
    with ThreadPoolExecutor(max_workers=4) as pool:
        errs = list(pool.map(lambda j: None if j[2].exists() and j[2].stat().st_size else extract_frame(*j), jobs))
    keep = {j[2].name for j in jobs}
    for f in (out_dir / "thumbs").glob("*.jpg"):
        if f.name not in keep:
            f.unlink()
    return names, [f"{j[2].name}: {e}" for j, e in zip(jobs, errs) if e]


def render_drafts(ep: Episode, cl: list[dict], out_dir: Path) -> tuple[dict, list[str]]:
    """Proxy drafts through ave.compile if it exists. Imported lazily; never required."""
    notes: list[str] = []
    try:
        compile_mod = importlib.import_module("ave.compile")
        render = compile_mod.render
    except Exception as e:  # not written yet, or broken: the review pack still works without video
        return {}, [f"drafts skipped: ave.compile is not available ({type(e).__name__}: {e}). Re-run with --drafts once it exists."]
    made = {}
    for c in cl:
        edit = c.get("edit")
        if not edit:
            continue
        src = ep.root / edit["path"]
        dst = out_dir / "drafts" / f"{c['id']}.mp4"
        try:
            if not (dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime):
                dst.parent.mkdir(parents=True, exist_ok=True)
                render(ep, editdoc.load(src), dst, preset="proxy", burn_captions=False, overwrite=True)
            made[c["id"]] = f"drafts/{c['id']}.mp4"
        except Exception as e:
            notes.append(f"draft for {c['id']} failed: {e}")
    return made, notes


# ---- HTML --------------------------------------------------------------------------------------------------

def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def _sentence_row(s: dict, idx: int, state: str, lead: int = 0, trail: int = 0) -> str:
    toks = [html.escape(t) for t in s["text"].split()]
    if lead:
        toks[:lead] = ["<del>" + " ".join(toks[:lead]) + "</del>"]
    if trail:
        toks[-trail:] = ["<del>" + " ".join(toks[-trail:]) + "</del>"]
    return (f'<div class="s {state}" data-i="{idx}" data-id="{s["id"]}" data-t0="{s["start"]}" data-t1="{s["end"]}">'
            f'<span class="t">{_mmss(s["start"])}</span><span class="sp">{html.escape(s["speaker"] or "--")}</span>'
            f'<span class="x">{" ".join(toks)}</span>'
            f'<span class="b"><button type="button" data-act="start" title="start the clip at this sentence">in</button>'
            f'<button type="button" data-act="end" title="end the clip at this sentence">out</button></span></div>')


def _bars(c: dict, weights: dict) -> str:
    rows = []
    for k, w in weights.items():
        v = c["inputs"]["scores"].get(k, 0)
        rows.append(f'<div class="bar"><span>{html.escape(k)}</span><i style="--v:{v * 10}%"></i><b>{v:g}</b><em>x{w:g}</em></div>')
    return "".join(rows)


def _sid_index(sid: str) -> int:
    return int(sid[1:])


def _alternates(c: dict, i0: int, j0: int) -> str:
    alts = c.get("alternates") or []
    if not alts:
        return ""
    items = []
    for a in alts:
        di, dj = _sid_index(a["start_sid"]) - i0, _sid_index(a["end_sid"]) - j0
        items.append(f'<li><span>{_mmss(a["start"])}&ndash;{_mmss(a["end"])} &middot; {a["duration"]:.0f}s &middot; score {a["score"]:.0f} &middot; '
                     f'{"ends on a sentence" if a.get("ends_on_sentence") else "ends mid-sentence"} &middot; {html.escape(", ".join(a.get("tags") or []) or "plain")}</span> '
                     f'<button type="button" class="link" data-act="alt" data-di="{di}" data-dj="{dj}" title="move this clip to that in/out">use this in/out</button></li>')
    return f'<details class="alts"><summary>{len(alts)} alternate in/out choice{"s" if len(alts) != 1 else ""}</summary><ul>{"".join(items)}</ul></details>'


def _card(c: dict, S: C.Sentences, names: dict, drafts: dict, rubric: dict, reasons: list[dict]) -> str:
    n_ctx = int(rubric["selects"]["context_sentences"])
    i0, j0 = S.by_id[c["sentence_ids"][0]]["i"], S.by_id[c["sentence_ids"][-1]]["i"]
    lo, hi = max(0, i0 - n_ctx), min(len(S.items) - 1, j0 + n_ctx)
    rows = []
    for k in range(lo, hi + 1):
        state = "ctx"   # the page script marks the live in/out range ("on") so nudges repaint instantly
        rows.append(_sentence_row(S.items[k], k, state,
                                  len(c["trimmed"]["leading"]) if k == i0 else 0, len(c["trimmed"]["trailing"]) if k == j0 else 0))
    th = names.get(c["id"], {})
    imgs = "".join(f'<figure><img loading="lazy" src="{th[t]}" alt="{t} frame"><figcaption>{t}</figcaption></figure>' for t in ("in", "mid", "out") if t in th)
    video = (f'<video controls preload="none" poster="{th.get("mid", "")}" src="{drafts[c["id"]]}"></video>' if c["id"] in drafts else "")
    chips = "".join(f'<span class="chip bad" title="{html.escape(p["reason"])}">-{p["points"]:g} {html.escape(p["name"].replace("_", " "))}</span>' for p in c["penalties"])
    chips += "".join(f'<span class="chip warn">{html.escape(w)}</span>' for w in c.get("warnings", []))
    tags = c.get("tags") or []
    tagchips = "".join(f'<span class="chip tag">{html.escape(t)}</span>' for t in tags)
    badge = "" if c["status"] == "proposed" else f'<span class="badge {c["status"]}">{c["status"]}</span>'
    if c.get("maybe"):
        badge += '<span class="badge maybe">maybe</span>'
    origin = "" if c.get("origin", "agent") == "agent" else '<span class="badge heur" title="surfaced by heuristics, not picked by the agent">heuristic</span>'
    hq = f'<p class="hq">hook line: &ldquo;{html.escape(c["hook_quote"])}&rdquo;</p>' if c.get("hook_quote") else ""
    hp = c.get("hook_pos_s")
    signals = (f'<p class="sig">hook at {hp:g}s &middot; ' if hp is not None else '<p class="sig">no clear hook &middot; ') \
        + ("ends on a full sentence" if c.get("ends_on_sentence", True) else "ends mid-sentence") + "</p>"
    extra = ""
    if c.get("complete_statement") is False:
        extra += f'<p class="sig inc"><b>Not a complete statement:</b> {html.escape(c.get("completeness_note") or "")}</p>'
    elif c.get("expanded"):
        extra += f'<p class="sig">{html.escape(c["expanded"]["note"])}</p>'
    v = c.get("context_variant")
    if v:
        extra += (f'<p class="sig inc"><b>Opens on a referent with no antecedent</b> ({html.escape(c.get("needs_context_reason") or "")}). '
                  f'Nudge the start earlier by {len(v["sentence_ids"]) - len(c["sentence_ids"])} sentence(s) to add: &ldquo;{html.escape(v["added_text"][:160])}&rdquo;</p>')
    tg = c.get("tighten") or []
    if tg:
        items = "".join(f'<li><b>{html.escape(t["kind"])}</b> -{t["saves_s"]:.1f}s &ldquo;{html.escape(t["text"][:90])}&rdquo; <span class="mut">{html.escape(t["reason"])}</span></li>' for t in tg)
        extra += (f'<details class="tight"><summary>{len(tg)} tighten suggestion{"s" if len(tg) != 1 else ""}: {c["duration"]:.0f}s &rarr; {c["tight_duration"]:.0f}s '
                  f'(suggestions only, nothing is cut)</summary><ul>{items}</ul></details>')
    rbtn = "".join(f'<button type="button" class="rc" data-reason="{html.escape(r["id"])}" aria-pressed="false">{html.escape(r["label"])}</button>' for r in reasons)
    why = html.escape(c.get("reason") or c["rationale"])
    primary = tags[0] if tags else "plain"
    title = html.escape(c["title"])
    return f'''<article class="card {c["status"]}" id="{c["id"]}" tabindex="0" aria-label="{title}" data-id="{c["id"]}" data-i0="{i0}" data-j0="{j0}" data-score="{c["total"]:.2f}" data-start="{c["start"]}" data-dur="{c["duration"]}" data-tag="{html.escape(primary)}" data-status="{c["status"]}{"-maybe" if c.get("maybe") else ""}">
<header><div class="score" title="score 0-100: weighted rubric score minus penalties">{c["total"]:.0f}</div>
<div><h2>{title} {badge}{origin}</h2><p class="meta"><span class="dur" data-base="{c["duration"]:.1f}">{c["duration"]:.0f}s</span> &middot; {_mmss(c["start"])}&ndash;{_mmss(c["end"])} &middot; {html.escape(", ".join(c["speakers"]))} &middot; {c["id"]}</p></div>
<div class="act" role="group" aria-label="Decision for {title}"><button type="button" class="ok" data-act="keep" aria-pressed="false">Keep <kbd>y</kbd></button><button type="button" class="mb" data-act="maybe" aria-pressed="false">Maybe <kbd>m</kbd></button><button type="button" class="no" data-act="skip" aria-pressed="false">Skip <kbd>s</kbd></button><button type="button" class="undo" data-act="undo" hidden>Undo <kbd>u</kbd></button></div></header>
<div class="chips">{tagchips}{chips}</div>
<div class="media">{video}<div class="thumbs">{imgs}</div></div>
<p class="hook"><b>Hook:</b> {html.escape(c["hook"])}</p>{hq}{signals}{extra}
<div class="reasons" role="group" aria-label="Why, for {title} (optional)"><span class="lbl">Why:</span>{rbtn}</div>
<div class="cols"><div class="tx">{"".join(rows)}</div>
<aside><div class="bars">{_bars(c, rubric["weights"])}</div><p class="why"><b>Reason it was surfaced:</b> {why}</p>
{_alternates(c, i0, j0)}
<p class="nudge">Boundary: <span class="nv">as proposed</span> <button type="button" class="link" data-act="reset">reset</button></p>
<input class="note" type="text" placeholder="note (optional)" aria-label="note for {title}"></aside></div>
</article>'''


CSS = """
:root{--bg:#f7f6f3;--fg:#1d1d1b;--mut:#6b6a66;--card:#fff;--line:#dedbd3;--acc:#2f5d50;--ok:#2f7d4f;--no:#a33a3a;--mb:#8a6a1c;--warn:#8a6a1c;--in:#e6f0ec}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#161715;--fg:#ecebe6;--mut:#a09f98;--card:#1f201d;--line:#34352f;--acc:#7fb5a3;--ok:#6fbf8d;--no:#e08585;--mb:#d9b45a;--warn:#d9b45a;--in:#26332e}}
:root[data-theme=dark]{--bg:#161715;--fg:#ecebe6;--mut:#a09f98;--card:#1f201d;--line:#34352f;--acc:#7fb5a3;--ok:#6fbf8d;--no:#e08585;--mb:#d9b45a;--warn:#d9b45a;--in:#26332e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.top h1{font-size:17px;margin:0}.top .sum{color:var(--mut);flex:1;min-width:220px}.top code{background:var(--card);border:1px solid var(--line);padding:2px 6px;border-radius:4px}
.top label{font-size:13px;color:var(--mut)}.top select{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:4px 6px}
main{max-width:1180px;margin:0 auto;padding:16px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:0 0 18px;padding:14px 16px}
.card:focus-visible,button:focus-visible,select:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.card.rejected,.card.dec-skip{opacity:.6}.card.approved,.card.dec-keep{border-color:var(--ok)}.card.dec-skip{border-color:var(--no)}.card.dec-maybe{border-color:var(--mb)}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap}header h2{font-size:17px;margin:0}.meta{margin:2px 0 0;color:var(--mut);font-size:13px}
.grp{font-size:15px;margin:22px 0 10px;padding-bottom:4px;border-bottom:1px solid var(--line);color:var(--mut)}
.score{min-width:46px;height:46px;border-radius:50%;background:var(--acc);color:var(--card);display:grid;place-items:center;font-weight:700;font-size:17px}
.act{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:6px 12px;cursor:pointer;min-height:32px}
.act .ok[aria-pressed=true]{background:var(--ok);color:#fff;border-color:var(--ok)}.act .mb[aria-pressed=true]{background:var(--mb);color:#fff;border-color:var(--mb)}.act .no[aria-pressed=true]{background:var(--no);color:#fff;border-color:var(--no)}
.rc{font-size:12px;border-radius:14px;padding:2px 10px;min-height:26px}.rc[aria-pressed=true]{background:var(--acc);color:var(--card);border-color:var(--acc)}
.reasons{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:8px 0}.reasons .lbl{font-size:13px;color:var(--mut)}
kbd{font:11px monospace;opacity:.7;margin-left:4px}.media{display:flex;gap:10px;margin:12px 0;flex-wrap:wrap}.media video{width:min(100%,480px);border-radius:6px;background:#000}
.thumbs{display:flex;gap:8px;flex-wrap:wrap}figure{margin:0}.thumbs img{width:200px;max-width:30vw;border-radius:5px;display:block;background:#0002}figcaption{font-size:11px;color:var(--mut);text-align:center}
.hook{margin:6px 0}.hq,.sig{margin:2px 0;color:var(--mut)}.sig.inc{color:var(--warn,#b45309)}.tight{margin:4px 0;font-size:13px}.tight .mut{color:var(--mut)}.chips{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}.chip{font-size:12px;border-radius:12px;padding:2px 9px;border:1px solid var(--line)}
.chip.tag{color:var(--acc);border-color:var(--acc)}.chip.bad{color:var(--no);border-color:var(--no)}.chip.warn{color:var(--warn);border-color:var(--warn)}.badge{font-size:12px;border-radius:10px;padding:1px 8px;vertical-align:middle;border:1px solid var(--line);font-weight:500;margin-left:4px}
.badge.approved{color:var(--ok);border-color:var(--ok)}.badge.rejected{color:var(--no);border-color:var(--no)}.badge.maybe{color:var(--mb);border-color:var(--mb)}.badge.heur{color:var(--mut)}
.cols{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(220px,1fr);gap:18px}@media(max-width:800px){.cols{grid-template-columns:1fr}.thumbs img{width:30vw}}
.tx{border:1px solid var(--line);border-radius:6px;overflow:hidden}.s{display:grid;grid-template-columns:48px 28px 1fr auto;gap:6px;padding:4px 8px;align-items:baseline;border-bottom:1px solid var(--line)}
.s:last-child{border:0}.s.ctx{color:var(--mut)}.s.on{background:var(--in)}.s.first{box-shadow:inset 3px 0 var(--acc)}.s.last{box-shadow:inset -3px 0 var(--no)}.s.first.last{box-shadow:inset 3px 0 var(--acc),inset -3px 0 var(--no)}
.s .t,.s .sp{font:12px monospace;color:var(--mut)}.s del{opacity:.6}.s .b{opacity:0;display:flex;gap:4px}.s:hover .b,.s:focus-within .b{opacity:1}.s .b button{padding:0 7px;font-size:12px;min-height:22px}
.bar{display:grid;grid-template-columns:105px 1fr 26px 30px;gap:6px;align-items:center;font-size:13px}.bar i{height:7px;border-radius:4px;background:linear-gradient(90deg,var(--acc) var(--v),var(--line) var(--v))}.bar em{color:var(--mut);font-style:normal;font-size:11px}
.why{margin:10px 0}.nudge{margin:6px 0;color:var(--mut);font-size:13px}.note{width:100%;border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:6px;padding:5px 8px;font:inherit}
.alts{font-size:13px;margin:8px 0}.alts ul{margin:6px 0;padding-left:18px}.alts li{margin:4px 0}
button.link{border:0;background:none;color:var(--acc);padding:0;text-decoration:underline;min-height:0}.warnbox{border:1px solid var(--warn);color:var(--warn);border-radius:8px;padding:8px 12px;margin-bottom:14px}
textarea#json{width:100%;min-height:120px;font:12px monospace;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px}
.sr{position:absolute;left:-9999px}
"""

JS = """
(function(){
var B=document.body.dataset,EP=B.ep,FP=B.fp,SES=B.session,LO=+B.min,HI=+B.max,BY=B.by;
var KEY='ave-clips:'+EP+':'+FP,state={};
try{state=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){state={}}
function save(){try{localStorage.setItem(KEY,JSON.stringify(state))}catch(e){}}
var main=document.querySelector('main'),cards=[].slice.call(document.querySelectorAll('.card'));
function cur(card){var s=state[card.dataset.id];if(!s){s=state[card.dataset.id]={verdict:null,reasons:[],nudge:{start:0,end:0},note:''}}if(!s.reasons)s.reasons=[];return s}
function fmt(n){return (n>0?'+':'')+n}
function paint(card){
  var s=cur(card),i0=+card.dataset.i0+s.nudge.start,j0=+card.dataset.j0+s.nudge.end,rows=[].slice.call(card.querySelectorAll('.s'));
  var t0=null,t1=null;
  rows.forEach(function(r){var k=+r.dataset.i,on=k>=i0&&k<=j0;r.classList.toggle('on',on);r.classList.toggle('first',k===i0);r.classList.toggle('last',k===j0);
    if(on){t0=t0===null?+r.dataset.t0:t0;t1=+r.dataset.t1}});
  var d=card.querySelector('.dur'),est=t0===null?0:t1-t0,moved=s.nudge.start||s.nudge.end;
  d.textContent=(moved?'~':'')+Math.round(moved?est:+d.dataset.base)+'s';d.style.color=(moved&&(est<LO||est>HI))?'var(--no)':'';
  card.querySelector('.nv').textContent=moved?('start '+fmt(s.nudge.start)+' sentences, end '+fmt(s.nudge.end)+' (re-snapped when applied)'):'as proposed';
  ['keep','maybe','skip'].forEach(function(v){card.classList.toggle('dec-'+v,s.verdict===v);card.querySelector('[data-act='+v+']').setAttribute('aria-pressed',s.verdict===v?'true':'false')});
  [].forEach.call(card.querySelectorAll('.rc'),function(b){b.setAttribute('aria-pressed',s.reasons.indexOf(b.dataset.reason)>=0?'true':'false')});
  card.querySelector('.undo').hidden=!(s.verdict||moved||s.reasons.length);card.querySelector('.note').value=s.note||'';
  card.dataset.live=s.verdict||(card.dataset.status.indexOf('approved')===0?'keep':card.dataset.status.indexOf('rejected')===0?'skip':card.dataset.status.indexOf('maybe')>0?'maybe':'undecided');
}
function counts(){
  var n=Object.keys(state).filter(function(k){return state[k].verdict||state[k].nudge.start||state[k].nudge.end}).length;
  document.getElementById('count').textContent=n+' of '+cards.length+' changed on this page';
  document.getElementById('json').value=JSON.stringify(payload(),null,2);
}
function payload(){
  var out=[];cards.forEach(function(c){var s=state[c.dataset.id];if(!s)return;var moved=s.nudge.start||s.nudge.end;
    if(!s.verdict&&!moved)return;
    var d={clip_id:c.dataset.id};if(s.verdict){d.verdict=s.verdict;d.reasons=s.reasons.slice()}else d.decision='nudge';
    if(moved)d.nudge={start:s.nudge.start,end:s.nudge.end};if(s.note)d.note=s.note;out.push(d)});
  var p={kind:'ave-clip-decisions',v:1,episode:EP.split(':')[0],session:SES||null,fp:FP,exported:new Date().toISOString(),decisions:out};
  if(BY)p.by=BY;return p;
}
function setVerdict(card,v){var s=cur(card);if(s.verdict===v){s.verdict=null}else{s.verdict=v}save();paint(card);counts()}
cards.forEach(function(card){
  card.addEventListener('click',function(e){
    var b=e.target.closest('button[data-act],button.rc');if(!b)return;var s=cur(card);
    if(b.classList.contains('rc')){var r=b.dataset.reason,k=s.reasons.indexOf(r);if(k>=0)s.reasons.splice(k,1);else s.reasons.push(r);save();paint(card);counts();return}
    var a=b.dataset.act,i0=+card.dataset.i0,j0=+card.dataset.j0,row=b.closest('.s'),k2=row?+row.dataset.i:null;
    if(a==='keep'||a==='maybe'||a==='skip'){setVerdict(card,a);return}
    else if(a==='undo'||a==='reset'){s.nudge={start:0,end:0};if(a==='undo'){s.verdict=null;s.reasons=[]}}
    else if(a==='start'&&k2<=j0+s.nudge.end)s.nudge.start=k2-i0;
    else if(a==='end'&&k2>=i0+s.nudge.start)s.nudge.end=k2-j0;
    else if(a==='alt'){s.nudge={start:+b.dataset.di,end:+b.dataset.dj}}
    save();paint(card);counts();
  });
  card.querySelector('.note').addEventListener('input',function(e){cur(card).note=e.target.value;save();counts()});
  paint(card);
});
function download(){
  var blob=new Blob([JSON.stringify(payload(),null,2)+'\\n'],{type:'application/json'}),a=document.createElement('a');
  a.href=URL.createObjectURL(blob);a.download='clip-decisions'+(SES?'.'+SES:'')+'.json';document.body.appendChild(a);a.click();a.remove();
}
document.getElementById('dl').addEventListener('click',download);
// sort + group (pure DOM reordering; nothing is fetched)
var sortSel=document.getElementById('sort'),groupSel=document.getElementById('group'),hideSel=document.getElementById('hide');
var KEYS={score:function(c){return -c.dataset.score},time:function(c){return +c.dataset.start},length:function(c){return -c.dataset.dur}};
function groupOf(c,mode){
  if(mode==='tag')return c.dataset.tag;
  if(mode==='decision')return c.dataset.live;
  if(mode==='band'){var s=+c.dataset.score;return s>=60?'60 and up':s>=45?'45 to 60':s>=30?'30 to 45':'below 30'}
  return '';
}
function layout(){
  [].forEach.call(main.querySelectorAll('.grp'),function(h){h.remove()});
  var k=KEYS[sortSel.value],mode=groupSel.value,hide=hideSel.checked;
  var list=cards.slice().sort(function(a,b){var d=k(a)-k(b);return d||(+a.dataset.start-+b.dataset.start)});
  var seen={},order=[];
  list.forEach(function(c){var g=groupOf(c,mode);if(!(g in seen)){seen[g]=[];order.push(g)}seen[g].push(c)});
  order.forEach(function(g){
    if(mode!=='none'){var h=document.createElement('h2');h.className='grp';h.textContent=g+' ('+seen[g].length+')';main.appendChild(h)}
    seen[g].forEach(function(c){c.hidden=hide&&c.dataset.live!=='undecided';main.appendChild(c)});
  });
}
sortSel.addEventListener('change',layout);groupSel.addEventListener('change',layout);hideSel.addEventListener('change',layout);
var at=0;function visible(){return cards.filter(function(c){return !c.hidden}).sort(function(a,b){return (a.compareDocumentPosition(b)&4)?-1:1})}
function focusAt(n){var v=visible();if(!v.length)return;at=Math.max(0,Math.min(v.length-1,n));v[at].focus();v[at].scrollIntoView({block:'center'})}
document.addEventListener('keydown',function(e){
  var t=e.target.tagName;if(t==='INPUT'||t==='TEXTAREA'||t==='SELECT'||e.metaKey||e.ctrlKey||e.altKey)return;
  var card=document.activeElement.closest&&document.activeElement.closest('.card'),v=visible();
  if(e.key==='j')focusAt(card?v.indexOf(card)+1:0);else if(e.key==='k')focusAt(card?v.indexOf(card)-1:0);
  else if(card&&(e.key==='y'||e.key==='m'||e.key==='s')){setVerdict(card,e.key==='y'?'keep':e.key==='m'?'maybe':'skip')}
  else if(card&&e.key==='u'){var s=cur(card);s.verdict=null;s.reasons=[];s.nudge={start:0,end:0};save();paint(card);counts()}
  else if(e.key==='d')download();
});
layout();counts();
})();
"""


def render_html(ep: Episode, data: dict, S: C.Sentences, names: dict, drafts: dict, notes: list[str], name: str | None = None) -> str:
    from . import learning
    rubric = data["rubric"]
    reasons = list(learning.load_reasons(ep).values())
    cl = sorted(data["clips"], key=lambda c: (c["status"] == "rejected", -c["total"]))
    cards = "\n".join(_card(c, S, names, drafts, rubric, reasons) for c in cl)
    open_ = [c for c in cl if c["status"] == "proposed" and not c.get("maybe")]
    src = data["source"]
    meta = read_title(ep)
    box = f'<div class="warnbox">{"<br>".join(html.escape(n) for n in notes)}</div>' if notes else ""
    lim = rubric["limits"]
    flag = f" --name {html.escape(name)}" if name else ""
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clip review - {html.escape(meta)}</title><style>{CSS}</style></head>
<body data-ep="{html.escape(ep.root.name + (':' + name if name else ''))}" data-session="{html.escape(name or '')}" data-by="" data-fp="{src["fingerprint"]}" data-min="{lim["hard_min_s"]}" data-max="{lim["hard_max_s"]}">
<div class="top"><h1>Clip review: {html.escape(meta)}</h1>
<span class="sum">All {len(cl)} surfaced sections from {C._label(src["stage"], name)} v{src["version"]} &middot; {len(open_)} awaiting a decision &middot; <span id="count" aria-live="polite"></span><br>
<small>Keys: j/k next/previous, y keep, m maybe, s skip, u undo, d download. Hover a sentence for in/out buttons.</small></span>
<label>Sort <select id="sort"><option value="score">Score, high to low</option><option value="time">Time in the episode</option><option value="length">Length, long to short</option></select></label>
<label>Group <select id="group"><option value="none">No grouping</option><option value="tag">By tag</option><option value="band">By score band</option><option value="decision">By decision</option></select></label>
<label><input type="checkbox" id="hide"> hide decided</label>
<button type="button" id="dl">Download decisions</button>
<small>then run <code>ave clips import-decisions {html.escape(str(ep.root))} ~/Downloads/clip-decisions{"." + html.escape(name) if name else ""}.json{flag}</code></small></div>
<main>{box}{cards}</main>
<details style="max-width:1180px;margin:0 auto;padding:0 16px 24px"><summary>Decisions as text (copy this if the download is blocked)</summary><label class="sr" for="json">decisions JSON</label><textarea id="json" readonly></textarea></details>
<script>{JS}</script></body></html>
'''


def read_title(ep: Episode) -> str:
    try:
        from .episode import read_json
        return read_json(ep.meta_path).get("title") or ep.root.name
    except Exception:
        return ep.root.name


def _session_view(data: dict, cand: dict, name: str | None, notes: list[str]) -> dict:
    """What the page shows. Named: that session's clips whose sentence table is the current one (a stale pick's ids index an
    older table, so it cannot be drawn against this one; it is hidden and said so). Unnamed: everything, as before."""
    if not name:
        return data
    mine = C.session_clips(data, name)
    fresh = [c for c in mine if not c.get("stale")]
    if len(fresh) < len(mine):
        notes.append(f"{len(mine) - len(fresh)} stale clip(s) hidden ({', '.join(c['id'] for c in mine if c.get('stale'))}): picked from an older "
                     f"{C._label(cand['source']['stage'], name)}; re-pick them after `ave clips prepare --name {name}`.")
    return {"rubric": data["rubric"], "clips": fresh, "source": cand["source"]}


def build_pack(ep: Episode, drafts: bool = False, out_dir: Path | None = None, name: str | None = None) -> dict:
    """Write exports/review[/<name>]/index.html (+ thumbs/, + drafts/ when asked). Returns a JSON-able report."""
    name = C.resolve_name(ep, name)
    data = C.load_clips(ep)
    if not data or not C.session_clips(data, name):
        raise SystemExit("No clips to review. Run: ave clips propose <episode> --picks picks.json" + (f" --name {name}" if name else ""))
    cand = C.load_candidates(ep, name)
    S, doc = C._live_table(ep, cand, data["rubric"])
    out = Path(out_dir) if out_dir else (ep.exports / "review" / name if name else ep.exports / "review")
    out.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    made: dict = {}
    if drafts:
        C.build(ep, name=name)   # drafts render the per-clip select docs
        data = C.load_clips(ep)
    view = _session_view(data, cand, name, notes)
    shown = [c for c in view["clips"] if c["status"] != "rejected"]
    if drafts:
        made, dn = render_drafts(ep, shown, out)
        notes += dn
    names, errs = make_thumbs(ep, doc, shown, out)
    notes += [f"thumbnail failed: {e}" for e in errs]
    index = out / "index.html"
    tmp = index.with_suffix(".html.tmp")
    tmp.write_text(render_html(ep, view, S, names, made, notes, name))
    tmp.replace(index)
    rep = {"index": str(index), "clips": len(view["clips"]), "thumbnails": sum(len(v) for v in names.values()) - len(errs),
           "drafts": sorted(made), "notes": notes, "open": f"open {index}"}
    return {"name": name, **rep} if name else rep
