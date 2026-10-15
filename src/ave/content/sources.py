"""Where the words come from, and the rule that keeps them honest.

Three source types land in a ContentDoc's `sources`, so templates never know where words came from:
  clip_quote     an approved clip in `work/clips.json` (its text is the words the quote is checked against)
  markdown       passages of the creator's own writing, split into sentence-addressable pieces by `ingest_writing`
  episode_meta   title / guest / show from `episode.json` (facts for titles and attribution, not quotable text)

The agent CHOOSES and justifies quotes; this module only checks them. A quote must appear word for word in its source
(case and punctuation ignored, exactly as `ave clips propose` checks `hook_quote`). No ellipses, no bracketed edits, no
paraphrase: shorten by choosing a shorter passage, never by cutting words out of the middle.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from .. import clips as C
from ..episode import Episode, read_json, write_json

_ELLIPSIS = re.compile(r"…|\.\.\.|\[[^\]]*\]")
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_ABBR = {"mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "e.g", "i.e", "no", "inc", "co", "jr", "sr"}


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---- the verbatim rule ----------------------------------------------------------------------------

def quote_problems(quote: str, text: str) -> list[str]:
    """Why `quote` is not a verbatim excerpt of `text` (empty list = it is)."""
    if _ELLIPSIS.search(quote):
        return ["a quote may not contain an ellipsis or [bracketed] edits; choose a shorter continuous passage instead"]
    q = C.norm_tokens(quote)
    if not q:
        return ["the quote has no words"]
    hay = C.norm_tokens(text)
    if C._quote_in(quote, hay):
        return []
    near = _nearest(q, hay)
    hint = f" Closest passage in the source: “{near}”." if near else ""
    return [f"“{quote[:70]}{'...' if len(quote) > 70 else ''}” does not appear verbatim in its source (checked word for word, ignoring case and punctuation); "
            f"a paraphrase is not a quote.{hint}"]


def _nearest(q: list[str], hay: list[str]) -> str:
    """The source window sharing the most words with the quote, to show the agent what it probably meant."""
    if len(hay) < 2:
        return ""
    n = len(q)
    qs = set(q)
    best, at = 0, -1
    for i in range(max(1, len(hay) - n + 1)):
        score = sum(1 for t in hay[i:i + n] if t in qs)
        if score > best:
            best, at = score, i
    if best < max(2, n // 2):
        return ""
    return " ".join(hay[at:at + n])[:160]


_FILLERS = {"uh", "um", "uhm", "er", "erm", "hmm", "mm"}


def disfluencies(text: str) -> list[str]:
    """Spoken-language noise inside a quote that a reader will see as a typo: a repeated word ("is is"), or a filler ("uh").
    Verbatim means these stay in; the fix is to choose a cleaner line, so the plan and propose point them out."""
    toks = C.norm_tokens(text)
    out = [f"repeated word \u201c{a}\u201d" for a, b in zip(toks, toks[1:]) if a == b and a.isalpha()]
    out += [f"filler \u201c{t}\u201d" for t in toks if t in _FILLERS]
    return out


def transcript_words(ep: Episode) -> list[dict]:
    p = ep.artifact("transcript.json")
    return read_json(p)["words"] if p.exists() else []


def locate(words: list[dict], quote: str) -> dict | None:
    """Find a quote in the raw transcript (episode time). Returns {t0, t1, speaker, occurrences} or None when the words are not
    contiguous in the raw transcript (cleanup can have removed fillers from the middle of a clip's text)."""
    q = C.norm_tokens(quote)
    if not q or not words:
        return None
    toks = [C.norm_token(w["w"]) for w in words]
    idx = [i for i, t in enumerate(toks) if t]           # words that survive normalisation
    flat = [toks[i] for i in idx]
    hits = [k for k in range(len(flat) - len(q) + 1) if flat[k:k + len(q)] == q]
    if not hits:
        return None
    a, b = idx[hits[0]], idx[hits[0] + len(q) - 1]
    spk = [w.get("speaker") for w in words[a:b + 1] if w.get("speaker")]
    return {"t0": round(words[a]["start"], 3), "t1": round(words[b]["end"], 3),
            "speaker": max(set(spk), key=spk.count) if spk else None, "occurrences": len(hits)}


def speaker_name(ep: Episode, sid: str | None) -> str | None:
    if not sid:
        return None
    try:
        return (read_json(ep.meta_path).get("speakers") or {}).get(sid, {}).get("name")
    except (OSError, ValueError):
        return None


# ---- clip sources ---------------------------------------------------------------------------------

def _clips(ep: Episode) -> list[dict]:
    d = C.load_clips(ep)
    return d["clips"] if d else []


def find_clip(ep: Episode, ref: str) -> dict:
    for c in _clips(ep):
        if ref in (c["id"], c["slug"]):
            return c
    raise ValueError(f"no clip {ref!r} in work/clips.json (have: {', '.join(c['id'] for c in _clips(ep)) or 'none; run ave clips propose'})")


def complete_clip(ep: Episode, c: dict, allow_incomplete: bool = False) -> tuple[dict, dict]:
    """The clip a quote may be taken from, and its completeness record: the clip itself when it is a complete statement, its
    `context_variant` when it is not and the variant is, else ValueError (the clip would hand the picker a mid-sentence passage).
    `allow_incomplete` is the explicit, recorded override (same rule as `ave shorts build`)."""
    from .. import completeness, shorts
    try:
        return completeness.gate(ep, c, shorts.load_config(ep), allow_incomplete)
    except SystemExit as e:
        raise ValueError(str(e)) from e


def quote_completeness(ep: Episode, quote: str, words: list[dict] | None = None) -> list[str]:
    """Why a quote does not start and end on a complete sentence/phrase (empty = it does). Judged on the text (a capital to begin,
    terminal punctuation to end, no continuation opener, no dangling last word) and, when the quote is found in the transcript,
    by `segment.Segmenter` on the real words around it."""
    from .. import segment
    t = quote.strip()
    cfg = segment.config({})
    toks = C.norm_tokens(t)
    out = []
    if not toks:
        return ["the quote has no words"]
    first = t.lstrip("\"'“‘([ ")
    if first[:1].isalpha() and first[:1].islower() and toks[0] != "i" and not toks[0].startswith("i'"):
        out.append(f"starts in lowercase ({toks[0]!r}): the middle of a sentence")
    if toks[0] in cfg["continuation_openers"] and not out:
        out.append(f"opens with {toks[0]!r}, which continues the previous thought")
    ended = t.rstrip("\"'”’)] ")
    if ended.endswith((",", "-", "–", "—")):
        out.append("ends on a comma or a dash: the clause is unfinished")
    elif not ended.endswith((".", "?", "!", "…")) and toks[-1] in cfg["dangling_endings"] and toks[-1] not in ("i", "you", "me", "it", "them", "that", "now", "too"):
        out.append(f"ends on {toks[-1]!r}, a word that needs something after it")
    words = words if words is not None else transcript_words(ep)
    loc = locate(words, t) if words else None
    if loc and not out:
        try:
            from .. import clips as clipsmod
            rubric = clipsmod.load_rubric(ep)
            seg = segment.Segmenter([{"w": w["w"], "start": w["start"], "end": w["end"], "speaker": w.get("speaker")} for w in words],
                                    {f.lower() for f in rubric["snap"]["filler_words"]}, rubric)
            a = next(k for k, w in enumerate(words) if abs(w["start"] - loc["t0"]) < 1e-6)
            b = next(k for k, w in enumerate(words) if abs(w["end"] - loc["t1"]) < 1e-6 and k >= a)
            v = seg.verdict(a, b)
            if not v["start_complete"]:      # (the end: a pull-quote may stop at a clause boundary; only a dangling end is refused, above)
                out.append("on the real words: " + v["completeness_note"].split("; ")[0])
        except (SystemExit, StopIteration):
            pass
    return out


def clip_source(ep: Episode, ref: str, allow_unapproved: bool = False, allow_incomplete: bool = False) -> dict:
    """A `clip_quote` source from an APPROVED clip (a proposed clip is the agent's guess, not the human's choice). The clip must be a
    complete statement: an incomplete one is replaced by its `context_variant` when that is complete, else refused."""
    c = find_clip(ep, ref)
    if c["status"] != "approved" and not allow_unapproved:
        raise ValueError(f"clip {c['id']} is {c['status']}, not approved. Quote only approved clips (ask the human to approve it "
                         f"in `ave clips review`), or pass allow_unapproved if the human said to")
    c, comp = complete_clip(ep, c, allow_incomplete)
    out = {"type": "clip_quote", "clip": c["id"], "slug": c["slug"], "title": c["title"], "status": c["status"],
           "speakers": c.get("speakers", []), "text": c["text"], "text_sha256": sha256_text(c["text"]),
           "completeness": {"complete": comp["complete"], "boundary": comp["boundary"], "override": comp["override"]}}
    if c.get("session"):        # clips of every session share work/clips.json; the quote records which edit it was cut from (N16)
        out.update(session=c["session"], source=c["source"])
    return out


def meta_source(ep: Episode) -> dict:
    m = read_json(ep.meta_path)
    names = [s.get("name") for s in (m.get("speakers") or {}).values() if s.get("name")]
    return {"type": "episode_meta", "title": m.get("title") or ep.root.name,
            "show": m.get("show"), "number": m.get("number"), "people": names}


# ---- Markdown writing -----------------------------------------------------------------------------

def split_sentences(text: str) -> list[str]:
    """Plain-prose sentence split. Conservative: abbreviations and initials do not end a sentence."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    out, start = [], 0
    for m in re.finditer(r"([.!?]+[\"”’')\]]*)\s+(?=[\"“‘'(\[]?[A-Z0-9])", text):
        before = text[start:m.start()].split()
        last = before[-1].lower().rstrip(".") if before else ""
        if m.group(1).startswith(".") and (last in _ABBR or re.fullmatch(r"[a-z]", last)):
            continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    if start < len(text):
        out.append(text[start:].strip())
    return out


def _plain(md: str) -> str:
    """Markdown inline markup -> the words a reader sees (so a quote matches what was written, not its asterisks)."""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = re.sub(r"(?<![\w*])[*_]([^*_]+)[*_](?![\w*])", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"^\s{0,3}(?:>\s?)+", "", s, flags=re.M)
    s = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", s, flags=re.M)
    return re.sub(r"\s+", " ", s).strip()


def parse_markdown(text: str, name: str) -> dict:
    """{title, date, url, passages: [{id, heading, text, sentences: [{id, text}]}]} for one Markdown file."""
    meta: dict = {}
    m = _FRONT.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        text = text[m.end():]
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"^(```|~~~).*?^\1\s*$", "", text, flags=re.S | re.M)       # fenced code is not prose
    heading, blocks, cur = "", [], []
    title = meta.get("title") if isinstance(meta.get("title"), str) else None

    def flush() -> None:
        body = _plain(" ".join(cur))
        if body:
            blocks.append((heading, body))
        cur.clear()

    for line in text.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if h:
            flush()
            heading = _plain(h.group(2))
            if h.group(1) == "#" and not title:
                title = heading
            continue
        if not line.strip():
            flush()
            continue
        bullet = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", line)
        if bullet:                       # each list item is its own passage: it is a separate thought, not the previous sentence's tail
            flush()
            cur.append(re.sub(r"^\s{0,3}(?:>\s?)+", "", bullet.group(1)).strip())
            flush()
        else:
            cur.append(re.sub(r"^\s{0,3}(?:>\s?)+", "", line).strip())
    flush()
    passages = []
    for n, (hd, body) in enumerate(blocks, 1):
        sents = split_sentences(body)
        pid = f"p{n:03d}"
        passages.append({"id": pid, "heading": hd, "text": body,
                         "sentences": [{"id": f"{pid}.s{k}", "text": s} for k, s in enumerate(sents, 1)]})
    return {"title": title or Path(name).stem.replace("-", " ").replace("_", " "),
            "date": str(meta.get("date")) if meta.get("date") else None, "url": meta.get("url"), "passages": passages}


def ingest_writing(ep: Episode, folder: str | Path) -> dict:
    """Split every .md under `folder` into sentence-addressable passages -> `<work>/content/writing.json`. The files are only read."""
    d = Path(folder).expanduser().resolve()
    if not d.is_dir():
        raise SystemExit(f"{d} is not a folder of Markdown files")
    files = []
    for f in sorted(d.rglob("*.md")):
        if any(part.startswith(".") for part in f.relative_to(d).parts):
            continue
        raw = f.read_text(encoding="utf-8")
        parsed = parse_markdown(raw, f.name)
        if not parsed["passages"]:
            continue
        files.append({"file": f.relative_to(d).as_posix(), "sha256": sha256_text(raw), **parsed})
    if not files:
        raise SystemExit(f"no Markdown passages found under {d}")
    out = {"version": 1, "dir": str(d), "files": files,
           "totals": {"files": len(files), "passages": sum(len(x["passages"]) for x in files),
                      "sentences": sum(len(p["sentences"]) for x in files for p in x["passages"])}}
    from . import manifest
    write_json(manifest.content_dir(ep) / "writing.json", out)
    return out


def load_writing(ep: Episode) -> dict | None:
    from . import manifest
    p = manifest.content_dir(ep) / "writing.json"
    return read_json(p) if p.exists() else None


def writing_source(ep: Episode, file: str, passages: list[str] | None = None, sentences: list[str] | None = None) -> dict:
    """A `markdown` source from ingested writing: named passages (whole) or sentence ids, in document order."""
    w = load_writing(ep)
    if not w:
        raise ValueError("no writing ingested yet. Run: ave content ingest-writing <project> <folder>")
    f = next((x for x in w["files"] if x["file"] == file), None)
    if not f:
        raise ValueError(f"{file!r} is not in the ingested writing (have: {', '.join(x['file'] for x in w['files'])})")
    if bool(passages) == bool(sentences):
        raise ValueError("give exactly one of passages or sentences")
    pool = {p["id"]: p["text"] for p in f["passages"]} if passages else {s["id"]: s["text"] for p in f["passages"] for s in p["sentences"]}
    want = passages or sentences
    unknown = [x for x in want if x not in pool]
    if unknown:
        raise ValueError(f"unknown {'passage' if passages else 'sentence'} ids {unknown} in {file}")
    order = list(pool)
    chosen = sorted(set(want), key=order.index)
    text = " ".join(pool[x] for x in chosen)
    return {"type": "markdown", "file": file, "file_sha256": f["sha256"], "title": f["title"], "url": f.get("url"), "date": f.get("date"),
            "passages": chosen if passages else None, "sentences": chosen if sentences else None,
            "text": text, "text_sha256": sha256_text(text)}


# ---- drift check ----------------------------------------------------------------------------------

def rebind_clip_sources(ep: Episode, doc: dict) -> list[dict]:
    """Repoint a `clip_quote` source whose clip id no longer exists (clips were re-proposed and renumbered) at the one clip that holds
    BYTE-IDENTICAL text (same sha256). The words every quote was checked against are unchanged, so the checks still hold. A source
    whose text exists nowhere, or in several clips, is left alone and `verify_sources` reports it. Mutates `doc`; returns the changes."""
    changes: list[dict] = []
    clips = _clips(ep)
    for sid, s in doc["sources"].items():
        if s["type"] != "clip_quote":
            continue
        try:
            live = find_clip(ep, s["clip"])
        except ValueError:
            live = None
        if live is not None and sha256_text(live["text"]) == s["text_sha256"]:
            continue
        same = [c for c in clips if sha256_text(c["text"]) == s["text_sha256"]]
        if len(same) != 1:
            continue
        c = same[0]
        changes.append({"source": sid, "from": s["clip"], "to": c["id"]})
        s.update(clip=c["id"], slug=c["slug"], title=c["title"], status=c["status"], rebound_from=s["clip"])
        if c.get("session"):
            s.update(session=c["session"], source=c["source"])
    return changes


def verify_sources(ep: Episode, doc: dict) -> list[str]:
    """Re-derive every source's text from disk and compare. A source whose clip or file changed since it was recorded is stale:
    its quotes were verified against words that are no longer there."""
    p: list[str] = []
    for sid, s in doc["sources"].items():
        if s["type"] == "clip_quote":
            try:
                live = find_clip(ep, s["clip"])
            except ValueError as e:
                p.append(f"source {sid}: {e}")
                continue
            if sha256_text(live["text"]) != s["text_sha256"]:
                p.append(f"source {sid}: clip {s['clip']} changed since this plan was made (re-propose the items that quote it)")
        elif s["type"] == "markdown":
            w = load_writing(ep)
            f = next((x for x in (w or {}).get("files", []) if x["file"] == s["file"]), None)
            if not f:
                p.append(f"source {sid}: {s['file']} is not in the ingested writing (run ave content ingest-writing again)")
                continue
            path = Path(w["dir"]) / s["file"]
            if path.is_file() and sha256_text(path.read_text(encoding="utf-8")) != s["file_sha256"]:
                p.append(f"source {sid}: {s['file']} changed on disk since it was quoted (re-ingest and re-propose)")
    return p


# ---- plan: candidates for the agent ---------------------------------------------------------------

def plan(ep: Episode, max_quote_chars: int = 240) -> dict:
    """Mechanical raw material for the agent to choose from. Nothing is chosen here.

    Lists approved clips with their quotable text (the hook/end quotes the clip finder already verified, plus the clip's
    sentences as candidate pull-quotes within the length limit), the episode facts, and any ingested writing. Writes
    `<work>/content/candidates.json`; the agent then writes picks and runs `ave content propose`.
    """
    from . import manifest
    m = read_json(ep.meta_path)
    words = transcript_words(ep)
    clips_out = []
    skipped = []
    for c in _clips(ep):
        if c["status"] != "approved":
            continue
        try:
            c, _comp = complete_clip(ep, c)
        except ValueError as e:
            skipped.append({"clip": c["id"], "problems": [ln.strip("- ").strip() for ln in str(e).splitlines() if ln.startswith("  - ")] or [str(e)[:200]]})
            continue
        sents = split_sentences(c["text"])
        cands = []
        for q, why in [(c.get("hook_quote"), "hook_quote (verified by the clip finder)"), (c.get("end_quote"), "end_quote (verified by the clip finder)")]:
            if q and len(q) <= max_quote_chars:
                cands.append({"quote": q, "from": why})
        for s in sents:
            if 40 <= len(s) <= max_quote_chars and not any(s == x["quote"] for x in cands):
                cands.append({"quote": s, "from": "sentence of the clip"})
        kept = []
        for x in cands:
            bad = quote_completeness(ep, x["quote"], words)
            if bad:
                skipped.append({"clip": c["id"], "quote": x["quote"][:80], "problems": bad})
            else:
                kept.append(x)
        cands = kept
        for x in cands:
            x["disfluencies"] = disfluencies(x["quote"])
            loc = locate(words, x["quote"])
            x["speaker"] = loc["speaker"] if loc else None
            x["speaker_name"] = speaker_name(ep, x["speaker"])
            x["t0"], x["t1"] = (loc["t0"], loc["t1"]) if loc else (None, None)
        clips_out.append({"clip": c["id"], "slug": c["slug"], "title": c["title"], "hook": c["hook"], "speakers": c["speakers"],
                          "score": c["total"], "duration": c["duration"], "quote_candidates": cands,
                          **({"session": c["session"]} if c.get("session") else {}),
                          **({"stale": c["stale_reason"]} if c.get("stale") else {})})
    w = load_writing(ep)
    out = {"version": 1, "episode": ep.root.name, "episode_meta": meta_source(ep),
           "approved_clips": clips_out, "skipped_incomplete": skipped,
           "writing": [{"file": f["file"], "title": f["title"], "passages": len(f["passages"])} for f in (w or {}).get("files", [])],
           "limits": {"max_quote_chars": max_quote_chars},
           "next": "Choose quotes, write picks.json (see skills/ave-content-plan), then: ave content propose <project> picks.json"}
    if not clips_out and not out["writing"]:
        out["note"] = ("Nothing to quote yet: no approved clips (work/clips.json) and no ingested writing. "
                       "Approve clips in `ave clips review`, or run `ave content ingest-writing <project> <folder>`. "
                       "A thumbnail or reel card needs only the episode facts above.")
    write_json(manifest.content_dir(ep) / "candidates.json", out)
    return out


def resolve_pick_source(ep: Episode, spec: Any, allow_unapproved: bool = False, allow_incomplete: bool = False) -> dict:
    """A pick's `source` -> a source dict. {"clip": "c01"} | {"writing": "soil.md", "passages": [...]|"sentences": [...]} | "episode"."""
    if spec in ("episode", "meta") or spec == {"episode": True}:
        return meta_source(ep)
    if isinstance(spec, dict) and "clip" in spec:
        return clip_source(ep, spec["clip"], allow_unapproved, allow_incomplete)
    if isinstance(spec, dict) and "writing" in spec:
        return writing_source(ep, spec["writing"], spec.get("passages"), spec.get("sentences"))
    raise ValueError("source must be {\"clip\": id}, {\"writing\": file, \"passages\"|\"sentences\": [...]}, or \"episode\"")
