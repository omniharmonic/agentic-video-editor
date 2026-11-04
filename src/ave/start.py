"""`ave start <folder>`: take new footage, or a half-finished episode, to a rough cut in one go (Director's Room, "Starting").

It chains the existing stages (ingest, audio, transcribe, sync, map cameras, plan cuts, trim, assemble, cleanup, a draft render)
and is resumable from disk: each stage first looks at what is already there and either reuses it or redoes it, and says which.
Once one stage is redone, every stage after it is redone too, so nothing downstream is left built on old work.

It stops for one thing only: who the speakers are (names missing from episode.json, or diarisation it cannot trust). That stop is
`NeedsDirector`, recorded on the board as an open decision with a plain question; running it again with the answer continues.

Everything else it decides itself and records as a flagged decision (`decision.recorded`, actor claude) with the options and the
one it chose: the sync reference, which camera frames which speaker, the draft trim (`ave trim propose`), the assembly canvas and
session handling, and the pacing pass of the cleanup. The director sees each at review and can approve or change it in a word.
Progress goes to the board as one task per stage. Nothing it decides leaves the system: it renders a draft for review only, never
publishes, queues or hands off, and `ave final` refuses its trim until the director confirms it with `ave trim set`.

Original footage is only read. Everything written goes in the episode's work/edits/exports folders, episode.json and .ave/.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from . import config
from .episode import Episode, read_json, write_json

STEPS = ("ingest", "audio", "transcribe", "speakers", "sync", "map", "plan", "trim", "assemble", "cleanup", "rough")
LABELS = {"ingest": "Look at the footage", "audio": "Analysis audio", "transcribe": "Transcript", "speakers": "Who is speaking",
          "sync": "Sync the cameras", "map": "Cameras to speakers", "plan": "Plan the cuts", "trim": "Real start and end (draft)",
          "assemble": "Assembly", "cleanup": "Rough cut", "rough": "Draft render of the rough cut", "clips": "Clip candidates"}
DEFAULT_POLICY = config.REPO_ROOT / "config" / "start.default.yaml"
HOME_ARTIFACT = "art_ep"
ACTOR = "claude"


class StartError(RuntimeError):
    """A stage failed. `step` names it; the message is the stage's own explanation."""

    def __init__(self, step: str, message: str, report: "Report | None" = None):
        super().__init__(message)
        self.step, self.report = step, report


class NeedsDirector(Exception):
    """Only the director can answer this (speaker identity). `question` is the plain question; the answer resumes the run."""

    def __init__(self, question: str, decision: str | None = None, report: "Report | None" = None):
        super().__init__(question)
        self.question, self.decision, self.report = question, decision, report


@dataclass
class Report:
    root: str
    through: str
    steps: list[dict] = field(default_factory=list)
    decisions_new: list[dict] = field(default_factory=list)
    decisions_open: list[dict] = field(default_factory=list)
    stopped: str | None = None
    question: str | None = None
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def status(self, step: str) -> str | None:
        return next((s["status"] for s in self.steps if s["step"] == step), None)

    def text(self) -> str:
        L = [f"Episode: {self.root}"]
        for s in self.steps:
            L.append(f"  {s['step']:<10} {s['status']:<8} {s['note']}" + (f"  ({s['seconds']:.1f}s)" if s["status"] == "done" else ""))
        if self.question:
            L += ["", "I need you for one thing before I go on:", f"  {self.question}"]
        if self.decisions_open:
            L += ["", f"Waiting for your answer on the board ({len(self.decisions_open)}): what I decided alone, and anything I asked"]
            new = {d["decision"] for d in self.decisions_new}
            for n, d in enumerate(self.decisions_open, 1):
                others = [o for o in d["options"] if o != d["chosen"]]
                L.append(f"  {n}. {d['summary']}" + ("  [new]" if d["decision"] in new else ""))
                L.append(f"     chose: {d['chosen']}")
                if others:
                    L.append(f"     other options: {'; '.join(others)}")
                if d.get("reason"):
                    L.append(f"     why: {d['reason']}")
        L += [f"Note: {n}" for n in self.notes]
        if not self.stopped:
            L += ["", "Ready for you: the rough cut. Review it in the Room or Resolve (`ave handoff " + self.root + " --stage rough`), and answer "
                  "each decision in a word (\"ok\", or what you want instead). Nothing has been published or sent anywhere."
                  if self.through == "rough" else f"Stopped after {self.through}, as asked."]
        L.append(f"Took {self.seconds:.1f}s.")
        return "\n".join(L)


# ---------------------------------------------------------------------------------------------------- policy
def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_policy(ep: Episode, home: Path | None = None) -> dict:
    pol = yaml.safe_load(DEFAULT_POLICY.read_text())
    for cand in (ep.root / "policy" / "start.yaml", Path(home or Path.home()) / ".config" / "ave" / "start.yaml"):
        if cand.is_file():
            return _merge(pol, yaml.safe_load(cand.read_text()) or {})
    return pol


