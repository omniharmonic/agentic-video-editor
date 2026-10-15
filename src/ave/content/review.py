"""Human review: a static contact sheet of every item, and `decide` to apply what the human chose.

Same shape as `ave clips review` / `ave clips decide`: one self-contained page (no server, nothing sent anywhere) whose
buttons write a `decisions.json` the human downloads, and a command that applies it as a NEW ContentDoc version. The page
shows what a reviewer needs to judge words and design together: the rendered images (with a phone-size toggle), the source
passage with the quote marked, who said it and when, the critic's measured verdict (fit, WCAG contrast, text size), the
agent's reason and confidence, alt text and caption. Approval is bound to the exact bytes: `decide` re-hashes every file and
refuses if one changed since it was rendered.
"""

from __future__ import annotations

import html
import json
import os
import time
from pathlib import Path

from .. import clips as C
from ..episode import Episode
from ..publish import safety
from . import manifest as M
from . import registry as R
from . import sources as S

DECISIONS_LOG = "decisions.jsonl"


# ---- helpers --------------------------------------------------------------------------------------

def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def mark_quote(text: str, quote: str) -> str:
    """HTML of `text` with the words of `quote` wrapped in <mark> (token-aligned, so punctuation/case differences still match)."""
    words = text.split()
    toks = [C.norm_token(w) for w in words]
    q = C.norm_tokens(quote)
    idx = [i for i, t in enumerate(toks) if t]
    flat = [toks[i] for i in idx]
    for k in range(len(flat) - len(q) + 1):
        if q and flat[k:k + len(q)] == q:
            a, b = idx[k], idx[k + len(q) - 1]
            return (html.escape(" ".join(words[:a])) + (" " if a else "") + "<mark>" + html.escape(" ".join(words[a:b + 1])) + "</mark>"
                    + (" " if b + 1 < len(words) else "") + html.escape(" ".join(words[b + 1:])))
    return html.escape(text)


def _unique_files(item: dict) -> list[tuple[str, str]]:
    """(file, alt text) once per image, in order (outputs repeat a file for every platform)."""
    seen, out = set(), []
    for o in item["outputs"]:
        if o["file"] not in seen:
            seen.add(o["file"])
            out.append((o["file"], o["alt_text"]))
    return out


# ---- the page -------------------------------------------------------------------------------------

