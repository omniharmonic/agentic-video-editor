"""CLI surface and the episode-level legal gate, with HTTP mocked."""
import argparse
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from ave import editdoc
from ave.assets import licenses, providers
from ave.commands import assets as cmd

from test_assets_providers import DATA, FakeResp, FakeSession, load


def run(capsys, *argv):
    p = argparse.ArgumentParser()
    cmd.register(p.add_subparsers(dest="cmd", required=True))
    a = p.parse_args(["assets", *map(str, argv)])
    rc = a.fn(a)
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


@pytest.fixture
def fake_providers(monkeypatch, tmp_path):
    http = FakeSession({"https://api.pexels.com/videos/search": FakeResp(load("pexels_videos_search.json")),
                        "https://pixabay.com/api/videos/": FakeResp(load("pixabay_videos_search.json")),
                        "https://videos.pexels.com/": FakeResp(content=b"PEXELS" * 20),
                        "https://cdn.pixabay.com/": FakeResp(content=b"PIXABAY" * 20)})

    def make(name):
        return providers.PROVIDERS[name](api_key="K", http=http, cache_dir=tmp_path / "cache")
    monkeypatch.setattr(cmd, "_provider", make)
    return http


def test_stock_search_refuses_cliche_queries_before_any_network(synth_episode, capsys, fake_providers):
    with pytest.raises(SystemExit, match="cliche"):
        run(capsys, "stock-search", synth_episode.root, "business handshake")
    assert fake_providers.calls == []
    rc, out = run(capsys, "stock-search", synth_episode.root, "business handshake", "--allow-cliche", "--provider", "pexels")
    assert rc == 0 and out["count"] == 2


def test_stock_search_flags_cliche_tags_on_results_and_persists_candidates(synth_episode, capsys, fake_providers):
    rc, out = run(capsys, "stock-search", synth_episode.root, "hands clay")
    ids = {(c["provider"], c["asset_id"]) for c in out["candidates"]}
    assert ("pexels", "3195394") in ids and ("pixabay", "125") in ids
    neon = next(c for c in out["candidates"] if c["asset_id"] == "9001")
    assert neon["cliche_tags"] is None  # "neon" alone is not on the list; only neon + brain
    assert (synth_episode.work / "assets" / "candidates.json").exists()


def test_stock_get_needs_prior_search_and_a_named_approver(synth_episode, capsys, fake_providers):
    with pytest.raises(SystemExit, match="stock-search"):
        run(capsys, "stock-get", synth_episode.root, "pexels", "3195394", "--approved-by", "ben")
    run(capsys, "stock-search", synth_episode.root, "hands clay", "--provider", "pexels")
    with pytest.raises(SystemExit, match="approved_by"):
        run(capsys, "stock-get", synth_episode.root, "pexels", "3195394", "--approved-by", " ")
    assert licenses.log_for(synth_episode).entries() == []
    rc, out = run(capsys, "stock-get", synth_episode.root, "pexels", "3195394", "--approved-by", "ben", "--query", "hands clay")
    assert rc == 0 and (synth_episode.root / out["file"]).read_bytes() == b"PEXELS" * 20
    e = licenses.log_for(synth_episode).entries()
    assert len(e) == 1 and e[0]["approved_by"] == "ben" and e[0]["license"] == "Pexels License"


def _final_doc_with(ep, music_file=None, broll_file=None):
    doc = editdoc.clone(editdoc.load_latest(ep, "assembly"))
    doc["stage"] = "final"
    if music_file:
        doc["tracks"]["music"].append({"id": "m1", "file": music_file, "o0": 0.0, "o1": 20.0, "in": 0.0, "gain_db": -20, "duck_db": -8,
                                       "fade_in_s": 1, "fade_out_s": 2, "license": {}})
    if broll_file:
        doc["tracks"]["overlays"].append({"id": "b1", "file": broll_file, "o0": 5.0, "o1": 7.5, "x": 0, "y": 0, "w": 640, "h": 360, "layer": 1,
                                          "kind": "broll", "alpha": False})
    editdoc.save_new_version(ep, doc)


def test_license_check_fails_for_unlogged_assets_then_passes_after_logging(synth_episode, capsys, fake_providers):
    ep = synth_episode
    track = ep.root / "work" / "assets" / "downloads" / "song.wav"
    track.parent.mkdir(parents=True)
    sf.write(track, np.zeros(4800, dtype=np.float32), 48000)
    run(capsys, "stock-search", ep.root, "hands clay", "--provider", "pexels")
    run(capsys, "stock-get", ep.root, "pexels", "3195394", "--approved-by", "ben")
    _final_doc_with(ep, music_file="work/assets/downloads/song.wav", broll_file="work/assets/downloads/pexels-3195394.mp4")
    rc, out = run(capsys, "license-check", ep.root)
    assert rc == 1 and out["ok"] is False
    assert [m["file"] for m in out["missing"]] == ["work/assets/downloads/song.wav"]
    assert [c["file"] for c in out["covered"]] == ["work/assets/downloads/pexels-3195394.mp4"]
    rc, _ = run(capsys, "log-license", ep.root, "--file", "work/assets/downloads/song.wav", "--source", "licensed-library", "--license", "Paid standard license",
                "--license-text", "Standard license, podcast use, invoice #123", "--url", "https://example.com/track/1", "--approved-by", "ben",
                "--attribution", "Music: Example Artist", "--attribution-required")
    rc, out = run(capsys, "license-check", ep.root)
    assert rc == 0 and out["ok"] is True and out["missing"] == []
    assert out["required_credits"] == ["Music: Example Artist"]
    assert any("Ada Example" in c for c in out["courtesy_credits"])


