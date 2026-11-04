"""Names derived from a version (exports, captions, cached pictures) never collide when two computers each make v1."""

from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from ave import audiogram, editdoc, final, shorts, stack
from ave.episode import Episode, write_json
from ave.project import Project
from ave.project.adopt import adopt
from conftest import needs_ffmpeg

NO_FACE = lambda img: []  # noqa: E731


def _computer(monkeypatch, tmp_path, name):
    home = tmp_path / f"home-{name}"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))


def _clip(ep, slug="synth-clip"):
    base = editdoc.load_latest(ep, "assembly")
    sl = editdoc.slice_doc(base, 7.5, 29.5, stage="select")
    sl["tracks"]["dialogue"][0]["xfade_ms"] = 0
    path = editdoc.save_new_version(ep, sl, name=slug)
    clip = {"id": "c01", "slug": slug, "title": "Synthetic clip", "hook": "Most people never finish what they start",
            "rationale": "test", "status": "approved", "start": 7.5, "end": 29.5, "duration": 22.0, "total": 71.0,
            "sentence_ids": ["s001"], "start_complete": True, "end_complete": True, "complete_statement": True,
            "edit": {"name": slug, "version": int(editdoc.VERSION_RE.match(path.name)["n"]), "path": str(path.relative_to(ep.root)),
                     "range": [7.5, 29.5], "source": ["assembly", 1]}}
    write_json(ep.work / "clips.json", {"clips": [clip]})
    return clip


def _sync(a, b):
    """Both ways: version files, captions, exports and logs arrive; nothing that exists is overwritten (logs only grow)."""
    for src, dst in ((a, b), (b, a)):
        for f in src.rglob("*"):
            rel = f.relative_to(src)
            if not f.is_file() or rel.parts[0] == ".ave" and rel.parts[1] != "log" or rel.parts[0] == "work":
                continue
            t = dst / rel
            if rel.parts[:2] == (".ave", "log"):
                if not t.exists() or t.stat().st_size < f.stat().st_size:
                    t.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, t)
            elif not t.exists():
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, t)


def _need_ready(root, need):
    p = root / need["path"]
    return p.is_file() and p.stat().st_size == need["bytes"] and hashlib.sha256(p.read_bytes()).hexdigest() == need["sha256"]


@needs_ffmpeg
def test_two_computers_render_their_v1_to_different_files(synth_episode, tmp_path, monkeypatch):
    _computer(monkeypatch, tmp_path, "a")
    a = synth_episode.root
    adopt(a)
    clip = _clip(synth_episode)
    b = tmp_path / "on-b"
    shutil.copytree(a, b)

    built, exported, finals = {}, {}, {}
    for name, root in (("a", a), ("b", b)):
        _computer(monkeypatch, tmp_path, name)
        ep = Episode(root)
        built[name] = shorts.build_short(ep, clip, detector=NO_FACE)
        exported[name] = shorts.export_short(ep, clip, ["youtube"])
        finals[name] = final.build_final(ep, render=True, do_retranscribe=False, preset="proxy")
    _computer(monkeypatch, tmp_path, "a")
    tag_a = Project.open(a).device.short
    _computer(monkeypatch, tmp_path, "b")
    tag_b = Project.open(b).device.short
    assert tag_a != tag_b
    assert built["a"]["label"] == f"v1~{tag_a}" and built["b"]["label"] == f"v1~{tag_b}"
    assert exported["a"]["exports"][0]["video"] != exported["b"]["exports"][0]["video"]
    assert f"v1~{tag_a}.youtube.mp4" in exported["a"]["exports"][0]["video"]
    assert finals["a"]["render"]["out"] != finals["b"]["render"]["out"] and f"~{tag_b}." in finals["b"]["render"]["out"]

    _sync(a, b)
    for root in (a, b):
        ep = Episode(root)
        shorts_docs = [editdoc.load(v.path) for v in editdoc.version_entries(ep, "short", "synth-clip")]
        srts = {d["captions"]["srt"] for d in shorts_docs}
        assert len(shorts_docs) == 2 and len(srts) == 2              # each short has its own caption files
        finals_srt = [editdoc.load(v.path)["captions"]["srt"] for v in editdoc.version_entries(ep, "final")]
        captioned = [x for x in finals_srt if x]
        assert len(finals_srt) == 4 and len(set(captioned)) == len(captioned) == 2   # two computers' captioned finals, never shared
        # every caption a logged version needs is still exactly the file it recorded
        events = [e for e in Project.open(root).read_events() if e["op"] == "version.added"]
        caption_needs = [n for e in events for n in e["needs"] if n["path"].endswith((".srt", ".ass"))]
        assert caption_needs and all(_need_ready(root, n) for n in caption_needs)


def test_unadopted_names_are_unchanged():
    assert editdoc.label(3) == "v3" and editdoc.label(editdoc.Version(3)) == "v3"
    assert editdoc.label(editdoc.Version(3, "abcd")) == "v3~abcd"
    assert editdoc.label_of("short.x.v3.json") == "v3" and editdoc.label_of("short.x.v3~abcd.json") == "v3~abcd"


