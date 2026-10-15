"""README.md and the self-contained index.html for a release package. No network, no external fonts or scripts."""

from __future__ import annotations

import html
import json
from urllib.parse import quote as urlquote

STATE_MARK = {"ok": "done", "warn": "check", "missing": "MISSING", "draft": "draft", "skipped": "skipped"}


def human(n: int | float | None) -> str:
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def clock(s: float | None) -> str:
    if not s:
        return ""
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def readme(m: dict, pl: dict) -> str:
    L = [f"# Release package: {m['title']}", "",
         f"Package {m['date']}" + (f" (version {m['package_version']})" if m["package_version"] > 1 else "") + f", built {m['created']}.",
         "", "**Nothing in this package has been sent, uploaded, posted or approved for publishing.** The post copy is a draft for you to edit.", "",
         "## Status", ""]
    L.append("**Complete**: every required piece is here." if m["complete"] else "**Not complete**: " + ", ".join(m["missing_required"]) + " still missing.")
    L += ["", "| Piece | State | Detail |", "|---|---|---|"]
    for c in m["checklist"]:
        L.append(f"| {c['label']}{' (required)' if c['required'] else ''} | {STATE_MARK.get(c['state'], c['state'])} | {c['detail'].replace('|', '/')} |")
    main = [f for f in m["files"] if not f["draft"]]
    L += ["", "## What is in the folders", ""]
    for role, label in (("episode_video", "episode/"), ("captions_srt", "captions/"), ("podcast_audio", "audio/"), ("thumbnail", "thumbnails/"),
                        ("short", "shorts/<platform>/"), ("quote_card", "images/"), ("post_copy", "copy/")):
        fs = [f for f in main if f["role"] == role or (role == "quote_card" and f["role"] == "carousel_slide") or (role == "captions_srt" and f["role"] == "captions_vtt")]
        if fs:
            L.append(f"- `{label}`: " + ", ".join(f"`{f['package_path'].split('/')[-1]}` ({human(f['bytes'])})" for f in fs[:6]) + (f" and {len(fs) - 6} more" if len(fs) > 6 else ""))
    drafts = [f for f in m["files"] if f["draft"] and f["role"] != "draft_notice"]
    if drafts:
        L += ["", "## Drafts (NOT approved, not part of the package)", "",
              "These are in `drafts/`, each file name starts with `DRAFT-`. Approve them, then run `ave package` again (it makes a new version).", ""]
        L += [f"- `{f['package_path']}`: {'; '.join(f['draft_reasons'])}" for f in drafts]
    refs = [f for f in m["files"] if not f["copied"]]
    if refs:
        L += ["", "## Files referenced, not copied", "", "The volume did not have room for these with the free-space reserve, so the manifest points at them:", ""]
        L += [f"- `{f['reference']}` ({human(f['bytes'])}), sha256 `{f['sha256']}`" for f in refs]
    L += ["", "## Check the files", "", "```", "cd <this folder> && shasum -a 256 -c checksums.sha256", "```", "",
          "`manifest.json` lists every file with size, sha256, duration, the stage and version it came from, approval status and license credits. "
          "Open `index.html` to browse and play everything and to copy post text.", "",
          "## Publishing is yours", "",
          "`copy/` holds drafts per platform; `copy/copy.json` is the editable source. Fill every `{{PLACEHOLDER}}` and `TODO:` before using a line.", "",
          "To put the approved items into the publish queue as drafts (still nothing is sent):", "", "```", f"ave package queue {m['episode']}", "```", "",
          "Then YOU approve each one by typing the start of its sha256: `! ave publish approve <project> <id>`. Sending needs `ave publish run <id> --yes` after that."]
    if m["credits"]:
        L += ["", "## Credits", ""] + [f"- {c}" for c in m["credits"]]
    return "\n".join(L) + "\n"


def _e(x) -> str:
    return html.escape(str(x), quote=True)


def _src(f: dict) -> str:
    if f["copied"]:
        return urlquote(f["path"])
    return "file://" + urlquote(f["reference"])