def test_license_check_detects_a_swapped_file_by_hash(synth_episode, capsys, fake_providers):
    ep = synth_episode
    run(capsys, "stock-search", ep.root, "hands clay", "--provider", "pexels")
    run(capsys, "stock-get", ep.root, "pexels", "3195394", "--approved-by", "ben")
    f = ep.root / "work/assets/downloads/pexels-3195394.mp4"
    _final_doc_with(ep, broll_file="work/assets/downloads/pexels-3195394.mp4")
    assert run(capsys, "license-check", ep.root)[0] == 0
    f.write_bytes(b"a different clip entirely")
    rc, out = run(capsys, "license-check", ep.root)
    assert rc == 1 and out["missing"]


def test_license_check_flags_a_tampered_log(synth_episode, capsys, fake_providers):
    ep = synth_episode
    run(capsys, "stock-search", ep.root, "hands clay", "--provider", "both")
    run(capsys, "stock-get", ep.root, "pexels", "3195394", "--approved-by", "ben")
    run(capsys, "stock-get", ep.root, "pixabay", "125", "--approved-by", "ben")
    _final_doc_with(ep, broll_file="work/assets/downloads/pexels-3195394.mp4")
    path = licenses.log_for(ep).path
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[1:]) + "\n")  # drop the first entry
    rc, out = run(capsys, "license-check", ep.root)
    assert rc == 1 and out["log_problems"]


def test_critique_command_exit_codes_and_list_input(tmp_path, capsys):
    good = {"id": "g", "query": "hands clay", "duration": 5, "has_audio": True,
            "scores": {"texture_light": 2, "handheld_observational": 2, "specific_over_generic": 2, "restraint": 2, "ambient_sound": 1, "no_logos_faces_unreleased": 2}}
    bad = dict(good, id="b", query="business handshake")
    f = tmp_path / "c.json"
    f.write_text(json.dumps(good))
    rc, out = run(capsys, "critique", f)
    assert rc == 0 and out["pass"] is True
    f.write_text(json.dumps([good, bad]))
    rc, out = run(capsys, "critique", f)
    assert rc == 1 and out["passed"] == 1 and [r["pass"] for r in out["results"]] == [True, False]


def test_checklist_prints_the_criteria_for_the_agent(capsys):
    rc, out = run(capsys, "checklist")
    assert rc == 0 and "texture_light" in out["criteria"] and out["hard_gates"] == ["no_logos_faces_unreleased"]


def test_pacing_command_reads_the_latest_edit(synth_episode, capsys, tmp_path):
    ep = synth_episode
    _final_doc_with(ep, broll_file="work/assets/downloads/x.mp4")
    doc = editdoc.clone(editdoc.load_latest(ep, "final"))
    doc["tracks"]["overlays"][0].update(o0=5.0, o1=5.8)  # 0.8 s: too short
    editdoc.save_new_version(ep, doc)
    rc, out = run(capsys, "pacing", ep.root)
    assert rc == 1 and out["violations"][0]["rule"] == "length" and out["edit"] == "final.v2.json"
    beats = tmp_path / "beats.json"
    beats.write_text(json.dumps([{"t0": 5.2, "t1": 5.5, "label": "laugh"}]))
    rc, out = run(capsys, "pacing", ep.root, "--beats", beats)
    assert any(v["rule"] == "emotional_beat" for v in out["violations"])


def test_music_commands_roundtrip(tmp_path, capsys):
    d = tmp_path / "lib"
    d.mkdir()
    t = np.arange(int(12 * 22050)) / 22050
    sf.write(d / "pad.wav", (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 22050)
    rc, out = run(capsys, "music-analyze", d / "pad.wav", "--duration", 8)
    assert rc == 0 and out["duration"] == pytest.approx(12, abs=0.1) and "beat_times" not in out and out["suggest"]["fits"]
    rc, out = run(capsys, "music-pick", "--dir", d, "--mood", "reflective", "--duration", 8)
    assert out["ranked"][0]["file"].endswith("pad.wav")


def test_index_search_tag_contact_sheet_commands(tmp_path, capsys):
    import subprocess
    f = tmp_path / "foot"
    f.mkdir()
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=2", "-pix_fmt", "yuv420p", str(f / "a.mp4")], check=True)
    db = tmp_path / "x.db"
    rc, out = run(capsys, "index", f, "--db", db)
    assert out["shots"] == 1 and out["face_detection"] in ("none", "haar", "yunet")
    rc, out = run(capsys, "search", "rain", "--db", db)
    assert out["count"] == 0 and "no shot has tags" in out["note"]
    sid = int(__import__("ave.assets.index", fromlist=["x"]).all_shots(db)[0]["id"])
    run(capsys, "tag", sid, "rain on glass", "--db", db)
    rc, out = run(capsys, "search", "rain glass", "--db", db)
    assert out["count"] == 1 and out["results"][0]["matched"] == ["rain", "glass"]
    rc, out = run(capsys, "contact-sheet", sid, "--db", db, "--out", tmp_path / "s.jpg")
    assert Path(out["contact_sheet"]).exists()
    with pytest.raises(SystemExit, match="999"):
        run(capsys, "contact-sheet", 999, "--db", db, "--out", tmp_path / "t.jpg")


def test_plugin_discovery_registers_assets(capsys):
    from ave.cli import build_parser
    a = build_parser().parse_args(["assets", "checklist"])
    assert a.fn is cmd.cmd_checklist