def _card(ep: Episode, doc: dict, item: dict, reg: R.Registry, out_dir: Path) -> str:
    tpl = reg.get(item["template"])
    crit = (item["review"] or {}).get("critic")
    files = _unique_files(item)
    imgs = "".join(f'<figure><img loading="lazy" src="{html.escape(os.path.relpath(ep.root / f, out_dir))}" alt="{html.escape(alt)}">'
                   + (f"<figcaption>slide {n}</figcaption>" if len(files) > 1 else "") + "</figure>"
                   for n, (f, alt) in enumerate(files, 1)) or '<p class="warn">Not rendered yet: run <code>ave content render</code>.</p>'
    badges = ""
    if crit and crit.get("measured") is False:
        badges = '<span class="chip bad">not measured (Canva render): LOOK at fit, contrast and text size yourself</span>'
        badges += "".join(f'<p class="note bad">{html.escape(n)}</p>' for n in crit["notes"])
    elif crit:
        ok = lambda b: "ok" if b else "bad"   # noqa: E731
        badges = (f'<span class="chip {ok(crit["fits"])}">fit {"inside safe area" if crit["fits"] else "OVERFLOWS"}</span>'
                  f'<span class="chip {ok(crit["contrast_ok"])}">contrast {crit["contrast"]:.1f}:1</span>'
                  f'<span class="chip {ok(crit["text_size_ok"])}">text size {"ok" if crit["text_size_ok"] else "too small"}</span>')
        badges += "".join(f'<p class="note bad">{html.escape(n)}</p>' for n in crit["notes"])
    src_html = ""
    ids = [item.get("source")] + [s.get("source") for lst in item["slots"].values() if isinstance(lst, list) for s in lst if isinstance(s, dict)]
    quotes = [q for q in R.walk_flagged(tpl.slots, R.with_defaults(tpl.slots, item["slots"]), "x-verbatim")]
    shown = set()
    for path, text, parent in quotes:
        sid = parent.get("source") or item.get("source")
        s = doc["sources"].get(sid) if sid else None
        if not s or not s.get("text"):
            continue
        where = ""
        for q in (item["provenance"].get("quotes") or []):
            if q["slot"] == path and q.get("located"):
                where = f' &middot; {html.escape(q.get("speaker_name") or q.get("speaker") or "")} at {_mmss(q["t0"])}'
        label = s.get("title") or s.get("file") or sid
        src_html += (f'<div class="src"><b>Verbatim from {html.escape(str(label))}</b>{where}<p>{mark_quote(s["text"], text)}</p></div>')
        shown.add(sid)
    warns = "".join(f'<p class="note warn">{html.escape(w)}</p>' for w in item.get("warnings", []))
    prov = item["provenance"]
    st = item["review"]["status"]
    alt_rows = "".join(f'<li>{html.escape(alt)}</li>' for _, alt in _unique_files(item))
    styles = "".join(f'<option value="{s}"{" selected" if s == item["style"] else ""}>{s}</option>' for s in tpl.styles)
    fmts = "".join(f'<option value="{f}"{" selected" if f == item["format"] else ""}>{f}</option>' for f in tpl.formats)
    return f'''<article class="card {st}" id="{html.escape(item["id"])}" tabindex="0" data-id="{html.escape(item["id"])}" data-style="{item["style"]}" data-format="{item["format"]}">
<header><div><h2>{html.escape(item["id"])} <span class="badge {st}">{st}</span></h2>
<p class="meta">{html.escape(tpl.title)} &middot; {item["style"]} &middot; {item["format"]} &middot; {", ".join(html.escape(p) for p in item["platforms"]) or "no platform"} &middot; confidence {item["confidence"]:.0%}</p></div>
<div class="act"><button type="button" class="ok" data-act="approve">Approve <kbd>a</kbd></button><button type="button" class="no" data-act="reject">Reject <kbd>r</kbd></button><button type="button" class="undo" data-act="undo" hidden>Undo</button></div></header>
<div class="imgs">{imgs}</div>
<div class="chips">{badges}</div>
<p class="why"><b>Why:</b> {html.escape(item["reason"])}</p>{warns}
{src_html}
<details><summary>Alt text, caption, provenance</summary><ol class="alt">{alt_rows}</ol><p><b>Caption:</b> {html.escape(item.get("caption") or "(derived from the slots at queue time)")}</p>
<p class="meta">renderer {html.escape(str(prov.get("renderer_version")))} &middot; template rev {html.escape(str(prov.get("template_rev")))} &middot; ai-generated {"yes" if item["license"]["ai_generated"] else "no"} &middot; assets {len(item["license"]["assets"])}</p></details>
<div class="nudge">Nudge: style <select data-k="style">{styles}</select> format <select data-k="format">{fmts}</select>
<input class="note-in" type="text" placeholder="note (optional)" aria-label="note for {html.escape(item["id"])}"></div>
</article>'''


CSS = """
:root{--bg:#f7f6f3;--fg:#1d1d1b;--mut:#6b6a66;--card:#fff;--line:#dedbd3;--acc:#2f5d50;--ok:#2f7d4f;--no:#a33a3a;--warn:#8a6a1c}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#161715;--fg:#ecebe6;--mut:#a09f98;--card:#1f201d;--line:#34352f;--acc:#7fb5a3;--ok:#6fbf8d;--no:#e08585;--warn:#d9b45a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.top h1{font-size:17px;margin:0}.top .sum{color:var(--mut);flex:1;min-width:220px}.top code{background:var(--card);border:1px solid var(--line);padding:2px 6px;border-radius:4px}
main{max-width:1240px;margin:0 auto;padding:16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}.card:focus{outline:2px solid var(--acc)}
.card.rejected,.card.dec-reject{opacity:.55}.card.approved,.card.dec-approve{border-color:var(--ok)}.card.dec-reject{border-color:var(--no)}
header{display:flex;gap:12px;align-items:flex-start}header h2{font-size:16px;margin:0;overflow-wrap:anywhere}.meta{margin:2px 0 0;color:var(--mut);font-size:13px}
.act{margin-left:auto;display:flex;gap:8px;flex-shrink:0}button,select{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:5px 11px;cursor:pointer}
button.ok:hover,.dec-approve .ok{background:var(--ok);color:#fff;border-color:var(--ok)}button.no:hover,.dec-reject .no{background:var(--no);color:#fff;border-color:var(--no)}
kbd{font:11px monospace;opacity:.7;margin-left:4px}
.imgs{display:flex;gap:10px;overflow-x:auto;margin:12px 0;padding-bottom:4px}figure{margin:0;flex:0 0 auto}.imgs img{display:block;width:min(100%,380px);max-width:70vw;border-radius:4px;border:1px solid var(--line);background:#0002}
body.phone .imgs img{width:390px;max-width:none}figcaption{font-size:11px;color:var(--mut);text-align:center}
.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{font-size:12px;border-radius:12px;padding:2px 9px;border:1px solid var(--line)}.chip.ok{color:var(--ok);border-color:var(--ok)}.chip.bad{color:var(--no);border-color:var(--no)}
.note{margin:6px 0 0;font-size:13px;flex-basis:100%}.note.bad{color:var(--no)}.note.warn,.warn{color:var(--warn)}
.badge{font-size:12px;border-radius:10px;padding:1px 8px;vertical-align:middle;border:1px solid var(--line);font-weight:500}.badge.approved{color:var(--ok);border-color:var(--ok)}.badge.rejected{color:var(--no);border-color:var(--no)}
.src{margin:10px 0;border-left:3px solid var(--acc);padding:2px 10px;font-size:14px;color:var(--mut)}.src p{margin:4px 0 0}mark{background:#d9ead3;color:inherit;padding:0 1px;border-radius:2px}
@media (prefers-color-scheme:dark){mark{background:#2d4a3c}}
details{margin:8px 0;color:var(--mut);font-size:13px}.alt{margin:6px 0;padding-left:20px}.nudge{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;color:var(--mut);font-size:13px}
.note-in{flex:1;min-width:140px;border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:6px;padding:5px 8px;font:inherit}
.why{margin:8px 0}.tog{display:flex;align-items:center;gap:6px;color:var(--mut);font-size:13px}
"""

