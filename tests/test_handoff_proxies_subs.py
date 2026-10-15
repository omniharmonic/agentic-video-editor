"""W10 `--use-proxies` and W7 `--try-subtitles` through scripts/resolve_handoff.py against the in-memory fake (never live Resolve).

Real-Resolve behaviour of both is UNVERIFIED; these tests pin the call sequence, the read-back logic and the fallbacks.
"""
import json
import os
import subprocess
from types import SimpleNamespace
from argparse import Namespace
from pathlib import Path

import pytest

from ave import handoff, proxies
from ave.commands import handoff as cmd_mod
from conftest import needs_ffmpeg
from test_handoff_script import TL0, FakeResolve, _doc_with_cuts, run_script

pytestmark = needs_ffmpeg

SRT = "1\n00:00:00,000 --> 00:00:01,000\nhello\n\n2\n00:00:01,000 --> 00:00:02,500\nworld\n"


class BridgeFake(FakeResolve):
    """FakeResolve + link_proxy_checked (with a MCP-shaped read-back) + an SRT that Resolve may or may not place."""

    def __init__(self, *, subtitle_mode="places", link_mode="ok"):
        super().__init__()
        self.proxy_of: dict[str, str] = {}
        self.link_calls: list[dict] = []
        self.subtitle_mode = subtitle_mode      # places | ignores | wrong_track | short
        self.link_mode = link_mode              # ok | no_readback | fail | raise
        self.sub_items = 0
        self.sub_tracks = 0

    def media_pool(self, action, params=None):
        p = params or {}
        if action == "link_proxy_checked":
            self.link_calls.append(p)
            if self.link_mode == "raise":
                raise RuntimeError("stale_handle")
            if self.link_mode == "fail":
                return {"success": False}
            self.proxy_of[p["clip_id"]] = p["proxy_path"]
            fields = {"Proxy Media Path": p["proxy_path"]} if self.link_mode == "ok" else {}
            return {"success": True, "readback": {"proxy_fields": fields},
                    "verified_operation": {"verification_status": "readback_verified" if fields else "api_success_unverified"}}
        if action == "append_to_timeline" and p.get("clip_ids") and not p.get("clip_infos"):
            if self.pool[p["clip_ids"][0]].endswith(".srt"):
                if self.subtitle_mode == "places":
                    self.sub_tracks, self.sub_items = 1, 2
                elif self.subtitle_mode == "short":
                    self.sub_tracks, self.sub_items = 1, 1
                elif self.subtitle_mode == "wrong_track":
                    self.tl["n"]["video"] += 1
                return {"success": True, "count": 1}
        return super().media_pool(action, params)

    def timeline(self, action, params=None):
        p = params or {}
        if p.get("track_type") == "subtitle":
            if action == "get_track_count":
                return {"count": self.sub_tracks}
            if action == "get_items":
                return {"items": [{"start": TL0, "duration": 30, "name": "x"}] * self.sub_items}
        return super().timeline(action, params)


def _plan(synth_episode, tmp_path, *, proxy_map=None, srt=None, try_subtitles=False):
    doc = _doc_with_cuts(synth_episode)
    master = tmp_path / "master.wav"
    master.write_bytes(b"x")
    p = handoff.plan(synth_episode, doc, name="T assembly v1", audio_master=master, proxies=proxy_map, srt=srt,
                     try_subtitles=try_subtitles, srt_copy=tmp_path / "T assembly v1.srt")
    for f in p["import"]:
        Path(f).touch(exist_ok=True)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(p))
    return doc, p, path


def _fake_proxies(ep, doc):
    out = {}
    for f in proxies.footage_used(ep, doc):
        px = proxies.proxy_path(ep, f)
        px.parent.mkdir(parents=True, exist_ok=True)
        px.touch()
        out[str(f)] = str(px)
    return out


def test_plan_keeps_originals_and_records_the_proxy_map(synth_episode, tmp_path):
    doc = _doc_with_cuts(synth_episode)
    pm = _fake_proxies(synth_episode, doc)
    _, p, _ = _plan(synth_episode, tmp_path, proxy_map=pm)
    assert p["proxies"] == pm
    assert all(v["file"] in pm for v in p["video"])                          # every clip still names the ORIGINAL
    assert not any(".proxies" in f or "/proxies/" in f for f in p["import"]) # proxies are never imported as media


def test_use_proxies_links_each_original_and_reads_back(monkeypatch, capsys, synth_episode, tmp_path):
    doc = _doc_with_cuts(synth_episode)
    pm = _fake_proxies(synth_episode, doc)
    _, p, path = _plan(synth_episode, tmp_path, proxy_map=pm)
    fake = BridgeFake()
    code, data, err = run_script(monkeypatch, capsys, fake, path, chunk=3)
    assert code == 0 and data["ok"], (data, err)
    r = data["proxies"]
    assert (r["requested"], r["linked"], r["readback_verified"], r["failed"]) == (len(pm), len(pm), len(pm), [])
    assert "UNVERIFIED" in r["status"]
    by_path = {v: k for k, v in fake.pool.items()}
    for orig, px in pm.items():
        assert fake.proxy_of[by_path[orig]] == px                            # the proxy is attached to the clip imported from the original
    assert all(not f.endswith(".mov") or "/proxies/" not in f for f in fake.pool.values())
    assert {os.path.basename(i["name"]) for i in fake.tl["tracks"][("video", 1)]} <= {Path(f).name for f in pm}