# ---------------------------------------------------------------------------------------------------- the board
def _rid(*parts) -> str:
    return "start-" + hashlib.sha256("\0".join(str(p) for p in parts).encode()).hexdigest()[:20]


def _key_rid(key: str, *parts) -> str:
    """A decision's request id names its key (`start-<key>-<hash>`), so later runs and commands find the decisions of one key."""
    return f"start-{key}-" + hashlib.sha256("\0".join(str(p) for p in parts).encode()).hexdigest()[:16]


def decisions_for(root: Path, key: str, *, open_only: bool = True) -> list[str]:
    """Ids of the decisions `ave start` (or `ave trim propose`) recorded for `key`, oldest first; only the open ones by default."""
    from .project import Project, board
    project = Project.open(root)
    st = project.state()
    prefix = f"start-{key}-"
    out = []
    for e in project.read_events():
        d = e.get("data") or {}
        if e.get("op") == "decision.recorded" and e.get("actor") == ACTOR and str(d.get("request_id", "")).startswith(prefix):
            did = d.get("decision")
            dec = st.decisions.get(did)
            if did not in out and dec is not None and (not open_only or board.decision_open(dec)):
                out.append(did)
    return out


def close_decisions(root: Path, key: str, *, choice: str, actor: str, quote: str | None = None, reason: str | None = None,
                    superseded_by: str | None = None, keep: str | None = None) -> list[str]:
    """Resolve every open decision of `key` (except `keep`). A director's act passes their literal words as `quote` and actor
    human; a redo that chose differently passes `superseded_by` (actor claude, no quote: it is not the director's answer)."""
    from .project import Project
    closed = []
    for did in decisions_for(root, key):
        if did == keep:
            continue
        data = {"decision": did, "choice": _cut(choice, 200)}
        if quote:
            data["quote"] = _cut(quote, 500)
        if reason:
            data["reason"] = _cut(reason, 500)
        if superseded_by:
            data["superseded_by"] = superseded_by
        Project.open(root).log.append("decision.resolved", artifact=HOME_ARTIFACT, data=data, actor=actor)
        closed.append(did)
    return closed


def open_claude_decisions(root: Path) -> list[dict]:
    """The decisions Claude made alone that are still open on the episode, for the warnings of `ave final`, `ave package` and
    `ave publish approve`. Empty when the folder is not a project. Never raises."""
    try:
        from .project import Project, board
        root = Path(root)
        if not (root / ".ave" / "project.json").is_file():
            return []
        st = Project.open(root).state()
        return [d for d in board.open_decisions(st, HOME_ARTIFACT)
                if ((st.decisions.get(d["decision"]) or {}).get("recorded") or {}).get("actor") == ACTOR]
    except Exception:          # a warning must never break the command it rides on
        return []


def open_decisions_warning(root: Path) -> str | None:
    rows = open_claude_decisions(root)
    if not rows:
        return None
    return (f"{len(rows)} decision(s) Claude made alone are still waiting for the director: "
            + "; ".join(f'{d["summary"]} (chose "{d["chosen"]}")' for d in rows)
            + ". This does not stop anything; answer them on the board.")