JS = """
(function(){
var EP=document.body.dataset.ep,FP=document.body.dataset.fp,KEY='ave-content:'+EP+':'+FP,state={};
try{state=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){state={}}
function save(){try{localStorage.setItem(KEY,JSON.stringify(state))}catch(e){}}
var cards=[].slice.call(document.querySelectorAll('.card'));
function cur(c){return state[c.dataset.id]||(state[c.dataset.id]={decision:null,style:c.dataset.style,format:c.dataset.format,note:''})}
function nudged(c,s){return s.style!==c.dataset.style||s.format!==c.dataset.format}
function paint(c){var s=cur(c);c.classList.toggle('dec-approve',s.decision==='approve');c.classList.toggle('dec-reject',s.decision==='reject');
  c.querySelector('[data-k=style]').value=s.style;c.querySelector('[data-k=format]').value=s.format;c.querySelector('.note-in').value=s.note||'';
  c.querySelector('.undo').hidden=!(s.decision||nudged(c,s));
  var n=Object.keys(state).filter(function(k){var d=state[k];return d.decision||d.style!==(document.getElementById(k)||{dataset:{}}).dataset.style||d.format!==(document.getElementById(k)||{dataset:{}}).dataset.format}).length;
  document.getElementById('count').textContent=n+' of '+cards.length+' decided';}
cards.forEach(function(c){
  c.addEventListener('click',function(e){var b=e.target.closest('button[data-act]');if(!b)return;var s=cur(c),a=b.dataset.act;
    if(a==='approve'||a==='reject')s.decision=(s.decision===a?null:a);else if(a==='undo'){s.decision=null;s.style=c.dataset.style;s.format=c.dataset.format}
    save();paint(c)});
  c.addEventListener('change',function(e){var k=e.target.dataset.k;if(k){cur(c)[k]=e.target.value;save();paint(c)}});
  c.querySelector('.note-in').addEventListener('input',function(e){cur(c).note=e.target.value;save()});
  paint(c);
});
function download(){var out=[];cards.forEach(function(c){var s=state[c.dataset.id];if(!s)return;var n=nudged(c,s);
  if(!s.decision&&!n&&!s.note)return;var d={id:c.dataset.id,decision:n&&!s.decision?'nudge':(s.decision||'pending')};
  if(n){d.nudge={};if(s.style!==c.dataset.style)d.nudge.style=s.style;if(s.format!==c.dataset.format)d.nudge.format=s.format;if(s.decision==='approve'){d.decision='nudge'}}
  if(s.note)d.note=s.note;out.push(d)});
  var blob=new Blob([JSON.stringify(out,null,2)+'\\n'],{type:'application/json'}),a=document.createElement('a');
  a.href=URL.createObjectURL(blob);a.download='decisions.json';document.body.appendChild(a);a.click();a.remove()}
document.getElementById('dl').addEventListener('click',download);
document.getElementById('phone').addEventListener('change',function(e){document.body.classList.toggle('phone',e.target.checked)});
var at=0;function focus(n){at=Math.max(0,Math.min(cards.length-1,n));cards[at].focus();cards[at].scrollIntoView({block:'center'})}
document.addEventListener('keydown',function(e){if(/INPUT|SELECT/.test(e.target.tagName)||e.metaKey||e.ctrlKey)return;
  var c=document.activeElement.closest&&document.activeElement.closest('.card');
  if(e.key==='j')focus(c?cards.indexOf(c)+1:0);else if(e.key==='k')focus(c?cards.indexOf(c)-1:0);
  else if(c&&(e.key==='a'||e.key==='r'||e.key==='u')){var s=cur(c);if(e.key==='u'){s.decision=null;s.style=c.dataset.style;s.format=c.dataset.format}else s.decision=(s.decision===(e.key==='a'?'approve':'reject')?null:(e.key==='a'?'approve':'reject'));save();paint(c)}
  else if(e.key==='d')download()});
})();
"""