def test_stack_picture_is_keyed_on_label_and_doc(tmp_path, monkeypatch):
    ep = Episode(tmp_path / "ep")
    ep.create("ep")
    calls = []

    def fake(ep_, doc, out):
        calls.append(out.name)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"pic")
        return out

    monkeypatch.setattr(stack, "render_picture", fake)
    doc = {"short": {"slug": "s"}, "sources": {}, "tracks": {"video": []}, "duration": 1.0}
    d1 = stack.render_doc(ep, doc, "v8~aaaa")
    stack.render_doc(ep, doc, "v8~aaaa")                       # same version, same doc: reused
    d2 = stack.render_doc(ep, doc, "v8~bbbb")                  # another computer's v8: its own picture
    assert calls == ["stack.s.v8~aaaa.mp4", "stack.s.v8~bbbb.mp4"]
    assert d1["sources"]["STACK"]["file"] != d2["sources"]["STACK"]["file"]
    changed = json.loads(json.dumps(doc)); changed["duration"] = 2.0
    stack.render_doc(ep, changed, "v8~aaaa")                   # same name, different doc: rendered again
    assert calls[-1] == "stack.s.v8~aaaa.mp4" and len(calls) == 3
    assert stack.render_doc(ep, doc, 8)["sources"]["STACK"]["file"].endswith("stack.s.v8.mp4")   # legacy number


def test_audiogram_picture_uses_the_label(tmp_path, monkeypatch):
    ep = Episode(tmp_path / "ep")
    ep.create("ep")
    monkeypatch.setattr(audiogram, "render_picture", lambda ep_, doc, out, cfg: out)
    doc = {"sources": {}, "tracks": {"video": []}, "duration": 1.0}
    assert audiogram.attach(ep, doc, "s", "v2~abcd", {})["picture"].endswith("audiogram.s.v2~abcd.mp4")
    assert audiogram.attach(ep, doc, "s", 2, {})["picture"].endswith("audiogram.s.v2.mp4")


def test_quote_reel_next_path_is_tagged_only_when_given(tmp_path):
    from ave.quote_reel import next_path
    (tmp_path / "reel.v1.mp4").write_bytes(b"")
    (tmp_path / "reel.v2~abcd.mp4").write_bytes(b"")
    assert next_path(tmp_path, "reel").name == "reel.v3.mp4"
    assert next_path(tmp_path, "reel", "efgh").name == "reel.v3~efgh.mp4"


def test_handoff_dialogue_master_is_keyed_on_label_and_doc(tmp_path, monkeypatch):
    from ave import compile as comp
    from ave.commands import handoff as H
    ep = Episode(tmp_path / "ep")
    ep.create("ep")
    made = []
    monkeypatch.setattr(comp, "compile_audio", lambda ep_, doc, out: (made.append(out.name), out.write_bytes(b"wav")))
    doc = {"stage": "rough", "duration": 1.0}
    assert H._master_audio(ep, doc, "rough", "v8~aaaa").name == "rough.v8~aaaa.dialogue.wav"
    H._master_audio(ep, doc, "rough", "v8~aaaa")
    H._master_audio(ep, doc, "rough", "v8~bbbb")
    H._master_audio(ep, dict(doc, duration=2.0), "rough", "v8~aaaa")
    assert made == ["rough.v8~aaaa.dialogue.wav", "rough.v8~bbbb.dialogue.wav", "rough.v8~aaaa.dialogue.wav"]
    assert H._master_path(ep, "rough", 8).name == "rough.v8.dialogue.wav"          # un-adopted: today's name


def test_each_computers_v8_gets_its_own_post_origin(tmp_path):
    from ave.studio.ingest_shorts import _origin_ref
    ep = Episode(tmp_path / "ep")
    clip = {"id": "c01"}
    assert _origin_ref(ep, clip, 8).endswith("::short::c01::v8")                   # legacy ref unchanged
    assert _origin_ref(ep, clip, "v8~aaaa") != _origin_ref(ep, clip, "v8~bbbb")


def test_ambiguous_numbers_explain_the_choices(tmp_path, capsys):
    from ave.cli import main
    ep = Episode(tmp_path / "ep")
    ep.create("ep")
    for tag in ("aaaa", "bbbb"):
        (ep.edits / f"rough.v8~{tag}.json").write_text("{}")
    assert editdoc.resolve(ep, "rough", 8, sha256=hashlib.sha256(b"{}").hexdigest()).name == "rough.v8~aaaa.json"  # same bytes
    with pytest.raises(editdoc.AmbiguousVersion, match="Choose one: v8~aaaa, v8~bbbb"):
        editdoc.path_for(ep, "rough", 8)
    with pytest.raises(SystemExit, match="Choose one: v8~aaaa, v8~bbbb"):
        main(["render", str(ep.root), "--stage", "rough", "--version", "8"])