CSS = """
:root{--bg:#f7f5f0;--panel:#fffdf8;--ink:#1f1d1a;--muted:#6a655d;--line:#ddd6c8;--accent:#2f5d50;--warn:#9a5b00;--bad:#a12b2b;--draft:#7a4a8a}
@media (prefers-color-scheme:dark){:root{--bg:#161512;--panel:#1e1c18;--ink:#ece7dc;--muted:#a39d90;--line:#37332b;--accent:#7fbfa9;--warn:#e0a24a;--bad:#e27b7b;--draft:#c89be0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 Georgia,'Times New Roman',serif}
header,main{max-width:1060px;margin:0 auto;padding:0 20px}header{padding-top:32px}
h1{font-size:1.9rem;margin:0 0 4px}h2{font-size:1.2rem;margin:34px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
.sub{color:var(--muted);font-family:Helvetica,Arial,sans-serif;font-size:.9rem}
.banner{margin:14px 0;padding:10px 14px;border:1px solid var(--line);background:var(--panel);border-radius:6px;font-family:Helvetica,Arial,sans-serif;font-size:.92rem}
table{border-collapse:collapse;width:100%;font-family:Helvetica,Arial,sans-serif;font-size:.88rem}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.s-ok{color:var(--accent)}.s-warn{color:var(--warn)}.s-missing{color:var(--bad);font-weight:bold}.s-draft{color:var(--draft)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px;font-family:Helvetica,Arial,sans-serif;font-size:.85rem;position:relative}
.card img,.card video{width:100%;display:block;border-radius:3px;background:#0002}.card .n{margin-top:6px;word-break:break-all}
.card.draft{border-style:dashed;border-color:var(--draft)}.mark{position:absolute;top:6px;left:6px;background:var(--draft);color:#fff;font-weight:bold;font-size:.7rem;padding:2px 8px;border-radius:3px;letter-spacing:.08em;z-index:2}
.wide video,.wide audio{width:100%}audio{width:100%}
pre{white-space:pre-wrap;word-wrap:break-word;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px;font:13.5px/1.5 ui-monospace,Menlo,monospace;margin:6px 0}
button{font:600 .8rem Helvetica,Arial,sans-serif;background:var(--accent);color:var(--bg);border:0;border-radius:4px;padding:6px 12px;cursor:pointer}
.copyblock{margin:14px 0}.copyblock .h{display:flex;gap:12px;align-items:center;font-family:Helvetica,Arial,sans-serif;font-size:.85rem}.over{color:var(--bad)}.prob{color:var(--warn);font-family:Helvetica,Arial,sans-serif;font-size:.82rem;margin:2px 0}
code{font-family:ui-monospace,Menlo,monospace;font-size:.85em}footer{color:var(--muted);font:.8rem Helvetica,Arial,sans-serif;padding:30px 20px;max-width:1060px;margin:0 auto}
@media(max-width:560px){body{font-size:15px}}
"""

JS = """
function copyText(id,btn){var t=document.getElementById(id).textContent;function ok(){var o=btn.textContent;btn.textContent='Copied';setTimeout(function(){btn.textContent=o},1200)}
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(ok,fallback)}else{fallback()}
function fallback(){var a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();try{document.execCommand('copy');ok()}catch(e){btn.textContent='Select and copy by hand'}document.body.removeChild(a)}}
"""


def _card(f: dict) -> str:
    kind = f["role"]
    s = _src(f)
    note = ""
    if not f["copied"]:
        note = f"<div class='prob'>{_e(f['flag'])}</div>"
    media = ""
    if kind in ("short", "episode_video"):
        media = f"<video controls preload='metadata' src='{s}'></video>"
    elif kind in ("thumbnail", "quote_card", "carousel_slide"):
        media = f"<img loading='lazy' src='{s}' alt='{_e(f.get('alt_text') or '')}'>"
    meta = [human(f["bytes"]), clock(f.get("duration_s")), f"{f['width']}x{f['height']}" if f.get("width") else "", f["approval"].get("status", "")]
    alt = f"<div>Alt text: {_e(f['alt_text'])}</div>" if f.get("alt_text") else ""
    why = f"<div class='prob'>{_e('; '.join(f['draft_reasons']))}</div>" if f["draft"] else ""
    return (f"<div class='card{' draft' if f['draft'] else ''}'>{'<span class=mark>DRAFT</span>' if f['draft'] else ''}{media}"
            f"<div class='n'><a href='{s}'>{_e(f['package_path'])}</a></div><div class='sub'>{_e(' · '.join(x for x in meta if x))}</div>{alt}{why}{note}</div>")