def doc_fingerprint(doc: dict) -> str:
    import hashlib
    return hashlib.sha1(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:12]


def render_html(ep: Episode, doc: dict, reg: R.Registry, out_dir: Path, version: int) -> str:
    cards = "\n".join(_card(ep, doc, it, reg, out_dir) for it in doc["items"])
    n_draft = sum(1 for i in doc["items"] if i["review"]["status"] == "draft")
    n_bad = sum(1 for i in doc["items"] if (i["review"].get("critic") or {}).get("ok") is False)
    title = html.escape(ep.root.name)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Content review - {title}</title><style>{CSS}</style></head>
<body data-ep="{title}" data-fp="{doc_fingerprint(doc)}">
<div class="top"><h1>Content review: {title}</h1>
<span class="sum">{len(doc["items"])} items from {html.escape(doc["id"])} v{version} &middot; {n_draft} awaiting a decision &middot; {n_bad} flagged by the critic &middot; <span id="count"></span><br>
<small>Keys: j/k next/previous, a approve, r reject, u undo, d download. Nothing is sent anywhere; approving here does not publish.</small></span>
<label class="tog"><input type="checkbox" id="phone"> phone size (390 px)</label>
<button type="button" id="dl">Download decisions.json</button>
<small>then run <code>ave content decide {html.escape(str(ep.root))} ~/Downloads/decisions.json</code></small></div>
<main>{cards}</main><script>{JS}</script></body></html>
'''


def build_review(ep: Episode, doc_id: str = "main", out_dir: Path | None = None, reg: R.Registry | None = None) -> dict:
    """Write `<work>/content/review/index.html`. Image paths in the page are relative, so the folder can be moved with `renders/`."""
    reg = reg or R.load_registry()
    path = M.latest_path(ep, doc_id)
    if not path:
        raise SystemExit(f"No content plan yet for {ep.root.name}. Run ave content propose first.")
    doc = M.load(path)
    if not doc["items"]:
        raise SystemExit("The plan has no items to review.")
    out = Path(out_dir) if out_dir else M.content_dir(ep) / "review"
    out.mkdir(parents=True, exist_ok=True)
    version = M.list_versions(ep, doc_id)[-1]
    index = out / "index.html"
    tmp = index.with_suffix(".html.tmp")
    tmp.write_text(render_html(ep, doc, reg, out, version))
    tmp.replace(index)
    unrendered = [i["id"] for i in doc["items"] if not i["outputs"]]
    return {"index": str(index), "doc": path.name, "items": len(doc["items"]), "unrendered": unrendered,
            "critic_flagged": [i["id"] for i in doc["items"] if (i["review"].get("critic") or {}).get("ok") is False],
            "open": f"open {index}"}


# ---- decide ---------------------------------------------------------------------------------------

def _log(ep: Episode, record: dict) -> None:
    p = M.content_dir(ep) / DECISIONS_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _bytes_problems(ep: Episode, item: dict) -> list[str]:
    p = []
    for o in item["outputs"]:
        f = ep.root / o["file"]
        if not f.is_file():
            p.append(f"{o['file']} is missing")
        elif safety.sha256_file(f) != o["sha256"]:
            p.append(f"{o['file']} changed on disk since it was rendered; re-render and review again")
    return p


def decide(ep: Episode, decisions, doc_id: str = "main", by: str = "", reg: R.Registry | None = None) -> dict:
    """Apply a decisions file as a NEW doc version. Each decision is {id, decision: approve|reject|nudge|pending, note?, override?, nudge?}.

    approve   needs a rendered item whose files still match their recorded hashes, alt text on every image, and a passing critic
              (a failing critic needs `"override": true` and a note saying why the human accepts it)
    reject    marks the item rejected (also voids an earlier approval)
    nudge     {style?, format?, slots?, alt_text?} changes the item and clears its renders: it must be rendered and reviewed again
              (alt_text alone only rewrites the description)
    Nothing is published: approved items are queued by `ave content queue`, and the human approves THAT in `ave publish approve`.
    """
    reg = reg or R.load_registry()
    if isinstance(decisions, dict):
        decisions = decisions.get("decisions")
    if not isinstance(decisions, list):
        raise SystemExit("decisions.json must be a JSON list of {id, decision, ...} (download it from the review page)")
    if not (by or "").strip():
        raise SystemExit("approver name required (--by)")
    doc = M.clone(M.load_latest(ep, doc_id))
    applied, refused, ignored = [], [], []
    for d in decisions:
        iid = d.get("id") if isinstance(d, dict) else None
        try:
            item = M.find_item(doc, iid)
        except KeyError as e:
            refused.append({"id": iid, "problems": [str(e)]})
            continue
        kind = d.get("decision")
        rv = item["review"]
        if kind == "approve":
            problems = []
            if not item["outputs"]:
                problems.append("not rendered yet (run ave content render)")
            problems += _bytes_problems(ep, item)
            if any(not o["alt_text"].strip() for o in item["outputs"]):
                problems.append("an image has no alt text")
            crit = rv.get("critic")
            override = bool(d.get("override"))
            if crit and not crit["ok"] and not override:
                problems.append("the critic flagged this item: " + "; ".join(crit["notes"]) +
                                '. Nudge or reject it, or approve with "override": true and a note')
            if override and not (d.get("note") or "").strip():
                problems.append("an override needs a note saying why the flagged item is acceptable")
            if rv["status"] == "rejected":
                problems.append("rejected items cannot be approved; ask the agent to propose it again")
            if problems:
                refused.append({"id": iid, "problems": problems})
                continue
            item["review"] = {**rv, "status": "approved", "by": by, "at": M.now(), "note": d.get("note", rv.get("note", "")),
                              **({"critic_override": True} if override else {})}
            applied.append({"id": iid, "decision": "approve"})
        elif kind == "reject":
            item["review"] = {**rv, "status": "rejected", "by": by, "at": M.now(), "note": d.get("note", "")}
            applied.append({"id": iid, "decision": "reject"})
        elif kind == "nudge":
            n = d.get("nudge") or {}
            if not isinstance(n, dict) or not n:
                refused.append({"id": iid, "problems": ["nudge needs {style?, format?, slots?, alt_text?}"]})
                continue
            if rv["status"] == "approved":
                item["review"] = {**rv, "status": "draft", "by": None, "at": None}
            trial = M.clone(item)
            alt_only = set(n) <= {"alt_text"}
            for k in ("style", "format"):
                if k in n:
                    trial[k] = n[k]
            if "slots" in n:
                trial["slots"] = {**trial["slots"], **n["slots"]}
            if "alt_text" in n:
                trial["alt_text"] = n["alt_text"]
            if not alt_only:
                trial["outputs"] = []
                trial["review"] = {"status": "draft", "critic": None, "by": None, "at": None, "note": d.get("note", "")}
            else:
                alts = [n["alt_text"]] if isinstance(n["alt_text"], str) else list(n["alt_text"])
                files = _unique_files(trial)
                if len(alts) != len(files):
                    refused.append({"id": iid, "problems": [f"alt_text needs {len(files)} description(s), got {len(alts)}"]})
                    continue
                by_file = dict(zip([f for f, _ in files], alts))
                for o in trial["outputs"]:
                    o["alt_text"] = by_file[o["file"]]
                trial["review"] = {**trial["review"], "status": "draft", "by": None, "at": None}
            probe = {**doc, "items": [trial]}
            problems = M.validate(probe, reg)
            if problems:
                refused.append({"id": iid, "problems": problems})
                continue
            item.clear()
            item.update(trial)
            applied.append({"id": iid, "decision": "nudge", "needs_render": not alt_only})
        elif kind in ("pending", None):
            ignored.append(iid)
        else:
            refused.append({"id": iid, "problems": [f"unknown decision {kind!r} (approve, reject, nudge)"]})
        _log(ep, {"at": M.now(), "by": by, "id": iid, "decision": kind, "note": d.get("note"), "override": bool(d.get("override"))})
    out = {"applied": applied, "refused": refused, "ignored": ignored}
    if applied:
        path = M.save_new_version(ep, doc, reg)
        out.update(doc=str(path), version=M.list_versions(ep, doc_id)[-1])
    approved = [i["id"] for i in doc["items"] if i["review"]["status"] == "approved"]
    out["approved_items"] = approved
    needs = [a["id"] for a in applied if a.get("needs_render")]
    out["next"] = (f"ave content render <project>   (re-render nudged items: {', '.join(needs)})" if needs
                   else ("ave content queue <project>" if approved else "ave content review <project>"))
    return out