def _cut(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


class Board:
    """The project board, written through the same board tools Claude uses over MCP (so request ids replay and the actor is claude)."""

    def __init__(self, root: Path):
        from .agent import AgentApp
        self.root = root
        self.app = AgentApp.for_board(root, by=ACTOR)
        self.run = hashlib.sha256(f"{time.time_ns()}".encode()).hexdigest()[:10]
        self.new: list[dict] = []

    def _state(self):
        from .project import Project
        return Project.open(self.root).state()

    def decide(self, key: str, summary: str, options: list[str], chosen: str, reason: str = "") -> dict:
        """Record a choice made alone. The same choice recorded again (a resumed run) replays: nothing is written twice."""
        opts = []
        for o in options:
            o = _cut(o, 200)
            if o not in opts:
                opts.append(o)
        chosen = _cut(chosen, 200)
        if chosen not in opts:
            opts.insert(0, chosen)
        opts = [chosen] + [o for o in opts if o != chosen][:7]
        if len(opts) < 2:
            opts.append("leave it to the director")
        summary, reason = _cut(summary, 300), _cut(reason, 500)
        args = {"request_id": _key_rid(key, summary, chosen, reason, *opts), "artifact": HOME_ARTIFACT, "summary": summary,
                "options": opts, "chosen": chosen, "reason": reason}
        r = self.app.board("decision_record", args)
        d = {"decision": r["decision"], "summary": summary, "options": opts, "chosen": chosen, "reason": reason, "key": key}
        # a redo that chose differently replaces the older open decision of the same key: never two contradicting ones open
        close_decisions(self.root, key, choice="superseded", actor=ACTOR, superseded_by=r["decision"], keep=r["decision"],
                        reason=f"Claude redid this and chose differently: {summary}")
        if not r["replayed"]:
            self.new.append(d)
        return d

    def open_decisions(self) -> list[dict]:
        from .project import board
        return board.open_decisions(self._state(), HOME_ARTIFACT)

    def task(self, step: str, status: str, note: str | None = None) -> None:
        """One task per stage; created once (replayed on resume), then moved todo -> doing -> done or blocked. A task already
        done is not set back to doing, and nothing is written when nothing changed, so a resumed run that reuses everything
        leaves the board as it was."""
        made = self.app.board("task_update", {"request_id": _rid("task", step), "artifact": HOME_ARTIFACT,
                                              "title": f"Start: {LABELS[step]}", "status": "todo"})
        tid = made["task"]
        cur = (self._state().tasks.get(tid) or {}).get("fields") or {}
        if status == "doing" and cur.get("status") == "done":
            return
        if cur.get("status") == status and (note is None or cur.get("note") == _cut(note, 500)):
            return
        args = {"request_id": _rid("task", step, self.run, status, note or ""), "task": tid, "status": status}
        if note:
            args["note"] = _cut(note, 500)
        self.app.board("task_update", args)


# ---------------------------------------------------------------------------------------------------- the run
class _Run:
    def __init__(self, ep: Episode, through: str, clips: bool, speakers: dict | None, home: Path | None, answer: str | None = None):
        self.ep, self.through, self.clips, self.speakers_given, self.home = ep, through, clips, speakers or {}, home
        self.answer = answer            # the literal --speaker arguments, the director's words when typed at a real terminal
        self.pol = load_policy(ep, home)
        self.report = Report(root=str(ep.root), through=through)
        self.board = Board(ep.root)
        self.redone = False             # once a stage is redone, every stage after it is redone

    # -- helpers
    def meta(self) -> dict:
        return read_json(self.ep.meta_path)

    def media(self) -> list[dict]:
        return read_json(self.ep.artifact("media.json"))["files"]

    def transcript(self) -> dict:
        return read_json(self.ep.artifact("transcript.json"))

    def sessions(self) -> list[str]:
        from . import sessions
        ids = sessions.session_ids(self.ep)
        return ids if len(ids) > 1 else []

    def step(self, name: str, fn) -> None:
        t = time.monotonic()
        self.board.task(name, "doing")
        try:
            status, note = fn()
        except NeedsDirector as e:
            self.board.task(name, "blocked", "waiting for the director: " + e.question)
            self.report.steps.append({"step": name, "status": "stopped", "note": "waiting for you (below)", "seconds": time.monotonic() - t})
            raise
        except SystemExit as e:
            msg = str(e.code) if not isinstance(e.code, int) else f"exited {e.code}"
            self.board.task(name, "blocked", msg)
            self.report.steps.append({"step": name, "status": "failed", "note": _cut(msg, 300), "seconds": time.monotonic() - t})
            raise StartError(name, msg, self.report) from e
        if status == "done":
            self.redone = True
        self.board.task(name, "done", None if status == "reused" else note)
        self.report.steps.append({"step": name, "status": status, "note": note, "seconds": round(time.monotonic() - t, 2)})

    def fresh(self, exists: bool) -> bool:
        """Reuse what is on disk unless an earlier stage was redone in this run."""
        return exists and not self.redone

    # -- stages
    def s_ingest(self):
        from . import media
        files = sorted(self.ep.rel(p) for p in self.ep.media_files())
        if not files:
            raise SystemExit(f"No footage found in {', '.join(str(d) for d in self.ep.raw_dirs)}. Put the recordings there first.")
        p = self.ep.artifact("media.json")
        if self.fresh(p.exists() and sorted(m["file"] for m in read_json(p)["files"]) == files):
            return "reused", f"{len(files)} file(s) already probed (work/media.json)"
        r = media.ingest(self.ep)
        return "done", f"probed {len(r['files'])} file(s): " + ", ".join(f"{Path(f['file']).name} ({f.get('duration') or 0:.0f}s)" for f in r["files"])

    def s_audio(self):
        from . import audio
        need = [m["file"] for m in self.media() if m["audio"]]
        if self.fresh(all(audio.analysis_wav_path(self.ep, f).exists() for f in need)):
            return "reused", f"analysis audio for {len(need)} file(s) already in work/audio/"
        audio.extract_all(self.ep)
        return "done", f"analysis audio for {len(need)} file(s)"

    def s_transcribe(self):
        from . import transcribe
        p = self.ep.artifact("transcript.json")
        if self.fresh(p.exists()):
            tr = read_json(p)
            return "reused", f"transcript of {tr['source']}: {len(tr['words'])} words, {len(tr.get('speakers') or {})} speaker(s)"
        engine = "fluidaudio" if config.fluidaudio_cli() or not (config.whisper_cli() and config.whisper_model()) else "whisper"
        named = len(self.meta().get("speakers") or {}) or None
        tr = transcribe.transcribe(self.ep, asr=engine, num_speakers=named)
        return "done", f"{len(tr['words'])} words from {tr['source']} ({engine}), {len(tr['speakers'])} speaker(s)"

    def s_speakers(self):
        """The one stop: every speaker must have a name the director gave, and the diarisation must be trustworthy.

        Names given with --speaker are written to episode.json. They answer an earlier stop only when a stop was recorded: then
        the answer is kept in episode.json (`speaker_answer`) and clears a diarisation doubt on later runs too. The open question
        is closed with the director's words only when a person runs the command at a real terminal (the literal arguments are
        the quote); otherwise it stays open, with "Names supplied on the command line" as Claude's report."""
        from . import humanpresence
        tr = self.transcript()
        ids = sorted({w.get("speaker") for w in tr["words"] if w.get("speaker")} | set(tr.get("speakers") or {}))
        meta = self.meta()
        given = {k: v.strip() for k, v in self.speakers_given.items() if isinstance(v, str) and v.strip()}
        unknown = sorted(set(given) - set(ids))
        if unknown:
            raise SystemExit(f"No speaker {', '.join(unknown)} in the transcript; its speakers are {', '.join(ids)}.")
        stops = decisions_for(self.ep.root, "speakers", open_only=False)
        open_q = decisions_for(self.ep.root, "speakers")
        if given:
            sp = meta.setdefault("speakers", {})
            for k, v in given.items():
                sp.setdefault(k, {})["name"] = v
            if stops:
                meta["speaker_answer"] = {"decision": stops[-1], "names": dict(sorted(given.items())),
                                          "via": "terminal" if humanpresence.is_human_terminal() else "command line",
                                          "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            write_json(self.ep.meta_path, meta)
        sp = meta.get("speakers") or {}

        def named(s):
            n = (sp.get(s) or {}).get("name")
            return isinstance(n, str) and n.strip() and n.strip().upper() != s.upper()
        unnamed = [s for s in ids if not named(s)]
        talk = {s: float(((tr.get("speakers") or {}).get(s) or {}).get("talk_seconds") or 0) for s in ids}
        total = sum(talk.values()) or 1.0
        doubts = []
        tiny = [s for s in ids if talk[s] / total < self.pol["speakers"]["min_share"]]
        if tiny:
            doubts.append(f"{', '.join(tiny)} speak{'s' if len(tiny) == 1 else ''} for only {', '.join(f'{talk[s]:.0f}s' for s in tiny)}, "
                          "which looks like the diarisation splitting one voice")
        agree = (tr.get("diarization_check") or {}).get("agreement")
        if agree is not None and agree < self.pol["speakers"]["min_agreement"]:
            doubts.append(f"the diarisation agrees with the microphone channels only {agree:.0%} of the time")
        ans = meta.get("speaker_answer") or {}
        if ans.get("decision") in stops and all((ans.get("names") or {}).get(s) == (sp.get(s) or {}).get("name") for s in ans.get("names") or {}):
            doubts = []        # the director named them in answer to the stop, having heard the evidence
        if not unnamed and not doubts:
            names = ", ".join(f"{s} {sp[s]['name']}" for s in ids)
            if given and open_q:
                if humanpresence.is_human_terminal() and self.answer:
                    close_decisions(self.ep.root, "speakers", choice="named by the director", actor="human", quote=self.answer)
                else:          # Claude's report on the task; the question stays open for the director
                    note = f"Names supplied on the command line: {names}. The question stays open for the director to confirm."
                    self.report.notes.append(note)
                    return "checked", note
            # naming speakers changes no media, so it never makes later stages redo their work ("checked", not "done")
            return ("checked" if given else "reused"), "speakers: " + names
        first = {}
        for u in tr.get("utterances") or []:
            if u["speaker"] in ids and u["speaker"] not in first and len(u.get("text", "").split()) >= 3:
                first[u["speaker"]] = u
        lines = []
        for s in ids:
            u = first.get(s)
            said = f', first says "{_cut(u["text"], 70)}" at {u["start"]:.1f}s' if u else ""
            lines.append(f"{s} talks for {talk[s]:.0f}s{said}")
        why = "; ".join(doubts) if doubts else f"{', '.join(unnamed)} {'has' if len(unnamed) == 1 else 'have'} no name in episode.json"
        flags = " ".join(f"--speaker {s}=NAME" for s in ids)
        question = (f"Who is speaking? {'; '.join(lines)}. I stopped because {why}, and a wrong guess would spoil the cut. "
                    f"Tell me their names (or run: ave start {self.ep.root} {flags}).")
        d = self.board.decide("speakers", f"Who is speaking: {', '.join(ids)} need names from the director",
                              ["stop and ask the director", "guess the names from the transcript"], "stop and ask the director",
                              why + ". " + "; ".join(lines))
        self.report.question = question
        raise NeedsDirector(question, d["decision"])

    def s_sync(self):
        from . import sync
        tr = self.transcript()
        ref = tr["source"]
        syncable = [m for m in self.media() if m["audio"]]
        p = self.ep.artifact("sync.json")
        if len(syncable) < 2:
            return "skipped", "only one file with sound: nothing to sync"
        if self.fresh(p.exists() and read_json(p).get("reference") == ref and {m["file"] for m in syncable} <= set(read_json(p)["files"])):
            return "reused", f"cameras already synced to {Path(ref).name} (work/sync.json)"
        r = sync.sync_episode(self.ep, reference=ref)
        offs = ", ".join(f"{Path(f).name} {v['start_offset']:+.3f}s" for f, v in r["files"].items() if f != ref)
        kind = "the recorder" if next(m for m in syncable if m["file"] == ref)["kind"] == "audio" else "a camera"
        self.board.decide("sync", f"Sync reference: {Path(ref).name} sets episode time",
                          [Path(m["file"]).name for m in syncable], Path(ref).name,
                          f"The transcript was made from it ({kind}), so transcript times are episode times. Offsets: {offs}.")
        self.report.notes += [f"sync: {w}" for w in r.get("warnings") or []]
        return "done", f"synced to {Path(ref).name}: {offs}" + (f"; {len(r['warnings'])} warning(s)" if r.get("warnings") else "")

    def _levels(self, cam: str, utts: dict[str, list], syncs: dict) -> dict[str, float]:
        """Each speaker's median sound level (dB) on this camera's own audio while they talk: the nearer camera hears them louder."""
        import numpy as np
        import soundfile as sf
        from . import audio, editdoc
        wav = audio.analysis_wav_path(self.ep, cam)
        if not wav.exists():
            return {}
        data, sr = sf.read(str(wav), dtype="float32", always_2d=False)
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        src = syncs.get(cam) or {"start_offset": 0.0}
        out = {}
        for spk, spans in utts.items():
            vals = []
            for a, b in spans:
                i0, i1 = (int(max(0.0, float(editdoc.src_time(src, t))) * sr) for t in (a, b))
                seg = data[i0:i1]
                if len(seg) >= sr * 0.3:
                    vals.append(float(np.sqrt(np.mean(np.square(seg)))) + 1e-9)
            if vals:
                out[spk] = 20 * math.log10(float(np.median(vals)))
        return out

    def s_map(self):
        """Decide which camera frames which speaker, per session, from each camera's own sound. Camera entries already in
        episode.json (the director's, or an earlier run's) are always kept: only cameras not yet mapped are added, and that is
        flagged too."""
        meta = self.meta()
        media = self.media()
        videos = sorted(m["file"] for m in media if m["kind"] == "video")
        existing = dict(meta.get("cameras") or {})
        mapped = {v.get("file") for v in existing.values()}
        if existing and set(videos) <= mapped and any((s or {}).get("camera") for s in (meta.get("speakers") or {}).values()):
            cams = ", ".join(f"{k} {Path(v['file']).name}" for k, v in existing.items())
            return "reused", f"cameras already mapped in episode.json ({cams})"
        from . import sessions as sesmod
        if not videos:
            raise SystemExit("No camera files: `ave start` makes video edits. For audio only, see the ave-pipeline skill.")
        tr = self.transcript()
        sp = meta.setdefault("speakers", {})
        order = []
        for u in sorted(tr.get("utterances") or [], key=lambda u: u["start"]):
            if u["speaker"] not in order:
                order.append(u["speaker"])
        order += sorted(s for s in (tr.get("speakers") or {}) if s not in order)
        syncs = read_json(self.ep.artifact("sync.json"))["files"] if self.ep.artifact("sync.json").exists() else {}
        groups = [(s["id"], [f for f in s["files"] if f in videos]) for s in sesmod.media_sessions(self.ep)]
        groups = [(sid, fs) for sid, fs in groups if fs]
        multi = len(groups) > 1
        if not multi:
            groups = [(None, videos)]
        key_of = {v["file"]: k for k, v in existing.items()}
        cameras, margin_db, added = dict(existing), float(self.pol["map"]["min_margin_db"]), []

        def name(s):
            return (sp.get(s) or {}).get("name") or s
        for gi, (sid, files) in enumerate(groups, 1):
            fresh = [f for f in files if f not in mapped]
            if not fresh:
                continue

            def has_cam(s):
                info = sp.get(s) or {}
                k = (info.get("cameras") or {}).get(sid) if multi else info.get("camera")
                k = k or (info.get("camera") if multi and (existing.get(info.get("camera")) or {}).get("session", sid) == sid else None)
                return k in existing and existing[k]["file"] in files
            todo = [s for s in order if not has_cam(s)]
            span = min(syncs.get(f, {}).get("start_offset", 0.0) for f in files)
            utts: dict[str, list] = {s: [] for s in order}
            for u in tr.get("utterances") or []:
                if u["speaker"] in utts and u["end"] - u["start"] >= 0.5 and u["start"] >= span:
                    utts[u["speaker"]].append((u["start"], u["end"]))
            for s in utts:
                utts[s] = sorted(utts[s], key=lambda x: x[0] - x[1])[:40]
            levels = {f: self._levels(f, utts, syncs) for f in fresh}

            def score(f, s):
                L = levels.get(f) or {}
                others = [L[o] for o in L if o != s]
                return (L[s] - sum(others) / len(others)) if s in L and others else 0.0
            n = min(len(todo), len(fresh))
            ranked = sorted(itertools.permutations(fresh, n), key=lambda perm: -sum(score(f, s) for f, s in zip(perm, todo)))
            gap = (sum(score(f, s) for f, s in zip(ranked[0], todo)) - sum(score(f, s) for f, s in zip(ranked[1], todo))) if len(ranked) > 1 else 0.0
            weak = gap < margin_db
            if weak:                       # no clear evidence: never let noise pick; the first to speak goes on the first camera
                plain = tuple(fresh[:n])
                ranked = [plain] + [x for x in ranked if x != plain]
            best = ranked[0]
            pick = dict(zip(todo, best))
            if not existing:
                for s in todo[n:]:         # more speakers than cameras: each extra one shares the camera that hears them best
                    pick[s] = max(fresh, key=lambda f: score(f, s))
            for f in fresh:
                k, i = None, 0
                while k is None or k in cameras:
                    k = chr(ord("A") + i) + (str(gi) if multi else "")
                    i += 1
                if pick.get(order[0]) == f:
                    role = "host"
                elif f in pick.values():
                    role = "guest"
                else:
                    role = "wide"
                cameras[k] = {"file": f, "role": role, **({"session": sid} if multi else {})}
                key_of[f] = k
                added.append(k)
            for s, f in pick.items():
                info = sp.setdefault(s, {})
                if multi:
                    info.setdefault("cameras", {})[sid] = key_of[f]
                    info.setdefault("camera", key_of[f])
                else:
                    info["camera"] = key_of[f]

            def says(perm):
                bits = [f"{name(s)} on {Path(f).name}" for s, f in zip(todo, perm)]
                bits += [f"{Path(f).name} as the wide" for f in fresh if f not in perm]
                return ", ".join(bits) or "every new camera as the wide"
            if len(files) > 1 or existing:
                where = f" for session {sid}" if sid else ""
                guess = (" (a guess: same sound on both cameras)" if len(fresh) == 2 else " (a guess: same sound on every camera)") if weak and n > 1 else ""
                if weak and n > 1:
                    why = ("The cameras' own sound does not tell the speakers apart" + (f" (best beats next by {gap:.1f} dB)" if levels else "")
                           + ", so the first to speak is on the first camera by file name. A still from each camera would settle it.")
                elif n > 1:
                    why = (f"{name(todo[0])} is {score(best[0], todo[0]):.1f} dB louder on {Path(best[0]).name}'s own sound than the others, "
                           f"and this mapping beats the next by {gap:.1f} dB.")
                else:
                    why = "Every speaker already had a camera, or there was only one choice."
                wide = [Path(f).name for f in fresh if f not in pick.values()]
                if wide:
                    why += f" {', '.join(wide)} frames nobody in particular, so it is the wide."
                if existing:
                    kept = ", ".join(f"{k} {Path(v['file']).name}" for k, v in existing.items())
                    self.board.decide(f"map-new-{sid or 'main'}", f"Added new camera(s){where} to the existing mapping{guess}: {says(best)}",
                                      [says(p) for p in ranked[:7]] + [f"leave {', '.join(Path(f).name for f in fresh)} out of the edit"],
                                      says(best), why + f" The cameras already mapped were kept as they were ({kept}).")
                else:
                    self.board.decide(f"map-{sid or 'main'}", f"Camera mapping{where}{guess}: {says(best)}",
                                      [says(p) for p in ranked[:8]], says(best), why)
        meta["cameras"] = cameras
        meta.setdefault("audio_source", tr["source"])
        write_json(self.ep.meta_path, meta)
        self.ep.mark_stage("map", by="claude", flagged=True)
        return "done", ("added " if existing else "mapped ") + ", ".join(f"{k} {Path(cameras[k]['file']).name} ({cameras[k]['role']})" for k in added)

    def s_plan(self):
        from . import cutplan, sessions as sesmod
        ids = self.sessions()
        if self.fresh(all(sesmod.planned(self.ep, s) for s in (ids or [None]))):
            return "reused", "cut list(s) already planned"
        stats = [cutplan.build_cutlist(self.ep, session=s)["stats"] for s in (ids or [None])]
        return "done", "planned " + "; ".join(json.dumps(x)[:120] for x in stats)

    def s_trim(self):
        from . import trim
        keys = self.sessions() or [None]
        book = self.meta().get("trim") or {}
        sug_p = self.ep.artifact(trim.SUGGESTIONS)
        sug = read_json(sug_p) if sug_p.exists() else {}

        def settled(k):
            key = trim.session_key(self.ep, k)
            s = sug.get(key)
            return bool(book.get(key)) or (s is not None and s["head"]["proposed_in"] is None and s["tail"]["proposed_out"] is None)
        if self.fresh(all(settled(k) for k in keys)):
            return "reused", "trim already " + ", ".join(
                f"{trim.session_key(self.ep, k)}: {'set by ' + book[trim.session_key(self.ep, k)]['by'] if book.get(trim.session_key(self.ep, k)) else 'not needed'}"
                for k in keys)
        notes, changed = [], False
        for k in keys:
            before = (self.meta().get("trim") or {}).get(trim.session_key(self.ep, k))
            r = trim.propose(self.ep, session=k, home=self.home)
            key = r["session"]
            if r["applied"]:
                rec = r["trim"]
                ends = " ".join(x for x in (f"in {rec['in']:.2f}s" if rec.get("in") is not None else "",
                                            f"out {rec['out']:.2f}s" if rec.get("out") is not None else "") if x)
                self.board.decide(f"trim-{key}", f"Trim for the draft ({key}): {ends}",
                                  [ends, "keep the whole recording"], ends,
                                  r["reason"] + ". Used for drafts only: the final needs your `ave trim set`.")
                notes.append(f"{key}: {ends} (draft only, flagged)")
                changed = changed or before != rec
            elif r["suggestion"] is not None and r["weak"]:
                w = r["weak"]
                self.board.decide(f"trim-{key}", f"Kept the whole recording for the draft ({key}): the trim suggestion was weak",
                                  ["keep the whole recording", *(f"trim {x['end']} at {x['at']:.2f}s" for x in w)], "keep the whole recording",
                                  "; ".join(f"{x['end']} {x['at']:.2f}s, confidence {x['confidence']:.2f}: {x['reason']}" for x in w))
                notes.append(f"{key}: whole recording kept (weak suggestion flagged)")
            else:
                notes.append(f"{key}: {r['reason']}")
        if not changed and not self.redone:
            return "reused", "; ".join(notes)
        return "done", "; ".join(notes)

    def _canvas(self) -> tuple[int, int]:
        cams = [m["video"] for m in self.media() if m["kind"] == "video" and m.get("video")]
        mw, mh = self.pol["assemble"]["max_canvas"]
        if not cams:
            return mw, mh
        w, h = max((c["width"], c["height"]) for c in cams)
        return (w, h) if h < mh and w <= mw else (mw, mh)

    def s_assemble(self):
        from . import editdoc, trim
        from .commands.core import _assemble_one
        ids = self.sessions()
        meta = self.meta()
        latest = {s: editdoc.latest_path(self.ep, "assembly", s) for s in (ids or [None])}

        def current(s, p):
            if p is None:
                return False
            used = editdoc.load(p).get("trim") or {}
            rec = trim.get(self.ep, meta, s) or {}
            return {k: used.get(k) for k in ("in", "out", "by")} == {k: rec.get(k) for k in ("in", "out", "by")}
        if self.fresh(all(current(s, p) for s, p in latest.items())):
            return "reused", "assembly already made: " + ", ".join(p.name for p in latest.values())
        w, h = self._canvas()
        out = [_assemble_one(self.ep, (w, h), s, actor=ACTOR) for s in (ids or [None])]
        mw, mh = self.pol["assemble"]["max_canvas"]
        if (w, h) != (mw, mh):
            self.board.decide("canvas", f"Assembly canvas {w}x{h}, the cameras' own size",
                              [f"{w}x{h} (the cameras' own size)", f"{mw}x{mh} (the usual size)"], f"{w}x{h} (the cameras' own size)",
                              "The cameras are smaller than the usual canvas, so scaling them up would only make a bigger file.")
        if ids:
            self.board.decide("sessions", f"Assembly: one per session ({', '.join(ids)})",
                              ["one assembly per session", "one combined assembly (`ave assemble --combined`)"], "one assembly per session",
                              "Each session can be judged on its own; joining them is a word away.")
        return "done", "; ".join(f"{Path(r['saved']).name}: {r['video_segments']} shots, {r['duration']:.1f}s, nothing removed" for r in out)

    def s_cleanup(self):
        from . import cleanup, editdoc
        ids = self.sessions()

        def current(s):
            r, a = editdoc.latest_path(self.ep, "rough", s), editdoc.latest_path(self.ep, "assembly", s)
            if r is None or a is None:
                return False
            rep = self.ep.artifact(cleanup.report_name(s))
            src = (read_json(rep).get("assembly") if rep.exists() else None) or {}
            made_from = src.get("file") if isinstance(src, dict) else src
            return (made_from and Path(str(made_from)).name == a.name) or (not made_from and r.stat().st_mtime >= a.stat().st_mtime)
        if self.fresh(all(current(s) for s in (ids or [None]))):
            return "reused", "rough cut already made: " + ", ".join(editdoc.latest_path(self.ep, "rough", s).name for s in (ids or [None]))
        meta = self.meta()
        pacing = len(meta.get("cameras") or {}) > 1
        notes, review = [], 0
        for s in ids or [None]:
            r = cleanup.run(self.ep, name=s, pacing=pacing, actor=ACTOR)
            t = r["totals"]
            review += sum(1 for d in r["decisions"] if d["review"])
            notes.append(f"{Path(r['rough']['file']).name}: {t['input_duration_s']:.1f}s -> {t['output_duration_s']:.1f}s, {t['cuts']} cuts")
        if pacing:
            self.board.decide("pacing", "Rough cut: pacing pass on (reaction shots break long holds on one face)",
                              ["pacing on", "pacing off (`ave cleanup --no-pacing`)"], "pacing on",
                              "There is more than one camera, so long holds can be broken with the other person listening; "
                              "the cleanup policy says how long a hold may be (pacing.max_hold_s).")
        if review:
            self.report.notes.append(f"the cleanup left {review} spot(s) uncut for you to look at: `ave cleanup explain {self.ep.root}`")
        return "done", "; ".join(notes)

    def s_rough(self):
        from . import compile as comp, editdoc, presets
        ids = self.sessions()
        preset = self.pol["render"]["preset"]
        ext = presets.get(preset)["ext"]
        outs = {s: self.ep.exports / f"{editdoc.latest_path(self.ep, 'rough', s).name[:-len('.json')]}.{preset}{ext}" for s in (ids or [None])}
        if self.fresh(all(o.exists() for o in outs.values())):
            return "reused", "draft already rendered: " + ", ".join(self.ep.rel(o) for o in outs.values())
        notes = []
        for s, out in outs.items():
            doc = editdoc.load(editdoc.latest_path(self.ep, "rough", s))
            try:
                r = comp.render(self.ep, doc, out, preset=preset, overwrite=True)
            except (comp.RenderError, comp.ffutil.FFmpegError, ValueError, FileNotFoundError) as e:
                raise SystemExit(f"draft render failed: {e}") from e
            notes.append(f"{self.ep.rel(out)} ({r['duration']:.1f}s, {'verified' if r.get('ok') else 'NOT verified'})")
        return "done", "; ".join(notes)

    def s_clips(self):
        from . import clips
        p = self.ep.artifact("clips.json")
        if self.fresh(p.exists()):
            return "reused", "clip candidates already proposed (work/clips.json)"
        names = self.sessions() or [None]
        n = 0
        for s in names:
            clips.prepare(self.ep, "rough", name=s)
            r = clips.propose(self.ep, None, name=s, recall_fill=True)
            n += len(r["accepted"])
        return "done", f"{n} clip candidate(s) proposed for you to choose from (`ave clips review {self.ep.root}`); none chosen"


def _prepare(root: Path) -> Episode:
    from .project.adopt import adopt
    root = Path(root).expanduser().resolve()
    ep = Episode(root)
    if not ep.meta_path.exists():
        from . import episode as epmod
        choice = epmod.detect_layout(root)
        has_raw = any((root / d).is_dir() for d in ("raw", "02_FOOTAGE", "03_AUDIO"))
        if root.is_dir() and not has_raw and any(p.suffix.lower() in epmod.MEDIA_EXTS for p in root.iterdir() if p.is_file()):
            raise StartError("ingest", f"{root} has footage at its top level. Original footage is never moved, so put the recordings in a "
                                       f"raw/ folder (or 02_FOOTAGE and 03_AUDIO) yourself, then run `ave start {root}` again.")
        ep.create(layout=None if choice == "flat" else choice)
        from . import progress
        progress.adopt(ep)
    if not (root / ".ave" / "project.json").is_file():
        adopt(root)
    else:
        from .project.adopt import align_layout
        align_layout(root)                       # project.json made before this episode chose its layout
    return ep


def run(root, through: str = "rough", clips: bool = False, speakers: dict | None = None, home: Path | None = None,
        answer: str | None = None) -> Report:
    """Take the episode at `root` to `through` (default the rough cut). Resumable; raises NeedsDirector for speaker identity and
    StartError when a stage fails, both carrying the report so far."""
    if through not in STEPS:
        raise ValueError(f"--through must be one of {', '.join(STEPS)}")
    t0 = time.monotonic()
    ep = _prepare(root)
    r = _Run(ep, through, clips, speakers, home, answer)
    try:
        for name in STEPS[: STEPS.index(through) + 1]:
            r.step(name, getattr(r, "s_" + name))
        if clips:
            r.step("clips", r.s_clips)
    except (NeedsDirector, StartError) as e:
        r.report.stopped = getattr(e, "step", None) or "speakers"
        _finish(r, t0)
        e.report = r.report
        raise
    _finish(r, t0)
    from . import progress, state
    n = len(r.report.decisions_open)
    if through == "rough":
        state.set_review(ep, f"rough cut ready from `ave start`, with {n} decision(s) Claude made alone flagged on the board: review the cut and answer each")
    progress.log(ep, f"ave start ran through {through}: " + ", ".join(f"{s['step']} {s['status']}" for s in r.report.steps)
                 + f"; {len(r.report.decisions_new)} new flagged decision(s)", kind="note", step="start")
    return r.report


def _finish(r: _Run, t0: float) -> None:
    r.report.decisions_new = [{k: v for k, v in d.items() if k != "key"} for d in r.board.new]
    try:
        r.report.decisions_open = r.board.open_decisions()
    except Exception:          # the board must never hide the report
        r.report.decisions_open = list(r.report.decisions_new)
    r.report.seconds = round(time.monotonic() - t0, 1)