def index(m: dict, blocks: list[dict]) -> str:
    files = m["files"]
    main = [f for f in files if not f["draft"]]
    drafts = [f for f in files if f["draft"] and f["role"] != "draft_notice"]
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{_e(m['title'])} release package</title><style>{CSS}</style></head><body><header>"
           f"<h1>{_e(m['title'])}</h1><div class='sub'>Release package {_e(m['date'])}{' v' + str(m['package_version']) if m['package_version'] > 1 else ''}"
           f" · {m['totals']['main_files']} files · {_e(human(sum(f['bytes'] for f in main)))} · built {_e(m['created'])}</div>"
           f"<div class='banner'>{'Complete: every required piece is here.' if m['complete'] else 'Not complete: ' + _e(', '.join(m['missing_required'])) + ' still missing.'} "
           "Nothing has been sent, posted or approved. Post copy is a draft for you to edit.</div></header><main>"]
    out.append("<h2>Checklist</h2><table><tr><th>Piece</th><th>State</th><th>Detail</th></tr>")
    for c in m["checklist"]:
        out.append(f"<tr><td>{_e(c['label'])}{' *' if c['required'] else ''}</td><td class='s-{_e(c['state'])}'>{_e(STATE_MARK.get(c['state'], c['state']))}</td><td>{_e(c['detail'])}</td></tr>")
    out.append("</table><div class='sub'>* required for a complete package</div>")

    vid = [f for f in main if f["role"] == "episode_video"]
    aud = [f for f in main if f["role"] == "podcast_audio"]
    if vid or aud:
        out.append("<h2>Episode</h2><div class='wide'>")
        for f in vid:
            out.append(f"<video controls preload='metadata' src='{_src(f)}'></video><div class='sub'>{_e(f['package_path'])} · {_e(human(f['bytes']))} · {_e(clock(f.get('duration_s')))}"
                       f" · {_e(f['source'].get('stage', ''))} v{_e(f['source'].get('version', ''))} · sha256 <code>{_e((f['sha256'] or '')[:16])}</code></div>"
                       + (f"<div class='prob'>{_e(f['flag'])}</div>" if f.get("flag") else ""))
        for f in aud:
            lo = f.get("loudness") or {}
            out.append(f"<h3>Podcast audio</h3><audio controls preload='none' src='{_src(f)}'></audio><div class='sub'>{_e(f['package_path'])} · {_e(human(f['bytes']))} · "
                       f"{_e(lo.get('lufs', '?'))} LUFS (target {_e(lo.get('target_lufs', '?'))}) · {_e(f.get('chapters', 0))} chapters</div>")
        out.append("</div>")
    ch = m["chapters"]["items"]
    if ch:
        from .chapters import fmt_ts
        out.append("<h2>Chapters</h2>" + ("<div class='prob'>Titles are machine drafts: write work/chapters.json to replace them.</div>" if m["chapters"]["draft_titles"] else "")
                   + "<pre>" + _e("\n".join(f"{fmt_ts(c['start'])} {c['title']}" for c in ch)) + "</pre>")
    caps = [f for f in main if f["role"] in ("captions_srt", "captions_vtt")]
    if caps:
        out.append("<h2>Captions</h2><table>" + "".join(f"<tr><td><a href='{_src(f)}'>{_e(f['package_path'])}</a></td><td>{_e(human(f['bytes']))}</td></tr>" for f in caps) + "</table>")
    for role, title in (("thumbnail", "Thumbnails"), ("quote_card", "Quote cards"), ("carousel_slide", "Carousels"), ("short", "Shorts")):
        fs = [f for f in main if f["role"] == role]
        if fs:
            out.append(f"<h2>{title}</h2><div class='grid'>" + "".join(_card(f) for f in fs) + "</div>")
    if blocks:
        out.append("<h2>Post copy (drafts)</h2><div class='banner'>Written from templates and the quotes you approved. Edit before using. Fill every {{PLACEHOLDER}} and TODO.</div>")
        for i, b in enumerate(blocks):
            over = b.get("limit") and b["chars"] > b["limit"]
            lim = f"{b['chars']} / {b['limit']} {b['unit']}" if b.get("limit") else f"{b['chars']} {b['unit']}"
            out.append(f"<div class='copyblock'><div class='h'><strong>{_e(b['label'])}</strong><span class='{'over' if over else 'sub'}'>{_e(lim)}</span>"
                       f"<button onclick=\"copyText('b{i}',this)\">Copy</button></div><pre id='b{i}'>{_e(b['text'])}</pre>"
                       + "".join(f"<div class='prob'>check: {_e(p)}</div>" for p in b["problems"]) + "</div>")
    if drafts:
        out.append("<h2>Drafts: not approved, not part of the package</h2><div class='banner'>DRAFT. Approve these, fix the reason shown, then run <code>ave package</code> again.</div>"
                   "<div class='grid'>" + "".join(_card(f) for f in drafts) + "</div>")
    out.append("<h2>Every file</h2><table><tr><th>File</th><th>Size</th><th>Source</th><th>Approval</th><th>sha256</th></tr>")
    for f in files:
        src = f["source"]
        label = f"{src.get('stage', '')} v{src.get('version', '')}".strip() if src.get("stage") else "generated"
        out.append(f"<tr><td><a href='{_src(f)}'>{_e(f['package_path'])}</a>{' <span class=s-draft>DRAFT</span>' if f['draft'] else ''}"
                   f"{'' if f['copied'] else ' <span class=s-warn>(referenced)</span>'}</td><td>{_e(human(f['bytes']))}</td><td>{_e(label)}</td>"
                   f"<td>{_e(f['approval'].get('status', ''))}</td><td><code>{_e((f['sha256'] or '')[:12])}</code></td></tr>")
    out.append("</table>")
    if m["credits"]:
        out.append("<h2>Credits</h2><ul>" + "".join(f"<li>{_e(c)}</li>" for c in m["credits"]) + "</ul>")
    out.append(f"</main><footer>Made by <code>ave package</code>. Verify with <code>shasum -a 256 -c checksums.sha256</code>. "
               f"<script type='application/json' id='manifest'>{json.dumps(m, ensure_ascii=False).replace('</', '<\\/')}</script></footer>"
               f"<script>{JS}</script></body></html>")
    return "".join(out)