def test_link_failures_are_reported_not_fatal(monkeypatch, capsys, synth_episode, tmp_path):
    pm = _fake_proxies(synth_episode, _doc_with_cuts(synth_episode))
    for mode in ("fail", "raise"):
        _, _, path = _plan(synth_episode, tmp_path, proxy_map=pm)
        code, data, _ = run_script(monkeypatch, capsys, BridgeFake(link_mode=mode), path, chunk=3)
        assert code == 0 and data["ok"]                                      # the timeline is fine; originals just play unproxied
        assert data["proxies"]["linked"] == 0 and len(data["proxies"]["failed"]) == len(pm)


def test_unread_link_is_counted_as_linked_but_not_verified(monkeypatch, capsys, synth_episode, tmp_path):
    pm = _fake_proxies(synth_episode, _doc_with_cuts(synth_episode))
    _, _, path = _plan(synth_episode, tmp_path, proxy_map=pm)
    code, data, _ = run_script(monkeypatch, capsys, BridgeFake(link_mode="no_readback"), path, chunk=3)
    assert data["proxies"]["linked"] == len(pm) and data["proxies"]["readback_verified"] == 0


def test_no_proxies_means_no_link_calls(monkeypatch, capsys, synth_episode, tmp_path):
    _, _, path = _plan(synth_episode, tmp_path)
    fake = BridgeFake()
    code, data, _ = run_script(monkeypatch, capsys, fake, path, chunk=3)
    assert code == 0 and fake.link_calls == [] and "proxies" not in data


def test_cmd_use_proxies_requires_proxies_and_records_originals(synth_episode, monkeypatch):
    seen = {}

    def fake_run(c, **k):
        seen["plan"] = json.loads(Path(c[2]).read_text())
        return subprocess.CompletedProcess(c, 0)
    monkeypatch.setattr(cmd_mod, "subprocess", SimpleNamespace(run=fake_run))
    args = dict(episode=str(synth_episode.root), stage="assembly", name=None, label=None, no_master=True, project=None,
                dry_run=True, layers=False, use_proxies=True, try_subtitles=False)
    with pytest.raises(SystemExit, match="ave proxies"):
        cmd_mod.cmd(Namespace(**args))
    proxies.run(synth_episode, proxies.footage_used(synth_episode, _doc_with_cuts(synth_episode)), "dnxhr-lb", 1080, hw=False,
                log=lambda m: None)
    assert cmd_mod.cmd(Namespace(**args)) == 0
    plan = seen["plan"]
    assert plan["proxies"] and all("/proxies/" not in v["file"] for v in plan["video"])


# ---- W7 subtitles

def _srt(tmp_path):
    f = tmp_path / "caps.srt"
    f.write_text(SRT)
    return f


def test_subtitles_not_attempted_without_the_flag_but_instructions_are_printed(monkeypatch, capsys, synth_episode, tmp_path):
    _, _, path = _plan(synth_episode, tmp_path, srt=_srt(tmp_path))
    fake = BridgeFake()
    code, data, _ = run_script(monkeypatch, capsys, fake, path, chunk=3)
    assert code == 0 and data["subtitles"].startswith("manual (not attempted")
    assert fake.sub_tracks == 0 and not any(v.endswith(".srt") for v in fake.pool.values())      # Resolve was not touched for the SRT
    assert any("File > Import > Subtitle" in l for l in data["subtitle_instructions"])
    assert "T assembly v1.srt" in " ".join(data["subtitle_instructions"])


def test_try_subtitles_declares_success_only_when_the_track_has_every_cue(monkeypatch, capsys, synth_episode, tmp_path):
    _, p, path = _plan(synth_episode, tmp_path, srt=_srt(tmp_path), try_subtitles=True)
    assert p["subtitles"]["cues"] == 2
    code, data, _ = run_script(monkeypatch, capsys, BridgeFake(subtitle_mode="places"), path, chunk=3)
    assert code == 0 and data["subtitles"].startswith("on the timeline") and "UNVERIFIED" in data["subtitles"]
    assert "subtitle_instructions" not in data


@pytest.mark.parametrize("mode, text", [("ignores", "no subtitle track"), ("wrong_track", "VIDEO/AUDIO"), ("short", "1 items but the SRT has 2")])
def test_try_subtitles_falls_back_to_manual_steps(monkeypatch, capsys, synth_episode, tmp_path, mode, text):
    _, _, path = _plan(synth_episode, tmp_path, srt=_srt(tmp_path), try_subtitles=True)
    code, data, _ = run_script(monkeypatch, capsys, BridgeFake(subtitle_mode=mode), path, chunk=3)
    assert code == 0 and data["ok"] and data["subtitles"].startswith("manual") and text in data["subtitles"]
    assert data["subtitle_instructions"]


def test_cmd_writes_the_srt_next_to_the_timeline_name(synth_episode, monkeypatch):
    monkeypatch.setattr(cmd_mod, "subprocess", SimpleNamespace(run=lambda c, **k: subprocess.CompletedProcess(c, 0)))
    ep = synth_episode
    srt = ep.captions / "x.srt"
    srt.parent.mkdir(parents=True, exist_ok=True)
    srt.write_text(SRT)
    doc_path = next(iter(sorted((ep.edits).glob("assembly.v*.json"))))
    d = json.loads(doc_path.read_text())
    d["captions"] = {"srt": ep.rel(srt)}
    doc_path.write_text(json.dumps(d))
    args = dict(episode=str(ep.root), stage="assembly", name=None, label=None, no_master=True, project=None,
                dry_run=False, layers=False, use_proxies=False, try_subtitles=False)
    assert cmd_mod.cmd(Namespace(**args)) == 0
    copies = [p for p in ep.captions.glob("*assembly v*.srt")]
    assert len(copies) == 1 and copies[0].read_text() == SRT
